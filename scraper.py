"""
스마트스토어 리뷰 수집 — API 직접 호출 방식.

엔드포인트: POST /api/v3/contents/reviews/search
- 페이지네이션: page (0-based) + size (최대 500)
- 인증: 셀러센터 쿠키 (영속 프로필 또는 주입)
- 응답: {"contents":[{...리뷰...}, ...]}

기존 엑셀 다운로드 → 파싱 흐름을 대체. UI 클릭/모달 가드/다운로드 대기 불필요.

실행: python3 scraper.py
"""
from playwright.sync_api import sync_playwright
import json, os, time
from datetime import datetime, timedelta
from pathlib import Path
import modal_guard

PROFILE_DIR = os.environ.get("SCRAPER_PROFILE_DIR") or os.path.abspath("data/browser_profile")
OUTPUT_FILE = "data/reviews.json"
DOWNLOAD_DIR = Path("data/downloads").resolve()  # 호환성: 외부에서 참조

REVIEW_SEARCH_URL = "https://sell.smartstore.naver.com/#/review/search"
REVIEW_API_PATH = "/api/v3/contents/reviews/search"
PAGE_SIZE = 500
MAX_PAGES = 200  # 안전 상한 (500 * 200 = 10만건)


def _to_iso(dt: datetime, end_of_day: bool = False) -> str:
    """셀러센터 API가 받는 ISO 8601 (한국 timezone)."""
    if end_of_day:
        return dt.strftime("%Y-%m-%dT23:59:59.999+09:00")
    return dt.strftime("%Y-%m-%dT00:00:00.000+09:00")


REVIEW_API_URL = "https://sell.smartstore.naver.com/api/v3/contents/reviews/search"


def _fetch_review_page(page, from_iso: str, to_iso: str, page_no: int, size: int) -> dict:
    """셀러센터 SPA 컨텍스트 안에서 fetch() — 쿠키/CSRF 자동 처리.

    절대 URL을 사용해야 page가 셀러센터 도메인이 아닐 때도 동작. 단, fetch는
    여전히 page의 origin에서 발사되므로 cross-origin이면 셀러센터 CORS 정책에
    걸릴 수 있음 → 호출 직전에 셀러센터 페이지가 활성화돼 있어야 함.
    """
    payload = {
        "reviewSearchSortType": "REVIEW_CREATE_DATE_DESC",
        "searchKeywordType": "IDS",
        "searchKeyword": "",
        "fromDate": from_iso,
        "toDate": to_iso,
        "useSelectedDate": False,
        "reviewTypes": [],
        "reviewContentClassTypes": [],
        "storeTypes": [],
        "reviewScores": [],
        "benefitKindTypeStringList": [],
        "contentsStatusTypes": [],
        "page": page_no,
        "size": size,
        "sort": [],
    }
    js = r"""
    async ({url, payload}) => {
        try {
            const r = await fetch(url, {
                method: 'POST',
                headers: {'content-type': 'application/json;charset=UTF-8'},
                body: JSON.stringify(payload),
                credentials: 'include'
            });
            const text = await r.text();
            if (!r.ok) return { __error: true, status: r.status, body: text.slice(0, 400) };
            try { return JSON.parse(text); }
            catch (e) { return { __error: true, status: r.status, body: 'JSON parse: ' + text.slice(0, 400) }; }
        } catch (e) {
            return { __error: true, status: 0, body: String(e) + ' (origin=' + location.origin + ')' };
        }
    }
    """
    return page.evaluate(js, {"url": REVIEW_API_URL, "payload": payload})


def _map_review(r: dict) -> dict:
    """API 응답 리뷰 객체 → reviews.json 포맷."""
    # 사진 URL — reviewAttaches[] 또는 reviewAttach
    photo_url = ""
    attaches = r.get("reviewAttaches") or []
    if attaches:
        photo_url = attaches[0].get("attachUrl") or attaches[0].get("url") or ""
    elif isinstance(r.get("reviewAttach"), dict):
        photo_url = r["reviewAttach"].get("attachUrl") or ""

    # 'YYYY-MM-DDTHH:MM:SS...' → 'YYYY-MM-DD'
    cd = r.get("createDate") or ""
    norm_date = cd[:10] if len(cd) >= 10 else cd

    return {
        "reviewer":      r.get("maskedWriterId") or "",
        "date":          norm_date,
        "rating":        str(r.get("reviewScore") or ""),
        "product":       r.get("productName") or "",
        "option":        "",  # productName에 옵션이 포함되어 별도 필드 없음
        "content":       r.get("reviewContent") or "",
        "photo_url":     photo_url,
        "replied":       bool(r.get("hasComment")),
        "reply_content": "",  # 답글 본문은 별도 API에서만 제공
        "order_no":      r.get("productOrderNo") or "",
        "review_id":     str(r.get("id") or ""),
        "scraped_at":    datetime.now().isoformat(),
    }


