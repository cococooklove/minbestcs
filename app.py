"""
리뷰 뷰어 웹서버
실행: python3 app.py
접속: http://localhost:5000
"""
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit, disconnect
import json, os, subprocess, sys
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading", ping_timeout=60, ping_interval=25)
REVIEWS_FILE = "data/reviews.json"
_agent_sid = None  # 연결된 로컬 에이전트 session id


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
        "need_reply_count": sum(1 for r in all_reviews if not r.get("replied") and not r.get("ai_reply")),
    }

    # 전체 기준 reviewer → 인덱스 목록 맵
    all_r = load_reviews()
    reviewer_map = {}
    for i, r in enumerate(all_r):
        rv = r.get("reviewer", "")
        if rv:
            reviewer_map.setdefault(rv, []).append(i)

    settings_data = load_settings()
    manual_tags   = load_manual_tags()

    # 필터된 리뷰에 _idx + reviewer_history + customer_type 붙이기
    indexed_reviews = []
    for r in reviews:
        for i, ar in enumerate(all_r):
            if (ar.get("content") == r.get("content") and
                    ar.get("date") == r.get("date") and
                    ar.get("reviewer") == r.get("reviewer")):
                history = [
                    {
                        "date": all_r[j].get("date"),
                        "rating": all_r[j].get("rating"),
                        "product": all_r[j].get("product"),
                        "content": all_r[j].get("content"),
                        "replied": all_r[j].get("replied"),
                    }
                    for j in reviewer_map.get(ar.get("reviewer", ""), [])
                    if j != i
                ]
                customer_type = calculate_customer_type(history, manual_tags, ar.get("reviewer", ""), settings_data)
                indexed_reviews.append({"_idx": i, **ar, "reviewer_history": history, "customer_type": customer_type})
                break

    return jsonify({"reviews": indexed_reviews, "stats": stats})


PROFILE_DIR = "data/browser_profile"
SETTINGS_FILE = "config/settings.json"
BRAND_TONE_FILE = "config/brand_tone.txt"


def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return {"auto_reply": False, "report_criteria": ["욕설", "경쟁사 언급", "광고성", "반복 내용"], "anthropic_api_key": ""}
    with open(SETTINGS_FILE, encoding="utf-8") as f:
        return json.load(f)


MANUAL_TAGS_FILE = "data/manual_tags.json"

def load_manual_tags():
    if not os.path.exists(MANUAL_TAGS_FILE):
        return {}
    with open(MANUAL_TAGS_FILE, encoding="utf-8") as f:
        return json.load(f)

def save_manual_tags(tags):
    with open(MANUAL_TAGS_FILE, "w", encoding="utf-8") as f:
        json.dump(tags, f, ensure_ascii=False, indent=2)

def calculate_customer_type(reviewer_history: list, manual_tags: dict, reviewer: str, settings: dict) -> str:
    manual_tag = manual_tags.get(reviewer, "")
    if manual_tag:
        return manual_tag
    threshold = settings.get("loyal_threshold", 3)
    count = len(reviewer_history)
    if count == 0:
        return "first"
    if count >= threshold - 1:
        return "loyal"
    return "repeat"


def save_settings(data):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@socketio.on("agent_auth")
def on_agent_auth(data):
    global _agent_sid
    if data.get("token") == os.environ.get("AGENT_TOKEN", ""):
        _agent_sid = request.sid
        emit("agent_ready", {"status": "ok"})
    else:
        disconnect()


@socketio.on("disconnect")
def on_disconnect():
    global _agent_sid
    if request.sid == _agent_sid:
        _agent_sid = None


@socketio.on("login_done")
def on_login_done(data):
    logged_in = data.get("success", False)
    socketio.emit("login_status", {"logged_in": logged_in, **data})


@socketio.on("scrape_done")
def on_scrape_done(data):
    socketio.emit("scrape_status", {"done": True, **data})


@socketio.on("agent_progress")
def on_agent_progress(data):
    socketio.emit("agent_progress", data)


