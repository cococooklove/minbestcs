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
import os, sys, time, json, functools

load_dotenv()

# 백그라운드 스레드에서 print()가 즉시 보이도록 flush 강제
print = functools.partial(print, flush=True)

PROFILE_DIR = os.environ.get("SCRAPER_PROFILE_DIR") or os.path.abspath("data/browser_profile")
SESSION_STATE_PATH = os.environ.get("SESSION_STATE_PATH") or os.path.abspath("data/session_state.json")

SELLER_HOME = "https://sell.smartstore.naver.com/"
# 세션 유효성은 '보호된' 페이지로 확인해야 한다. 홈(/)은 미인증에도 일부 진입 가능하지만
# 리뷰 페이지는 commerce ID 재인증을 요구하므로 정확한 검증점이 된다.
SELLER_VERIFY = "https://sell.smartstore.naver.com/#/review/search"
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


def restore_session(context) -> bool:
    """저장된 cookies/localStorage를 context에 주입. 파일 없거나 실패 시 False."""
    if not os.path.exists(SESSION_STATE_PATH):
        return False
    try:
        with open(SESSION_STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
        cookies = state.get("cookies", [])
        if cookies:
            context.add_cookies(cookies)
            print(f"[auto_login.session] cookies {len(cookies)}개 복원")
        # localStorage는 origin별로 page에서 복원해야 하므로 여기서는 cookies만
        return True
    except Exception as e:
        print(f"[auto_login.session] 복원 실패: {e}")
        return False


def save_session(context) -> None:
    """현재 context의 cookies + localStorage를 파일에 저장."""
    try:
        os.makedirs(os.path.dirname(SESSION_STATE_PATH), exist_ok=True)
        context.storage_state(path=SESSION_STATE_PATH)
        print(f"[auto_login.session] 저장 완료: {SESSION_STATE_PATH}")
    except Exception as e:
        print(f"[auto_login.session] 저장 실패: {e}")


def _is_on_seller_center(page) -> bool:
    """현재 페이지가 셀러센터(로그인 후 상태)인지 확인.

    URL뿐 아니라 title도 함께 본다. SPA 라우팅 중 잠깐 셀러센터 도메인에 머무는
    'false positive' 구간을 거르기 위해.
    """
    url = (page.url or "").lower()
    if "sell.smartstore.naver.com" not in url:
        return False
    if any(h in url for h in LOGIN_URL_HINTS):
        return False
    try:
        title = (page.title() or "")
        if "커머스 ID" in title or title.strip() == "로그인":
            return False
    except Exception:
        pass
    return True


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
    """저장된 세션으로 '보호된 페이지'(리뷰 검색)에 접근되는지로 판정한다.

    홈(/)은 부분 세션에서도 진입 가능해 false positive가 나기 쉬우므로 사용 안 함.
    """
    try:
        page.goto(SELLER_VERIFY, timeout=15000, wait_until="domcontentloaded")
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    time.sleep(1)
    return _is_on_seller_center(page)


def _open_qr_tab(target) -> bool:
    """nid 로그인 페이지에서 'QR코드' 탭으로 전환. 성공하면 True."""
    for sel in (
        "a:has-text('QR코드')",
        "button:has-text('QR코드')",
        "[role='tab']:has-text('QR')",
        "li:has-text('QR코드') a",
    ):
        try:
            target.locator(sel).first.click(timeout=3000)
            print(f"[auto_login.qr] QR 탭 클릭 성공: {sel}")
            return True
        except Exception:
            continue
    print("[auto_login.qr] QR 탭 셀렉터 모두 실패")
    return False


# nid 로그인 popup을 minimal하게 — QR 영역만 남기고 헤더/탭/풋터/광고/링크 모두 숨김
_POPUP_MINIMAL_CSS = """
/* 헤더, 탭, 풋터, 광고, 보조 링크 숨김 */
header, nav, footer,
.header, .footer, .gnb, .lnb, .navbar, .nav_area,
[class*="banner" i], [class*="advert" i], [class*="footer" i], [class*="header" i],
[class*="membership" i], [class*="benefit" i], [class*="recommend" i],
.login_tab_area, .login_type, [role="tablist"], ul.tab, .tab_login, .tab_box, .login_tab,
.find_box, .help_box, .link_box, .link_login, .login_link,
[class*="find_" i], [class*="login_help"],
.go_login, .find_pw, .find_id, .join {
    display: none !important;
}
/* body 여백 최소화 */
html, body {
    margin: 0 !important;
    padding: 16px !important;
    background: #fff !important;
    height: auto !important;
    min-height: 0 !important;
}
body { overflow: hidden !important; }
/* 메인 컨테이너 padding 제거 */
.wrap, .content, .container, main, .main, [class*="content" i] {
    padding: 0 !important;
    margin: 0 !important;
    min-height: 0 !important;
}
"""


def _make_popup_minimal(popup, width: int = 400, height: int = 480) -> None:
    """popup의 viewport를 작게 + CSS로 QR 영역만 남김.

    screenshot(full_page=False)는 viewport 크기로 캡처되므로 viewport를 작게 만들면
    캡처 이미지도 그만큼 작고 빈 공간이 안 생긴다.
    """
    # 1) viewport 크기 조정 — 캡처 크기에 영향
    try:
        popup.set_viewport_size({"width": width, "height": height})
        print(f"[auto_login.qr] popup viewport {width}x{height}")
    except Exception as e:
        print(f"[auto_login.qr] viewport 변경 실패: {e}")

    # 2) CSS 주입 — 헤더/탭/풋터/광고/링크 숨김
    try:
        popup.add_style_tag(content=_POPUP_MINIMAL_CSS)
        print("[auto_login.qr] popup 최소화 CSS 주입")
    except Exception as e:
        print(f"[auto_login.qr] CSS 주입 실패: {e}")


def _extract_qr_data(popup) -> dict:
    """popup에서 QR 이미지 + 안내문 이미지(네이버 원본) + 남은시간 + 인증번호 추출.

    Returns: {"qr_image": bytes|None, "guide_image": bytes|None, "time_left": str|None, "code": str|None}
    """
    data = {"qr_image": None, "guide_image": None, "time_left": None, "code": None}

    # QR 이미지 — canvas 우선, 없으면 img
    for sel in ("canvas", "img[src*='qr' i]", "img[alt*='QR' i]", "[class*='qr'] canvas", "[class*='qr'] img"):
        try:
            loc = popup.locator(sel).first
            if loc.count() == 0:
                continue
            bb = loc.bounding_box()
            if not bb or bb["width"] < 40:
                continue
            data["qr_image"] = loc.screenshot()
            break
        except Exception:
            continue

    # 안내문 영역 — "네이버 앱"/"렌즈" 텍스트 포함하는 가장 안쪽 컨테이너의 bounding box
    try:
        guide_box = popup.evaluate(
            r"""() => {
                const elems = document.querySelectorAll('div, p, ul, section');
                let best = null;
                let bestArea = Infinity;
                for (const el of elems) {
                    const t = el.innerText || '';
                    if (!t.includes('네이버 앱') || !t.includes('렌즈')) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 50 || r.height < 30) continue;
                    const area = r.width * r.height;
                    if (area < bestArea) { best = r; bestArea = area; }
                }
                if (!best) return null;
                // 약간의 여백
                return {x: Math.max(0, best.left - 4), y: Math.max(0, best.top - 4),
                        width: best.width + 8, height: best.height + 8};
            }"""
        )
        if guide_box and guide_box.get("width", 0) > 50:
            data["guide_image"] = popup.screenshot(clip=guide_box)
    except Exception as e:
        print(f"[auto_login.qr] 안내문 영역 캡처 실패: {e}")

    # 남은시간 + 인증번호 텍스트
    try:
        info = popup.evaluate(
            r"""() => {
                const txt = (document.body && document.body.innerText) || "";
                const t = txt.match(/(\d{2}분\s*\d{1,2}초)/);
                const c = txt.match(/숫자\s*중\s*(\d{1,3})/) || txt.match(/(\d{2,3})\s*를\s*선택/);
                return {
                    time: t ? t[1].replace(/\s+/g, ' ') : null,
                    code: c ? c[1] : null,
                };
            }"""
        )
        data["time_left"] = info.get("time")
        data["code"] = info.get("code")
    except Exception:
        pass

    return data


def _wait_for_popup_close(popup, max_seconds: int = 300, on_qr=None, poll_interval: float = 1.0) -> bool:
    """popup이 닫히기를 대기. 사용자가 QR 스캔/캡차 처리 등 직접 작업할 시간.

    on_qr: 콜백(dict). 주기적으로 popup에서 QR 데이터 추출해 호출.
    """
    print(f"[auto_login.qr] popup 종료 대기 중 (최대 {max_seconds}s — QR 스캔/직접 로그인)...")
    deadline = time.time() + max_seconds
    while time.time() < deadline:
        try:
            if popup.is_closed():
                print("[auto_login.qr] popup 종료 감지 — 로그인 진행됨")
                return True
        except Exception:
            print("[auto_login.qr] popup 접근 불가 (이미 닫힘)")
            return True
        if on_qr is not None:
            try:
                data = _extract_qr_data(popup)
                on_qr(data)
            except Exception as e:
                print(f"[auto_login.qr] 추출 실패: {e}")
        time.sleep(poll_interval)
    print("[auto_login.qr] popup 종료 대기 시간 초과")
    return False


def _autofill_login(page, naver_id: str = "", naver_pw: str = "", on_qr=None) -> None:
    """네이버 로그인 흐름 시작. QR 탭을 열고 사용자가 모바일로 스캔할 때까지 대기.

    - accounts.commerce.naver.com 페이지면 '네이버 아이디로 로그인' 탭 → OAuth popup → QR 탭 → 대기
    - 이미 nid.naver.com 페이지면 거기서 QR 탭 → 대기
    - 그 외엔 nid로 직접 이동 → QR 탭 → 대기

    naver_id/naver_pw는 현재 사용하지 않지만(QR 방식), 향후 fallback용으로 시그니처 유지.
    """
    cur = (page.url or "").lower()
    print(f"[auto_login.qr] 진입 URL={page.url}")

    if "nid.naver.com" in cur:
        _open_qr_tab(page)
        _wait_for_popup_close(page, on_qr=on_qr)  # page 자체를 대기 (단일 페이지 흐름)
        return

    if "accounts.commerce.naver.com" in cur:
        # OAuth popup 띄움
        try:
            with page.expect_popup(timeout=15000) as popup_info:
                clicked = False
                for sel in (
                    "button:has-text('네이버 아이디로 로그인')",
                    "[class*='Login_btn_more']",
                ):
                    try:
                        page.locator(sel).first.click(timeout=5000)
                        print(f"[auto_login.qr] 탭 클릭 성공: {sel}")
                        clicked = True
                        break
                    except Exception:
                        continue
                if not clicked:
                    raise RuntimeError("'네이버 아이디로 로그인' 탭 클릭 실패")
            popup = popup_info.value
            try:
                popup.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            print(f"[auto_login.qr] OAuth popup URL={popup.url}")
            _open_qr_tab(popup)
            time.sleep(0.8)  # QR 탭 렌더링 대기
            _make_popup_minimal(popup)
            _wait_for_popup_close(popup, on_qr=on_qr)
            return
        except Exception as e:
            print(f"[auto_login.qr] popup 흐름 실패: {e} — nid 직접 이동 폴백")
            page.goto(NAVER_LOGIN, timeout=15000, wait_until="domcontentloaded")
            _open_qr_tab(page)
            time.sleep(2)
            return

    # 폴백
    print("[auto_login.qr] 알 수 없는 페이지 — nid 직접 이동")
    page.goto(NAVER_LOGIN, timeout=15000, wait_until="domcontentloaded")
    _open_qr_tab(page)
    time.sleep(2)


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
                      headless: bool = False, timeout_per_step: int = 120, on_qr=None) -> str:
    """이미 열려있는 페이지의 로그인 상태를 보장.

    on_qr: bytes 콜백. popup이 떴을 때 주기적으로 화면을 캡처해 호출. 외부 UI에 QR 표시 용.
    Returns: 'seller' | 'intervention' | 'failed' | 'timeout'
    """
    naver_id = (naver_id or os.environ.get("NAVER_ID", "")).strip()
    naver_pw = (naver_pw or os.environ.get("NAVER_PW", "")).strip()
    print(f"[auto_login.ensure] 시작 URL={page.url}")

    if _is_on_seller_center(page):
        print("[auto_login.ensure] 이미 셀러센터")
        save_session(page.context)
        return "seller"

    print("[auto_login.ensure] _try_session() 호출")
    ok = _try_session(page)
    print(f"[auto_login.ensure] _try_session={ok}, URL={page.url}, title={_safe_title(page)}")
    if ok:
        save_session(page.context)
        return "seller"

    # headless에서 on_qr 콜백이 없으면 popup 인증 불가
    if headless and on_qr is None:
        print("[auto_login.ensure] 세션 만료 + headless + on_qr 없음 → 즉시 실패")
        return "failed"

    if not naver_id or not naver_pw:
        if headless:
            return "failed"
        print("[auto_login.ensure] ID/PW 없음 → 사람 대기")
        ok = _wait_for_human(page, max_seconds=timeout_per_step * 2)
        return "seller" if ok else "timeout"

    print(f"[auto_login.ensure] 자동입력 시도 (id={naver_id}, on_qr={'yes' if on_qr else 'no'})")
    _autofill_login(page, naver_id, naver_pw, on_qr=on_qr)
    print(f"[auto_login.ensure] 자동입력 직후 URL={page.url}")

    result = _wait_after_login(page, max_seconds=timeout_per_step)
    print(f"[auto_login.ensure] _wait_after_login={result}, URL={page.url}, title={_safe_title(page)}")

    if result == "seller":
        save_session(page.context)
        return "seller"
    if result == "intervention":
        if headless:
            return "intervention"
        ok = _wait_for_human(page, max_seconds=timeout_per_step * 2)
        if ok:
            save_session(page.context)
            return "seller"
        return "intervention"
    return result


