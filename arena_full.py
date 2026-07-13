#!/usr/bin/env python3
"""풀게임 신경망(batoo_net_full.pt) vs 기존 UCT AI 대결 — 실력 측정.

사용법:  python3 arena_full.py [판수]   (기본 6)
풀게임 신경망은 히든·스캔까지 스스로 두고, UCT(server.py의 '어려움')는 착수+규칙기반 히든/스캔.
공정 비교를 위해 히든 1·스캔 1 규칙으로 진행.
"""
import importlib.util
import random
import sys
import time

import torch


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tb = load("tbfull", "train_batoo_full.py")
sv = load("sv", "server.py")

GAMES = int(sys.argv[1]) if len(sys.argv) > 1 else 6
UCT_TIME = 1.0
NET_SIMS = 128

dev = "mps" if torch.backends.mps.is_available() else "cpu"
net = tb.BatooNet().to(dev)
net.load_state_dict(torch.load("batoo_net_full.pt", map_location=dev)["net"])
net.eval()


def uct_action(tbgame, player_color):
    """server.py의 UCT로 한 수 — tb.Game 상태를 server 게임 dict로 옮겨 계산."""
    g = sv.new_game({"mainSec": 0, "byoSec": 0, "byoPeriods": 3, "baseCount": 3,
                     "hiddenMax": 1, "scanMax": 1, "aiLevel": "어려움"}, "기본")
    g["phase"] = "play"
    g["ai"] = True
    # UCT를 '플레이어 2'로 두게 하고, 그 색을 player_color로 매핑
    g["black_player"] = 2 if player_color == tb.BLACK else 1
    g["board"] = tbgame.board[:]
    g["turn"] = player_color
    g["point_spots"] = [{"idx": i, "val": v} for i, v in tbgame.spots]
    g["compensation"] = tbgame.komi
    g["bases"] = {sv.BLACK: tbgame.bases[tb.BLACK][:], sv.WHITE: tbgame.bases[tb.WHITE][:]}
    g["hidden"] = {sv.BLACK: tbgame.hidden[tb.BLACK][:], sv.WHITE: tbgame.hidden[tb.WHITE][:]}
    g["hidden_used"] = {sv.BLACK: tbgame.hidden_used[tb.BLACK], sv.WHITE: tbgame.hidden_used[tb.WHITE]}
    g["scan"] = {sv.BLACK: tbgame.scan_given[tb.BLACK], sv.WHITE: tbgame.scan_given[tb.WHITE]}
    g["board_history"] = set(tbgame.history)
    old = sv.MCTS_TIME
    sv.MCTS_TIME = UCT_TIME
    before = g["board"][:]
    turn0 = g["turn"]
    sv.ai_play(g, 2 if player_color == tb.BLACK else 1)
    sv.MCTS_TIME = old
    # server가 둔 결과를 tb.Game으로 반영: 바뀐 칸 찾기
    tbgame.board = g["board"][:]
    tbgame.hidden = {tb.BLACK: g["hidden"][sv.BLACK][:], tb.WHITE: g["hidden"][sv.WHITE][:]}
    tbgame.hidden_used = {tb.BLACK: g["hidden_used"][sv.BLACK], tb.WHITE: g["hidden_used"][sv.WHITE]}
    tbgame.scan_given = {tb.BLACK: g["scan"][sv.BLACK], tb.WHITE: g["scan"][sv.WHITE]}
    tbgame.bases = {tb.BLACK: g["bases"][sv.BLACK][:], tb.WHITE: g["bases"][sv.WHITE][:]}
    tbgame.turn = g["turn"]
    tbgame.history.add(tuple(tbgame.board))
    tbgame.moves += 1
    if tbgame.moves >= tb.MAX_MOVES:
        tbgame.over = True


def one_game(net_black):
    g = tb.Game()
    g.komi = random.randint(0, 7)
    guard = 0
    while not g.over and guard < 220:
        guard += 1
        net_turn = (g.turn == tb.BLACK) == net_black
        if net_turn:
            _, act = tb.mcts_policy(net, g, NET_SIMS, dev, temperature=0.2 if g.moves < 8 else 0)
            if not g.play(act):
                g.play(tb.A_PASS)
        else:
            uct_action(g, g.turn)
    d = g.score_diff()
    nd = d if net_black else -d
    if nd == 0:
        nd = -0.5 if net_black else 0.5
    return nd


wins = 0
t0 = time.time()
print(f"🧠 풀게임 신경망(iter510) vs ⚔️ UCT({UCT_TIME}s) — {GAMES}판")
for i in range(GAMES):
    d = one_game(net_black=(i % 2 == 0))
    if d > 0:
        wins += 1
    print(f"  게임 {i+1} (신경망={'흑' if i % 2 == 0 else '백'}): "
          f"{'승' if d > 0 else '패'} ({d:+.0f}점)")
print(f"\n풀게임 신경망 {wins}승 {GAMES - wins}패 (승률 {wins/GAMES*100:.0f}%) | {time.time()-t0:.0f}초")
