"""
사용자가 직접 답글을 다는 흐름을 캡처해서 진짜 API/셀렉터를 알아낸다.

실행:
    python3 record_reply_flow.py

브라우저가 열리면:
1. 자동 로그인 (세션 있으면 즉시 통과)
2. 리뷰 검색 페이지로 이동
3. 사용자가 직접: 주문번호 검색 → 답글작성 → 텍스트 입력 → 등록
4. 답글이 게시되면 이 터미널에서 Ctrl+C → 모든 데이터 저장

저장 위치: recordings/<timestamp>/
  - network.json           모든 요청/응답
  - reply_api_candidates.json  답글 등록으로 추정되는 POST/PUT 요청만
  - final_dom.html         최종 화면 DOM
  - final_shot.png         최종 화면 스크린샷
"""
import os
import sys
import time
import json
import signal
import functools
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

import auto_login

load_dotenv()
print = functools.partial(print, flush=True)

PROFILE_DIR = os.environ.get("SCRAPER_PROFILE_DIR") or os.path.abspath("data/browser_profile")
OUT_DIR = Path("recordings") / datetime.now().strftime("%Y-%m-%d_%H%M%S")


def _clean_locks():
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lp = os.path.join(PROFILE_DIR, lock)
        if os.path.exists(lp):
            try:
                os.remove(lp)
            except OSError:
                pass


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"저장 경로: {OUT_DIR.resolve()}")

    _clean_locks()

    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=False,
        viewport={"width": 1440, "height": 900},
        accept_downloads=True,
    )
    auto_login.restore_session(context)

    # 네트워크 이벤트 수집
    net_events = []

    def _on_request(req):
        try:
            entry = {
                "t": time.time(),
                "kind": "request",
                "method": req.method,
                "url": req.url,
                "resource_type": req.resource_type,
                "headers": dict(req.headers),
                "post_data": req.post_data,
            }
            net_events.append(entry)
            # 답글 관련 요청은 콘솔에 즉시 출력 (사용자가 어떤 API인지 실시간 확인)
            if req.method in ("POST", "PUT", "DELETE", "PATCH") and "review" in req.url.lower():
                print(f"\n[★ 답글 후보 요청] {req.method} {req.url}")
                if req.post_data:
                    body = req.post_data[:500]
                    print(f"  body: {body}")
        except Exception:
            pass

    def _on_response(res):
        try:
            net_events.append({
                "t": time.time(),
                "kind": "response",
                "status": res.status,
                "url": res.url,
                "content_type": res.headers.get("content-type", ""),
            })
        except Exception:
            pass

    def _attach(p):
        p.on("request", _on_request)
        p.on("response", _on_response)

    for p in context.pages:
        _attach(p)
    context.on("page", _attach)

    page = context.pages[0] if context.pages else context.new_page()

    print("=" * 70)
    print("브라우저가 열렸습니다.")
    print("1. (자동) 셀러센터 로그인 + 리뷰 검색 페이지 이동")
    print("2. (사용자) 주문번호 검색 → 답글작성 → 텍스트 입력 → 등록")
    print("3. (사용자) 답글 등록 확인 후 이 터미널에서 Ctrl+C")
    print("=" * 70)

    # 로그인 보장
    status = auto_login.ensure_logged_in(
        page,
        naver_id=os.environ.get("NAVER_ID", ""),
        naver_pw=os.environ.get("NAVER_PW", ""),
    )
    print(f"[로그인] status={status}, url={page.url}")

    if status != "seller":
        print("로그인 실패. 종료.")
        try:
            context.close()
        finally:
            pw.stop()
        return 1

    # 리뷰 검색 페이지 진입
    try:
        page.goto("https://sell.smartstore.naver.com/#/review/search", timeout=20000)
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    print()
    print(">>> 이제 답글을 직접 작성하세요. 끝나면 Ctrl+C <<<")
    print()

    def _save_and_exit(signum=None, frame=None):
        print("\n캡처 종료, 저장 중...")
        # 전체 네트워크
        try:
            (OUT_DIR / "network.json").write_text(
                json.dumps(net_events, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"  network.json 저장 실패: {e}")
        # 답글 등록으로 추정되는 요청만 추출
        candidates = [
            e for e in net_events
            if e.get("kind") == "request"
            and e.get("method") in ("POST", "PUT", "PATCH")
            and "review" in (e.get("url") or "").lower()
        ]
        try:
            (OUT_DIR / "reply_api_candidates.json").write_text(
                json.dumps(candidates, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"  reply_api_candidates.json 저장 실패: {e}")
        # 최종 DOM/스크린샷
        try:
            (OUT_DIR / "final_dom.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        try:
            page.screenshot(path=str(OUT_DIR / "final_shot.png"), full_page=True)
        except Exception:
            pass

        print(f"\n저장 완료: {OUT_DIR.resolve()}")
        print(f"  네트워크 이벤트: {len(net_events)}건")
        print(f"  답글 API 후보:   {len(candidates)}건")
        if candidates:
            print("\n[요약] 답글 API 후보:")
            for c in candidates:
                print(f"  {c['method']:6s} {c['url']}")
        try:
            context.close()
        finally:
            pw.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _save_and_exit)
    signal.signal(signal.SIGTERM, _save_and_exit)

    # 무한 대기 — page.wait_for_timeout으로 Playwright 이벤트 펌프 유지
    # (time.sleep만 쓰면 sync API의 콜백이 막힘 — 네트워크 이벤트 안 잡힘)
    while True:
        try:
            page.wait_for_timeout(500)
        except Exception:
            # 페이지가 닫혔거나 컨텍스트 끊김
            break


if __name__ == "__main__":
    main()
