"""
리뷰 뷰어 웹서버
실행: python3 app.py
접속: http://localhost:5000
"""
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO
import json, os, threading, webbrowser, sys, subprocess, math, queue, base64
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
if not IS_SERVER:
    # 로컬: templates/*.html, static/* 수정 시 서버 재시작 없이 즉시 반영
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading", ping_timeout=60, ping_interval=25)

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    if response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
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

# 캡차 답안 채널 — 로그인 스레드와 HTTP 엔드포인트 사이의 동기 큐
_captcha_q: "queue.Queue[str|None]" = queue.Queue()


def _b64_image(b: bytes | None) -> str | None:
    if not b:
        return None
    return "data:image/png;base64," + base64.b64encode(b).decode("ascii")


def _make_on_captcha():
    """auto_login에 전달할 동기 콜백. 호출되면 모달 띄우고 답안 큐를 블로킹."""
    # 이전 잔존 답안 비우기 (가짜 통과 방지)
    while not _captcha_q.empty():
        try: _captcha_q.get_nowait()
        except queue.Empty: break

    def _on_captcha(data):
        timeout = int(data.get("timeout") or 180)
        try:
            socketio.emit("captcha_required", {
                "image": _b64_image(data.get("image")),
                "hint": data.get("hint"),
                "attempt": data.get("attempt", 1),
                "timeout": timeout,
            })
        except Exception as e:
            print(f"[on_captcha] emit 실패: {e}")
            return None
        try:
            answer = _captcha_q.get(timeout=timeout)
        except queue.Empty:
            print("[on_captcha] 답안 타임아웃")
            try: socketio.emit("captcha_done", {"reason": "timeout"})
            except Exception: pass
            return None
        try: socketio.emit("captcha_done", {"reason": "submitted"})
        except Exception: pass
        return answer
    return _on_captcha

# 클라이언트별 세션 정보 (멀티 클라이언트 대응 — 우선 메모리 보관, 만료시간 표시용)
# { client_id: {"expires_at": float, "cookies": list, "updated_at": float} }
_sessions_by_client = {}
_sessions_lock = threading.Lock()

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
    client_id = (data.get("client_id") or "").strip() or None
    _log(f"[EXT-DBG] 쿠키 수신 요청: {len(cookies)}개 (client_id={client_id})")
    if not cookies:
        return jsonify({"error": "쿠키가 없습니다. 네이버에 로그인 후 다시 시도해주세요."}), 400
    _session_cookies = cookies
    if client_id:
        import time
        with _sessions_lock:
            _sessions_by_client[client_id] = {
                "expires_at": _earliest_expires(cookies, "expirationDate"),
                "cookies": cookies,
                "updated_at": time.time(),
            }
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
    refund = request.args.get("refund", "")
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
                      or q in (r.get("reviewer") or "").lower()
                      or q in (r.get("order_no") or "").lower()):
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
        if refund == "completed" and r.get("refund_status") != "completed":
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
        "refund_count":      sum(1 for r in all_r if r.get("refund_status") == "completed"),
        "draft_count":       sum(1 for r in all_r if r.get("reply_status") == "draft"),
        "needs_review_count":sum(1 for r in all_r if r.get("reply_status") == "needs_review"),
        "need_reply_count":  sum(1 for r in all_r if not r.get("replied") and not r.get("ai_reply")),
        "approved_pending_count": sum(
            1 for r in all_r
            if r.get("reply_status") == "approved" and not r.get("replied") and (r.get("review_id") or "").strip()
        ),
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
    body = request.get_json(silent=True) or {}
    naver_id = (body.get("naver_id") or "").strip()
    naver_pw = body.get("naver_pw") or ""
    client_id = (body.get("client_id") or "").strip() or None
    def run_login():
        global _login_pw, _login_context, _login_page
        try:
            ensure_chromium()
            import auto_login as login_mod
            login_mod.PROFILE_DIR = PROFILE_DIR
            success, pw, context, page = login_mod.main(
                keep_open=True, naver_id=naver_id, naver_pw=naver_pw,
                on_captcha=_make_on_captcha(),
            )
            if success:
                _login_pw, _login_context, _login_page = pw, context, page
                if client_id and os.path.exists(SESSION_STATE_PATH):
                    try:
                        import time as _t
                        with open(SESSION_STATE_PATH, encoding="utf-8") as f:
                            state = json.load(f)
                        with _sessions_lock:
                            _sessions_by_client[client_id] = {
                                "expires_at": _earliest_expires(state.get("cookies", []), "expires"),
                                "cookies": state.get("cookies", []),
                                "updated_at": _t.time(),
                            }
                    except Exception:
                        pass
            socketio.emit("login_status", {"logged_in": bool(success)})
        except Exception as e:
            socketio.emit("login_status", {"logged_in": False, "error": str(e)})
    threading.Thread(target=run_login, daemon=True).start()
    return jsonify({"status": "started"})


