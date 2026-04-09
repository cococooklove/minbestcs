"""
리뷰 뷰어 웹서버
실행: python3 app.py
접속: http://localhost:5000
"""
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO
import json, os, threading, webbrowser, sys, subprocess, math
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

if getattr(sys, 'frozen', False):
    _base_dir = os.path.dirname(sys.executable)
else:
    _base_dir = os.path.dirname(os.path.abspath(__file__))

load_dotenv(os.path.join(_base_dir, ".env"))

IS_SERVER = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_SERVICE_NAME"))

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading", ping_timeout=60, ping_interval=25)

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response
REVIEWS_FILE = os.path.join(_base_dir, "data", "reviews.json")

_reviews_cache = None
_reviews_cache_mtime = 0.0
_reviews_cache_lock = threading.Lock()

_login_pw = None
_login_context = None
_login_page = None
_scraping = False
_session_cookies = None
_progress_step = ""

def _log(msg):
    from datetime import datetime
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

EVENT_LOG_FILE = os.path.join(_base_dir, "data", "event_log.json")

def _log_event(event_type, message, detail=None):
    from datetime import datetime
    entry = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type": event_type, "msg": message}
    if detail:
        entry["detail"] = str(detail)
    try:
        os.makedirs(os.path.dirname(EVENT_LOG_FILE), exist_ok=True)
        logs = []
        if os.path.exists(EVENT_LOG_FILE):
            with open(EVENT_LOG_FILE, encoding="utf-8") as f:
                logs = json.load(f)
        logs.insert(0, entry)
        logs = logs[:200]  # 최대 200개 유지
        with open(EVENT_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(logs, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def ensure_chromium():
    import glob, platform, subprocess
    # 환경변수로 경로가 이미 지정된 경우 (Railway 볼륨, Playwright 베이스 이미지 등)
    _pw_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not _pw_path:
        if platform.system() == "Windows":
            _pw_path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
        elif platform.system() == "Darwin":
            _pw_path = os.path.join(os.path.expanduser("~"), "Library", "Caches", "ms-playwright")
        else:
            _pw_path = os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright")
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _pw_path
    if not glob.glob(os.path.join(_pw_path, "chromium*")):
        socketio.emit("agent_progress", {"step": "Chromium 설치 중 (최초 1회, 이후 유지됩니다)..."})
        from playwright._impl._driver import compute_driver_executable
        driver_executable, driver_cli = compute_driver_executable()
        subprocess.run([str(driver_executable), str(driver_cli), "install", "chromium"], check=True)


def load_reviews():
    global _reviews_cache, _reviews_cache_mtime
    if not os.path.exists(REVIEWS_FILE):
        return []
    with _reviews_cache_lock:
        try:
            mtime = os.path.getmtime(REVIEWS_FILE)
        except OSError:
            return _reviews_cache or []
        if _reviews_cache is not None and mtime == _reviews_cache_mtime:
            return _reviews_cache
        with open(REVIEWS_FILE, encoding="utf-8") as f:
            _reviews_cache = json.load(f)
        _reviews_cache_mtime = mtime
        return _reviews_cache


def save_reviews(reviews):
    with open(REVIEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(reviews, f, ensure_ascii=False, indent=2)


def invalidate_reviews_cache():
    global _reviews_cache, _reviews_cache_mtime
    with _reviews_cache_lock:
        _reviews_cache = None
        _reviews_cache_mtime = 0.0


@app.route("/api/status")
def api_status():
    return jsonify({
        "scraping": _scraping,
        "has_cookies": bool(_session_cookies),
        "step": _progress_step,
    })


@app.route("/")
def index():
    return render_template("index.html", is_server=IS_SERVER)


@app.route("/api/reset", methods=["POST"])
def api_reset():
    global _scraping, _progress_step
    _scraping = False
    _progress_step = ""
    return jsonify({"status": "reset"})


@app.route("/api/latest-review-date")
def api_latest_review_date():
    reviews = load_reviews()
    if not reviews:
        return jsonify({"date": None})
    latest = max((r.get("date", "") for r in reviews if r.get("date")), default=None)
    return jsonify({"date": latest})


@app.route("/api/reviews/clear", methods=["POST"])
def api_reviews_clear():
    if os.path.exists(REVIEWS_FILE):
        os.remove(REVIEWS_FILE)
    return jsonify({"ok": True})


@app.route("/api/screenshot")
def api_screenshot():
    """가장 최근 스크린샷 반환 (디버그용)"""
    from flask import send_file
    screenshot_dir = os.path.join(_base_dir, "data", "screenshots")
    if not os.path.exists(screenshot_dir):
        return jsonify({"error": "스크린샷 없음"}), 404
    files = sorted(
        [f for f in os.listdir(screenshot_dir) if f.endswith(".png")],
        reverse=True
    )
    if not files:
        return jsonify({"error": "스크린샷 없음"}), 404
    # ?all=1 이면 목록 반환
    if request.args.get("all"):
        return jsonify({"files": files[:10]})
    # ?file=filename 이면 특정 파일 반환
    req_file = request.args.get("file")
    if req_file:
        target = os.path.join(screenshot_dir, os.path.basename(req_file))
        if not os.path.exists(target):
            return jsonify({"error": "파일 없음"}), 404
        return send_file(target, mimetype="image/png")
    return send_file(os.path.join(screenshot_dir, files[0]), mimetype="image/png")


@app.route("/api/client-progress", methods=["POST"])
def api_client_progress():
    """확장프로그램에서 보내는 진행 상황을 UI에 emit"""
    global _scraping, _progress_step
    data = request.json or {}
    step = data.get("step", "")
    _progress_step = step
    _log(f"[EXT] {step}")
    if step.startswith("실패"):
        _scraping = False
        socketio.emit("collect_status", {"step": "done", "success": False, "error": step})
    elif step == "완료":
        _scraping = False
        _log("✅ 확장프로그램 수집 완료")
        socketio.emit("collect_status", {"step": "done", "success": True})
    else:
        _scraping = True
        socketio.emit("agent_progress", {"step": step})
    return jsonify({"ok": True})


@app.route("/api/log", methods=["POST"])
def api_log():
    """확장프로그램에서 보내는 디버그 로그"""
    data = request.json or {}
    msg = data.get("msg", "")
    if msg:
        _log(f"[EXT-DBG] {msg}")
    return jsonify({"ok": True})


@app.route("/api/upload-excel", methods=["POST"])
def api_upload_excel():
    """확장프로그램이 직접 다운로드한 엑셀 파일을 받아 파싱·저장"""
    global _scraping, _progress_step
    if "file" not in request.files:
        return jsonify({"error": "파일 없음"}), 400
    f = request.files["file"]
    os.makedirs(os.path.join(_base_dir, "data", "downloads"), exist_ok=True)
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    excel_path = os.path.join(_base_dir, "data", "downloads", f"reviews_{timestamp}.xlsx")
    f.save(excel_path)

    def _parse_and_save():
        global _scraping, _progress_step
        try:
            import scraper as scraper_mod
            scraper_mod.OUTPUT_FILE = REVIEWS_FILE
            _progress_step = "엑셀 파싱 중..."
            socketio.emit("agent_progress", {"step": _progress_step})
            new_reviews = scraper_mod.excel_to_reviews(excel_path)
            _progress_step = f"파싱 완료: {len(new_reviews)}건"
            socketio.emit("agent_progress", {"step": _progress_step})

            existing = []
            if os.path.exists(REVIEWS_FILE):
                with open(REVIEWS_FILE, encoding="utf-8") as fp:
                    existing = json.load(fp)
            existing_keys = {(r.get("order_no"), r.get("reviewer"), r.get("date")) for r in existing}
            added = [r for r in new_reviews if (r.get("order_no"), r.get("reviewer"), r.get("date")) not in existing_keys]
            merged = added + existing
            os.makedirs(os.path.dirname(REVIEWS_FILE), exist_ok=True)
            with open(REVIEWS_FILE, "w", encoding="utf-8") as fp:
                json.dump(merged, fp, ensure_ascii=False, indent=2)

            _scraping = False
            _log(f"✅ 엑셀 업로드 성공: {len(added)}건 신규 / 전체 {len(merged)}건")
            socketio.emit("collect_status", {"step": "done", "success": True})
        except Exception as e:
            _scraping = False
            _log(f"❌ 엑셀 업로드 실패: {e}")
            socketio.emit("collect_status", {"step": "done", "success": False, "error": str(e)})

    threading.Thread(target=_parse_and_save, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/cookies", methods=["POST"])
def api_receive_cookies():
    global _session_cookies, _scraping
    if _scraping:
        return jsonify({"error": "수집 중입니다. 잠시 기다려주세요."}), 400
    data = request.json or {}
    cookies = data.get("cookies", [])
    _log(f"[EXT-DBG] 쿠키 수신 요청: {len(cookies)}개")
    if not cookies:
        return jsonify({"error": "쿠키가 없습니다. 네이버에 로그인 후 다시 시도해주세요."}), 400
    _session_cookies = cookies
    _log("🟡 수집 시작 (쿠키 수신)")
    socketio.emit("collect_status", {"step": "cookies_received"})
    threading.Thread(target=_run_server_collect, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/extension/download")
def download_extension():
    import io, zipfile
    from flask import send_file
    server_url = request.host_url.rstrip('/')
    ext_dir = os.path.join(_base_dir, "extension")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fname in ['manifest.json', 'popup.html', 'popup.js', 'background.js', 'content_main.js', 'content.js']:
            fpath = os.path.join(ext_dir, fname)
            if os.path.exists(fpath):
                zf.write(fpath, fname)
        zf.writestr('config.js', f'const SERVER_URL = "{server_url}";')
    buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name='민베스트_확장프로그램.zip')


@app.route("/api/reviews")
def api_reviews():
    all_r = load_reviews()  # 단 1회 로드

    q = request.args.get("q", "").strip().lower()
    rating = request.args.get("rating", "")
    replied = request.args.get("replied", "")
    sentiment = request.args.get("sentiment", "")
    topic = request.args.get("topic", "")
    reportable = request.args.get("reportable", "")
    sort = request.args.get("sort", "newest")
    date_from = request.args.get("date_from", "")  # YYYY-MM-DD
    date_to   = request.args.get("date_to",   "")  # YYYY-MM-DD
    page     = max(1, int(request.args.get("page", 1)))
    per_page = max(10, min(200, int(request.args.get("per_page", 50))))

    # 단일 패스 필터
    def _matches(i):
        r = all_r[i]
        if q and not (q in (r.get("content") or "").lower()
                      or q in (r.get("product") or "").lower()
                      or q in (r.get("reviewer") or "").lower()):
            return False
        if rating and not str(r.get("rating", "")).startswith(rating):
            return False
        if replied == "yes" and not r.get("replied"):
            return False
        if replied == "no" and r.get("replied"):
            return False
        if sentiment and r.get("sentiment") != sentiment:
            return False
        if topic and topic not in (r.get("topics") or []):
            return False
        if reportable == "yes" and not r.get("reportable"):
            return False
        if date_from and (r.get("date") or "") < date_from:
            return False
        if date_to and (r.get("date") or "") > date_to:
            return False
        return True

    reviews_idx = [i for i in range(len(all_r)) if _matches(i)]

    sort_key = {
        "oldest":      lambda i: all_r[i].get("date", ""),
        "rating_high": lambda i: float(all_r[i].get("rating", 0) or 0),
        "rating_low":  lambda i: float(all_r[i].get("rating", 0) or 0),
        "newest":      lambda i: all_r[i].get("date", ""),
    }
    reviews_idx = sorted(reviews_idx, key=sort_key.get(sort, sort_key["newest"]),
                         reverse=sort not in ("oldest", "rating_low"))

    # 페이지네이션 슬라이싱
    total_filtered = len(reviews_idx)
    start = (page - 1) * per_page
    reviews_idx_page = reviews_idx[start: start + per_page]

    # 통계
    ratings = [float(r.get("rating", 0) or 0) for r in all_r if r.get("rating")]
    stats = {
        "total": len(all_r),
        "filtered": total_filtered,
        "avg_rating": round(sum(ratings) / len(ratings), 1) if ratings else 0,
        "replied_count": sum(1 for r in all_r if r.get("replied")),
        "rating_dist": {str(i): sum(1 for r in ratings if int(r) == i) for i in range(1, 6)},
        "sentiment_dist": {
            "positive":    sum(1 for r in all_r if r.get("sentiment") == "positive"),
            "negative":    sum(1 for r in all_r if r.get("sentiment") == "negative"),
            "mixed":       sum(1 for r in all_r if r.get("sentiment") == "mixed"),
            "unclassified":sum(1 for r in all_r if not r.get("sentiment")),
        },
        "reportable_count":  sum(1 for r in all_r if r.get("reportable")),
        "draft_count":       sum(1 for r in all_r if r.get("reply_status") == "draft"),
        "need_reply_count":  sum(1 for r in all_r if not r.get("replied") and not r.get("ai_reply")),
    }

    # reviewer → 인덱스 목록 맵 (O(n) 선처리)
    reviewer_map = {}
    for i, r in enumerate(all_r):
        rv = r.get("reviewer", "")
        if rv:
            reviewer_map.setdefault(rv, []).append(i)

    settings_data = load_settings()
    manual_tags   = load_manual_tags()

    indexed_reviews = []
    for i in reviews_idx_page:
        r = all_r[i]
        history = [
            {
                "date":    all_r[j].get("date"),
                "rating":  all_r[j].get("rating"),
                "product": all_r[j].get("product"),
                "content": all_r[j].get("content"),
                "replied": all_r[j].get("replied"),
            }
            for j in reviewer_map.get(r.get("reviewer", ""), [])
            if j != i
        ]
        history.sort(key=lambda h: h.get("date") or "", reverse=True)
        customer_type = calculate_customer_type(history, manual_tags, r.get("reviewer", ""), settings_data)
        indexed_reviews.append({"_idx": i, **r, "reviewer_history": history, "customer_type": customer_type})

    return jsonify({
        "reviews": indexed_reviews,
        "stats": stats,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total_filtered": total_filtered,
            "total_pages": math.ceil(total_filtered / per_page) if total_filtered else 1,
        }
    })


PROFILE_DIR = os.path.join(_base_dir, "data", "browser_profile")
SETTINGS_FILE = os.path.join(_base_dir, "config", "settings.json")
BRAND_TONE_FILE = os.path.join(_base_dir, "config", "brand_tone.txt")


def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return {"auto_reply": False, "report_criteria": ["욕설", "경쟁사 언급", "광고성", "반복 내용"], "openai_api_key": ""}
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


@app.route("/api/login/start", methods=["POST"])
def api_login_start():
    def run_login():
        global _login_pw, _login_context, _login_page
        try:
            ensure_chromium()
            import login as login_mod
            login_mod.PROFILE_DIR = PROFILE_DIR
            success, pw, context, page = login_mod.main(keep_open=True)
            if success:
                _login_pw, _login_context, _login_page = pw, context, page
            socketio.emit("login_status", {"logged_in": bool(success)})
        except Exception as e:
            socketio.emit("login_status", {"logged_in": False, "error": str(e)})
    threading.Thread(target=run_login, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/login/status")
def api_login_status():
    cookies_path = os.path.join(PROFILE_DIR, "Default", "Cookies")
    logged_in = os.path.exists(cookies_path)
    return jsonify({"logged_in": logged_in})


def _run_server_collect():
    """IS_SERVER 모드: 쿠키로 headless 수집"""
    global _scraping, _progress_step

    def _progress(msg):
        global _progress_step
        _progress_step = msg
        socketio.emit("agent_progress", {"step": msg})

    _scraping = True
    _progress_step = "수집 준비 중..."
    try:
        socketio.emit("collect_status", {"step": "scraping"})
        ensure_chromium()
        import scraper
        scraper.PROFILE_DIR = PROFILE_DIR
        scraper.OUTPUT_FILE = REVIEWS_FILE
        scraper.DOWNLOAD_DIR = Path(os.path.join(_base_dir, "data", "downloads"))
        scraper.main(
            progress_cb=_progress,
            cookies=_session_cookies,
            headless=True,
        )
        _progress_step = "완료"
        _log("✅ 수집 성공")
        socketio.emit("collect_status", {"step": "done", "success": True})
    except Exception as e:
        _progress_step = f"실패: {e}"
        _log(f"❌ 수집 실패: {e}")
        socketio.emit("collect_status", {"step": "done", "success": False, "error": str(e)})
    finally:
        _scraping = False


@app.route("/api/collect", methods=["POST"])
def api_collect():
    global _scraping
    if _scraping:
        return jsonify({"error": "수집 중입니다. 잠시 기다려주세요."}), 400
    if IS_SERVER:
        if not _session_cookies:
            return jsonify({"error": "확장 프로그램에서 수집을 시작해주세요."}), 400
        threading.Thread(target=_run_server_collect, daemon=True).start()
        return jsonify({"status": "started"})
    def run_collect():
        global _login_pw, _login_context, _login_page, _scraping
        try:
            if _login_page is None:
                socketio.emit("collect_status", {"step": "login_start"})
                ensure_chromium()
                import login as login_mod
                login_mod.PROFILE_DIR = PROFILE_DIR
                success, pw, context, page = login_mod.main(keep_open=True)
                if not success:
                    socketio.emit("collect_status", {"step": "login_failed", "error": "로그인 시간 초과"})
                    return
                _login_pw, _login_context, _login_page = pw, context, page
                socketio.emit("collect_status", {"step": "login_done"})
            _scraping = True
            socketio.emit("collect_status", {"step": "scraping"})
            ensure_chromium()
            import scraper
            scraper.PROFILE_DIR = PROFILE_DIR
            scraper.OUTPUT_FILE = REVIEWS_FILE
            scraper.DOWNLOAD_DIR = Path(os.path.join(_base_dir, "data", "downloads"))
            scraper.main(
                progress_cb=lambda msg: socketio.emit("agent_progress", {"step": msg}),
                existing_page=_login_page,
            )
            socketio.emit("collect_status", {"step": "done", "success": True})
        except Exception as e:
            socketio.emit("collect_status", {"step": "done", "success": False, "error": str(e)})
        finally:
            _scraping = False
    threading.Thread(target=run_collect, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/classify/progress")
def api_classify_progress():
    path = "data/classify_progress.json"
    if not os.path.exists(path):
        return jsonify({"running": False, "done": 0, "total": 0, "step": ""})
    with open(path, encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route("/api/classify/count")
def api_classify_count():
    """기간별 미분류 리뷰 건수 반환"""
    from datetime import datetime, timedelta
    reviews = load_reviews()
    periods = {"30": 30, "90": 90, "180": 180, "365": 365, "0": 0}
    result = {}
    for key, days in periods.items():
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d") if days > 0 else None
        count = sum(1 for r in reviews
                    if r.get("sentiment") is None and not r.get("replied")
                    and (cutoff is None or r.get("date", "") >= cutoff))
        result[key] = count
    return jsonify(result)


@app.route("/api/classify", methods=["POST"])
def api_classify():
    """미분류 리뷰 일괄 분류"""
    data = request.get_json() or {}
    days = str(data.get("days", 365))
    _log(f"🤖 AI 분석 시작 ({days}일 기준)")
    subprocess.Popen([sys.executable, "classifier.py", "--days", days], cwd=_base_dir)
    mtime = os.path.getmtime(REVIEWS_FILE) if os.path.exists(REVIEWS_FILE) else 0
    return jsonify({"status": "started", "mtime": mtime})


@app.route("/api/reply/generate/<int:idx>", methods=["POST"])
def api_generate_reply(idx):
    """특정 리뷰 AI 답변 생성"""
    reviews = load_reviews()
    if idx >= len(reviews):
        return jsonify({"error": "not found"}), 404

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return jsonify({"error": "OPENAI_API_KEY 환경변수가 없습니다. export OPENAI_API_KEY=sk-... 후 재실행하세요."}), 400

    try:
        from openai import OpenAI
        from classifier import generate_reply, load_brand_tone
        client = OpenAI(api_key=api_key)
        brand_tone = load_brand_tone()
        settings_data = load_settings()
        reply = generate_reply(reviews[idx], brand_tone, client, settings=settings_data)
        auto_reply = settings_data.get("auto_reply", False)
        reviews[idx]["ai_reply"] = reply
        reviews[idx]["reply_status"] = "approved" if auto_reply else "draft"
        save_reviews(reviews)
        invalidate_reviews_cache()
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
    if "reply" in data:
        old_reply = reviews[idx].get("ai_reply", "")
        new_reply = data["reply"]
        if old_reply and old_reply != new_reply:
            history = reviews[idx].get("reply_history", [])
            history.insert(0, {"text": old_reply, "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M")})
            reviews[idx]["reply_history"] = history[:5]
        reviews[idx]["ai_reply"] = new_reply
    reviews[idx]["reply_status"] = "approved"
    save_reviews(reviews)
    invalidate_reviews_cache()
    threading.Thread(target=maybe_auto_finetune, daemon=True).start()
    return jsonify({"status": "approved"})


@app.route("/api/reply/reject/<int:idx>", methods=["POST"])
def api_reject_reply(idx):
    """답변 초안 거절"""
    reviews = load_reviews()
    if idx >= len(reviews):
        return jsonify({"error": "not found"}), 404
    old_reply = reviews[idx].get("ai_reply", "")
    if old_reply:
        history = reviews[idx].get("reply_history", [])
        history.insert(0, {"text": old_reply, "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M")})
        reviews[idx]["reply_history"] = history[:5]
    reviews[idx]["ai_reply"] = ""
    reviews[idx]["reply_status"] = "none"
    save_reviews(reviews)
    invalidate_reviews_cache()
    return jsonify({"status": "rejected"})


def _insert_coupon_text(ai_reply, coupon_text):
    """쿠폰 문구를 AI 답변의 적절한 위치(마지막 단락 앞)에 삽입. 삽입된 블록 텍스트도 반환."""
    block = "\n\n" + coupon_text
    if not ai_reply:
        return coupon_text, coupon_text
    paragraphs = ai_reply.split("\n\n")
    if len(paragraphs) >= 2:
        paragraphs.insert(-1, coupon_text)
    else:
        paragraphs.append(coupon_text)
    return "\n\n".join(paragraphs), block


@app.route("/api/coupon/approve/<int:idx>", methods=["POST"])
def api_coupon_approve(idx):
    """쿠폰 승인"""
    reviews = load_reviews()
    if idx >= len(reviews):
        return jsonify({"error": "not found"}), 404
    review = reviews[idx]
    reviews[idx]["coupon_status"] = "approved"

    settings = load_settings()
    coupon_rules = settings.get("coupon_rules", [])
    coupon_tmpl = settings.get("reply_coupon_template", "감사의 마음을 담아 {coupon}({amount})을 발급해드렸으니 다음 구매에 꼭 활용해 주세요.")
    matched_coupon = None
    for rule in coupon_rules:
        if not rule.get("enabled"):
            continue
        cond = rule.get("condition", "")
        if cond == "rating_5_photo" and float(review.get("rating") or 0) >= 5:
            matched_coupon = rule; break
        elif cond == "content_100" and len(review.get("content") or "") >= 100:
            matched_coupon = rule; break
        elif cond == "repurchase" and len(review.get("reviewer_history") or []) >= 1:
            matched_coupon = rule; break

    if matched_coupon:
        name = matched_coupon.get("coupon", "")
        amount = (matched_coupon.get("coupon_amount") or "").strip()
        min_purchase = (matched_coupon.get("min_purchase_amount") or "").strip()
        if amount:
            coupon_text = coupon_tmpl.replace("{coupon}", name).replace("{amount}", amount)
        else:
            coupon_text = coupon_tmpl.replace("{coupon}", name).replace("({amount})", "").replace("{amount}", "").strip()
        if min_purchase:
            coupon_text += f" (사용 조건: {min_purchase} 구매 시)"

        existing = review.get("ai_reply") or ""
        prev = review.get("coupon_appended_text", "")
        if prev and prev in existing:
            existing = existing.replace(prev, "", 1).strip("\n")
        new_reply, block = _insert_coupon_text(existing, coupon_text)
        reviews[idx]["ai_reply"] = new_reply
        reviews[idx]["coupon_appended_text"] = block

    save_reviews(reviews)
    invalidate_reviews_cache()
    return jsonify({"status": "approved"})


@app.route("/api/coupon/manual/<int:idx>", methods=["POST"])
def api_coupon_manual(idx):
    """수동 쿠폰 발급"""
    reviews = load_reviews()
    if idx >= len(reviews):
        return jsonify({"error": "not found"}), 404
    data = request.get_json() or {}
    purchase_amount = data.get("purchase_amount")
    coupon = (data.get("coupon") or "").strip()
    coupon_amount_text = ""

    if purchase_amount is not None:
        settings = load_settings()
        amount_rules = [
            r for r in settings.get("coupon_rules", [])
            if r.get("condition") == "manual_amount" and r.get("enabled") and r.get("min_amount")
        ]
        amount_rules.sort(key=lambda r: r.get("min_amount", 0), reverse=True)
        matched = next((r for r in amount_rules if purchase_amount >= r["min_amount"]), None)
        if not matched:
            return jsonify({"status": "no_match"})
        coupon = matched.get("coupon", "")
        coupon_amount_text = matched.get("coupon_amount", "")

    if not coupon:
        return jsonify({"error": "쿠폰명 필요"}), 400

    review = reviews[idx]
    reviews[idx]["coupon_status"] = "manual"
    reviews[idx]["manual_coupon"] = coupon

    if coupon_amount_text:
        settings = load_settings()
        tmpl = settings.get("reply_coupon_template", "감사의 마음을 담아 {coupon}({amount})을 발급해드렸으니 다음 구매에 꼭 활용해 주세요.")
        coupon_text = tmpl.replace("{coupon}", coupon).replace("{amount}", coupon_amount_text)
    else:
        coupon_text = f"감사의 마음을 담아 {coupon}을 발급해드렸으니 다음 구매에 꼭 활용해 주세요."

    existing = review.get("ai_reply") or ""
    prev = review.get("coupon_appended_text", "")
    if prev and prev in existing:
        existing = existing.replace(prev, "", 1).strip("\n")
    new_reply, block = _insert_coupon_text(existing, coupon_text)
    reviews[idx]["ai_reply"] = new_reply
    reviews[idx]["coupon_appended_text"] = block

    save_reviews(reviews)
    invalidate_reviews_cache()
    return jsonify({"status": "manual", "coupon": coupon})


@app.route("/api/coupon/revoke/<int:idx>", methods=["POST"])
def api_coupon_revoke(idx):
    """쿠폰 승인 취소"""
    reviews = load_reviews()
    if idx >= len(reviews):
        return jsonify({"error": "not found"}), 404
    review = reviews[idx]
    prev = review.get("coupon_appended_text", "")
    if prev:
        existing = review.get("ai_reply") or ""
        if prev in existing:
            reviews[idx]["ai_reply"] = existing.replace(prev, "", 1).strip("\n")
        reviews[idx]["coupon_appended_text"] = ""
    reviews[idx]["coupon_status"] = "none"
    reviews[idx]["manual_coupon"] = ""
    save_reviews(reviews)
    invalidate_reviews_cache()
    return jsonify({"status": "none"})


@app.route("/api/approve/all/<int:idx>", methods=["POST"])
def api_approve_all(idx):
    """쿠폰 + 답변 동시 승인"""
    reviews = load_reviews()
    if idx >= len(reviews):
        return jsonify({"error": "not found"}), 404
    data = request.get_json() or {}
    approve_reply  = data.get("approve_reply",  True)
    approve_coupon = data.get("approve_coupon", True)
    if approve_reply:
        if "reply" in data:
            reviews[idx]["ai_reply"] = data["reply"]
        reviews[idx]["reply_status"] = "approved"
    if approve_coupon:
        reviews[idx]["coupon_status"] = "approved"
    save_reviews(reviews)
    invalidate_reviews_cache()
    if approve_reply:
        threading.Thread(target=maybe_auto_finetune, daemon=True).start()
    return jsonify({"status": "ok"})


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
    s["openai_api_key_set"] = bool(s.get("openai_api_key") or os.environ.get("OPENAI_API_KEY"))
    s["openai_api_key"] = ""  # 키 값 자체는 노출 안 함
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
    if data.get("openai_api_key"):
        s["openai_api_key"] = data["openai_api_key"]
        os.environ["OPENAI_API_KEY"] = data["openai_api_key"]
    if "coupon_rules" in data:
        s["coupon_rules"] = data["coupon_rules"]
    if "reply_coupon_template" in data:
        s["reply_coupon_template"] = data["reply_coupon_template"]
    if "sensitive_expressions" in data:
        s["sensitive_expressions"] = data["sensitive_expressions"]
    if "loyal_threshold" in data:
        s["loyal_threshold"] = int(data["loyal_threshold"])
    if "finetune_auto_threshold" in data:
        s["finetune_auto_threshold"] = int(data.get("finetune_auto_threshold") or 0)
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


_finetune_running = False
_finetune_lock = threading.Lock()


def _run_finetune_job():
    """백그라운드 파인튜닝 실행 (공통)"""
    global _finetune_running
    import io
    try:
        settings = load_settings()
        reviews = load_reviews()
        approved = [r for r in reviews if r.get("reply_status") == "approved" and r.get("ai_reply")]
        if len(approved) < 10:
            _log(f"파인튜닝 스킵: 승인 답변 {len(approved)}개 (최소 10개 필요)")
            return

        brand_tone = ""
        if os.path.exists(BRAND_TONE_FILE):
            with open(BRAND_TONE_FILE, encoding="utf-8") as f:
                brand_tone = f.read()

        lines = []
        for r in approved:
            user_msg = f"리뷰 내용: {r.get('content', '')}\n별점: {r.get('rating', '')}점\n상품: {r.get('product', '')}"
            entry = {
                "messages": [
                    {"role": "system", "content": f"당신은 건강기능식품 브랜드의 고객 담당자입니다.\n{brand_tone}\n답변만 출력하세요."},
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": r["ai_reply"]},
                ]
            }
            lines.append(json.dumps(entry, ensure_ascii=False))
        jsonl_bytes = "\n".join(lines).encode("utf-8")

        from openai import OpenAI
        api_key = os.environ.get("OPENAI_API_KEY", settings.get("openai_api_key", ""))
        client = OpenAI(api_key=api_key)
        file_obj = client.files.create(
            file=("finetune_data.jsonl", io.BytesIO(jsonl_bytes), "application/json"),
            purpose="fine-tune",
        )
        job = client.fine_tuning.jobs.create(training_file=file_obj.id, model="gpt-4o-mini")
        settings = load_settings()
        settings["finetune_job_id"] = job.id
        settings["finetune_last_count"] = len(approved)
        save_settings(settings)
        _log(f"파인튜닝 시작: job_id={job.id}, 학습 데이터={len(approved)}개")
    except Exception as e:
        _log(f"파인튜닝 오류: {e}")
    finally:
        global _finetune_running
        _finetune_running = False


def maybe_auto_finetune():
    """승인 답변 누적 임계값 초과 시 자동 파인튜닝 트리거"""
    global _finetune_running
    settings = load_settings()
    threshold = settings.get("finetune_auto_threshold", 0)
    if not threshold:
        return
    if settings.get("finetune_job_id"):  # 진행 중인 job 있으면 스킵
        return
    with _finetune_lock:
        if _finetune_running:
            return

    reviews = load_reviews()
    current_approved = sum(1 for r in reviews if r.get("reply_status") == "approved" and r.get("ai_reply"))
    last_count = settings.get("finetune_last_count", 0)

    if current_approved - last_count < threshold:
        return

    with _finetune_lock:
        if _finetune_running:
            return
        _finetune_running = True

    _log(f"자동 파인튜닝 트리거: 승인 {current_approved}개 (이전 {last_count}개, 임계값 {threshold})")
    threading.Thread(target=_run_finetune_job, daemon=True).start()


@app.route("/api/admin/finetune/status", methods=["GET"])
def finetune_status():
    """파인튜닝 상태 조회"""
    settings = load_settings()
    reviews = load_reviews()
    training_count = sum(
        1 for r in reviews
        if r.get("reply_status") == "approved" and r.get("ai_reply")
    )
    result = {
        "training_count": training_count,
        "active_model": settings.get("active_model", "gpt-4o-mini"),
        "job_id": settings.get("finetune_job_id", ""),
        "job_status": "none",
        "fine_tuned_model": "",
    }
    result["auto_threshold"] = settings.get("finetune_auto_threshold", 0)
    result["last_count"] = settings.get("finetune_last_count", 0)
    job_id = settings.get("finetune_job_id", "")
    if job_id:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", settings.get("openai_api_key", "")))
            job = client.fine_tuning.jobs.retrieve(job_id)
            result["job_status"] = job.status
            result["fine_tuned_model"] = job.fine_tuned_model or ""
            # 완료 시 자동 활성화
            if job.status == "succeeded" and job.fine_tuned_model:
                s = load_settings()
                if s.get("active_model") != job.fine_tuned_model:
                    s["active_model"] = job.fine_tuned_model
                    s["finetune_job_id"] = ""
                    save_settings(s)
                    result["active_model"] = job.fine_tuned_model
                    result["job_id"] = ""
                    _log(f"파인튜닝 완료 — 자동 전환: {job.fine_tuned_model}")
        except Exception as e:
            result["job_status"] = f"error: {e}"
    return jsonify(result)


@app.route("/api/admin/finetune/start", methods=["POST"])
def finetune_start():
    """파인튜닝 수동 시작"""
    global _finetune_running
    settings = load_settings()
    if settings.get("finetune_job_id"):
        return jsonify({"error": "이미 진행 중인 파인튜닝이 있습니다."}), 400
    reviews = load_reviews()
    approved_count = sum(1 for r in reviews if r.get("reply_status") == "approved" and r.get("ai_reply"))
    if approved_count < 10:
        return jsonify({"error": f"승인된 답변이 {approved_count}개입니다. 최소 10개 필요합니다."}), 400
    with _finetune_lock:
        if _finetune_running:
            return jsonify({"error": "이미 파인튜닝이 실행 중입니다."}), 400
        _finetune_running = True
    threading.Thread(target=_run_finetune_job, daemon=True).start()
    return jsonify({"status": "started", "training_count": approved_count})


@app.route("/api/admin/finetune/activate", methods=["POST"])
def finetune_activate():
    """파인튜닝 완료된 모델을 활성 모델로 전환"""
    data = request.get_json() or {}
    model_id = (data.get("model_id") or "").strip()
    if not model_id:
        return jsonify({"error": "model_id 필요"}), 400
    settings = load_settings()
    settings["active_model"] = model_id
    save_settings(settings)
    return jsonify({"status": "ok", "active_model": model_id})


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
    global _scraping
    if _scraping:
        return jsonify({"error": "수집 중입니다. 잠시 기다려주세요."}), 400
    if _login_page is None:
        return jsonify({"error": "로그인 먼저 해주세요."}), 400
    def run_scrape():
        global _scraping
        _scraping = True
        try:
            ensure_chromium()
            import scraper
            scraper.PROFILE_DIR = PROFILE_DIR
            scraper.OUTPUT_FILE = REVIEWS_FILE
            scraper.DOWNLOAD_DIR = Path(os.path.join(_base_dir, "data", "downloads"))
            scraper.main(
                progress_cb=lambda msg: socketio.emit("agent_progress", {"step": msg}),
                existing_page=_login_page,
            )
            socketio.emit("scrape_status", {"done": True, "success": True})
        except Exception as e:
            socketio.emit("scrape_status", {"done": True, "success": False, "error": str(e)})
        finally:
            _scraping = False
    threading.Thread(target=run_scrape, daemon=True).start()
    mtime = os.path.getmtime(REVIEWS_FILE) if os.path.exists(REVIEWS_FILE) else 0
    return jsonify({"status": "started", "mtime": mtime})


@app.route("/api/scrape/status")
def api_scrape_status():
    mtime = os.path.getmtime(REVIEWS_FILE) if os.path.exists(REVIEWS_FILE) else 0
    return jsonify({"mtime": mtime})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    socketio.run(app, host="127.0.0.1", port=port, debug=False, allow_unsafe_werkzeug=True)
