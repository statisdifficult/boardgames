#!/usr/bin/env python3
"""바투 온라인 대전 서버 (서버 권위 방식) — 파이썬 표준 라이브러리만 사용.

서버가 게임 규칙 전체를 직접 실행하는 '심판'이다:
  - 클라이언트는 액션({type, idx, ...})만 보낸다. 상태를 직접 만들 수 없다.
  - 서버가 착수 검증·따내기·히든·스캔·계가·시계를 전부 계산한다.
  - 각 플레이어에게는 그 사람이 봐도 되는 것만 내려준다:
      상대의 미공개 히든 → 보드에서 제거된 채 전송 (개수만 공개)
      상대의 베이스빌드 → 공개 전엔 아예 전송 안 됨
      상대의 베팅액 → 양쪽 다 제출한 뒤에만 공개 (동시 비밀 베팅)
  → 개발자 도구를 열어도 비밀 정보가 없다.

실행:  python3 server.py [포트]     (기본 8890)
접속:  http://localhost:8890/batoo/
"""
import json
import math
import os
import random
import secrets
import string
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8890
ROOT = os.path.dirname(os.path.abspath(__file__))

# ---- 게임 상수 (클라이언트 batoo/index.html 과 동일해야 함) ----
N = 11
EMPTY, BLACK, WHITE = 0, 1, 2
SCAN_COST = 2
BASE_VALUE = 5
PLUS_VALUES = [5, 3, 2]
MINUS_VALUES = [-5, -3, -2]
FIELDS = {
    "기본": [
        {"plus": [24, 96, 60], "minus": [30, 90, 63]},
        {"plus": [30, 90, 60], "minus": [24, 96, 57]},
        {"plus": [27, 93, 60], "minus": [57, 63, 96]},
    ],
    "스카이": [
        {"plus": [60, 27, 93], "minus": [24, 30, 96]},
        {"plus": [60, 57, 63], "minus": [24, 90, 96]},
    ],
    "카오스": [
        {"plus": [36, 84, 60], "minus": [26, 94, 38]},
        {"plus": [46, 74, 16], "minus": [104, 48, 72]},
        {"plus": [25, 95, 59], "minus": [61, 29, 91]},
    ],
}

rooms = {}   # code -> {tokens:{1,2}, game:{...}}
lock = threading.Lock()


def opp(c):
    return WHITE if c == BLACK else BLACK


def neighbors(idx):
    x, y = idx % N, idx // N
    r = []
    if x > 0: r.append(idx - 1)
    if x < N - 1: r.append(idx + 1)
    if y > 0: r.append(idx - N)
    if y < N - 1: r.append(idx + N)
    return r


def group_of(board, idx):
    color = board[idx]
    stones = [idx]
    seen = {idx}
    libs = set()
    i = 0
    while i < len(stones):
        for nb in neighbors(stones[i]):
            if board[nb] == EMPTY:
                libs.add(nb)
            elif board[nb] == color and nb not in seen:
                seen.add(nb)
                stones.append(nb)
        i += 1
    return stones, len(libs)


def gen_point_spots(field):
    layout = random.choice(FIELDS[field])
    pv = PLUS_VALUES[:]
    mv = MINUS_VALUES[:]
    random.shuffle(pv)
    random.shuffle(mv)
    spots = [{"idx": i, "val": v} for i, v in zip(layout["plus"], pv)]
    spots += [{"idx": i, "val": v} for i, v in zip(layout["minus"], mv)]
    return spots


def new_game(settings, field):
    return {
        "settings": settings, "field": field,
        "phase": "base",                       # base → bet → play
        "point_spots": [],
        "base_stones": {1: [], 2: []},         # 플레이어 키
        "base_confirmed": {1: False, 2: False},
        "bets": {1: None, 2: None},
        "rebet_done": False,   # 베팅 동점 시 원작 규칙: 1회 재베팅 → 그래도 동점이면 무작위
        "black_player": 0, "compensation": 0,
        "board": [EMPTY] * (N * N),
        "board_history": set(),
        "turn": BLACK, "capB": 0, "capW": 0, "passes": 0,
        "hidden": {BLACK: [], WHITE: []},      # 색 키
        "revealed": [],
        "hidden_used": {BLACK: 0, WHITE: 0},
        "scan": {BLACK: 0, WHITE: 0},
        "bases": {BLACK: [], WHITE: []},
        "last_move": None,
        "scoring": False, "dead": None,
        "over": False, "result": None,
        "clocks": None, "clock_ts": time.time(),
        "notices": {1: "", 2: ""},
        "reveal_notes": [],
        "seq": 0,
    }


def color_of(g, player):
    return BLACK if g["black_player"] == player else WHITE


def player_of_color(g, color):
    return g["black_player"] if color == BLACK else (2 if g["black_player"] == 1 else 1)


def color_name(g, color):
    return f"{'흑' if color == BLACK else '백'}(P{player_of_color(g, color)})"


def notify(g, p1_msg, p2_msg=None):
    g["notices"][1] = p1_msg
    g["notices"][2] = p2_msg if p2_msg is not None else p1_msg


def notify_colors(g, mover_color, mover_msg, other_msg):
    mp = player_of_color(g, mover_color)
    g["notices"][mp] = mover_msg
    g["notices"][2 if mp == 1 else 1] = other_msg


# ---- 시계 ----
def timed(g):
    s = g["settings"]
    return s["mainSec"] > 0 or s["byoSec"] > 0


def start_clocks(g):
    if not timed(g):
        g["clocks"] = None
        return
    s = g["settings"]
    def mk():
        return {"main": s["mainSec"],
                "byoLeft": s["byoPeriods"] if s["byoSec"] > 0 else 0,
                "byoCur": s["byoSec"]}
    g["clocks"] = {BLACK: mk(), WHITE: mk()}
    g["clock_ts"] = time.time()


def update_clocks(g):
    now = time.time()
    dt = now - g["clock_ts"]
    g["clock_ts"] = now
    if not g["clocks"] or g["phase"] != "play" or g["scoring"] or g["over"]:
        return
    s = g["settings"]
    c = g["clocks"][g["turn"]]
    if c["main"] > 0:
        c["main"] -= dt
        if c["main"] <= 0:
            c["main"] = 0
            if s["byoSec"] <= 0:
                time_loss(g, g["turn"])
    elif s["byoSec"] > 0:
        c["byoCur"] -= dt
        while c["byoCur"] <= 0:
            c["byoLeft"] -= 1
            if c["byoLeft"] <= 0:
                c["byoCur"] = 0
                time_loss(g, g["turn"])
                return
            c["byoCur"] += s["byoSec"]


def reset_byo(g, color):
    if g["clocks"] and g["settings"]["byoSec"] > 0:
        c = g["clocks"][color]
        if c["main"] <= 0 and c["byoLeft"] > 0:
            c["byoCur"] = g["settings"]["byoSec"]


def time_loss(g, loser):
    winner = opp(loser)
    g["over"] = True
    g["scoring"] = False
    g["result"] = (f'<b style="color:var(--accent)">⏱️ {color_name(g, loser)} 시간패 — '
                   f'{"⚫" if winner == BLACK else "⚪"} {color_name(g, winner)} 승!</b>')
    notify(g, "⏱️ 시간패로 대국이 끝났습니다.")
    g["seq"] += 1