SESSION_STATE_PATH = os.path.join(_base_dir, "data", "session_state.json")
# 셀러센터(smartstore) 세션 만료를 가장 잘 반영하는 쿠키들. 첫 매칭의 expires를 사용.
_SESSION_COOKIE_NAMES = ("kit.session", "NACT")


def _earliest_expires(cookies, expires_key):
    """쿠키 리스트에서 세션 쿠키들의 expires 최소값을 반환. 없으면 None."""
    import time
    vals = []
    for c in cookies or []:
        if c.get("name") in _SESSION_COOKIE_NAMES:
            exp = c.get(expires_key)
            if isinstance(exp, (int, float)) and exp > 0:
                vals.append(float(exp))
    if not vals:
        return None
    earliest = min(vals)
    return earliest if earliest > time.time() else None


def _global_session_expires_at():
    """글로벌 폴백: 확장에서 받은 인메모리 쿠키 → session_state.json 순으로 확인."""
    exp = _earliest_expires(_session_cookies, "expirationDate")
    if exp:
        return exp
    if os.path.exists(SESSION_STATE_PATH):
        try:
            with open(SESSION_STATE_PATH, encoding="utf-8") as f:
                state = json.load(f)
            return _earliest_expires(state.get("cookies", []), "expires")
        except Exception:
            return None
    return None


def _session_expires_at(client_id=None):
    """주어진 client_id의 세션 만료 unix epoch(seconds). 없으면 글로벌 폴백."""
    import time
    if client_id:
        with _sessions_lock:
            rec = _sessions_by_client.get(client_id)
        if rec:
            exp = rec.get("expires_at")
            if exp and exp > time.time():
                return float(exp)
    return _global_session_expires_at()


@app.route("/api/login/status")
def api_login_status():
    import time
    client_id = (request.args.get("client_id") or "").strip() or None
    cookies_path = os.path.join(PROFILE_DIR, "Default", "Cookies")
    logged_in = os.path.exists(cookies_path)
    expires_at = _session_expires_at(client_id)
    remaining = int(expires_at - time.time()) if expires_at else None
    return jsonify({
        "logged_in": logged_in,
        "expires_at": expires_at,
        "remaining_seconds": remaining,
        "client_id": client_id,
    })


@app.route("/api/login/cancel", methods=["POST"])
def api_login_cancel():
    """진행 중인 자동 로그인 취소 — chromium 강제 종료."""
    global _login_pw, _login_context, _login_page
    try:
        if _login_context is not None:
            try: _login_context.close()
            except Exception: pass
        if _login_pw is not None:
            try: _login_pw.stop()
            except Exception: pass
    finally:
        _login_pw = _login_context = _login_page = None
    # 잔여 chromium도 정리
    try:
        import subprocess
        subprocess.run(["pkill", "-f", "chromium_headless_shell"], check=False)
        subprocess.run(["pkill", "-f", "ms-playwright/chromium"], check=False)
    except Exception:
        pass
    socketio.emit("login_qr_done", {})
    socketio.emit("collect_status", {"step": "done", "success": False, "error": "사용자 취소"})
    # 캡차 대기 중인 콜백도 깨우기 (None → 취소로 처리)
    try: _captcha_q.put_nowait(None)
    except Exception: pass
    return jsonify({"status": "cancelled"})


@app.route("/api/captcha/answer", methods=["POST"])
def api_captcha_answer():
    """캡차 모달에서 사용자가 입력한 답안을 로그인 콜백으로 전달."""
    body = request.get_json(silent=True) or {}
    answer = (body.get("answer") or "").strip()
    if not answer:
        return jsonify({"error": "answer required"}), 400
    try:
        _captcha_q.put_nowait(answer)
    except queue.Full:
        return jsonify({"error": "queue full"}), 503
    return jsonify({"status": "ok"})


