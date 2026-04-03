"""
스마트스토어 셀러센터에 답글 자동 게시
- post_reply(idx): 특정 리뷰 1건 게시
- 실행: python3 reply_poster.py <idx>
"""
import json, os, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

REVIEWS_FILE = "data/reviews.json"
PROFILE_DIR = os.path.abspath("data/browser_profile")
POST_PROGRESS_FILE = "data/post_progress.json"


def write_post_progress(step, success=None, error=None):
    with open(POST_PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({"step": step, "success": success, "error": error, "running": step != "완료"}, f)


def load_reviews():
    with open(REVIEWS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_reviews(reviews):
    with open(REVIEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(reviews, f, ensure_ascii=False, indent=2)


def post_reply(idx: int) -> dict:
    """단일 리뷰 idx에 대해 셀러센터에 답글 게시. 결과 dict 반환."""
    reviews = load_reviews()
    if idx >= len(reviews):
        return {"ok": False, "error": "리뷰를 찾을 수 없습니다."}

    r = reviews[idx]
    reply_text = r.get("ai_reply", "").strip()
    order_no = r.get("order_no", "").strip()

    if not reply_text:
        return {"ok": False, "error": "게시할 답글 내용이 없습니다."}
    if not order_no:
        return {"ok": False, "error": "주문번호(order_no)가 없어 자동 게시 불가합니다."}
    if r.get("replied"):
        return {"ok": False, "error": "이미 답글이 달린 리뷰입니다."}

    # 락 파일 정리
    for lock in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        lp = os.path.join(PROFILE_DIR, lock)
        if os.path.exists(lp):
            os.remove(lp)

    write_post_progress("브라우저 시작 중")
    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=False,
        slow_mo=80,
        viewport={"width": 1440, "height": 900},
    )
    result = {"ok": False, "error": ""}
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.on("dialog", lambda d: d.accept())

        write_post_progress("셀러센터 이동 중")
        page.goto("https://sell.smartstore.naver.com/#/review/search", timeout=20000)
        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(3)

        # 로그인 확인
        if any(x in page.url.lower() for x in ("login", "nidlogin", "oauth", "signin")):
            result["error"] = "로그인이 필요합니다. 먼저 네이버 로그인을 완료하세요."
            return result

        write_post_progress(f"주문번호 {order_no} 검색 중")

        # 주문번호 검색 입력
        try:
            # 검색 유형 선택 (주문번호)
            search_type_sel = page.locator("select").first
            search_type_sel.select_option(label="상품주문번호") if search_type_sel else None
        except Exception:
            pass

        try:
            search_input = page.get_by_placeholder("검색어를 입력해 주세요").first
            search_input.fill(order_no)
            page.keyboard.press("Enter")
            page.wait_for_load_state("networkidle", timeout=10000)
            time.sleep(2)
        except Exception as e:
            result["error"] = f"검색 입력 실패: {e}"
            return result

        write_post_progress("답글작성 버튼 찾는 중")

        # 답글작성 버튼 찾기 (해당 행)
        try:
            reply_btn = page.get_by_text("답글작성").first
            reply_btn.wait_for(state="visible", timeout=8000)
            reply_btn.click()
            time.sleep(1)
        except Exception as e:
            result["error"] = f"답글작성 버튼을 찾을 수 없습니다: {e}"
            return result

        write_post_progress("답글 입력 중")

        # 답글 텍스트 입력
        try:
            textarea = page.locator("textarea").last
            textarea.wait_for(state="visible", timeout=5000)
            textarea.click()
            textarea.fill(reply_text)
            time.sleep(0.5)
        except Exception as e:
            result["error"] = f"텍스트 입력 실패: {e}"
            return result

        # 등록/확인 버튼 클릭
        for sel in [
            "button:has-text('등록')",
            "button:has-text('확인')",
            "button:has-text('저장')",
            "[class*='btn'][class*='primary']",
        ]:
            try:
                btn = page.wait_for_selector(sel, timeout=3000, state="visible")
                if btn:
                    btn.click()
                    time.sleep(2)
                    break
            except Exception:
                continue

        write_post_progress("완료", success=True)
        result["ok"] = True

        # reviews.json 업데이트
        reviews[idx]["replied"] = True
        reviews[idx]["reply_status"] = "posted"
        save_reviews(reviews)

    except Exception as e:
        result["error"] = str(e)
        write_post_progress("오류", error=str(e))
    finally:
        context.close()
        pw.stop()

    write_post_progress("완료", success=result["ok"], error=result.get("error"))
    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python3 reply_poster.py <리뷰_인덱스>")
        sys.exit(1)
    idx = int(sys.argv[1])
    res = post_reply(idx)
    print("성공" if res["ok"] else f"실패: {res['error']}")
