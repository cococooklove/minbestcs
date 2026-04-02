"""
리뷰 뷰어 웹서버
실행: python3 app.py
접속: http://localhost:5000
"""
from flask import Flask, render_template, jsonify, request
import json, os

app = Flask(__name__)
REVIEWS_FILE = "data/reviews.json"


def load_reviews():
    if not os.path.exists(REVIEWS_FILE):
        return []
    with open(REVIEWS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_reviews(reviews):
    with open(REVIEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(reviews, f, ensure_ascii=False, indent=2)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/reviews")
def api_reviews():
    reviews = load_reviews()

    q = request.args.get("q", "").strip().lower()
    rating = request.args.get("rating", "")
    replied = request.args.get("replied", "")
    sentiment = request.args.get("sentiment", "")
    topic = request.args.get("topic", "")
    reportable = request.args.get("reportable", "")
    sort = request.args.get("sort", "newest")

    if q:
        reviews = [r for r in reviews if q in r.get("content", "").lower()
                   or q in r.get("product", "").lower()
                   or q in r.get("reviewer", "").lower()]
    if rating:
        reviews = [r for r in reviews if str(r.get("rating", "")).startswith(rating)]
    if replied == "yes":
        reviews = [r for r in reviews if r.get("replied")]
    elif replied == "no":
        reviews = [r for r in reviews if not r.get("replied")]
    if sentiment:
        reviews = [r for r in reviews if r.get("sentiment") == sentiment]
    if topic:
        reviews = [r for r in reviews if topic in (r.get("topics") or [])]
    if reportable == "yes":
        reviews = [r for r in reviews if r.get("reportable")]

    sort_key = {
        "oldest": lambda r: r.get("date", ""),
        "rating_high": lambda r: float(r.get("rating", 0) or 0),
        "rating_low": lambda r: float(r.get("rating", 0) or 0),
        "newest": lambda r: r.get("date", ""),
    }
    reviews = sorted(reviews, key=sort_key.get(sort, sort_key["newest"]),
                     reverse=sort not in ("oldest", "rating_low"))

    all_reviews = load_reviews()
    ratings = [float(r.get("rating", 0) or 0) for r in all_reviews if r.get("rating")]
    stats = {
        "total": len(all_reviews),
        "filtered": len(reviews),
        "avg_rating": round(sum(ratings) / len(ratings), 1) if ratings else 0,
        "replied_count": sum(1 for r in all_reviews if r.get("replied")),
        "rating_dist": {str(i): sum(1 for r in ratings if int(r) == i) for i in range(1, 6)},
        "sentiment_dist": {
            "positive": sum(1 for r in all_reviews if r.get("sentiment") == "positive"),
            "negative": sum(1 for r in all_reviews if r.get("sentiment") == "negative"),
            "mixed": sum(1 for r in all_reviews if r.get("sentiment") == "mixed"),
            "unclassified": sum(1 for r in all_reviews if not r.get("sentiment")),
        },
        "reportable_count": sum(1 for r in all_reviews if r.get("reportable")),
        "draft_count": sum(1 for r in all_reviews if r.get("reply_status") == "draft"),
    }

    # 인덱스 포함해서 반환 (답변 승인 등에 필요)
    indexed = [{"_idx": i, **r} for i, r in enumerate(load_reviews())
               if any(r2.get("content") == r.get("content") and r2.get("date") == r.get("date")
                      for r2 in reviews)]
    # 필터된 리뷰에 _idx 붙이기
    all_r = load_reviews()
    indexed_reviews = []
    for r in reviews:
        for i, ar in enumerate(all_r):
            if (ar.get("content") == r.get("content") and
                    ar.get("date") == r.get("date") and
                    ar.get("reviewer") == r.get("reviewer")):
                indexed_reviews.append({"_idx": i, **ar})
                break

    return jsonify({"reviews": indexed_reviews, "stats": stats})


@app.route("/api/classify", methods=["POST"])
def api_classify():
    """미분류 리뷰 일괄 분류"""
    import subprocess, sys
    subprocess.Popen([sys.executable, "classifier.py"])
    return jsonify({"status": "started"})


@app.route("/api/reply/generate/<int:idx>", methods=["POST"])
def api_generate_reply(idx):
    """특정 리뷰 AI 답변 생성"""
    reviews = load_reviews()
    if idx >= len(reviews):
        return jsonify({"error": "not found"}), 404

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY 없음. 환경변수를 설정하세요."}), 400

    try:
        import anthropic
        from classifier import generate_reply, load_brand_tone
        client = anthropic.Anthropic(api_key=api_key)
        brand_tone = load_brand_tone()
        reply = generate_reply(reviews[idx], brand_tone, client)
        reviews[idx]["ai_reply"] = reply
        reviews[idx]["reply_status"] = "draft"
        save_reviews(reviews)
        return jsonify({"reply": reply, "status": "draft"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reply/approve/<int:idx>", methods=["POST"])
def api_approve_reply(idx):
    """답변 승인"""
    reviews = load_reviews()
    if idx >= len(reviews):
        return jsonify({"error": "not found"}), 404

    data = request.get_json() or {}
    # 수정된 답변 내용 반영 가능
    if "reply" in data:
        reviews[idx]["ai_reply"] = data["reply"]
    reviews[idx]["reply_status"] = "approved"
    save_reviews(reviews)
    return jsonify({"status": "approved"})


@app.route("/api/reply/reject/<int:idx>", methods=["POST"])
def api_reject_reply(idx):
    """답변 초안 거절"""
    reviews = load_reviews()
    if idx >= len(reviews):
        return jsonify({"error": "not found"}), 404
    reviews[idx]["ai_reply"] = ""
    reviews[idx]["reply_status"] = "none"
    save_reviews(reviews)
    return jsonify({"status": "rejected"})


@app.route("/api/stats/voc")
def api_voc_stats():
    """VOC 통계 - 주제별/감성별 집계"""
    reviews = load_reviews()
    topic_counts = {}
    for r in reviews:
        for t in (r.get("topics") or []):
            topic_counts[t] = topic_counts.get(t, 0) + 1

    return jsonify({
        "topic_dist": dict(sorted(topic_counts.items(), key=lambda x: x[1], reverse=True)),
        "sentiment_by_product": _sentiment_by_product(reviews),
    })


def _sentiment_by_product(reviews):
    result = {}
    for r in reviews:
        p = r.get("product", "기타")
        if p not in result:
            result[p] = {"positive": 0, "negative": 0, "mixed": 0}
        s = r.get("sentiment")
        if s in result[p]:
            result[p][s] += 1
    return result


if __name__ == "__main__":
    app.run(debug=True, port=5000)
