# 온라인 대전 서버 무료 배포 (Render)

맥을 꺼도 24시간 도는 온라인 대전 서버를 만드는 방법. 5~10분 걸리고 무료.

## 뭐가 배포되나

`cloud_server.py` 하나가 전부 담당:

| 경로 | 담당 |
|---|---|
| `/` | 게임 허브 (index.html) |
| `/batoo/` + `/api/*` | 바투 (온라인 대전 · AI — server.py) |
| `/baduk/` `/omok/` `/chess/` `/janggi/` + `/games/api/*` | 온라인 2인 대전 (games_server.py) |
| 나머지 게임 폴더 | 정적 파일로 그대로 플레이 가능 |

- 파이썬 표준 라이브러리만 사용 — 의존성 설치 없음
- torch는 안 올라가므로 바투 "신경망" 난이도는 자동으로 UCT(어려움)로 폴백

## 배포 순서

1. **[render.com](https://render.com) 가입** — "Sign in with GitHub" 누르면 끝
2. 대시보드에서 **New → Blueprint** 클릭
3. **이 레포(`boardgames`)를 연결** — GitHub 권한 요청이 뜨면 이 레포만 허용해도 됨
4. `render.yaml`을 자동으로 읽어서 설정이 채워짐 → **Apply/Deploy** 클릭
5. 2~3분 뒤 `https://boardgames-XXXX.onrender.com` 형태의 주소가 나옴

끝. 그 주소가 게임 허브고, `주소/batoo/` 가 바투.

## 상대와 대전하는 법

1. 내가 `https://…onrender.com/batoo/` 접속 → [온라인 · 방 만들기] → 방 코드 확인
2. 상대에게 같은 주소 + 방 코드 전달
3. 상대는 [온라인 · 참가]에 코드 입력

바둑/오목/체스/장기도 동일 (각 게임 페이지에서 온라인 모드 선택).

## 무료 티어 특성

- **15분간 접속이 없으면 잠들고, 다음 접속 때 깨어나는 데 30초쯤 걸림** — 첫 화면이 늦게 떠도 고장 아님. 대국 중에는 1초 폴링이 계속 가므로 잠들지 않음
- 서버가 재시작되면 진행 중이던 방은 사라짐 (방 코드 다시 만들면 됨)
- 월 750시간 무료 — 서비스 1개면 사실상 무제한

## 로컬 확인 (선택)

```bash
python3 cloud_server.py 8890
# → http://localhost:8890/        허브
# → http://localhost:8890/batoo/  바투 (온라인 + AI)
# → http://localhost:8890/baduk/  바둑 (온라인은 /games/api 사용)
```

## 코드를 고치면?

`git push` 하면 Render가 자동으로 다시 배포한다 (Auto-Deploy 기본 켜짐).
