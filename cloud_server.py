#!/usr/bin/env python3
# cloud_server.py — 클라우드 배포용 통합 서버 (Render/Fly 등)
#
# 문제: server.py(바투)와 games_server.py(바둑/오목/체스/장기)가 같은 /api/* 경로를 쓰는데
#       클라우드 무료 티어는 포트를 하나만 열어준다.
# 해법: 한 프로세스에서 경로로 라우팅 —
#       /api/*        → server.py (바투, 기존 경로 그대로)
#       /games/api/*  → games_server.py (/games 프리픽스를 벗겨서 전달)
#       그 외         → 정적 파일 (허브·각 게임 HTML)
#
# 실행:  python3 cloud_server.py [포트]     (PORT 환경변수가 있으면 그것을 우선)
# torch가 없으면 바투 '신경망' 난이도는 자동으로 UCT로 폴백된다 (server.py 내장 동작).

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)                       # 정적 파일 기준 디렉터리
sys.path.insert(0, ROOT)

import server as batoo               # noqa: E402  (바투 서버 — __main__ 가드 있음)
import games_server as games         # noqa: E402  (보드게임 서버 — __main__ 가드 있음)

from http.server import ThreadingHTTPServer  # noqa: E402

PREFIX = "/games"


from http.server import SimpleHTTPRequestHandler  # noqa: E402


class Handler(batoo.Handler, games.Handler):
    """경로 기반 디스패처. 헬퍼(_json/_body/_player)는 MRO상 batoo 쪽이 쓰인다
    (두 구현은 의미상 동일 — 같은 rooms 구조 {tokens:{1,2}}를 다룬다)."""

    def __init__(self, *args, **kwargs):
        # 두 부모가 각자 directory=ROOT를 주입해 중복되므로 곧장 조상으로 간다
        SimpleHTTPRequestHandler.__init__(self, *args, directory=ROOT, **kwargs)

    def _is_games(self):
        return self.path.startswith(PREFIX + "/api/")

    def do_POST(self):
        if self._is_games():
            self.path = self.path[len(PREFIX):]
            return games.Handler.do_POST(self)
        return batoo.Handler.do_POST(self)

    def do_GET(self):
        if self._is_games():
            self.path = self.path[len(PREFIX):]
            return games.Handler.do_GET(self)
        return batoo.Handler.do_GET(self)

    def do_OPTIONS(self):
        return games.Handler.do_OPTIONS(self)


if __name__ == "__main__":
    port = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8890))
    print(f"통합 서버 가동 — http://localhost:{port}/  (허브)")
    print(f"  바투 API: /api/*   ·   보드게임 API: {PREFIX}/api/*")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
