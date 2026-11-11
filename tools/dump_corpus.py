"""临时诊断：取一批股票的日线和关键数据，供离线设计“操作解读”。只在 debug 分支上用。"""
import json, sys, time
sys.path.insert(0, "scraper")
import scrape, requests
S = requests.Session(); S.headers.update(scrape.HEADERS)
codes = open("tools/codes.txt").read().split()
out = {}
for c in codes:
    rec = {}
    for name, u in (("chart", f"https://www.asx.com.au/asx/1/chart/highcharts?asx_code={c}&years=2"),
                    ("chart10", f"https://www.asx.com.au/asx/1/chart/highcharts?asx_code={c}&years=10") if c in ("BHP", "KAR") else (None, None),
                    ("stats", f"https://asx.api.markitdigital.com/asx-research/1.0/companies/{c}/key-statistics"),
                    ("header", f"https://asx.api.markitdigital.com/asx-research/1.0/companies/{c}/header")):
        if not name: continue
        try:
            t0 = time.time(); r = S.get(u, timeout=30)
            j = r.json() if "json" in r.headers.get("Content-Type", "") else None
            if name.startswith("chart"):
                rec[name] = {"status": r.status_code, "n": len(j) if isinstance(j, list) else None, "secs": round(time.time() - t0, 2),
                             "first": j[0] if isinstance(j, list) and j else None, "rows": j[-320:] if isinstance(j, list) else str(r.text[:200])}
            else:
                d = (j or {}).get("data") or {}
                rec[name] = {"status": r.status_code, **{k: d.get(k) for k in ("priceLast", "priceClose", "priceFiftyTwoWeekHigh", "priceFiftyTwoWeekLow",
                             "volumeAverage", "numOfShares", "marketCap", "sector", "industryGroup", "displayName", "securityType", "statusCode")}}
        except Exception as ex:
            rec[name] = {"error": str(ex)}
        time.sleep(0.4)
    out[c] = rec; print(c, rec.get("chart", {}).get("n"), flush=True)
import os; os.makedirs("out", exist_ok=True)
json.dump(out, open("out/prices.json", "w"))
