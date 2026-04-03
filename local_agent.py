"""
민베스트 로컬 에이전트
Railway 서버와 WebSocket으로 연결, 로그인/수집을 로컬에서 처리

환경변수 (.env):
  RAILWAY_URL=https://your-app.railway.app
  AGENT_TOKEN=your-secret-token
"""
import os, sys, json, subprocess, time, webbrowser, threading
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

RAILWAY_URL  = os.environ.get("RAILWAY_URL", "").rstrip("/")
AGENT_TOKEN  = os.environ.get("AGENT_TOKEN", "")
REVIEWS_FILE = "data/reviews.json"
PROFILE_DIR  = "data/browser_profile"

if not RAILWAY_URL:
    print("오류: .env에 RAILWAY_URL이 없습니다.")
    sys.exit(1)
if not AGENT_TOKEN:
    print("오류: .env에 AGENT_TOKEN이 없습니다.")
    sys.exit(1)

import socketio as sio_client

sio = sio_client.Client(reconnection=True, reconnection_attempts=0, reconnection_delay=3)


def upload_reviews():
    import urllib.request
    if not os.path.exists(REVIEWS_FILE):
        return False
    with open(REVIEWS_FILE, encoding="utf-8") as f:
        reviews = json.load(f)
    body = json.dumps(reviews, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{RAILWAY_URL}/api/upload/reviews",
        data=body,
        headers={"Content-Type": "application/json", "X-Upload-Token": AGENT_TOKEN},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            print(f"업로드 완료: {result['count']}건")
            return True
    except Exception as e:
        print(f"업로드 실패: {e}")
        return False


@sio.event
def connect():
    print(f"서버 연결됨: {RAILWAY_URL}")
    sio.emit("agent_auth", {"token": AGENT_TOKEN})


@sio.event
def connect_error(data):
    print(f"연결 오류: {data}")


@sio.event
def disconnect():
    print("서버 연결 해제됨. 재연결 중...")


@sio.on("agent_ready")
def on_agent_ready(data):
    print("에이전트 인증 완료. 대기 중...")


@sio.on("do_login")
def on_do_login(data):
    print("로그인 요청 받음. 브라우저를 엽니다...")
    sio.emit("agent_progress", {"step": "브라우저를 열고 있습니다..."})

    def run_login():
        try:
            subprocess.run([sys.executable, "login.py"], check=False)
            # Cookies 파일 존재 확인
            cookies_path = Path(PROFILE_DIR) / "Default" / "Cookies"
            if cookies_path.exists():
                sio.emit("login_done", {"success": True})
                print("로그인 완료")
            else:
                sio.emit("login_done", {"success": False, "error": "쿠키 파일을 찾을 수 없습니다."})
        except Exception as e:
            sio.emit("login_done", {"success": False, "error": str(e)})

    threading.Thread(target=run_login, daemon=True).start()


@sio.on("do_scrape")
def on_do_scrape(data):
    print("수집 요청 받음. 스크래핑을 시작합니다...")
    sio.emit("agent_progress", {"step": "리뷰 수집 중..."})

    def run_scrape():
        try:
            subprocess.run([sys.executable, "scraper.py"], check=False)
            sio.emit("agent_progress", {"step": "수집 완료. 업로드 중..."})
            success = upload_reviews()
            sio.emit("scrape_done", {"success": success})
            print("수집 및 업로드 완료")
        except Exception as e:
            sio.emit("scrape_done", {"success": False, "error": str(e)})

    threading.Thread(target=run_scrape, daemon=True).start()


def main():
    print(f"민베스트 로컬 에이전트 시작")
    print(f"서버: {RAILWAY_URL}")

    # 브라우저에서 Railway URL 열기
    threading.Timer(2.0, lambda: webbrowser.open(RAILWAY_URL)).start()

    try:
        sio.connect(RAILWAY_URL, transports=["websocket", "polling"])
        sio.wait()
    except KeyboardInterrupt:
        print("종료합니다.")
        sio.disconnect()


if __name__ == "__main__":
    main()
