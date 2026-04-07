"""
스마트스토어 리뷰 엑셀 다운로드 → JSON 변환
실행: python3 scraper.py
"""
from playwright.sync_api import sync_playwright
import json, os, time
from datetime import datetime
from pathlib import Path
import openpyxl

PROFILE_DIR = os.environ.get("SCRAPER_PROFILE_DIR") or os.path.abspath("data/browser_profile")
OUTPUT_FILE = "data/reviews.json"
DOWNLOAD_DIR = Path("data/downloads").resolve()


def wait_for_login(page):
    """자동 로그인 후 셀러센터 진입 대기"""
    from login import auto_login, wait_for_seller_center
    print("자동 로그인 시도 중...")
    auto_login(page)
    wait_for_seller_center(page)


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
        "reviewer":      ["등록자", "작성자", "구매자", "회원ID"],
        "date":          ["리뷰등록일", "작성일", "리뷰작성일", "날짜"],
        "rating":        ["구매자평점", "별점", "평점", "리뷰점수"],
        "product":       ["상품명", "상품"],
        "option":        ["옵션", "선택옵션"],
        "content":       ["리뷰상세내용", "리뷰내용", "내용", "리뷰"],
        "photo_url":     ["포토/영상", "포토", "이미지"],
        "replied":       ["답글여부", "답변여부"],
        "reply_content": ["답글내용", "답변내용"],
        "order_no":      ["상품주문번호", "주문번호"],
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
        raw_date = cell(row, "date")
        import re as _re
        m = _re.match(r'(\d{4})\.(\d{2})\.(\d{2})', raw_date)
        norm_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else raw_date
        reviews.append({
            "reviewer":      cell(row, "reviewer"),
            "date":          norm_date,
            "rating":        cell(row, "rating"),
            "product":       cell(row, "product"),
            "option":        cell(row, "option"),
            "content":       cell(row, "content"),
            "photo_url":     cell(row, "photo_url"),
            "replied":       replied_val in ("Y", "y", "완료", "답글있음", "True", "true", "1"),
            "reply_content": cell(row, "reply_content"),
            "order_no":      cell(row, "order_no"),
            "scraped_at":    datetime.now().isoformat(),
        })
    return reviews


