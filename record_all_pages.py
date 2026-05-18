"""
셀러센터 "리뷰 관리" 페이지(/#/review/search)에서 가능한 모든 상호작용을 자동으로
트리거해, 이 페이지가 호출하는 모든 API를 한 번에 캡쳐한다.

사용 흐름:
1. 실행 → 브라우저 창이 열린다.
2. 사용자는 로그인만 한다 (기존 세션이 있으면 자동 복원되어 건너뜀).
3. 사용자가 "완료" 신호를 보내면 (touch /tmp/recorder_proceed.flag) 자동 캡쳐 시작.
4. 자동으로 기간(1/3/6/12개월) 검색 + 엑셀다운로드 흐름까지 트리거.
5. 끝나면 점진 저장된 데이터로 즉시 종료.

저장 위치: recordings/all_<timestamp>/
  - api.jsonl                 XHR/fetch/document 요청+응답 (body 포함, append-only)
  - nav.jsonl                 페이지 네비게이션 로그
  - downloads.jsonl           다운로드 이벤트
  - downloads/                실제 받은 파일들
  - cookies.json              최종 쿠키
  - summary.json              요약 통계
"""
import os
import sys
import time
import json
import signal
import threading
import functools
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

import auto_login
import modal_guard

load_dotenv()
print = functools.partial(print, flush=True)

PROFILE_DIR = os.environ.get("SCRAPER_PROFILE_DIR") or os.path.abspath("data/browser_profile")
OUT_DIR = Path("recordings") / f"all_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}"

# 사용자가 로그인 끝났음을 알리는 신호 파일 (외부에서 touch)
PROCEED_FLAG = Path("/tmp/recorder_proceed.flag")

# 같은 도메인 화이트리스트 — 이 안에서만 자동 크롤
ALLOW_HOSTS = (
    "sell.smartstore.naver.com",
    "smartstore.naver.com",
    "center.shopping.naver.com",
)

# URL/링크 텍스트에 이 단어가 있으면 자동 클릭 안 함 (위험 차단)
DANGER_KEYWORDS = (
    "delete", "remove", "logout", "signout", "withdraw",
    "탈퇴", "삭제", "로그아웃", "회원탈퇴", "취소",
)

# 응답 body 캡쳐 대상 — 정적 리소스 노이즈 제거
BODY_CAPTURE_TYPES = ("xhr", "fetch", "document")
BODY_SKIP_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
                       ".css", ".woff", ".woff2", ".ttf", ".ico", ".mp4")
BODY_MAX_BYTES = 8 * 1024  # 8KB per response


def _clean_locks():
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lp = os.path.join(PROFILE_DIR, lock)
        if os.path.exists(lp):
            try:
                os.remove(lp)
            except OSError:
                pass