@app.route("/api/captcha/cancel", methods=["POST"])
def api_captcha_cancel():
    """캡차 모달 닫기 — 콜백을 None으로 깨워 흐름 종료."""
    try: _captcha_q.put_nowait(None)
    except Exception: pass
    return jsonify({"status": "ok"})


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
    body = request.get_json(silent=True) or {}
    naver_id = (body.get("naver_id") or "").strip()
    naver_pw = body.get("naver_pw") or ""
    client_id = (body.get("client_id") or "").strip() or None
    def run_collect():
        global _login_pw, _login_context, _login_page, _scraping
        try:
            if _login_page is None:
                socketio.emit("collect_status", {"step": "login_start"})
                ensure_chromium()
                import auto_login as login_mod
                login_mod.PROFILE_DIR = PROFILE_DIR
                # popup에서 QR + 남은시간 + 인증번호 추출 → 우리 UI로 전송 (chromium 창 안 띄움)
                import base64
                def _on_qr(data):
                    def _b64(b):
                        if not b: return None
                        return "data:image/png;base64," + base64.b64encode(b).decode("ascii")
                    try:
                        socketio.emit("login_qr", {
                            "image": _b64(data.get("qr_image")),
                            "guide_image": _b64(data.get("guide_image")),
                            "time_left": data.get("time_left"),
                            "code": data.get("code"),
                        })
                    except Exception as e:
                        print(f"[on_qr] emit 실패: {e}")

                # 항상 headless로 시도 — 세션 살아있으면 즉시 통과 (QR 안 뜸),
                # 만료면 popup 캡처가 _on_qr로 우리 UI에 전달되어 modal 자동 열림
                success, pw, context, page = login_mod.main(
                    keep_open=True, naver_id=naver_id, naver_pw=naver_pw,
                    headless=True, on_qr=_on_qr, on_captcha=_make_on_captcha(),
                )
                socketio.emit("login_qr_done", {})
                if not success:
                    socketio.emit("collect_status", {"step": "login_failed", "error": "로그인 실패"})
                    return
                _login_pw, _login_context, _login_page = pw, context, page
                if client_id and os.path.exists(SESSION_STATE_PATH):
                    try:
                        import time as _t
                        with open(SESSION_STATE_PATH, encoding="utf-8") as f:
                            _st = json.load(f)
                        with _sessions_lock:
                            _sessions_by_client[client_id] = {
                                "expires_at": _earliest_expires(_st.get("cookies", []), "expires"),
                                "cookies": _st.get("cookies", []),
                                "updated_at": _t.time(),
                            }
                    except Exception:
                        pass
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
            # 수집 성공/실패 무관, 스크래핑 중 갱신된 쿠키를 영속화 (다음 콜드 스타트 캡차 방지)
            try:
                if _login_context is not None:
                    import auto_login as _al
                    _al.save_session(_login_context)
            except Exception as _e:
                print(f"[collect] save_session 실패: {_e}")
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


def _needs_classify(r, reanalyze: bool) -> bool:
    if r.get("replied"):
        return False
    if reanalyze:
        return True
    return r.get("sentiment") is None


@app.route("/api/classify/count")
def api_classify_count():
    """기간별 미분류 리뷰 건수 반환.

    - 쿼리 파라미터 없음: 사전정의 기간(30/90/180/365/전체)별 건수.
    - date_from/date_to 지정: {"custom": N} 형태로 단일 건수.
    - reanalyze=1: 이미 분석된(미답변) 리뷰까지 포함해 건수 산정.
    """
    from datetime import datetime, timedelta
    reviews = load_reviews()
    reanalyze = request.args.get("reanalyze") in ("1", "true", "True")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")

    if date_from or date_to:
        df = date_from or "0000-00-00"
        dt = date_to or "9999-99-99"
        count = sum(1 for r in reviews
                    if _needs_classify(r, reanalyze)
                    and df <= r.get("date", "") <= dt)
        return jsonify({"custom": count})

    periods = {"30": 30, "90": 90, "180": 180, "365": 365, "0": 0}
    result = {}
    for key, days in periods.items():
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d") if days > 0 else None
        count = sum(1 for r in reviews
                    if _needs_classify(r, reanalyze)
                    and (cutoff is None or r.get("date", "") >= cutoff))
        result[key] = count
    return jsonify(result)


