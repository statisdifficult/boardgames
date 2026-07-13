#!/bin/bash
# start_online.sh — 온라인 공개 원클릭 (통합 서버 + 인터넷 터널)
#
# 사용법:  ./start_online.sh          (기본 8890 포트)
# 종료:    Ctrl+C 한 번 (서버·터널 같이 꺼짐)
#
# 뜨는 https://xxxx.lhr.life 주소를 상대에게 보내면 됨:
#   주소/            → 게임 허브
#   주소/batoo/      → 바투 (온라인 · AI)
#   주소/omok/ 등    → 온라인 2인 대전 (바둑/오목/체스/장기)
# 주소는 실행할 때마다 바뀜. 고정 주소가 필요하면 DEPLOY.md(Render) 참고.

PORT=${1:-8890}
cd "$(dirname "$0")"

python3 cloud_server.py "$PORT" &
SERVER_PID=$!
cleanup() { kill "$SERVER_PID" 2>/dev/null; }
trap cleanup EXIT INT TERM

sleep 1.5
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
  echo "❌ 서버가 켜지지 않았습니다. 포트 $PORT 가 이미 사용 중인지 확인하세요."
  exit 1
fi

echo ""
echo "🌐 인터넷 터널을 여는 중… 아래에 https://xxxx.lhr.life 주소가 나오면 그걸 공유하세요."
echo "   (이 창을 닫으면 서버와 터널이 함께 종료됩니다)"
echo ""
ssh -o StrictHostKeyChecking=accept-new \
    -o ServerAliveInterval=30 \
    -o ExitOnForwardFailure=yes \
    -R 80:localhost:"$PORT" nokey@localhost.run
