#!/usr/bin/env python3
"""게임원 리그 기록 자동 수집기 (파이썬 표준 라이브러리만 사용)

리그 주소 하나만 주면 타자/투수 기록과 경기 결과를 받아와
gameone/gameone_data.json 으로 저장한다. 전력분석 도구(gameone/index.html)를
서버로 열어 두면 새로고침만으로 자동 반영된다.

사용법:
  python3 gameone_sync.py "https://www.gameone.kr/league/?lig_idx=10165"
  python3 gameone_sync.py 10165 --serve          # 받은 뒤 바로 도구 열기 (http://localhost:8899/gameone/)
  python3 gameone_sync.py 10165 --dump html_dump  # 받은 원본 HTML도 저장 (인식이 안 될 때 확인용)
  python3 gameone_sync.py --from-dir html_dump    # 저장해 둔 HTML로 다시 분석 (접속 없이)

예의: 요청 사이 간격(기본 1.5초), 페이지 수 제한. 개인 분석 용도로만 쓰세요.
"""
import argparse, html, json, os, re, ssl, sys, time, urllib.parse, urllib.request
from datetime import datetime
from html.parser import HTMLParser

BASE = "https://www.gameone.kr"
ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DEFAULT = os.path.join(ROOT, "gameone", "gameone_data.json")

# ---------------------------------------------------------------- 접속
def ssl_ctx():
    ctx = ssl.create_default_context()
    # gameone.kr 서버가 오래된 DH 키를 써서 최신 OpenSSL 기본값으로는 접속이 거부됨 → 보안레벨만 1로 (인증서 검증은 그대로)
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    except ssl.SSLError:
        pass
    return ctx

CTX = ssl_ctx()
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

class Fetcher:
    def __init__(self, delay, dump_dir=None, from_dir=None):
        self.delay, self.dump_dir, self.from_dir = delay, dump_dir, from_dir
        self.last = 0.0
        self.n = 0

    @staticmethod
    def fname(url):
        return re.sub(r"[^0-9A-Za-z_.-]+", "_", url.replace(BASE, ""))[:150] + ".html"

    def get(self, url):
        if self.from_dir:
            p = os.path.join(self.from_dir, self.fname(url))
            if not os.path.exists(p):
                return None
            with open(p, encoding="utf-8") as f:
                return f.read()
        wait = self.delay - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
        try:
            with urllib.request.urlopen(req, context=CTX, timeout=25) as r:
                raw = r.read()
                cs = r.headers.get_content_charset()
        except Exception as e:
            print(f"  ! 접속 실패 {url}: {e}", file=sys.stderr)
            return None
        self.n += 1
        text = None
        for enc in [cs, "utf-8", "cp949"]:
            if not enc:
                continue
            try:
                text = raw.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                pass
        if text is None:
            text = raw.decode("utf-8", "replace")
        if self.dump_dir:
            os.makedirs(self.dump_dir, exist_ok=True)
            with open(os.path.join(self.dump_dir, self.fname(url)), "w", encoding="utf-8") as f:
                f.write(text)
        return text

