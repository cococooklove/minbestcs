"""
엑셀 다운로드 흐름을 캡처해서 실제 API 엔드포인트/페이로드/응답을 알아낸다.

실행:
    python3 record_excel_download.py

브라우저가 열리면:
1. (자동) 셀러센터 로그인 + 리뷰 검색 페이지 이동
2. (사용자) 기간 설정 → 검색 → 엑셀다운 버튼 → 확인 클릭
3. (자동) 다운로드 시작/완료 이벤트 + 모든 네트워크 자동 캡처
4. 다운로드 완료 후 이 터미널에서 Ctrl+C → 모든 데이터 저장

저장 위치: recordings/excel_<timestamp>/
  - network.json                전체 요청/응답
  - excel_api_candidates.json   엑셀/다운로드/export 관련 요청만 추출
  - excel_responses.json        후보 요청의 응답 body 일부 (앞 4KB)
  - downloads/                  실제 받은 파일
  - final_dom.html              최종 화면 DOM
  - final_shot.png              최종 화면 스크린샷
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
import modal_guard

load_dotenv()
print = functools.partial(print, flush=True)

PROFILE_DIR = os.environ.get("SCRAPER_PROFILE_DIR") or os.path.abspath("data/browser_profile")
OUT_DIR = Path("recordings") / f"excel_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}"


# 엑셀/다운로드 관련 후보 키워드 — URL 매칭용 (대소문자 무시)
CANDIDATE_KEYWORDS = (
    "excel", "export", "download", "xlsx",
    # 한글 URL은 거의 없지만 혹시 모르니
    "엑셀", "다운로드",
)

# 후보로 응답 body까지 캡처할 요청 식별 키워드 (URL 또는 method 기반)
RESPONSE_CAPTURE_KEYWORDS = ("excel", "export", "download", "job", "task", "status")


def _clean_locks():
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lp = os.path.join(PROFILE_DIR, lock)
        if os.path.exists(lp):
            try:
                os.remove(lp)
            except OSError:
                pass


def _looks_excel_related(url: str, method: str = "") -> bool:
    if not url:
        return False
    u = url.lower()
    # 정적 리소스 노이즈 제외
    if any(u.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".woff", ".woff2", ".ttf")):
        return False
    return any(kw in u for kw in CANDIDATE_KEYWORDS)


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

    net_events = []
    # 응답 body는 메모리/저장공간을 많이 차지하므로 후보 요청에 대해서만 캡처
    response_bodies = []
    download_log = []

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
            if _looks_excel_related(req.url, req.method):
                print(f"\n[★ 엑셀/다운로드 후보 요청] {req.method} {req.url}")
                if req.post_data:
                    body = req.post_data[:800]
                    print(f"  body: {body}")
        except Exception as e:
            print(f"  request 캡처 실패: {e}")

    def _on_response(res):
        try:
            entry = {
                "t": time.time(),
                "kind": "response",
                "status": res.status,
                "url": res.url,
                "content_type": res.headers.get("content-type", ""),
            }
            net_events.append(entry)
            # 후보 요청의 응답 body 일부 저장
            if any(kw in res.url.lower() for kw in RESPONSE_CAPTURE_KEYWORDS):
                snippet = None
                try:
                    body = res.body()  # bytes
                    snippet = body[:4096]
                except Exception:
                    snippet = None
                response_bodies.append({
                    "t": time.time(),
                    "url": res.url,
                    "status": res.status,
                    "content_type": res.headers.get("content-type", ""),
                    "headers": dict(res.headers),
                    "body_b64_first_4kb": (snippet.hex() if snippet else None),
                    "body_text_first_4kb": (
                        snippet.decode("utf-8", errors="replace") if snippet else None
                    ),
                })
                print(f"  ← {res.status} {res.url} ({res.headers.get('content-type','')})")
        except Exception as e:
            print(f"  response 캡처 실패: {e}")

    def _on_download(dl):
        try:
            ts = datetime.now().strftime("%H%M%S")
            sugg = dl.suggested_filename or "download.bin"
            path = OUT_DIR / "downloads" / f"{ts}_{sugg}"
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
            download_log.append(entry)
            print(f"\n[★ 다운로드 이벤트] {sugg} ← {dl.url}")
            if path:
                print(f"  저장: {path}")
        except Exception as e:
            print(f"  download 핸들러 실패: {e}")

    def _attach(p):
        p.on("request", _on_request)
        p.on("response", _on_response)
        p.on("download", _on_download)

    for p in context.pages:
        _attach(p)
    context.on("page", _attach)
    # context 레벨 download 이벤트도 잡기 (Playwright는 page 레벨로 충분하지만 안전망)
    context.on("page", _attach)

    page = context.pages[0] if context.pages else context.new_page()

    print("=" * 70)
    print("브라우저가 열렸습니다.")
    print("1. (자동) 셀러센터 로그인 + 리뷰 검색 페이지 이동")
    print("2. (사용자) 기간 설정 → 검색 → 엑셀다운 → 확인")
    print("3. (자동) 모든 네트워크 + 다운로드 이벤트 캡처")
    print("4. 다운로드 완료 후 이 터미널에서 Ctrl+C")
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
    print(">>> 이제 엑셀 다운로드를 직접 수행하세요. 끝나면 Ctrl+C <<<")
    print()

    def _save_and_exit(signum=None, frame=None):
        print("\n캡처 종료, 저장 중...")

        # 1) 전체 네트워크
        try:
            (OUT_DIR / "network.json").write_text(
                json.dumps(net_events, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"  network.json 저장 실패: {e}")

        # 2) 엑셀 관련 요청만 추출
        candidates = [
            e for e in net_events
            if e.get("kind") == "request"
            and _looks_excel_related(e.get("url") or "")
        ]
        try:
            (OUT_DIR / "excel_api_candidates.json").write_text(
                json.dumps(candidates, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"  excel_api_candidates.json 저장 실패: {e}")

        # 3) 응답 body
        try:
            (OUT_DIR / "excel_responses.json").write_text(
                json.dumps(response_bodies, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"  excel_responses.json 저장 실패: {e}")

        # 4) 다운로드 메타
        try:
            (OUT_DIR / "downloads.json").write_text(
                json.dumps(download_log, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"  downloads.json 저장 실패: {e}")

        # 5) 쿠키 (직접 API 호출 시 필요)
        try:
            cookies = context.cookies()
            (OUT_DIR / "cookies.json").write_text(
                json.dumps(cookies, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"  cookies.json 저장 실패: {e}")

        # 6) 최종 DOM/스크린샷
        try:
            (OUT_DIR / "final_dom.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        try:
            page.screenshot(path=str(OUT_DIR / "final_shot.png"), full_page=True)
        except Exception:
            pass

        print(f"\n저장 완료: {OUT_DIR.resolve()}")
        print(f"  전체 네트워크 이벤트: {len(net_events)}건")
        print(f"  엑셀 API 후보:        {len(candidates)}건")
        print(f"  응답 body 캡처:       {len(response_bodies)}건")
        print(f"  다운로드 이벤트:      {len(download_log)}건")
        if candidates:
            print("\n[요약] 엑셀 API 후보 요청:")
            for c in candidates:
                print(f"  {c['method']:6s} {c['url']}")
        if download_log:
            print("\n[요약] 다운로드 이벤트:")
            for d in download_log:
                print(f"  {d['suggested_filename']}  ←  {d['url']}")
        try:
            context.close()
        finally:
            pw.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _save_and_exit)
    signal.signal(signal.SIGTERM, _save_and_exit)

    # 무한 대기 — sync API 이벤트 펌프 유지
    while True:
        try:
            page.wait_for_timeout(500)
        except Exception:
            break


if __name__ == "__main__":
    main()
