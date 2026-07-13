# 리눅스 상시 게임서버 인수인계

리눅스 머신에서 보드게임 서버를 **24시간 켜두고**, 재부팅·크래시에도 저절로 살아나게 만드는 문서.
(학습 돌리는 법은 `README_LINUX_TRAIN.md` — 이 문서는 서버 운영만. 같은 머신에서 학습과 서버를 같이 돌려도 됨: 서버는 대기 시 메모리 ~50MB, CPU ~0%라 학습에 지장 없음.)

## 0. 한눈에

```
python3 cloud_server.py 8890     ← 이거 하나가 전부 (허브+바투+온라인 게임 전체)
```
- 의존성 0 (파이썬 표준 라이브러리만). torch는 **선택** — 있으면 바투 "신경망" 난이도가 진짜 신경망(iter739)으로, 없으면 UCT로 자동 폴백.
- 상시 운영은 아래 systemd 설정 권장 (재부팅·크래시 자동 복구).
- 인터넷 공개는 두 갈래: **공인 IP가 있으면 포트만 열면 끝**, 없으면 ssh 터널.

## 1. 코드 받기 (비공개 레포 — 인증 필요)

셋 중 하나:

```bash
# (a) GitHub CLI가 있으면 제일 쉬움
gh auth login && gh repo clone statisdifficult/boardgames ~/boardgames

# (b) SSH 키 — 이 머신의 공개키를 github.com/settings/keys 에 등록 후
ssh-keygen -t ed25519   # 없을 때만
cat ~/.ssh/id_ed25519.pub   # → GitHub에 등록
git clone git@github.com:statisdifficult/boardgames.git ~/boardgames

# (c) 토큰 — github.com/settings/tokens 에서 fine-grained 토큰(이 레포 read) 발급 후
git clone https://<토큰>@github.com/statisdifficult/boardgames.git ~/boardgames
```

이미 학습용으로 레포가 있다면 그냥 `cd ~/boardgames && git pull`.

## 2. 일단 손으로 켜보기 (동작 확인)

```bash
cd ~/boardgames
python3 cloud_server.py 8890
# → "통합 서버 가동" 출력 확인
# 다른 터미널에서:
curl http://localhost:8890/games/api/health    # {"ok": true, ...} 나오면 정상
```

브라우저 되는 환경이면 `http://<이 머신 IP>:8890/` 열어 허브 확인 (같은 네트워크에서).

## 3. 상시 운영 — systemd 등록

`YOUR_USER`를 실제 계정명으로 바꿔서 그대로 붙여넣기:

```bash
sudo tee /etc/systemd/system/boardgames.service > /dev/null <<'EOF'
[Unit]
Description=Boardgames unified server (batoo + online games)
After=network-online.target
Wants=network-online.target

[Service]
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/boardgames
ExecStart=/usr/bin/python3 cloud_server.py 8890
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now boardgames    # 지금 켜고 + 부팅 시 자동 시작
```

운영 명령 모음:

```bash
systemctl status boardgames               # 상태
journalctl -u boardgames -f               # 로그 실시간
sudo systemctl restart boardgames         # 재시작 (코드 업데이트 후)
```

## 4. 인터넷 공개

### 4-A. 공인 IP / 포트 개방이 되는 머신 (연구실 서버 등) — 권장

방화벽에서 8890만 열면 끝. 주소 고정, 터널 불필요:

```bash
sudo ufw allow 8890/tcp        # ufw 쓰는 경우
# 또는 iptables/기관 방화벽 규칙에서 8890 오픈
```

접속 주소: `http://<공인IP>:8890/` (친구에게 이 주소 + 방 코드만 주면 됨)

⚠️ 기관 네트워크면 포트 개방이 정책상 막혀 있을 수 있음 — 그러면 4-B로.

### 4-B. 포트를 못 여는 경우 — ssh 터널 (localhost.run)

```bash
sudo tee /etc/systemd/system/boardgames-tunnel.service > /dev/null <<'EOF'
[Unit]
Description=Boardgames internet tunnel (localhost.run)
After=boardgames.service
Requires=boardgames.service

[Service]
User=YOUR_USER
ExecStart=/usr/bin/ssh -T -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -R 80:localhost:8890 nokey@localhost.run
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now boardgames-tunnel
```

현재 공개 주소 확인 (터널이 재연결될 때마다 바뀜):

```bash
journalctl -u boardgames-tunnel --no-pager | grep -oE 'https://[a-z0-9]+\.lhr\.life' | tail -1
```

⚠️ 터널 방식의 한계: 주소가 재연결마다 바뀜. 고정 주소가 필요하면 4-A(포트 개방)나 Render(`DEPLOY.md`)를 쓸 것.

## 5. (선택) 신경망 AI 켜기

torch를 깔면 바투 "신경망" 난이도가 학습된 모델(iter739)로 동작:

```bash
pip3 install torch numpy --index-url https://download.pytorch.org/whl/cpu   # CPU판 (서버엔 충분)
sudo systemctl restart boardgames
journalctl -u boardgames | grep 신경망    # "🧠 신경망 로드: ... iter 739" 나오면 성공
```

메모리 +300~500MB. 학습과 병행 시 GPU/CPU 경합은 무시할 수준 (추론은 수당 1회, 밀리초 단위).

## 6. 코드 업데이트 (맥이나 웹 세션에서 push한 뒤)

```bash
cd ~/boardgames && git pull && sudo systemctl restart boardgames
```

## 7. 문제 생기면

| 증상 | 확인 |
|---|---|
| 접속 안 됨 | `systemctl status boardgames` → active인지. `curl localhost:8890/games/api/health` |
| 포트 충돌 | `python3 cloud_server.py 8891`처럼 다른 포트로 (service 파일의 8890 두 군데 수정) |
| 터널 주소 모름 | 위 4-B의 journalctl grep 명령 |
| 신경망 안 뜸 | torch 설치 여부, `journalctl -u boardgames | grep -i "신경망\|error"` |
| 방이 사라짐 | 서버 재시작 시 방은 메모리라 소멸 — 정상 (방 코드 새로 만들면 됨) |

## 참고: 학습과 같이 쓰기

이 머신이 README_LINUX_TRAIN.md의 학습 머신이라면 — 학습(`train_batoo_full.py --transplant batoo_net.pt --workers N`)을 돌리면서 서버를 같이 켜둬도 됨. 새 풀게임 모델이 완성되면:
1. `arena_full.py`로 흑:백 균형 확인 (CLAUDE.md의 AI 현황 참고)
2. 정상이면 커밋 → 서버 머신에서 `git pull` + 재시작 → server.py의 풀게임 로더 재활성화는 CLAUDE.md 절차대로
