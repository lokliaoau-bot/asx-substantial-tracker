"""离线测试：不访问 ASX，用模拟的列表页和 PDF 验证解析、匹配和主流程。

运行：pip install -r requirements-dev.txt && python -m pytest -q
"""
import datetime as dt
import io
import json
import shutil
import sys
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scraper"))
import scrape  # noqa: E402

HOLDERS, NOISE = scrape.load_watchlist(ROOT / "scraper" / "watchlist.json")


def make_pdf(lines, encrypt=False):
    buf = io.BytesIO()
    kw = {}
    if encrypt:
        from reportlab.lib import pdfencrypt
        kw["encrypt"] = pdfencrypt.StandardEncryption("", canPrint=1)
    c = canvas.Canvas(buf, **kw)
    y = 800
    for line in lines:
        c.drawString(40, y, line)
        y -= 16
    c.showPage()
    c.save()
    return buf.getvalue()


def text_of(lines):
    return scrape.pdf_text(make_pdf(lines))


# ---------------------------------------------------------------- 列表页

LISTING = """<html><body><h2>Company announcements published on 30 October 2026</h2><table>
<tr><th>ASX Code</th><th>Date</th><th>Price sens.</th><th>Headline</th></tr>
<tr><td>SLC</td><td>30/10/2026<br><span>4:10 PM</span></td><td class="pricesens"></td>
<td><a href="/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId=1001">Becoming a substantial holder<br><span class="page">3 pages</span> <span class="filesize">120.1KB</span></a></td></tr>
<tr><td>ABC</td><td>30/10/2026<br><span>10:05 AM</span></td><td class="pricesens"><img src="x.gif"></td>
<td><a href="/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId=1002">Change in substantial holding<br><span class="page">2 pages</span></a></td></tr>
<tr><td>XYZ</td><td>30/10/2026 12:30 PM</td><td><img class="pricesens" src="a.gif"></td>
<td><a href="/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId=1003">Change in substantial holding</a></td></tr>
<tr><td>GMD</td><td>30/10/2026<br><span>6:30 PM</span></td><td class="pricesens"></td>
<td><a href="/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId=1004">Becoming a substantial holder from MUFG 712 pages 3.0MB</a></td></tr>
<tr><td>WAX</td><td>30/10/2026<br><span>5:00 PM</span></td><td class="pricesens"></td>
<td><a href="/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId=1005">Change in substantial holding from WAM</a></td></tr>
<tr><td>BHP</td><td>30/10/2026<br><span>9:00 AM</span></td><td class="pricesens"></td>
<td><a href="/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId=1006">Quarterly Activities Report</a></td></tr>
</table></body></html>"""


def test_parse_listing_fields():
    rows, ok = scrape.parse_listing(LISTING, dt.date(2026, 10, 30))
    assert ok and len(rows) == 6
    by = {r["id"]: r for r in rows}
    assert by["1001"]["time"] == "16:10" and by["1002"]["time"] == "10:05" and by["1003"]["time"] == "12:30"
    assert by["1001"]["headline"] == "Becoming a substantial holder" and by["1001"]["pages"] == 3
    assert by["1004"]["headline"] == "Becoming a substantial holder from MUFG" and by["1004"]["pages"] == 712
    assert by["1002"]["price_sensitive"] is True          # 图标在带 class 的单元格里
    assert by["1003"]["price_sensitive"] is True          # class 直接在图标上
    assert by["1001"]["price_sensitive"] is False
    assert all(r["date"] == "2026-10-30" for r in rows)


def test_time_sorts_correctly_in_24h():
    assert sorted(["16:10", "10:05", "09:30"]) == ["09:30", "10:05", "16:10"]
    assert scrape.to_24h("12", "05", "A") == "00:05" and scrape.to_24h("12", "30", "P") == "12:30"


def test_listing_structure_detection():
    assert scrape.parse_listing("<table><tr><th>ASX Code</th></tr></table>", dt.date.today()) == ([], True)
    assert scrape.parse_listing("<html><body>Access denied</body></html>", dt.date.today()) == ([], False)