@app.route("/api/classify", methods=["POST"])
def api_classify():
    """미분류 리뷰 일괄 분류"""
    data = request.get_json() or {}
    date_from = data.get("date_from")
    date_to = data.get("date_to")
    reanalyze = bool(data.get("reanalyze", False))

    cmd = [sys.executable, "classifier.py"]
    if date_from or date_to:
        if date_from: cmd += ["--date-from", date_from]
        if date_to:   cmd += ["--date-to", date_to]
        _log(f"🤖 AI 분석 시작 ({date_from or '처음'} ~ {date_to or '오늘'}{', 재분석' if reanalyze else ''})")
    else:
        days = str(data.get("days", 365))
        cmd += ["--days", days]
        _log(f"🤖 AI 분석 시작 ({days}일 기준{', 재분석' if reanalyze else ''})")
    if reanalyze:
        cmd.append("--reanalyze")

    subprocess.Popen(cmd, cwd=_base_dir)
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
        return jsonify({"error": "AI 서비스가 설정되지 않았습니다. 서버 관리자에게 문의하세요."}), 400

    try:
        from openai import OpenAI
        from classifier import generate_reply, load_brand_tone
        import rag
        client = OpenAI(api_key=api_key)
        brand_tone = load_brand_tone()
        settings_data = load_settings()
        top_k = int(settings_data.get("rag_top_k", 3))
        examples = []
        if top_k > 0:
            try:
                examples = rag.retrieve_similar(reviews[idx], client, top_k=top_k)
            except Exception:
                examples = []
        gen = generate_reply(reviews[idx], brand_tone, client, settings=settings_data, examples=examples)
        reply = gen.get("text", "") if isinstance(gen, dict) else (gen or "")
        sensitive_remaining = gen.get("sensitive_remaining", []) if isinstance(gen, dict) else []
        if not reply:
            return jsonify({"error": "AI 답변 생성에 실패했습니다. API 키와 크레딧을 확인해주세요."}), 500
        auto_reply = settings_data.get("auto_reply", False)
        reviews[idx]["ai_reply"] = reply
        if sensitive_remaining:
            # 민감 표현이 남아있으면 auto_reply 설정과 무관하게 자동 등록 차단
            reviews[idx]["reply_status"] = "needs_review"
            reviews[idx]["sensitive_flags"] = sensitive_remaining
        else:
            reviews[idx]["reply_status"] = "approved" if auto_reply else "draft"
            reviews[idx].pop("sensitive_flags", None)
        save_reviews(reviews)
        invalidate_reviews_cache()
        return jsonify({
            "reply": reply,
            "status": reviews[idx]["reply_status"],
            "sensitive_remaining": sensitive_remaining,
        })
    except Exception:
        return jsonify({"error": "AI 답변 생성 중 오류가 발생했습니다."}), 500


@app.route("/api/reply/approve/<int:idx>", methods=["POST"])
def api_approve_reply(idx):
    """답변 승인. force=true 없이는 민감 표현 포함 시 거부."""
    reviews = load_reviews()
    if idx >= len(reviews):
        return jsonify({"error": "not found"}), 404

    data = request.get_json() or {}
    force = bool(data.get("force"))
    if "reply" in data:
        old_reply = reviews[idx].get("ai_reply", "")
        new_reply = data["reply"]
        if old_reply and old_reply != new_reply:
            history = reviews[idx].get("reply_history", [])
            history.insert(0, {"text": old_reply, "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M")})
            reviews[idx]["reply_history"] = history[:5]
        reviews[idx]["ai_reply"] = new_reply

    # 승인 직전 민감 표현 재검증
    from classifier import _contains_sensitive
    settings_data = load_settings()
    sensitive = settings_data.get("sensitive_expressions", [])
    found = _contains_sensitive(reviews[idx].get("ai_reply", ""), sensitive)
    if found and not force:
        reviews[idx]["sensitive_flags"] = found
        if reviews[idx].get("reply_status") != "needs_review":
            reviews[idx]["reply_status"] = "needs_review"
            save_reviews(reviews)
            invalidate_reviews_cache()
        return jsonify({
            "error": "민감 표현이 포함되어 있습니다. 확인 후 force=true로 재승인하세요.",
            "sensitive_remaining": found,
        }), 400

    reviews[idx]["reply_status"] = "approved"
    reviews[idx].pop("sensitive_flags", None)
    save_reviews(reviews)
    invalidate_reviews_cache()
    threading.Thread(target=_rag_upsert_async, args=(dict(reviews[idx]),), daemon=True).start()
    return jsonify({"status": "approved", "forced": bool(found and force)})


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


