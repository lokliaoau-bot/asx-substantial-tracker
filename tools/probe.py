"""临时探路（debug-r15）：周末的当天公告页长什么样。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scraper"))
import scrape  # noqa: E402
OUT = Path("out"); OUT.mkdir(exist_ok=True)
for url in scrape.LIST_PAGES:
    try:
        t = scrape.fetch(url).text
    except Exception as e:  # noqa: BLE001
        t = "ERR " + str(e)
    (OUT / url.rsplit("/", 1)[-1].replace(".do", ".html")).write_text(t, encoding="utf-8")
