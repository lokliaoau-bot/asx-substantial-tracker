"""第二十一轮探路（只在 debug-r21 分支）：L1 的 LIN / EMR / MIN 明细均价和股价对不上——逐行打印 parse_trades 读到的东西，和当天收盘价并排。"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scraper"))
import scrape
import insight

OUT = Path("out")
OUT.mkdir(exist_ok=True)
ROOT = Path(__file__).resolve().parent.parent
st = json.loads((ROOT / "data" / "all_notices.json").read_text("utf-8"))["entries"]
want = {"03160606", "mk-2924-03150696-6A1350229", "mk-2924-03152641-6A1351121", "mk-2924-03152587-6A1351089",
        "mk-2924-03152581-6A1351082", "03165234"}
# 对照：同一批里均价对得上的 L1 公告也抓两份
for i, e in st.items():
    if "L1" in str(e.get("holder_name")) and e.get("code") in ("WGX", "PPC", "BMC") and len(want) < 9:
        want.add(i)
closes = {}
report = {}
for i in sorted(want):
    e = st.get(i)
    if not e:
        continue
    code = e["code"]
    if code not in closes:
        try:
            ser = scrape.price_series(code)
            closes[code] = {dt.datetime.utcfromtimestamp(p[0] / 1000).date().isoformat(): p[1] for p in ser if p and p[1]}
        except Exception as ex:
            closes[code] = {}
    try:
        data, _ = scrape.download_pdf(e["url"])
    except Exception as ex:
        report[i] = {"err": str(ex)}
        continue
    pages = scrape.trade_pages(data)
    (OUT / f"{code}_{i}_pages.txt").write_text("\n\n=====PAGE=====\n\n".join(pages), "utf-8")
    res = insight.parse_trades(pages, e.get("date", ""), e.get("type", ""))
    rows = []
    for r in res["rows"]:
        d = r.get("date")
        rows.append(dict(r, close=closes[code].get(str(d)), unit=(r.get("cons") / r.get("qty")) if r.get("cons") and r.get("qty") else None))
    report[i] = {"code": code, "date": e.get("date"), "summary": res["summary"], "rows": rows, "n_pages": len(pages)}
(OUT / "rows.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), "utf-8")
print("done", len(report))
