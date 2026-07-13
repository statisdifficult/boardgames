#!/bin/bash
# 바투 서버를 인터넷에 공개하는 터널 — 설치 불필요 (맥 기본 ssh 사용)
#
# 사용법:
#   1) 먼저 게임 서버 실행:  python3 server.py        (포트 8890)
#   2) 새 터미널에서:        ./tunnel.sh
#   3) 출력에 나오는 https://….lhr.life 주소를 상대에게 보내면 끝!
#      (상대는 그 주소 뒤에 /batoo/ 를 붙여 접속)
#
# 주소는 실행할 때마다 바뀌고, 이 창을 닫으면 터널도 닫힙니다.

PORT=${1:-8890}

echo "🌐 인터넷 터널을 여는 중… (포트 $PORT)"
echo "   아래에 https://xxxx.lhr.life 주소가 나오면, 상대에게"
echo "   그 주소 + /batoo/  를 알려주세요. (예: https://xxxx.lhr.life/batoo/)"
echo ""

exec ssh -o StrictHostKeyChecking=accept-new \
         -o ServerAliveInterval=30 \
         -o ExitOnForwardFailure=yes \
         -R 80:localhost:"$PORT" nokey@localhost.run
