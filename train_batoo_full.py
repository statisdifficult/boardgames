#!/usr/bin/env python3
"""바투 풀게임 학습 — 베팅·히든·스캔까지 정책이 직접 배우는 확장판.

기존 train_batoo.py(착수만 학습)와의 차이:
  - 행동 364개: 일반착수 121 + 히든착수 121 + 스캔(3×3 중심) 121 + 쉼 1
  - 불완전정보: 각자 자기 시점으로 수읽기, 시뮬레이션마다 상대 히든 위치를 샘플링(determinization)
  - 베팅: 절반은 무작위 덤(탐험), 절반은 가치망 공정가 질의(실전형)
  - 이식: --transplant 로 기존 7평면/122행동 체크포인트의 몸통을 옮겨와 이어서 배움

■ 리눅스 다코어 CPU 실행:
    python3 train_batoo_full.py --smoke --workers 2                       # 동작 확인
    python3 train_batoo_full.py --hours 24 --workers 24 --games-per-iter 48 \
        --transplant batoo_net.pt                                          # 기존 지식 이식 후 시작
    → 결과물: batoo_net_full.pt (기존 batoo_net.pt와 별개 파일)

로그 지표: 기존 지표 + "히든 h·스캔 s" (판 48개 합계 — 둘 다 0이 지속되면 그 기능을 안 배우는 것)
"""
import argparse
import math
import multiprocessing as mp
import os
import random
import time

import torch
import torch.nn as nn
import torch.nn.functional as F

# ================= 게임 상수 =================
N = 11
NN = N * N
EMPTY, BLACK, WHITE = 0, 1, 2
BASE_VALUE = 5
SCAN_COST = 2
HIDDEN_MAX = 1     # 대국당 히든 횟수 (원작 규칙)
SCAN_MAX = 1       # 대국당 스캔 횟수 (원작 규칙 — 히든과 동일하게 1회)
PLUS_VALUES = [5, 3, 2]
MINUS_VALUES = [-5, -3, -2]
FIELDS = [
    {"plus": [24, 96, 60], "minus": [30, 90, 63]},
    {"plus": [30, 90, 60], "minus": [24, 96, 57]},
    {"plus": [27, 93, 60], "minus": [57, 63, 96]},
    {"plus": [60, 27, 93], "minus": [24, 30, 96]},
    {"plus": [60, 57, 63], "minus": [24, 90, 96]},
    {"plus": [36, 84, 60], "minus": [26, 94, 38]},
    {"plus": [46, 74, 16], "minus": [104, 48, 72]},
    {"plus": [25, 95, 59], "minus": [61, 29, 91]},
]

# 행동 인코딩: [0,121) 일반착수 / [121,242) 히든착수 / [242,363) 스캔 중심 / 363 쉼
A_OPEN, A_HID, A_SCAN, A_PASS = 0, NN, 2 * NN, 3 * NN
N_ACTIONS = 3 * NN + 1
IN_PLANES = 11
MAX_MOVES = 140      # 돌 착수+쉼 기준 (스캔은 미포함)
MAX_ACTIONS = 200    # 안전 상한

NEIGHBORS = []
for _i in range(NN):
    _x, _y = _i % N, _i // N
    _r = []
    if _x > 0: _r.append(_i - 1)
    if _x < N - 1: _r.append(_i + 1)
    if _y > 0: _r.append(_i - N)
    if _y < N - 1: _r.append(_i + N)
    NEIGHBORS.append(_r)


def opp(c):
    return WHITE if c == BLACK else BLACK


def group_of(board, idx):
    color = board[idx]
    stones = [idx]
    seen = {idx}
    libs = 0
    lib_seen = set()
    i = 0
    while i < len(stones):
        for nb in NEIGHBORS[stones[i]]:
            v = board[nb]
            if v == EMPTY:
                if nb not in lib_seen:
                    lib_seen.add(nb)
                    libs += 1
            elif v == color and nb not in seen:
                seen.add(nb)
                stones.append(nb)
        i += 1
    return stones, libs


