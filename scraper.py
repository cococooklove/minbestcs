"""
스마트스토어 리뷰 엑셀 다운로드 → JSON 변환
실행: python3 scraper.py
"""
from playwright.sync_api import sync_playwright
import json, os, time
from datetime import datetime
from pathlib import Path
import openpyxl

PROFILE_DIR = os.path.abspath("data/browser_profile")
OUTPUT_FILE = "data/reviews.json"
DOWNLOAD_DIR = Path("data/downloads").resolve()


def wait_for_login(page):
    """로그인될 때까지 대기"""
    LOGIN_INDICATORS = ("login", "nidlogin", "naver.com/nid", "oauth", "signin")
    print("로그인 대기 중... (로그인 완료되면 자동으로 진행됩니다)")
    while True:
        url = page.url
        is_login = any(x in url.lower() for x in LOGIN_INDICATORS)
        on_store = "sell.smartstore.naver.com" in url and not is_login
        if on_store:
            print(f"로그인 확인: {url}")
            time.sleep(2)
            return
        time.sleep(1.5)


def excel_to_reviews(filepath):
    wb = openpyxl.load_workbook(filepath)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    # 헤더 찾기
    header_row_idx = next((i for i, r in enumerate(rows) if any(r)), 0)
    headers = [str(h).strip() if h else "" for h in rows[header_row_idx]]
    print(f"  헤더: {headers}")

    col_map = {
        "reviewer":      ["작성자", "구매자", "회원ID", "아이디"],
        "date":          ["작성일", "리뷰작성일", "날짜"],
        "rating":        ["별점", "평점", "리뷰점수", "점수"],
        "product":       ["상품명", "상품"],
        "option":        ["옵션", "선택옵션"],
        "content":       ["리뷰내용", "내용", "리뷰"],
        "replied":       ["답글여부", "답변여부", "답글"],
        "reply_content": ["답글내용", "답변내용"],
        "order_no":      ["주문번호"],
    }

    def find_col(field):
        for kw in col_map[field]:
            for i, h in enumerate(headers):
                if kw in h:
                    return i
        return None

    idx = {f: find_col(f) for f in col_map}

    def cell(row, field):
        i = idx.get(field)
        if i is None or i >= len(row):
            return ""
        v = row[i]
        return str(v).strip() if v is not None else ""

    reviews = []
    for row in rows[header_row_idx + 1:]:
        if not any(row):
            continue
        replied_val = cell(row, "replied")
        reviews.append({
            "reviewer":      cell(row, "reviewer"),
            "date":          cell(row, "date"),
            "rating":        cell(row, "rating"),
            "product":       cell(row, "product"),
            "option":        cell(row, "option"),
            "content":       cell(row, "content"),
            "replied":       replied_val in ("Y", "y", "완료", "답글있음", "True", "true", "1"),
            "reply_content": cell(row, "reply_content"),
            "order_no":      cell(row, "order_no"),
            "scraped_at":    datetime.now().isoformat(),
        })
    return reviews


def main():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    os.makedirs(PROFILE_DIR, exist_ok=True)

    # 락 파일 정리
    for lock in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        lp = os.path.join(PROFILE_DIR, lock)
        if os.path.exists(lp):
            os.remove(lp)

    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=False,
        slow_mo=50,
        viewport={"width": 1440, "height": 900},
        accept_downloads=True,
    )
    excel_path = None
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.on("dialog", lambda d: d.accept())

        # 스마트스토어 리뷰 페이지로 이동
        print("리뷰 관리 페이지로 이동 중...")
        try:
            page.goto("https://sell.smartstore.naver.com/#/review/search", timeout=15000)
        except Exception:
            pass
        time.sleep(2)

        # 로그인 안 되어 있으면 대기
        url = page.url
        if "sell.smartstore.naver.com" not in url or any(
            x in url.lower() for x in ("login", "nidlogin", "oauth", "signin")
        ):
            wait_for_login(page)
            try:
                page.goto("https://sell.smartstore.naver.com/#/review/search", timeout=15000)
            except Exception:
                pass
            time.sleep(3)

        print("리뷰 페이지 로딩 대기 중...")
        time.sleep(3)

        # 엑셀다운 버튼 클릭
        print("'엑셀다운' 버튼 탐색 중...")
        btn = page.get_by_text("엑셀다운").first
        btn.wait_for(state="visible", timeout=15000)
        print("버튼 클릭...")

        with page.expect_download(timeout=60000) as dl_info:
            btn.click()
            time.sleep(2)
            for sel in [
                "button:has-text('확인')",
                "button:has-text('다운로드')",
                "button:has-text('예')",
                "[class*='Modal'] button[class*='primary']",
                "[class*='modal'] button[class*='confirm']",
                "[role='dialog'] button",
            ]:
                try:
                    confirm = page.wait_for_selector(sel, timeout=2000, state="visible")
                    if confirm:
                        print("팝업 확인 클릭")
                        confirm.click()
                        break
                except Exception:
                    continue

        download = dl_info.value
        print(f"다운로드 완료: {download.suggested_filename}")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        excel_path = DOWNLOAD_DIR / f"reviews_{timestamp}.xlsx"
        download.save_as(str(excel_path))

    except Exception:
        import traceback
        traceback.print_exc()
        input("\n오류 발생. Enter를 눌러 종료: ")
    finally:
        context.close()
        pw.stop()

    # 엑셀 파싱
    print("\n엑셀 파싱 중...")
    new_reviews = excel_to_reviews(str(excel_path))
    print(f"파싱 완료: {len(new_reviews)}건")

    existing = []
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            existing = json.load(f)

    existing_keys = {(r.get("content", ""), r.get("date", ""), r.get("reviewer", "")) for r in existing}
    added = [r for r in new_reviews if (r.get("content", ""), r.get("date", ""), r.get("reviewer", "")) not in existing_keys]
    all_reviews = existing + added

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_reviews, f, ensure_ascii=False, indent=2)

    print(f"\n완료: 신규 {len(added)}건 추가 / 전체 {len(all_reviews)}건")
    print(f"웹 뷰어: python3 app.py → http://localhost:5000")


if __name__ == "__main__":
    main()
