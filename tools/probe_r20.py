"""第二十轮探路（只在 debug-r20 分支）：
1. ASIC 每日做空 CSV：看文件格式、编码、能往前取多久、我们关心的股票有没有数
2. Regal Partners（RPL）和 L1 Group（L1G）的规模（FUM）月报：标题、正文版式
"""
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scraper"))
import scrape
from bs4 import BeautifulSoup

OUT = Path("out")
OUT.mkdir(exist_ok=True)
ROOT = Path(__file__).resolve().parent.parent
codes = set()
for x in json.loads((ROOT / "data" / "notices.json").read_text("utf-8")).get("notices", []):
    codes.add(x.get("code"))
for k in json.loads((ROOT / "data" / "history.json").read_text("utf-8")).get("pos", {}):
    codes.add(k.split("|", 1)[1])
codes.discard(None)

# ---------------------------------------------------------------- 1. ASIC 做空
ASIC = "https://download.asic.gov.au/short-selling/RR{d}-001-SSDailyAggShortPos.csv"
short = {"files": []}
today = dt.date(2026, 11, 14)
dates = [today - dt.timedelta(days=i) for i in range(0, 21)]
dates += [dt.date(2026, 6, 1), dt.date(2025, 11, 3), dt.date(2025, 3, 3), dt.date(2024, 6, 3), dt.date(2023, 11, 6), dt.date(2022, 11, 7)]
first_raw = None
for d in dates:
    if d.weekday() >= 5:
        continue
    url = ASIC.format(d=d.strftime("%Y%m%d"))
    rec = {"d": d.isoformat(), "url": url}
    t0 = time.time()
    try:
        r = scrape.session.get(url, timeout=40)
        rec.update(status=r.status_code, bytes=len(r.content), ctype=r.headers.get("Content-Type", ""), secs=round(time.time() - t0, 1))
        if r.status_code == 200:
            raw = r.content
            if first_raw is None:
                first_raw = raw
                (OUT / f"asic_{d:%Y%m%d}.raw").write_bytes(raw[:6000])
            enc = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
            text = raw.decode(enc, errors="replace")
            lines = text.splitlines()
            rec["enc"] = enc
            rec["head"] = lines[:3]
            rec["n_lines"] = len(lines)
            hits = {}
            for ln in lines[1:]:
                cells = [c.strip().strip('"') for c in re.split(r"\t|,(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", ln)]
                if len(cells) >= 2 and cells[1] in codes:
                    hits[cells[1]] = cells
            rec["hits"] = len(hits)
            rec["sample"] = dict(list(hits.items())[:12])
    except Exception as ex:
        rec["err"] = f"{type(ex).__name__}: {ex}"
    short["files"].append(rec)
    time.sleep(0.5)
(OUT / "asic.json").write_text(json.dumps(short, ensure_ascii=False, indent=1), "utf-8")
print("asic done", sum(1 for f in short["files"] if f.get("status") == 200))

# ---------------------------------------------------------------- 2. 规模月报
FUM = re.compile(r"funds?\s+under\s+management|\bFUM\b|assets\s+under\s+management", re.I)
index = []
for code in ("RPL", "L1G"):
    for year in (2026, 2025, 2024):
        url = scrape.COMPANY_YEAR.format(code=code, year=year)
        try:
            html = scrape.fetch(url, timeout=40, tries=2).text
        except Exception as ex:
            index.append({"code": code, "year": year, "err": str(ex)})
            continue
        soup = BeautifulSoup(html, "html.parser")
        rows = []
        for tr in soup.find_all("tr"):
            a = tr.find("a", href=re.compile(r"idsId|displayAnnouncement", re.I))
            tds = tr.find_all("td")
            if a is None or len(tds) < 3:
                continue
            h = a.get_text(" ", strip=True)
            if FUM.search(h):
                rows.append({"d": tds[0].get_text(" ", strip=True), "h": h[:140], "href": a["href"]})
        index.append({"code": code, "year": year, "n": len(rows), "rows": rows})
        got = 0
        for r in rows:
            if got >= (6 if year == 2026 else 3):
                break
            href = r["href"] if r["href"].startswith("http") else scrape.BASE + r["href"]
            try:
                data, _ = scrape.download_pdf(href)
                texts = scrape.pdf_texts(data, pages=4)
            except Exception as ex:
                r["err"] = str(ex)
                continue
            got += 1
            m = re.search(r"idsId=(\d+)", href)
            (OUT / f"fum_{code}_{m.group(1) if m else got}.txt").write_text(f"{r['d']} | {r['h']}\n\n" + "\n\n=====\n\n".join(texts), "utf-8")
            time.sleep(1)
        time.sleep(1)
(OUT / "fum_index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), "utf-8")
print("fum done", sum(1 for _ in OUT.glob("fum_*.txt")))
