#!/usr/bin/env python3
# games_server.py — 보드게임 세트 온라인 서버 (바둑/오목/체스/장기 2인 대전)
#
# 파이썬 표준 라이브러리만 사용. 방 코드 기반 매칭, 수 중계 + 차례/참가자 검증.
# 규칙 검증은 양쪽 클라이언트가 동일한 JS 엔진으로 수행한다 (완전 공개 정보 게임이라
# 서버가 숨길 정보가 없음 — batoo/홀덤과 달리 규칙 포팅 불필요).
#
# 실행:  python3 games_server.py [포트]     (기본 8891)
# 정적 파일도 같이 서빙하므로 http://localhost:8891/baduk/ 등으로 바로 플레이 가능.
#
# 확장 지점 (로그인/전적 대비):
#  - 모든 종국은 /api/action {type:"result"} 로 서버에 보고되어 room["result"]에 저장됨
#    → 나중에 SQLite에 INSERT + 계정 시스템만 붙이면 전적/레이팅이 됨.
#  - 토큰이 플레이어 식별자 — 로그인 도입 시 토큰을 세션과 연결하면 됨.

import json
import os
import secrets
import string
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8891
ROOT = os.path.dirname(os.path.abspath(__file__))
GAMES = {"baduk", "omok", "chess", "janggi"}
ROOM_TTL = 60 * 60 * 6          # 6시간 지난 방 정리

lock = threading.Lock()
rooms = {}   # code -> room

# room 구조:
# { game, settings, tokens:{1:token,2:token|None}, seq, events:[...], created, touched,
#   game_no,            # 리매치 횟수 (0부터) — 홀짝으로 선공 스왑
#   move_count,         # 현재 판의 수 (차례 검증용)
#   rematch:{1:bool,2:bool}, over:bool, result:dict|None, results:[...] }
# event = { seq, t, by, data, ts } — t: join|move|resign|drawOffer|drawAccept|drawDecline|restart|result


def new_code():
    while True:
        c = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
        if c not in rooms:
            return c


def first_player(room):
    """이번 판 선공 플레이어 (방장 먼저, 리매치마다 스왑)"""
    return 1 if room["game_no"] % 2 == 0 else 2


def turn_player(room):
    """지금 수를 둘 차례인 플레이어"""
    return first_player(room) if room["move_count"] % 2 == 0 else (3 - first_player(room))


def push(room, t, by, data=None):
    room["seq"] += 1
    room["events"].append({"seq": room["seq"], "t": t, "by": by, "data": data, "ts": time.time()})
    room["touched"] = time.time()