# ---- 규칙 엔진 ----
def compute_move(g, idx):
    """성공: (board, captured) / 불법: None"""
    if g["board"][idx] != EMPTY:
        return None
    board = g["board"][:]
    me, op = g["turn"], opp(g["turn"])
    board[idx] = me
    captured = []
    for nb in neighbors(idx):
        if board[nb] == op:
            stones, libs = group_of(board, nb)
            if libs == 0:
                for s in stones:
                    board[s] = EMPTY
                captured += stones
    _, own_libs = group_of(board, idx)
    if own_libs == 0:
        return None                    # 자살수
    if tuple(board) in g["board_history"]:
        return None                    # 동형반복(패)
    return board, captured


def legal_on_view(g, idx):
    """상대 히든을 뺀 '보이는 보드'에서 합법인지"""
    op = opp(g["turn"])
    view = g["board"][:]
    for h in g["hidden"][op]:
        view[h] = EMPTY
    if view[idx] != EMPTY:
        return False
    view[idx] = g["turn"]
    for nb in neighbors(idx):
        if view[nb] == op:
            _, libs = group_of(view, nb)
            if libs == 0:
                return True
    _, libs = group_of(view, idx)
    return libs > 0


def attempt_move(g, idx, as_hidden):
    me, op = g["turn"], opp(g["turn"])

    # 상대 히든을 찍음 → 발각! 그 착수는 무효, 차례는 유지되어 다시 아무 곳에나 둘 수 있다 (원작 규칙)
    if idx in g["hidden"][op]:
        g["hidden"][op].remove(idx)
        g["revealed"].append(idx)
        # turn/passes 그대로 — 같은 사람이 다시 착수
        notify_colors(g, me,
                      "💥 히든 발각! 그 자리엔 상대 히든이 있었습니다. 다른 곳에 다시 두세요.",
                      "💥 내 히든이 발각되었습니다! (상대는 차례를 잃지 않습니다)")
        return None

    if as_hidden and g["hidden_used"][me] >= g["settings"]["hiddenMax"]:
        return "히든을 모두 사용했습니다."

    r = compute_move(g, idx)
    if r is None:
        if g["board"][idx] == EMPTY and g["hidden"][op] and legal_on_view(g, idx):
            g["revealed"] += g["hidden"][op]
            g["hidden"][op] = []
            notify_colors(g, me,
                          "💥 상대 히든 때문에 둘 수 없는 자리였습니다 — 히든 공개! 다시 두세요.",
                          "💥 내 히든이 공개되었습니다!")
            return None
        return "둘 수 없는 자리입니다."

    board, captured = r
    notes_me, notes_op = [], []

    if captured:
        cap_set = set(captured)
        lost_bases = [i for i in g["bases"][op] if i in cap_set]
        if lost_bases:
            g["bases"][op] = [i for i in g["bases"][op] if i not in cap_set]
            notes_me.append(f"🔥 상대 베이스 돌 {len(lost_bases)}개 격파! (−{len(lost_bases) * BASE_VALUE}점)")
            notes_op.append(f"🔥 내 베이스 돌 {len(lost_bases)}개가 잡혔습니다! (−{len(lost_bases) * BASE_VALUE}점)")
        cap_hidden = [i for i in g["hidden"][op] if i in cap_set]
        if cap_hidden:
            g["hidden"][op] = [i for i in g["hidden"][op] if i not in cap_set]
            notes_me.append(f"💥 상대 히든 {len(cap_hidden)}개가 잡혀 제거되었습니다!")
            notes_op.append("💥 내 히든이 잡혀 제거되었습니다!")
        g["revealed"] = [i for i in g["revealed"] if i not in cap_set]
        adj = set()
        for c in captured:
            adj.update(neighbors(c))
        helpers = [i for i in g["hidden"][me] if i in adj]
        if helpers:
            g["hidden"][me] = [i for i in g["hidden"][me] if i not in adj]
            g["revealed"] += helpers
            notes_me.append("⚡ 포획에 가담한 내 히든이 공개되었습니다.")
            notes_op.append("⚡ 포획에 가담한 상대 히든이 공개되었습니다.")

    if as_hidden:
        g["hidden_used"][me] += 1
        if captured:
            g["revealed"].append(idx)
            notes_me.append("⚡ 히든이 돌을 따내며 즉시 공개되었습니다!")
            notes_op.append("⚡ 상대 히든이 돌을 따내며 즉시 공개되었습니다!")
        else:
            g["hidden"][me].append(idx)
            notes_me.append("🕶️ 히든을 사용했습니다.")
            notes_op.append("🕶️ 상대가 히든을 사용했습니다! (위치 비공개)")
        g["last_move"] = None
    else:
        g["last_move"] = idx

    g["board"] = board
    g["board_history"].add(tuple(board))
    if me == BLACK:
        g["capB"] += len(captured)
    else:
        g["capW"] += len(captured)
    g["turn"] = op
    g["passes"] = 0
    notify_colors(g, me, " ".join(notes_me), " ".join(notes_op))
    return None


def do_scan(g, center):
    me, op = g["turn"], opp(g["turn"])
    if not (0 <= center < N * N):
        return "범위 밖입니다."
    # 원작 규칙: 스캔은 대국당 1회 (히든과 동일하게 횟수 제한)
    if g["scan"][me] // SCAN_COST >= g["settings"].get("scanMax", 1):
        return "스캔을 모두 사용했습니다."
    r0, c0 = center // N, center % N
    area = set()
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            r, c = r0 + dr, c0 + dc
            if 0 <= r < N and 0 <= c < N:
                area.add(r * N + c)
    found = [i for i in g["hidden"][op] if i in area]
    g["scan"][me] += SCAN_COST
    g["hidden"][op] = [i for i in g["hidden"][op] if i not in area]
    g["revealed"] += found
    if found:
        notify_colors(g, me,
                      f"📡 스캔 성공! 히든을 찾았습니다. (상대에게 +{SCAN_COST}점)",
                      f"📡 상대의 스캔에 내 히든이 발각되었습니다! (+{SCAN_COST}점 획득)")
    else:
        notify_colors(g, me,
                      f"📡 스캔 실패 — 범위 안에 히든이 없습니다. (상대에게 +{SCAN_COST}점)",
                      f"📡 상대가 스캔을 사용했지만 빗나갔습니다. (+{SCAN_COST}점 획득)")
    return None


def resolve_reveal(g):
    notes = []
    s1 = set(g["base_stones"][1])
    s2 = set(g["base_stones"][2])
    overlap = s1 & s2
    if overlap:
        s1 -= overlap
        s2 -= overlap
        notes.append(f"⚡ 같은 자리에 놓인 돌 {len(overlap)}개는 서로 상쇄되어 사라졌습니다.")
    board = [EMPTY] * (N * N)
    for i in s1:
        board[i] = 1     # 임시 색: P1=1, P2=2 (베팅 후 확정)
    for i in s2:
        board[i] = 2
    # 활로 없는 그룹 동시 제거
    to_remove = set()
    checked = set()
    for i in range(N * N):
        if board[i] != EMPTY and i not in checked:
            stones, libs = group_of(board, i)
            checked.update(stones)
            if libs == 0:
                to_remove.update(stones)
    if to_remove:
        for i in to_remove:
            board[i] = EMPTY
        notes.append(f"⚡ 활로가 없는 돌 {len(to_remove)}개가 제거되었습니다.")
    g["board"] = board
    g["point_spots"] = gen_point_spots(g["field"])
    g["reveal_notes"] = notes
    g["phase"] = "bet"
    extra = (" ".join(notes) + " " if notes else "")
    notify(g, f"⚡ {extra}베이스 & 포인트 공개! 흑번 베팅을 진행하세요. (동시 비밀 베팅)")