@app.route("/api/agent/status")
def api_agent_status():
    return jsonify({"connected": _agent_sid is not None})


@app.route("/api/login/start", methods=["POST"])
def api_login_start():
    if _agent_sid:
        socketio.emit("do_login", {}, to=_agent_sid)
        return jsonify({"status": "sent_to_agent"})
    if os.environ.get("RAILWAY_ENVIRONMENT"):
        return jsonify({"error": "로컬 에이전트가 연결되어 있지 않습니다."}), 400
    subprocess.Popen([sys.executable, "login.py"])
    return jsonify({"status": "started"})


@app.route("/api/login/status")
def api_login_status():
    cookies_path = os.path.join(PROFILE_DIR, "Default", "Cookies")
    logged_in = os.path.exists(cookies_path)
    return jsonify({"logged_in": logged_in})


@app.route("/api/classify/progress")
def api_classify_progress():
    path = "data/classify_progress.json"
    if not os.path.exists(path):
        return jsonify({"running": False, "done": 0, "total": 0, "step": ""})
    with open(path, encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route("/api/classify", methods=["POST"])
def api_classify():
    """미분류 리뷰 일괄 분류"""
    subprocess.Popen([sys.executable, "classifier.py"])
    mtime = os.path.getmtime(REVIEWS_FILE) if os.path.exists(REVIEWS_FILE) else 0
    return jsonify({"status": "started", "mtime": mtime})


@app.route("/api/reply/generate/<int:idx>", methods=["POST"])
def api_generate_reply(idx):
    """특정 리뷰 AI 답변 생성"""
    reviews = load_reviews()
    if idx >= len(reviews):
        return jsonify({"error": "not found"}), 404

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY 환경변수가 없습니다. export ANTHROPIC_API_KEY=sk-ant-... 후 재실행하세요."}), 400

    try:
        import anthropic
        from classifier import generate_reply, load_brand_tone
        client = anthropic.Anthropic(api_key=api_key)
        brand_tone = load_brand_tone()
        reply = generate_reply(reviews[idx], brand_tone, client)
        auto_reply = load_settings().get("auto_reply", False)
        reviews[idx]["ai_reply"] = reply
        reviews[idx]["reply_status"] = "approved" if auto_reply else "draft"
        save_reviews(reviews)
        return jsonify({"reply": reply, "status": reviews[idx]["reply_status"]})
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


@app.route("/api/review/tag/<int:idx>", methods=["POST"])
def api_tag_review(idx):
    """고객 수동 태그 (gift 등)"""
    reviews = load_reviews()
    if idx >= len(reviews):
        return jsonify({"error": "not found"}), 404
    data = request.get_json() or {}
    tag = data.get("tag", "")
    reviewer = reviews[idx].get("reviewer", "")
    if not reviewer:
        return jsonify({"error": "reviewer 없음"}), 400
    manual_tags = load_manual_tags()
    if tag:
        manual_tags[reviewer] = tag
    else:
        manual_tags.pop(reviewer, None)
    save_manual_tags(manual_tags)
    return jsonify({"status": "ok", "tag": tag})


@app.route("/api/stats/daily")
def api_stats_daily():
    from datetime import date, timedelta
    reviews = load_reviews()
    counts = {}
    for r in reviews:
        d = r.get("date", "")
        if d and len(d) >= 10:
            key = d[:10]
            counts[key] = counts.get(key, 0) + 1
    if counts:
        all_dates = sorted(counts.keys())
        start = all_dates[0]
        end = date.today().isoformat()
        cur = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
        dates, vals = [], []
        while cur <= end_d:
            s = cur.isoformat()
            dates.append(s)
            vals.append(counts.get(s, 0))
            cur += timedelta(days=1)
    else:
        dates, vals = [], []
    return jsonify({"dates": dates, "counts": vals})


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


@app.route("/api/admin/config", methods=["GET"])
def admin_config_get():
    s = load_settings()
    s["anthropic_api_key_set"] = bool(s.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY"))
    s["anthropic_api_key"] = ""  # 키 값 자체는 노출 안 함
    return jsonify(s)


@app.route("/api/admin/config", methods=["POST"])
def admin_config_post():
    data = request.get_json() or {}
    s = load_settings()
    if "auto_reply" in data:
        s["auto_reply"] = bool(data["auto_reply"])
    if "auto_generate_reply" in data:
        s["auto_generate_reply"] = bool(data["auto_generate_reply"])
    if "report_criteria" in data:
        s["report_criteria"] = data["report_criteria"]
    if data.get("anthropic_api_key"):
        s["anthropic_api_key"] = data["anthropic_api_key"]
        os.environ["ANTHROPIC_API_KEY"] = data["anthropic_api_key"]
    if "coupon_rules" in data:
        s["coupon_rules"] = data["coupon_rules"]
    if "loyal_threshold" in data:
        s["loyal_threshold"] = int(data["loyal_threshold"])
    if "customer_type_hints" in data:
        s["customer_type_hints"] = data["customer_type_hints"]
    save_settings(s)
    return jsonify({"status": "ok"})


@app.route("/api/admin/brand-tone", methods=["GET"])
def admin_brand_tone_get():
    content = ""
    if os.path.exists(BRAND_TONE_FILE):
        with open(BRAND_TONE_FILE, encoding="utf-8") as f:
            content = f.read()
    return jsonify({"content": content})


@app.route("/api/admin/brand-tone", methods=["POST"])
def admin_brand_tone_post():
    data = request.get_json() or {}
    with open(BRAND_TONE_FILE, "w", encoding="utf-8") as f:
        f.write(data.get("content", ""))
    return jsonify({"status": "ok"})


@app.route("/api/reply/post/<int:idx>", methods=["POST"])
def api_post_reply(idx):
    """셀러센터에 답글 게시 (Playwright 비동기 실행)"""
    reviews = load_reviews()
    if idx >= len(reviews):
        return jsonify({"error": "not found"}), 404
    r = reviews[idx]
    if not r.get("ai_reply"):
        return jsonify({"error": "게시할 답글이 없습니다."}), 400
    if r.get("replied"):
        return jsonify({"error": "이미 답글이 달린 리뷰입니다."}), 400
    subprocess.Popen([sys.executable, "reply_poster.py", str(idx)])
    return jsonify({"status": "started"})


@app.route("/api/reply/post-progress")
def api_post_progress():
    path = "data/post_progress.json"
    if not os.path.exists(path):
        return jsonify({"running": False, "step": "", "success": None, "error": None})
    with open(path, encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    if _agent_sid:
        socketio.emit("do_scrape", {}, to=_agent_sid)
        return jsonify({"status": "sent_to_agent"})
    if os.environ.get("RAILWAY_ENVIRONMENT"):
        return jsonify({"error": "로컬 에이전트가 연결되어 있지 않습니다."}), 400
    subprocess.Popen([sys.executable, "scraper.py"])
    mtime = os.path.getmtime(REVIEWS_FILE) if os.path.exists(REVIEWS_FILE) else 0
    return jsonify({"status": "started", "mtime": mtime})


@app.route("/api/upload/reviews", methods=["POST"])
def api_upload_reviews():
    """로컬에서 수집한 reviews.json을 Railway로 업로드"""
    token = request.headers.get("X-Upload-Token", "")
    if token != os.environ.get("AGENT_TOKEN", ""):
        return jsonify({"error": "인증 실패"}), 401
    data = request.get_json()
    if not isinstance(data, list):
        return jsonify({"error": "reviews 배열이 필요합니다"}), 400
    os.makedirs("data", exist_ok=True)
    save_reviews(data)
    return jsonify({"status": "ok", "count": len(data)})


@app.route("/api/scrape/status")
def api_scrape_status():
    mtime = os.path.getmtime(REVIEWS_FILE) if os.path.exists(REVIEWS_FILE) else 0
    return jsonify({"mtime": mtime})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    if not os.environ.get("RAILWAY_ENVIRONMENT"):
        import webbrowser, threading
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
