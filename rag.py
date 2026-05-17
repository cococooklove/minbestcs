"""
RAG (Retrieval-Augmented Generation) — 승인된 답변을 임베딩 인덱스에 저장하고,
새 리뷰에 대해 유사한 과거 사례를 검색해 few-shot 예시로 활용.

인덱스 포맷 (data/rag_index.json):
[
  {
    "key": "<review_id 또는 content 해시>",
    "content": "...",
    "rating": 5,
    "product": "...",
    "reply": "...",
    "embedding": [float, ...],   # text-embedding-3-small (1536 dim)
    "updated_at": "YYYY-MM-DD HH:MM"
  },
  ...
]
"""
import json, os, math, hashlib, threading
from datetime import datetime

RAG_INDEX_FILE = "data/rag_index.json"
EMBED_MODEL = "text-embedding-3-small"
_io_lock = threading.Lock()


def _entry_key(review: dict) -> str:
    rid = (review.get("review_id") or "").strip()
    if rid:
        return rid
    base = (review.get("content") or "") + "|" + (review.get("product") or "")
    return "h:" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def load_index() -> list:
    if not os.path.exists(RAG_INDEX_FILE):
        return []
    try:
        with open(RAG_INDEX_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_index(index: list) -> None:
    os.makedirs(os.path.dirname(RAG_INDEX_FILE), exist_ok=True)
    with _io_lock:
        with open(RAG_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)


def _embed_input(review: dict) -> str:
    parts = [
        review.get("content") or "",
        f"별점 {review.get('rating', '')}점" if review.get("rating") else "",
        f"상품: {review.get('product', '')}" if review.get("product") else "",
    ]
    return "\n".join(p for p in parts if p)[:2000]


def embed_text(text: str, client) -> list:
    resp = client.embeddings.create(model=EMBED_MODEL, input=text)
    return resp.data[0].embedding


def embed_batch(texts: list, client) -> list:
    if not texts:
        return []
    resp = client.embeddings.create(model=EMBED_MODEL, input=texts)
    return [d.embedding for d in resp.data]


def _cosine(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def upsert_review(review: dict, client) -> bool:
    """승인된 리뷰 하나를 인덱스에 추가/갱신. 실패 시 False."""
    reply = review.get("ai_reply") or ""
    if not reply:
        return False
    try:
        emb = embed_text(_embed_input(review), client)
    except Exception:
        return False
    key = _entry_key(review)
    index = load_index()
    entry = {
        "key": key,
        "content": review.get("content", ""),
        "rating": review.get("rating", ""),
        "product": review.get("product", ""),
        "reply": reply,
        "embedding": emb,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    found = False
    for i, e in enumerate(index):
        if e.get("key") == key:
            index[i] = entry
            found = True
            break
    if not found:
        index.append(entry)
    save_index(index)
    return True


def remove_review(review: dict) -> bool:
    key = _entry_key(review)
    index = load_index()
    new_index = [e for e in index if e.get("key") != key]
    if len(new_index) != len(index):
        save_index(new_index)
        return True
    return False


def rebuild_index(reviews: list, client, batch_size: int = 100) -> int:
    """승인 + ai_reply 존재 + sensitive_flags 없음 인 리뷰 전체 재임베딩."""
    approved = [
        r for r in reviews
        if r.get("reply_status") == "approved"
        and r.get("ai_reply")
        and not r.get("sensitive_flags")
    ]
    new_index = []
    for i in range(0, len(approved), batch_size):
        chunk = approved[i:i + batch_size]
        inputs = [_embed_input(r) for r in chunk]
        try:
            embs = embed_batch(inputs, client)
        except Exception:
            embs = []
            for txt in inputs:
                try:
                    embs.append(embed_text(txt, client))
                except Exception:
                    embs.append(None)
        for r, emb in zip(chunk, embs):
            if not emb:
                continue
            new_index.append({
                "key": _entry_key(r),
                "content": r.get("content", ""),
                "rating": r.get("rating", ""),
                "product": r.get("product", ""),
                "reply": r["ai_reply"],
                "embedding": emb,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            })
    save_index(new_index)
    return len(new_index)


def retrieve_similar(review: dict, client, top_k: int = 3,
                     min_score: float = 0.3) -> list:
    """새 리뷰에 대해 top_k 유사 사례 반환. 점수 < min_score는 제외."""
    index = load_index()
    if not index:
        return []
    try:
        q_emb = embed_text(_embed_input(review), client)
    except Exception:
        return []
    scored = []
    for e in index:
        emb = e.get("embedding")
        if not emb:
            continue
        s = _cosine(q_emb, emb)
        if s >= min_score:
            scored.append((s, e))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {
            "score": round(s, 4),
            "content": e.get("content", ""),
            "rating": e.get("rating", ""),
            "product": e.get("product", ""),
            "reply": e.get("reply", ""),
        }
        for s, e in scored[:top_k]
    ]


def index_stats() -> dict:
    index = load_index()
    return {
        "count": len(index),
        "last_updated": max((e.get("updated_at", "") for e in index), default=""),
    }
