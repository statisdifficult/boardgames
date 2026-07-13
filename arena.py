#!/usr/bin/env python3
"""아레나 — 학습된 신경망 vs 기존 UCT AI 자동 대결로 실력 측정.

사용법:  python3 arena.py [판수]     (기본 6판, batoo_net.pt 필요)
학습이 잘 됐는지 판정: 신경망 승률이 50%를 넘게 유지되면 성공적으로 배우고 있는 것.
"""
import importlib.util
import random
import sys
import time


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sv = load("sv", "server.py")

if not sv.net_available():
    print("❌ batoo_net.pt 가 없습니다. 학습된 모델 파일을 이 폴더에 넣고 다시 실행하세요.")
    sys.exit(1)

GAMES = int(sys.argv[1]) if len(sys.argv) > 1 else 6
UCT_TIME = 1.0     # 상대(UCT)의 수당 생각 시간

S = {"mainSec": 0, "byoSec": 0, "byoPeriods": 3, "baseCount": 3, "hiddenMax": 0, "aiLevel": "보통"}


def one_game(net_is_black):
    g = sv.new_game(dict(S), "기본")
    g["ai"] = True
    g["phase"] = "play"
    g["compensation"] = 0
    g["point_spots"] = sv.gen_point_spots("기본")
    pool = [i for i in range(121) if 1 <= i // 11 <= 9 and 1 <= i % 11 <= 9]
    spots = random.sample(pool, 6)
    for i in spots[:3]:
        g["board"][i] = 1
    for i in spots[3:]:
        g["board"][i] = 2
    g["bases"][1] = spots[:3]
    g["bases"][2] = spots[3:]
    g["board_history"] = {tuple(g["board"])}
    g["turn"] = 1

    for _ in range(160):
        if g["over"] or g["scoring"]:
            break
        c = g["turn"]
        is_net = (c == 1) == net_is_black
        g["black_player"] = 2 if c == 1 else 1        # 두는 색이 '플레이어 2'가 되도록
        if is_net:
            if not sv.ai_play_net(g):
                g["settings"]["aiLevel"] = "어려움"    # 폴백
                old = sv.MCTS_TIME
                sv.MCTS_TIME = UCT_TIME
                sv.ai_play(g)
                sv.MCTS_TIME = old
        else:
            g["settings"]["aiLevel"] = "어려움"
            old = sv.MCTS_TIME
            sv.MCTS_TIME = UCT_TIME
            sv.ai_play(g)
            sv.MCTS_TIME = old
    _, _, _, _, sc = sv.compute_score(g, g["board"])
    nc = 1 if net_is_black else 2
    d = sc[nc] - sc[3 - nc]
    if d == 0:                       # 동점=백 승
        d = -0.5 if net_is_black else 0.5
    return d


wins = 0
t0 = time.time()
print(f"🧠 신경망 vs ⚔️ UCT({UCT_TIME}s) — {GAMES}판")
for i in range(GAMES):
    d = one_game(net_is_black=(i % 2 == 0))
    if d > 0:
        wins += 1
    print(f"  게임 {i + 1} (신경망={'흑' if i % 2 == 0 else '백'}): "
          f"{'승' if d > 0 else '패' if d < 0 else '무'} ({d:+.0f}점)")
print(f"\n신경망 {wins}승 {GAMES - wins}패 (승률 {wins / GAMES * 100:.0f}%) | {time.time() - t0:.0f}초")
print("→ 승률 50% 초과가 유지되면 학습이 UCT를 넘어선 것. 학습을 더 하면 계속 올라갑니다.")
