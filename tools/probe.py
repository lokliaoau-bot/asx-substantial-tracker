"""临时探路（debug-r10）：更早的股价、全年公告页、基金月报标题。"""
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
for y in (2, 5, 10):
    t = get(f"https://www.asx.com.au/asx/1/chart/highcharts?asx_code=BAP&years={y}")
    try:
        d = json.loads(t); res[f"price_years_{y}"] = [len(d), d[0][0], d[-1][0]]
    except Exception:  # noqa: BLE001
        res[f"price_years_{y}"] = t[:200]
    time.sleep(0.6)
def rows(html):
    out = []
    for tr in BeautifulSoup(html, "html.parser").find_all("tr"):
        tds = tr.find_all("td"); a = tr.find("a", href=re.compile("idsId"))
        if len(tds) >= 3 and a:
            out.append([re.sub(r"\s+", " ", tds[0].get_text(" ", strip=True)), re.sub(r"\s+", " ", a.get_text(" ", strip=True))[:110], a["href"][-12:]])
    return out
for code, yr in (("BAP", 2026), ("BAP", 2025), ("BAP", 2024), ("DXN", 2025)):
    r = rows(get(f"https://www.asx.com.au/asx/v2/statistics/announcements.do?by=asxCode&asxCode={code}&timeframe=Y&year={yr}"))
    res[f"year_{code}_{yr}"] = {"n": len(r), "substantial": [x for x in r if "ubstantial" in x[1]]}
    time.sleep(0.6)
for code in ("WAM", "WMI", "WAX", "LSF", "RF1", "RPL", "PIC", "PL8", "REG"):
    r = rows(get(f"https://www.asx.com.au/asx/v2/statistics/announcements.do?by=asxCode&asxCode={code}&timeframe=D&period=M3"))
    res[f"fund_{code}"] = [x for x in r if re.search(r"update|report|NTA|monthly|newsletter|performance|FUM|funds under", x[1], re.I)][:25]
    time.sleep(0.6)
(OUT / "probe.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
# 一份月报的文字（WAM 系列的 Investment Update）
try:
    cand = next(x for k in ("fund_WAM", "fund_LSF", "fund_RF1") for x in res[k] if re.search(r"investment update|monthly|NTA", x[1], re.I))
    for k in ("fund_WAM", "fund_LSF", "fund_RF1", "fund_PIC"):
        c = next((x for x in res[k] if re.search(r"investment update|monthly|NTA|report", x[1], re.I)), None)
        if not c: continue
        i = re.search(r"idsId=(\d+)", "idsId=" + c[2].split("idsId=")[-1]).group(1)
        data, _ = scrape.download_pdf(f"https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId={i}")
        (OUT / f"report_{k}.txt").write_text("\n\f\n".join(scrape.trade_pages(data, 12)), encoding="utf-8")
        time.sleep(0.6)
except Exception as e:  # noqa: BLE001
    (OUT / "report_err.txt").write_text(str(e))