def resolve_bets(g):
    # 원작 규칙: 베팅 동점이면 1회 재베팅. 재베팅도 동점이면 그 값으로 확정 + 무작위 선공.
    if g["bets"][1] == g["bets"][2] and not g["rebet_done"]:
        g["rebet_done"] = True
        tie_val = g["bets"][1]
        g["bets"] = {1: None, 2: None}
        notify(g, f"⚖️ 베팅 동점({tie_val}집)! 재베팅을 진행합니다.")
        return

    tie = g["bets"][1] == g["bets"][2]
    if g["bets"][1] > g["bets"][2]:
        g["black_player"] = 1
    elif g["bets"][2] > g["bets"][1]:
        g["black_player"] = 2
    else:
        g["black_player"] = random.choice([1, 2])
    g["compensation"] = g["bets"][g["black_player"]]

    if g["black_player"] == 2:      # 색 확정: 승자 돌 → 흑
        g["board"] = [BLACK if v == 2 else WHITE if v == 1 else EMPTY for v in g["board"]]
    # (black_player==1이면 이미 P1=1=BLACK)

    for i, v in enumerate(g["board"]):
        if v != EMPTY:
            g["bases"][v].append(i)
    g["board_history"] = {tuple(g["board"])}
    g["turn"] = BLACK
    g["phase"] = "play"
    start_clocks(g)
    tie_note = " (재베팅도 동점 → 그 값으로 확정, 무작위)" if tie else ""
    notify(g, f"🎬 대국 시작! 베팅 P1 {g['bets'][1]} : P2 {g['bets'][2]}{tie_note} — "
              f"⚫ 흑 = P{g['black_player']} · 턴베팅 +{g['compensation']}")


def compute_score(g, board):
    minus_set = {p["idx"] for p in g["point_spots"] if p["val"] < 0}
    terr = {BLACK: 0, WHITE: 0}
    stones = {BLACK: 0, WHITE: 0}
    seen = [False] * (N * N)
    for i in range(N * N):
        if board[i] != EMPTY:
            stones[board[i]] += 1
        elif not seen[i]:
            region = [i]
            seen[i] = True
            touch = set()
            j = 0
            while j < len(region):
                for nb in neighbors(region[j]):
                    if board[nb] != EMPTY:
                        touch.add(board[nb])
                    elif not seen[nb]:
                        seen[nb] = True
                        region.append(nb)
                j += 1
            if len(touch) == 1:
                owner = touch.pop()
                terr[owner] += len(region) - sum(1 for c in region if c in minus_set)
    points = {BLACK: 0, WHITE: 0}
    for p in g["point_spots"]:
        if board[p["idx"]] != EMPTY:
            points[board[p["idx"]]] += p["val"]
    base_n = {c: sum(1 for i in g["bases"][c] if board[i] == c) for c in (BLACK, WHITE)}
    score = {
        BLACK: stones[BLACK] + terr[BLACK] + base_n[BLACK] * BASE_VALUE + points[BLACK] + g["scan"][WHITE],
        WHITE: stones[WHITE] + terr[WHITE] + base_n[WHITE] * BASE_VALUE + points[WHITE] + g["scan"][BLACK] + g["compensation"],
    }
    return stones, terr, base_n, points, score


def finalize_score(g):
    board = g["board"][:]
    for i in range(N * N):
        if g["dead"][i]:
            board[i] = EMPTY
    stones, terr, base_n, points, score = compute_score(g, board)
    diff = abs(score[BLACK] - score[WHITE])
    if score[BLACK] == score[WHITE]:
        # 동점은 후공(백) 승 — 흑은 선수를 샀으므로 베팅을 '초과'해야 이긴다
        winner_text = f"동점 — ⚪ 백 (플레이어 {player_of_color(g, WHITE)}) 승! (동점은 후공 승)"
    else:
        wc = BLACK if score[BLACK] > score[WHITE] else WHITE
        winner_text = f"{'⚫ 흑' if wc == BLACK else '⚪ 백'} (플레이어 {player_of_color(g, wc)}) {diff}집 승!"

    def pt(v):
        return f" {'+' if v > 0 else '−'} 포인트 {abs(v)}" if v else ""

    def sg(v):
        return f" + 상대스캔 {v}" if v else ""

    g["result"] = (
        f"⚫ 흑 (P{player_of_color(g, BLACK)}): 돌 {stones[BLACK]} + 집 {terr[BLACK]} + 베이스 {base_n[BLACK]}×{BASE_VALUE}"
        f"{pt(points[BLACK])}{sg(g['scan'][WHITE])} = <b>{score[BLACK]}</b><br>"
        f"⚪ 백 (P{player_of_color(g, WHITE)}): 돌 {stones[WHITE]} + 집 {terr[WHITE]} + 베이스 {base_n[WHITE]}×{BASE_VALUE}"
        f"{pt(points[WHITE])}{sg(g['scan'][BLACK])} + 턴베팅 {g['compensation']} = <b>{score[WHITE]}</b><br>"
        f'<b style="color:var(--accent)">{winner_text}</b>'
    )
    g["over"] = True
    notify(g, "🧮 계가가 끝났습니다.")