@app.route("/api/refund/toggle/<int:idx>", methods=["POST"])
def api_refund_toggle(idx):
    """환불 완료 토글"""
    reviews = load_reviews()
    if idx >= len(reviews):
        return jsonify({"error": "not found"}), 404
    current = reviews[idx].get("refund_status", "none")
    reviews[idx]["refund_status"] = "none" if current == "completed" else "completed"
    save_reviews(reviews)
    invalidate_reviews_cache()
    return jsonify({"status": reviews[idx]["refund_status"]})


@app.route("/api/approve/all/<int:idx>", methods=["POST"])
def api_approve_all(idx):
    """쿠폰 + 답변 동시 승인. 답변에 민감 표현 포함 시 force=true 없이는 거부."""
    reviews = load_reviews()
    if idx >= len(reviews):
        return jsonify({"error": "not found"}), 404
    data = request.get_json() or {}
    approve_reply  = data.get("approve_reply",  True)
    approve_coupon = data.get("approve_coupon", True)
    force = bool(data.get("force"))
    if approve_reply:
        if "reply" in data:
            reviews[idx]["ai_reply"] = data["reply"]
        from classifier import _contains_sensitive
        settings_data = load_settings()
        sensitive = settings_data.get("sensitive_expressions", [])
        found = _contains_sensitive(reviews[idx].get("ai_reply", ""), sensitive)
        if found and not force:
            reviews[idx]["sensitive_flags"] = found
            if reviews[idx].get("reply_status") != "needs_review":
                reviews[idx]["reply_status"] = "needs_review"
                save_reviews(reviews)
                invalidate_reviews_cache()
            return jsonify({
                "error": "민감 표현이 포함되어 있습니다. 확인 후 force=true로 재승인하세요.",
                "sensitive_remaining": found,
            }), 400
        reviews[idx]["reply_status"] = "approved"
        reviews[idx].pop("sensitive_flags", None)
    if approve_coupon:
        reviews[idx]["coupon_status"] = "approved"
    save_reviews(reviews)
    invalidate_reviews_cache()
    if approve_reply:
        threading.Thread(target=_rag_upsert_async, args=(dict(reviews[idx]),), daemon=True).start()
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
    if "rag_auto_index" in data:
        s["rag_auto_index"] = bool(data["rag_auto_index"])
    if "rag_top_k" in data:
        try:
            s["rag_top_k"] = max(0, min(10, int(data["rag_top_k"])))
        except (TypeError, ValueError):
            pass
    if "customer_type_hints" in data:
        s["customer_type_hints"] = data["customer_type_hints"]
    if "spelling_correction" in data:
        s["spelling_correction"] = bool(data["spelling_correction"])
    if "auto_retry_sensitive" in data:
        s["auto_retry_sensitive"] = bool(data["auto_retry_sensitive"])
    if "skip_reportable_reply" in data:
        s["skip_reportable_reply"] = bool(data["skip_reportable_reply"])
    if "test_mode" in data:
        s["test_mode"] = bool(data["test_mode"])
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
        approved = [r for r in reviews if r.get("reply_status") == "approved" and r.get("ai_reply") and not r.get("sensitive_flags")]
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
    current_approved = sum(1 for r in reviews if r.get("reply_status") == "approved" and r.get("ai_reply") and not r.get("sensitive_flags"))
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
        if r.get("reply_status") == "approved" and r.get("ai_reply") and not r.get("sensitive_flags")
    )
    result = {
        "training_count": training_count,
        "active_model": f"review_v{settings.get('finetune_version', 0)}",
        "job_id": settings.get("finetune_job_id", ""),
        "job_status": "none",
        "fine_tuned_model": "",
    }
    result["auto_threshold"] = settings.get("finetune_auto_threshold", 0)
    result["last_count"] = settings.get("finetune_last_count", 0)
    result["version"] = settings.get("finetune_version", 0)
    job_id = settings.get("finetune_job_id", "")
    if job_id:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", settings.get("openai_api_key", "")))
            job = client.fine_tuning.jobs.retrieve(job_id)
            result["job_status"] = job.status
            result["fine_tuned_model"] = job.fine_tuned_model or ""
            # 완료 시 자동 활성화 + 버전 증가
            if job.status == "succeeded" and job.fine_tuned_model:
                s = load_settings()
                if s.get("active_model") != job.fine_tuned_model:
                    s["active_model"] = job.fine_tuned_model
                    s["finetune_job_id"] = ""
                    s["finetune_version"] = s.get("finetune_version", 0) + 1
                    save_settings(s)
                    result["active_model"] = f"review_v{s['finetune_version']}"
                    result["job_id"] = ""
                    result["version"] = s["finetune_version"]
                    _log(f"파인튜닝 완료 — 자동 전환: {job.fine_tuned_model} (review_v{s['finetune_version']})")
        except Exception:
            result["job_status"] = "error"
    return jsonify(result)


