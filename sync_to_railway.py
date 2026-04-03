"""
로컬에서 수집한 reviews.json을 Railway 서버로 업로드

사용법:
  python3 sync_to_railway.py

환경변수 (.env):
  RAILWAY_URL=https://your-app.railway.app
  UPLOAD_TOKEN=your-secret-token
"""
import json, os, sys
from dotenv import load_dotenv

load_dotenv()

RAILWAY_URL  = os.environ.get("RAILWAY_URL", "").rstrip("/")
UPLOAD_TOKEN = os.environ.get("UPLOAD_TOKEN", "")
REVIEWS_FILE = "data/reviews.json"

if not RAILWAY_URL:
    print("오류: .env에 RAILWAY_URL이 없습니다.")
    sys.exit(1)
if not UPLOAD_TOKEN:
    print("오류: .env에 UPLOAD_TOKEN이 없습니다.")
    sys.exit(1)
if not os.path.exists(REVIEWS_FILE):
    print(f"오류: {REVIEWS_FILE} 파일이 없습니다. 먼저 리뷰를 수집해주세요.")
    sys.exit(1)

import urllib.request

with open(REVIEWS_FILE, encoding="utf-8") as f:
    reviews = json.load(f)

body = json.dumps(reviews, ensure_ascii=False).encode("utf-8")
req = urllib.request.Request(
    f"{RAILWAY_URL}/api/upload/reviews",
    data=body,
    headers={"Content-Type": "application/json", "X-Upload-Token": UPLOAD_TOKEN},
    method="POST",
)
try:
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
        print(f"업로드 완료: {result['count']}건")
except urllib.error.HTTPError as e:
    print(f"실패: {e.code} {e.read().decode()}")
