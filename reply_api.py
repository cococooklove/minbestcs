"""
답글 등록 HTTP 직접 호출 (Track A).

사용자가 셀러센터에서 답글을 다는 실제 API를 record_reply_flow.py로 캡처한 결과:

    POST https://sell.smartstore.naver.com/api/v3/contents/reviews/comment/bulk-create
    Content-Type: application/json
    Body: {"reviewIds":[<review_id>], "commentContent":"<text>"}

인증: session_state.json에 저장된 cookies로 처리.
UI 자동화 전혀 없음 — 한 번의 POST로 끝.

사용:
    from reply_api import post_reply
    result = post_reply(review_id=4725280154, text="감사합니다", dry_run=False)
"""
import os
import json
import functools
from playwright.sync_api import sync_playwright

print = functools.partial(print, flush=True)

SESSION_STATE_PATH = os.environ.get("SESSION_STATE_PATH") or os.path.abspath("data/session_state.json")
PROFILE_DIR = os.environ.get("SCRAPER_PROFILE_DIR") or os.path.abspath("data/browser_profile")

REPLY_ENDPOINT = "https://sell.smartstore.naver.com/api/v3/contents/reviews/comment/bulk-create"

_DEFAULT_HEADERS = {
    "content-type": "application/json;charset=UTF-8",
    "referer": "https://sell.smartstore.naver.com/",
    "x-current-state": "https://sell.smartstore.naver.com/#/review/search",
    "x-current-statename": "main.contents.review.search",
    "x-to-statename": "main.contents.review.search",
    "cache-control": "no-cache",
    "pragma": "no-cache",
}


def is_available() -> bool:
    """답글 API 호출 가능 상태인지 — session_state.json에 cookies가 있어야 함."""
    if not os.path.exists(SESSION_STATE_PATH):
        return False
    try:
        with open(SESSION_STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
        return bool(state.get("cookies"))
    except Exception:
        return False


def _request_context(pw):
    """저장된 cookies로 인증된 APIRequestContext를 만든다."""
    return pw.request.new_context(storage_state=SESSION_STATE_PATH)


def post_reply(review_id, text: str, dry_run: bool = False) -> dict:
    """단일 review_id에 답글 등록.

    Returns:
        {"ok": bool, "status": int, "body": str, "error": str|None}
    """
    if not review_id:
        return {"ok": False, "status": 0, "body": "", "error": "review_id 비어있음"}
    if not text or not text.strip():
        return {"ok": False, "status": 0, "body": "", "error": "답글 내용 비어있음"}
    if not is_available():
        return {"ok": False, "status": 0, "body": "", "error": "session_state.json 없음 — auto_login 먼저"}

    # review_id는 숫자로 변환
    try:
        rid_int = int(str(review_id).strip())
    except Exception:
        return {"ok": False, "status": 0, "body": "", "error": f"review_id 정수 변환 실패: {review_id!r}"}

    payload = {"reviewIds": [rid_int], "commentContent": text}

    if dry_run:
        print(f"[reply_api] DRY-RUN — 실제 호출 안 함")
        print(f"  POST {REPLY_ENDPOINT}")
        print(f"  body: {json.dumps(payload, ensure_ascii=False)}")
        # 동등한 curl 명령
        body_str = json.dumps(payload, ensure_ascii=False).replace("'", "'\\''")
        print(f"\n  $ curl -X POST '{REPLY_ENDPOINT}' \\")
        for k, v in _DEFAULT_HEADERS.items():
            print(f"      -H '{k}: {v}' \\")
        print(f"      -b $(cat {SESSION_STATE_PATH} | jq -r '.cookies[] | \"\\(.name)=\\(.value)\"' | paste -sd ';') \\")
        print(f"      -d '{body_str}'")
        return {"ok": True, "status": 0, "body": "(dry-run)", "error": None}

    with sync_playwright() as pw:
        try:
            ctx = _request_context(pw)
            response = ctx.post(
                REPLY_ENDPOINT,
                headers=_DEFAULT_HEADERS,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=15000,
            )
            status = response.status
            body = response.text()
            ok = 200 <= status < 300
            print(f"[reply_api] POST → {status}")
            print(f"  response: {body[:300]}")
            ctx.dispose()
            return {"ok": ok, "status": status, "body": body, "error": None if ok else f"HTTP {status}"}
        except Exception as e:
            return {"ok": False, "status": 0, "body": "", "error": str(e)}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("사용법: python3 reply_api.py <review_id> <답글 텍스트> [--dry-run]")
        sys.exit(1)
    rid = sys.argv[1]
    txt = sys.argv[2]
    dry = "--dry-run" in sys.argv[3:]
    r = post_reply(rid, txt, dry_run=dry)
    print(f"\n결과: {r}")
    sys.exit(0 if r["ok"] else 1)