@app.route("/api/admin/finetune/start", methods=["POST"])
def finetune_start():
    """파인튜닝 수동 시작"""
    global _finetune_running
    settings = load_settings()
    if settings.get("finetune_job_id"):
        return jsonify({"error": "이미 진행 중인 파인튜닝이 있습니다."}), 400
    reviews = load_reviews()
    approved_count = sum(1 for r in reviews if r.get("reply_status") == "approved" and r.get("ai_reply") and not r.get("sensitive_flags"))
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


# ── 답글 게시 동시성 관리 ──
_post_progress = {}            # idx -> {step, running, success, error, started_at}
_post_lock = threading.Lock()
_reviews_io_lock = threading.Lock()


def _set_post_progress(idx, **kw):
    with _post_lock:
        cur = _post_progress.get(idx, {})
        cur.update(kw)
        _post_progress[idx] = cur


@app.route("/api/reply/post/<int:idx>", methods=["POST"])
def api_post_reply(idx):
    """셀러센터에 답글 게시 (인메모리 threading + per-idx 진행 추적)"""
    reviews = load_reviews()
    if idx >= len(reviews):
        return jsonify({"error": "not found"}), 404
    r = reviews[idx]
    if not r.get("ai_reply"):
        return jsonify({"error": "게시할 답글이 없습니다."}), 400
    if r.get("replied"):
        return jsonify({"error": "이미 답글이 달린 리뷰입니다."}), 400
    if not (r.get("review_id") or "").strip():
        return jsonify({"error": "review_id 없음 — 리뷰를 재수집해주세요"}), 422

    # 세션 사전 가드 (cookies 만료/없음 → 412)
    try:
        import reply_api
        if not reply_api.is_available():
            return jsonify({"error": "세션 없음 — 다시 로그인해 주세요"}), 412
    except Exception:
        pass

    # 동일 idx 중복 실행 차단
    with _post_lock:
        cur = _post_progress.get(idx)
        if cur and cur.get("running"):
            return jsonify({"error": "이미 게시 진행 중입니다."}), 409

    body = request.get_json(silent=True) or {}
    dry_run = bool(body.get("dry_run"))
    if body.get("naver_id"):
        os.environ["NAVER_ID"] = body["naver_id"].strip()
    if body.get("naver_pw"):
        os.environ["NAVER_PW"] = body["naver_pw"]

    import time
    _set_post_progress(idx, step="시작", running=True, success=None,
                       error=None, started_at=time.time())

    def _run():
        try:
            import reply_poster
            res = reply_poster.post_reply(
                idx, dry_run=dry_run,
                progress_cb=lambda step: _set_post_progress(idx, step=step),
                io_lock=_reviews_io_lock,
            )
            _set_post_progress(idx,
                step="완료" if res["ok"] else "오류",
                running=False, success=res["ok"], error=res.get("error"))
            if res["ok"] and not dry_run:
                invalidate_reviews_cache()
        except Exception as e:
            _set_post_progress(idx, step="오류", running=False,
                               success=False, error=str(e))

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/reply/post-progress")
def api_post_progress():
    """idx 쿼리로 단건 조회. 없으면 전체 dict 반환(호환)."""
    idx_arg = request.args.get("idx", type=int)
    if idx_arg is not None:
        with _post_lock:
            cur = _post_progress.get(idx_arg)
        return jsonify(cur or {"running": False, "step": "", "success": None, "error": None})
    with _post_lock:
        return jsonify(dict(_post_progress))