# ---- 액션 처리 ----
def apply_action(g, player, a):
    """성공: None (seq는 호출부에서 증가) / 실패: 에러 메시지"""
    t = a.get("type")

    if g["over"]:
        return "대국이 이미 끝났습니다."

    if t == "baseConfirm":
        if g["phase"] != "base":
            return "지금은 베이스빌드 단계가 아닙니다."
        if g["base_confirmed"][player]:
            return "이미 확정했습니다."
        stones = a.get("stones") or []
        cnt = g["settings"]["baseCount"]
        if (len(stones) != cnt or len(set(stones)) != cnt or
                any(not isinstance(i, int) or not (0 <= i < N * N) for i in stones)):
            return f"베이스 돌 {cnt}개를 정확히 놓아야 합니다."
        g["base_stones"][player] = list(stones)
        g["base_confirmed"][player] = True
        if all(g["base_confirmed"].values()):
            resolve_reveal(g)
        else:
            g["notices"][player] = "내 베이스빌드 완료 — 상대를 기다립니다…"
            g["notices"][2 if player == 1 else 1] = "⏳ 상대가 베이스빌드를 확정했습니다!"
        return None

    if t == "bet":
        if g["phase"] != "bet":
            return "지금은 베팅 단계가 아닙니다."
        if g["bets"][player] is not None:
            return "이미 베팅했습니다."
        v = a.get("value")
        if not isinstance(v, (int, float)) or v != int(v) or not (0 <= v <= 50):
            return "베팅은 0~50 사이의 정수여야 합니다."
        g["bets"][player] = int(v)
        if all(b is not None for b in g["bets"].values()):
            resolve_bets(g)
        else:
            g["notices"][player] = "내 베팅 완료 — 상대의 베팅을 기다립니다…"
            g["notices"][2 if player == 1 else 1] = "💰 상대가 베팅을 마쳤습니다!"
        return None

    if g["phase"] != "play":
        return "아직 대국이 시작되지 않았습니다."

    my = color_of(g, player)

    if t == "resign":
        winner = opp(my)
        g["over"] = True
        g["scoring"] = False
        g["result"] = (f'<b style="color:var(--accent)">{color_name(g, my)} 기권 — '
                       f'{"⚫" if winner == BLACK else "⚪"} {color_name(g, winner)} 승!</b>')
        notify(g, f"🏳️ {color_name(g, my)}가 기권했습니다.")
        return None

    if t == "requestScoring":
        if g["scoring"]:
            return "이미 계가 중입니다."
        g["scoring"] = True
        g["dead"] = [False] * (N * N)
        notify(g, "🧮 계가 — 모든 히든이 공개되었습니다. 죽은 돌을 클릭하세요.")
        return None

    if t == "resumeGame":
        if not g["scoring"]:
            return "계가 중이 아닙니다."
        g["scoring"] = False
        g["dead"] = None
        g["passes"] = 0
        g["clock_ts"] = time.time()
        notify(g, "▶️ 대국을 재개합니다.")
        return None

    if t == "toggleDead":
        if not g["scoring"]:
            return "계가 중이 아닙니다."
        idx = a.get("idx", -1)
        if not (0 <= idx < N * N) or g["board"][idx] == EMPTY:
            return "돌이 있는 자리를 클릭하세요."
        stones, _ = group_of(g["board"], idx)
        flag = not g["dead"][idx]
        for s in stones:
            g["dead"][s] = flag
        g["notices"][player] = ""
        return None

    if t == "finalizeScore":
        if not g["scoring"]:
            return "계가 중이 아닙니다."
        finalize_score(g)
        return None

    if g["scoring"]:
        return "계가 중에는 둘 수 없습니다."
    if g["turn"] != my:
        return "상대 차례입니다."

    if t in ("move", "hidden"):
        idx = a.get("idx", -1)
        if not (0 <= idx < N * N):
            return "범위 밖입니다."
        return attempt_move(g, idx, t == "hidden")

    if t == "scan":
        return do_scan(g, a.get("idx", -1))

    if t == "pass":
        g["passes"] += 1
        g["turn"] = opp(g["turn"])
        g["last_move"] = None
        if g["passes"] >= 2:
            g["scoring"] = True
            g["dead"] = [False] * (N * N)
            notify(g, "🧮 양쪽 연속 쉼 — 계가 모드. 죽은 돌을 클릭하세요.")
        else:
            notify_colors(g, my, "한 수 쉬었습니다.", "⏭️ 상대가 한 수 쉬었습니다.")
        return None

    return "알 수 없는 액션입니다."


# ---- 플레이어별 뷰 ----
def view_for(g, player):
    other = 2 if player == 1 else 1
    v = {
        "phase": g["phase"],
        "settings": g["settings"],
        "currentField": g["field"],
        "pointSpots": g["point_spots"],
        "blackPlayer": g["black_player"],
        "compensation": g["compensation"],
        "myPlayer": player,
        "myBaseConfirmed": g["base_confirmed"][player],
        "oppBaseConfirmed": g["base_confirmed"][other],
        "myBetSubmitted": g["bets"][player] is not None,
        "oppBetSubmitted": g["bets"][other] is not None,
        "scoringMode": g["scoring"],
        "gameOver": g["over"],
        "result": g["result"],
        "notice": g["notices"][player],
        "clocks": g["clocks"],
        "deadFlags": g["dead"],
    }
    if g.get("aivai"):
        v["spectator"] = True
    if g["phase"] == "bet":
        v["board"] = g["board"]                 # 공개된 베이스 (공용)
    elif g["phase"] == "play":
        my = color_of(g, player)
        op = opp(my)
        board = g["board"][:]
        if g.get("aivai"):
            # 관전자는 양쪽 히든을 다 본다 (마커로 표시)
            v["revealed"] = g["revealed"] + g["hidden"][BLACK] + g["hidden"][WHITE]
            v["myHidden"] = []
            v["oppHiddenCount"] = 0
        else:
            if not g["scoring"]:                 # 계가 땐 전부 공개
                for i in g["hidden"][op]:
                    board[i] = EMPTY             # 상대 미공개 히든은 아예 전송하지 않음
            v["revealed"] = g["revealed"]
            v["myHidden"] = g["hidden"][my]
            v["oppHiddenCount"] = len(g["hidden"][op])
        v["board"] = board
        v["turn"] = g["turn"]
        v["capB"] = g["capB"]
        v["capW"] = g["capW"]
        v["passes"] = g["passes"]
        v["hiddenUsed"] = {1: g["hidden_used"][BLACK], 2: g["hidden_used"][WHITE]}
        v["scan"] = {1: g["scan"][BLACK], 2: g["scan"][WHITE]}
        v["bases"] = {1: g["bases"][BLACK], 2: g["bases"][WHITE]}
        v["lastMove"] = g["last_move"]
        v["bets"] = g["bets"]                    # 양쪽 제출 후에만 존재하는 단계
    return v


# ---- AI 상대 (휴리스틱) ----
# 서버 안에 사는 가상 플레이어 2. 사람의 미공개 히든은 AI도 못 본 채 판단한다.

def ai_view_board(g, player=2):
    my = color_of(g, player)
    view = g["board"][:]
    for i in g["hidden"][opp(my)]:
        view[i] = EMPTY
    return view


