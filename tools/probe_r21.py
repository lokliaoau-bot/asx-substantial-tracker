"""第二十一轮探路 b：Regal 的 AI1 603（03090167）逐行明细，和当天收盘价并排。"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scraper"))
import scrape
import insight

OUT = Path("out")
OUT.mkdir(exist_ok=True)
ser = scrape.price_series("AI1")
closes = {dt.datetime.utcfromtimestamp(p[0] / 1000).date().isoformat(): (p[1], p[2] if len(p) > 2 else None) for p in ser if p and p[1]}
url = "https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId=03090167"
data, _ = scrape.download_pdf(url)
pages = scrape.trade_pages(data)
(OUT / "AI1_03090167_pages.txt").write_text("\n\n=====PAGE=====\n\n".join(pages), "utf-8")
(OUT / "AI1_texts.txt").write_text("\n\n=====\n\n".join(scrape.pdf_texts(data)), "utf-8")
res = insight.parse_trades(pages, "2026-05-04", "becoming")
rows = [dict(r, close=closes.get(r["date"])) for r in res["rows"]]
(OUT / "ai1.json").write_text(json.dumps({"summary": res["summary"], "rows": rows,
                                          "closes": {d: v for d, v in closes.items() if "2026-01-01" <= d <= "2026-05-10"}}, indent=1, default=str), "utf-8")
print("ok")
