#!/usr/bin/env python3
"""바투 미니 알파제로 학습 스크립트 — 이 파일 하나로 완결 (게임 규칙 내장).

■ 리눅스 다코어 CPU (쓰레드리퍼 등) — 권장 실행법:
    pip install torch --index-url https://download.pytorch.org/whl/cpu   # CPU 전용(다운로드 작음)
    python3 train_batoo.py --smoke --workers 2       # 먼저 몇 분짜리 동작 확인!
    python3 train_batoo.py --hours 24 --workers 24 --games-per-iter 48
    → --workers = 병렬 자가대국 프로세스 수 (물리코어의 60~80% 권장: 3970X는 24, 3990X는 40)

■ GPU (코랩 T4, RTX 3090):
    pip install torch
    python train_batoo.py --hours 8                  # workers 없이 (GPU는 단일 프로세스)

→ batoo_net.pt 가 매 iter 저장됨. 이 파일을 맥의 ~/Desktop/야구/ 로 복사하면 게임에 반영.
이어서 학습: batoo_net.pt 가 폴더에 있으면 자동으로 이어서 학습한다.
⚠️ 병렬(--workers) 경로는 맥에서 문법 검증만 됨 — 실행 전 --smoke --workers 2 필수.

구조 (알파제로 방식):
    작은 ResNet(정책+가치) → 신경망이 이끄는 MCTS로 자가대국 → 그 기보로 신경망 훈련 → 반복
    입력 평면: [내 돌, 상대 돌, 포인트 지점(±5 정규화), 내 베이스, 상대 베이스, 상수1]
    학습 대상은 '보이는 판' 게임 (히든은 서버의 기존 로직이 계속 담당)
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

# ================= 게임 규칙 (server.py와 동일한 바투 코어) =================
N = 11
NN = N * N
EMPTY, BLACK, WHITE = 0, 1, 2
BASE_VALUE = 5
PLUS_VALUES = [5, 3, 2]
MINUS_VALUES = [-5, -3, -2]
FIELDS = [   # 필드 3종의 배치 패턴 (server.py의 FIELDS 통합)
    {"plus": [24, 96, 60], "minus": [30, 90, 63]},
    {"plus": [30, 90, 60], "minus": [24, 96, 57]},
    {"plus": [27, 93, 60], "minus": [57, 63, 96]},
    {"plus": [60, 27, 93], "minus": [24, 30, 96]},
    {"plus": [60, 57, 63], "minus": [24, 90, 96]},
    {"plus": [36, 84, 60], "minus": [26, 94, 38]},
    {"plus": [46, 74, 16], "minus": [104, 48, 72]},
    {"plus": [25, 95, 59], "minus": [61, 29, 91]},
]
PASS = NN            # 행동 121 = 한 수 쉼
MAX_MOVES = 140      # 대국 길이 상한

NEIGHBORS = []
for _i in range(NN):
    _x, _y = _i % N, _i // N
    _r = []
    if _x > 0: _r.append(_i - 1)
    if _x < N - 1: _r.append(_i + 1)
    if _y > 0: _r.append(_i - N)
    if _y < N - 1: _r.append(_i + N)
    NEIGHBORS.append(_r)


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
    """학습용 바투 (보이는 판): 중국룰 + 포인트 + 베이스, 히든 없음"""
    __slots__ = ("board", "turn", "passes", "history", "spots", "spot_map",
                 "bases", "moves", "over", "komi")

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
        # 무작위 베이스 3+3 (겹침 없음, 1선 제외)
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
        self.over = False
        # 턴베팅(덤): 백이 받는 점수 — 매판 무작위 정수 (원작: 정수 베팅, 동점=백 승)
        self.komi = random.randint(0, 7)

    def legal_mask(self):
        """빠른 합법수 마스크 (빈 자리 & 자기 눈 아님). 자살/패는 착수 시점에 걸러냄"""
        mask = [False] * (NN + 1)
        b, t = self.board, self.turn
        any_move = False
        for i in range(NN):
            if b[i] != EMPTY:
                continue
            nbs = NEIGHBORS[i]
            if all(b[nb] == t for nb in nbs):
                continue
            mask[i] = True
            any_move = True
        # 한 수 쉼은 둘 곳이 없거나 종반(60수 이후)에만 허용
        # — 초반 쌍쉼으로 게임이 붕괴하는 것을 방지 (동점=백승 규칙과 조합 시 필수)
        mask[PASS] = (not any_move) or self.moves >= 60
        return mask

    def play(self, action):
        """착수. 성공 True / 불법(자살·패) False — 실패 시 상태 불변"""
        if action == PASS:
            self.passes += 1
            self.turn = WHITE if self.turn == BLACK else BLACK
            self.moves += 1
            if self.passes >= 2 or self.moves >= MAX_MOVES:
                self.over = True
            return True
        b = self.board[:]
        t = self.turn
        op = WHITE if t == BLACK else BLACK
        if b[action] != EMPTY:
            return False
        b[action] = t
        for nb in NEIGHBORS[action]:
            if b[nb] == op:
                stones, libs = group_of(b, nb)
                if libs == 0:
                    for s in stones:
                        b[s] = EMPTY
        _, libs = group_of(b, action)
        if libs == 0:
            return False
        key = tuple(b)
        if key in self.history:
            return False
        self.board = b
        self.history.add(key)
        self.turn = op
        self.passes = 0
        self.moves += 1
        if self.moves >= MAX_MOVES:
            self.over = True
        return True

    def score_diff(self):
        """흑 기준 바투 점수차 (돌+집+베이스×5+포인트, 빈 −점 집 제외)"""
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
        sB = stones[BLACK] + terr[BLACK] + base_n[BLACK] * BASE_VALUE + pts[BLACK]
        sW = stones[WHITE] + terr[WHITE] + base_n[WHITE] * BASE_VALUE + pts[WHITE] + self.komi
        return sB - sW

    def planes(self):
        """신경망 입력 7평면 (현재 차례 시점)"""
        me = self.turn
        op = WHITE if me == BLACK else BLACK
        p = torch.zeros(7, N, N)
        for i in range(NN):
            r, c = divmod(i, N)
            v = self.board[i]
            if v == me:
                p[0, r, c] = 1.0
            elif v == op:
                p[1, r, c] = 1.0
            if self.spot_map[i]:
                p[2, r, c] = self.spot_map[i] / 5.0
        for i in self.bases[me]:
            p[3, i // N, i % N] = 1.0
        for i in self.bases[op]:
            p[4, i // N, i % N] = 1.0
        p[5].fill_(1.0)
        # 덤 평면: 내가 흑이면 −komi(부담), 백이면 +komi(보너스) — /10 정규화
        p[6].fill_((self.komi if me == WHITE else -self.komi) / 10.0)
        return p


# ================= 신경망 (작은 ResNet, 정책+가치) =================
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
        self.stem = nn.Sequential(nn.Conv2d(7, ch, 3, padding=1), nn.BatchNorm2d(ch), nn.ReLU())
        self.res = nn.Sequential(*[ResBlock(ch) for _ in range(blocks)])
        self.phead = nn.Sequential(nn.Conv2d(ch, 2, 1), nn.BatchNorm2d(2), nn.ReLU(), nn.Flatten(),
                                   nn.Linear(2 * NN, NN + 1))
        self.vhead = nn.Sequential(nn.Conv2d(ch, 1, 1), nn.BatchNorm2d(1), nn.ReLU(), nn.Flatten(),
                                   nn.Linear(NN, 64), nn.ReLU(), nn.Linear(64, 1), nn.Tanh())

    def forward(self, x):
        h = self.res(self.stem(x))
        return self.phead(h), self.vhead(h).squeeze(-1)


# ================= 신경망 MCTS (PUCT) =================
class TNode:
    __slots__ = ("P", "Nv", "W", "children", "legal")

    def __init__(self, priors, legal):
        self.P = priors          # dict action -> prior
        self.Nv = {}             # action -> 방문수
        self.W = {}              # action -> 누적 가치
        self.children = {}       # action -> TNode
        self.legal = legal


C_PUCT = 1.5
DIR_ALPHA = 0.3
DIR_EPS = 0.25


@torch.no_grad()
def evaluate_batch(net, games, device):
    x = torch.stack([g.planes() for g in games]).to(device)
    logits, values = net(x)
    return logits.cpu(), values.cpu()


def make_node(logits, game, add_noise=False):
    mask = game.legal_mask()
    legal = [a for a in range(NN + 1) if mask[a]]
    lg = logits.clone()
    pri = torch.softmax(lg[legal], dim=0).tolist()
    priors = dict(zip(legal, pri))
    if add_noise and len(legal) > 1:
        noise = [random.gammavariate(DIR_ALPHA, 1.0) for _ in legal]
        s = sum(noise)
        for a, n in zip(legal, noise):
            priors[a] = (1 - DIR_EPS) * priors[a] + DIR_EPS * (n / s)
    return TNode(priors, legal)


def select_action(node):
    total = sum(node.Nv.values()) + 1
    best, best_u = None, -1e9
    sq = math.sqrt(total)
    for a in node.legal:
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
    c.bases = g.bases
    c.moves = g.moves
    c.over = g.over
    c.komi = g.komi
    return c


def mcts_policy(net, game, sims, device, temperature):
    """한 수를 위한 MCTS. (방문수 기반 정책 분포, 선택 행동) 반환"""
    logits, _ = evaluate_batch(net, [game], device)
    root = make_node(logits[0], game, add_noise=True)

    for _ in range(sims):
        g = clone_game(game)
        node = root
        path = []
        # 선택
        while True:
            a = select_action(node)
            ok = g.play(a)
            if not ok:                       # 자살/패 — 이 행동을 불법 처리하고 재선택
                node.legal.remove(a)
                node.P.pop(a, None)
                if not node.legal:
                    break
                continue
            path.append((node, a))
            if g.over or a not in node.children:
                break
            node = node.children[a]
        # 평가 + 확장
        if g.over:
            d = g.score_diff()
            v = 1.0 if d > 0 else -1.0                        # 흑 기준 (동점=백 승)
            v = v if g.turn == BLACK else -v                  # 차례 기준으로 변환
        else:
            lg, vv = evaluate_batch(net, [g], device)
            leaf = make_node(lg[0], g)
            if path:
                path[-1][0].children[path[-1][1]] = leaf
            v = float(vv[0])                                  # g.turn(둘 사람) 기준
        # 역전파 — v는 '그 노드에서 둘 사람' 기준이므로 한 수 거슬러갈 때마다 부호 반전
        for parent, a in reversed(path):
            v = -v
            parent.Nv[a] = parent.Nv.get(a, 0) + 1
            parent.W[a] = parent.W.get(a, 0.0) + v

    visits = torch.zeros(NN + 1)
    for a, n in root.Nv.items():
        visits[a] = n
    if visits.sum() == 0:
        visits[PASS] = 1.0
    if temperature > 0:
        probs = visits ** (1.0 / temperature)
        probs /= probs.sum()
        action = int(torch.multinomial(probs, 1))
    else:
        action = int(visits.argmax())
    policy = visits / visits.sum()
    return policy, action


# ================= 자가대국 → 학습 데이터 =================
def selfplay_game(net, sims, device):
    game = Game()
    records = []      # (planes, policy, turn)
    while not game.over:
        temp = 1.0 if game.moves < 14 else 0.15
        policy, action = mcts_policy(net, game, sims, device, temp)
        records.append((game.planes(), policy, game.turn))
        game.play(action)
    d = game.score_diff()
    z_black = 1.0 if d > 0 else -1.0                          # 동점=백 승
    data = []
    for planes, policy, turn in records:
        z = z_black if turn == BLACK else -z_black
        data.append((planes, policy, z))
    return data, d, game.moves


TRANSFORMS = list(range(8))      # 8방향 대칭 증강


def transform_sample(planes, policy, t):
    p = planes
    pol_board = policy[:NN].view(N, N)
    if t & 1:
        p = torch.flip(p, [2]); pol_board = torch.flip(pol_board, [1])
    if t & 2:
        p = torch.flip(p, [1]); pol_board = torch.flip(pol_board, [0])
    if t & 4:
        p = p.transpose(1, 2); pol_board = pol_board.t()
    pol = torch.cat([pol_board.reshape(-1), policy[NN:]])
    return p.contiguous(), pol.contiguous()


# ================= 병렬 자가대국 (다코어 CPU용) =================
def selfplay_worker(job):
    """워커 프로세스: 체크포인트를 읽어 n판 자가대국. (--workers N 일 때 사용)"""
    ckpt_path, sims, n_games, seed = job
    torch.set_num_threads(2)          # 워커당 스레드 제한 (프로세스 병렬이 주력)
    random.seed(seed)
    torch.manual_seed(seed)
    net = BatooNet()
    if os.path.exists(ckpt_path):
        try:
            net.load_state_dict(torch.load(ckpt_path, map_location="cpu")["net"])
        except Exception:
            pass                       # 첫 iter 등 — 초기 가중치로 진행
    net.eval()
    out = []
    for _ in range(n_games):
        out.append(selfplay_game(net, sims, "cpu"))
    return out


# ================= 게이트 (자기 과신 소용돌이 방지) =================
GATE_EVERY = 5     # 몇 iter마다 검증하나
GATE_GAMES = 10    # 스파링 판수
GATE_SIMS = 48
GATE_KEEP = 0.45   # 최고기록 상대 승률이 이 밑이면 롤백


def quick_match(net_a, net_b, games, sims, device):
    """net_a vs net_b 스파링. a의 승수 반환 (동점=백 승)"""
    wins = 0
    for i in range(games):
        g = Game()
        a_black = (i % 2 == 0)
        guard = 0
        while not g.over and guard < 200:
            guard += 1
            net = net_a if (g.turn == BLACK) == a_black else net_b
            _, act = mcts_policy(net, g, sims, device, temperature=0.25 if g.moves < 8 else 0)
            if not g.play(act):
                mask = g.legal_mask()
                legal = [x for x in range(NN + 1) if mask[x]]
                g.play(random.choice(legal))
        d = g.score_diff()
        a_diff = d if a_black else -d
        if a_diff == 0:
            a_diff = -0.5 if a_black else 0.5
        if a_diff > 0:
            wins += 1
    return wins


# ================= 메인 루프 =================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=8.0, help="총 학습 시간")
    ap.add_argument("--sims", type=int, default=96, help="수당 MCTS 시뮬레이션")
    ap.add_argument("--games-per-iter", type=int, default=24)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--buffer", type=int, default=120000)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--out", default="batoo_net.pt")
    ap.add_argument("--workers", type=int, default=0,
                    help="병렬 자가대국 프로세스 수 (다코어 CPU용, 예: 쓰레드리퍼는 16~32. 0=단일)")
    ap.add_argument("--smoke", action="store_true", help="연기 테스트 (몇 분짜리 1바퀴)")
    args = ap.parse_args()

    if args.smoke:
        args.hours = 0.02
        args.sims = 12
        args.games_per_iter = 2

    device = ("cuda" if torch.cuda.is_available()
              else "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
              else "cpu")
    print(f"장치: {device} | 시뮬레이션/수: {args.sims} | 출력: {args.out}")

    net = BatooNet().to(device)
    opt = torch.optim.Adam(net.parameters(), lr=args.lr, weight_decay=1e-4)
    start_iter = 0
    if os.path.exists(args.out):
        try:
            ck = torch.load(args.out, map_location=device)
            net.load_state_dict(ck["net"])
            opt.load_state_dict(ck["opt"])
            start_iter = ck.get("iter", 0)
            print(f"체크포인트 이어서: iter {start_iter}")
        except Exception as e:
            print(f"⚠️ 기존 체크포인트가 새 구조와 호환되지 않아 새로 시작합니다: {e}")

    # 병렬 풀은 spawn 방식으로 1회만 생성 (fork는 torch 뮤텍스 상속으로 데드락 위험)
    pool = None
    if args.workers > 0:
        pool = mp.get_context("spawn").Pool(args.workers)

    buffer = []
    deadline = time.time() + args.hours * 3600
    it = start_iter
    total_games = 0
    best_sd = {k: v.clone() for k, v in net.state_dict().items()}   # 최고기록 스냅샷

    while time.time() < deadline:
        it += 1
        # ---- 자가대국 ----
        net.eval()
        t0 = time.time()
        new_samples = 0
        b_wins = w_wins = draws = 0
        if args.workers > 0:
            # 병렬: 워커들이 최신 체크포인트를 읽어 동시에 둔다
            torch.save({"net": net.state_dict(), "opt": opt.state_dict(), "iter": it}, args.out)
            per = max(1, args.games_per_iter // args.workers)
            jobs = [(args.out, args.sims, per, random.randrange(1 << 30)) for _ in range(args.workers)]
            results = []
            for chunk in pool.imap_unordered(selfplay_worker, jobs):
                results.extend(chunk)
        else:
            results = [selfplay_game(net, args.sims, device) for _ in range(args.games_per_iter)]
        for data, d, moves in results:
            total_games += 1
            if d > 0:
                b_wins += 1
            elif d < 0:
                w_wins += 1
            else:
                draws += 1
            for planes, policy, z in data:
                t = random.choice(TRANSFORMS)          # 대칭 증강 1개 샘플
                p2, pol2 = transform_sample(planes, policy, t)
                buffer.append((p2, pol2, z))
                new_samples += 1
        if len(buffer) > args.buffer:
            buffer = buffer[-args.buffer:]
        sp_time = time.time() - t0

        # ---- 훈련 ----
        net.train()
        steps = max(8, new_samples // args.batch)      # 과적합 억제: 스텝 절반
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

        # ---- 게이트: 신모델이 최고기록을 못 이기면 롤백 ----
        if it % GATE_EVERY == 0:
            net.eval()
            best_net = BatooNet().to(device)
            best_net.load_state_dict(best_sd)
            best_net.eval()
            w = quick_match(net, best_net, GATE_GAMES, GATE_SIMS, device)
            if w / GATE_GAMES >= GATE_KEEP:
                best_sd = {k: v.clone() for k, v in net.state_dict().items()}
                print(f"  [게이트] 신모델 {w}/{GATE_GAMES}승 → 채택 (최고기록 갱신)", flush=True)
            else:
                net.load_state_dict(best_sd)
                opt = torch.optim.Adam(net.parameters(), lr=args.lr, weight_decay=1e-4)
                print(f"  [게이트] 신모델 {w}/{GATE_GAMES}승 → ⛔ 롤백 (최고기록 유지)", flush=True)

        torch.save({"net": net.state_dict(), "opt": opt.state_dict(), "iter": it}, args.out)
        remain = (deadline - time.time()) / 3600
        print(f"[iter {it}] 대국 {b_wins + w_wins + draws}판({sp_time:.0f}s, 흑 {b_wins}·백 {w_wins}·무 {draws}) "
              f"| 버퍼 {len(buffer)} | 정책손실 {ploss/steps:.3f} 가치손실 {vloss/steps:.3f} "
              f"| 누적 {total_games}판 | 남은 {remain:.1f}h", flush=True)

    if pool is not None:
        pool.close()
        pool.join()
    print(f"완료 — {args.out} 저장됨. 이 파일을 맥의 게임 폴더로 복사하세요.")


if __name__ == "__main__":
    main()