def ai_basebuild(g, player=2):
    """매판 성향을 랜덤으로 굴려 가중치 추첨 — 판마다 포석이 달라져 패턴이 안 읽힘"""
    cnt = g["settings"]["baseCount"]
    style = random.random()          # <0.25 중앙지향 / <0.5 실리(귀·변)지향 / 나머지 혼합
    stones = []
    tries = 0
    while len(stones) < cnt and tries < 300:
        tries += 1
        r = random.randint(1, N - 2)
        c = random.randint(1, N - 2)
        idx = r * N + c
        if idx in stones:
            continue
        edge = min(r, c, N - 1 - r, N - 1 - c)          # 가장자리에서 몇 선인가
        w = {1: 0.5, 2: 1.0, 3: 0.9, 4: 0.6, 5: 0.5}.get(edge, 0.3)
        if style < 0.25:
            w *= 1.6 if edge >= 3 else 0.4               # 중앙지향
        elif style < 0.5:
            w *= 1.6 if edge <= 2 else 0.5               # 실리지향
        if any(abs(r - s // N) + abs(c - s % N) < 3 for s in stones):
            w *= 0.2                                      # 서로 뭉치지 않게
        if random.random() < w:
            stones.append(idx)
    while len(stones) < cnt:                              # 안전망
        idx = random.randint(0, N * N - 1)
        if idx not in stones:
            stones.append(idx)
    apply_action(g, player, {"type": "baseConfirm", "stones": stones})


def ai_bet_net(g, player=2):
    """신경망으로 공개된 배치를 평가해 베팅: '내가 흑이 되어 덤 k를 낼 때'의
    가치가 0 근처로 떨어지는 k가 공정가 — 그 언저리에서 부른다."""
    net = load_net()
    if net is None:
        return None
    try:
        import torch
        tb = _net["tb"]
        # 공개 보드(P1=1, P2=2)를 '내가 흑' 기준으로 변환
        me_raw = player          # 보드에서 내 돌 값
        op_raw = 2 if player == 1 else 1
        board = [BLACK if v == me_raw else WHITE if v == op_raw else EMPTY for v in g["board"]]
        my_bases = [i for i in g["base_stones"][player] if board[i] == BLACK]
        op_bases = [i for i in g["base_stones"][2 if player == 1 else 1] if board[i] == WHITE]

        game = tb.Game.__new__(tb.Game)
        game.board = board
        game.turn = BLACK
        game.passes = 0
        game.history = {tuple(board)}
        game.spots = [(p["idx"], p["val"]) for p in g["point_spots"]]
        game.spot_map = [0] * (N * N)
        for i, v in game.spots:
            game.spot_map[i] = v
        game.bases = {BLACK: my_bases, WHITE: op_bases}
        game.moves = 0
        game.over = False

        fair = 0
        with torch.no_grad():
            for k in range(0, 8):
                game.komi = k
                x = game.planes().unsqueeze(0).to(_net["device"])
                _, v = net(x)
                if float(v[0]) > 0.05:   # 덤 k를 내고 흑을 해도 아직 유리
                    fair = k
                else:
                    break
        bid = max(0, min(8, fair + random.choice([-1, 0, 0, 1])))   # 약간의 교란
        return bid
    except Exception as e:
        print("신경망 베팅 실패:", e)
        return None


def ai_bet(g, player=2):
    # 학습 모델이 있으면 배치를 평가해서 베팅, 없으면 현실적 범위 랜덤 (가끔 변칙)
    if g["settings"].get("aiLevel") == "신경망":
        v = ai_bet_net(g, player)
        if v is not None:
            apply_action(g, player, {"type": "bet", "value": v})
            return
    if random.random() < 0.12:
        v = random.choice([0, 1, 6, 7])
    else:
        v = random.choice([2, 3, 4, 5])
    apply_action(g, player, {"type": "bet", "value": v})


def ai_maybe_hidden(g, my, gain):
    """이 수를 히든으로 둘까? — 결정타 매복 + 평범한 수 교란, 확률 랜덤"""
    if g["hidden_used"][my] >= g["settings"]["hiddenMax"]:
        return False
    stones_on = sum(1 for x in g["board"] if x != EMPTY)
    r = random.random()
    if gain >= 6 and r < 0.3:
        return True                   # 결정타를 몰래
    if stones_on <= 30 and r < 0.07:
        return True                   # 평범한 수를 몰래 (교란용)
    return False


def ai_trap_hidden(g, player=2):
    """상대 약한 그룹의 활로에 매복 히든. 성공하면 True (차례 소모)"""
    my = color_of(g, player)
    op_c = opp(my)
    if g["hidden_used"][my] >= g["settings"]["hiddenMax"]:
        return False
    view = ai_view_board(g)
    lib_pts = []
    seen = set()
    for i in range(N * N):
        if view[i] == op_c and i not in seen:
            stones, libs = group_of(view, i)
            seen.update(stones)
            if libs <= 3:
                for s in stones:
                    for nb in neighbors(s):
                        if view[nb] == EMPTY:
                            lib_pts.append(nb)
    if not lib_pts:
        return False
    idx = random.choice(lib_pts)
    return apply_action(g, player, {"type": "hidden", "idx": idx}) is None


def ai_scan_center(g, player=2):
    """내 그룹 중 활로가 가장 적은 그룹의 활로 지점 — 히든 매복이 있을 법한 곳"""
    my = color_of(g, player)
    view = ai_view_board(g, player)
    best = None
    seen = set()
    for i in range(N * N):
        if view[i] == my and i not in seen:
            stones, libs = group_of(view, i)
            seen.update(stones)
            lib_pt = None
            for s in stones:
                for nb in neighbors(s):
                    if view[nb] == EMPTY:
                        lib_pt = nb
                        break
                if lib_pt is not None:
                    break
            if lib_pt is not None and (best is None or libs < best[0]):
                best = (libs, lib_pt)
    return best[1] if best else None


def group_libs(board, idx):
    """그룹의 돌 목록과 활로 '지점' 집합"""
    color = board[idx]
    stones = [idx]
    seen = {idx}
    libs = set()
    i = 0
    while i < len(stones):
        for nb in neighbors(stones[i]):
            if board[nb] == EMPTY:
                libs.add(nb)
            elif board[nb] == color and nb not in seen:
                seen.add(nb)
                stones.append(nb)
        i += 1
    return stones, libs


def influence_territory(board):
    """거리 기반 세력 추정 — 빈 자리가 어느 쪽 영역이 될 것 같은지 (흑점수, 백점수)"""
    INF = 99

    def dists(color):
        d = [INF] * (N * N)
        q = [i for i in range(N * N) if board[i] == color]
        for i in q:
            d[i] = 0
        head = 0
        while head < len(q):
            i = q[head]
            head += 1
            for nb in neighbors(i):
                if board[nb] == EMPTY and d[nb] > d[i] + 1:
                    d[nb] = d[i] + 1
                    q.append(nb)
        return d

    db, dw = dists(BLACK), dists(WHITE)
    tb = tw = 0.0
    for i in range(N * N):
        if board[i] != EMPTY:
            continue
        b, w = db[i], dw[i]
        if b < w:
            tb += min(1.0, 0.34 * (min(w, 12) - b))
        elif w < b:
            tw += min(1.0, 0.34 * (min(b, 12) - w))
    return tb, tw


def group_safety(board, my):
    """그룹 안전도: 내 위험 그룹 감점, 상대 위험 그룹 가점"""
    val = 0.0
    seen = set()
    for i in range(N * N):
        v = board[i]
        if v != EMPTY and i not in seen:
            stones, libs = group_of(board, i)
            seen.update(stones)
            if v == my:
                if libs == 1:
                    val -= 2.0 * len(stones)
                elif libs == 2:
                    val -= 0.6 * len(stones)
            else:
                if libs == 1:
                    val += 1.2 * len(stones)
                elif libs == 2:
                    val += 0.4 * len(stones)
    return val


def ai_evaluate_moves(g, player=2):
    """후보 수 평가 = 실제 계가 점수차 + 세력(잠재 집) + 그룹 안전도 + 자잘한 형태 보너스"""
    my = color_of(g, player)
    op_c = opp(my)
    view = ai_view_board(g, player)

    cands = []
    for idx in range(N * N):
        if view[idx] != EMPTY:
            continue
        nbs = neighbors(idx)
        if all(view[nb] == my for nb in nbs):
            continue                     # 자기 눈 메우기 회피
        board = view[:]
        board[idx] = my
        captured = []
        for nb in nbs:
            if board[nb] == op_c:
                stones, libs = group_of(board, nb)
                if libs == 0:
                    for s in stones:
                        board[s] = EMPTY
                    captured += stones
        _, my_libs = group_of(board, idx)
        if my_libs == 0:
            continue                     # 자살수

        _, _, _, _, sc = compute_score(g, board)
        diff = sc[my] - sc[op_c]

        tb, tw = influence_territory(board)                     # 잠재 집 (세력)
        diff += 0.5 * ((tb - tw) if my == BLACK else (tw - tb))

        diff += group_safety(board, my)                          # 그룹 위험/압박

        bonus = 0.0
        if any(g["board"][nb2] != EMPTY for nb2 in nbs):
            bonus += 0.3                 # 기존 돌 근처 선호 (약하게)
        r, c = idx // N, idx % N
        if r in (0, N - 1) or c in (0, N - 1):
            bonus -= 1.0                 # 1선 회피
        bonus += random.random() * 0.3   # 동점 셔플
        cands.append((diff + bonus, idx, len(captured)))
    cands.sort(key=lambda x: -x[0])
    return cands


# ---- 어려움: UCT 몬테카를로 트리 탐색 ----
# 시뮬레이션 결과를 트리로 쌓아 유망한 수를 더 깊이 파고든다 — 상대 응수까지 읽음.
MCTS_TIME = 2.5      # 수당 생각 시간(초)
UCB_C = 1.1          # 탐험 계수
MAX_CHILDREN = 12    # 노드당 후보 폭 (좁게 유지해 깊이를 벌게 함)


def playout(g, board, turn, max_moves=90):
    """무작위 실전 진행 + 전술 반사(방금 수 주변 단수 따내기/도망). 최종 보드 반환"""
    b = board[:]
    t = turn
    passes = 0
    last = None

    def place(idx):
        nbs = neighbors(idx)
        if all(b[nb] == t for nb in nbs):
            return False                      # 자기 눈 메우기
        b[idx] = t
        for nb in nbs:
            if b[nb] == opp(t):
                stones, libs = group_of(b, nb)
                if libs == 0:
                    for s in stones:
                        b[s] = EMPTY
        _, libs = group_of(b, idx)
        if libs == 0:
            b[idx] = EMPTY                    # 자살수 롤백
            return False
        return True

    for _ in range(max_moves):
        placed = False
        # 전술 반사: 방금 수 주변에 단수 그룹이 생겼으면 따내기/도망을 우선
        if last is not None and random.random() < 0.8:
            tactical = []
            checked = set()
            for nb in neighbors(last):
                if b[nb] != EMPTY and nb not in checked:
                    stones, libs = group_libs(b, nb)
                    checked.update(stones)
                    if len(libs) == 1:
                        tactical.append(next(iter(libs)))
            random.shuffle(tactical)
            for idx in tactical:
                if place(idx):
                    placed = True
                    last = idx
                    break
        if not placed:
            empties = [i for i in range(N * N) if b[i] == EMPTY]
            random.shuffle(empties)
            for idx in empties:
                if place(idx):
                    placed = True
                    last = idx
                    break
        if placed:
            passes = 0
        else:
            passes += 1
            last = None
            if passes >= 2:
                break
        t = opp(t)
    return b


def try_place(board, turn, idx):
    """따내기 포함 착수 시뮬레이션. 성공: 새 보드 / 자살수: None"""
    b = board[:]
    b[idx] = turn
    for nb in neighbors(idx):
        if b[nb] == opp(turn):
            stones, libs = group_of(b, nb)
            if libs == 0:
                for s in stones:
                    b[s] = EMPTY
    _, libs = group_of(b, idx)
    if libs == 0:
        return None
    return b


def legal_moves_shuffled(board, turn):
    pts = []
    for i in range(N * N):
        if board[i] != EMPTY:
            continue
        if all(board[nb] == turn for nb in neighbors(i)):
            continue                       # 자기 눈 메우기
        pts.append(i)
    random.shuffle(pts)
    return pts


class MNode:
    __slots__ = ("board", "turn", "passes", "move", "parent", "children", "untried", "visits", "value")

    def __init__(self, board, turn, passes, move=None, parent=None, untried=None):
        self.board = board
        self.turn = turn
        self.passes = passes
        self.move = move
        self.parent = parent
        self.children = []
        self.untried = untried if untried is not None else legal_moves_shuffled(board, turn) + ["pass"]
        self.visits = 0
        self.value = 0.0


def ucb_select(node, ai):
    logN = math.log(node.visits + 1)
    best, best_u = None, -1e9
    for c in node.children:
        mean = c.value / c.visits
        q = mean if node.turn == ai else 1.0 - mean   # 상대 차례 노드에선 AI에게 나쁜 수가 '좋은' 수
        u = q + UCB_C * math.sqrt(logN / c.visits)
        if u > best_u:
            best_u, best = u, c
    return best


def mcts_choose(g, root_order, time_budget, player=2):
    """UCT 탐색 후 루트 후보를 방문수 순으로 반환. (moves, 반복횟수)"""
    ai = color_of(g, player)
    root = MNode(ai_view_board(g, player), ai, g["passes"],
                 untried=root_order[:MAX_CHILDREN + 6] + ["pass"])
    deadline = time.time() + time_budget
    iters = 0

    while time.time() < deadline:
        iters += 1
        node = root
        # 1) 선택 + 2) 확장
        while True:
            if node.passes >= 2:
                break                                          # 종국 노드
            if node.untried and len(node.children) < MAX_CHILDREN:
                mv = node.untried.pop(0)
                if mv == "pass":
                    child = MNode(node.board[:], opp(node.turn), node.passes + 1, "pass", node)
                else:
                    b = try_place(node.board, node.turn, mv)
                    if b is None:
                        continue                               # 자살수 → 같은 노드에서 다음 후보
                    child = MNode(b, opp(node.turn), 0, mv, node)
                node.children.append(child)
                node = child
                break                                          # 새 노드에서 롤아웃
            if not node.children:
                break
            node = ucb_select(node, ai)

        # 3) 롤아웃 + 평가 (바투 점수 기준 승/패)
        final = node.board if node.passes >= 2 else playout(g, node.board, node.turn, max_moves=70)
        _, _, _, _, sc = compute_score(g, final)
        d = sc[ai] - sc[opp(ai)]
        reward = 1.0 if d > 0 else (0.5 if d == 0 else 0.0)
        reward += max(-0.1, min(0.1, d / 200.0))               # 근소한 마진 보너스

        # 4) 역전파
        while node:
            node.visits += 1
            node.value += reward
            node = node.parent

    ranked = sorted(root.children, key=lambda c: -c.visits)
    return [c.move for c in ranked], iters


def ai_play_mcts(g, cands, diff0, player=2):
    my = color_of(g, player)
    gain = {idx: s - diff0 for s, idx, _ in cands}
    root_order = [idx for _, idx, _ in cands]
    ranked, _ = mcts_choose(g, root_order, MCTS_TIME, player)

    for mv in ranked:
        if mv == "pass":
            if apply_action(g, player, {"type": "pass"}) is None:
                return
            continue
        as_hidden = ai_maybe_hidden(g, my, gain.get(mv, 0))
        if apply_action(g, player, {"type": "hidden" if as_hidden else "move", "idx": mv}) is None:
            return
    apply_action(g, player, {"type": "pass"})


def ai_play(g, player=2):
    level = g["settings"].get("aiLevel", "보통")
    my = color_of(g, player)

    # (풀게임 신경망은 백 시점 정책 학습 부실 버그로 비활성화 — 착수전용 iter739 사용)
    # if level == "신경망" and net_full_available():
    #     if ai_play_net_full(g, player):
    #         return

    # 상대가 히든을 갖고 있고 내 스캔이 남았으면 가끔 스캔 (내 약한 그룹 주변)
    scan_left = g["scan"][my] // SCAN_COST < g["settings"].get("scanMax", 1)
    if scan_left and g["hidden"][opp(my)] and random.random() < 0.15:
        center = ai_scan_center(g, player)
        if center is not None:
            apply_action(g, player, {"type": "scan", "idx": center})

    # 가끔은 좋은 수 대신 함정 히든을 판다 (상대 약한 그룹 활로에 매복)
    if random.random() < 0.07 and ai_trap_hidden(g, player):
        return

    cands = ai_evaluate_moves(g, player)
    base_view = ai_view_board(g, player)
    _, _, _, _, sc0 = compute_score(g, base_view)
    diff0 = sc0[my] - sc0[opp(my)]

    if not cands:
        apply_action(g, player, {"type": "pass"})
        return

    if level == "신경망":
        gains = {idx: s - diff0 for s, idx, _ in cands}
        if ai_play_net(g, player, gains):       # 착수전용 신경망 (iter739, 검증됨)
            return
        ai_play_mcts(g, cands, diff0, player)   # 폴백 UCT
        return

    if level == "어려움":
        ai_play_mcts(g, cands, diff0, player)   # 몬테카를로 수읽기
        return

    tried = 0
    while cands and tried < 10:
        if level == "보통":
            # 최선수 위주 가중 선택 (60% / 25% / 15%)
            r = random.random()
            k = 0 if r < 0.6 else 1 if r < 0.85 else 2
            pick = cands[min(k, len(cands) - 1)]
        else:
            pick = random.choice(cands[:8])
        score, idx, ncap = pick
        as_hidden = ai_maybe_hidden(g, my, score - diff0)
        err = apply_action(g, player, {"type": "hidden" if as_hidden else "move", "idx": idx})
        if err is None:
            return
        cands.remove(pick)
        tried += 1
    apply_action(g, player, {"type": "pass"})    # 둘 곳 없음 → 한 수 쉼


# ---- 신경망 AI (학습된 batoo_net.pt 가 게임 폴더에 있으면 활성화) ----
NET_PATH = os.path.join(ROOT, "batoo_net.pt")
NET_SIMS = 160       # 신경망 MCTS 시뮬레이션 수
_net = {"tried": False, "net": None, "tb": None, "device": "cpu"}


def load_net():
    """batoo_net.pt 로드 (1회 시도, torch 없거나 파일 없으면 None)"""
    if _net["tried"]:
        return _net["net"]
    _net["tried"] = True
    if not os.path.exists(NET_PATH):
        return None
    try:
        import importlib.util
        import torch
        spec = importlib.util.spec_from_file_location("train_batoo", os.path.join(ROOT, "train_batoo.py"))
        tb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tb)
        device = ("mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
                  else "cuda" if torch.cuda.is_available() else "cpu")
        net = tb.BatooNet().to(device)
        ck = torch.load(NET_PATH, map_location=device)
        net.load_state_dict(ck["net"])
        net.eval()
        _net.update(net=net, tb=tb, device=device)
        print(f"🧠 신경망 로드: {NET_PATH} (iter {ck.get('iter', '?')}, {device})")
        return net
    except Exception as e:
        print("신경망 로드 실패:", e)
        return None


def net_available():
    return load_net() is not None


# ---- 풀게임 신경망 (batoo_net_full.pt — 히든·스캔까지 직접 두는 모델) ----
NET_FULL_PATH = os.path.join(ROOT, "batoo_net_full.pt")
NET_FULL_SIMS = 128
_netfull = {"tried": False, "net": None, "tb": None, "device": "cpu"}


def load_net_full():
    if _netfull["tried"]:
        return _netfull["net"]
    _netfull["tried"] = True
    if not os.path.exists(NET_FULL_PATH):
        return None
    try:
        import importlib.util
        import torch
        spec = importlib.util.spec_from_file_location("train_batoo_full", os.path.join(ROOT, "train_batoo_full.py"))
        tb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tb)
        device = ("mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
                  else "cuda" if torch.cuda.is_available() else "cpu")
        net = tb.BatooNet().to(device)
        ck = torch.load(NET_FULL_PATH, map_location=device)
        net.load_state_dict(ck["net"])
        net.eval()
        _netfull.update(net=net, tb=tb, device=device)
        print(f"🧠 풀게임 신경망 로드: {NET_FULL_PATH} (iter {ck.get('iter', '?')}, {device})")
        return net
    except Exception as e:
        print("풀게임 신경망 로드 실패:", e)
        return None