# ---------------------------------------------------------------- HTML 표 파서
class TableParser(HTMLParser):
    """모든 <table>을 [행[셀 텍스트]] 로 뽑고, 링크도 모은다. 중첩 표는 각각 따로."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.links = [], []
        self.stack = []          # 열린 표들: {"rows":[], "row":None, "cell":None}
        self.skip = 0            # script/style 안

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "table":
            self.stack.append({"rows": [], "row": None, "cell": None})
        elif not self.stack:
            return
        elif tag == "tr":
            t = self.stack[-1]
            t["row"] = []
            t["rows"].append(t["row"])
        elif tag in ("td", "th"):
            t = self.stack[-1]
            if t["row"] is None:
                t["row"] = []
                t["rows"].append(t["row"])
            t["cell"] = []
            span = int(a.get("colspan") or 1) if str(a.get("colspan") or "1").isdigit() else 1
            t["row"].append(t["cell"])
            for _ in range(min(span, 20) - 1):   # colspan 만큼 빈칸 채워 열 위치 유지
                t["row"].append([])
        elif tag == "br" and self.stack[-1]["cell"] is not None:
            self.stack[-1]["cell"].append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag == "table" and self.stack:
            t = self.stack.pop()
            rows = [[re.sub(r"\s+", " ", "".join(c)).strip() for c in r] for r in t["rows"]]
            self.tables.append([r for r in rows if any(r)])
        elif self.stack and tag in ("td", "th"):
            self.stack[-1]["cell"] = None

    def handle_data(self, data):
        if self.skip or not self.stack:
            return
        c = self.stack[-1]["cell"]
        if c is not None:
            c.append(data)

def parse_html(text):
    p = TableParser()
    p.feed(text)
    p.close()
    return p.tables, p.links

# ---------------------------------------------------------------- 열 이름 사전 (gameone/index.html 과 동일하게 유지)
def norm(s):
    return re.sub(r"[\s ()（）\[\]]", "", str(s)).lower()

HB = {"name": ["이름", "선수", "선수명", "성명", "타자", "타자명"], "no": ["등번호", "배번", "no", "no.", "번호", "백넘버"],
      "team": ["팀", "팀명", "소속", "소속팀", "구단"], "G": ["경기", "경기수", "게임", "게임수", "출장"], "PA": ["타석", "타석수"],
      "AB": ["타수"], "R": ["득점"], "H": ["안타", "총안타"], "1B": ["1루타", "단타"], "2B": ["2루타", "2타"], "3B": ["3루타", "3타"],
      "HR": ["홈런"], "RBI": ["타점"], "BB": ["볼넷", "4구", "사구四球"], "HBP": ["사구", "死球", "몸에맞는공", "데드볼"],
      "SO": ["삼진"], "SB": ["도루"], "SH": ["희타", "희번", "희생번트", "희생타"], "SF": ["희비", "희생플라이"],
      "GDP": ["병살", "병살타"], "E": ["실책"]}
HP = {"name": ["이름", "선수", "선수명", "성명", "투수", "투수명"], "no": HB["no"], "team": HB["team"],
      "G": ["경기", "경기수", "게임", "출장"], "W": ["승", "승리"], "L": ["패", "패전"], "SV": ["세", "세이브"], "HLD": ["홀드"],
      "IP": ["이닝", "투구이닝", "투구회"], "BF": ["타자", "상대타자", "타자수", "상대타자수"], "H": ["피안타", "안타"],
      "HR": ["피홈런", "홈런"], "BB": ["볼넷", "4구", "피볼넷", "사구四球"], "HBP": ["사구", "死球", "피사구"],
      "SO": ["탈삼진", "삼진"], "R": ["실점"], "ER": ["자책", "자책점"], "NP": ["투구수"]}
BATK = ["G", "PA", "AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "HBP", "SO", "SB", "SH", "SF", "GDP", "E"]
PITK = ["G", "W", "L", "SV", "HLD", "OUTS", "BF", "H", "HR", "BB", "HBP", "SO", "R", "ER", "NP"]

def num(v):
    v = str(v).replace(",", "").strip()
    try:
        return float(v) if "." in v else int(v)
    except ValueError:
        m = re.match(r"-?\d+", v)
        return int(m.group()) if m else 0

def parse_ip(s):
    s = str(s).strip().replace("⅓", " 1/3").replace("⅔", " 2/3")
    m = re.fullmatch(r"(\d+)?\s*(?:(\d)\s*/\s*3)?", s)
    if m and (m.group(1) or m.group(2)):
        return int(m.group(1) or 0) * 3 + int(m.group(2) or 0)
    m = re.fullmatch(r"(\d+)\.(\d)", s)
    if m:
        f = int(m.group(2))
        return int(m.group(1)) * 3 + f if f <= 2 else round(float(s) * 3)
    try:
        return round(float(s) * 3)
    except ValueError:
        return 0

def split_name(raw):
    s, no = str(raw).strip(), ""
    m = re.match(r"^(.*?)\s*[(（\[]\s*(\d{1,3})\s*[)）\]]\s*$", s)
    if m:
        s, no = m.group(1), m.group(2)
    else:
        m = re.match(r"^(\d{1,3})\s*[.\s]\s*(\D.*)$", s)
        if m:
            no, s = m.group(1), m.group(2)
    return re.sub(r"\s+", "", s), no

def find_header(rows, d):
    idx = {norm(w): k for k, ws in d.items() for w in ws}
    for i, r in enumerate(rows[:15]):
        mp = {}
        for j, c in enumerate(r):
            k = idx.get(norm(c))
            if k and k not in mp:
                mp[k] = j
        if len(mp) >= 3 and "name" in mp:
            return i, mp
    return None

def parse_stat_tables(tables, mode):
    """페이지의 표들 중 기록표를 찾아 선수 레코드 목록 반환"""
    d = HB if mode == "bat" else HP
    out = []
    for rows in tables:
        h = find_header(rows, d)
        if not h:
            continue
        hi, mp = h
        for r in rows[hi + 1:]:
            if find_header([r], d) or mp["name"] >= len(r):
                continue
            raw = r[mp["name"]]
            if not raw or re.fullmatch(r"(합계|계|total|팀합계|평균)", raw.strip(), re.I):
                continue
            name, no = split_name(raw)
            if not name or re.fullmatch(r"[\d.\-]+", name):
                continue
            rec = {"name": name, "no": no, "team": "", "stats": {}}
            if "no" in mp and mp["no"] < len(r) and r[mp["no"]]:
                rec["no"] = r[mp["no"]].strip()
            if "team" in mp and mp["team"] < len(r):
                rec["team"] = r[mp["team"]].strip()
            st = rec["stats"]
            for k, j in mp.items():
                if k in ("name", "no", "team") or j >= len(r):
                    continue
                if k == "IP":
                    st["OUTS"] = parse_ip(r[j])
                else:
                    st[k] = num(r[j])
            if mode == "bat":
                if "H" not in mp and "1B" in mp:
                    st["H"] = sum(int(st.get(k, 0)) for k in ("1B", "2B", "3B", "HR"))
                st.pop("1B", None)
                if "PA" not in mp:
                    st["PA"] = sum(int(st.get(k, 0)) for k in ("AB", "BB", "HBP", "SF", "SH"))
            out.append(rec)
    return out

DATE_RE = re.compile(r"(\d{4})[-./년\s]+(\d{1,2})[-./월\s]+(\d{1,2})")
SHORT_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[./월]\s*(\d{1,2})일?(?!\d)")
GAME_RE = re.compile(r"([^\s\d:：\-]+(?:\s?[^\s\d:：\-]+)?)\s+(\d{1,2})\s*[:：\-]\s*(\d{1,2})\s+([^\s\d:：\-]+(?:\s?[^\s\d:：\-]+)?)")
STATUS_RE = re.compile(r"(?<!\S)(경기종료|종료|우천취소|취소|몰수승|몰수패|몰수|연기|진행중|예정|vs|VS|승|패|무|홈|원정)(?!\S)")
NOT_TEAM = re.compile(r"^(vs|VS|승|패|무|종료|경기종료|취소|우천취소|몰수|결과|스코어|점수|홈|원정|구장|\d+회)$")

def parse_games(tables, year):
    games, last_date = [], ""
    for rows in tables:
        for r in rows:
            line = " ".join(c for c in r if c)
            dm = DATE_RE.search(line)
            if dm:
                last_date = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
                line = line.replace(dm.group(0), " ")
            else:
                sm = SHORT_DATE_RE.search(line)
                if sm and year and 1 <= int(sm.group(1)) <= 12:
                    last_date = f"{year}-{int(sm.group(1)):02d}-{int(sm.group(2)):02d}"
                    line = line.replace(sm.group(0), " ")
            line = re.sub(r"[(（]\s*[일월화수목금토]\s*(요일)?[)）]", " ", line)
            line = STATUS_RE.sub(" ", line)
            line = re.sub(r"(?<!\d)\d{1,2}:\d{2}(?=\s+[^\d\s:])(?!\s*\d)", " ", line, count=1)  # 경기 시각 "09:00 팀명" 제거
            m = GAME_RE.search(line)
            if not m:
                continue
            a, sa, sb, b = m.group(1).strip(), int(m.group(2)), int(m.group(3)), m.group(4).strip()
            if NOT_TEAM.match(a) or NOT_TEAM.match(b) or a == b:
                continue
            games.append({"date": last_date, "A": a, "B": b, "sa": sa, "sb": sb})
    # 같은 경기 중복 제거
    seen, uniq = set(), []
    for g in games:
        k = (g["date"], g["A"], g["B"], g["sa"], g["sb"])
        if k not in seen:
            seen.add(k)
            uniq.append(g)
    return uniq

# ---------------------------------------------------------------- 페이지 넘김
PAGE_KEYS = ("page", "pg", "p", "cpage", "curpage", "page_no", "pageno", "nowpage")

def page_links(links, base_url):
    """같은 경로에서 페이지 번호만 다른 링크들"""
    bu = urllib.parse.urlparse(base_url)
    bq = dict(urllib.parse.parse_qsl(bu.query))
    found = {}
    for href in links:
        href = html.unescape(href)
        if href.startswith("javascript") or href.startswith("#"):
            continue
        u = urllib.parse.urlparse(urllib.parse.urljoin(base_url, href))
        if u.path.rstrip("/") != bu.path.rstrip("/"):
            continue
        q = dict(urllib.parse.parse_qsl(u.query))
        for k in PAGE_KEYS:
            if k in q and q[k].isdigit():
                rest = {kk: vv for kk, vv in q.items() if kk != k}
                base_rest = {kk: vv for kk, vv in bq.items() if kk != k}
                if rest == base_rest:
                    found[int(q[k])] = urllib.parse.urlunparse(u._replace(query=urllib.parse.urlencode(q), fragment=""))
    return found

def fetch_all_pages(f, url, max_pages):
    texts, done, queue = [], set(), {1: url}
    while queue and len(done) < max_pages:
        pn = min(queue)
        u = queue.pop(pn)
        if pn in done:
            continue
        done.add(pn)
        t = f.get(u)
        if not t:
            continue
        texts.append(t)
        _, links = parse_html(t)
        for k, v in page_links(links, url).items():
            if k not in done and k not in queue:
                queue[k] = v
    return texts

# ---------------------------------------------------------------- 메인
def lig_from(arg):
    if arg.isdigit():
        return arg
    m = re.search(r"lig_idx=(\d+)", arg)
    if not m:
        sys.exit("리그 주소에서 lig_idx 를 찾지 못했습니다. 예) https://www.gameone.kr/league/?lig_idx=10165")
    return m.group(1)

def main():
    ap = argparse.ArgumentParser(description="게임원 리그 기록 → 전력분석 도구 데이터")
    ap.add_argument("league", nargs="?", help="리그 주소 또는 lig_idx 숫자")
    ap.add_argument("--year", help="경기 날짜에 연도가 없을 때 붙일 연도 (기본: 올해)", default=None)
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--delay", type=float, default=1.5, help="요청 간격(초)")
    ap.add_argument("--max-pages", type=int, default=15, help="기록 종류별 최대 페이지 수")
    ap.add_argument("--dump", help="받은 HTML 원본 저장 폴더")
    ap.add_argument("--from-dir", help="저장해 둔 HTML 폴더로 분석(접속 안 함)")
    ap.add_argument("--serve", action="store_true", help="끝나면 http://localhost:8899/gameone/ 로 도구 열기")
    a = ap.parse_args()

    if a.from_dir and not a.league:
        names = os.listdir(a.from_dir)
        m = next((re.search(r"lig_idx_(\d+)", n) for n in names if "lig_idx_" in n), None)
        a.league = m.group(1) if m else "0"
    if not a.league:
        ap.error("리그 주소 또는 lig_idx 를 넣어주세요")
    lig = lig_from(a.league)
    f = Fetcher(a.delay, a.dump, a.from_dir)
    extra = ""
    year = a.year or str(datetime.now().year)

    urls = {
        "bat": f"{BASE}/league/record/batter?lig_idx={lig}{extra}",
        "pit": f"{BASE}/league/record/pitcher?lig_idx={lig}{extra}",
        "res": f"{BASE}/league/schedule/result?lig_idx={lig}{extra}",
    }
    teams = {}

    def team(name):
        name = name or "리그 선수(팀 미상)"
        if name not in teams:
            teams[name] = {"name": name, "players": {}, "games": []}
        return teams[name]

    report = {}
    for mode in ("bat", "pit"):
        print(f"[{'타자' if mode == 'bat' else '투수'}] {urls[mode]}")
        texts = fetch_all_pages(f, urls[mode], a.max_pages)
        recs = []
        for t in texts:
            tables, _ = parse_html(t)
            recs += parse_stat_tables(tables, mode)
        # 페이지 간 중복 제거
        seen, uniq = set(), []
        for r in recs:
            k = (r["team"], r["name"], r["no"])
            if k not in seen:
                seen.add(k)
                uniq.append(r)
        report[mode] = (len(texts), len(uniq))
        print(f"   페이지 {len(texts)}개, 선수 {len(uniq)}명")
        keys = BATK if mode == "bat" else PITK
        for r in uniq:
            pl = team(r["team"])["players"]
            # 타자표·투수표 중 한쪽에만 등번호가 있어도 같은 선수로 합침
            key = next((k for k in pl if k[0] == r["name"] and (not k[1] or not r["no"] or k[1] == r["no"])), None)
            if key and not key[1] and r["no"]:
                pl[(r["name"], r["no"])] = pl.pop(key)
                pl[(r["name"], r["no"])]["no"] = r["no"]
                key = (r["name"], r["no"])
            p = pl[key] if key else pl.setdefault((r["name"], r["no"]), {"name": r["name"], "no": r["no"], "bat": None, "pit": None})
            p[mode] = {k: r["stats"].get(k, 0) for k in keys}

    print(f"[경기 결과] {urls['res']}")
    texts = fetch_all_pages(f, urls["res"], a.max_pages)
    games = []
    for t in texts:
        tables, _ = parse_html(t)
        games += parse_games(tables, year)
    report["res"] = (len(texts), len(games))
    print(f"   페이지 {len(texts)}개, 경기 {len(games)}개")
    for g in games:
        for me, opp, rf, ra in ((g["A"], g["B"], g["sa"], g["sb"]), (g["B"], g["A"], g["sb"], g["sa"])):
            team(me)["games"].append({"date": g["date"], "opp": opp, "rf": rf, "ra": ra})

    data = {
        "source": "gameone_sync",
        "league": lig,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "teams": [{"name": t["name"], "players": list(t["players"].values()), "games": t["games"]} for t in teams.values()],
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fo:
        json.dump(data, fo, ensure_ascii=False, indent=1)
    print(f"\n저장: {a.out}  (팀 {len(teams)}개)")

    if report["bat"][1] == 0 and report["pit"][1] == 0:
        print("\n⚠️  선수 기록 표를 찾지 못했습니다. 게임원 페이지 구조가 예상과 다를 수 있어요.\n"
              "   --dump html_dump 옵션으로 다시 실행해서 생긴 html_dump 폴더의 파일을 보내주시면 맞춰드립니다.")

    if a.serve:
        import functools, http.server, webbrowser
        port = 8899
        h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ROOT)
        url = f"http://localhost:{port}/gameone/"
        print(f"\n도구 열기: {url}  (Ctrl+C로 종료)")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        http.server.ThreadingHTTPServer(("127.0.0.1", port), h).serve_forever()
    else:
        print("도구 반영: python3 cloud_server.py 실행 후 http://localhost:8890/gameone/ 새로고침\n"
              "         (또는 이 명령에 --serve 를 붙이면 바로 열림 / 데이터 탭 'JSON 불러오기'로 파일 직접 선택도 가능)")

if __name__ == "__main__":
    main()