def _is_allowed_host(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    return any(host.endswith(h) for h in ALLOW_HOSTS)


def _is_dangerous(text_or_url: str) -> bool:
    s = (text_or_url or "").lower()
    return any(kw in s for kw in DANGER_KEYWORDS)


def _safe_post_data(req):
    """req.post_data가 binary면 hex로 폴백."""
    try:
        return req.post_data
    except Exception:
        try:
            buf = req.post_data_buffer
            return ("__hex__:" + buf.hex()) if buf else None
        except Exception:
            return None


def _safe_headers(headers_obj):
    """playwright Headers → dict, decode 실패 시 안전 변환."""
    try:
        return dict(headers_obj)
    except Exception:
        out = {}
        try:
            for k in headers_obj:
                try:
                    out[k] = headers_obj.get(k)
                except Exception:
                    pass
        except Exception:
            pass
        return out


# XHR/fetch/document만 body까지 캡쳐 — 정적 리소스 제외
API_CAPTURE_TYPES = ("xhr", "fetch", "document")
API_BODY_MAX = 64 * 1024  # 64KB per response


class Recorder:
    """이벤트 핸들러로 API/nav/다운로드를 점진 저장. HAR 불필요."""

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.api_path = out_dir / "api.jsonl"
        self.nav_path = out_dir / "nav.jsonl"
        self.dl_path = out_dir / "downloads.jsonl"
        self.pages_seen = {}
        self.download_log = []
        self.api_count = 0
        self._stopped = False
        self._api_fp = open(self.api_path, "a", encoding="utf-8")
        self._nav_fp = open(self.nav_path, "a", encoding="utf-8")
        self._dl_fp = open(self.dl_path, "a", encoding="utf-8")

    def _write(self, fp, ev):
        if self._stopped:
            return
        try:
            fp.write(json.dumps(ev, ensure_ascii=False) + "\n")
            fp.flush()
        except Exception:
            pass

    def on_request_finished(self, req):
        """body가 준비된 후 발생 — res.body() 호출이 안전."""
        if self._stopped:
            return
        try:
            rt = ""
            try:
                rt = req.resource_type
            except Exception:
                pass
            if rt not in API_CAPTURE_TYPES:
                return
            try:
                res = req.response()
            except Exception:
                res = None
            if res is None:
                return

            # body — 가능한 만큼만
            body_text = None
            try:
                body = res.body()
                if body:
                    body_text = body[:API_BODY_MAX].decode("utf-8", errors="replace")
            except Exception:
                body_text = None

            ev = {
                "t": time.time(),
                "method": req.method,
                "url": req.url,
                "resource_type": rt,
                "status": res.status,
                "request_headers": _safe_headers(req.headers),
                "request_post_data": _safe_post_data(req),
                "response_headers": _safe_headers(res.headers),
                "response_body": body_text,
            }
            self.api_count += 1
            self._write(self._api_fp, ev)
        except Exception as e:
            print(f"  api 캡쳐 실패: {e}")

    def on_download(self, dl):
        if self._stopped:
            return
        try:
            ts = datetime.now().strftime("%H%M%S")
            sugg = dl.suggested_filename or "download.bin"
            path = self.out_dir / "downloads" / f"{ts}_{sugg}"
            try:
                dl.save_as(str(path))
            except Exception as e:
                print(f"  다운로드 저장 실패: {e}")
                path = None
            entry = {
                "t": time.time(),
                "suggested_filename": sugg,
                "url": dl.url,
                "saved_to": str(path) if path else None,
            }
            self.download_log.append(entry)
            self._write(self._dl_fp, entry)
            print(f"  [★ 다운로드] {sugg}  ←  {dl.url}")
        except Exception as e:
            print(f"  download 핸들러 실패: {e}")

    def on_framenav(self, frame):
        if self._stopped:
            return
        try:
            if frame.parent_frame is not None:
                return
            url = frame.url
            self.pages_seen[url] = self.pages_seen.get(url, 0) + 1
            self._write(self._nav_fp, {"t": time.time(), "url": url})
            if self.pages_seen[url] == 1:
                print(f"  [페이지] {url}")
        except Exception:
            pass

    def attach_page(self, page):
        page.on("requestfinished", self.on_request_finished)
        page.on("download", self.on_download)
        page.on("framenavigated", self.on_framenav)

    def stop(self):
        self._stopped = True
        for fp in (self._api_fp, self._nav_fp, self._dl_fp):
            try:
                fp.close()
            except Exception:
                pass


def _wait_for_proceed_signal(context):
    """사용자가 로그인 완료 후 신호를 줄 때까지 무한 대기.

    신호 = PROCEED_FLAG 파일 생성. 외부에서 `touch /tmp/recorder_proceed.flag`.
    이벤트 루프를 펌프하기 위해 context.pages[0].wait_for_timeout 사용.
    """
    # 기존 stale flag 정리
    if PROCEED_FLAG.exists():
        try:
            PROCEED_FLAG.unlink()
        except Exception:
            pass

    print("\n" + "=" * 70)
    print(">>> 브라우저에서 로그인 + 셀러센터까지 직접 이동하세요.")
    print(">>> 끝나면 대화에 '완료'라고 입력하세요. (그 외엔 무한 대기)")
    print(f">>> (수동 트리거: touch {PROCEED_FLAG})")
    print("=" * 70)

    last_log = 0
    while not PROCEED_FLAG.exists():
        # 이벤트 펌프 유지 (Playwright sync 핸들러가 동작하려면 필요)
        try:
            if context.pages:
                context.pages[0].wait_for_timeout(500)
            else:
                time.sleep(0.5)
        except Exception:
            time.sleep(0.5)
        # 1분마다 현재 탭 URL 로그
        if time.time() - last_log > 60:
            urls = []
            for p in list(context.pages):
                try:
                    urls.append(p.url)
                except Exception:
                    pass
            print(f"  [대기 중] 열린 탭: {urls}")
            last_log = time.time()

    try:
        PROCEED_FLAG.unlink()
    except Exception:
        pass
    print("\n[신호 수신] 자동 캡쳐 시작")


def _find_seller_page(context):
    """현재 열린 탭 중 셀러센터 탭을 찾는다."""
    for p in list(context.pages):
        try:
            u = (p.url or "").lower()
            if "sell.smartstore.naver.com" in u and "login" not in u and "nidlogin" not in u:
                return p
        except Exception:
            continue
    return None


REVIEW_URL = "https://sell.smartstore.naver.com/#/review/search"


def _ensure_review_page(page):
    """현재 페이지가 리뷰 관리가 아니면 강제 이동. 검색폼이 렌더될 때까지 대기."""
    try:
        cur = (page.url or "").lower()
    except Exception:
        cur = ""
    if "#/review/search" not in cur:
        try:
            page.goto(REVIEW_URL, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass
    try:
        # 검색 폼이 보일 때까지 — "초기화" 버튼 출현이 신호
        page.locator("button:has-text('초기화')").first.wait_for(state="visible", timeout=15000)
    except Exception:
        pass
    time.sleep(1)


def _exercise_review_page(page):
    """리뷰 관리 페이지에서 모든 상호작용을 자동 트리거.

    각 시도 전에 review/search로 강제 복귀 → 다른 페이지로 잘못 이동해도 회복.
    페이지네이션은 셀렉터 오작동 위험이 커서 제거 (단일 페이지 API로 충분).
    """
    print("\n=== 리뷰 관리 페이지 진입 ===")
    _ensure_review_page(page)
    time.sleep(2)

    PERIODS = ["1개월", "3개월", "6개월", "1년"]
    for period in PERIODS:
        print(f"\n--- 기간 = {period} ---")
        _ensure_review_page(page)
        try:
            page.click("button:has-text('초기화')", timeout=8000)
            time.sleep(1.2)
        except Exception as e:
            print(f"  초기화 실패: {e}")
            continue
        try:
            page.click(f"button:has-text('{period}')", timeout=6000)
            time.sleep(0.8)
        except Exception as e:
            print(f"  {period} 클릭 실패: {e}")
            continue
        try:
            page.click("button:has-text('검색')", timeout=8000)
            print(f"  검색 OK ({period})")
        except Exception as e:
            print(f"  검색 실패: {e}")
            continue
        try:
            page.locator("text=/리뷰목록\\s*\\(/").first.wait_for(state="visible", timeout=20000)
        except Exception:
            time.sleep(4)
        time.sleep(1.5)

    # 엑셀다운로드 흐름 트리거 (1년 기준)
    print("\n--- 엑셀다운로드 흐름 ---")
    _ensure_review_page(page)
    try:
        page.click("button:has-text('초기화')", timeout=8000)
        time.sleep(1.2)
        page.click("button:has-text('1년')", timeout=6000)
        time.sleep(0.8)
        page.click("button:has-text('검색')", timeout=8000)
        try:
            page.locator("text=/리뷰목록\\s*\\(/").first.wait_for(state="visible", timeout=20000)
        except Exception:
            time.sleep(6)
    except Exception as e:
        print(f"  검색 단계 실패: {e}")

    EXCEL_BTN_SELECTORS = [
        "button:has-text('엑셀다운로드')",
        "button:has-text('엑셀 다운로드')",
        "button:has-text('엑셀다운')",
        "button:has-text('엑셀 다운')",
    ]
    btn = None
    for sel in EXCEL_BTN_SELECTORS:
        try:
            cand = page.locator(sel).first
            cand.wait_for(state="visible", timeout=8000)
            btn = cand
            print(f"  엑셀다운 버튼: {sel}")
            break
        except Exception:
            continue
    if btn is None:
        print("  엑셀다운 버튼 미발견 — 건너뜀")
        return
    try:
        btn.click()
        time.sleep(3)
    except Exception as e:
        print(f"  엑셀다운 클릭 실패: {e}")
        return

    POPUP_SELS = [
        "[role='dialog'] button:has-text('확인')",
        "[role='dialog'] button:has-text('다운로드')",
        "[class*='Modal'] button:has-text('확인')",
        "[class*='modal'] button:has-text('확인')",
    ]
    for sel in POPUP_SELS:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=2000)
            loc.click()
            print(f"  팝업 확인: {sel}")
            time.sleep(2)
            break
        except Exception:
            continue
    print("  엑셀 다운로드 트리거 완료 — Recorder가 자동 수신 (30초 대기)")
    time.sleep(30)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "downloads").mkdir(parents=True, exist_ok=True)
    print(f"저장 경로: {OUT_DIR.resolve()}")

    _clean_locks()

    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=False,
        viewport={"width": 1440, "height": 900},
        accept_downloads=True,
    )
    modal_guard.install(context)
    modal_guard.attach_dialog_autoaccept(context)
    auto_login.restore_session(context)

    rec = Recorder(OUT_DIR)

    def _attach_page(p):
        rec.attach_page(p)

    for p in context.pages:
        _attach_page(p)
    context.on("page", _attach_page)

    print("=" * 70)
    print("브라우저가 열렸습니다 — 기존 세션 자동 시도 → 필요 시 로그인 후 '완료'")
    print("=" * 70)

    def _save_and_exit(signum=None, frame=None):
        print("\n캡쳐 종료, 저장 중...")
        rec.stop()
        try:
            cookies = context.cookies()
            (OUT_DIR / "cookies.json").write_text(
                json.dumps(cookies, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"  cookies.json 저장 실패: {e}")
        try:
            (OUT_DIR / "pages.json").write_text(
                json.dumps(rec.pages_seen, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"  pages.json 저장 실패: {e}")
        try:
            (OUT_DIR / "summary.json").write_text(
                json.dumps({
                    "api_count": rec.api_count,
                    "downloads": len(rec.download_log),
                    "unique_pages": len(rec.pages_seen),
                    "out_dir": str(OUT_DIR.resolve()),
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"  summary.json 저장 실패: {e}")

        print(f"\n저장 완료: {OUT_DIR.resolve()}")
        print(f"  API 캡쳐: {rec.api_count}건 / 다운로드: {len(rec.download_log)} / 페이지: {len(rec.pages_seen)}")
        if rec.download_log:
            print("\n[다운로드 목록]")
            for d in rec.download_log:
                print(f"  {d['suggested_filename']}  ←  {d['url']}")

        # context.close()가 hang하는 알려진 케이스 — 점진 저장으로 데이터는 이미 안전.
        # 5초 정도만 닫기 시도하고 안 되면 강제 종료.
        def _force_kill_after(timeout=8):
            time.sleep(timeout)
            os._exit(0)
        threading.Thread(target=_force_kill_after, args=(8,), daemon=True).start()

        try:
            context.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
        os._exit(0)

    signal.signal(signal.SIGINT, _save_and_exit)
    signal.signal(signal.SIGTERM, _save_and_exit)

    # 1) 기존 영속 세션으로 자동 진입 시도
    page = context.pages[0] if context.pages else context.new_page()
    try:
        page.bring_to_front()
    except Exception:
        pass
    try:
        page.goto(REVIEW_URL, wait_until="domcontentloaded", timeout=20000)
    except Exception:
        pass
    time.sleep(3)

    # 2) URL이 아니라 "검색 폼이 실제로 보이는지"로 세션 유효성 검사
    def _form_visible(p, timeout_ms=6000):
        try:
            p.locator("button:has-text('초기화')").first.wait_for(state="visible", timeout=timeout_ms)
            return True
        except Exception:
            return False

    if _form_visible(page, timeout_ms=6000):
        print("\n[✓] 세션 유효 — 검색 폼 확인됨, 자동 진행")
    else:
        print("\n[!] 검색 폼이 안 보임 (로그인 필요 또는 페이지 미로드)")
        print("    → 브라우저에서 로그인 + 셀러센터 진입 후 '완료' 입력")
        _wait_for_proceed_signal(context)
        # 셀러센터 탭 재탐색 + 리뷰 페이지로 이동
        seller_page = _find_seller_page(context)
        if seller_page is not None:
            page = seller_page
        try:
            page.goto(REVIEW_URL, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass
        time.sleep(2)
        if not _form_visible(page, timeout_ms=15000):
            print("  [경고] 신호 받은 후에도 검색 폼이 안 보임 — 진행 중단")
            _save_and_exit()
            return
        print("  [✓] 검색 폼 확인됨")
    print(f"[작업 탭] {page.url}")

    try:
        _exercise_review_page(page)
    except Exception as e:
        print(f"리뷰 페이지 자동 캡쳐 중 예외: {e}")

    print("\n=== 자동 캡쳐 완료 — 저장하고 종료합니다 ===")
    _save_and_exit()


if __name__ == "__main__":
    main()