def test_classify_and_hint():
    assert scrape.classify("Becoming a substantial holder") == "becoming"
    assert scrape.classify("Change in substantial holding from WAM") == "change"
    assert scrape.classify("Ceasing to be a substantial holder") == "ceasing"
    assert scrape.headline_hint("Change in substantial holding from WAM") == "WAM"
    assert scrape.headline_hint("Becoming a substantial holder") == ""


# ---------------------------------------------------------------- PDF 地址

def test_resolve_pdf_link_variants():
    base = "https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?idsId=1"
    assert scrape.resolve_pdf_link('<input name="pdfURL" value="/asxpdf/20261030/pdf/a.pdf">', base) \
        == "https://www.asx.com.au/asxpdf/20261030/pdf/a.pdf"
    assert scrape.resolve_pdf_link('<script>u="https://announcements.asx.com.au/asxpdf/20261030/pdf/b.pdf"</script>', base) \
        == "https://announcements.asx.com.au/asxpdf/20261030/pdf/b.pdf"
    assert scrape.resolve_pdf_link('<a href="javascript:alert(1).pdf">x</a>', base) == ""


# ---------------------------------------------------------------- 申报方解析与匹配

def entry_from(lines, hint=""):
    t = text_of(lines)
    name, region = scrape.parse_holder_section(t)
    return {"holder_name": name, "region": "" if name else region, "hint": hint}, t


def test_holder_name_same_line_and_next_line():
    e, t = entry_from(["To Company Name/Scheme Superloop Limited", "1. Details of substantial holder (1)",
                       "Name Paradice Investment Management Pty Ltd ACN 090 148 619"])
    assert e["holder_name"] == "Paradice Investment Management Pty Ltd"
    assert scrape.parse_company(t) == "Superloop Limited"
    assert scrape.match_entry(e, HOLDERS) == ("Paradice Investment Management", "高")
    e, _ = entry_from(["1. Details of substantial holder (1)", "Name", "Regal Funds Management Pty Limited", "ACN 107 576 821"])
    assert scrape.match_entry(e, HOLDERS) == ("Regal Funds Management", "高")


def test_no_false_positive_when_target_company_has_watchlist_name():
    e, _ = entry_from(["To Company Name/Scheme Washington H. Soul Pattinson and Company Limited",
                       "1. Details of substantial holder (1)", "Name BlackRock Group (BlackRock Inc. and subsidiaries)"])
    assert scrape.match_entry(e, HOLDERS) == (None, "")
    e, _ = entry_from(["To Company Name/Scheme WAM Capital Limited", "Name Some Super Fund Pty Ltd"])
    assert scrape.match_entry(e, HOLDERS) == (None, "")


def test_medium_and_low_confidence():
    e, _ = entry_from(["1. Details of substantial holder (1)", "Holder: Ellerston Capital Limited"])
    assert e["holder_name"] == "" and scrape.match_entry(e, HOLDERS) == ("Ellerston Capital", "中")
    assert scrape.match_entry({"holder_name": "", "region": "", "hint": "WAM"}, HOLDERS) == ("Wilson Asset Management", "低")
    # 名称解析出来、但不在名单里时，不能再拿标题去猜
    assert scrape.match_entry({"holder_name": "BlackRock Group", "region": "", "hint": "WAM"}, HOLDERS) == (None, "")


def test_noise_detection():
    assert scrape.is_noise("MUFG", HOLDERS, NOISE)
    assert not scrape.is_noise("WAM", HOLDERS, NOISE)
    assert not scrape.is_noise("", HOLDERS, NOISE)
    assert not scrape.is_noise("MSCI Inc", HOLDERS, NOISE)   # “MS” 只按整词匹配


# ---------------------------------------------------------------- 比例与日期

