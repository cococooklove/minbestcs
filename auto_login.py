"""
네이버 스마트스토어 자동 로그인 (ID/PW 입력 자동화 + 세션 재사용)

login.py와 같은 시그니처를 유지하되 ID/PW 인자를 추가로 받는다.
세션이 유효하면 그대로 사용, 만료면 ID/PW 자동입력, 캡차/2FA 감지 시 사람에게 위임.

호출 예:
    from auto_login import main
    success, pw, context, page = main(keep_open=True, naver_id="...", naver_pw="...")
"""
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv
import os, sys, time, functools

load_dotenv()

# 백그라운드 스레드에서 print()가 즉시 보이도록 flush 강제
print = functools.partial(print, flush=True)

PROFILE_DIR = os.environ.get("SCRAPER_PROFILE_DIR") or os.path.abspath("data/browser_profile")

SELLER_HOME = "https://sell.smartstore.naver.com/"
NAVER_LOGIN = "https://nid.naver.com/nidlogin.login"

LOGIN_URL_HINTS = ("login", "nidlogin", "oauth", "signin")
INTERVENTION_URL_HINTS = (
    "nidregisterdevice",   # 새 기기 등록
    "captcha",
    "deviceconfirm",
    "otp",
    "twofactor",
    "info/help",           # 보안 알림
)


def _clean_profile_locks():
    os.makedirs(PROFILE_DIR, exist_ok=True)
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lp = os.path.join(PROFILE_DIR, lock)
        if os.path.exists(lp):
            try:
                os.remove(lp)
            except OSError:
                pass


def _is_on_seller_center(page) -> bool:
    """현재 페이지가 셀러센터(로그인 후 상태)인지 확인."""
    url = (page.url or "").lower()
    if "sell.smartstore.naver.com" not in url:
        return False
    return not any(h in url for h in LOGIN_URL_HINTS)


def _needs_human(page) -> bool:
    """캡차/2단계 인증/기기 등록 등 자동화로 처리 불가한 페이지인지."""
    url = (page.url or "").lower()
    if any(h in url for h in INTERVENTION_URL_HINTS):
        return True
    try:
        if page.locator("#captchaimg, .captcha_img, #captcha").count() > 0:
            return True
    except Exception:
        pass
    return False


def _try_session(page) -> bool:
    """저장된 세션으로 셀러센터 접근 시도. 5초 내 진입하면 True."""
    try:
        page.goto(SELLER_HOME, timeout=15000, wait_until="domcontentloaded")
    except Exception:
        pass
    for _ in range(10):
        if _is_on_seller_center(page):
            return True
        time.sleep(0.5)
    return False


def _autofill_login(page, naver_id: str, naver_pw: str) -> None:
    """네이버 로그인 페이지에서 ID/PW 입력 후 제출.

    document.execCommand('insertText')에 가까운 동작을 위해 evaluate로 값을 직접 주입.
    page.fill()은 일부 환경에서 봇으로 감지되므로 우회.
    """
    page.goto(NAVER_LOGIN, timeout=15000, wait_until="domcontentloaded")
    page.wait_for_selector("#id", timeout=10000)

    page.evaluate(
        """([id, pw]) => {
            const idEl = document.querySelector('#id');
            const pwEl = document.querySelector('#pw');
            if (idEl) { idEl.value = id; idEl.dispatchEvent(new Event('input', {bubbles: true})); }
            if (pwEl) { pwEl.value = pw; pwEl.dispatchEvent(new Event('input', {bubbles: true})); }
        }""",
        [naver_id, naver_pw],
    )
    # "로그인 상태 유지" 체크 (있을 때만)
    try:
        keep = page.locator("#keep")
        if keep.count() > 0 and not keep.is_checked():
            keep.check()
    except Exception:
        pass

    # 제출
    try:
        page.locator(".btn_login, button[type=submit], input[type=submit]").first.click(timeout=5000)
    except Exception:
        page.keyboard.press("Enter")


