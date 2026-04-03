#!/bin/bash
# 민베스트 로컬 에이전트 실행파일 빌드
# 사용법: bash build_agent.sh
#
# 결과물:
#   dist/MinBestAgent        (Mac)
#   dist/MinBestAgent.exe    (Windows, Windows에서 실행 시)
#
# 클라이언트 전달물:
#   1. dist/MinBestAgent (또는 .exe)
#   2. .env 파일 (RAILWAY_URL, AGENT_TOKEN)

set -e

echo "필요 패키지 설치 중..."
pip install pyinstaller "python-socketio[client]" python-dotenv playwright --quiet

echo "실행파일 빌드 중..."
pyinstaller \
  --onefile \
  --name "MinBestAgent" \
  --add-data ".env:." \
  --hidden-import=engineio.async_drivers.threading \
  local_agent.py

echo ""
echo "빌드 완료!"
echo "  Mac/Linux: dist/MinBestAgent"
echo "  Windows:   dist/MinBestAgent.exe"
echo ""
echo "클라이언트에게 전달할 파일:"
echo "  1. dist/MinBestAgent (실행파일)"
echo "  2. .env (RAILWAY_URL, AGENT_TOKEN 포함)"
