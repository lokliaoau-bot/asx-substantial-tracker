"""第十九轮探路（只在 debug-r19 分支）：读几家公司近 6 个月 / 全年公告里的 Appendix 3B / 2A，把 PDF 文字存下来看版式。"""
import json, re, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scraper"))
import scrape
from bs4 import BeautifulSoup

OUT = Path("out"); OUT.mkdir(exist_ok=True)
CODES = ["PPC", "TTT", "RIV", "EOL", "PC2", "EMR", "GTI", "YRL", "SPG", "BMC", "TOR", "MP1", "GLS", "DXN", "WGX"]
HEAD = re.compile(r"proposed\s+issue\s+of\s+securities|application\s+for\s+quotation|notification\s+regarding\s+unquoted|"
                  r"placement|entitlement|share\s+purchase\s+plan|cleansing", re.I)
index = []
for code in CODES:
    for url in (scrape.COMPANY_ANNS.format(code=code),
                scrape.BASE + f"/asx/v2/statistics/announcements.do?by=asxCode&asxCode={code}&timeframe=Y&year=2026"):
        try:
            html = scrape.fetch(url, timeout=40, tries=2).text
        except Exception as ex:
            index.append({"code": code, "err": str(ex)}); continue
        soup = BeautifulSoup(html, "html.parser")
        rows = []
        for tr in soup.find_all("tr"):
            a = tr.find("a", href=re.compile(r"idsId|displayAnnouncement", re.I))
            tds = tr.find_all("td")
            if a is None or len(tds) < 3:
                continue
            h = a.get_text(" ", strip=True)
            if HEAD.search(h):
                rows.append({"d": tds[0].get_text(" ", strip=True), "h": h[:120], "href": a["href"]})
        index.append({"code": code, "url": url, "n": len(rows), "rows": rows[:30]})
        if rows:
            break
    got = 0
    for r in index[-1].get("rows", []):
        if got >= 4 or not re.search(r"proposed\s+issue|application\s+for\s+quotation", r["h"], re.I):
            continue
        href = r["href"] if r["href"].startswith("http") else scrape.BASE + r["href"]
        try:
            data, _ = scrape.download_pdf(href)
            texts = scrape.pdf_texts(data, pages=12)
        except Exception as ex:
            r["err"] = str(ex); continue
        got += 1
        m = re.search(r"idsId=(\d+)", href)
        (OUT / f"{code}_{m.group(1) if m else got}.txt").write_text(f"{r['d']} | {r['h']}\n\n" + "\n\n=====\n\n".join(texts), "utf-8")
        time.sleep(1)
    time.sleep(1)
(OUT / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), "utf-8")
print("done", sum(1 for _ in OUT.glob("*.txt")))