# ── 일괄 게시 ──
_bulk_progress = {
    "running": False, "total": 0, "done": 0,
    "success": 0, "fail": 0, "current_idx": None,
    "failures": [],   # [{idx, reviewer, error}]
    "started_at": None,
}
_bulk_lock = threading.Lock()
_BULK_MAX = 50
_BULK_GAP_SEC = 0.8


def _set_bulk(**kw):
    with _bulk_lock:
        _bulk_progress.update(kw)


@app.route("/api/reply/post/bulk", methods=["POST"])
def api_post_bulk():
    """다수 idx를 순차 게시. rate-limit 보호 + per-idx 결과 누적."""
    with _bulk_lock:
        if _bulk_progress.get("running"):
            return jsonify({"error": "이미 일괄 게시가 진행 중입니다."}), 409

    data = request.get_json(silent=True) or {}
    indices_in = data.get("indices") or []
    dry_run = bool(data.get("dry_run"))
    skip_needs_review = bool(data.get("skip_needs_review", True))

    try:
        import reply_api
        if not reply_api.is_available():
            return jsonify({"error": "세션 없음 — 다시 로그인해 주세요"}), 412
    except Exception:
        pass

    reviews = load_reviews()
    # 유효 idx만 선별
    valid = []
    for i in indices_in:
        try:
            i = int(i)
        except (TypeError, ValueError):
            continue
        if not (0 <= i < len(reviews)):
            continue
        r = reviews[i]
        if r.get("replied"):
            continue
        if r.get("reply_status") != "approved":
            continue
        if not (r.get("ai_reply") or "").strip():
            continue
        if not (r.get("review_id") or "").strip():
            continue
        if skip_needs_review and r.get("sensitive_flags"):
            continue
        valid.append(i)
        if len(valid) >= _BULK_MAX:
            break

    if not valid:
        return jsonify({"error": "게시 가능한 항목이 없습니다."}), 400

    import time
    with _bulk_lock:
        _bulk_progress.update({
            "running": True, "total": len(valid), "done": 0,
            "success": 0, "fail": 0, "current_idx": None,
            "failures": [], "started_at": time.time(),
        })

    if data.get("naver_id"):
        os.environ["NAVER_ID"] = data["naver_id"].strip()
    if data.get("naver_pw"):
        os.environ["NAVER_PW"] = data["naver_pw"]

    def _runner(idx_list):
        import reply_poster, time as _t
        for i in idx_list:
            with _bulk_lock:
                _bulk_progress["current_idx"] = i
            # 게시별 진행 추적도 같이 갱신
            _set_post_progress(i, step="시작", running=True, success=None, error=None)
            try:
                res = reply_poster.post_reply(
                    i, dry_run=dry_run,
                    progress_cb=lambda step, _i=i: _set_post_progress(_i, step=step),
                    io_lock=_reviews_io_lock,
                )
                _set_post_progress(i,
                    step="완료" if res["ok"] else "오류",
                    running=False, success=res["ok"], error=res.get("error"))
                with _bulk_lock:
                    _bulk_progress["done"] += 1
                    if res["ok"]:
                        _bulk_progress["success"] += 1
                    else:
                        _bulk_progress["fail"] += 1
                        reviewer = ""
                        try:
                            reviewer = load_reviews()[i].get("reviewer", "")
                        except Exception:
                            pass
                        _bulk_progress["failures"].append({
                            "idx": i, "reviewer": reviewer, "error": res.get("error") or "실패",
                        })
            except Exception as e:
                _set_post_progress(i, step="오류", running=False, success=False, error=str(e))
                with _bulk_lock:
                    _bulk_progress["done"] += 1
                    _bulk_progress["fail"] += 1
                    _bulk_progress["failures"].append({"idx": i, "reviewer": "", "error": str(e)})
            _t.sleep(_BULK_GAP_SEC)
        with _bulk_lock:
            _bulk_progress["running"] = False
            _bulk_progress["current_idx"] = None
        if not dry_run:
            invalidate_reviews_cache()

    threading.Thread(target=_runner, args=(list(valid),), daemon=True).start()
    return jsonify({"status": "started", "total": len(valid)})


@app.route("/api/reply/post/bulk/progress")
def api_post_bulk_progress():
    with _bulk_lock:
        return jsonify(dict(_bulk_progress))


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
