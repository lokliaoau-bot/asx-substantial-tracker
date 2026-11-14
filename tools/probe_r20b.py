"""第二十轮探路 b（只在 debug-r20 分支）：给每条持仓历史和近期公告取“交易窗口首尾”和“之后 60 天”的 ASIC 做空比例，
离线检验“做空数据能不能帮着猜目的”。只存我们关心的股票。"""
import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scraper"))
import scrape

OUT = Path("out")
OUT.mkdir(exist_ok=True)
ROOT = Path(__file__).resolve().parent.parent
ASIC = "https://download.asic.gov.au/short-selling/RR{d}-001-SSDailyAggShortPos.csv"
LATEST = dt.date(2026, 11, 9)


def bday(d: dt.date) -> dt.date:
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def iso(s):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


codes, want = set(), set()
recs = []
hist = json.loads((ROOT / "data" / "history.json").read_text("utf-8"))
for key, rows in hist.get("pos", {}).items():
    code = key.split("|", 1)[1]
    for r in rows:
        tr = r.get("tr") or {}
        a = iso(tr.get("first")) or iso(r.get("cd")) or iso(r.get("date"))
        b = iso(r.get("cd")) or iso(tr.get("last")) or iso(r.get("date"))
        if not a or not b:
            continue
        recs.append((code, a, b))
store = json.loads((ROOT / "data" / "all_notices.json").read_text("utf-8")).get("entries", {})
for e in store.values():
    if not isinstance(e, dict) or not e.get("code"):
        continue
    tr = e.get("trades") or {}
    a = iso(tr.get("first")) or iso(e.get("change_date")) or iso(e.get("date"))
    b = iso(e.get("change_date")) or iso(tr.get("last")) or iso(e.get("date"))
    if a and b:
        recs.append((e["code"], a, b))
for code, a, b in recs:
    codes.add(code)
    for d in (a - dt.timedelta(days=1), b, b + dt.timedelta(days=60)):
        d = bday(d)
        if dt.date(2010, 1, 1) <= d <= LATEST:
            want.add(d)
print(len(codes), "codes", len(want), "dates")
res, fail = {}, {}
t0 = time.time()
for d in sorted(want):
    got = None
    for back in range(0, 5):                 # 假期没有文件：往前找最近的一天
        dd = bday(d - dt.timedelta(days=back))
        try:
            r = scrape.session.get(ASIC.format(d=dd.strftime("%Y%m%d")), timeout=30)
        except Exception as ex:
            fail[d.isoformat()] = str(ex)
            continue
        if r.status_code == 200:
            got = (dd, r.content.decode("utf-8-sig", errors="replace"))
            break
    if not got:
        fail.setdefault(d.isoformat(), "no file")
        continue
    day = {}
    for ln in got[1].splitlines()[1:]:
        cells = ln.rsplit(",", 4)
        if len(cells) == 5 and cells[1] in codes:
            try:
                day[cells[1]] = [int(cells[2]), int(cells[3]), float(cells[4])]
            except ValueError:
                pass
    res[d.isoformat()] = {"file": got[0].isoformat(), "n_lines": got[1].count("\n"), "rows": day}
    if time.time() - t0 > 1500:
        break
(OUT / "short_by_date.json").write_text(json.dumps({"res": res, "fail": fail}, ensure_ascii=False), "utf-8")
print("done", len(res), "dates", len(fail), "fails", round(time.time() - t0), "s")
