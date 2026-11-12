"""临时探路（debug-r12）：指数日线、基金月报正文。"""
import json, sys, time, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scraper"))
import scrape  # noqa: E402
from bs4 import BeautifulSoup
OUT = Path("out"); OUT.mkdir(exist_ok=True)
res = {}
def get(url):
    try:
        return scrape.fetch(url, timeout=40, tries=1).text
    except Exception as e:  # noqa: BLE001
        return "ERR " + str(e)[:200]
for code in ("XJO", "XAO", "XSO", "XKO", "IOZ", "VAS", "STW", "A200"):
    t = get(f"https://www.asx.com.au/asx/1/chart/highcharts?asx_code={code}&years=2")
    try:
        d = json.loads(t); res["px_" + code] = [len(d), d[0], d[-1]]
    except Exception:  # noqa: BLE001
        res["px_" + code] = t[:200]
    time.sleep(0.6)
for u in ("https://asx.api.markitdigital.com/asx-research/1.0/markets/indices/XJO/chart?range=2y",
          "https://asx.api.markitdigital.com/asx-research/1.0/companies/XJO/header",
          "https://asx.api.markitdigital.com/asx-research/1.0/etfs/VAS/header"):
    res[u] = get(u)[:400]; time.sleep(0.6)
for code, pat in (("WAM", r"investment update"), ("WMI", r"investment update"), ("LSF", r"monthly report"), ("RF1", r"monthly report"), ("PIC", r"monthly investment update"), ("PL8", r"investment update")):
    html = get(f"https://www.asx.com.au/asx/v2/statistics/announcements.do?by=asxCode&asxCode={code}&timeframe=D&period=M3")
    rows = scrape.parse_company_rows(html) or []
    hit = next((r for r in rows if re.search(pat, r["headline"], re.I)), None)
    res["report_" + code] = hit
    if hit:
        try:
            data, _ = scrape.download_pdf(hit["url"])
            (OUT / f"report_{code}.txt").write_text("\n\f\n".join(scrape.trade_pages(data, 8)), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            res["report_err_" + code] = str(e)[:200]
    time.sleep(0.6)
(OUT / "probe.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
