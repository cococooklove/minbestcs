"""
스마트스토어 셀러센터 리뷰 관리 페이지 베이스라인 캡처.

세 가지를 한 번에 기록한다:
  1. 네트워크 로그 (모든 요청·응답 — network.json)
  2. DOM (각 단계의 HTML 전체)
  3. 스크린샷 (각 단계의 전체 화면)

결과: baselines/<YYYY-MM-DD_HHMMSS>/{network.json, dom/*.html, shot/*.png, visible_buttons.txt}

실행:
    python3 capture_baseline.py

세션이 살아 있으면 즉시 진행. 만료 시 OAuth popup의 QR 코드 탭이 자동으로 열리고
사용자가 모바일 네이버 앱으로 스캔하면 진행됨.
"""
import os
import sys
import time
import json
import functools
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

import auto_login
import ui_selectors as S

load_dotenv()
print = functools.partial(print, flush=True)

PROFILE_DIR = os.environ.get("SCRAPER_PROFILE_DIR") or os.path.abspath("data/browser_profile")
BASE_DIR = Path("baselines") / datetime.now().strftime("%Y-%m-%d_%H%M%S")


def _clean_profile_locks():
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lp = os.path.join(PROFILE_DIR, lock)
        if os.path.exists(lp):
            try:
                os.remove(lp)
            except OSError:
                pass


def _save_step(page, name: str):
    """현재 페이지를 DOM + 스크린샷으로 저장."""
    (BASE_DIR / "dom").mkdir(parents=True, exist_ok=True)
    (BASE_DIR / "shot").mkdir(parents=True, exist_ok=True)
    try:
        (BASE_DIR / "dom" / f"{name}.html").write_text(page.content(), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠ DOM 저장 실패({name}): {e}")
    try:
        page.screenshot(path=str(BASE_DIR / "shot" / f"{name}.png"), full_page=True)
    except Exception as e:
        print(f"  ⚠ 스크린샷 실패({name}): {e}")
    print(f"  → {name} 저장")


def main():
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"베이스라인 저장 경로: {BASE_DIR.resolve()}")

    _clean_profile_locks()

    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=False,
        viewport={"width": 1440, "height": 900},
        accept_downloads=True,
    )
    # 저장된 세션 복원 (persistent profile에 더해서 cookies 명시 주입)
    auto_login.restore_session(context)

    # 네트워크 로그 수집 (record_har_path 가 persistent context에서 silent fail하므로 직접 수집)
    net_events = []

    def _on_request(req):
        try:
            net_events.append({
                "t": time.time(),
                "kind": "request",
                "method": req.method,
                "url": req.url,
                "resource_type": req.resource_type,
                "headers": dict(req.headers),
            })
        except Exception:
            pass

    def _on_response(res):
        try:
            ct = res.headers.get("content-type", "")
            net_events.append({
                "t": time.time(),
                "kind": "response",
                "status": res.status,
                "url": res.url,
                "content_type": ct,
                "headers": dict(res.headers),
            })
        except Exception:
            pass

    def _attach(p):
        p.on("request", _on_request)
        p.on("response", _on_response)

    for p in context.pages:
        _attach(p)
    context.on("page", _attach)  # popup 포함 새 페이지 자동 attach

    page = context.pages[0] if context.pages else context.new_page()

    try:
        # 1. 로그인 보장
        print("[1] 로그인 보장")
        status = auto_login.ensure_logged_in(
            page,
            naver_id=os.environ.get("NAVER_ID", ""),
            naver_pw=os.environ.get("NAVER_PW", ""),
        )
        if status != "seller":
            print(f"  ✗ 로그인 실패: {status}")
            return 1
        print("  ✓ 로그인 OK")

        # 2. 셀러센터 홈
        print("[2] 셀러센터 홈")
        page.goto(S.SELLER_HOME, wait_until="domcontentloaded", timeout=20000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        time.sleep(1)
        _save_step(page, "01_seller_home")

        # 3. 리뷰 검색 페이지 진입
        print("[3] 리뷰 검색 페이지 진입")
        page.goto(S.REVIEW_SEARCH, wait_until="domcontentloaded", timeout=20000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        time.sleep(3)

        # 검색 페이지에서 로그인 페이지로 리다이렉트됐는지 점검 → 자동 재로그인
        cur = (page.url or "").lower()
        title = ""
        try:
            title = page.title()
        except Exception:
            pass
        print(f"  [debug] 현재 URL: {page.url}")
        print(f"  [debug] 현재 title: {title}")
        # title 기반으로도 로그인 페이지 감지 (iframe/SPA navigation 대응)
        is_login_page = (
            any(h in cur for h in S.LOGIN_URL_HINTS)
            or "커머스 id" in title.lower()
            or "로그인" in title
        )
        if is_login_page:
            print(f"  ⚠ 로그인 페이지로 리다이렉트됨 — 자동 재로그인 시도")
            status = auto_login.ensure_logged_in(
                page,
                naver_id=os.environ.get("NAVER_ID", ""),
                naver_pw=os.environ.get("NAVER_PW", ""),
            )
            if status != "seller":
                print(f"  ✗ 재로그인 실패: {status}")
                _save_step(page, "02_review_search_relogin_failed")
                return 1
            print("  ✓ 재로그인 OK — 검색 페이지 재진입")
            page.goto(S.REVIEW_SEARCH, wait_until="domcontentloaded", timeout=20000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            time.sleep(3)

        _save_step(page, "02_review_search_initial")

        # 4. 초기화 → 1년 → 검색
        print("[4] 초기화 / 1년 / 검색")
        try:
            page.click(S.REVIEW_PAGE["reset_btn"], timeout=8000)
            time.sleep(0.5)
            page.click(S.REVIEW_PAGE["period_1year"], timeout=8000)
            time.sleep(0.5)
            page.click(S.REVIEW_PAGE["search_btn"], timeout=8000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            time.sleep(2)
            _save_step(page, "03_review_search_results")
        except Exception as e:
            print(f"  ⚠ 검색 단계 실패: {e}")
            _save_step(page, "03_review_search_failed")

        # 5. 페이지에 보이는 버튼/입력 셀렉터 기록 (UI 변경 감지용)
        try:
            visible_btns = page.locator(S.REVIEW_PAGE["visible_buttons"]).all_text_contents()
            (BASE_DIR / "visible_buttons.txt").write_text(
                "\n".join(visible_btns), encoding="utf-8"
            )
            print(f"  → 보이는 버튼 {len(visible_btns)}개 기록")
        except Exception as e:
            print(f"  ⚠ 버튼 목록 기록 실패: {e}")

        # 네트워크 로그 저장
        try:
            (BASE_DIR / "network.json").write_text(
                json.dumps(net_events, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"  → 네트워크 이벤트 {len(net_events)}건 기록")
        except Exception as e:
            print(f"  ⚠ network.json 저장 실패: {e}")

        print()
        print("캡처 완료. 결과:")
        print(f"  NET:    {BASE_DIR / 'network.json'}")
        print(f"  DOM:    {BASE_DIR / 'dom'}/*.html")
        print(f"  SHOT:   {BASE_DIR / 'shot'}/*.png")
        print(f"  버튼:    {BASE_DIR / 'visible_buttons.txt'}")
        return 0
    except Exception as e:
        print(f"예외: {e}")
        return 1
    finally:
        # HAR은 context.close() 시점에 flush
        try:
            context.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
