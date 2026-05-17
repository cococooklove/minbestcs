"""
답글 등록 API 1회 캡처 — 실제로 한 건 답글을 등록하면서 네트워크 추적.

실행:
    python3 capture_post_api.py [idx] ["답글 텍스트"]
기본:
    idx=0, text="감사합니다 :)"

결과: recordings/auto_post_<timestamp>/{network.json, reply_api_candidates.json, final.png}
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
import modal_guard

load_dotenv()
print = functools.partial(print, flush=True)

PROFILE_DIR = os.environ.get("SCRAPER_PROFILE_DIR") or os.path.abspath("data/browser_profile")
OUT_DIR = Path("recordings") / f"auto_post_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}"


def main(idx: int, reply_text: str) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"저장 경로: {OUT_DIR.resolve()}")

    reviews = json.load(open("data/reviews.json", encoding="utf-8"))
    if idx >= len(reviews):
        print(f"idx {idx} 범위 밖 (전체 {len(reviews)})")
        return 1
    r = reviews[idx]
    order_no = r.get("order_no", "")
    print(f"대상: idx={idx}, order_no={order_no}, 평점={r.get('rating')}")
    print(f"  내용: {(r.get('content') or '')[:80]}")
    print(f"  답글: {reply_text!r}")

    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lp = os.path.join(PROFILE_DIR, lock)
        if os.path.exists(lp):
            try:
                os.remove(lp)
            except OSError:
                pass

    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=False,
        slow_mo=60,
        viewport={"width": 1440, "height": 900},
        accept_downloads=True,
    )
    auto_login.restore_session(context)
    modal_guard.install(context)

    net_events = []

    def _on_request(req):
        try:
            net_events.append({
                "t": time.time(), "kind": "request",
                "method": req.method, "url": req.url,
                "resource_type": req.resource_type,
                "headers": dict(req.headers),
                "post_data": req.post_data,
            })
        except Exception:
            pass

    def _on_response(res):
        try:
            net_events.append({
                "t": time.time(), "kind": "response",
                "status": res.status, "url": res.url,
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

    status = auto_login.ensure_logged_in(
        page,
        naver_id=os.environ.get("NAVER_ID", ""),
        naver_pw=os.environ.get("NAVER_PW", ""),
    )
    print(f"[로그인] {status}")
    if status != "seller":
        try: context.close()
        finally: pw.stop()
        return 1

    modal_guard.apply_now(page)  # 이미 열린 페이지에도 즉시 적용

    try:
        page.goto("https://sell.smartstore.naver.com/#/review/search", timeout=20000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        time.sleep(2)

        # 검색
        filled = False
        for ph in ("입력 후 검색하세요.", "검색어를 입력해 주세요"):
            try:
                loc = page.get_by_placeholder(ph).first
                loc.wait_for(state="visible", timeout=4000)
                loc.fill(order_no)
                page.keyboard.press("Enter")
                filled = True
                print(f"[search] {ph!r}에 {order_no} 입력 + Enter")
                break
            except Exception:
                continue
        if not filled:
            raise RuntimeError("검색 입력란 찾기 실패")

        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        time.sleep(3)

        # 검색 결과 첫 행 체크박스 선택 (일괄 답글작성을 위해)
        cb_selected = False
        for sel in (
            "table tbody tr:first-child input[type='checkbox']",
            ".seller-grid-area tbody tr:first-child input[type='checkbox']",
            "tbody input[type='checkbox']",
        ):
            try:
                cb = page.locator(sel).first
                cb.wait_for(state="visible", timeout=4000)
                cb.check()
                cb_selected = True
                print(f"[check] 첫 행 체크박스 선택: {sel}")
                break
            except Exception:
                continue
        if not cb_selected:
            print("[warn] 체크박스 못 찾음 — 그대로 진행")
        time.sleep(1)

        # 답글작성 (일괄) 클릭 — openBulkUpdateCommentModal
        reply_btn = page.locator("button:has-text('답글작성')").first
        reply_btn.wait_for(state="visible", timeout=8000)
        reply_btn.click()
        print("[click] 답글작성 (일괄)")
        time.sleep(2)

        # 모달 textarea
        textarea = page.locator("[role='dialog'] textarea, .modal textarea").last
        textarea.wait_for(state="visible", timeout=6000)
        textarea.click()
        textarea.fill(reply_text)
        print(f"[fill] textarea ← {reply_text!r}")
        time.sleep(0.5)

        # 등록 버튼
        submitted = False
        for sel in (
            "[role='dialog'] button:has-text('등록')",
            ".modal button:has-text('등록')",
            "button:has-text('등록')",
            ".modal button[class*='primary']",
        ):
            try:
                btn = page.locator(sel).first
                btn.wait_for(state="visible", timeout=3000)
                btn.click()
                print(f"[click] 등록 ({sel})")
                submitted = True
                break
            except Exception:
                continue
        if not submitted:
            print("[warn] 등록 버튼 못 찾음")

        # API 호출 대기
        time.sleep(5)
        print("[done] 답글 등록 완료 (예상)")

    except Exception as e:
        print(f"[error] {e}")
        try:
            page.screenshot(path=str(OUT_DIR / "error.png"), full_page=True)
            (OUT_DIR / "error_dom.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
    finally:
        try:
            (OUT_DIR / "network.json").write_text(
                json.dumps(net_events, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            page.screenshot(path=str(OUT_DIR / "final.png"), full_page=True)
        except Exception:
            pass

        # 답글 후보 추출
        candidates = []
        for e in net_events:
            if e.get("kind") != "request":
                continue
            if e.get("method") not in ("POST", "PUT", "PATCH"):
                continue
            u = e.get("url", "")
            if "sell.smartstore" not in u:
                continue
            body = e.get("post_data") or ""
            url_l = u.lower()
            if (
                reply_text in body
                or any(k in body.lower() for k in ("reply", "comment", "answerresponse", "answer"))
                or any(k in url_l for k in ("reply", "comment", "answer"))
            ):
                candidates.append(e)

        (OUT_DIR / "reply_api_candidates.json").write_text(
            json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n=== 답글 등록 API 후보: {len(candidates)}건 ===")
        for c in candidates:
            body = (c.get("post_data") or "")[:200]
            print(f"  {c['method']:6s} {c['url']}")
            if body:
                print(f"     body: {body}")

        try:
            context.close()
        finally:
            pw.stop()

    return 0


if __name__ == "__main__":
    idx = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    text = sys.argv[2] if len(sys.argv) > 2 else "감사합니다 :)"
    sys.exit(main(idx, text))