def _safe_title(page) -> str:
    try:
        return page.title() or ""
    except Exception:
        return "?"


def main(keep_open: bool = False, naver_id: str = None, naver_pw: str = None,
         headless: bool = False, timeout_per_step: int = 120, on_qr=None):
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

    # headful일 때 메인창은 화면 밖 + 1픽셀 — popup(OAuth)만 사용자에게 보이도록
    if not headless:
        try:
            cdp = context.new_cdp_session(page)
            wi = cdp.send("Browser.getWindowForTarget")
            cdp.send("Browser.setWindowBounds", {
                "windowId": wi["windowId"],
                "bounds": {"left": -10000, "top": -10000, "width": 1, "height": 1},
            })
            print("[auto_login] 메인창 화면 밖 이동")
        except Exception as e:
            print(f"[auto_login] 메인창 hide 실패: {e}")

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
            save_session(context)
            return _finish(True)

        # 2) headless + on_qr 콜백 없으면 popup 인증 불가
        if headless and on_qr is None:
            print("[auto_login] 세션 만료 + headless + on_qr 없음 → 즉시 실패")
            return _finish(False)

        # 3) ID/PW 없으면 사람 로그인 대기
        if not naver_id or not naver_pw:
            if headless:
                return _finish(False)
            print("[auto_login] ID/PW 미입력 — 브라우저에서 수동 로그인 대기")
            ok = _wait_for_human(page, max_seconds=timeout_per_step * 2)
            return _finish(ok)

        # 4) 자동 입력 (popup은 on_qr 콜백으로 외부 UI에 표시)
        print(f"[auto_login] 자동 로그인 시도: {naver_id}, on_qr={'yes' if on_qr else 'no'}")
        _autofill_login(page, naver_id, naver_pw, on_qr=on_qr)

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
