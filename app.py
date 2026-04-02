"""
3단계: 리뷰 뷰어 웹서버
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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/reviews")
def api_reviews():
    reviews = load_reviews()

    # 필터링
    q = request.args.get("q", "").strip().lower()
    rating = request.args.get("rating", "")
    replied = request.args.get("replied", "")
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

    if sort == "oldest":
        reviews = sorted(reviews, key=lambda r: r.get("date", ""))
    elif sort == "rating_high":
        reviews = sorted(reviews, key=lambda r: float(r.get("rating", 0) or 0), reverse=True)
    elif sort == "rating_low":
        reviews = sorted(reviews, key=lambda r: float(r.get("rating", 0) or 0))
    else:  # newest
        reviews = sorted(reviews, key=lambda r: r.get("date", ""), reverse=True)

    # 통계
    all_reviews = load_reviews()
    ratings = [float(r.get("rating", 0) or 0) for r in all_reviews if r.get("rating")]
    stats = {
        "total": len(all_reviews),
        "filtered": len(reviews),
        "avg_rating": round(sum(ratings) / len(ratings), 1) if ratings else 0,
        "replied_count": sum(1 for r in all_reviews if r.get("replied")),
        "rating_dist": {str(i): sum(1 for r in ratings if int(r) == i) for i in range(1, 6)},
    }

    return jsonify({"reviews": reviews, "stats": stats})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