def test_parse_details():
    t = "There was a change in the interests of the substantial holder on 27/10/26\n2. Previous and present voting power\nOrdinary 1,000 6.00% 1,000 6.00%"
    d = scrape.parse_details(t, "change")
    assert d == {"change_date": "2026-10-27", "voting_power": [6.0, 6.0]}
    d = scrape.parse_details("The holder became a substantial holder on 28 October 2026\n2. Details of voting power\nOrdinary 25,000 5.12%", "becoming")
    assert d == {"change_date": "2026-10-28", "voting_power": [5.12]}
    d = scrape.parse_details("The holder ceased to be a substantial holder on 3-Nov-2026 voting power 4.1%", "ceasing")
    assert d == {"change_date": "2026-11-03", "voting_power": []}


def test_encrypted_pdf_is_read():
    t = scrape.pdf_text(make_pdf(["1. Details of substantial holder (1)", "Name L1 Capital Pty Ltd"], encrypt=True))
    assert "L1 Capital" in t


# ---------------------------------------------------------------- 主流程

PDFS = {
    "1001": ["Form 603", "To Company Name/Scheme Superloop Limited", "1. Details of substantial holder (1)",
             "Name Paradice Investment Management Pty Ltd", "The holder became a substantial holder on 28/10/2026",
             "2. Details of voting power", "Fully paid ordinary shares 25,123,456 25,123,456 5.12%"],
    "1002": ["Form 604", "To Company Name/Scheme ABC Limited", "1. Details of substantial holder (1)",
             "Name Some Random Fund Pty Ltd", "There was a change in the interests of the substantial holder on 27/10/2026",
             "2. Previous and present voting power", "Ordinary 1 6.01% 2 7.05%"],
    "1003": ["Form 604", "To Company Name/Scheme XYZ Limited", "1. Details of substantial holder (1)",
             "Name Celeste Funds Management Limited", "There was a change in the interests of the substantial holder on 29/10/2026",
             "2. Previous and present voting power", "Ordinary 1 6.20% 2 7.40%"],
    "1005": ["Form 604", "1. Details of substantial holder (1)", "Name WAM Capital Limited",
             "There was a change in the interests of the substantial holder on 29/10/2026",
             "2. Previous and present voting power", "Ordinary 1 8.00% 2 9.10%"],
}