def ai_play_net_full(g, player=2):
    """풀게임 신경망으로 한 수 — 착수/히든/스캔을 신경망이 직접 선택. 성공 True."""
    net = load_net_full()
    if net is None:
        return False
    tb = _netfull["tb"]
    my = color_of(g, player)

    # 서버 상태 → tb.Game (색 기준으로 그대로 매핑, 상대 히든은 tb가 view에서 가림)
    game = tb.Game.__new__(tb.Game)
    game.board = g["board"][:]
    game.turn = my
    game.passes = g["passes"]
    game.history = set(g["board_history"])
    game.spots = [(p["idx"], p["val"]) for p in g["point_spots"]]
    game.spot_map = [0] * (N * N)
    for i, v in game.spots:
        game.spot_map[i] = v
    game.bases = {BLACK: g["bases"][BLACK][:], WHITE: g["bases"][WHITE][:]}
    game.moves = sum(1 for x in g["board"] if x != EMPTY)
    game.acts = game.moves
    game.over = False
    game.komi = g["compensation"]
    game.hidden = {BLACK: g["hidden"][BLACK][:], WHITE: g["hidden"][WHITE][:]}
    game.hidden_used = {BLACK: g["hidden_used"][BLACK], WHITE: g["hidden_used"][WHITE]}
    game.scan_given = {BLACK: g["scan"][BLACK], WHITE: g["scan"][WHITE]}
    game.scan_cnt = {BLACK: g["scan"][BLACK] // SCAN_COST, WHITE: g["scan"][WHITE] // SCAN_COST}

    try:
        _, action = tb.mcts_policy(net, game, NET_FULL_SIMS, _netfull["device"], temperature=0)
    except Exception as e:
        print("풀게임 신경망 착수 실패:", e)
        return False

    # tb 행동 → 서버 액션
    if action == tb.A_PASS:
        return apply_action(g, player, {"type": "pass"}) is None
    if action >= tb.A_SCAN:
        idx = action - tb.A_SCAN
        return apply_action(g, player, {"type": "scan", "idx": idx}) is None
    if action >= tb.A_HID:
        idx = action - tb.A_HID
        return apply_action(g, player, {"type": "hidden", "idx": idx}) is None
    return apply_action(g, player, {"type": "move", "idx": action}) is None


def net_full_available():
    return load_net_full() is not None


def ai_play_net(g, player=2, gains=None):
    """신경망 MCTS로 한 수. 성공 True / 불가 False(→ UCT 폴백)
    gains: 휴리스틱 이득 맵(착수→점수이득) — 히든(결정타 매복) 판단에 사용"""
    net = load_net()
    if net is None:
        return False
    tb = _net["tb"]
    my = color_of(g, player)
    view = ai_view_board(g, player)

    # 서버 상태 → 학습용 Game 어댑터 (보이는 판 기준)
    game = tb.Game.__new__(tb.Game)
    game.board = view[:]
    game.turn = my
    game.passes = g["passes"]
    game.history = {tuple(view)}
    game.spots = [(p["idx"], p["val"]) for p in g["point_spots"]]
    game.spot_map = [0] * (N * N)
    for i, v in game.spots:
        game.spot_map[i] = v
    game.bases = {BLACK: g["bases"][BLACK][:], WHITE: g["bases"][WHITE][:]}
    game.moves = sum(1 for x in view if x != EMPTY)
    game.over = False
    game.komi = g["compensation"]      # 실전 턴베팅 값을 신경망에 그대로 전달

    try:
        _, action = tb.mcts_policy(net, game, NET_SIMS, _net["device"], temperature=0)
    except Exception as e:
        print("신경망 착수 실패:", e)
        return False

    if action == tb.PASS:
        return apply_action(g, player, {"type": "pass"}) is None
    # 히든 사용 판단은 기존 로직 재사용 (이득값은 휴리스틱 평가에서)
    as_hidden = ai_maybe_hidden(g, my, (gains or {}).get(action, 0))
    err = apply_action(g, player, {"type": "hidden" if as_hidden else "move", "idx": action})
    return err is None


AIVAI_DELAY = 1.6    # 관전 모드에서 수와 수 사이 간격(초) — 사람이 따라볼 수 있는 속도


def run_ai(g):
    """사람 액션/폴링 후 호출 — AI 차례의 일을 전부 처리. 뭔가 했으면 True."""
    if not g.get("ai") or g["over"]:
        return False

    # ---- AI vs AI 관전 모드: 양쪽 다 AI, 착수는 페이스 조절 ----
    if g.get("aivai"):
        acted = False
        for p in (1, 2):
            if g["phase"] == "base" and not g["base_confirmed"][p]:
                ai_basebuild(g, p)
                acted = True
            if g["phase"] == "bet" and g["bets"][p] is None:
                ai_bet(g, p)
                acted = True
        if (g["phase"] == "play" and not g["scoring"] and not g["over"]
                and time.time() - g.get("last_ai_move", 0) >= AIVAI_DELAY):
            p = player_of_color(g, g["turn"])
            ai_play(g, p)
            g["last_ai_move"] = time.time()
            g["clock_ts"] = time.time()
            acted = True
        if g["scoring"] and not g["over"]:
            finalize_score(g)             # 양쪽 쉼으로 종국 → 그대로 자동 계가
            acted = True
        return acted

    # ---- 사람 vs AI: AI는 플레이어 2 ----
    acted = False
    for _ in range(5):
        if g["phase"] == "base" and not g["base_confirmed"][2]:
            ai_basebuild(g)
            acted = True
            continue
        if g["phase"] == "bet" and g["bets"][2] is None:
            ai_bet(g)
            acted = True
            continue
        if (g["phase"] == "play" and not g["scoring"] and not g["over"]
                and g["turn"] == color_of(g, 2)):
            ai_play(g)
            g["clock_ts"] = time.time()   # AI 생각 시간은 사람 시계에서 빼지 않음
            acted = True
            continue
        break
    return acted


# ---- HTTP ----
def new_code():
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(4))
        if code not in rooms:
            return code


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def _player(self, room, token):
        for p, t in room["tokens"].items():
            if t == token and t is not None:
                return p
        return None

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self._body()
        except Exception:
            return self._json({"error": "bad json"}, 400)

        with lock:
            if path == "/api/create":
                s = data.get("settings") or {}
                settings = {
                    "mainSec": int(s.get("mainSec", 300)),
                    "byoSec": int(s.get("byoSec", 20)),
                    "byoPeriods": int(s.get("byoPeriods", 3)),
                    "baseCount": max(1, min(6, int(s.get("baseCount", 3)))),
                    "hiddenMax": max(0, min(3, int(s.get("hiddenMax", 1)))),
                    "scanMax": max(0, min(3, int(s.get("scanMax", 1)))),   # 원작: 스캔 대국당 1회
                    "aiLevel": s.get("aiLevel") if s.get("aiLevel") in ("쉬움", "보통", "어려움", "신경망") else "보통",
                }
                field = data.get("field") if data.get("field") in FIELDS else "기본"
                is_aivai = bool(data.get("aivai"))
                is_ai = bool(data.get("ai")) or is_aivai
                if is_aivai:
                    settings["mainSec"] = 0     # 관전 모드는 시간제한 없음
                    settings["byoSec"] = 0
                code = new_code()
                token = secrets.token_hex(8)
                # AI 방은 2번 자리를 서버가 차지 (외부에서 join 불가)
                rooms[code] = {"tokens": {1: token, 2: "AI" if is_ai else None},
                               "game": new_game(settings, field)}
                g = rooms[code]["game"]
                if is_aivai:
                    g["ai"] = True
                    g["aivai"] = True
                    run_ai(g)   # 양쪽 베이스빌드·베팅 즉시 → 바로 관전 시작
                    g["notices"][1] = f"👀 AI vs AI 관전 ({settings['aiLevel']}) — 편하게 지켜보세요!"
                elif is_ai:
                    g["ai"] = True
                    run_ai(g)   # AI가 즉시 베이스빌드 확정
                    g["notices"][1] = f"🤖 AI({settings['aiLevel']})와의 대국! 베이스빌드를 놓고 확정하세요."
                else:
                    g["notices"][1] = "베이스빌드를 놓고 확정하세요. 상대 입장을 기다리는 중…"
                g["seq"] += 1
                return self._json({"room": code, "player": 1, "token": token, "ai": is_ai,
                                   "aivai": is_aivai,
                                   "seq": g["seq"], "view": view_for(g, 1)})

            if path == "/api/join":
                code = (data.get("room") or "").strip().upper()
                room = rooms.get(code)
                if not room:
                    return self._json({"error": "방을 찾을 수 없습니다"}, 404)
                if room["tokens"][2]:
                    return self._json({"error": "방이 가득 찼습니다"}, 409)
                token = secrets.token_hex(8)
                room["tokens"][2] = token
                g = room["game"]
                g["notices"][2] = "입장 완료! 베이스빌드를 놓고 확정하세요."
                g["notices"][1] = "✅ 상대가 입장했습니다!"
                g["seq"] += 1
                return self._json({"room": code, "player": 2, "token": token,
                                   "seq": g["seq"], "view": view_for(g, 2)})

            if path == "/api/action":
                room = rooms.get(data.get("room"))
                if not room:
                    return self._json({"error": "no room"}, 404)
                player = self._player(room, data.get("token"))
                if not player:
                    return self._json({"error": "bad token"}, 403)
                g = room["game"]
                if g.get("aivai"):
                    return self._json({"seq": g["seq"], "view": view_for(g, player),
                                       "error": "관전 모드입니다 — AI 대국을 지켜보세요."})
                update_clocks(g)
                if g["over"] and (data.get("action") or {}).get("type") not in ("toggleDead",):
                    return self._json({"seq": g["seq"], "view": view_for(g, player),
                                       "error": None})
                prev_turn = g["turn"]
                err = apply_action(g, player, data.get("action") or {})
                if err:
                    return self._json({"seq": g["seq"], "view": view_for(g, player), "error": err})
                if g["phase"] == "play" and g["turn"] != prev_turn:
                    reset_byo(g, prev_turn)
                    g["clock_ts"] = time.time()
                run_ai(g)   # AI 방이면 이어서 AI가 행동
                g["seq"] += 1
                return self._json({"seq": g["seq"], "view": view_for(g, player), "error": None})

        return self._json({"error": "not found"}, 404)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/poll":
            q = parse_qs(parsed.query)
            with lock:
                room = rooms.get((q.get("room", [""])[0]).upper())
                if not room:
                    return self._json({"error": "no room"}, 404)
                player = self._player(room, q.get("token", [""])[0])
                if not player:
                    return self._json({"error": "bad token"}, 403)
                g = room["game"]
                update_clocks(g)          # 폴링 시에도 시간패 판정
                if run_ai(g):             # AI가 밀린 일을 처리했다면 갱신 알림
                    g["seq"] += 1
                since = int(q.get("since", ["0"])[0])
                resp = {"seq": g["seq"]}
                if g["seq"] > since:
                    resp["view"] = view_for(g, player)
                elif g["clocks"]:
                    resp["clocks"] = g["clocks"]   # 시계만 보정
                return self._json(resp)
        return super().do_GET()


if __name__ == "__main__":
    print(f"바투 서버(서버 권위) 가동: http://localhost:{PORT}/batoo/")
    print("같은 와이파이의 상대는 이 컴퓨터의 IP 주소로 접속하세요.")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