class Game:
    """풀게임 바투: 진실 보드 + 색별 미공개 히든 + 스캔. planes()는 '현재 둘 사람' 시점."""
    __slots__ = ("board", "turn", "passes", "history", "spots", "spot_map", "bases",
                 "moves", "acts", "over", "komi",
                 "hidden", "hidden_used", "scan_given", "scan_cnt")

    def __init__(self):
        self.board = [EMPTY] * NN
        layout = random.choice(FIELDS)
        pv = PLUS_VALUES[:]; random.shuffle(pv)
        mv = MINUS_VALUES[:]; random.shuffle(mv)
        self.spots = ([(i, v) for i, v in zip(layout["plus"], pv)] +
                      [(i, v) for i, v in zip(layout["minus"], mv)])
        self.spot_map = [0] * NN
        for i, v in self.spots:
            self.spot_map[i] = v
        pool = [i for i in range(NN) if 1 <= i // N <= 9 and 1 <= i % N <= 9]
        picks = random.sample(pool, 6)
        for i in picks[:3]:
            self.board[i] = BLACK
        for i in picks[3:]:
            self.board[i] = WHITE
        self.bases = {BLACK: picks[:3], WHITE: picks[3:]}
        self.turn = BLACK
        self.passes = 0
        self.history = {tuple(self.board)}
        self.moves = 0
        self.acts = 0
        self.over = False
        self.komi = random.randint(0, 7)          # selfplay에서 재설정될 수 있음
        self.hidden = {BLACK: [], WHITE: []}      # 미공개 히든 위치 (진실)
        self.hidden_used = {BLACK: 0, WHITE: 0}
        self.scan_given = {BLACK: 0, WHITE: 0}    # 스캔으로 상대에게 준 점수
        self.scan_cnt = {BLACK: 0, WHITE: 0}   # 통계용(로그 표시), 제한 목적 아님

    # ---- 시점 ----
    def view_board(self, viewer):
        """viewer가 보는 판 = 진실 − 상대의 미공개 히든"""
        b = self.board[:]
        for i in self.hidden[opp(viewer)]:
            b[i] = EMPTY
        return b

    def legal_mask(self):
        """현재 둘 사람 기준 364 행동 마스크 (자기 시점 — 상대 히든은 모름)"""
        me = self.turn
        vb = self.view_board(me)
        mask = [False] * N_ACTIONS
        any_move = False
        can_hide = self.hidden_used[me] < HIDDEN_MAX
        for i in range(NN):
            if vb[i] != EMPTY:
                continue
            if all(vb[nb] == me for nb in NEIGHBORS[i]):
                continue                            # 자기 눈
            mask[A_OPEN + i] = True
            any_move = True
            if can_hide:
                mask[A_HID + i] = True
        # 스캔: 상대가 미공개 히든을 갖고 있(다고 아는) 때만
        opp_unrev = self.hidden_used[opp(me)] - (0 if self.hidden[opp(me)] else self.hidden_used[opp(me)])
        # 공개 정보로는 '사용 수'만 알지만, 발각/스캔으로 공개된 것은 양쪽 다 안다.
        # 미공개 개수 = len(self.hidden[opp]) 는 사실 비밀이지만, '사용했고 아직 안 드러남'은 추적 가능 정보.
        opp_unrev = len(self.hidden[opp(me)])
        if opp_unrev > 0 and self.scan_cnt[me] < SCAN_MAX:   # 스캔 잔여 있고 상대 미공개 히든 존재
            for i in range(NN):
                mask[A_SCAN + i] = True
        mask[A_PASS] = (not any_move) or self.moves >= 60
        return mask

    # ---- 진행 ----
    def _apply_captures(self, b, placed, me):
        """placed 착수 후 상대 그룹 따내기. (b는 진실 보드, 제자리 수정) 잡힌 목록 반환"""
        captured = []
        for nb in NEIGHBORS[placed]:
            if b[nb] == opp(me):
                stones, libs = group_of(b, nb)
                if libs == 0:
                    for s in stones:
                        b[s] = EMPTY
                    captured += stones
        return captured

    def _post_capture_bookkeeping(self, me, captured):
        cap = set(captured)
        o = opp(me)
        self.hidden[o] = [i for i in self.hidden[o] if i not in cap]
        self.bases[o] = [i for i in self.bases[o] if i not in cap]
        # 포획에 가담한 내 히든은 공개 (목록에서 제거 = 이제 보이는 돌)
        adj = set()
        for c in captured:
            adj.update(NEIGHBORS[c])
        self.hidden[me] = [i for i in self.hidden[me] if i not in adj]

    def play(self, action):
        """진실 규칙으로 행동 적용. 성공 True / 불법 False(상태 불변)"""
        if self.over:
            return False
        me = self.turn
        self.acts += 1
        if self.acts >= MAX_ACTIONS:
            self.over = True

        if action == A_PASS:
            self.passes += 1
            self.turn = opp(me)
            self.moves += 1
            if self.passes >= 2 or self.moves >= MAX_MOVES:
                self.over = True
            return True

        if action >= A_SCAN:                       # 스캔 (차례 유지)
            center = action - A_SCAN
            r0, c0 = center // N, center % N
            area = set()
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    r, c = r0 + dr, c0 + dc
                    if 0 <= r < N and 0 <= c < N:
                        area.add(r * N + c)
            o = opp(me)
            self.hidden[o] = [i for i in self.hidden[o] if i not in area]   # 발견 = 공개
            self.scan_given[me] += SCAN_COST
            self.scan_cnt[me] += 1
            return True

        as_hidden = action >= A_HID
        idx = action - A_HID if as_hidden else action

        # 상대 미공개 히든을 찍음 → 발각! 착수 무효, 차례 유지 → 다시 착수 (원작 규칙)
        # turn/moves 그대로 (acts는 상단에서 증가 → MAX_ACTIONS로 폭주 방지)
        if idx in self.hidden[opp(me)]:
            self.hidden[opp(me)].remove(idx)
            return True

        if as_hidden and self.hidden_used[me] >= HIDDEN_MAX:
            return False
        if self.board[idx] != EMPTY:
            return False

        b = self.board[:]
        b[idx] = me
        captured = self._apply_captures(b, idx, me)
        _, libs = group_of(b, idx)
        if libs == 0:
            return False                            # 자살수
        key = tuple(b)
        if key in self.history:
            # 진실 기준 패. 보이는 판에선 합법이었다면 → 상대 히든 전부 공개 + 차례 소모 (간이 규칙)
            if self.hidden[opp(me)]:
                self.hidden[opp(me)] = []
                self.turn = opp(me)
                self.passes = 0
                self.moves += 1
                return True
            return False

        self.board = b
        self.history.add(key)
        self._post_capture_bookkeeping(me, captured)
        if as_hidden:
            self.hidden_used[me] += 1
            if not captured:                        # 따냈으면 즉시 공개(목록에 안 넣음)
                self.hidden[me].append(idx)
        self.turn = opp(me)
        self.passes = 0
        self.moves += 1
        if self.moves >= MAX_MOVES:
            self.over = True
        return True

    # ---- 점수 ----
    def score_diff(self):
        """흑 기준 최종 점수차 (종국엔 히든도 모두 돌로 계산됨 — board가 진실이므로 자동)"""
        b = self.board
        minus_set = {i for i, v in self.spots if v < 0}
        terr = {BLACK: 0, WHITE: 0}
        stones = {BLACK: 0, WHITE: 0}
        seen = [False] * NN
        for i in range(NN):
            if b[i] != EMPTY:
                stones[b[i]] += 1
            elif not seen[i]:
                region = [i]; seen[i] = True
                touch = set()
                j = 0
                while j < len(region):
                    for nb in NEIGHBORS[region[j]]:
                        if b[nb] != EMPTY:
                            touch.add(b[nb])
                        elif not seen[nb]:
                            seen[nb] = True
                            region.append(nb)
                    j += 1
                if len(touch) == 1:
                    o = touch.pop()
                    terr[o] += len(region) - sum(1 for c in region if c in minus_set)
        pts = {BLACK: 0, WHITE: 0}
        for i, v in self.spots:
            if b[i] != EMPTY:
                pts[b[i]] += v
        base_n = {c: sum(1 for i in self.bases[c] if b[i] == c) for c in (BLACK, WHITE)}
        sB = (stones[BLACK] + terr[BLACK] + base_n[BLACK] * BASE_VALUE + pts[BLACK]
              + self.scan_given[WHITE])
        sW = (stones[WHITE] + terr[WHITE] + base_n[WHITE] * BASE_VALUE + pts[WHITE]
              + self.scan_given[BLACK] + self.komi)
        return sB - sW

    # ---- 신경망 입력 (현재 둘 사람 시점, 11평면) ----
    def planes(self):
        me = self.turn
        o = opp(me)
        vb = self.view_board(me)
        p = torch.zeros(IN_PLANES, N, N)
        for i in range(NN):
            r, c = divmod(i, N)
            v = vb[i]
            if v == me:
                p[0, r, c] = 1.0
            elif v == o:
                p[1, r, c] = 1.0
            if self.spot_map[i]:
                p[2, r, c] = self.spot_map[i] / 5.0
        for i in self.bases[me]:
            p[3, i // N, i % N] = 1.0
        for i in self.bases[o]:
            p[4, i // N, i % N] = 1.0
        p[5].fill_(1.0)
        p[6].fill_((self.komi if me == WHITE else -self.komi) / 10.0)
        for i in self.hidden[me]:                   # 내 미공개 히든 (나는 안다)
            p[7, i // N, i % N] = 1.0
        p[8].fill_(float(HIDDEN_MAX - self.hidden_used[me]))          # 내 히든 잔여
        p[9].fill_(float(len(self.hidden[o])))                        # 상대 미공개 히든 개수
        p[10].fill_((self.scan_given[o] - self.scan_given[me]) / 10.0)  # 스캔 수지 (내가 받을 점수 −)
        return p


# ================= 신경망 =================
class ResBlock(nn.Module):
    def __init__(self, ch):
        super().__init__()
        self.c1 = nn.Conv2d(ch, ch, 3, padding=1)
        self.b1 = nn.BatchNorm2d(ch)
        self.c2 = nn.Conv2d(ch, ch, 3, padding=1)
        self.b2 = nn.BatchNorm2d(ch)

    def forward(self, x):
        y = F.relu(self.b1(self.c1(x)))
        y = self.b2(self.c2(y))
        return F.relu(x + y)


class BatooNet(nn.Module):
    def __init__(self, ch=64, blocks=6):
        super().__init__()
        self.stem = nn.Sequential(nn.Conv2d(IN_PLANES, ch, 3, padding=1), nn.BatchNorm2d(ch), nn.ReLU())
        self.res = nn.Sequential(*[ResBlock(ch) for _ in range(blocks)])
        self.phead = nn.Sequential(nn.Conv2d(ch, 2, 1), nn.BatchNorm2d(2), nn.ReLU(), nn.Flatten(),
                                   nn.Linear(2 * NN, N_ACTIONS))
        self.vhead = nn.Sequential(nn.Conv2d(ch, 1, 1), nn.BatchNorm2d(1), nn.ReLU(), nn.Flatten(),
                                   nn.Linear(NN, 64), nn.ReLU(), nn.Linear(64, 1), nn.Tanh())

    def forward(self, x):
        h = self.res(self.stem(x))
        return self.phead(h), self.vhead(h).squeeze(-1)


def transplant(old_sd):
    """기존 7평면/122행동 체크포인트 → 새 11평면/364행동 신경망으로 이식"""
    net = BatooNet()
    sd = net.state_dict()
    for k, v in old_sd.items():
        if k == "stem.0.weight":
            sd[k][:, :7] = v                        # 기존 7평면 복사, 새 4평면은 초기값
        elif k == "phead.4.weight":
            sd[k][A_OPEN:A_OPEN + NN] = v[:NN]      # 일반착수 복사
            sd[k][A_HID:A_HID + NN] = v[:NN]        # 히든착수 = 일반착수에서 출발
            sd[k][A_PASS] = v[NN]                   # 쉼 복사 (스캔은 초기값)
        elif k == "phead.4.bias":
            sd[k][A_OPEN:A_OPEN + NN] = v[:NN]
            sd[k][A_HID:A_HID + NN] = v[:NN] - 2.0  # 처음엔 잘 안 숨기게
            sd[k][A_SCAN:A_SCAN + NN] = -3.0        # 처음엔 잘 안 찍게
            sd[k][A_PASS] = v[NN]
        elif k in sd and sd[k].shape == v.shape:
            sd[k] = v
    net.load_state_dict(sd)
    return net


# ================= MCTS (determinization 포함) =================
class TNode:
    __slots__ = ("P", "Nv", "W", "children", "legal", "turn")

    def __init__(self, priors, legal, turn):
        self.P = priors
        self.Nv = {}
        self.W = {}
        self.children = {}
        self.legal = legal
        self.turn = turn


C_PUCT = 1.5
DIR_ALPHA = 0.25
DIR_EPS = 0.25


@torch.no_grad()
def net_eval(net, game, device):
    x = game.planes().unsqueeze(0).to(device)
    logits, value = net(x)
    return logits[0].cpu(), float(value[0])


def make_node(logits, game, add_noise=False):
    mask = game.legal_mask()
    legal = [a for a in range(N_ACTIONS) if mask[a]]
    pri = torch.softmax(logits[legal], dim=0).tolist()
    priors = dict(zip(legal, pri))
    if add_noise and len(legal) > 1:
        noise = [random.gammavariate(DIR_ALPHA, 1.0) for _ in legal]
        s = sum(noise)
        for a, n in zip(legal, noise):
            priors[a] = (1 - DIR_EPS) * priors[a] + DIR_EPS * (n / s)
    return TNode(priors, legal, game.turn)


def select_action(node, banned):
    total = sum(node.Nv.values()) + 1
    sq = math.sqrt(total)
    best, best_u = None, -1e9
    for a in node.legal:
        if a in banned:
            continue
        n = node.Nv.get(a, 0)
        q = (node.W.get(a, 0.0) / n) if n else 0.0
        u = q + C_PUCT * node.P[a] * sq / (1 + n)
        if u > best_u:
            best_u, best = u, a
    return best


def clone_game(g):
    c = Game.__new__(Game)
    c.board = g.board[:]
    c.turn = g.turn
    c.passes = g.passes
    c.history = set(g.history)
    c.spots = g.spots
    c.spot_map = g.spot_map
    c.bases = {BLACK: g.bases[BLACK][:], WHITE: g.bases[WHITE][:]}
    c.moves = g.moves
    c.acts = g.acts
    c.over = g.over
    c.komi = g.komi
    c.hidden = {BLACK: g.hidden[BLACK][:], WHITE: g.hidden[WHITE][:]}
    c.hidden_used = dict(g.hidden_used)
    c.scan_given = dict(g.scan_given)
    c.scan_cnt = dict(g.scan_cnt)
    return c


def determinize(g, mover):
    """mover의 지식 상태로 변환: 상대의 미공개 히든을 '있을 법한 자리'로 재배치"""
    o = opp(mover)
    k = len(g.hidden[o])
    if k == 0:
        return
    for i in g.hidden[o]:
        g.board[i] = EMPTY
    vb = g.view_board(mover)                        # 이제 진짜 view와 동일
    empties = [i for i in range(NN) if vb[i] == EMPTY]
    spots = random.sample(empties, min(k, len(empties)))
    g.hidden[o] = spots
    for i in spots:
        g.board[i] = o
    g.history = {tuple(g.board)}


def mcts_policy(net, game, sims, device, temperature):
    logits, _ = net_eval(net, game, device)
    root = make_node(logits, game, add_noise=True)
    mover = game.turn

    for _ in range(sims):
        g = clone_game(game)
        determinize(g, mover)
        node = root
        path = []
        banned = set()
        while True:
            a = select_action(node, banned)
            if a is None:
                break
            ok = g.play(a)
            if not ok:
                banned.add(a)                        # 이 시뮬레이션에서만 제외 (결정화 의존적일 수 있음)
                continue
            path.append((node, a))
            banned = set()
            if g.over or a not in node.children:
                break
            node = node.children[a]

        if g.over:
            d = g.score_diff()
            v_black = 1.0 if d > 0 else -1.0         # 동점=백 승
        else:
            lg, vv = net_eval(net, g, device)
            leaf = make_node(lg, g)
            if path:
                path[-1][0].children[path[-1][1]] = leaf
            v_black = vv if g.turn == BLACK else -vv

        for parent, a in path:                       # 각 노드의 '둘 사람' 시점으로 보상
            r = v_black if parent.turn == BLACK else -v_black
            parent.Nv[a] = parent.Nv.get(a, 0) + 1
            parent.W[a] = parent.W.get(a, 0.0) + r

    visits = torch.zeros(N_ACTIONS)
    for a, n in root.Nv.items():
        visits[a] = n
    if visits.sum() == 0:
        visits[A_PASS] = 1.0
    if temperature > 0:
        probs = visits ** (1.0 / temperature)
        probs /= probs.sum()
        action = int(torch.multinomial(probs, 1))
    else:
        action = int(visits.argmax())
    return visits / visits.sum(), action


# ================= 자가대국 =================
def choose_komi(net, game, device):
    """신경망이 '이 배치의 공정 덤'을 직접 계산해 베팅한다.
    → 매 판 덤이 판세에 맞아 흑/백 승률이 50:50 근처로 균형 잡히고,
      그 결과 지는 쪽(백) 국면도 '이길 수 있는 판'이 되어 정책이 안 무너진다.
      (덤 무작위 고정이 흑/백 불균형→지는쪽 정책 붕괴를 유발한 것을 교정)"""
    saved = game.komi
    fair = 0
    with torch.no_grad():
        for k in range(8):
            game.komi = k
            _, v = net_eval(net, game, device)   # 흑(선수) 관점 가치
            if v > 0.0:                          # 덤 k를 내도 흑이 아직 유리 → 공정가는 더 위
                fair = k
            else:
                break
    game.komi = saved
    # 15%만 완전 무작위 탐험(덤 분포 다양성 확보), 나머지는 공정가 ±1
    if random.random() < 0.15:
        return random.randint(0, 7)
    return max(0, min(7, fair + random.choice([-1, 0, 0, 1])))


def selfplay_game(net, sims, device):
    game = Game()
    game.komi = choose_komi(net, game, device)
    records = []
    stats = {"hidden": 0, "scan": 0}
    while not game.over:
        temp = 1.0 if game.moves < 14 else 0.15
        policy, action = mcts_policy(net, game, sims, device, temp)
        records.append((game.planes(), policy, game.turn))
        if A_HID <= action < A_SCAN:
            stats["hidden"] += 1
        elif A_SCAN <= action < A_PASS:
            stats["scan"] += 1
        if not game.play(action):
            game.play(A_PASS)                        # 안전망 (진실상 불법 — 드묾)
    d = game.score_diff()
    z_black = 1.0 if d > 0 else -1.0
    data = []
    for planes, policy, turn in records:
        z = z_black if turn == BLACK else -z_black
        data.append((planes, policy, z))
    return data, d, game.moves, stats


TRANSFORMS = list(range(8))


def transform_sample(planes, policy, t):
    p = planes
    blocks = [policy[A_OPEN:A_OPEN + NN].view(N, N),
              policy[A_HID:A_HID + NN].view(N, N),
              policy[A_SCAN:A_SCAN + NN].view(N, N)]
    if t & 1:
        p = torch.flip(p, [2]); blocks = [torch.flip(b, [1]) for b in blocks]
    if t & 2:
        p = torch.flip(p, [1]); blocks = [torch.flip(b, [0]) for b in blocks]
    if t & 4:
        p = p.transpose(1, 2); blocks = [b.t() for b in blocks]
    pol = torch.cat([b.reshape(-1) for b in blocks] + [policy[A_PASS:]])
    return p.contiguous(), pol.contiguous()


# ================= 게이트 =================
GATE_EVERY = 5
GATE_GAMES = 10
GATE_SIMS = 48
GATE_KEEP = 0.45


def quick_match(net_a, net_b, games, sims, device):
    wins = 0
    for i in range(games):
        g = Game()
        a_black = (i % 2 == 0)
        guard = 0
        while not g.over and guard < MAX_ACTIONS:
            guard += 1
            net = net_a if (g.turn == BLACK) == a_black else net_b
            _, act = mcts_policy(net, g, sims, device, temperature=0.25 if g.moves < 8 else 0)
            if not g.play(act):
                g.play(A_PASS)
        d = g.score_diff()
        a_diff = d if a_black else -d
        if a_diff == 0:
            a_diff = -0.5 if a_black else 0.5
        if a_diff > 0:
            wins += 1
    return wins


# ================= 병렬 자가대국 =================
def selfplay_worker(job):
    ckpt_path, sims, n_games, seed = job
    torch.set_num_threads(2)
    random.seed(seed)
    torch.manual_seed(seed)
    net = BatooNet()
    if os.path.exists(ckpt_path):
        try:
            net.load_state_dict(torch.load(ckpt_path, map_location="cpu")["net"])
        except Exception:
            pass
    net.eval()
    return [selfplay_game(net, sims, "cpu") for _ in range(n_games)]


# ================= 메인 =================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--sims", type=int, default=96)
    ap.add_argument("--games-per-iter", type=int, default=24)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--buffer", type=int, default=120000)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--out", default="batoo_net_full.pt")
    ap.add_argument("--transplant", default="", help="기존 착수전용 체크포인트에서 몸통 이식 (out이 없을 때만)")
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()

    if args.smoke:
        args.hours = 0.02
        args.sims = 12
        args.games_per_iter = 2

    device = ("cuda" if torch.cuda.is_available()
              else "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
              else "cpu")
    print(f"장치: {device} | 시뮬레이션/수: {args.sims} | 행동 {N_ACTIONS} | 출력: {args.out}")

    net = BatooNet().to(device)
    start_iter = 0
    if os.path.exists(args.out):
        try:
            ck = torch.load(args.out, map_location=device)
            net.load_state_dict(ck["net"])
            start_iter = ck.get("iter", 0)
            print(f"풀게임 체크포인트 이어서: iter {start_iter}")
        except Exception as e:
            print(f"⚠️ 체크포인트 로드 실패 — 새로 시작: {e}")
    elif args.transplant and os.path.exists(args.transplant):
        old = torch.load(args.transplant, map_location="cpu")
        net = transplant(old["net"]).to(device)
        print(f"🔁 이식 완료: {args.transplant} (iter {old.get('iter', '?')})의 몸통 → 풀게임 신경망")
    opt = torch.optim.Adam(net.parameters(), lr=args.lr, weight_decay=1e-4)
    if os.path.exists(args.out):
        try:
            opt.load_state_dict(torch.load(args.out, map_location=device)["opt"])
        except Exception:
            pass

    pool = None
    if args.workers > 0:
        pool = mp.get_context("spawn").Pool(args.workers)

    buffer = []
    deadline = time.time() + args.hours * 3600
    it = start_iter
    total_games = 0
    best_sd = {k: v.clone() for k, v in net.state_dict().items()}

    while time.time() < deadline:
        it += 1
        net.eval()
        t0 = time.time()
        new_samples = 0
        b_wins = w_wins = draws = 0
        hid_n = scan_n = 0
        if args.workers > 0:
            torch.save({"net": net.state_dict(), "opt": opt.state_dict(), "iter": it, "full": True}, args.out)
            per = max(1, args.games_per_iter // args.workers)
            jobs = [(args.out, args.sims, per, random.randrange(1 << 30)) for _ in range(args.workers)]
            results = []
            for chunk in pool.imap_unordered(selfplay_worker, jobs):
                results.extend(chunk)
        else:
            results = [selfplay_game(net, args.sims, device) for _ in range(args.games_per_iter)]
        for data, d, moves, stats in results:
            total_games += 1
            if d > 0:
                b_wins += 1
            elif d < 0:
                w_wins += 1
            else:
                draws += 1
            hid_n += stats["hidden"]
            scan_n += stats["scan"]
            for planes, policy, z in data:
                t = random.choice(TRANSFORMS)
                p2, pol2 = transform_sample(planes, policy, t)
                buffer.append((p2, pol2, z))
                new_samples += 1
        if len(buffer) > args.buffer:
            buffer = buffer[-args.buffer:]
        sp_time = time.time() - t0

        net.train()
        steps = max(8, new_samples // args.batch)
        ploss = vloss = 0.0
        for _ in range(steps):
            batch = random.sample(buffer, min(args.batch, len(buffer)))
            x = torch.stack([b[0] for b in batch]).to(device)
            pt = torch.stack([b[1] for b in batch]).to(device)
            zt = torch.tensor([b[2] for b in batch], dtype=torch.float32).to(device)
            logits, v = net(x)
            lp = -(pt * F.log_softmax(logits, dim=1)).sum(1).mean()
            lv = F.mse_loss(v, zt)
            loss = lp + lv
            opt.zero_grad()
            loss.backward()
            opt.step()
            ploss += float(lp.detach())
            vloss += float(lv.detach())

        if it % GATE_EVERY == 0:
            net.eval()
            best_net = BatooNet().to(device)
            best_net.load_state_dict(best_sd)
            best_net.eval()
            w = quick_match(net, best_net, GATE_GAMES, GATE_SIMS, device)
            if w / GATE_GAMES >= GATE_KEEP:
                best_sd = {k: v.clone() for k, v in net.state_dict().items()}
                print(f"  [게이트] 신모델 {w}/{GATE_GAMES}승 → 채택", flush=True)
            else:
                net.load_state_dict(best_sd)
                opt = torch.optim.Adam(net.parameters(), lr=args.lr, weight_decay=1e-4)
                print(f"  [게이트] 신모델 {w}/{GATE_GAMES}승 → ⛔ 롤백", flush=True)

        torch.save({"net": net.state_dict(), "opt": opt.state_dict(), "iter": it, "full": True}, args.out)
        remain = (deadline - time.time()) / 3600
        n_games_done = b_wins + w_wins + draws
        print(f"[iter {it}] 대국 {n_games_done}판({sp_time:.0f}s, 흑 {b_wins}·백 {w_wins}·무 {draws}, "
              f"히든 {hid_n}·스캔 {scan_n}) | 버퍼 {len(buffer)} "
              f"| 정책손실 {ploss/steps:.3f} 가치손실 {vloss/steps:.3f} "
              f"| 누적 {total_games}판 | 남은 {remain:.1f}h", flush=True)

    if pool is not None:
        pool.close()
        pool.join()
    print(f"완료 — {args.out} 저장됨.")


if __name__ == "__main__":
    main()