def cleanup():
    now = time.time()
    for code in [c for c, r in rooms.items() if now - r["touched"] > ROOM_TTL]:
        del rooms[code]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def _player(self, room, token):
        for p in (1, 2):
            if token and room["tokens"].get(p) == token:
                return p
        return None

    # ---------------- POST ----------------
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self._body()
        except Exception:
            return self._json({"error": "bad json"}, 400)

        with lock:
            cleanup()

            if path == "/api/create":
                game = data.get("game")
                if game not in GAMES:
                    return self._json({"error": "지원하지 않는 게임입니다"}, 400)
                code = new_code()
                token = secrets.token_hex(8)
                rooms[code] = {
                    "game": game,
                    "settings": data.get("settings") or {},
                    "tokens": {1: token, 2: None},
                    "seq": 0, "events": [],
                    "created": time.time(), "touched": time.time(),
                    "game_no": 0, "move_count": 0,
                    "rematch": {1: False, 2: False},
                    "over": False, "result": None, "results": [],
                }
                return self._json({"room": code, "player": 1, "token": token, "seq": 0})

            if path == "/api/join":
                code = (data.get("room") or "").strip().upper()
                room = rooms.get(code)
                if not room:
                    return self._json({"error": "방을 찾을 수 없습니다"}, 404)
                if data.get("game") and data["game"] != room["game"]:
                    return self._json({"error": f"이 방은 다른 게임({room['game']}) 방입니다"}, 400)
                if room["tokens"][2]:
                    return self._json({"error": "방이 가득 찼습니다"}, 409)
                token = secrets.token_hex(8)
                room["tokens"][2] = token
                push(room, "join", 2)
                return self._json({"room": code, "player": 2, "token": token,
                                   "game": room["game"], "settings": room["settings"],
                                   "seq": room["seq"]})

            if path == "/api/action":
                room = rooms.get((data.get("room") or "").strip().upper())
                if not room:
                    return self._json({"error": "no room"}, 404)
                player = self._player(room, data.get("token"))
                if not player:
                    return self._json({"error": "bad token"}, 403)
                if not room["tokens"][2]:
                    return self._json({"error": "상대가 아직 입장하지 않았습니다"}, 409)
                a = data.get("action") or {}
                t = a.get("type")

                if t == "move":
                    if room["over"]:
                        return self._json({"error": "이미 끝난 판입니다", "seq": room["seq"]}, 409)
                    if turn_player(room) != player:
                        return self._json({"error": "내 차례가 아닙니다", "seq": room["seq"]}, 409)
                    n = a.get("n")
                    if n is not None and n != room["move_count"]:
                        return self._json({"error": "동기화 불일치", "seq": room["seq"]}, 409)
                    push(room, "move", player, {"n": room["move_count"], "move": a.get("data")})
                    room["move_count"] += 1
                    return self._json({"seq": room["seq"], "n": room["move_count"]})

                if t in ("resign", "drawOffer", "drawAccept", "drawDecline"):
                    if room["over"] and t != "drawDecline":
                        if t == "resign":
                            return self._json({"error": "이미 끝난 판입니다", "seq": room["seq"]}, 409)
                    if t == "resign" or t == "drawAccept":
                        room["over"] = True
                    push(room, t, player)
                    return self._json({"seq": room["seq"]})

                if t == "result":
                    # 클라이언트가 판정한 종국 결과 보고 (전적 저장용 — 먼저 온 보고를 기록)
                    room["over"] = True
                    if room["result"] is None:
                        room["result"] = {"by": player, "game_no": room["game_no"],
                                          "data": a.get("data"), "ts": time.time()}
                        room["results"].append(room["result"])
                        push(room, "result", player, a.get("data"))
                    return self._json({"seq": room["seq"]})

                if t == "rematch":
                    room["rematch"][player] = True
                    if all(room["rematch"].values()):
                        room["rematch"] = {1: False, 2: False}
                        room["game_no"] += 1
                        room["move_count"] = 0
                        room["over"] = False
                        room["result"] = None
                        push(room, "restart", player,
                             {"game_no": room["game_no"], "first": first_player(room)})
                    else:
                        push(room, "rematch", player)
                    return self._json({"seq": room["seq"]})

                return self._json({"error": "unknown action"}, 400)

        return self._json({"error": "not found"}, 404)

    # ---------------- GET ----------------
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/poll":
            q = parse_qs(parsed.query)
            with lock:
                room = rooms.get((q.get("room", [""])[0]).strip().upper())
                if not room:
                    return self._json({"error": "no room"}, 404)
                player = self._player(room, q.get("token", [""])[0])
                if not player:
                    return self._json({"error": "bad token"}, 403)
                since = int(q.get("since", ["0"])[0])
                room["touched"] = time.time()
                return self._json({
                    "seq": room["seq"],
                    "events": [e for e in room["events"] if e["seq"] > since],
                    "joined": bool(room["tokens"][2]),
                    "game_no": room["game_no"],
                    "first": first_player(room),
                    "turn": turn_player(room),
                    "n": room["move_count"],
                    "over": room["over"],
                })
        if parsed.path == "/api/health":
            return self._json({"ok": True, "rooms": len(rooms)})
        return super().do_GET()


if __name__ == "__main__":
    print(f"보드게임 온라인 서버 — http://localhost:{PORT}  (정적 파일 + 방 API)")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