def _wait_after_login(page, max_seconds: int = 120) -> str:
    """로그인 제출 후 결과 판정. 반환: 'seller' | 'intervention' | 'timeout'."""
    deadline = time.time() + max_seconds
    while time.time() < deadline:
        if _is_on_seller_center(page):
            return "seller"
        if _needs_human(page):
            return "intervention"
        time.sleep(0.5)
    return "timeout"


def _wait_for_human(page, max_seconds: int = 300) -> bool:
    """사람이 캡차/2FA를 처리할 시간을 준다. 셀러센터 도달 시 True."""
    print("[auto_login] 사람의 개입이 필요합니다. 브라우저에서 처리해주세요...")
    deadline = time.time() + max_seconds
    while time.time() < deadline:
        if _is_on_seller_center(page):
            return True
        time.sleep(1)
    return False


def ensure_logged_in(page, naver_id: str = "", naver_pw: str = "",
                      headless: bool = False, timeout_per_step: int = 120) -> str:
    """이미 열려있는 페이지의 로그인 상태를 보장.

    Returns: 'seller' | 'intervention' | 'failed' | 'timeout'
    """
    naver_id = (naver_id or os.environ.get("NAVER_ID", "")).strip()
    naver_pw = (naver_pw or os.environ.get("NAVER_PW", "")).strip()

    if _is_on_seller_center(page):
        return "seller"
    if _try_session(page):
        return "seller"
    if not naver_id or not naver_pw:
        if headless:
            return "failed"
        ok = _wait_for_human(page, max_seconds=timeout_per_step * 2)
        return "seller" if ok else "timeout"
    _autofill_login(page, naver_id, naver_pw)
    result = _wait_after_login(page, max_seconds=timeout_per_step)
    if result == "intervention":
        if headless:
            return "intervention"
        ok = _wait_for_human(page, max_seconds=timeout_per_step * 2)
        return "seller" if ok else "intervention"
    return result


def main(keep_open: bool = False, naver_id: str = None, naver_pw: str = None,
         headless: bool = False, timeout_per_step: int = 120):
    """
    Returns: (success: bool, pw, context, page)
      - keep_open=False 면 context/pw/page는 None 으로 반환되고 context는 close.
      - keep_open=True 면 호출자가 직접 close 책임.
    """
    naver_id = (naver_id or os.environ.get("NAVER_ID", "")).strip()
    naver_pw = (naver_pw or os.environ.get("NAVER_PW", "")).strip()

    _clean_profile_locks()

    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=headless,
        slow_mo=0 if headless else 50,
        viewport={"width": 1440, "height": 900},
        accept_downloads=True,
    )
    page = context.pages[0] if context.pages else context.new_page()

    def _finish(success: bool):
        if keep_open and success:
            return success, pw, context, page
        try:
            context.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
        return success, None, None, None

    try:
        # 1) 저장된 세션 시도
        if _try_session(page):
            print("[auto_login] 기존 세션 유효 — 자동 로그인 생략")
            return _finish(True)

        # 2) ID/PW 없으면 사람 로그인 대기 (login.py와 동일 폴백)
        if not naver_id or not naver_pw:
            print("[auto_login] ID/PW 미입력 — 브라우저에서 수동 로그인 대기")
            ok = _wait_for_human(page, max_seconds=timeout_per_step * 2)
            return _finish(ok)

        # 3) 자동 입력
        print(f"[auto_login] 자동 로그인 시도: {naver_id}")
        _autofill_login(page, naver_id, naver_pw)

        # 4) 결과 판정
        result = _wait_after_login(page, max_seconds=timeout_per_step)
        if result == "seller":
            print("[auto_login] 자동 로그인 성공")
            return _finish(True)
        if result == "intervention":
            if headless:
                print("[auto_login] 캡차/2FA 감지 — headless 모드에서 처리 불가")
                return _finish(False)
            ok = _wait_for_human(page, max_seconds=timeout_per_step * 2)
            return _finish(ok)
        print("[auto_login] 로그인 시간 초과")
        return _finish(False)
    except Exception as e:
        print(f"[auto_login] 예외: {e}")
        return _finish(False)


if __name__ == "__main__":
    success, *_ = main()
    print(f"결과: {'성공' if success else '실패'}")