def main(progress_cb=None, existing_page=None, cookies=None, headless=False):
    def progress(msg):
        print(msg)
        if progress_cb:
            progress_cb(msg)

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    browser = None
    # 기존 로그인 페이지 재사용
    if existing_page is not None:
        pw = None
        context = None
        page = existing_page
        page.on("dialog", lambda d: d.accept())
    elif cookies:
        # 서버 headless 모드: 쿠키 주입
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                  "--disable-gpu", "--single-process"],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
        )
        # Chrome 확장 sameSite 값 → Playwright 형식 변환
        SAME_SITE_MAP = {"no_restriction": "None", "lax": "Lax", "strict": "Strict", "unspecified": "Lax"}
        normalized = []
        for c in cookies:
            c = dict(c)
            c["sameSite"] = SAME_SITE_MAP.get(str(c.get("sameSite", "")).lower(), "Lax")
            normalized.append(c)
        context.add_cookies(normalized)
        page = context.new_page()
        page.on("dialog", lambda d: d.accept())
    else:
        os.makedirs(PROFILE_DIR, exist_ok=True)
        for lock in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
            lp = os.path.join(PROFILE_DIR, lock)
            if os.path.exists(lp):
                os.remove(lp)
        pw = sync_playwright().start()
        context = pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=headless,
            slow_mo=50,
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.on("dialog", lambda d: d.accept())

    def _is_on_login_page(pg):
        url = pg.url.lower()
        return any(x in url for x in ("nid.naver.com", "login", "nidlogin", "oauth", "signin", "checklogin"))

    excel_path = None
    try:
        if existing_page is None and not cookies:
            # 셀러센터로 이동 후 로그인 대기 (최대 5분, 로컬 직접 실행 전용)
            progress("셀러센터로 이동 중...")
            try:
                page.goto("https://sell.smartstore.naver.com/#/review/search",
                          wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass
            progress("로그인 대기 중...")
            for _ in range(300):
                url = page.url.lower()
                if "sell.smartstore.naver.com" in url and not _is_on_login_page(page):
                    break
                time.sleep(1)
            else:
                raise Exception("로그인 시간 초과 (5분). 다시 시도해주세요.")
            progress("로그인 확인됨. 수집 시작...")
            time.sleep(3)

        # 리뷰 페이지로 이동
        progress("리뷰 페이지 로딩 중...")
        try:
            page.goto("https://sell.smartstore.naver.com/#/review/search",
                      wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass
        time.sleep(5)

        # 세션 만료 감지
        def _needs_auth_check():
            if _is_on_login_page(page):
                return True
            try:
                return page.locator("text=로그인 상태를 확인").count() > 0
            except Exception:
                return False

        if _needs_auth_check():
            if cookies:
                # headless 모드: 쿠키 만료 → 즉시 실패
                raise Exception("쿠키가 만료되었습니다. 확장 프로그램에서 다시 수집을 시작해주세요.")
            # 로컬/existing_page 모드: 사용자 개입 대기
            progress("브라우저에서 로그인 확인을 완료해주세요...")
            for _ in range(300):
                time.sleep(1)
                if not _needs_auth_check():
                    break
            else:
                raise Exception("로그인 확인 시간 초과. 다시 로그인해주세요.")
            time.sleep(2)

        # 전체 선택 + 1년 기간 설정 후 검색
        progress("최근 1년치 리뷰를 다운로드 중입니다. 잠시 기다려주세요...")
        try:
            page.click("button:has-text('초기화')", timeout=8000)
            time.sleep(2)
            page.click("button:has-text('1년')", timeout=8000)
            time.sleep(1)
            page.click("button:has-text('검색')", timeout=8000)
            progress("검색 결과 로딩 중...")
            time.sleep(6)
        except Exception as e:
            progress(f"기간 설정 실패(기본값으로 진행): {e}")

        # 엑셀다운 버튼 대기
        progress("엑셀다운 버튼 찾는 중...")
        btn = page.get_by_text("엑셀다운").first
        btn.wait_for(state="visible", timeout=60000)
        progress("엑셀다운 버튼 클릭 중...")

        # 팝업 확인 + 다운로드를 분리해서 처리
        POPUP_SELS = [
            "[role='dialog'] button:has-text('확인')",
            "[role='dialog'] button:has-text('다운로드')",
            "[role='dialog'] button:has-text('예')",
            "[class*='Modal'] button:has-text('확인')",
            "[class*='modal'] button:has-text('확인')",
            "[class*='Popup'] button:has-text('확인')",
            "[class*='popup'] button:has-text('확인')",
            "[class*='Modal'] button[class*='primary']",
            "[class*='modal'] button[class*='confirm']",
        ]
        _popup_clicked = False
        _screenshot_dir = Path(os.path.dirname(OUTPUT_FILE)) / "screenshots"
        _screenshot_dir.mkdir(parents=True, exist_ok=True)

        def _save_screenshot(label):
            try:
                ts = datetime.now().strftime("%H%M%S")
                path = _screenshot_dir / f"{label}_{ts}.png"
                page.screenshot(path=str(path), full_page=False)
                return str(path)
            except Exception:
                return None

        try:
            with page.expect_download(timeout=120000) as dl_info:
                btn.click()
                time.sleep(3)

                # 버튼 클릭 직후 화면 캡처
                _save_screenshot("after_excel_click")

                # 현재 페이지에 보이는 모든 버튼 텍스트 로그
                try:
                    visible_btns = page.locator("button:visible").all_text_contents()
                    progress(f"[디버그] 감지된 버튼들: {', '.join(visible_btns[:10])}")
                except Exception:
                    pass

                for sel in POPUP_SELS:
                    try:
                        confirm = page.wait_for_selector(sel, timeout=2000, state="visible")
                        if confirm:
                            progress(f"팝업 감지됨 — 확인 클릭 중... ({sel})")
                            _save_screenshot("popup_found")
                            try:
                                confirm.click()
                            except Exception:
                                confirm.evaluate("el => el.click()")
                            _popup_clicked = True
                            _save_screenshot("after_popup_click")
                            time.sleep(2)
                            # 2차 팝업 대응
                            for sel2 in POPUP_SELS:
                                try:
                                    confirm2 = page.wait_for_selector(sel2, timeout=2000, state="visible")
                                    if confirm2:
                                        progress(f"2차 팝업 감지됨 — 확인 클릭 중... ({sel2})")
                                        _save_screenshot("second_popup_found")
                                        try:
                                            confirm2.click()
                                        except Exception:
                                            confirm2.evaluate("el => el.click()")
                                        time.sleep(2)
                                        break
                                except Exception:
                                    continue
                            break
                    except Exception:
                        continue

                if not _popup_clicked:
                    progress("[디버그] 팝업 버튼을 찾지 못함 — 다운로드 이벤트 대기 중...")
                    _save_screenshot("no_popup_found")

                progress("다운로드 시작 대기 중...")

        except Exception:
            _save_screenshot("download_timeout")
            try:
                cur_url = page.url
            except Exception:
                cur_url = "알 수 없음"
            if "login" in cur_url.lower() or "nid.naver" in cur_url.lower():
                raise Exception("세션이 만료되어 로그인 페이지로 이동되었습니다. 확장프로그램에서 다시 수집을 시작해주세요.")
            elif _popup_clicked:
                raise Exception(
                    "팝업 확인 클릭 후 다운로드가 시작되지 않았습니다. "
                    "셀러센터 엑셀 다운로드 팝업 구조가 변경되었거나 권한이 없을 수 있습니다. "
                    "(/api/screenshot 에서 캡처 확인 가능)"
                )
            else:
                raise Exception(
                    "엑셀다운 버튼 클릭 후 다운로드 팝업이 나타나지 않았습니다. "
                    "(/api/screenshot 에서 캡처 확인 가능)"
                )

        download = dl_info.value
        progress(f"다운로드 완료: {download.suggested_filename}")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        excel_path = DOWNLOAD_DIR / f"reviews_{timestamp}.xlsx"
        download.save_as(str(excel_path))

    except Exception as e:
        import traceback
        traceback.print_exc()
        progress(f"수집 실패: {e}")
        raise
    finally:
        if context:
            context.close()
        if browser:
            browser.close()
        if pw:
            pw.stop()

    if excel_path is None:
        raise Exception("엑셀 파일 다운로드 실패")

    # 엑셀 파싱
    progress("엑셀 파싱 중...")
    try:
        new_reviews = excel_to_reviews(str(excel_path))
    except Exception as e:
        raise Exception(f"엑셀 파싱 실패: {e}")
    progress(f"파싱 완료: {len(new_reviews)}건")

    try:
        existing = []
        if os.path.exists(OUTPUT_FILE):
            with open(OUTPUT_FILE, encoding="utf-8") as f:
                existing = json.load(f)

        existing_keys = {(r.get("content", ""), r.get("date", ""), r.get("reviewer", "")) for r in existing}
        added = [r for r in new_reviews if (r.get("content", ""), r.get("date", ""), r.get("reviewer", "")) not in existing_keys]
        all_reviews = existing + added

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(all_reviews, f, ensure_ascii=False, indent=2)
    except Exception as e:
        raise Exception(f"리뷰 저장 실패: {e}")

    progress(f"완료: 신규 {len(added)}건 추가 / 전체 {len(all_reviews)}건")



if __name__ == "__main__":
    main()