def _ensure_review_page(page, progress):
    """리뷰 페이지 진입 + 검색 폼 가시 확인. 폼이 안 보이면 인증/로딩 실패로 간주."""
    try:
        cur = (page.url or "").lower()
    except Exception:
        cur = ""
    if "#/review/search" not in cur:
        try:
            page.goto(REVIEW_SEARCH_URL, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass
    try:
        page.locator("button:has-text('초기화')").first.wait_for(state="visible", timeout=15000)
    except Exception:
        # 세션 만료/페이지 미로드
        raise Exception(
            "리뷰 검색 페이지가 로드되지 않았습니다. "
            "로그인 세션이 만료되었을 수 있습니다 (확장프로그램에서 다시 수집을 시작해주세요)."
        )


def main(progress_cb=None, existing_page=None, cookies=None, headless=False):
    def progress(msg):
        print(msg)
        if progress_cb:
            progress_cb(msg)

    pw = None
    browser = None
    context = None
    own_context = False
    page = None

    try:
        # === 컨텍스트 셋업 ===
        if existing_page is not None:
            # 이미 로그인된 페이지 재사용 (로컬 UI 모드)
            page = existing_page
            context = page.context
            modal_guard.attach_dialog_autoaccept(page)
            modal_guard.apply_now(page)
        elif cookies:
            # 서버 headless 모드: 쿠키 주입
            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                      "--disable-gpu", "--single-process"],
            )
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            modal_guard.install(context)
            modal_guard.attach_dialog_autoaccept(context)
            SAME_SITE_MAP = {"no_restriction": "None", "lax": "Lax",
                             "strict": "Strict", "unspecified": "Lax"}
            normalized = []
            for c in cookies:
                c = dict(c)
                c["sameSite"] = SAME_SITE_MAP.get(str(c.get("sameSite", "")).lower(), "Lax")
                normalized.append(c)
            context.add_cookies(normalized)
            page = context.new_page()
            own_context = True
        else:
            # 로컬 영속 프로필 모드 (auto_login 사용)
            os.makedirs(PROFILE_DIR, exist_ok=True)
            for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                lp = os.path.join(PROFILE_DIR, lock)
                if os.path.exists(lp):
                    try: os.remove(lp)
                    except OSError: pass
            pw = sync_playwright().start()
            context = pw.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                headless=headless,
                viewport={"width": 1440, "height": 900},
            )
            modal_guard.install(context)
            modal_guard.attach_dialog_autoaccept(context)
            try:
                import auto_login as _al
                _al.restore_session(context)
            except Exception:
                pass
            page = context.pages[0] if context.pages else context.new_page()
            modal_guard.apply_now(page)
            own_context = True

            progress("자동 로그인 중...")
            try:
                import auto_login as _al
                status = _al.ensure_logged_in(
                    page,
                    naver_id=os.environ.get("NAVER_ID", ""),
                    naver_pw=os.environ.get("NAVER_PW", ""),
                )
            except Exception as e:
                raise Exception(f"자동 로그인 실패: {e}")
            if status != "seller":
                raise Exception(f"자동 로그인 실패: {status}")
            progress("로그인 OK")

        # === 리뷰 페이지 진입 (referer/SPA 컨텍스트 셋업) ===
        progress("리뷰 검색 페이지 로딩 중...")
        _ensure_review_page(page, progress)

        # === API 직접 호출 (1년치) ===
        # 셀러센터 API는 "최대 1년"을 엄격히 검사 (365일 = 거부, 364일 = 허용)
        to_dt = datetime.now()
        from_dt = to_dt - timedelta(days=364)
        from_iso = _to_iso(from_dt)
        to_iso = _to_iso(to_dt, end_of_day=True)
        progress(f"수집 기간: {from_dt.date()} ~ {to_dt.date()}")

        all_reviews = []
        for page_no in range(MAX_PAGES):
            progress(f"리뷰 page={page_no} 조회 중 (누적 {len(all_reviews)}건)...")
            result = _fetch_review_page(page, from_iso, to_iso, page_no, PAGE_SIZE)
            if isinstance(result, dict) and result.get("__error"):
                raise Exception(
                    f"리뷰 API 실패: status={result.get('status')} "
                    f"body={(result.get('body') or '')[:200]}"
                )
            contents = (result or {}).get("contents") or []
            if not contents:
                progress(f"page={page_no} 결과 없음 — 종료")
                break
            all_reviews.extend(_map_review(r) for r in contents)
            if len(contents) < PAGE_SIZE:
                progress(f"page={page_no} 마지막 페이지 (n={len(contents)})")
                break
        else:
            progress(f"[경고] MAX_PAGES({MAX_PAGES}) 도달 — 중단")

        progress(f"API 수집 완료: 총 {len(all_reviews)}건")

    except Exception as e:
        import traceback; traceback.print_exc()
        progress(f"수집 실패: {e}")
        raise
    finally:
        if own_context:
            try:
                if context: context.close()
            except Exception: pass
            try:
                if browser: browser.close()
            except Exception: pass
            try:
                if pw: pw.stop()
            except Exception: pass

    # === reviews.json 병합 (기존과 동일) ===
    progress("저장 중...")
    try:
        existing = []
        if os.path.exists(OUTPUT_FILE):
            with open(OUTPUT_FILE, encoding="utf-8") as f:
                existing = json.load(f)
        key = lambda r: (r.get("content",""), r.get("date",""), r.get("reviewer",""))
        existing_keys = {key(r) for r in existing}
        added = [r for r in all_reviews if key(r) not in existing_keys]
        merged = existing + added
        os.makedirs(os.path.dirname(OUTPUT_FILE) or ".", exist_ok=True)
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
    except Exception as e:
        raise Exception(f"리뷰 저장 실패: {e}")

    progress(f"완료: 신규 {len(added)}건 추가 / 전체 {len(merged)}건")


if __name__ == "__main__":
    main()
