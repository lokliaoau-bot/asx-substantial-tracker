"""临时探路：试 ASX 的公司公告接口，并把命中名单公告的交易明细原文导出来。只在 debug-r8 分支用。"""
import json, sys, time, traceback
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scraper"))
import scrape  # noqa: E402

OUT = Path("out"); OUT.mkdir(exist_ok=True)
res = {}
URLS = [
    "https://www.asx.com.au/asx/1/company/{c}/announcements?count=20&market_sensitive=false",
    "https://www.asx.com.au/asx/1/company/{c}/announcements?count=200&market_sensitive=false",
    "https://www.asx.com.au/asx/1/company/{c}?fields=primary_share,latest_annual_reports,last_dividend,primary_share.indices",
    "https://asx.api.markitdigital.com/asx-research/1.0/companies/{c}/announcements",
    "https://asx.api.markitdigital.com/asx-research/1.0/companies/{c}/announcements?count=200",
    "https://asx.api.markitdigital.com/asx-research/1.0/companies/{c}/header",
    "https://asx.api.markitdigital.com/asx-research/1.0/companies/{c}/about",
    "https://asx.api.markitdigital.com/asx-research/1.0/companies/{c}/key-statistics",
    "https://asx.api.markitdigital.com/asx-research/1.0/markets/announcements?asxCodes[]={c}&page=0&itemsPerPage=200",
    "https://asx.api.markitdigital.com/asx-research/1.0/markets/announcements?symbol={c}&page=0&itemsPerPage=200",
    "https://www.asx.com.au/asx/v2/statistics/announcements.do?by=asxCode&asxCode={c}&timeframe=D&period=M6",
    "https://www.asx.com.au/asx/v2/statistics/announcements.do?by=asxCode&asxCode={c}&timeframe=Y&year=2026",
]
for c in ("PPE", "BHP"):
    for u in URLS:
        url = u.format(c=c)
        try:
            r = scrape.fetch(url, timeout=40, tries=1)
            t = r.text
            res[url] = {"ok": True, "len": len(t), "head": t[:6000]}
            if "announcements.do" in url and c == "PPE":
                (OUT / ("list_" + ("y" if "year" in url else "m6") + ".html")).write_text(t, encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            res[url] = {"ok": False, "err": f"{type(e).__name__}: {e}"[:400]}
        time.sleep(0.6)
for url in ("https://download.asic.gov.au/short-selling/RR20261105-001-SSDailyAggShortPos.csv",
            "https://download.asic.gov.au/short-selling/RR20261105-001-SSDailyAggShortPos.json",
            "https://asic.gov.au/Reports/Daily/2026/11/RR20261105-001-SSDailyAggShortPos.csv"):
    try:
        r = scrape.fetch(url, timeout=40, tries=1)
        res[url] = {"ok": True, "len": len(r.text), "head": r.text[:1500]}
    except Exception as e:  # noqa: BLE001
        res[url] = {"ok": False, "err": f"{type(e).__name__}: {e}"[:400]}
(OUT / "probe.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

# 命中名单公告的明细原文
store = json.loads(Path("data/all_notices.json").read_text(encoding="utf-8"))["entries"]
(OUT / "texts").mkdir(exist_ok=True)
n = 0
for e in store.values():
    if not e.get("detail_v") or e.get("ocr"):
        continue
    try:
        data, _ = scrape.download_pdf(e["url"])
        pages = scrape.trade_pages(data)
        (OUT / "texts" / f"{e['id']}.txt").write_text("\n\f\n".join(pages), encoding="utf-8")
        n += 1
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    time.sleep(0.4)
print("texts", n)
