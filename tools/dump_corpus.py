"""临时诊断：导出全部已存公告的全文语料、耗时，并试探股价接口。只在 debug 分支上用。"""
import io, json, sys, time, threading
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "scraper")
import scrape, requests
from pypdf import PdfReader, PdfWriter

store = json.load(open("data/all_notices.json"))["entries"]
holders, noise = scrape.load_watchlist()
MAXP = 12
lock = threading.Lock()
corpus = {}

def page_texts(data):
    out = {"layout": [], "plain": [], "n_pages": 0}
    try:
        r = PdfReader(io.BytesIO(data))
        if r.is_encrypted:
            r.decrypt("")
        out["n_pages"] = len(r.pages)
        for p in r.pages[:MAXP]:
            for mode in ("layout", "plain"):
                try:
                    t = p.extract_text(extraction_mode="layout") if mode == "layout" else p.extract_text()
                except Exception as ex:
                    t = ""
                out[mode].append(scrape.clean_text(t or ""))
    except Exception as ex:
        out["error"] = f"{type(ex).__name__}: {ex}"
    return out

def work(e):
    i = e["id"]
    rec = {k: e.get(k) for k in ("id", "code", "date", "time", "headline", "type", "hint", "status", "holder_name", "company",
                                 "voting_power", "change_date", "ocr", "source", "url", "region")}
    rec["match"] = scrape.match_entry(e, holders)[0]
    if e.get("status") in ("noise", "skipped"):
        with lock: corpus[i] = rec
        return
    try:
        t0 = time.time(); data, pdf_url = scrape.download_pdf(e["url"]); rec["t_download"] = round(time.time() - t0, 2)
        rec["bytes"] = len(data)
        t0 = time.time(); rec["texts"] = scrape.pdf_texts(data); rec["t_text2"] = round(time.time() - t0, 2)
        t0 = time.time(); rec["pages"] = page_texts(data); rec["t_full"] = round(time.time() - t0, 2)
        if e.get("ocr") or not rec["texts"]:
            t0 = time.time()
            try:
                rec["ocr_texts"] = scrape.ocr_texts(data, pages=6)
            except Exception as ex:
                rec["ocr_error"] = str(ex)
            rec["t_ocr"] = round(time.time() - t0, 2)
        if rec["match"] or not e.get("holder_name"):
            try:
                r = PdfReader(io.BytesIO(data))
                if r.is_encrypted: r.decrypt("")
                w = PdfWriter()
                for p in r.pages[:MAXP]: w.add_page(p)
                buf = io.BytesIO(); w.write(buf)
                if len(buf.getvalue()) < 3_000_000:
                    open(f"out/pdfs/{e['code']}_{i}.pdf", "wb").write(buf.getvalue())
            except Exception as ex:
                rec["trim_error"] = str(ex)
    except Exception as ex:
        rec["error"] = f"{type(ex).__name__}: {ex}"
    with lock: corpus[i] = rec
    print("ok" if "error" not in rec else "FAIL", i, e["code"], rec.get("t_download"), rec.get("t_text2"), rec.get("t_ocr"), flush=True)

t_all = time.time()
import os
os.environ["OMP_THREAD_LIMIT"] = "1"
with ThreadPoolExecutor(max_workers=4) as ex:
    list(ex.map(work, sorted(store.values(), key=lambda e: e["id"])))
print("total secs", round(time.time() - t_all, 1))
json.dump({"total_secs": round(time.time() - t_all, 1), "entries": corpus}, open("out/corpus.json", "w"), ensure_ascii=False)

# ---- 股价接口试探
S = requests.Session(); S.headers.update(scrape.HEADERS)
probes = {}
urls = []
for c in ("BHP", "SGH", "KAR"):
    urls += [f"https://asx.api.markitdigital.com/asx-research/1.0/companies/{c}/header",
             f"https://asx.api.markitdigital.com/asx-research/1.0/companies/{c}/key-statistics"]
urls += ["https://asx.api.markitdigital.com/asx-research/1.0/companies/BHP/about",
         "https://asx.api.markitdigital.com/asx-research/1.0/companies/BHP/announcements?count=3",
         "https://asx.api.markitdigital.com/asx-research/1.0/charts/BHP?timeframe=1y",
         "https://asx.api.markitdigital.com/asx-research/1.0/companies/BHP/charts?timeframe=1Y",
         "https://asx.api.markitdigital.com/asx-research/1.0/companies/BHP/price-history?count=260",
         "https://asx.api.markitdigital.com/asx-research/1.0/companies/BHP/prices?interval=daily&count=260",
         "https://www.asx.com.au/asx/1/share/BHP",
         "https://www.asx.com.au/asx/1/share/BHP/prices?interval=daily&count=260",
         "https://www.asx.com.au/asx/1/chart/highcharts?asx_code=BHP&years=1",
         "https://query1.finance.yahoo.com/v8/finance/chart/BHP.AX?range=1y&interval=1d",
         "https://stooq.com/q/d/l/?s=bhp.au&i=d"]
for u in urls:
    try:
        r = S.get(u, timeout=30)
        probes[u] = {"status": r.status_code, "type": r.headers.get("Content-Type"), "len": len(r.content), "body": r.text[:2500]}
    except Exception as ex:
        probes[u] = {"error": str(ex)}
    time.sleep(1)
# 看 ASX 公司页面的前端脚本里还调用了哪些 markitdigital 接口（找图表/历史价格）
try:
    import re
    html = S.get("https://www.asx.com.au/markets/company/BHP", timeout=40).text
    srcs = sorted(set(re.findall(r'src="([^"]+\.js[^"]*)"', html)))
    found = set()
    for s in srcs[:80]:
        u = s if s.startswith("http") else "https://www.asx.com.au" + s
        if "asx.com.au" not in u: continue
        try: js = S.get(u, timeout=40).text
        except Exception: continue
        for m in re.finditer(r"asx-research/1\.0/[A-Za-z0-9_\-/${}.?=&+\"'` ]{3,120}", js):
            found.add(m.group(0)[:160])
        time.sleep(0.3)
    probes["_frontend_endpoints"] = sorted(found)[:300]
    probes["_scripts"] = srcs[:80]
except Exception as ex:
    probes["_frontend_error"] = str(ex)
json.dump(probes, open("out/probes.json", "w"), ensure_ascii=False, indent=1)
