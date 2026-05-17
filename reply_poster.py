"""
스마트스토어 셀러센터에 답글 자동 게시 — HTTP 직접 호출.

실제 셀러센터 API를 record로 캡처해 확인:
    POST /api/v3/contents/reviews/comment/bulk-create
    Body: {"reviewIds":[<rid>], "commentContent":"<text>"}

reviews.json의 'review_id' 필드를 사용. 그 필드가 없으면 재수집 필요.
인증: session_state.json 의 cookies (auto_login.save_session으로 저장됨).

실행:
    python3 reply_poster.py <idx> [--dry-run]
"""
import json
import os
import sys
import functools

import reply_api

print = functools.partial(print, flush=True)

REVIEWS_FILE = "data/reviews.json"
POST_PROGRESS_FILE = "data/post_progress.json"


def write_post_progress(step, success=None, error=None):
    os.makedirs(os.path.dirname(POST_PROGRESS_FILE), exist_ok=True)
    with open(POST_PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({"step": step, "success": success, "error": error, "running": step != "완료"}, f)


def load_reviews():
    with open(REVIEWS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_reviews(reviews):
    with open(REVIEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(reviews, f, ensure_ascii=False, indent=2)


def post_reply(idx: int, dry_run: bool = False, progress_cb=None, io_lock=None) -> dict:
    """단일 리뷰 idx에 답글 게시. dry_run=True면 실제 POST 안 함.

    progress_cb(step:str): 진행 콜백. 미지정 시 글로벌 파일 IPC로 폴백(CLI 호환).
    io_lock: reviews.json 동시 IO 보호용 threading.Lock(주입형). 없으면 보호 없음.

    Returns:
        {"ok": bool, "error": str|None, "status": int|None}
    """
    def _progress(step, **kw):
        if progress_cb is not None:
            try:
                progress_cb(step)
            except Exception:
                pass
        else:
            write_post_progress(step, **kw)

    def _load():
        if io_lock is not None:
            with io_lock:
                return load_reviews()
        return load_reviews()

    def _save(rs):
        if io_lock is not None:
            with io_lock:
                save_reviews(rs)
        else:
            save_reviews(rs)

    reviews = _load()
    if idx < 0 or idx >= len(reviews):
        return {"ok": False, "error": f"리뷰 idx {idx} 범위 밖 (전체 {len(reviews)})"}

    r = reviews[idx]
    reply_text = (r.get("ai_reply") or "").strip()
    review_id = (r.get("review_id") or "").strip()

    _progress("검증 중")
    if not review_id:
        return {"ok": False, "error": "review_id 없음 — 재수집 필요"}
    if r.get("replied"):
        return {"ok": False, "error": "이미 답글이 달린 리뷰입니다."}
    if not reply_text:
        if dry_run:
            reply_text = "[dry-run] 검증용 텍스트 — 실제 게시되지 않습니다."
        else:
            return {"ok": False, "error": "게시할 답글 내용이 없습니다."}

    _progress("답글 등록 API 호출 중")
    result = reply_api.post_reply(review_id=review_id, text=reply_text, dry_run=dry_run)

    if not result["ok"]:
        _progress("오류", error=result.get("error"))
        return {"ok": False, "error": result.get("error"), "status": result.get("status")}

    # dry-run이 아닌 경우에만 reviews.json 업데이트
    if not dry_run:
        _progress("저장 중")
        # 최신 reviews를 재읽기 (다른 게시가 동시 진행 가능성)
        reviews_latest = _load()
        if 0 <= idx < len(reviews_latest):
            reviews_latest[idx]["replied"] = True
            reviews_latest[idx]["reply_status"] = "posted"
            _save(reviews_latest)

    _progress("완료", success=True)
    return {"ok": True, "error": None, "status": result.get("status")}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python3 reply_poster.py <리뷰_인덱스> [--dry-run]")
        sys.exit(1)
    idx = int(sys.argv[1])
    dry_run = "--dry-run" in sys.argv[2:]
    res = post_reply(idx, dry_run=dry_run)
    print(f"\n결과: {res}")
    sys.exit(0 if res["ok"] else 1)