class FakeASX:
    def __init__(self, listing=LISTING, fail_ids=(), lists_down=False):
        self.listing, self.fail_ids, self.lists_down = listing, set(fail_ids), lists_down
        self.pdf_downloads = []

    def __call__(self, url, *, max_bytes=None, tries=3, timeout=60):
        if url.endswith("Anns.do"):
            if self.lists_down:
                raise scrape.FetchError("HTTP 503")
            return scrape.Fetched(url, self.listing.encode(), "text/html; charset=utf-8")
        if "idsId=" in url:
            i = url.split("idsId=")[1]
            self.pdf_downloads.append(i)
            if i in self.fail_ids:
                raise scrape.FetchError("HTTP 500")
            if i == "1003":   # 模拟“同意条款”中间页
                return scrape.Fetched(url, f'<input name="pdfURL" value="/asxpdf/20261030/pdf/{i}.pdf">'.encode(), "text/html")
            return scrape.Fetched(url, make_pdf(PDFS[i]), "application/pdf")
        if url.endswith(".pdf"):
            i = url.rsplit("/", 1)[1][:-4]
            return scrape.Fetched(url, make_pdf(PDFS[i]), "application/pdf")
        raise AssertionError("unexpected url " + url)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    (tmp_path / "scraper").mkdir()
    (tmp_path / "data").mkdir()
    shutil.copy(ROOT / "scraper" / "watchlist.json", tmp_path / "scraper" / "watchlist.json")
    monkeypatch.setattr(scrape, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(scrape, "NOTICES_FILE", tmp_path / "data" / "notices.json")
    monkeypatch.setattr(scrape, "STORE_FILE", tmp_path / "data" / "all_notices.json")
    monkeypatch.setattr(scrape, "LEGACY_SEEN_FILE", tmp_path / "data" / "seen.json")
    monkeypatch.setattr(scrape, "WATCH_FILE", tmp_path / "scraper" / "watchlist.json")
    monkeypatch.setattr(scrape, "PAUSE", 0)
    return tmp_path


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_main_end_to_end(sandbox, monkeypatch):
    fake = FakeASX()
    monkeypatch.setattr(scrape, "fetch", fake)
    assert scrape.main() == 0
    out = read(sandbox / "data" / "notices.json")
    got = {n["code"]: n for n in out["notices"]}
    assert set(got) == {"SLC", "XYZ", "WAX"}
    assert got["SLC"]["holder"] == "Paradice Investment Management" and got["SLC"]["voting_power"] == [5.12]
    assert got["SLC"]["company"] == "Superloop Limited" and got["SLC"]["change_date"] == "2026-10-28"
    assert got["XYZ"]["pdf_url"].endswith("/asxpdf/20261030/pdf/1003.pdf")
    assert got["WAX"]["holder"] == "Wilson Asset Management" and got["WAX"]["confidence"] == "高"
    assert "1004" not in fake.pdf_downloads              # MUFG 噪音不下载
    assert out["last_run"]["ok"] and out["last_run"]["skipped_noise"] == 1
    assert [n["time"] for n in out["notices"]] == sorted([n["time"] for n in out["notices"]], reverse=True)
    # 第二次运行：不重复下载
    fake.pdf_downloads.clear()
    assert scrape.main() == 0 and fake.pdf_downloads == []


def test_watchlist_change_is_retroactive(sandbox, monkeypatch):
    fake = FakeASX()
    monkeypatch.setattr(scrape, "fetch", fake)
    scrape.main()
    w = read(sandbox / "scraper" / "watchlist.json")
    w["holders"].append({"name": "Some Random Fund", "aliases": ["Some Random Fund"], "hints": []})
    (sandbox / "scraper" / "watchlist.json").write_text(json.dumps(w), encoding="utf-8")
    fake.pdf_downloads.clear()
    scrape.main()
    codes = {n["code"] for n in read(sandbox / "data" / "notices.json")["notices"]}
    assert "ABC" in codes and fake.pdf_downloads == []


def test_failed_download_is_retried_then_given_up(sandbox, monkeypatch):
    fake = FakeASX(fail_ids={"1001"})
    monkeypatch.setattr(scrape, "fetch", fake)
    for _ in range(scrape.MAX_ATTEMPTS + 1):
        scrape.main()
    assert fake.pdf_downloads.count("1001") == scrape.MAX_ATTEMPTS
    fake.fail_ids.clear()
    store = read(sandbox / "data" / "all_notices.json")
    assert store["entries"]["1001"]["status"] == "error"


def test_lists_down_returns_failure_but_keeps_data(sandbox, monkeypatch):
    monkeypatch.setattr(scrape, "fetch", FakeASX())
    scrape.main()
    monkeypatch.setattr(scrape, "fetch", FakeASX(lists_down=True))
    assert scrape.main() == 2
    out = read(sandbox / "data" / "notices.json")
    assert out["last_run"]["ok"] is False and out["last_run"]["errors"]
    assert len(out["notices"]) == 3                       # 旧数据还在


def test_max_pdfs_defers_rest(sandbox, monkeypatch):
    fake = FakeASX()
    monkeypatch.setattr(scrape, "fetch", fake)
    monkeypatch.setattr(scrape, "MAX_PDFS", 1)
    scrape.main()
    assert len(fake.pdf_downloads) == 1
    assert fake.pdf_downloads == ["1005"]                 # 标题带关注名单缩写的优先处理
    assert read(sandbox / "data" / "notices.json")["last_run"]["deferred"] == 3


def test_legacy_v1_data_is_migrated(sandbox, monkeypatch):
    legacy = {"updated_at": "2026-10-30T20:00:00+11:00", "scanned_days": ["2026-10-29"],
              "notices": [{"id": "999", "code": "OLD", "date": "2026-10-29", "time": "4:10PM",
                           "headline": "Becoming a substantial holder", "url": "https://www.asx.com.au/x",
                           "type": "becoming", "holder": "L1 Capital", "confidence": "高", "voting_power": [5.5]}]}
    (sandbox / "data" / "notices.json").write_text(json.dumps(legacy), encoding="utf-8")
    (sandbox / "data" / "seen.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(scrape, "fetch", FakeASX())
    scrape.main()
    out = read(sandbox / "data" / "notices.json")
    assert "OLD" in {n["code"] for n in out["notices"]}
    assert not (sandbox / "data" / "seen.json").exists()


# ---------------------------------------------------------------- 第二轮审查补充的测试

def test_column_layout_name_and_date():
    # PDF 按列排版：标签在前，值被挤到后面几行
    lines = ["1. Details of substantial holder (1)", "Name", "ACN/ARSN (if applicable)",
             "The holder became a substantial holder on", "Paradice Investment Management Pty Ltd",
             "090 148 619", "28/10/2026", "2. Details of voting power", "Ordinary 25,000 5.12%"]
    t = text_of(lines)
    name, _ = scrape.parse_holder_section(t)
    assert name == "Paradice Investment Management Pty Ltd"
    assert scrape.parse_details(t, "becoming")["change_date"] == "2026-10-28"


def test_label_lines_are_not_taken_as_names():
    t = text_of(["1. Details of substantial holder (1)", "Name", "ACN/ARSN (if applicable)",
                 "There was a change in the interests of the substantial holder on", "27/10/2026"])
    assert scrape.parse_holder_section(t)[0] == ""


def test_region_stops_at_section_two():
    t = text_of(["1. Details of substantial holder (1)", "Holder: Vanguard Group",
                 "2. Details of voting power", "Shares in Washington H. Soul Pattinson and Company Limited 5.2%"])
    name, region = scrape.parse_holder_section(t)
    assert name == "" and "Soul Pattinson" not in region
    assert scrape.match_entry({"holder_name": name, "region": region, "hint": ""}, HOLDERS) == (None, "")


def test_company_line_variants():
    assert scrape.parse_company("To: Company Name / Scheme: Superloop Limited ACN 169 263 094") == "Superloop Limited"
    assert scrape.parse_company("To Company Name/Scheme\nSuperloop Limited\n") == "Superloop Limited"


def test_pdf_link_prefers_announcement_over_other_pdfs():
    html = '<a href="/terms-of-use.pdf">Terms</a><script>u="/asxpdf/20261030/pdf/real.pdf"</script>'
    assert scrape.resolve_pdf_link(html, "https://www.asx.com.au/x").endswith("/asxpdf/20261030/pdf/real.pdf")


def test_unexpected_error_in_one_notice_does_not_abort_run(sandbox, monkeypatch):
    fake = FakeASX()
    monkeypatch.setattr(scrape, "fetch", fake)
    real = scrape.parse_company

    def boom(text):
        if "Superloop" in text:
            raise ValueError("模拟解析 bug")
        return real(text)

    monkeypatch.setattr(scrape, "parse_company", boom)
    assert scrape.main() == 0
    out = read(sandbox / "data" / "notices.json")
    assert {n["code"] for n in out["notices"]} == {"XYZ", "WAX"}     # 其他公告照常处理
    assert any("下载失败" in e or "重试" in e for e in out["last_run"]["errors"])
    store = read(sandbox / "data" / "all_notices.json")
    assert store["entries"]["1001"]["status"] == "error" and "ValueError" in store["entries"]["1001"]["error"]


def test_substantial_count_is_unique(sandbox, monkeypatch):
    monkeypatch.setattr(scrape, "fetch", FakeASX())
    scrape.main()
    assert read(sandbox / "data" / "notices.json")["last_run"]["substantial_found"] == 5


def test_names_starting_with_the_are_kept():
    t = text_of(["1. Details of substantial holder (1)", "Name The Vanguard Group, Inc."])
    assert scrape.parse_holder_section(t)[0] == "The Vanguard Group, Inc."
