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
    "1004": ["Form 603", "To Company Name/Scheme GMD Limited", "1. Details of substantial holder (1)",
             "Name Mitsubishi UFJ Financial Group, Inc.", "The holder became a substantial holder on 28/10/2026",
             "2. Details of voting power", "Ordinary 1 5.30%"],
    "1005": ["Form 604", "1. Details of substantial holder (1)", "Name WAM Capital Limited",
             "There was a change in the interests of the substantial holder on 29/10/2026",
             "2. Previous and present voting power", "Ordinary 1 8.00% 2 9.10%"],
}


class FakeASX:
    def __init__(self, listing=LISTING, fail_ids=(), lists_down=False, prev_listing=None, broken_ids=(),
                 api_items=(), api_down=False, api_pdfs=None, prices=None, events=None, sectors=None, years=None):
        self.listing, self.fail_ids, self.lists_down = listing, set(fail_ids), lists_down
        self.prev_listing, self.broken_ids = prev_listing, set(broken_ids)
        self.api_items, self.api_down, self.api_pdfs = list(api_items), api_down, api_pdfs or {}
        self.pdf_downloads = []
        self.api_pages = []
        self.prices = prices or {}          # 股票代码 → 日线 [[毫秒, 收盘, 成交量], ...]；没有的代码返回 404
        self.price_calls = []
        self.events, self.sectors, self.event_calls = events or {}, sectors or {}, []
        self.years, self.year_calls = years or {}, []

    def __call__(self, url, *, max_bytes=None, tries=3, timeout=60):
        if "/chart/highcharts" in url:
            code = url.split("asx_code=")[1].split("&")[0]
            self.price_calls.append(code)
            if code not in self.prices:
                raise scrape.FetchError("HTTP 404")
            return scrape.Fetched(url, json.dumps(self.prices[code]).encode(), "application/json")
        if "announcements.do?by=asxCode" in url and "timeframe=Y" in url:
            key = (url.split("asxCode=")[1].split("&")[0], int(url.split("year=")[1].split("&")[0]))
            self.year_calls.append(key)
            if key not in self.years:
                return scrape.Fetched(url, b"<html><h2>Search results</h2>No announcements</html>", "text/html")
            return scrape.Fetched(url, self.years[key].encode(), "text/html; charset=utf-8")
        if "announcements.do?by=asxCode" in url:
            code = url.split("asxCode=")[1].split("&")[0]
            self.event_calls.append(code)
            if code not in self.events:
                raise scrape.FetchError("HTTP 404")
            return scrape.Fetched(url, self.events[code].encode(), "text/html; charset=utf-8")
        if "/companies/" in url and url.endswith("/header"):
            code = url.split("/companies/")[1].split("/")[0]
            if code not in self.sectors:
                raise scrape.FetchError("HTTP 404")
            return scrape.Fetched(url, json.dumps({"data": {"sector": self.sectors[code]}}).encode(), "application/json")
        if url.startswith(scrape.MARKET_API):
            if self.api_down:
                raise scrape.FetchError("HTTP 503")
            page = int(url.split("page=")[1].split("&")[0])
            size = int(url.split("itemsPerPage=")[1].split("&")[0])
            self.api_pages.append(page)
            body = {"data": {"count": len(self.api_items), "items": self.api_items[page * size:(page + 1) * size]}}
            return scrape.Fetched(url, json.dumps(body).encode(), "application/json; charset=utf-8")
        if url.startswith(scrape.FILE_API):
            key = url[len(scrape.FILE_API):]
            self.pdf_downloads.append("mk-" + key)
            return scrape.Fetched(url, make_pdf(self.api_pdfs[key]), "application/pdf")
        if url.endswith("Anns.do"):
            if self.lists_down:
                raise scrape.FetchError("HTTP 503")
            page = self.prev_listing if url.endswith("prevBusDayAnns.do") and self.prev_listing is not None else self.listing
            return scrape.Fetched(url, page.encode(), "text/html; charset=utf-8")
        if "idsId=" in url:
            i = url.split("idsId=")[1]
            self.pdf_downloads.append(i)
            if i in self.fail_ids:
                raise scrape.FetchError("HTTP 500")
            if i in self.broken_ids:   # 模拟下载不完整的 PDF
                return scrape.Fetched(url, b"%PDF-1.4\n1 0 obj <<", "application/pdf")
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
    monkeypatch.setattr(scrape, "HISTORY_FILE", tmp_path / "data" / "history.json")
    monkeypatch.setattr(scrape, "CALIBRATION_FILE", tmp_path / "data" / "calibration.json")
    monkeypatch.setattr(scrape, "REPORTS_FILE", tmp_path / "data" / "fund_reports.json")
    monkeypatch.setattr(scrape, "LEGACY_SEEN_FILE", tmp_path / "data" / "seen.json")
    monkeypatch.setattr(scrape, "WATCH_FILE", tmp_path / "scraper" / "watchlist.json")
    monkeypatch.setattr(scrape, "PAUSE", 0)
    monkeypatch.setattr(scrape, "WORKERS", 1)        # 测试默认单线程，下载顺序固定；并行另有专门的测试
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
    assert "ABC" in codes
    # 新命中的那一份会再下载一次，用来读交易明细（操作解读要用）；其他的不重复下载
    assert len(fake.pdf_downloads) == 1
    abc = next(e for e in read(sandbox / "data" / "all_notices.json")["entries"].values() if e["code"] == "ABC")
    assert abc["detail_v"] == scrape.INSIGHT_VERSION
    fake.pdf_downloads.clear()
    scrape.main()
    assert fake.pdf_downloads == []


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


# ---------------------------------------------------------------- 真实 ASX PDF 回归测试
# 2026-11-02 第一次真实运行时，约 4 成公告解析不出申报方，变动日也有取错的。
# 下面的样本取自当天真实公告（只保留前两页），每个对应一种排版问题。

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def real(name: str, ntype: str) -> dict:
    texts = scrape.pdf_texts((FIXTURES / name).read_bytes())
    return scrape.analyse_texts(texts, ntype)


def test_real_template_form_name_and_values():
    # 模板填写式 604：默认提取时“Name”和名称分家，变动日曾被取成上一次申报的日期
    r = real("604_template_jbh.pdf", "change")
    assert r["holder_name"].startswith("State Street Corporation")
    assert r["voting_power"] == [8.78, 7.75]
    assert r["change_date"] == "2026-10-29"
    assert r["company"] == "JB HI-FI LIMITED"


def test_real_template_form_603():
    r = real("603_template_sgm.pdf", "becoming")
    assert r["holder_name"].startswith("State Street Corporation")
    assert r["voting_power"] == [5.1] and r["change_date"] == "2026-10-29"


def test_real_split_letters_falls_back_to_plain_text():
    # 版面模式把字拆开（“Nam e M ercer”），要靠默认模式拿到正确名称，不能被乱码抢先
    r = real("604_split_letters_ppe.pdf", "change")
    assert r["holder_name"].startswith("Mercer Investments (Australia) Limited")
    assert r["voting_power"] == [6.546, 7.562] and r["change_date"] == "2026-10-29"


def test_real_name_above_label():
    r = real("605_name_above_label_ael.pdf", "ceasing")
    assert r["holder_name"].startswith("Pinnacle Investment Management Group Limited")
    assert r["change_date"] == "2026-10-29" and r["voting_power"] == []


def test_real_spaced_date():
    # “29 /10/2026”多了空格，曾被跳过而取到上一次申报的 27/10
    r = real("604_spaced_date_bap.pdf", "change")
    assert r["change_date"] == "2026-10-29"
    assert r["holder_name"].startswith("Tanarra Capital Australia Pty Ltd")
    assert r["voting_power"] == [12.98, 10.34]


def test_real_custom_format_without_name_label():
    r = real("604_no_name_label_elv.pdf", "change")
    assert r["holder_name"].startswith("Citigroup Global Markets Australia Pty Limited")
    assert r["voting_power"] == [8.227, 9.6699]


def test_real_scanned_pdf_is_unreadable():
    # 扫描件只剩 ASX 水印，应判为读不出文字，交给标题兜底匹配
    assert scrape.pdf_texts((FIXTURES / "603_scanned_bry.pdf").read_bytes()) == []


def test_old_parser_entries_are_reparsed(sandbox, monkeypatch):
    monkeypatch.setattr(scrape, "fetch", FakeASX())
    scrape.main()
    store = read(sandbox / "data" / "all_notices.json")
    e = store["entries"]["1001"]
    assert e["status"] == "parsed" and e["parser"] == scrape.PARSER_VERSION
    e["parser"] = 1                                   # 模拟旧版解析器留下的记录
    e["holder_name"] = ""
    (sandbox / "data" / "all_notices.json").write_text(json.dumps(store), encoding="utf-8")
    scrape.main()
    e2 = read(sandbox / "data" / "all_notices.json")["entries"]["1001"]
    assert e2["parser"] == scrape.PARSER_VERSION and e2["holder_name"]


# ---------------------------------------------------------------- 第四轮审查（2026-11-03）补充的测试

def test_multi_row_table_uses_grand_total():
    # CFS 母公司新进 Karoon：0.05% / 4.95% 两个分项 + GRAND TOTAL 5.00%，以前取成了 0.05%
    r = real("603_grand_total_cfs.pdf", "becoming")
    assert r["holder_name"].startswith("Superannuation and Investments HoldCo Pty Ltd")
    assert r["voting_power"] == [5.0] and r["change_date"] == "2026-11-02"


def test_percent_sign_only_in_header():
    # 表头写 “Voting Power (%)”，数字 17.57 / 18.58 不带 % 号，以前一个都读不出来
    r = real("604_bare_percent_amx.pdf", "change")
    assert r["voting_power"] == [17.57, 18.58] and r["change_date"] == "2026-11-02"
    assert scrape.match_entry({"holder_name": r["holder_name"]}, HOLDERS) == ("Perennial", "高")


def test_six_decimal_percentages_and_2018_form():
    r = real("604_six_decimals_ctt.pdf", "change")
    assert r["voting_power"] == [5.298524, 6.323057]
    assert r["company"] == "Cettire Limited" and r["change_date"] == "2026-11-02"


def test_nz_fmc_act_form():
    r = real("nz_fmc_gtk.pdf", "change")
    assert r["holder_name"].startswith("Mitsubishi UFJ Financial Group")
    assert r["voting_power"] == [6.584, 5.368] and r["change_date"] == "2026-10-29"
    assert r["company"] == "Gentrack Group Ltd"


def test_irish_tr1_form():
    r = real("tr1_jhx.pdf", "change")
    assert r["holder_name"] == "D1 Capital Partners L.P."
    assert r["voting_power"] == [5.0161, 4.3083] and r["change_date"] == "2026-10-27"
    assert scrape.clean_company(r["company"]) == "James Hardie Industries plc"


def test_company_line_variants_real():
    assert real("604_to_colon_hlo.pdf", "change")["company"] == "HELLOWORLD TRAVEL LIMITED"
    r = real("604_australiansuper_pwh.pdf", "change")     # 没有 “To” 的写法；也是养老金申报的真实样本
    assert r["company"] == "PWR Holdings Limited" and r["holder_name"] == "AustralianSuper Pty Ltd"
    assert r["voting_power"] == [7.07, 5.98] and r["change_date"] == "2026-10-28"


def test_clean_company():
    assert scrape.clean_company("Energy One Limited (ASX: EOL)") == "Energy One Limited"
    assert scrape.clean_company("NRW Holdings Limited NWH.AX") == "NRW Holdings Limited"
    assert scrape.clean_company("Elevra Lithium Ltd (“ELV”, Ordinary Fu") == "Elevra Lithium Ltd"
    assert scrape.clean_company("Superloop Limited") == "Superloop Limited"


def test_infer_type_from_form():
    assert scrape.infer_type("Form 604\nCorporations Act 2001\nNotice of change of interests") == "change"
    assert scrape.infer_type("Form 603 Notice of initial substantial holder") == "becoming"
    assert scrape.infer_type("Disclosure of movement of 1% or more in substantial holding") == "change"
    assert scrape.infer_type("Annual Report") == ""


def test_abbreviation_hints_are_case_sensitive():
    assert scrape.is_noise("MS", HOLDERS, NOISE)
    assert not scrape.is_noise("Ms J Smith", HOLDERS, NOISE)          # 以前被当成 Morgan Stanley 跳过
    assert scrape.match_entry({"hint": "TOP"}, HOLDERS) == ("Thorney / Tiga Trading", "低")
    assert scrape.match_entry({"hint": "Top End Holdings Pty Ltd"}, HOLDERS) == (None, "")
    assert scrape.is_noise("Citigroup Global Markets", HOLDERS, NOISE)  # 非缩写仍然不分大小写


def test_target_code_for_notices_lodged_under_holder_code():
    base = {"status": "parsed", "holder_name": "Wilson Asset Management Group", "date": "2026-11-02", "type": "change"}
    out = scrape.build_notices({
        "a": {**base, "id": "a", "code": "WMI", "headline": "Change in substantial holding for EOL",
              "company": "Energy One Limited (ASX: EOL)"},
        "b": {**base, "id": "b", "code": "DXN", "headline": "Ceasing to be a substantial holder for DXN from WAM/WAA/WMI"},
    }, HOLDERS)
    by = {n["id"]: n for n in out}
    assert by["a"]["code"] == "EOL" and by["a"]["lodged_code"] == "WMI" and by["a"]["company"] == "Energy One Limited"
    assert by["b"]["code"] == "DXN" and by["b"]["lodged_code"] == ""


def test_noise_entries_are_reconsidered_after_list_change(sandbox, monkeypatch):
    fake = FakeASX()
    monkeypatch.setattr(scrape, "fetch", fake)
    scrape.main()
    assert "1004" not in fake.pdf_downloads
    w = read(sandbox / "scraper" / "watchlist.json")
    w["noise_hints"] = [x for x in w["noise_hints"] if x not in ("MUFG", "Mitsubishi")]
    w["holders"].append({"name": "MUFG", "aliases": ["Mitsubishi UFJ Financial Group"], "hints": ["MUFG"]})
    (sandbox / "scraper" / "watchlist.json").write_text(json.dumps(w), encoding="utf-8")
    fake.pdf_downloads.clear()
    scrape.main()
    assert fake.pdf_downloads == ["1004"]
    got = {n["code"]: n for n in read(sandbox / "data" / "notices.json")["notices"]}
    assert got["GMD"]["holder"] == "MUFG" and got["GMD"]["confidence"] == "高"


def test_broken_pdf_is_retried_not_marked_unreadable(sandbox, monkeypatch):
    fake = FakeASX(broken_ids={"1001"})
    monkeypatch.setattr(scrape, "fetch", fake)
    scrape.main()
    e = read(sandbox / "data" / "all_notices.json")["entries"]["1001"]
    assert e["status"] == "error" and "PDF 打不开" in e["error"]
    fake.broken_ids.clear()
    scrape.main()
    e = read(sandbox / "data" / "all_notices.json")["entries"]["1001"]
    assert e["status"] == "parsed" and e["holder_name"].startswith("Paradice")


EMPTY_LISTING = "<table><tr><th>ASX Code</th></tr></table>"


def test_today_fetched_before_close_is_partial(sandbox, monkeypatch):
    fake = FakeASX(prev_listing=EMPTY_LISTING)          # 只有“今日公告”有数据
    monkeypatch.setattr(scrape, "fetch", fake)
    monkeypatch.setattr(scrape, "now_syd", lambda: dt.datetime(2026, 10, 30, 14, 5, tzinfo=scrape.SYD))
    scrape.main()
    out = read(sandbox / "data" / "notices.json")
    assert out["partial_days"] == {"2026-10-30": "14:05"}
    assert "2026-10-30" in out["scanned_days"]           # 覆盖天数照算，另外单独提示“只抓到 14:05”
    # 当天晚上 9 点后再抓一次就算完整
    monkeypatch.setattr(scrape, "now_syd", lambda: dt.datetime(2026, 10, 30, 21, 30, tzinfo=scrape.SYD))
    scrape.main()
    out = read(sandbox / "data" / "notices.json")
    assert out["partial_days"] == {} and out["scanned_days"] == ["2026-10-30"]


def test_previous_business_day_page_completes_a_partial_day(sandbox, monkeypatch):
    monkeypatch.setattr(scrape, "fetch", FakeASX(prev_listing=EMPTY_LISTING))
    monkeypatch.setattr(scrape, "now_syd", lambda: dt.datetime(2026, 10, 30, 11, 0, tzinfo=scrape.SYD))
    scrape.main()
    # 下一个交易日：上一交易日列表里是 30 日的全部公告
    monkeypatch.setattr(scrape, "fetch", FakeASX(listing=EMPTY_LISTING, prev_listing=LISTING))
    monkeypatch.setattr(scrape, "now_syd", lambda: dt.datetime(2026, 11, 2, 9, 0, tzinfo=scrape.SYD))
    scrape.main()
    out = read(sandbox / "data" / "notices.json")
    assert out["partial_days"] == {} and "2026-10-30" in out["scanned_days"]


def test_2018_form_labels_are_not_names():
    # 2018 版表格多了 “NFPFRN (if applicable)” 一栏，曾被当成申报方名称（TVL 公告），挡住了后面的匹配
    t = text_of(["1. Details of substantial holder (1)", "Name", "ACN/ARSN/APFRN (if applicable)",
                 "NFPFRN (if applicable) Not applicable", "The holder became a substantial holder on 28/10/2026"])
    assert scrape.parse_holder_section(t)[0] == ""
    t = text_of(["1. Details of substantial holder (1)", "Name UDN Eden Management LTD and persons listed in Annexure A",
                 "ACN/ARSN/APFRN (if applicable) Not applicable", "NFPFRN (if applicable) Not applicable"])
    assert scrape.parse_holder_section(t)[0] == "UDN Eden Management LTD and persons listed in Annexure A"


# ---------------------------------------------------------------- 养老金（2026-11-03 加入）

SUPER_NAMES = {
    "AustralianSuper Pty Ltd": "AustralianSuper",
    "Australian Retirement Trust Pty Ltd as trustee for the Australian Retirement Trust": "Australian Retirement Trust",
    "Aware Super Pty Ltd as trustee of Aware Super": "Aware Super",
    "UniSuper Limited as trustee for UniSuper and UniSuper Management Pty Limited": "UniSuper",
    "Host-Plus Pty Limited": "Hostplus",
    "United Super Pty Ltd as trustee for the Construction and Building Unions Superannuation Fund": "Cbus",
    "H.E.S.T. Australia Ltd as trustee for HESTA": "HESTA",
    "Retail Employees Superannuation Pty Limited": "Rest",
    "Superannuation and Investments HoldCo Pty Ltd and its related bodies corporate": "Colonial First State (CFS)",
    "Commonwealth Superannuation Corporation": "CSC（公务员和军人养老金）",
    "NULIS Nominees (Australia) Limited": "Insignia / MLC",
    "Mercer Investments (Australia) Limited as Trustee of WSSP Australian Equities Trust": "Mercer",
    "Future Fund Board of Guardians": "Future Fund",
}


def test_super_funds_match_their_trustee_names():
    groups = {h.name: h.group for h in HOLDERS}
    assert sum(g == "养老金" for g in groups.values()) == 13 and sum(g == "主动基金" for g in groups.values()) == 10
    for name, want in SUPER_NAMES.items():
        assert scrape.match_entry({"holder_name": name}, HOLDERS) == (want, "高"), name
        assert groups[want] == "养老金"


def test_super_funds_no_false_positives():
    for name in ("GZ FAMILY HOLDINGS PTY LTD ATF GZ FAMILY TRUST and GZ SUPER", "Commonwealth Bank of Australia",
                 "Kefford Holdings Pty Ltd ATF The Kefford Family Super Fund", "Rest Easy Holdings Pty Ltd",
                 "Superloop Limited", "Art Gallery Investments Pty Ltd", "Awareness Capital Pty Ltd"):
        assert scrape.match_entry({"holder_name": name}, HOLDERS) == (None, ""), name
    assert scrape.match_entry({"hint": "ART"}, HOLDERS) == ("Australian Retirement Trust", "低")
    assert scrape.match_entry({"hint": "Art Smith"}, HOLDERS) == (None, "")
    assert scrape.match_entry({"hint": "Restaurant Group"}, HOLDERS) == (None, "")


def test_super_funds_real_pdfs_and_group_in_output():
    pwh = real("604_australiansuper_pwh.pdf", "change")
    cfs = real("603_grand_total_cfs.pdf", "becoming")
    out = scrape.build_notices({
        "1": {"id": "1", "code": "PWH", "date": "2026-10-30", "headline": "Change in substantial holding", "status": "parsed", **pwh},
        "2": {"id": "2", "code": "KAR", "date": "2026-11-03", "headline": "Becoming a substantial holder", "status": "parsed", **cfs},
    }, HOLDERS)
    by = {n["code"]: n for n in out}
    assert by["PWH"]["holder"] == "AustralianSuper" and by["PWH"]["group"] == "养老金" and by["PWH"]["voting_power"] == [7.07, 5.98]
    assert by["KAR"]["holder"] == "Colonial First State (CFS)" and by["KAR"]["voting_power"] == [5.0]


def test_groups_written_to_notices_json(sandbox, monkeypatch):
    monkeypatch.setattr(scrape, "fetch", FakeASX())
    scrape.main()
    out = read(sandbox / "data" / "notices.json")
    assert out["groups"]["AustralianSuper"] == "养老金" and out["groups"]["L1 Capital"] == "主动基金"
    assert all(n["group"] == "主动基金" for n in out["notices"])


# ---------------------------------------------------------------- OCR（扫描件文字识别）

needs_ocr = pytest.mark.skipif(not scrape.ocr_available(), reason="没有安装 tesseract / pypdfium2")
SCANNED = (FIXTURES / "604_scanned_fgr.pdf").read_bytes()


@needs_ocr
def test_ocr_reads_real_scanned_form():
    assert scrape.pdf_texts(SCANNED) == []                 # 没有文字层
    texts = scrape.ocr_texts(SCANNED)
    r = scrape.analyse_texts(texts, "change", ocr=True)
    assert r["holder_name"].startswith("Specialty Materials Investments")
    assert r["voting_power"] == [7.39, 6.38] and r["change_date"] == "2026-10-30"
    assert r["company"] == "First Graphene Limited"


@needs_ocr
def test_scanned_notice_end_to_end(sandbox, monkeypatch):
    fake = FakeASX()
    real_call = fake.__call__

    def serve(url, **kw):
        if url.endswith("idsId=1002"):
            fake.pdf_downloads.append("1002")
            return scrape.Fetched(url, SCANNED, "application/pdf")
        return real_call(url, **kw)

    monkeypatch.setattr(scrape, "fetch", serve)
    scrape.main()
    e = read(sandbox / "data" / "all_notices.json")["entries"]["1002"]
    assert e["status"] == "parsed" and e["ocr"] is True and e["holder_name"].startswith("Specialty Materials")


def fake_ocr(monkeypatch, text_lines):
    monkeypatch.setattr(scrape, "ocr_available", lambda: True)
    monkeypatch.setattr(scrape, "ocr_texts", lambda data, pages=3: [scrape.clean_text("\n".join(text_lines))])
    monkeypatch.setattr(scrape, "pdf_texts", lambda data, pages=2: [])          # 全部当成扫描件


SCAN_TEXT = ["To Company Name/Scheme Superloop Limited", "1. Details of substantial holder (1)",
             "tame Paradice Investment Management Pty Ltd", "There was a change in the interests ofthe 29/10/2026",
             "substantial holder on", "The previous notice was dated 01/09/2026", "2. Previous and present voting power",
             "Ordinary 1,000 6.10% 2,000 7.20%", "3. Changes in relevant interests"]


def test_ocr_budget_defers_rest_without_counting_attempts(sandbox, monkeypatch):
    fake_ocr(monkeypatch, SCAN_TEXT)
    monkeypatch.setattr(scrape, "fetch", FakeASX())
    monkeypatch.setattr(scrape, "MAX_OCR", 1)
    scrape.main()
    entries = read(sandbox / "data" / "all_notices.json")["entries"]
    st = sorted(e["status"] for k, e in entries.items() if k != "1004")
    assert st == ["new", "new", "new", "parsed"]
    assert all(e["attempts"] == 0 for e in entries.values() if e["status"] == "new" and e["id"] != "1004")
    assert read(sandbox / "data" / "notices.json")["last_run"]["deferred"] == 3
    monkeypatch.setattr(scrape, "MAX_OCR", 60)
    scrape.main()
    entries = read(sandbox / "data" / "all_notices.json")["entries"]
    assert all(e["status"] in ("parsed", "noise") for e in entries.values())
    assert all(e.get("ocr") for e in entries.values() if e["status"] == "parsed")


def test_ocr_tolerant_parsing(sandbox, monkeypatch):
    fake_ocr(monkeypatch, SCAN_TEXT)
    monkeypatch.setattr(scrape, "fetch", FakeASX())
    scrape.main()
    got = {n["code"]: n for n in read(sandbox / "data" / "notices.json")["notices"]}
    slc = got["SLC"]                                       # “tame” = 认错的 “Name”；“ofthe” 少了空格
    assert slc["holder"] == "Paradice Investment Management" and slc["ocr"] is True
    assert slc["change_date"] == "2026-10-29"


def test_without_tesseract_scans_stay_unreadable(sandbox, monkeypatch):
    monkeypatch.setattr(scrape, "ocr_available", lambda: False)
    monkeypatch.setattr(scrape, "pdf_texts", lambda data, pages=2: [])
    monkeypatch.setattr(scrape, "fetch", FakeASX())
    assert scrape.main() == 0
    entries = read(sandbox / "data" / "all_notices.json")["entries"]
    assert {e["status"] for k, e in entries.items() if k != "1004"} == {"unreadable"}
    # 低可信度：标题带 “from WAM” 的仍然能匹配
    got = {n["code"]: n for n in read(sandbox / "data" / "notices.json")["notices"]}
    assert got["WAX"]["confidence"] == "低"


def test_ocr_garbled_name_does_not_block_other_clues():
    assert scrape.match_entry({"holder_name": "Ra int Fer eased ios", "ocr": True, "hint": "WAM"}, HOLDERS) == ("Wilson Asset Management", "低")
    assert scrape.match_entry({"holder_name": "Ra int Fer eased ios", "hint": "WAM"}, HOLDERS) == (None, "")
    assert scrape.match_entry({"holder_name": "Ra int Fer", "ocr": True, "region": "Name Regal Funds Management Pty"}, HOLDERS) \
        == ("Regal Funds Management", "中")


def test_name_on_section_header_line_and_missing_spaces():
    t = text_of(["1. Details of substantial holder (1) MR ANGELO KOTSES", "Name", "ACN/ARSN/APEFRN (if applicable)",
                 "NFPFRN (if applicable)", "There was a change in the interests ofthe 30/10/2026", "substantial holder on",
                 "The previous notice was dated 24/03/2026"])
    assert scrape.parse_holder_section(t)[0] == "MR ANGELO KOTSES"
    assert scrape.parse_details(t, "change")["change_date"] == "2026-10-30"


def test_section_header_split_by_ocr():
    t = "1. Details of substantial\nSet\ntame Specialty Materials Investments, LLC\nACN/ARSN (if applicable) N/A"
    assert scrape.parse_holder_section(t, first_line=False, ocr=True)[0] == "Specialty Materials Investments, LLC"
    # 正常文字版里，“details of substantial holding” 这类封面信措辞不能当成第一节
    letter = "Please find attached details of substantial holding.\nRegards\nJohn"
    assert scrape.parse_holder_section(letter) == ("", "")


def test_ocr_number_safeguards():
    assert scrape.parse_date("on 03/04/2626 and 3 April 2626") == ""          # 2026 被认成 2626
    assert scrape.parse_date("03/04/2626 then 01/04/2026") == "2026-04-01"
    seg = "2. Details of voting power\nOrdinary 31,181,215 te1.215\n"
    assert scrape.pick_voting_power(seg, "becoming", True, ocr=True) == []      # 扫描件不认不带 % 的数字
    assert scrape.pick_voting_power("2. Details of voting power\nOrdinary 1 1.21%\n", "becoming", True) == []   # 新进必然 ≥5%
    assert scrape.pick_voting_power("x\nOrdinary 1 73.54% 2 3.1%\n", "change", True, ocr=True) == []
    assert scrape.pick_voting_power("x\nOrdinary 1 73.54% 2 50.59%\n", "change", True, ocr=True) == [73.54, 50.59]


# ---------------------------------------------------------------- 第 2 轮审查：OCR 的异常情况

def test_ocr_env_failure_is_retried_when_ocr_works_again(sandbox, monkeypatch):
    monkeypatch.setattr(scrape, "ocr_available", lambda: True)
    monkeypatch.setattr(scrape, "pdf_texts", lambda data, pages=2: [])

    def broken(data, pages=3):
        raise scrape.OCREnvError("tesseract 每一页都报错")

    monkeypatch.setattr(scrape, "ocr_texts", broken)
    monkeypatch.setattr(scrape, "fetch", FakeASX())
    scrape.main()
    e = read(sandbox / "data" / "all_notices.json")["entries"]["1001"]
    assert e["status"] == "unreadable" and e["ocr_tried"] is False
    fake_ocr(monkeypatch, SCAN_TEXT)                       # 下一次运行 OCR 正常了
    scrape.main()
    e = read(sandbox / "data" / "all_notices.json")["entries"]["1001"]
    assert e["status"] == "parsed" and e["ocr"] is True and e["holder_name"].startswith("Paradice")


def test_pdf_that_pdfium_cannot_open_is_unreadable_not_retried(sandbox, monkeypatch):
    import pypdfium2

    def locked(*a, **k):
        raise pypdfium2.PdfiumError("Incorrect password error")

    monkeypatch.setattr(scrape, "pdf_texts", lambda data, pages=2: [])
    monkeypatch.setattr(pypdfium2, "PdfDocument", locked)
    monkeypatch.setattr(scrape, "ocr_available", lambda: True)
    monkeypatch.setattr(scrape, "fetch", FakeASX())
    scrape.main()
    out = read(sandbox / "data" / "notices.json")
    e = read(sandbox / "data" / "all_notices.json")["entries"]["1001"]
    assert e["status"] == "unreadable" and e["ocr_tried"] is True
    assert out["last_run"]["errors"] == []                # 不会每次都报“下载失败”


def test_ocr_time_budget_defers(sandbox, monkeypatch):
    fake_ocr(monkeypatch, SCAN_TEXT)
    monkeypatch.setattr(scrape, "fetch", FakeASX())
    monkeypatch.setattr(scrape, "OCR_TIME_BUDGET", -1)    # 时间已经用完
    scrape.main()
    out = read(sandbox / "data" / "notices.json")
    assert out["last_run"]["deferred"] == 4 and out["last_run"]["pdf_checked"] == 0
    assert all(e["status"] in ("new", "noise") for e in read(sandbox / "data" / "all_notices.json")["entries"].values())


def test_typed_cover_letter_with_scanned_form_uses_ocr(sandbox, monkeypatch):
    letter = "Dear Sir/Madam\nPlease find attached a notice of change of interests of substantial holder.\nYours faithfully"
    fake_ocr(monkeypatch, SCAN_TEXT)
    monkeypatch.setattr(scrape, "pdf_texts", lambda data, pages=2: [letter])
    monkeypatch.setattr(scrape, "fetch", FakeASX())
    scrape.main()
    e = read(sandbox / "data" / "all_notices.json")["entries"]["1002"]
    assert e["ocr"] is True and e["holder_name"].startswith("Paradice") and e["voting_power"] == [6.1, 7.2]


def test_text_pdf_with_form_does_not_trigger_ocr(sandbox, monkeypatch):
    calls = []
    monkeypatch.setattr(scrape, "ocr_available", lambda: True)
    monkeypatch.setattr(scrape, "ocr_texts", lambda data, pages=3: calls.append(1) or [])
    monkeypatch.setattr(scrape, "fetch", FakeASX())
    scrape.main()
    assert calls == []                                    # 有文字层、认得出第一节，就不浪费时间做 OCR
    e = read(sandbox / "data" / "all_notices.json")["entries"]["1001"]
    assert e["ocr"] is False and e["status"] == "parsed"


def test_ocr_result_without_anything_keeps_text_result(sandbox, monkeypatch):
    letter = "Dear Sir/Madam\nPlease find attached a notice.\nYours faithfully, The Company Secretary of Something"
    fake_ocr(monkeypatch, ["Illegible scan " * 5])
    monkeypatch.setattr(scrape, "pdf_texts", lambda data, pages=2: [letter])
    monkeypatch.setattr(scrape, "fetch", FakeASX())
    scrape.main()
    e = read(sandbox / "data" / "all_notices.json")["entries"]["1001"]
    assert e["status"] == "parsed" and e["ocr"] is False and e["ocr_tried"] is True
    scrape.main()                                         # 已经试过 OCR 的不会每次重读
    assert read(sandbox / "data" / "notices.json")["last_run"]["pdf_checked"] == 0


# ---------------------------------------------------------------- 第六轮（2026-11-04）：成功率和准确率

def test_more_date_formats():
    for s in ("on 28/October/2026", "on October 28, 2026", "on Oct. 28, 2026", "on // 28 10 2026", "on / / 28102026",
              "on 10/28/2026", "on 28-Oct-2026", "on 28th October 2026"):
        assert scrape.parse_date(s) == "2026-10-28", s
    assert scrape.parse_date("on 15 Sept 2026") == "2026-09-15"
    assert scrape.parse_date("ACN 062 576 238") == ""                     # 不会把 ACN 当成日期


def test_real_jpmorgan_and_morgan_stanley_dates():
    r = real("603_slash_month_jpm_flt.pdf", "becoming")                  # 28/October/2026
    assert r["holder_name"].startswith("JPMorgan Chase") and r["change_date"] == "2026-10-28" and r["voting_power"] == [5.23]
    r = real("604_us_date_ms_oml.pdf", "change")                         # October 28, 2026（美式）
    assert r["change_date"] == "2026-10-28" and r["voting_power"] == [8.97, 7.84]


def test_real_comb_field_dates():
    r = real("603_spaced_digits_tzn.pdf", "becoming")                    # “28 10 2026”
    assert r["change_date"] == "2026-10-28" and r["voting_power"] == [5.58]
    r = real("603_8digit_date_mlg.pdf", "becoming")                      # “28102026”
    assert r["change_date"] == "2026-10-28" and r["voting_power"] == [5.19]


def test_real_custom_forms_without_section_header():
    r = real("603_custom_name_perennial_pc2.pdf", "becoming")           # 以前漏掉的 Perennial 新进
    assert r["holder_name"] == "Perennial Value Management Limited"
    assert r["change_date"] == "2026-10-29" and r["voting_power"] == [5.06]
    assert scrape.match_entry({"holder_name": r["holder_name"]}, HOLDERS) == ("Perennial", "高")
    r = real("notice_custom_nyr.pdf", "becoming")
    assert r["holder_name"] == "John Moore" and r["change_date"] == "2026-10-30" and r["voting_power"] == [5.58]


def test_real_sec_13g():
    r = real("sec13g_aai.pdf", "becoming")
    assert r["holder_name"] == "Vanguard Portfolio Management" and r["voting_power"] == [5.04]
    assert r["change_date"] == "2026-09-30" and r["company"] == "Alcoa Corp"


def test_see_annexure_is_not_a_name():
    r = real("603_see_annexure_wmc.pdf", "becoming")
    assert r["holder_name"] == "" and r["voting_power"] == [29.5]
    assert not scrape.looks_like_name("See Annexure A") and not scrape.looks_like_name("Refer to Annexure B")


def test_fallback_name_ignores_cover_letter_and_signature():
    letter = "Dear Sir\nName: Jane Citizen\nCompany Secretary\nForm 603\nNotice of initial substantial holder\nName: Regal Funds Management Pty Ltd\n2. Details of voting power\nOrdinary 1 6.1%"
    assert scrape.fallback_name(letter) == "Regal Funds Management Pty Ltd"
    sig = "Form 604\nNotice of change of interests\n2. Previous and present voting power\nOrdinary 1 6% 2 7%\nSignature\nName: John Smith"
    assert scrape.fallback_name(sig) == ""


def test_new_aliases():
    assert scrape.match_entry({"holder_name": "L1 Group Limited"}, HOLDERS) == ("L1 Capital", "高")
    assert scrape.match_entry({"holder_name": "Wyloo Consolidated Investments Pty Ltd"}, HOLDERS) == ("Tattarang (Andrew Forrest)", "高")


def test_ocr_junk_and_section_two_variants():
    assert scrape.clean_name("> Regal Funds Management") == "Regal Funds Management"
    assert not scrape.looks_like_name(scrape.clean_name("> 2. Details of voting power"))
    t = "1. Details of substantial holder (1)\nName See Annexure A\n> 2. Details of voting power\nThe total"
    assert scrape.parse_holder_section(t, ocr=True)[0] == ""
    seg = "2, Previous and present voting power\nav) 884% 99%\nORDS 157,488,475\n(14.45% based on 1,751,348,821)"
    assert scrape.pick_voting_power(seg, "change", True, ocr=True) == []       # 扫描件不跨行拼数字


def test_strict_form_type_overrides_headline(sandbox, monkeypatch):
    form = ["Form 604", "Notice of change of interests of substantial holder", "To Company Name/Scheme ABC Limited",
            "1. Details of substantial holder (1)", "Name Some Random Fund Pty Ltd",
            "There was a change in the interests of the substantial holder on 27/10/2026",
            "2. Previous and present voting power", "Ordinary 1 6.01% 2 7.05%"]
    monkeypatch.setitem(PDFS, "1001", form)
    monkeypatch.setattr(scrape, "fetch", FakeASX(listing=LISTING.replace("Becoming a substantial holder<br>", "Ceasing to be a substantial holder<br>")))
    scrape.main()
    e = read(sandbox / "data" / "all_notices.json")["entries"]["1001"]
    assert e["type"] == "change" and e["voting_power"] == [6.01, 7.05]


def test_fetch_404_is_retried_later(monkeypatch):
    class Resp:
        status_code, headers, url = 404, {}, "u"
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def iter_content(self, chunk_size=0): return iter([])
    calls = []
    monkeypatch.setattr(scrape.session, "get", lambda *a, **k: calls.append(1) or Resp())
    with pytest.raises(scrape.FetchError) as ex:
        scrape.fetch("https://www.asx.com.au/x.pdf")
    assert not isinstance(ex.value, scrape.PermanentFetchError) and len(calls) == 1


def test_weekend_today_page_with_friday_rows_is_complete():
    scanned, partial = set(), {}
    scrape.mark_days({"2026-11-06"}, dt.date(2026, 11, 7), "10:00", scanned, partial)   # 周六上午打开
    assert scanned == {"2026-11-06"} and partial == {}
    scrape.mark_days({"2026-11-09"}, dt.date(2026, 11, 9), "10:00", scanned, partial)   # 周一上午：当天只抓到一部分
    assert partial == {"2026-11-09": "10:00"}


# ---------------- 补抓：全市场公告接口

def api_item(symbol, headline, utc, key, types=(), ps=False):
    return {"symbol": symbol, "headline": headline, "date": utc, "documentKey": key,
            "announcementTypes": list(types), "isPriceSensitive": ps}


LISTING_HUB = LISTING.replace("</table>", """<tr><td>HUB</td><td>30/10/2026<br><span>4:56 PM</span></td><td class="pricesens"></td>
<td><a href="/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId=1007">Change in substantial holding for KIT</a></td></tr></table>""")
PDFS["1007"] = ["Form 604", "To Company Name/Scheme Kapstream Investment Trust", "1. Details of substantial holder (1)",
                "Name HUB24 Limited", "There was a change in the interests of the substantial holder on 28/10/2026",
                "2. Previous and present voting power", "Ordinary 1 6.12% 2 5.12%"]
REGAL_603 = ["Form 603", "To Company Name/Scheme Old Day Limited", "1. Details of substantial holder (1)",
             "Name Regal Funds Management Pty Limited", "The holder became a substantial holder on 27/10/2026",
             "2. Details of voting power", "Ordinary 1 6.40%"]


def backfill_fake(**kw):
    items = [
        api_item("SLC", "Becoming a substantial holder", "2026-10-30T05:10:00.000Z", "2924-1001-A"),        # 列表页已有
        api_item("KIT", "Change in substantial holding from HUB", "2026-10-30T05:56:00.000Z", "2924-1007-A"),  # “for KIT” 的另一条发布
        api_item("OLD", "Becoming a substantial holder", "2026-10-28T23:00:00.000Z", "2924-9001-A",
                 types=["Becoming a substantial holder"]),                                                  # 漏掉的那天
        api_item("ZZZ", "Quarterly Activities Report", "2026-10-28T22:00:00.000Z", "2924-9002-A"),         # 不是大股东公告
        api_item("GEN", "Substantial Holder Notice", "2026-10-28T21:00:00.000Z", "2924-9003-A",
                 types=["Ceasing to be a substantial holder"]),
        api_item("OLD2", "Change in substantial holding", "2026-10-01T00:00:00.000Z", "2924-9004-A"),     # 窗口以外
    ]
    pdfs = {"2924-9001-A": REGAL_603,
            "2924-9003-A": ["Form 605", "1. Details of substantial holder (1)", "Name Somebody Else Pty Ltd",
                            "The holder ceased to be a substantial holder on 27/10/2026"],
            "2924-9004-A": REGAL_603}
    return FakeASX(listing=LISTING_HUB, api_items=items, api_pdfs=pdfs, **kw)


def test_backfill_adds_missed_days_and_skips_duplicates(sandbox, monkeypatch):
    fake = backfill_fake()
    monkeypatch.setattr(scrape, "fetch", fake)
    monkeypatch.setattr(scrape, "now_syd", lambda: dt.datetime(2026, 10, 30, 21, 30, tzinfo=scrape.SYD))
    assert scrape.main() == 0
    entries = read(sandbox / "data" / "all_notices.json")["entries"]
    assert "mk-2924-1001-A" not in entries and "mk-2924-1007-A" not in entries      # 和列表页重复的不再加
    old = entries["mk-2924-9001-A"]
    assert old["status"] == "parsed" and old["date"] == "2026-10-29" and old["time"] == "10:00"
    assert old["url"] == scrape.FILE_API + "2924-9001-A" and old["holder_name"].startswith("Regal Funds")
    assert entries["mk-2924-9003-A"]["type"] == "ceasing"                             # 标题看不出类型时用接口给的类型
    assert "mk-2924-9002-A" not in entries
    out = read(sandbox / "data" / "notices.json")
    got = {n["code"]: n for n in out["notices"]}
    assert got["OLD"]["holder"] == "Regal Funds Management" and got["OLD"]["pdf_url"].startswith(scrape.FILE_API)
    bf = out["last_run"]["backfill"]
    assert bf["ok"] and bf["added"] == 2 and out["last_run"]["errors"] == []        # 10 月 1 日那条在 16 天窗口以外
    assert {"2026-10-29", "2026-10-30"} <= set(out["scanned_days"]) and out["partial_days"] == {}
    # 再跑一次：不重复添加、不重复下载
    fake.pdf_downloads.clear()
    scrape.main()
    assert fake.pdf_downloads == [] and len(read(sandbox / "data" / "all_notices.json")["entries"]) == len(entries)


def test_backfill_stops_paging_once_window_is_covered(sandbox, monkeypatch):
    fake = backfill_fake()
    monkeypatch.setattr(scrape, "fetch", fake)
    monkeypatch.setattr(scrape, "API_PAGE_SIZE", 1)
    monkeypatch.setattr(scrape, "BACKFILL_DAYS", 3)
    monkeypatch.setattr(scrape, "now_syd", lambda: dt.datetime(2026, 10, 30, 21, 30, tzinfo=scrape.SYD))
    scrape.main()
    assert fake.api_pages == [0, 1, 2, 3, 4, 5]          # 第 6 条（10 月 1 日）已经早于窗口起点，翻到这里就停
    out = read(sandbox / "data" / "notices.json")
    assert out["last_run"]["backfill"]["from"] == "2026-10-27"      # 覆盖起点不早于窗口起点（3 天前）


def test_backfill_failure_does_not_break_list_scraping(sandbox, monkeypatch):
    monkeypatch.setattr(scrape, "fetch", backfill_fake(api_down=True))
    assert scrape.main() == 0
    out = read(sandbox / "data" / "notices.json")
    assert out["last_run"]["ok"] and not out["last_run"]["backfill"]["ok"]
    assert any("补抓接口" in e for e in out["last_run"]["errors"])
    assert {n["code"] for n in out["notices"]} >= {"SLC", "XYZ", "WAX"}


def test_backfill_partial_today_before_9pm(sandbox, monkeypatch):
    monkeypatch.setattr(scrape, "fetch", backfill_fake(prev_listing=EMPTY_LISTING))
    monkeypatch.setattr(scrape, "now_syd", lambda: dt.datetime(2026, 10, 30, 15, 0, tzinfo=scrape.SYD))
    scrape.main()
    out = read(sandbox / "data" / "notices.json")
    assert out["partial_days"] == {"2026-10-30": "15:00"} and "2026-10-29" in out["scanned_days"]


@needs_ocr
def test_image_overlay_form_gets_name_from_ocr(sandbox, monkeypatch):
    data = (FIXTURES / "603_image_overlay_tvl.pdf").read_bytes()
    texts = scrape.pdf_texts(data)
    assert texts and scrape.analyse_texts(texts, "becoming")["holder_name"] == ""   # 文字层只有空白模板
    fake = FakeASX()
    real_call = fake.__call__
    monkeypatch.setattr(scrape, "fetch", lambda url, **kw: scrape.Fetched(url, data, "application/pdf")
                        if url.endswith("idsId=1001") else real_call(url, **kw))
    scrape.main()
    e = read(sandbox / "data" / "all_notices.json")["entries"]["1001"]
    assert e["ocr"] is True and e["holder_name"].startswith("Certane CT") and e["voting_power"] == [5.02]


@needs_ocr
def test_ocr_fills_only_missing_fields():
    data = (FIXTURES / "603_8digit_date_mlg.pdf").read_bytes()
    e = {"url": "u", "type": "becoming"}
    orig = scrape.download_pdf
    try:
        scrape.download_pdf = lambda url: (data, "https://x/mlg.pdf")
        scrape.process_entry(e, scrape.OCRBudget())
    finally:
        scrape.download_pdf = orig
    assert e["holder_name"].startswith("Sohra Peak Capital Partners") and e["ocr"] is True
    assert e["change_date"] == "2026-10-28" and e["voting_power"] == [5.19]          # 数字仍用文字层的


def test_backfill_keeps_pages_already_fetched_when_a_later_page_fails(sandbox, monkeypatch):
    fake = backfill_fake()
    real_call = fake.__call__

    def flaky(url, **kw):
        if url.startswith(scrape.MARKET_API) and "page=3&" in url:
            raise scrape.FetchError("HTTP 502")
        return real_call(url, **kw)

    monkeypatch.setattr(scrape, "fetch", flaky)
    monkeypatch.setattr(scrape, "API_PAGE_SIZE", 1)
    monkeypatch.setattr(scrape, "now_syd", lambda: dt.datetime(2026, 10, 30, 21, 30, tzinfo=scrape.SYD))
    assert scrape.main() == 0
    out = read(sandbox / "data" / "notices.json")
    assert out["last_run"]["backfill"]["ok"] and out["last_run"]["backfill"]["added"] == 1   # 第 3 页（OLD）拿到了
    assert any("只翻到第 3 页" in e for e in out["last_run"]["errors"])
    assert "OLD" in {n["code"] for n in out["notices"]}


# ---------------------------------------------------------------- 第 2 轮审查（补抓的边界情况）

def test_backfill_ignores_items_older_than_window(sandbox, monkeypatch):
    fake = backfill_fake()
    monkeypatch.setattr(scrape, "fetch", fake)
    monkeypatch.setattr(scrape, "now_syd", lambda: dt.datetime(2026, 10, 30, 21, 30, tzinfo=scrape.SYD))
    scrape.main()
    entries = read(sandbox / "data" / "all_notices.json")["entries"]
    assert "mk-2924-9004-A" not in entries                 # 10 月 1 日那条早于 16 天窗口，不下载
    assert "mk-2924-9004-A" not in fake.pdf_downloads
    out = read(sandbox / "data" / "notices.json")
    assert out["last_run"]["backfill"]["from"] == "2026-10-14"      # 覆盖天数也不早于窗口起点
    assert min(out["scanned_days"]) >= "2026-10-14"


def test_same_notice_with_one_minute_difference_shown_once():
    base = {"status": "parsed", "holder_name": "Regal Funds Management Pty Limited", "type": "change",
            "change_date": "2026-10-28", "voting_power": [6.1, 7.2], "date": "2026-10-30", "headline": "Change in substantial holding"}
    out = scrape.build_notices({
        "1001": {**base, "id": "1001", "code": "ABC", "time": "16:10"},
        "mk-x": {**base, "id": "mk-x", "code": "ABC", "time": "16:11"},
        "1002": {**base, "id": "1002", "code": "XYZ", "time": "16:10"},                                 # 不同股票，不合并
        "1003": {**base, "id": "1003", "code": "ABC", "time": "17:00", "voting_power": [], "change_date": ""},   # 缺数字的不合并
    }, HOLDERS)
    assert sorted(n["id"] for n in out) == ["1001", "1002", "1003"]


# ---------------------------------------------------------------- 第 3 轮审查：接口下载的 PDF 是 AES 加密的

def encrypted_pdf(lines, user=""):
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    for p in PdfReader(io.BytesIO(make_pdf(lines))).pages:
        w.add_page(p)
    w.encrypt(user_password=user, owner_password="owner-secret", algorithm="AES-128")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


AES_LINES = ["Form 603", "To Company Name/Scheme Old Day Limited", "1. Details of substantial holder (1)",
             "Name Regal Funds Management Pty Limited", "The holder became a substantial holder on 27/10/2026",
             "2. Details of voting power", "Ordinary 1 6.40%"]


def test_aes_encrypted_pdf_with_empty_password_is_read():
    texts = scrape.pdf_texts(encrypted_pdf(AES_LINES))
    r = scrape.analyse_texts(texts, "becoming")
    assert r["holder_name"] == "Regal Funds Management Pty Limited" and r["voting_power"] == [6.4]


def test_pdfium_fallback_when_pypdf_cannot_open(monkeypatch):
    from pypdf.errors import DependencyError

    def no_crypto(*a, **k):
        raise DependencyError("cryptography>=3.1 is required for AES algorithm")

    monkeypatch.setattr(scrape, "PdfReader", no_crypto)        # 模拟正式环境缺 cryptography
    texts = scrape.pdf_texts(encrypted_pdf(AES_LINES))
    assert scrape.analyse_texts(texts, "becoming")["holder_name"] == "Regal Funds Management Pty Limited"


def test_pdf_needing_a_real_password_is_unreadable_not_an_error():
    assert scrape.pdf_texts(encrypted_pdf(AES_LINES, user="secret")) == []


def test_real_api_pdf_is_aes_encrypted_and_readable():
    data = (FIXTURES / "api_aes_encrypted_jns.pdf").read_bytes()       # 从全市场接口下载的原始 PDF
    assert b"/Encrypt" in data
    texts = scrape.pdf_texts(data)
    assert scrape.strict_form_type(texts[0]) == "ceasing"
    r = scrape.analyse_texts(texts, "ceasing")
    assert r["holder_name"] == "Acorn Capital Limited" and r["change_date"] == "2026-10-30"


def test_amended_notice_hides_the_original():
    base = {"status": "parsed", "holder_name": "Ellerston Capital Limited", "type": "change", "voting_power": [5.99, 7.12],
            "code": "MYG", "date": "2026-10-27"}
    out = scrape.build_notices({
        "a": {**base, "id": "a", "time": "17:40", "headline": "Change in substantial holding", "change_date": "2026-01-14"},
        "b": {**base, "id": "b", "time": "18:34", "headline": "Change in substantial holding - Amended", "change_date": "2026-10-23"},
        "c": {**base, "id": "c", "time": "10:00", "date": "2026-10-20", "headline": "Change in substantial holding",
              "change_date": "2026-10-16", "voting_power": [4.9, 5.99]},          # 之前另一份真实变动，不受影响
    }, HOLDERS)
    assert sorted(n["id"] for n in out) == ["b", "c"]


def test_real_glued_votes_and_percentage():
    r = real("603_glued_pct_alphinity_slc.pdf", "becoming")          # “25,887,0385.02%”
    assert r["holder_name"].startswith("Alphinity Investment Management") and r["voting_power"] == [5.02]


def test_real_nz_beginning_to_have_substantial_holding():
    r = real("nz_beginning_firstcape_fbu.pdf", "becoming")           # “total percentage held 5.032%” / “in class:” 在下一行
    assert r["holder_name"] == "FirstCape Group Limited" and r["voting_power"] == [5.032] and r["change_date"] == "2026-10-19"


def test_real_split_voting_power_header_and_date_after_in_the():
    r = real("604_split_header_hancock_bm1.pdf", "change")           # “voting powe r”、“Votingpower”；日期在 “in the” 之后
    assert r["holder_name"].startswith("Mrs Georgina Hope Rinehart") and r["voting_power"] == [6.19, 8.0]
    assert r["change_date"] == "2026-10-20"


def test_change_date_is_the_change_not_when_holder_became_aware():
    t = ("There was a change in the interests of 2 November 2026\nthe substantial holder on (HUB became aware of the change on "
         "4 November 2026)\nThe previous notice was given to the 29 May 2026")
    assert scrape.parse_details(t, "change")["change_date"] == "2026-11-02"


# ---------------------------------------------------------------- 第七轮（2026-11-11）：提速

def test_parallel_workers_give_same_result_as_serial(sandbox, monkeypatch):
    def run(workers):
        for f in (sandbox / "data").glob("*.json"):
            f.unlink()
        fake = backfill_fake()
        monkeypatch.setattr(scrape, "fetch", fake)
        monkeypatch.setattr(scrape, "WORKERS", workers)
        monkeypatch.setattr(scrape, "now_syd", lambda: dt.datetime(2026, 10, 30, 21, 30, tzinfo=scrape.SYD))
        assert scrape.main() == 0
        out = read(sandbox / "data" / "notices.json")
        entries = read(sandbox / "data" / "all_notices.json")["entries"]
        strip = lambda e: {k: v for k, v in e.items() if k != "checked_at"}
        return ([{k: v for k, v in n.items()} for n in out["notices"]], {k: strip(v) for k, v in entries.items()},
                sorted(fake.pdf_downloads), out["last_run"]["pdf_checked"])
    assert run(1) == run(4)


def test_parallel_ocr_budget_is_respected(sandbox, monkeypatch):
    fake_ocr(monkeypatch, SCAN_TEXT)
    monkeypatch.setattr(scrape, "fetch", FakeASX())
    monkeypatch.setattr(scrape, "WORKERS", 4)
    monkeypatch.setattr(scrape, "MAX_OCR", 2)
    scrape.main()
    entries = read(sandbox / "data" / "all_notices.json")["entries"]
    st = sorted(e["status"] for k, e in entries.items() if k != "1004")
    assert st == ["new", "new", "parsed", "parsed"]            # 4 份扫描件，名额 2 份，其余留到下次
    assert read(sandbox / "data" / "notices.json")["last_run"]["deferred"] == 2


def many_items(n_days=20, per_day=30):
    """接口里每天 per_day 条普通公告 + 1 条大股东公告，从 10 月 30 日往前 n_days 天。"""
    items = []
    for d in range(n_days):
        day = dt.date(2026, 10, 30) - dt.timedelta(days=d)
        for k in range(per_day):
            items.append(api_item("AAA", "Quarterly report", f"{day.isoformat()}T0{k % 6}:{k:02d}:00.000Z", f"q-{d}-{k}"))
        items.append(api_item(f"S{d:02d}", "Becoming a substantial holder", f"{day.isoformat()}T00:00:30.000Z", f"s-{d}",
                              types=["Becoming a substantial holder"]))
    items.sort(key=lambda it: it["date"], reverse=True)
    return items


def test_backfill_is_incremental_after_first_full_pass(sandbox, monkeypatch):
    items = many_items()
    pdfs = {f"s-{d}": REGAL_603 for d in range(20)}
    fake = FakeASX(listing=EMPTY_LISTING, api_items=items, api_pdfs=pdfs)
    monkeypatch.setattr(scrape, "fetch", fake)
    monkeypatch.setattr(scrape, "API_PAGE_SIZE", 50)
    monkeypatch.setattr(scrape, "API_MAX_PAGES", 40)
    monkeypatch.setattr(scrape, "now_syd", lambda: dt.datetime(2026, 10, 30, 21, 30, tzinfo=scrape.SYD))
    scrape.main()
    first_pages = len(fake.api_pages)
    out = read(sandbox / "data" / "notices.json")
    assert first_pages >= 10 and out["last_run"]["backfill"]["incremental"] is False
    store = read(sandbox / "data" / "all_notices.json")
    assert store["api_synced_at"].startswith("2026-10-30") and store["api_full_at"]

    # 第二天：接口里多了一天的公告。只需要翻最前面一两页
    day = dt.date(2026, 10, 31)
    new = [api_item("AAA", "Quarterly report", f"{day.isoformat()}T01:{k:02d}:00.000Z", f"n-{k}") for k in range(20)]
    new.append(api_item("NEW", "Becoming a substantial holder", f"{day.isoformat()}T02:00:00.000Z", "s-new",
                        types=["Becoming a substantial holder"]))
    fake.api_items = sorted(new + items, key=lambda it: it["date"], reverse=True)
    fake.api_pdfs["s-new"] = REGAL_603
    fake.api_pages.clear()
    monkeypatch.setattr(scrape, "now_syd", lambda: dt.datetime(2026, 10, 31, 21, 30, tzinfo=scrape.SYD))
    scrape.main()
    out = read(sandbox / "data" / "notices.json")
    assert len(fake.api_pages) <= 2 < first_pages
    assert out["last_run"]["backfill"]["incremental"] is True and out["last_run"]["backfill"]["added"] == 1
    assert "mk-s-new" in read(sandbox / "data" / "all_notices.json")["entries"]
    assert {"2026-10-30", "2026-10-31"} <= set(out["scanned_days"]) and out["partial_days"] == {}


def test_incremental_backfill_completes_yesterdays_partial_day(sandbox, monkeypatch):
    items = many_items()
    fake = FakeASX(listing=EMPTY_LISTING, api_items=items, api_pdfs={f"s-{d}": REGAL_603 for d in range(20)})
    monkeypatch.setattr(scrape, "fetch", fake)
    monkeypatch.setattr(scrape, "now_syd", lambda: dt.datetime(2026, 10, 30, 15, 0, tzinfo=scrape.SYD))   # 下午 3 点：当天只抓到一部分
    scrape.main()
    assert read(sandbox / "data" / "notices.json")["partial_days"] == {"2026-10-30": "15:00"}
    late = api_item("LATE", "Becoming a substantial holder", "2026-10-30T08:30:00.000Z", "s-late")           # 悉尼 19:30 发的
    fake.api_items = sorted([late] + items, key=lambda it: it["date"], reverse=True)
    fake.api_pdfs["s-late"] = REGAL_603
    monkeypatch.setattr(scrape, "now_syd", lambda: dt.datetime(2026, 11, 2, 9, 0, tzinfo=scrape.SYD))       # 周一早上
    scrape.main()
    out = read(sandbox / "data" / "notices.json")
    assert out["last_run"]["backfill"]["incremental"] is True
    assert out["partial_days"] == {"2026-11-02": "09:00"} and "2026-10-30" in out["scanned_days"]
    assert "mk-s-late" in read(sandbox / "data" / "all_notices.json")["entries"]


def test_full_window_rescan_every_week_and_after_failed_sync(sandbox, monkeypatch):
    items = many_items()
    fake = FakeASX(listing=EMPTY_LISTING, api_items=items, api_pdfs={f"s-{d}": REGAL_603 for d in range(20)})
    monkeypatch.setattr(scrape, "fetch", fake)
    monkeypatch.setattr(scrape, "now_syd", lambda: dt.datetime(2026, 10, 30, 21, 30, tzinfo=scrape.SYD))
    scrape.main()
    monkeypatch.setattr(scrape, "now_syd", lambda: dt.datetime(2026, 11, 8, 21, 30, tzinfo=scrape.SYD))      # 9 天后
    scrape.main()
    assert read(sandbox / "data" / "notices.json")["last_run"]["backfill"]["incremental"] is False
    store = read(sandbox / "data" / "all_notices.json")
    assert store["api_full_at"].startswith("2026-11-08")
    # 同步点坏掉（比如手工改坏了）时退回整窗口
    assert scrape.api_since({"api_synced_at": "garbage", "api_full_at": store["api_full_at"]},
                            dt.datetime(2026, 11, 9, 9, 0, tzinfo=scrape.SYD)) is None
    assert scrape.api_since({}, dt.datetime(2026, 11, 9, 9, 0, tzinfo=scrape.SYD)) is None


def test_failed_later_page_does_not_advance_sync_point(sandbox, monkeypatch):
    fake = backfill_fake()
    real_call = fake.__call__

    def flaky(url, **kw):
        if url.startswith(scrape.MARKET_API) and "page=3&" in url:
            raise scrape.FetchError("HTTP 502")
        return real_call(url, **kw)

    monkeypatch.setattr(scrape, "fetch", flaky)
    monkeypatch.setattr(scrape, "API_PAGE_SIZE", 1)
    monkeypatch.setattr(scrape, "now_syd", lambda: dt.datetime(2026, 10, 30, 21, 30, tzinfo=scrape.SYD))
    scrape.main()
    assert "api_synced_at" not in read(sandbox / "data" / "all_notices.json")     # 下次还是从头翻


# ---------------------------------------------------------------- 第七轮（2026-11-11）：准确率

def test_more_date_formats_round7():
    assert scrape.parse_date("on October / 21 / 2026") == "2026-10-21"          # Bank of Nova Scotia
    assert scrape.parse_date("holder on / /05 11 2026") == "2026-11-05"         # BlackRock：方格前面带斜杠
    assert scrape.parse_date(": 13 October2026") == "2026-10-13"                 # 月份和年份粘在一起
    assert scrape.parse_date("27th of October 2026") == "2026-10-27"
    assert scrape.parse_date("ACN 062 576 238") == "" and scrape.parse_date("Level 45, 101 Collins Street") == ""


def test_percentage_odd_spellings():
    pick = lambda seg, t="change": scrape.pick_voting_power("2. Previous and present voting power\n" + seg + "\n3. Changes in relevant", t, True)
    assert pick("Ful ly pai d ordi nary shar es 36, 338, 793 25. 9 3 % 36, 903, 079 22. 95%") == [25.93, 22.95]   # 以前读成 [3.0, 95.0]
    assert pick("Ordinary Shares 4,458,618 9.31%* 6,031,726 12. 60%*") == [9.31, 12.6]                              # 以前读成 [9.31, 60.0]
    assert pick("88,192,952 88,192,952 6,93%", "becoming") == [6.93]                                              # 欧式逗号，以前读成 93%
    assert pick("CDIs over fully paid ordinary 10,671,838 7.54 9,986,838 7.04%") == [7.54, 7.04]                  # 只有一个带 %
    assert pick("Ordinary shares 38,231,831 18.26% 40,553,764 19.26") == [18.26, 19.26]
    assert pick("Ordinary Shares 76,406,237 11.36% 64,019,970 9.52%") == [11.36, 9.52]                            # 正常写法不受影响
    assert pick("ORD 1,000,000 6.10% 1,200,000 7.20% $2.50") == [6.1, 7.2]


def test_prose_percentages_are_not_a_voting_power_table():
    letter = ("Voting power consists of the shareholder's own relevant interest.\nobtains voting power of 5% or more in\n"
              "subsequent voting power change of 1 % or more\nHolder A 19.90% 19.90%")
    assert scrape.parse_details(letter, "change")["voting_power"] == []


def test_name_label_in_the_middle_of_a_multi_line_name():
    t = ("1. Details of substantial holder (1)\nAckland Investments No.1 Pty Limited as trustee for Ackland Investments No.1 Trust (Ackland)\n"
         "Goodman Limited (Goodman), Goodman Funds Management Limited\nName and Goodman Logistics (HK) Limited and each of their controlled entities\n"
         "ACN 703 706 174\nThe holder became a substantial holder on 9/11/2026\n2. Details of voting power")
    assert scrape.parse_holder_section(t)[0].startswith("Ackland Investments No.1 Pty Limited")


def test_tr1_name_on_the_line_after_the_labels():
    t = ("NOTIFICATION OF MAJOR HOLDINGS\n1. Identity of the issuer:\nJAMES HARDIE INDUSTRIES PLC\n"
         "3. Details of person subject to the notification obligationiv :\nName: City and country of registered office (if applicable):\n"
         "FMR LLC Wilmington, USA\n5. Date on which the threshold was crossed or reachedvi:\n27th of October 2026\n"
         "Resulting situation\non the date on which 7.1589% 0.0000% 7.1589% 5\nPosition of previous\nnotification 6.0444% 0% 6.0444%")
    r = scrape.parse_other_forms(t, "change")
    assert r["holder_name"] == "FMR LLC Wilmington, USA" and r["change_date"] == "2026-10-27" and r["voting_power"] == [6.0444, 7.1589]


# ---------------------------------------------------------------- 操作解读（insight.py）

import insight  # noqa: E402

SAMPLES = json.loads((FIXTURES / "insight_samples.json").read_text(encoding="utf-8"))
STYLE = {h.name: h.style for h in HOLDERS}


def sample(name: str, prev: dict | None = None) -> tuple[dict, dict, dict]:
    """返回 (明细汇总, 判断结果, 中间数据)。完全离线：文字和股价都来自真实公告的存档。"""
    s = SAMPLES["samples"][name]
    votes = insight.parse_votes(scrape.voting_section(s["text"])[0], s["voting_power"], s["type"])
    tr = insight.parse_trades(s["pages"], s["date"], s["type"])["summary"]
    px = insight.price_context(SAMPLES["prices"][s["code"]], s["change_date"] or tr.get("last") or s["date"],
                               tr.get("first", ""), tr.get("last", ""))
    j = insight.judge({"type": s["type"], "voting_power": s["voting_power"], "votes": votes, "trades": tr, "px": px,
                       "style": STYLE[s["holder"]], "prev": prev, "date": s["date"]})
    return tr, j, {"votes": votes, "px": px}


def test_votes_and_trades_reconcile_exactly_on_real_notices():
    # 第二节的持股数量变化 = 附件里逐笔交易的净额。两边独立读出来还能对上，说明都读对了
    for name in ("bap_hostplus_604", "amx_perennial_604", "nol_regal_604", "fpr_art_604"):
        tr, _, mid = sample(name)
        assert tr["net_qty"] == mid["votes"][1] - mid["votes"][0], name


def test_real_sell_into_weakness_hostplus_bap():
    tr, j, mid = sample("bap_hostplus_604")
    assert mid["votes"] == [76406237, 64019970]
    assert (tr["n"], tr["buy_qty"], tr["sell_qty"], tr["first"], tr["last"]) == (8, 0, 12386267, "2026-09-17", "2026-10-26")
    assert tr["sell_price"] == pytest.approx(0.821, abs=0.001)          # 对价带负号：-$9,397.80 / -12,000
    # 第十一轮：这只股票 12 个月跌了 57%，但最近 3 个月翻了一倍——以前标“像止损”，现在标“趁反弹减仓”
    assert j["action"] == "trim_bounce" and j["tone"] == "bear" and j["style"] == "养老金"
    assert j["purposes"][0]["k"] == "bounce" and "stop" in [p["k"] for p in j["purposes"][:3]]   # 第十七轮：波动大，“跌了一大段”打了折
    assert any("1,239 万股" in r for r in j["reasons"]) and any("趁反弹卖" in r for r in j["reasons"])


def test_real_profit_taking_perennial_amx():
    tr, j, _ = sample("amx_perennial_604")
    assert tr["sell_qty"] == 9499064 and tr["sell_price"] == pytest.approx(0.3393, abs=0.001)
    assert j["action"] == "trim_profit" and j["confidence"] == "高"
    assert any("四成以上" in r for r in j["reasons"])


def test_real_603_rows_with_holder_name_before_the_date():
    tr, j, mid = sample("mp1_art_603")                                 # “… Trust 26/06/2026 365,077.25 Ordinary Fully Paid 17,848”
    assert tr["n"] == 52 and tr["buy_qty"] == 2462905 and tr["sell_qty"] == 0
    assert tr["buy_price"] == pytest.approx(18.68, abs=0.01)           # 对价是总金额，要除以股数
    assert j["action"] == "cross_up"                                   # 4 个月只买了现有持仓的两成：早有底仓


def test_real_issuer_share_issue_row_is_not_a_trade():
    tr, j, mid = sample("flx_perennial_603")                           # “Issue of 67,938,918 fully paid ordinary shares …”
    assert tr["buy_qty"] == 6411118 and tr["kind_rows"]["dilution"] == 1
    assert tr["buy_qty"] < mid["votes"][0]


def test_real_dilution_l1_gls():
    tr, j, mid = sample("gls_l1_604")
    assert mid["votes"] == [79710074, 79710074] and tr["n"] == 0
    assert j["action"] == "passive_dilute" and j["tone"] == "neutral" and j["confidence"] == "高"


def test_real_collateral_is_not_a_sale_cbus_acl():
    tr, j, _ = sample("acl_cbus_605")                                  # “Collateral Posted … 649,625-”，以及期初/期末余额行
    assert tr["n"] == 1 and tr["sell_qty"] == 649625 and tr["kinds"] == {"lending": 649625}
    assert j["action"] == "lending" and j["tone"] == "neutral"


def test_real_new_position_ellerston_dxn():
    tr, j, mid = sample("dxn_ellerston_603")
    assert tr["n"] == 23 and tr["buy_qty"] == 42325943
    assert j["action"] == "build" and j["tone"] == "bull"


def test_real_two_way_trading_regal_nol():
    tr, j, _ = sample("nol_regal_604")
    assert tr["buy_qty"] == 14491130 and tr["sell_qty"] == 16005756
    assert j["action"] == "flip" and j["style"] == "多空对冲基金"                 # 第十八轮：买的大多在前、卖的大多在后
    assert any("先买进再卖出" in r and "基本持平" in r for r in j["reasons"])
    assert any("2025/10/23" in r for r in j["reasons"])                # 跨年的明细带上年份


def test_real_sold_higher_than_bought_art_fpr():
    _, j, _ = sample("fpr_art_604")
    assert j["action"] == "trim_profit" and any("卖出均价比买入均价高" in r for r in j["reasons"])


def test_slipping_just_under_the_line_and_coming_back():
    _, j, _ = sample("kar_cfs_605", prev={"vp": 5.0, "votes": 34880000, "type": "becoming", "date": "2026-11-09"})
    assert j["action"] == "slip" and j["confidence"] == "低"           # 平台/多经理，信号最弱
    back = insight.judge({"type": "becoming", "voting_power": [5.0], "votes": [34880000], "style": "platform",
                          "prev": {"vp": None, "votes": None, "type": "ceasing", "date": "2026-11-05"}, "date": "2026-11-09"})
    assert back["action"] == "back"


def base_note(**kw):
    n = {"type": "change", "voting_power": [10.0, 8.0], "votes": [10_000_000, 8_000_000], "style": "active", "date": "2026-11-10"}
    n.update(kw)
    return n


def test_judge_special_cases():
    j = insight.judge(base_note(voting_power=[6.0, 7.5], votes=[6_000_000, 6_000_000]))
    assert j["action"] == "passive_up"                                                    # 股数没变、比例上升：回购
    j = insight.judge(base_note(voting_power=[13.71, 10.24], votes=[345_266_739, 23_017_782]))
    assert j["action"] == "restructure"                                                   # 股份合并
    tr = {"n": 3, "buy_qty": 2_000_000, "sell_qty": 0, "net_qty": 2_000_000, "first": "2026-10-01", "last": "2026-10-02",
          "kinds": {"placement": 1_800_000, "on_market": 200_000}, "kind_rows": {"placement": 1, "on_market": 2}}
    j = insight.judge(base_note(voting_power=[8.0, 10.0], votes=[8_000_000, 10_000_000], trades=tr))
    assert j["action"] == "add_placement" and j["tone"] == "bull"
    tr = {"n": 2, "buy_qty": 0, "sell_qty": 2_000_000, "net_qty": -2_000_000, "first": "2026-10-01", "last": "2026-10-02",
          "kinds": {"takeover": 2_000_000}, "kind_rows": {"takeover": 2}}
    assert insight.judge(base_note(trades=tr))["action"] == "takeover"
    assert insight.judge({"type": "other"}) is None


def test_judge_uses_holdings_when_rows_do_not_add_up():
    tr = {"n": 5, "buy_qty": 0, "sell_qty": 300_000, "net_qty": -300_000, "first": "2026-10-01", "last": "2026-10-09",
          "kinds": {"on_market": 300_000}, "kind_rows": {"on_market": 5}}
    j = insight.judge(base_note(trades=tr))                            # 明细只读到一部分
    assert j["tone"] == "bear" and any("对不上" in r for r in j["reasons"]) and any("200 万股" in r for r in j["reasons"])


def test_judge_drops_prices_that_disagree_with_the_market():
    tr = {"n": 3, "buy_qty": 0, "sell_qty": 2_000_000, "net_qty": -2_000_000, "first": "2026-10-01", "last": "2026-10-09",
          "sell_price": 99.0, "kinds": {"on_market": 2_000_000}, "kind_rows": {"on_market": 3}}
    px = {"p": 2.0, "r3m": 0.0, "r6m": 0.0, "r12m": 0.0, "pos": 0.5, "hi": 2.5, "lo": 1.5, "t_lo": 1.9, "t_hi": 2.1}
    j = insight.judge(base_note(trades=tr, px=px))
    assert j["action"] == "trim" and not any("$99" in r for r in j["reasons"])


def test_ocr_and_platform_lower_confidence():
    assert insight.judge(base_note(ocr=True, votes=[]))["confidence"] == "低"
    assert insight.judge(base_note(style="platform", votes=[]))["confidence"] == "低"


def test_price_context_basic():
    day = lambda d: int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() * 1000)
    d0 = dt.date(2025, 10, 1)
    series = [[day(d0 + dt.timedelta(days=i)), 3.0 - i / 400, 1000 + i] for i in range(400) if (d0 + dt.timedelta(days=i)).weekday() < 5]
    px = insight.price_context(series, "2026-10-28", "2026-10-20", "2026-10-27")
    assert px["p"] == pytest.approx(3.0 - 392 / 400, abs=0.01) and px["pos"] < 0.05 and px["r3m"] < -0.08 and px["r12m"] < -0.25
    assert px["t_lo"] <= px["t_hi"] and px["adv"] > 1000
    assert insight.price_context(series, "2027-06-01") == {}           # 参考日之后很久没有数据
    assert insight.price_context([], "2026-10-28") == {} and insight.price_context(series, "") == {}


TRADE_604 = ["Form 604", "To Company Name/Scheme Superloop Limited", "1. Details of substantial holder (1)",
             "Name Paradice Investment Management Pty Ltd", "There was a change in the interests of the",
             "substantial holder on 28/10/2026", "The previous notice was dated 01/09/2026",
             "2. Previous and present voting power", "Ordinary 10,000,000 6.10% 12,000,000 7.20%",
             "3. Changes in relevant interests",
             "20/10/2026 Paradice On market purchase $2,100,000.00 1,000,000 1,000,000",
             "27/10/2026 Paradice On market purchase $2,000,000.00 1,000,000 1,000,000",
             "4. Present relevant interests"]


def falling_prices():
    day = lambda d: int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() * 1000)
    d0 = dt.date(2025, 10, 1)
    return [[day(d0 + dt.timedelta(days=i)), round(3.0 - i / 400, 4), 50_000] for i in range(400)
            if (d0 + dt.timedelta(days=i)).weekday() < 5]


def test_insight_end_to_end(sandbox, monkeypatch):
    monkeypatch.setitem(PDFS, "1001", TRADE_604)
    fake = FakeASX(prices={"SLC": falling_prices()})
    monkeypatch.setattr(scrape, "fetch", fake)
    assert scrape.main() == 0
    e = read(sandbox / "data" / "all_notices.json")["entries"]["1001"]
    assert e["votes"] == [10_000_000, 12_000_000] and e["trades"]["buy_qty"] == 2_000_000 and e["trades"]["buy_price"] == 2.05
    assert e["detail_v"] == scrape.INSIGHT_VERSION and e["px"]["pos"] < 0.1
    out = read(sandbox / "data" / "notices.json")
    n = next(x for x in out["notices"] if x["code"] == "SLC")
    assert n["insight"]["action"] == "add_dip" and n["insight"]["label"] == "逢低加仓" and n["insight"]["confidence"] == "高"
    assert out["styles"]["Paradice Investment Management"] == "主动选股基金"
    assert out["last_run"]["insight"]["prices_ok"] == 1
    # 没命中名单的公告不读明细、不取股价
    other = read(sandbox / "data" / "all_notices.json")["entries"]["1002"]
    assert "detail_v" not in other and "ABC" not in fake.price_calls
    # 第二次运行：不重复下载，也不重复取已经有的股价
    fake.pdf_downloads.clear()
    n_calls = fake.price_calls.count("SLC")
    scrape.main()
    assert fake.pdf_downloads == [] and fake.price_calls.count("SLC") == n_calls


def test_price_failure_is_retried_a_few_times_then_dropped(sandbox, monkeypatch):
    fake = FakeASX()                                                   # 没有任何股价
    monkeypatch.setattr(scrape, "fetch", fake)
    for _ in range(5):
        assert scrape.main() == 0
    assert fake.price_calls.count("SLC") == scrape.DETAIL_TRIES
    out = read(sandbox / "data" / "notices.json")
    assert out["last_run"]["ok"] and out["last_run"]["errors"] == []
    n = next(x for x in out["notices"] if x["code"] == "SLC")
    assert n["insight"]["action"] == "build"                           # 没有股价也能给出基本判断


def test_real_pdf_trade_table_end_to_end():
    data = (FIXTURES / "604_trades_perennial_amx.pdf").read_bytes()
    e = {"type": "change", "voting_power": [18.58, 8.58], "date": "2026-11-10", "ocr": False}
    scrape.add_detail(e, data)
    assert e["votes"] == [17644814, 8150859] and e["trades"]["net_qty"] == -9493955


def test_reparse_clears_stale_detail(sandbox, monkeypatch):
    monkeypatch.setattr(scrape, "fetch", FakeASX(prices={"SLC": falling_prices()}))
    scrape.main()
    store = read(sandbox / "data" / "all_notices.json")
    store["entries"]["1001"]["parser"] = scrape.PARSER_VERSION - 1
    store["entries"]["1001"]["px"] = {"p": 123}
    (sandbox / "data" / "all_notices.json").write_text(json.dumps(store), encoding="utf-8")
    scrape.main()
    e = read(sandbox / "data" / "all_notices.json")["entries"]["1001"]
    assert e["parser"] == scrape.PARSER_VERSION and e["px"].get("p") != 123 and e["detail_v"] == scrape.INSIGHT_VERSION


def test_judge_with_almost_no_data_is_low_confidence():
    j = insight.judge({"type": "becoming", "voting_power": [], "style": "active"})
    assert j["action"] == "build" and j["confidence"] == "低"


# ---------------------------------------------------------------- 第八轮：公司大事、成本价、横向比较

def test_company_events_from_real_page():
    evs = scrape.parse_company_events((FIXTURES / "company_anns_ppe.html").read_text(encoding="utf-8"))
    kinds = {e["k"] for e in evs}
    assert {"buyback", "results"} <= kinds
    assert any(e["d"] == "2026-08-31" and e["k"] == "results" for e in evs)            # FY26 Annual Results / Appendix 4E
    assert sum(e["k"] == "buyback" for e in evs) == 2                                    # 几十份每日回购更新只留头尾
    assert not any("substantial" in e["h"].lower() or "Briefing" in e["h"] or "General Meeting" in e["h"] for e in evs)
    assert evs == sorted(evs, key=lambda e: e["d"])
    assert scrape.parse_company_events("<html><body>Access denied</body></html>") is None
    assert scrape.parse_company_events("<html><h2>Search results</h2><p>No announcements</p></html>") == []


@pytest.mark.parametrize("headline,kind", [
    ("Completion of $50m Placement", "raise"), ("Proposed issue of securities - ABC", "raise"),
    ("Share Purchase Plan Offer Booklet", "raise"), ("Appendix 4D and Half Year Report", "results"),
    ("Quarterly Activities Report", "results"), ("Trading Update and FY27 Guidance", "results"),
    ("Receipt of Non-Binding Indicative Proposal", "takeover"), ("Scheme Booklet registered with ASIC", "takeover"),
    ("Update - Notification of buy-back - ABC", "buyback"), ("Results of Annual General Meeting", ""),
    ("Updated - Sep 2026 Quarterly Activities Report Advisory", ""), ("Change of Director's Interest Notice", ""), ("Becoming a substantial holder", ""), ("Half Year Results Briefing Date", ""),
])
def test_classify_event(headline, kind):
    assert scrape.classify_event(headline) == kind


def test_cost_basis_moving_average():
    recs = [{"date": "2026-01-10", "buy_qty": 1000, "sell_qty": 0, "buy_price": 1.0, "votes": [1000]},
            {"date": "2026-03-10", "buy_qty": 1000, "sell_qty": 0, "buy_price": None, "p": 2.0, "votes": [1000, 2000]},   # 没写价格：用收盘价
            {"date": "2026-05-10", "buy_qty": 0, "sell_qty": 1000, "votes": [2000, 1000]}]
    c = insight.cost_basis(recs, holding=1000)
    assert c["avg"] == 1.5 and c["cov"] == 1.0 and c["n"] == 2 and c["since"] == "2026-01-10"
    assert insight.cost_basis(recs, holding=4000)["cov"] == 0.25
    assert insight.cost_basis([{"date": "2026-01-10", "buy_qty": 0, "sell_qty": 500}], 1000) == {}
    # 累计出来的股数比披露的持仓还多（中间有没看到的卖出）：压回披露数
    c = insight.cost_basis([{"date": "2026-01-10", "buy_qty": 5000, "buy_price": 1.0, "votes": [1000]}], holding=1000)
    assert c["avg"] == 1.0 and c["cov"] == 1.0


def _sell(**kw):
    n = {"type": "change", "voting_power": [8.0, 6.0], "votes": [8_000_000, 6_000_000], "style": "active", "date": "2026-11-05",
         "change_date": "2026-11-03",
         "trades": {"n": 4, "n_buy": 0, "n_sell": 4, "buy_qty": 0, "sell_qty": 2_000_000, "net_qty": -2_000_000, "first": "2026-10-20",
                    "last": "2026-11-03", "days": 4, "sell_price": 2.0, "buy_price": None, "kinds": {"on_market": 2_000_000}},
         "px": {"p": 2.0, "r1m": 0.0, "r3m": 0.02, "r6m": 0.05, "r12m": 0.05, "pos": 0.5, "hi": 2.5, "lo": 1.5, "t_lo": 1.9, "t_hi": 2.1}}
    n.update(kw)
    return n


def test_judge_uses_cost_basis_for_sells():
    assert insight.judge(_sell())["action"] == "trim"                                    # 没有成本：看不出
    r = insight.judge(_sell(cost={"avg": 1.2, "cov": 0.8, "since": "2026-01-10", "n": 3}))
    assert r["action"] == "trim_profit" and any("平均买入成本约 $1.20" in x and "赚钱卖" in x for x in r["reasons"])
    r = insight.judge(_sell(cost={"avg": 3.0, "cov": 0.8, "since": "2026-01-10", "n": 3}))
    assert r["action"] == "trim_loss" and any("亏钱卖" in x for x in r["reasons"])
    r = insight.judge(_sell(cost={"avg": 2.05, "cov": 0.8, "since": "2026-01-10", "n": 3}))
    assert r["action"] == "trim" and any("和成本差不多" in x for x in r["reasons"])
    assert insight.judge(_sell(cost={"avg": 1.2, "cov": 0.2, "since": "2026-01-10", "n": 1}))["action"] == "trim"   # 已知成本的部分太少，不用
    assert insight.judge(_sell(cost={"avg": 0.01, "cov": 0.9, "since": "2026-01-10", "n": 1}))["action"] == "trim"  # 离谱的数字不用


def _buy(**kw):
    n = {"type": "change", "voting_power": [6.0, 8.0], "votes": [6_000_000, 8_000_000], "style": "active", "date": "2026-11-05",
         "change_date": "2026-11-03",
         "trades": {"n": 3, "n_buy": 3, "n_sell": 0, "buy_qty": 2_000_000, "sell_qty": 0, "net_qty": 2_000_000, "first": "2026-10-28",
                    "last": "2026-11-03", "days": 3, "buy_price": 2.0, "sell_price": None, "kinds": {"other": 2_000_000}},
         "px": {"p": 2.0, "r1m": 0.0, "r3m": 0.02, "r6m": 0.05, "r12m": 0.05, "pos": 0.5, "hi": 2.5, "lo": 1.5, "t_lo": 1.9, "t_hi": 2.1}}
    n.update(kw)
    return n


RAISE = [{"d": "2026-10-27", "k": "raise", "h": "Successful $40m Placement", "s": True}]


def test_judge_infers_placement_from_event_and_volume():
    assert insight.judge(_buy())["action"] == "add"
    # 买的比同期全市场成交量还多 + 公司刚公告融资 → 参与配售
    n = _buy(ev=RAISE)
    n["px"]["t_vol"] = 900_000
    r = insight.judge(n)
    assert r["action"] == "add_placement" and "参与融资" in r["tags"]
    assert any("2.2 倍" in x for x in r["reasons"]) and any("Successful $40m Placement" in x for x in r["reasons"])
    # 有融资公告，但买入量只是成交量的一小部分：只提一句，不改结论
    n = _buy(ev=RAISE)
    n["px"]["t_vol"] = 50_000_000
    r = insight.judge(n)
    assert r["action"] == "add" and any("一部分买入可能来自认购新股" in x for x in r["reasons"])
    # 融资公告离得太远：不提
    r = insight.judge(_buy(ev=[{"d": "2026-06-01", "k": "raise", "h": "Placement", "s": True}]))
    assert not any("融资" in x for x in r["reasons"])
    # 没有融资公告，但量超过成交量：只说不是场内
    n = _buy()
    n["px"]["t_vol"] = 900_000
    r = insight.judge(n)
    assert r["action"] == "add" and any("不可能都是在场内成交" in x for x in r["reasons"])


def test_judge_takeover_and_results_events():
    tk = [{"d": "2026-10-15", "k": "takeover", "h": "Receipt of Non-Binding Indicative Proposal", "s": True, "j": 0.31}]
    r = insight.judge(_buy(ev=tk, style="hedge"))
    assert r["action"] == "arb" and r["tone"] == "neutral" and "收购相关" in r["tags"]
    r = insight.judge(_buy(ev=tk, style="active"))
    assert r["action"] == "add" and any("赌收购成功" in x for x in r["reasons"])
    r = insight.judge(_sell(ev=tk))
    assert r["action"] == "trim_deal" and r["tone"] == "neutral" and any("借收购带来的涨价退出" in x and "+31%" in x for x in r["reasons"])
    # 并购公告后股价没涨：公司多半是出手收购的一方（或还没定），不能说是套利
    quiet = [{"d": "2026-10-15", "k": "takeover", "h": "BET: BetMakers enters Scheme Implementation Deed with TAH", "s": True, "j": 0.01}]
    r = insight.judge(_buy(ev=quiet, style="hedge"))
    assert r["action"] == "add" and "收购相关" not in r["tags"] and any("不据此下结论" in x for x in r["reasons"])
    assert insight.judge(_buy(ev=[dict(quiet[0], j=None)], style="hedge"))["action"] == "add"
    # 近期只有走流程的公告，但更早那条消息让股价跳涨过：仍然算被收购方
    both = [{"d": "2026-06-01", "k": "takeover", "h": "Takeover proposal from XYZ", "s": True, "j": 0.4},
            {"d": "2026-10-12", "k": "takeover", "h": "Lodgement of Scheme Booklet with ASIC", "s": False, "j": 0.0}]
    r = insight.judge(_buy(ev=both, style="hedge"))
    assert r["action"] == "arb" and any("Takeover proposal from XYZ" in x for x in r["reasons"])
    assert insight.judge(_buy(ev=both[:1], style="hedge"))["action"] == "add"            # 只有半年前那条、近期没有后续：不算
    res = [{"d": "2026-10-26", "k": "results", "h": "Appendix 4D and Half Year Report", "s": True, "j": -0.18}]
    r = insight.judge(_buy(ev=res))
    assert "业绩后" in r["tags"] and any("对这份业绩的反应" in x and "公布后股价 -18%" in x for x in r["reasons"])
    long = _buy(ev=res)                                    # 交易拖了几个月：不能说是对业绩的反应
    long["trades"]["first"] = "2026-06-01"
    assert "业绩后" not in insight.judge(long)["tags"]
    assert insight.judge(_buy(ev=[{"bad": 1}, "x", None]))["action"] == "add"            # 坏数据不出错


def test_passive_dilution_cites_raise():
    n = {"type": "change", "voting_power": [8.0, 6.9], "votes": [8_000_000, 8_000_000], "style": "active", "date": "2026-11-05",
         "change_date": "2026-11-03", "trades": {}, "px": {}, "ev": RAISE}
    r = insight.judge(n)
    assert r["action"] == "passive_dilute" and any("对得上" in x and "Placement" in x for x in r["reasons"])


def _n(holder, code, action, date="2026-11-05", sector="", style="主动选股基金"):
    return {"holder": holder, "code": code, "date": date, "sector": sector,
            "insight": {"action": action, "style": style, "reasons": ["第一句。", f"{style}：说明。"]}}


def test_add_context_same_fund_other_stocks():
    ns = [_n("A", "AAA", "trim", sector="Materials"), _n("A", "BBB", "add", sector="Financials"), _n("A", "CCC", "build", sector="Financials"),
          _n("B", "DDD", "trim"), _n("B", "EEE", "exit"), _n("B", "FFF", "trim_loss"),
          _n("C", "GGG", "add"), _n("C", "HHH", "add", date="2026-09-01"),
          _n("D", "III", "trim", style="养老金"), _n("D", "JJJ", "add", style="养老金")]
    insight.add_context(ns)
    a = ns[0]["insight"]
    assert a["tags"] == ["换股"] and "BBB、CCC" in a["reasons"][1] and "行业" not in ns[1]["insight"]["reasons"][1] and "卖的是原材料/矿业、买的多是金融，像行业轮动" in a["reasons"][1]
    assert a["reasons"][-1].startswith("主动选股基金：")                               # 插在基金类型那句前面
    assert ns[1]["insight"]["tags"] == ["换股"]
    assert ns[3]["insight"]["tags"] == ["整体减仓"] and "应付赎回" in ns[3]["insight"]["reasons"][1]
    assert "tags" not in ns[6]["insight"]                                              # 另一条隔了两个月，不算同期
    assert not ns[8]["insight"].get("tags") and "整体并没有在撤" in ns[8]["insight"]["reasons"][1]   # 养老金：只提一句，不打“换股”标签


def test_add_context_peers_on_same_stock():
    ns = [_n("A", "XYZ", "add"), _n("B", "XYZ", "add_dip", date="2026-10-20"), _n("C", "XYZ", "exit_loss", date="2026-10-25"),
          _n("E", "XYZ", "lending"), _n("F", "XYZ", "trim", date="2026-08-01")]
    insight.add_context(ns)
    assert ns[0]["insight"]["tags"] == ["多家同向", "有人反向"]
    assert ns[2]["insight"]["tags"] == ["有人反向"]
    assert "tags" not in ns[3]["insight"] and "tags" not in ns[4]["insight"]
    insight.add_context([{"holder": "A", "code": "X", "insight": None}, {"holder": "A"}])   # 缺字段不出错


def test_price_context_window_volume():
    series = [[int(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000) + i * 86400000, 1.0 + i * 0.001, 1000] for i in range(300)]
    px = insight.price_context(series, "2026-10-20", "2026-10-01", "2026-10-10")
    assert px["t_vol"] == 10_000
    assert "t_vol" not in insight.price_context(series, "2026-10-20", "2025-06-01", "2026-10-10")   # 明细开始得比日线还早：算不全，不给


def test_trade_rows_with_glued_side():
    pg = "Date  Person  Consideration  Number  Class\n26/10/2026 Primary Person $145,627.04 89,799 SecuritiesBuy\n23/10/2026 Primary Person $66,327.55 42,861 SecuritiesSell\n"
    s = insight.parse_trades([pg], "2026-10-28", "change")["summary"]
    assert s["buy_qty"] == 89_799 and s["sell_qty"] == 42_861


def test_history_survives_pruning_and_feeds_cost(sandbox, monkeypatch):
    monkeypatch.setitem(PDFS, "1001", TRADE_604)
    page = (FIXTURES / "company_anns_ppe.html").read_text(encoding="utf-8")
    fake = FakeASX(prices={"SLC": falling_prices()}, events={"SLC": page}, sectors={"SLC": "Communication Services"})
    monkeypatch.setattr(scrape, "fetch", fake)
    assert scrape.main() == 0
    hist = read(sandbox / "data" / "history.json")
    key = next(k for k in hist["pos"] if k.endswith("|SLC"))
    rec = hist["pos"][key][0]
    assert rec["id"] == "1001" and rec["buy_qty"] == 2_000_000 and rec["buy_price"] == 2.05 and rec["votes"] == [10_000_000, 12_000_000]
    out = read(sandbox / "data" / "notices.json")
    n = next(x for x in out["notices"] if x["code"] == "SLC")
    assert n["sector"] == "Communication Services" and out["last_run"]["insight"]["history_added"] >= 1
    e = read(sandbox / "data" / "all_notices.json")["entries"]["1001"]
    assert isinstance(e["ev"], list) and fake.event_calls.count("SLC") == 1
    # 再跑不重复记、不重复取
    scrape.main()
    assert len(read(sandbox / "data" / "history.json")["pos"][key]) == 1 and fake.event_calls.count("SLC") == 1
    # 公告过了保留期被清掉，持仓历史还在；之后的卖出能用上成本
    holders, _ = scrape.load_watchlist()
    later = {"id": "9", "date": "2027-03-01", "time": "10:00", "type": "change", "votes": [12_000_000, 9_000_000]}
    c = scrape.cost_for(hist, key.split("|")[0], "SLC", later)
    assert c["avg"] == 2.05 and 0.15 < c["cov"] < 0.2
    assert scrape.cost_for(hist, key.split("|")[0], "SLC", {"id": "1001", "date": rec["date"], "time": rec["time"]}) == {}   # 不拿自己算自己
    assert scrape.cost_for({}, "X", "Y", later) == {} and scrape.cost_for(None, "X", "Y", later) == {}


def test_events_failure_does_not_break_run(sandbox, monkeypatch):
    monkeypatch.setitem(PDFS, "1001", TRADE_604)
    fake = FakeASX(prices={"SLC": falling_prices()}, events={"SLC": "<html>blocked</html>"})
    monkeypatch.setattr(scrape, "fetch", fake)
    assert scrape.main() == 0
    out = read(sandbox / "data" / "notices.json")
    n = next(x for x in out["notices"] if x["code"] == "SLC")
    assert n["insight"]["action"] == "add_dip" and out["last_run"]["errors"] == [] and n["sector"] == ""


def test_corrupt_history_file_is_replaced(sandbox, monkeypatch):
    (sandbox / "data" / "history.json").write_text('["not", "a dict"]', encoding="utf-8")
    monkeypatch.setattr(scrape, "fetch", FakeASX())
    assert scrape.main() == 0
    assert isinstance(read(sandbox / "data" / "history.json")["pos"], dict)


def test_event_moves():
    day = lambda i: int(dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc).timestamp() * 1000) + i * 86400000
    series = [[day(i), p, 1000] for i, p in enumerate([1.0, 1.0, 1.0, 1.3, 1.32, 1.3])]      # 10/1..10/6
    evs = [{"d": "2026-10-04", "k": "takeover"}, {"d": "2026-10-03", "k": "results"}, {"d": "2026-09-01", "k": "raise"},
           {"d": "2026-12-01", "k": "raise"}]
    insight.event_moves(series, evs)
    assert evs[0]["j"] == 0.32 and evs[1]["j"] == 0.3            # 前一天收盘 → 公告日后第二个收盘
    assert "j" not in evs[2] and "j" not in evs[3]               # 日线范围以外
    insight.event_moves([], [{"d": "2026-10-04"}])
    insight.event_moves([["x"], None], [{"d": "2026-10-04"}])


# ---------------------------------------------------------------- 第九轮：目的打分

def _f(side="buy", **kw):
    f = {"side": side, "t": "change", "style": "active", "size": 0.1, "part": None, "offmkt": False, "f_up": 0.0, "f_down": 0.0,
         "u3": 0.0, "lowpos": 0.0, "highpos": 0.0, "plc": 0.0, "raise": False, "so_up": False, "target": False, "res_j": None,
         "cost_d": None, "gain": None, "own_gain": None, "seq_same": 0, "opp_days": None, "base": "add" if side == "buy" else "trim", "q": 2}
    f.update(kw)
    return f


def top(f, ctx=None):
    r = insight.score_purposes(f, ctx)
    assert abs(sum(x["p"] for x in r) - 1) < 0.02 and r == sorted(r, key=lambda x: -x["s"])
    return r[0]["k"]


def test_score_baseline_wins_without_evidence():
    assert top(_f("buy")) == "base" and top(_f("sell")) == "base"
    assert top(_f("buy", lowpos=1.0)) == "base"                       # 只是在低位、没跌过：不算逢低
    assert top(_f("sell", f_up=1.0)) == "base"                        # 只是涨过、不在高位：分数刚好和基准线持平


def test_score_price_patterns():
    assert top(_f("buy", f_down=1.0)) == "dip" and top(_f("buy", f_down=0.5, lowpos=0.5)) == "dip"
    assert top(_f("buy", u3=1.0, highpos=0.5)) == "mom"
    assert top(_f("sell", f_up=1.0, highpos=0.5)) == "profit"
    assert top(_f("sell", f_down=1.0)) == "stop" and top(_f("sell", f_down=0.5, lowpos=0.5)) == "stop"


def test_score_cost_overrides_price_pattern():
    # 股价在高位、涨了很多，但它自己是亏钱卖的：止损
    assert top(_f("sell", f_up=1.0, highpos=1.0, gain=-0.3)) == "stop"
    assert top(_f("sell", f_down=1.0, lowpos=1.0, gain=0.4)) == "profit"
    assert top(_f("sell", f_up=1.0, highpos=0.5, gain=0.02)) == "base"          # 和成本差不多


def test_score_mechanism_beats_timing():
    # 跌了一轮之后买，但其实是认购新股：参与融资，不是逢低
    assert top(_f("buy", f_down=1.3, lowpos=1.0, **{"raise": True, "so_up": True})) == "plc"
    assert top(_f("buy", f_down=1.3, lowpos=1.0, **{"raise": True})) == "dip"   # 只有融资公告，没有别的佐证
    assert top(_f("buy", plc=1.0, u3=1.3, highpos=1.0)) == "plc"
    assert top(_f("buy", target=True, style="hedge")) == "arb" and top(_f("buy", target=True, style="active")) == "base"


def test_score_cross_stock_context():
    assert top(_f("sell"), {"ns": 3, "nb": 0}) == "flow" and top(_f("sell"), {"ns": 1, "nb": 0}) == "base"
    assert top(_f("sell"), {"ns": 0, "nb": 3}) == "swap" and top(_f("sell", style="super"), {"ns": 0, "nb": 3}) == "base"
    assert top(_f("buy"), {"nb": 5, "ns": 0}) == "flow" and top(_f("buy"), {"nb": 2, "ns": 0}) == "base"
    assert top(_f("buy"), {"nb": 0, "ns": 4}) == "swap"
    assert top(_f("sell", f_down=1.3, lowpos=1.0), {"ns": 3, "nb": 0}) == "stop"   # 个股证据强的时候不被横向证据盖过


def test_score_short_term_and_rebalance():
    assert top(_f("sell", opp_days=20)) == "short" and top(_f("sell", opp_days=90)) == "base"
    assert top(_f("buy", opp_days=10, style="hedge")) == "short"
    assert top(_f("sell", size=0.08, f_up=0.9, part=0.02, style="super")) == "rebal"
    assert top(_f("sell", size=0.5, f_up=0.9)) == "base"


def test_finalize_label_confidence_and_summary():
    n = _sell(cost={"avg": 1.2, "cov": 0.8, "since": "2026-01-10", "n": 3})
    r = insight.judge(n)
    assert r["action"] == "trim_profit" and r["purposes"][0]["k"] == "profit" and r["purposes"][0]["p"] > 0.5
    assert len(r["purposes"]) == 3 and sum(1 for x in r["reasons"] if x.startswith("综合打分：")) == 1
    assert any("最像「获利了结」" in x and "卖出价高于自己的买入成本" in x for x in r["reasons"])
    plain = insight.judge(_sell())
    assert plain["action"] == "trim" and any("按普通减仓看" in x for x in plain["reasons"]) and plain["confidence"] == "中"
    assert isinstance(plain["strength"], int) and 0 <= plain["strength"] <= 100
    # 横向证据来了以后重新打分：只留一条“综合打分”
    insight.finalize(plain, {"ns": 4, "nb": 0, "peer_same": 1})
    assert plain["action"] == "trim_flow" and plain["tone"] == "neutral" and sum(1 for x in plain["reasons"] if x.startswith("综合打分：")) == 1
    assert plain["reasons"][-1].startswith("主动选股基金：")
    insight.finalize({"action": "rotate", "reasons": []})                 # 没有打分数据的不动


def test_add_context_rescores_and_build_notices_strips_internal(sandbox, monkeypatch):
    ns = [{"holder": "A", "code": k, "date": "2026-11-05", "sector": "", "insight": insight.judge(_sell(date="2026-11-05"))}
          for k in ("AAA", "BBB", "CCC", "DDD", "EEE")]
    insight.add_context(ns)
    assert all(n["insight"]["action"] == "trim_flow" and "整体减仓" in n["insight"]["tags"] for n in ns)
    two = [{"holder": "B", "code": k, "date": "2026-11-05", "sector": "", "insight": insight.judge(_sell(date="2026-11-05"))} for k in ("AAA", "BBB", "CCC")]
    insight.add_context(two)
    assert all(n["insight"]["action"] == "trim" and "整体减仓" in n["insight"]["tags"] for n in two)   # 只有三只：提一句，但不够改标签
    monkeypatch.setitem(PDFS, "1001", TRADE_604)
    monkeypatch.setattr(scrape, "fetch", FakeASX(prices={"SLC": falling_prices()}))
    assert scrape.main() == 0
    n = next(x for x in read(sandbox / "data" / "notices.json")["notices"] if x["code"] == "SLC")
    assert "_f" not in n["insight"] and n["insight"]["purposes"][0]["k"] == "dip" and n["insight"]["strength"] is not None


def test_sequence_for():
    hist = {"pos": {"A|XYZ": [
        {"id": "1", "date": "2026-06-01", "time": "", "type": "becoming", "votes": [100]},
        {"id": "2", "date": "2026-09-01", "time": "", "type": "change", "votes": [100, 80]},
        {"id": "3", "date": "2026-10-01", "time": "", "type": "change", "votes": [80, 120]},
        {"id": "4", "date": "2026-10-20", "time": "", "type": "change", "vp": [6.0, 7.0], "votes": []}]}}
    e = {"id": "5", "date": "2026-11-05", "time": "10:00", "type": "change", "votes": [150, 180]}
    assert scrape.sequence_for(hist, "A", "XYZ", e) == {"same": 2, "opp_days": 65}
    sell = {"id": "6", "date": "2026-11-05", "time": "10:00", "type": "ceasing"}
    assert scrape.sequence_for(hist, "A", "XYZ", sell) == {"same": 0, "opp_days": 16}
    assert scrape.sequence_for(hist, "A", "NOPE", e) == {"same": 0, "opp_days": None}
    assert scrape.sequence_for(hist, "A", "XYZ", {"id": "7", "date": "bad", "type": "becoming"}) == {}
    assert scrape.sequence_for(None, "A", "XYZ", {"id": "8", "date": "2026-11-05", "type": "change", "votes": [5, 5]}) == {}
    r = insight.judge(_buy(seq={"same": 2, "opp_days": None}))
    assert "连续加仓" in r["tags"] and any("连续第 3 次加仓" in x for x in r["reasons"])
    r = insight.judge(_sell(seq={"same": 0, "opp_days": 12}))
    assert r["action"] == "short_out" and any("12 天前的公告还是加仓" in x for x in r["reasons"])


def test_strength():
    assert insight.strength_of(_f(size=None)) is None
    assert insight.strength_of(_f(size=0.3, part=0.3, seq_same=2), {"peer_same": 1}) == 100
    assert insight.strength_of(_f(size=0.03, part=0.01)) < 15
    assert insight.strength_of(_f(size=0.3, offmkt=True)) > 70


def test_close_call_keeps_generic_label():
    f = _f("buy", f_down=0.9)                      # 逢低 0.78
    ins = {"reasons": ["主动选股基金：x。"], "style": "主动选股基金", "_f": f}
    insight.finalize(ins, {"nb": 5, "ns": 0})     # 整体加仓 0.9：两种目的几乎同分
    assert ins["action"] == "add" and "分不出主次" in ins["reasons"][0] and ins["purposes"][0]["k"] in ("flow", "dip")
    insight.finalize(ins, {"nb": 0, "ns": 0})     # 没有横向证据时，逢低是唯一高过基准线的
    assert ins["action"] == "add_dip" and "最像「逢低买入」" in ins["reasons"][0] and len(ins["reasons"]) == 2


# ---------------------------------------------------------------- 第十轮：回补历史、逐笔时间线、回测

def year_page(rows):
    trs = "".join(f'<tr><td>{d}<br/><span class="dates-time">{t}</span></td><td class="pricesens"></td>'
                  f'<td><a href="/asx/v2/statistics/displayAnnouncement.do?display=pdf&amp;idsId={i}">{h}<br/>3 pages 200KB</a></td></tr>'
                  for d, t, h, i in rows)
    return f"<html><h2>Search results</h2><table><tr><th>Date</th></tr>{trs}</table></html>"


OLD_604 = ["Form 604", "To Company Name/Scheme Superloop Limited", "1. Details of substantial holder (1)",
           "Name Paradice Investment Management Pty Ltd", "There was a change in the interests of the",
           "substantial holder on 31/08/2026", "The previous notice was given to the company on 02/06/2026",
           "2. Previous and present voting power", "Ordinary 8,500,000 5.20% 10,000,000 6.10%",
           "3. Changes in relevant interests",
           "24/08/2026 Paradice On market purchase $1,500,000.00 500,000 500,000",
           "25/08/2026 Paradice On market purchase $1,450,000.00 500,000 500,000",
           "26/08/2026 Paradice On market purchase $1,400,000.00 250,000 250,000",
           "27/08/2026 Paradice On market purchase $700,000.00 250,000 250,000",
           "4. Present relevant interests"]
OLD_603 = ["Form 603", "To Company Name/Scheme Superloop Limited", "1. Details of substantial holder (1)",
           "Name Paradice Investment Management Pty Ltd", "The holder became a substantial holder on 01/06/2026",
           "2. Details of voting power", "Fully paid ordinary shares 8,500,000 8,500,000 5.20%"]
OTHER_604 = ["Form 604", "To Company Name/Scheme Superloop Limited", "1. Details of substantial holder (1)",
             "Name Some Random Fund Pty Ltd", "There was a change in the interests of the substantial holder on 31/08/2026",
             "2. Previous and present voting power", "Ordinary 1 6.01% 2 7.05%"]


def test_parse_prev_notice():
    assert scrape.parse_prev_notice("The previous notice was given to the company on 21/10/2026\nThe previous notice was dated 20/10/2026") == "2026-10-21"
    assert scrape.parse_prev_notice("The previous notice was dated 29 October 2026") == "2026-10-29"
    assert scrape.parse_prev_notice("The previous notice was given to the company on: 09/11/2026") == "2026-11-09"
    assert scrape.parse_prev_notice("The previous notice was given to the company on 31 / 08 / 2026") == "2026-08-31"
    assert scrape.parse_prev_notice("The previous notice was given to the company on\n") == "" and scrape.parse_prev_notice("") == ""


def test_backfill_chases_previous_notices(sandbox, monkeypatch):
    monkeypatch.setitem(PDFS, "1001", TRADE_604)                       # 上一份公告：01/09/2026
    monkeypatch.setitem(PDFS, "2001", OLD_604)
    monkeypatch.setitem(PDFS, "2002", OTHER_604)
    monkeypatch.setitem(PDFS, "2003", OLD_603)
    years = {("SLC", 2026): year_page([
        ("28/10/2026", "5:00 pm", "Change in substantial holding", "1001"),
        ("01/09/2026", "9:00 am", "Change in substantial holding", "2002"),     # 别人的，同一天
        ("01/09/2026", "4:00 pm", "Change in substantial holding", "2001"),
        ("15/07/2026", "4:00 pm", "Annual Report", "2999"),
        ("02/06/2026", "4:00 pm", "Becoming a substantial holder", "2003")])}
    fake = FakeASX(prices={"SLC": falling_prices()}, years=years)
    monkeypatch.setattr(scrape, "fetch", fake)
    assert scrape.main() == 0
    hist = read(sandbox / "data" / "history.json")
    key = next(k for k in hist["pos"] if k.endswith("|SLC"))
    rows = hist["pos"][key]
    assert [r["id"] for r in rows] == ["2003", "2001", "1001"] and rows[0]["type"] == "becoming" and rows[1]["bf"] == 1
    assert rows[1]["buy_qty"] == 1_500_000 and rows[1]["prev_notice"] == "2026-06-02" and rows[1]["px"]["p"] > 0
    assert rows[1]["tl"]["n"] == 4 and rows[1]["fwd"]["1m"] < 0 and "3m" not in rows[1]["fwd"]      # 3 个月还没到
    assert hist["meta"][key]["done"] == "start"
    run = read(sandbox / "data" / "notices.json")["last_run"]["insight"]
    assert run["backfill_read"] == 3 and run["backfill_added"] == 2
    # 这次的公告现在有成本价，也知道是连续加仓
    n = next(x for x in read(sandbox / "data" / "notices.json")["notices"] if x["code"] == "SLC")
    assert "连续加仓" in n["insight"]["tags"] and any("连续第 2 次加仓" in r for r in n["insight"]["reasons"])
    c = scrape.cost_for(hist, key.split("|")[0], "SLC", {"id": "1001", "date": "2026-10-30", "time": "17:00", "type": "change", "votes": [10_000_000, 12_000_000]})
    assert c["n"] == 1 and 3.3 < c["avg"] < 3.4 and c["cov"] == 0.15           # 只知道 150 万股的成本，不到四成，所以解读里不拿它下结论
    # 追完了：再跑不再读旧公告
    fake.pdf_downloads.clear()
    scrape.main()
    assert fake.pdf_downloads == []
    cal = read(sandbox / "data" / "calibration.json")
    assert sum(v["n"] for v in cal.values()) >= 2 and all(":" in k for k in cal)


def test_backfill_budget_and_missing_pages(sandbox, monkeypatch):
    monkeypatch.setitem(PDFS, "1001", TRADE_604)
    monkeypatch.setattr(scrape, "BACKFILL_PDFS", 0)
    fake = FakeASX(prices={"SLC": falling_prices()})
    monkeypatch.setattr(scrape, "fetch", fake)
    assert scrape.main() == 0
    hist = read(sandbox / "data" / "history.json")
    key = next(k for k in hist["pos"] if k.endswith("|SLC"))
    assert not (hist.get("meta", {}).get(key) or {}).get("done") and fake.year_calls == []        # 没名额：留到下次
    monkeypatch.setattr(scrape, "BACKFILL_PDFS", 50)
    assert scrape.main() == 0                                                                      # 公司那年没有对得上的公告
    hist = read(sandbox / "data" / "history.json")
    assert hist["meta"][key]["done"] == "not_found" and read(sandbox / "data" / "notices.json")["last_run"]["errors"] == []


def test_timeline_and_forward_returns():
    day = lambda i: int(dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc).timestamp() * 1000) + i * 86400000
    closes = [1.00, 0.95, 0.97, 0.90, 0.92, 0.85, 0.86, 0.80, 0.82, 0.78]
    series = [[day(i), p, 1000] for i, p in enumerate(closes)]
    iso = lambda i: (dt.date(2026, 9, 1) + dt.timedelta(days=i)).isoformat()
    daily = [[iso(1), 400], [iso(3), 300], [iso(4), 50], [iso(5), 200], [iso(7), 300], [iso(8), -100]]
    tl = insight.timeline(series, daily, "buy")
    assert tl["n"] == 5 and tl["down_share"] == 0.96 and tl["r_win"] == -0.2 and tl["base_down"] == round(4 / 7, 3)
    assert insight.timeline(series, daily, "sell") == {} and insight.timeline([], daily, "buy") == {}
    assert insight.timeline(series, [["x"], None, [iso(1), "a"]], "buy") == {}
    f = _f("buy", tl=tl)
    assert top(f) == "dip" and any("集中在股价下跌的日子" in w for w in insight.score_purposes(f)[0]["why"])
    assert insight.forward_returns(series, iso(0)) == {} and insight.forward_returns(series, "2030-01-01") == {}
    long = [[day(i), 1.0 + i * 0.01, 1] for i in range(200)]
    fw = insight.forward_returns(long, iso(0))
    assert fw["1m"] == 0.3 and fw["3m"] == 0.91 and fw["6m"] == 1.82


def test_parse_company_rows():
    rows = scrape.parse_company_rows((FIXTURES / "company_anns_ppe.html").read_text(encoding="utf-8"))
    r = next(x for x in rows if x["id"] == "03161285")
    assert r["date"] == "2026-11-02" and r["time"] == "18:31" and r["headline"] == "Change in substantial holding"
    assert scrape.parse_company_rows("<html>blocked</html>") is None


# ---------------------------------------------------------------- 第十一轮：趋势结构、持仓阶段、基金习惯、交易形状

def _px(**kw):
    px = {"p": 2.0, "r1m": 0.0, "r3m": 0.02, "r6m": 0.05, "r12m": 0.05, "pos": 0.5, "hi": 2.5, "lo": 1.5, "t_lo": 1.9, "t_hi": 2.1}
    px.update(kw)
    return px


def test_pullback_buy_is_not_dip_or_chase():
    # 一年涨了 80%，最近 3 个月回落 20%，股价在区间中部：回调买入
    r = insight.judge(_buy(px=_px(r12m=0.8, r6m=0.1, r3m=-0.2, r1m=-0.1, pos=0.55)))
    assert r["action"] == "add_pull" and r["tone"] == "bull" and r["purposes"][0]["k"] == "pull"
    assert any("上涨途中的回调" in x for x in r["reasons"])
    # 同样的回落，但已经跌到一年的最低位：那是逢低，不是回调
    r = insight.judge(_buy(px=_px(r12m=0.8, r6m=-0.3, r3m=-0.4, r1m=-0.2, pos=0.1)))
    assert r["action"] == "add_dip" and all(p["k"] != "pull" for p in r["purposes"])
    # 回落太小（1 个月 -8%）不算
    r = insight.judge(_buy(px=_px(r12m=0.8, r6m=0.3, r3m=0.05, r1m=-0.08, pos=0.7)))
    assert r["action"] != "add_pull" and not any("上涨途中的回调" in x for x in r["reasons"])
    # 长期没涨：不算
    assert insight.judge(_buy(px=_px(r12m=0.1, r3m=-0.2, pos=0.5)))["action"] != "add_pull"
    # 603 的基础标签不变（和逢低/顺势一样，只进理由和概率）
    r = insight.judge(_buy(type="becoming", voting_power=[5.5], votes=[8_000_000], px=_px(r12m=0.8, r3m=-0.2, pos=0.55)))
    assert r["action"] in ("build", "cross_up") and r["purposes"][0]["k"] == "pull"


def test_bounce_sell_depends_on_cost():
    px = _px(r12m=-0.55, r6m=0.3, r3m=0.9, r1m=0.1, pos=0.3)
    r = insight.judge(_sell(px=px))
    assert r["action"] == "trim_bounce" and r["tone"] == "bear" and any("趁反弹卖" in x for x in r["reasons"])
    # 知道成本而且是赚钱卖的：就是获利了结
    r = insight.judge(_sell(px=px, cost={"avg": 1.0, "cov": 0.9, "since": "2026-01-10", "n": 3}))
    assert r["action"] == "trim_profit"
    # 知道成本而且亏着：反弹减亏和止损不冲突，不算“分不出主次”
    r = insight.judge(_sell(px=px, cost={"avg": 4.0, "cov": 0.9, "since": "2025-01-10", "n": 3}))
    assert r["action"] in ("trim_bounce", "trim_loss") and {p["k"] for p in r["purposes"][:2]} == {"bounce", "stop"}
    assert not any("分不出主次" in x for x in r["reasons"])
    # 退出类
    r = insight.judge(_sell(type="ceasing", voting_power=[], votes=[], px=px, prev={"vp": 7.0, "votes": 7_000_000, "type": "change", "date": "2026-08-01"}))
    assert r["action"] == "exit_bounce"
    # 长期没怎么跌：不算
    assert insight.judge(_sell(px=_px(r12m=-0.1, r3m=0.3)))["action"] != "trim_bounce"


HIST11 = {"pos": {"A|XYZ": [
    {"id": "1", "date": "2025-03-10", "time": "", "type": "becoming", "cd": "2025-03-06", "first": "2024-12-01", "vp": [5.2], "votes": [5_200_000],
     "buy_qty": 4_000_000, "sell_qty": 0},
    {"id": "2", "date": "2025-08-01", "time": "", "type": "change", "cd": "2025-07-30", "vp": [5.2, 10.0], "votes": [5_200_000, 10_000_000],
     "buy_qty": 4_800_000, "sell_qty": 0},
    {"id": "3", "date": "2026-06-01", "time": "", "type": "change", "cd": "2026-05-28", "vp": [10.0, 8.0], "votes": [10_000_000, 8_000_000],
     "buy_qty": 0, "sell_qty": 2_000_000},
]}}


def test_stage_for():
    e = {"id": "4", "date": "2026-11-05", "time": "10:00", "type": "change", "voting_power": [8.0, 6.0], "votes": [8_000_000, 6_000_000]}
    st = scrape.stage_for(HIST11, "A", "XYZ", e)
    assert st["peak"] == 10.0 and st["off"] == 0.4 and st["off_before"] == 0.2 and st["by"] == "股数"
    assert (st["n_buy"], st["n_sell"], st["known_start"], st["fresh"]) == (2, 1, True, True)
    assert st["age"] == (dt.date(2026, 11, 5) - dt.date(2024, 12, 1)).days      # 知道建仓时间：从第一笔买入算
    # 退出类：只知道在 5% 以下，按“至少”算，用比例
    st = scrape.stage_for(HIST11, "A", "XYZ", {"id": "5", "date": "2026-11-05", "time": "", "type": "ceasing"})
    assert st["off"] == 0.5 and st["by"] == "比例"
    # 没有更早的记录、日期坏了、没有历史：空
    assert scrape.stage_for(HIST11, "A", "NOPE", e) == {}
    assert scrape.stage_for(None, "A", "XYZ", e) == {}
    assert scrape.stage_for(HIST11, "A", "XYZ", dict(e, date="bad")) == {}
    # 自己这份不算“之前”；比第一份还早的也没有“之前”
    assert scrape.stage_for(HIST11, "A", "XYZ", {"id": "1", "date": "2025-03-10", "time": "", "type": "becoming", "voting_power": [5.2]}) == {}


def test_stage_handles_share_consolidation_dilution_and_reentry():
    # 10 合 1：合并之前的股数不能拿来比，从合并之后重新算
    hist = {"pos": {"A|XYZ": [
        {"id": "1", "date": "2025-03-10", "time": "", "type": "becoming", "vp": [8.0], "votes": [80_000_000]},
        {"id": "2", "date": "2025-09-10", "time": "", "type": "change", "vp": [8.0, 9.0], "votes": [8_000_000, 9_000_000]}]}}
    e = {"id": "3", "date": "2026-02-01", "time": "", "type": "change", "voting_power": [9.0, 8.5], "votes": [9_000_000, 8_500_000]}
    st = scrape.stage_for(hist, "A", "XYZ", e)
    assert st["off"] == round(1 - 8.5 / 9, 3) and st["by"] == "股数" and st["peak"] == 9.0
    # 这份公告自己前后就差了几倍（合并发生在这一份里）：不给
    assert scrape.stage_for(hist, "A", "XYZ", dict(e, voting_power=[9.0, 9.0], votes=[9_000_000, 900_000])) == {}
    # 有一份没读到股数：退回用比例
    hist2 = {"pos": {"A|XYZ": [{"id": "1", "date": "2025-03-10", "time": "", "type": "becoming", "vp": [10.0], "votes": []}]}}
    st = scrape.stage_for(hist2, "A", "XYZ", e)
    assert st["by"] == "比例" and st["off"] == 0.15 and st["fresh"] is False
    # 退出过又回来：只看回来之后这一段
    hist3 = {"pos": {"A|XYZ": HIST11["pos"]["A|XYZ"] + [
        {"id": "9", "date": "2026-07-01", "time": "", "type": "ceasing", "vp": [], "votes": []},
        {"id": "10", "date": "2026-09-01", "time": "", "type": "becoming", "cd": "2026-08-28", "vp": [5.5], "votes": [5_500_000], "buy_qty": 0}]}}
    st = scrape.stage_for(hist3, "A", "XYZ", {"id": "11", "date": "2026-11-05", "time": "", "type": "change", "voting_power": [5.5, 6.5],
                                                "votes": [5_500_000, 6_500_000]})
    assert st["peak"] == 5.5 and st["off"] == 0 and (st["n_buy"], st["n_sell"]) == (1, 0) and st["fresh"] is False
    assert st["age"] == (dt.date(2026, 11, 5) - dt.date(2026, 8, 28)).days     # 不知道建仓时间：从越过 5% 那天算
    assert scrape.stage_for(hist3, "A", "XYZ", {"id": "10", "date": "2026-09-01", "time": "", "type": "becoming", "voting_power": [5.5]}) == {}


def _stage(**kw):
    st = {"age": 600, "known_start": True, "fresh": True, "peak": 10.0, "off": 0.4, "off_before": 0.2, "by": "股数", "n_buy": 2, "n_sell": 1, "hist": 5}
    st.update(kw)
    return st


def test_stage_drives_wind_rebal_and_short():
    # 已经比最高点少了四成、连续第 3 次减：分批减仓
    r = insight.judge(_sell(stage=_stage(), seq={"same": 2, "opp_days": None}))
    assert r["action"] == "trim_wind" and "已减三成" in r["tags"] and any(x.startswith("持仓阶段：从建仓算起约 20 个月") for x in r["reasons"])
    assert any("比最高点少了 40%（按股数算）" in x for x in r["reasons"])
    r = insight.judge(_sell(type="ceasing", voting_power=[], votes=[], stage=_stage(off=0.5, by="比例"), seq={"same": 1, "opp_days": None},
                            prev={"vp": 7.0, "votes": 7_000_000, "type": "change", "date": "2026-08-01"}))
    assert r["action"] == "exit_wind" and any("至少少了 50%（按持股比例算" in x for x in r["reasons"])
    # 之前没减过：一次卖出不叫“分批”
    r = insight.judge(_sell(stage=_stage(n_sell=0)))
    assert r["action"] != "trim_wind" and "已减三成" not in r["tags"] and any("建仓以来第一次减仓" in x for x in r["reasons"])
    # 涨了之后小幅减：以前是“仓位管理”；但已经减掉三成以上的就不是“减一点”了
    small = dict(voting_power=[8.0, 7.2], votes=[8_000_000, 7_200_000], style="super", px=_px(r6m=0.15, pos=0.5, t_vol=50_000_000))
    tr = dict(_sell()["trades"], sell_qty=800_000, net_qty=-800_000)
    assert insight.judge(_sell(trades=tr, **small))["action"] == "trim_rebal"
    assert insight.judge(_sell(trades=tr, stage=_stage(off=0.05, n_sell=0, age=400), **small))["action"] == "trim_rebal"   # 成为大股东一年多第一次小减
    assert insight.judge(_sell(trades=tr, stage=_stage(off=0.35), **small))["action"] != "trim_rebal"
    # 刚建仓 3 个月就卖：短线
    quick = _stage(age=90, off=0.25, n_buy=1, n_sell=0)
    sc = lambda **kw: {p["k"]: p["s"] for p in insight.score_purposes(insight.judge(_sell(**kw))["_f"])}
    assert sc(stage=quick)["short"] == pytest.approx(sc()["short"] + 1.6)
    assert insight.judge(_sell(stage=quick))["action"] == "trim"                       # 单凭这一条不够，还是普通减仓
    r = insight.judge(_sell(stage=quick, habit={"n": 30, "follow": 0.5, "rev": 0.3, "n_buy": 30, "contr": 0.5}))
    assert r["purposes"][0]["k"] == "short" and r["action"] == "short_out" and any("建仓才 3 个月就开始卖" in x for x in r["reasons"])
    # 上一份反向公告就在 60 天内的，已经按“刚加仓就掉头”加过分，不重复加
    assert sc(stage=quick, seq={"same": 0, "opp_days": 20})["short"] == pytest.approx(sc(seq={"same": 0, "opp_days": 20})["short"])
    # 603 只是“越过 5%”、不知道什么时候建的仓：不能说刚买就卖
    r = insight.judge(_sell(stage=_stage(age=90, off=0.25, n_buy=1, n_sell=0, fresh=False)))
    assert r["action"] == "trim" and any("成为大股东以来第一次减仓" in x for x in r["reasons"])
    # 坏的输入不出错
    for bad in ({"age": None}, {"age": 10}, {"age": "x", "peak": 5}, {"age": 10, "peak": 0}, "oops" and {}):
        assert insight.judge(_sell(stage=bad))["action"] == "trim"


def test_reentry_wording_needs_a_real_sale():
    r = insight.judge(_buy(stage=_stage(off=0.1, off_before=0.3, n_sell=2)))
    assert any("之前已经从最高点减掉过 30%，这次是往回买" in x for x in r["reasons"])
    r = insight.judge(_buy(stage=_stage(off=0.1, off_before=0.3, n_sell=0, by="比例")))      # 比例掉了但没卖过：是被摊薄，不说“减掉过”
    assert not any("往回买" in x for x in r["reasons"])
    r = insight.judge(_buy(stage=_stage(off=0.0, off_before=0.0, n_sell=0)))
    assert any("持仓的新高" in x for x in r["reasons"])


def test_habits_counts_shrinks_and_respects_as_of():
    def rows(dirs, start="2025-01-01", step=40, px=None):
        out, d, v = [], dt.date.fromisoformat(start), 100
        for i, s in enumerate(dirs):
            nv = v + 10 if s == "b" else v - 10
            out.append({"id": f"{start}-{i}", "date": (d + dt.timedelta(days=step * i)).isoformat(), "time": "", "type": "change",
                        "votes": [v, nv], "px": px or {}})
            v = nv
        return out
    hist = {"pos": {"Steady|AAA": rows("bbbbbbbbbbbbb", px={"pos": 0.2, "r3m": -0.2}),            # 12 次有后续，全部同方向；都在弱的时候买
                    "Flippy|BBB": rows("bsbsbsbsbsbsb", px={"pos": 0.9, "r3m": 0.3})}}            # 12 次有后续，全部 40 天内掉头
    h = scrape.habits(hist)
    assert h["Steady"]["n"] == 12 and h["Flippy"]["n"] == 12
    assert h["Steady"]["follow"] == round((12 + 8 * 0.5) / 20, 3) and h["Flippy"]["follow"] == round((0 + 8 * 0.5) / 20, 3)   # 向全体平均（0.5）收缩
    assert h["Steady"]["rev"] == round((0 + 8 * 0.5) / 20, 3) and h["Flippy"]["rev"] == round((12 + 8 * 0.5) / 20, 3)
    assert h["Steady"]["contr"] > 0.8 > 0.5 > h["Flippy"]["contr"] and h["Steady"]["n_buy"] == 13 and h["Flippy"]["n_buy"] == 7
    # as_of：只数那一天之前的（回头看时不偷看之后的事）
    early = scrape.habits(hist, as_of="2025-03-01")
    assert early["Steady"]["n"] == 1 and early["Flippy"]["n"] == 1
    assert scrape.habits(hist, as_of="2024-01-01") == {} and scrape.habits(None) == {} and scrape.habits({"pos": {}}) == {}
    # 掉头但隔了很久：不算“3 个月内掉头”
    slow = scrape.habits({"pos": {"Slow|CCC": rows("bsbsbsb", step=200)}})
    assert slow["Slow"]["rev"] == 0 and slow["Slow"]["follow"] == 0


def test_habit_feeds_scoring_and_reason():
    steady = {"n": 30, "follow": 0.9, "rev": 0.03, "n_buy": 30, "contr": 0.7}
    flippy = {"n": 30, "follow": 0.5, "rev": 0.3, "n_buy": 30, "contr": 0.4}
    a = insight.judge(_buy(habit=steady, seq={"same": 1, "opp_days": None}))
    b = insight.judge(_buy(habit=flippy, seq={"same": 1, "opp_days": None}))
    assert any("这家基金的习惯（按它过去 30 次" in x and "一旦开始就倾向一路做下去" in x for x in a["reasons"])
    assert any("经常做一段就反过来" in x for x in b["reasons"])
    pa = {p["k"]: p["s"] for p in insight.score_purposes(a["_f"])}
    pb = {p["k"]: p["s"] for p in insight.score_purposes(b["_f"])}
    assert pa["prog"] > pb["prog"] and pa["short"] < pb["short"] and pa["dip"] > pb["dip"] and pa["mom"] < pb["mom"]
    # 连续第 3 次加仓 + 习惯一路加：按计划建仓
    r = insight.judge(_buy(habit=steady, seq={"same": 2, "opp_days": None}))
    assert r["action"] == "add_prog" and r["tone"] == "bull"
    # 样本太少不用，也不提；缺字段的不用
    r = insight.judge(_buy(habit={"n": 3, "follow": 1.0, "rev": 0.0, "n_buy": 3, "contr": 1.0}))
    assert not any("习惯" in x for x in r["reasons"]) and "habit" not in r["_f"]
    assert "habit" not in insight.judge(_buy(habit={"n": 30}))["_f"]
    r = insight.judge(_buy(habit={"n": 8, "follow": 0.9, "rev": 0.0, "n_buy": 8, "contr": 0.6}))
    assert any("样本还少，仅供参考" in x for x in r["reasons"])


def test_prog_and_price_purposes_are_compatible_not_a_tie():
    # “按计划建仓”和“逢低买入”几乎同分：两件事不冲突，不能退回“普通加仓”
    f = _f(side="buy", base="add", f_down=0.8, lowpos=0.3, seq_same=2, habit={"n": 30, "follow": 0.85, "rev": 0.05, "n_buy": 0, "contr": 0.6})
    ranked = {p["k"]: p["s"] for p in insight.score_purposes(f)}
    assert abs(ranked["prog"] - ranked["dip"]) <= insight.TIE
    ins = {"reasons": [], "style": "养老金", "_f": f}
    insight.finalize(ins)
    assert ins["action"] in ("add_dip", "add_prog") and not any("分不出主次" in x for x in ins["reasons"])
    # 真正互斥的（逢低 vs 顺势）几乎同分时，照旧不硬贴标签
    f2 = _f(side="buy", base="add", f_down=1.0, u3=1.2)
    ins2 = {"reasons": [], "style": "养老金", "_f": f2}
    insight.finalize(ins2)
    assert ins2["action"] == "add" and any("分不出主次" in x for x in ins2["reasons"])


def test_block_of_and_block_evidence():
    daily = [["2026-10-20", 100_000], ["2026-10-21", 3_000_000], ["2026-10-22", -50_000], ["bad"], ["2026-10-23", "x"]]
    assert insight.block_of(daily, 200_000, "buy") == {"d": "2026-10-21", "top": round(3_000_000 / 3_100_000, 3), "x": 15.0}
    assert insight.block_of(daily, 200_000, "sell") == {}                      # 卖的那天只有 5 万股
    assert insight.block_of(daily, 1_000_000, "buy") == {}                     # 只有日均量的 3 倍
    assert insight.block_of([["2026-10-20", 1_000_000], ["2026-10-21", 1_100_000], ["2026-10-22", 1_200_000]], 100_000, "buy") == {}   # 很大但分散
    assert insight.block_of(daily, None, "buy") == {} and insight.block_of(None, 100, "buy") == {} and insight.block_of([], 100, "sell") == {}
    tr = dict(_sell()["trades"], daily=[["2026-10-20", -100_000], ["2026-11-03", -1_900_000]])
    r = insight.judge(_sell(trades=tr, px=_px(adv=100_000)))
    assert "大宗" in r["tags"] and any(x.startswith("交易形状：这次卖的量有 95% 集中在 11/3 一天，是平时日成交量的 19 倍") for x in r["reasons"])
    assert "大宗" not in insight.judge(_sell(trades=tr, px=_px(adv=1_000_000)))["tags"]
    # 一次性大宗卖出不是“涨多了减一点”
    small = dict(voting_power=[8.0, 7.2], votes=[8_000_000, 7_200_000], style="super")
    tr2 = dict(_sell()["trades"], sell_qty=800_000, net_qty=-800_000)
    assert insight.judge(_sell(trades=tr2, px=_px(r6m=0.15, pos=0.5, t_vol=50_000_000, adv=50_000), **small))["action"] == "trim_rebal"
    tr2["daily"] = [["2026-11-03", -800_000]]
    r = insight.judge(_sell(trades=tr2, px=_px(r6m=0.15, pos=0.5, t_vol=50_000_000, adv=50_000), **small))
    assert r["action"] != "trim_rebal" and "大宗" in r["tags"]
    # 买入：一天拿下一大笔，更像接大宗或认购新股
    trb = dict(_buy()["trades"], daily=[["2026-11-03", 2_000_000]])
    a = {p["k"]: p["s"] for p in insight.score_purposes(insight.judge(_buy(trades=trb, px=_px(adv=100_000)))["_f"])}
    b = {p["k"]: p["s"] for p in insight.score_purposes(insight.judge(_buy(trades=trb, px=_px(adv=10_000_000)))["_f"])}
    assert a["plc"] > b["plc"] and a["dip"] < b["dip"]
    # 持仓历史里会记下来（旧记录没有逐日明细，回头看时靠这个）
    e = {"id": "1", "date": "2026-11-05", "type": "change", "change_date": "2026-11-03", "voting_power": [8.0, 6.0], "votes": [8_000_000, 6_000_000],
         "trades": dict(tr), "px": _px(adv=100_000)}
    rec = insight.history_record(e)
    assert rec["blk"]["d"] == "2026-11-03" and "daily" not in rec["tr"] and "daily" in e["trades"]
    assert insight.judge(_sell(blk=rec["blk"]))["tags"] == ["大宗"]
    assert "blk" not in insight.history_record(dict(e, px=_px()))


def test_round11_fields_do_not_leak_and_old_inputs_still_work():
    # 不给新字段时，和以前一样（旧数据、读不到历史时不出错）
    plain = insight.judge(_sell())
    assert plain["action"] == "trim" and not any(x.startswith(("持仓阶段", "这家基金的习惯", "交易形状", "长短期")) for x in plain["reasons"])
    ks = {p["k"] for p in insight.score_purposes(plain["_f"])}
    assert {"wind", "bounce"} <= ks and {"pull", "prog"} <= {p["k"] for p in insight.score_purposes(insight.judge(_buy())["_f"])}
    for a in ("add_pull", "add_prog", "trim_bounce", "exit_bounce", "trim_wind", "exit_wind"):
        assert a in insight.LABELS and a in insight.TONE and (a in insight.BUY_ACTIONS or a in insight.SELL_ACTIONS)
    for side, names in insight.PURPOSE_NAMES.items():
        for k in names:
            for base, action in insight.PURPOSE_ACTION.get((side, k), {}).items():
                assert action in insight.LABELS, (side, k, base)


def test_build_notices_passes_stage_and_habit(sandbox):
    holders, _ = scrape.load_watchlist()
    name = alias = "Paradice Investment Management"
    assert any(h.name == name for h in holders)
    hist = {"pos": {scrape.pos_key(name, "XYZ"): [dict(r) for r in HIST11["pos"]["A|XYZ"]]}}
    e = {"id": "77", "code": "XYZ", "date": "2026-11-05", "time": "10:00", "headline": "Change in substantial holding", "status": "parsed",
         "type": "change", "holder_name": alias, "voting_power": [8.0, 6.0], "votes": [8_000_000, 6_000_000], "change_date": "2026-11-03",
         "trades": _sell()["trades"], "px": _px()}
    out = scrape.build_notices({"77": e}, holders, hist)
    assert len(out) == 1 and out[0]["holder"] == name
    ins = out[0]["insight"]
    assert any(x.startswith("持仓阶段：") and "比最高点少了 40%" in x for x in ins["reasons"]) and "_f" not in ins and ins["side"] == "sell"
    # 没有历史也照常
    assert scrape.build_notices({"77": e}, holders, None)[0]["insight"]["action"]


def test_calibrate_reports_quick_reversals():
    holders, _ = scrape.load_watchlist()
    name = holders[0].name
    px = _px()
    rows = [{"id": "1", "date": "2026-01-10", "time": "", "type": "change", "cd": "2026-01-08", "vp": [6.0, 7.0], "votes": [6_000_000, 7_000_000], "px": px},
            {"id": "2", "date": "2026-02-10", "time": "", "type": "change", "cd": "2026-02-08", "vp": [7.0, 6.0], "votes": [7_000_000, 6_000_000], "px": px},
            {"id": "3", "date": "2026-09-10", "time": "", "type": "change", "cd": "2026-09-08", "vp": [6.0, 7.0], "votes": [6_000_000, 7_000_000], "px": px}]
    cal = scrape.calibrate({"pos": {scrape.pos_key(name, "XYZ"): rows}}, holders)
    buy = next(v for k, v in cal.items() if k.startswith("buy:"))
    sell = next(v for k, v in cal.items() if k.startswith("sell:"))
    assert buy["n"] == 2 and buy["n_next"] == 1 and buy["same_next"] == 0 and buy["rev_next"] == 1.0       # 31 天后就反向
    assert sell["n_next"] == 1 and sell["rev_next"] == 0.0                                                  # 反向了，但隔了 7 个月


# ---------------------------------------------------------------- 第十二轮：相对大盘、基金月报原话

def test_market_returns_and_excess_scoring():
    pts = [((dt.date(2025, 10, 1) + dt.timedelta(days=i)).isoformat(), 100 - i * 0.05) for i in range(400)]
    m = insight.market_returns(pts, "2026-10-28")
    assert m["m3"] < -0.05 and m["m12"] < -0.15 and set(m) == {"m1", "m3", "m6", "m12"}
    assert insight.market_returns(pts, "2030-01-01") == {} and insight.market_returns([], "2026-10-28") == {}
    assert "m12" not in insight.market_returns(pts[300:], "2026-10-28")
    # 股票 3 个月跌 22%：单看是逢低；但大盘同期也跌了 20%，就不算这只股票的逢低
    n = _buy()
    n["px"].update(r3m=-0.22, r6m=-0.22, r12m=-0.22, pos=0.5)
    assert insight.judge(n)["action"] == "add_dip"
    n["px"]["m"] = {"m1": -0.05, "m3": -0.20, "m6": -0.20, "m12": -0.20}
    r = insight.judge(n)
    assert r["action"] == "add" and any("同期大盘（ASX 200）3 个月 -20%" in x for x in r["reasons"])
    n["px"]["m"] = {"m3": "bad", "m6": None, "m12": 99}                   # 坏数据：当没有
    assert insight.judge(n)["action"] == "add_dip"
    n["px"]["m"] = "x"
    assert insight.judge(n)["action"] == "add_dip"


def test_add_market_only_fetches_when_needed(sandbox, monkeypatch):
    day = lambda d: int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() * 1000)
    idx = [[day(dt.date(2025, 6, 1) + dt.timedelta(days=i)), 0, 0, 0, 8000 + i, 1] for i in range(600)]
    monkeypatch.setitem(PDFS, "1001", TRADE_604)
    fake = FakeASX(prices={"SLC": falling_prices(), "XJO": idx, "XSO": [[t, 0, 0, 0, 3000 - i, 1] for i, (t, *_r) in enumerate(idx)]})
    monkeypatch.setattr(scrape, "fetch", fake)
    assert scrape.main() == 0
    e = read(sandbox / "data" / "all_notices.json")["entries"]["1001"]
    bm = scrape.benchmark_of(e["px"])
    assert e["px"]["mb"] == bm and fake.price_calls.count(bm) == 1
    assert (e["px"]["m"]["m3"] > 0) == (bm == "XJO")                       # 小盘股和小盘指数（这里在跌）比
    hist = read(sandbox / "data" / "history.json")
    assert all("m" in r["px"] for rows in hist["pos"].values() for r in rows if r.get("px", {}).get("p"))
    scrape.main()
    assert fake.price_calls.count(bm) == 1                                 # 都补过了：不再取
    # 指数取不到：照常跑，用股票自己的涨跌
    fake2 = FakeASX(prices={"SLC": falling_prices()})
    monkeypatch.setattr(scrape, "fetch", fake2)
    (sandbox / "data" / "all_notices.json").unlink()
    (sandbox / "data" / "history.json").unlink()
    assert scrape.main() == 0 and read(sandbox / "data" / "notices.json")["last_run"]["errors"] == []


REPORT_TEXT = ["Investment Update October 2026 ASX: WAM",
               "The WAM Capital investment portfolio decreased in October. Diversified investor WAM Research (ASX: WAX) was a contributor",
               "to the investment portfolio performance while Maas Group Holdings (ASX: MGH) was a detractor. The WAX share price",
               "increased after the company upgraded its earnings expectations. We remain confident in the medium-term outlook.",
               "Dividends since inception are shown below."]


def test_fund_report_quotes_end_to_end(sandbox, monkeypatch):
    monkeypatch.setitem(PDFS, "3001", REPORT_TEXT)
    monkeypatch.setitem(PDFS, "3002", ["Appendix 3Y"])
    page = year_page([("28/10/2026", "9:38 am", "October 2026 Investment Update", "3001"),
                      ("27/10/2026", "9:00 am", "Change of Director's Interest Notice", "3002"),
                      ("01/02/2026", "9:00 am", "January 2026 Investment Update", "3003")])     # 太旧，不读
    fake = FakeASX(events={"WAM": page})
    monkeypatch.setattr(scrape, "fetch", fake)
    assert scrape.main() == 0
    store = read(sandbox / "data" / "fund_reports.json")
    assert [r["id"] for r in store["reports"]] == ["3001"] and "(ASX: WAX)" in store["reports"][0]["text"]
    n = next(x for x in read(sandbox / "data" / "notices.json")["notices"] if x["code"] == "WAX")
    q = n["insight"]["quotes"][0]
    assert q["fund"] == "WAM" and "was a contributor" in q["text"] and "Dividends since inception" not in q["text"]
    assert "The WAX share price increased" in q["text"]                 # 后面用简称接着讲的那句也带上
    assert "有基金原话" in n["insight"]["tags"] and any(r.startswith("基金自己的说法（WAM《October 2026 Investment Update》") for r in n["insight"]["reasons"])
    assert n["insight"]["reasons"][-1].startswith(n["insight"]["style"] + "：") or n["ocr"]
    other = next(x for x in read(sandbox / "data" / "notices.json")["notices"] if x["code"] != "WAX")
    assert "quotes" not in (other["insight"] or {})
    before = len(fake.pdf_downloads)
    scrape.main()
    assert "3001" not in [str(u) for u in fake.pdf_downloads[before:]] and len(read(sandbox / "data" / "fund_reports.json")["reports"]) == 1


def test_fund_quotes_matching():
    store = {"reports": [
        {"id": "1", "holder": "L1 Capital", "fund": "LSF", "date": "2026-11-09", "title": "Monthly Report - October 2026", "url": "https://www.asx.com.au/x",
         "text": "Key detractors included Mineral Resources and Lloyds. Mineral Resources fell 15.7% over the month as prices weakened. Other text here."},
        {"id": "2", "holder": "L1 Capital", "fund": "LSF", "date": "2026-03-09", "title": "Monthly Report - February 2026", "url": "", "text": "Mineral Resources rose."},
        {"id": "3", "holder": "Regal Funds Management", "fund": "RF1", "date": "2026-11-09", "title": "x", "url": "", "text": "Mineral Resources was sold."}]}
    q = scrape.fund_quotes(store, "L1 Capital", "MIN", "Mineral Resources Limited", "2026-11-05")
    assert len(q) == 1 and q[0]["text"].startswith("Key detractors included Mineral Resources") and "fell 15.7%" in q[0]["text"]
    assert scrape.fund_quotes(store, "L1 Capital", "BHP", "BHP Group Limited", "2026-11-05") == []      # 名字太短太泛：不搜
    assert scrape.fund_quotes(store, "Hostplus", "MIN", "Mineral Resources Limited", "2026-11-05") == []
    assert scrape.fund_quotes({}, "L1 Capital", "MIN", "Mineral Resources Limited", "2026-11-05") == []
    assert scrape.fund_quotes({"reports": ["x", {"holder": "L1 Capital"}]}, "L1 Capital", "MIN", "Mineral Resources", "2026-11-05") == []
    assert scrape.name_key("The Lottery Corporation Limited") == "Lottery" and scrape.name_key("Data#3 Limited") == "" and scrape.name_key("Maas Group Holdings") == ""


def test_fund_quotes_skip_table_junk():
    junk = "Cents per share 16.0 14.0 12.0 10.0 8.0 6.0 4.0 2.0 1999/00 2001/02 2003/04 2005/06 Energy One 2.1% Maas 1.9% 2007/08 2009/10 2011/12."
    store = {"reports": [{"id": "1", "holder": "Wilson Asset Management", "fund": "WAM", "date": "2026-11-11", "title": "October 2026 Investment Update",
                          "url": "", "text": "The portfolio decreased in October. " + junk}]}
    q = scrape.fund_quotes(store, "Wilson Asset Management", "EOL", "Energy One Limited", "2026-11-05")
    assert q == [{"fund": "WAM", "title": "October 2026 Investment Update", "date": "2026-11-11", "url": "", "text": ""}]   # 只在表格里出现：不摘句，只说在持仓表里


def test_fund_quotes_skip_boilerplate_and_own_products():
    rep = lambda text, d="2026-10-13", i="1": {"id": i, "holder": "Regal Funds Management", "fund": "RF1", "date": d, "title": "Unitholder Monthly Report", "url": "", "text": text}
    store = {"reports": [rep("Equity Trustees is a subsidiary of EQT Holdings Limited (ABN 22 607 797 615), a publicly listed company on the Australian Securities Exchange (ASX: EQT). Markets fell.")]}
    assert scrape.fund_quotes(store, "Regal Funds Management", "EQT", "EQT Holdings Limited", "2026-10-20") == []
    store = {"reports": [rep("The Regal Investment Fund NAV decreased in September. Regal Asian Investments also fell during the month overall.")]}
    assert scrape.fund_quotes(store, "Regal Funds Management", "RG1", "Regal Asian Investments Limited", "2026-10-20") == []
    junk = "Top holdings 4.1% 3.9% 3.5% 3.2% 3.0% 2.9% Energy One 2.8% 2.7% 2.6% 2.5% 2.4% 2.3% 2.2% 2.1% 2.0% 1.9% end."
    store = {"reports": [dict(rep(junk, "2026-10-13", "1"), holder="Wilson Asset Management"), dict(rep(junk, "2026-09-13", "2"), holder="Wilson Asset Management")]}
    q = scrape.fund_quotes(store, "Wilson Asset Management", "EOL", "Energy One Limited", "2026-10-20")
    assert len(q) == 1 and q[0]["text"] == "" and q[0]["date"] == "2026-10-13"       # 只在表格里：只提最新一份


# ---------------------------------------------------------------- 第十三轮：说明写在日期行上面的表格

HOSTPLUS_LAYOUT = """Annexure 1
Date of change Person whose Nature of change Consideration given Class and number of Person's
Citicorp Nominees Pty
Limited ACF Host- Plus
Pty Limited ATF the
Hostplus Superannuation Purchase of
18/09/2025 Fund ordinary shares $4,907.76 143 143
Citicorp Nominees Pty
Limited ACF Host- Plus
Pty Limited ATF the
Hostplus Pooled Sale of
19/09/2025 Superannuation Trust shares -$55,212.84 -1,626 -1,626
Citicorp Nominees Pty
Limited ACF Host- Plus
Pty Limited ATF the
Hostplus Pooled Purchase of
19/09/2025 Superannuation Trust ordinary shares $873,397.41 25,682 25,682
Citicorp Nominees Pty
Limited ACF Host- Plus
Pty Limited ATF the
Hostplus Pooled Purchase of
22/09/2025 Superannuation Trust ordinary shares $700,000.00 20,360 20,360
"""

AUSSUPER_LAYOUT = """3/02/2026 JPMorgan Nominees Australia Ltd SELL 24,559.58 ORD -32,862 -32,862

Acquisition of shares
JPMorgan Nominees Australia Ltd pursuant of the Capital
4/02/2026 raising announced 5,978,262.00 ORD 7,971,016 7,971,016
29 Jan 2026
4/02/2026 JPMorgan Nominees Australia Ltd SELL 50,541.66 ORD -67,914 -67,914
4/02/2026 JPMorgan Nominees Australia Ltd BUY 2,792.80 ORD 3,730 3,730
13/03/2026 JPMorgan Nominees Australia Ltd BUY 46,189.97 ORD 84,983 84,983
Acquisition of shares
16/03/2026 JPMorgan Nominees Australia Ltd pursuant of the Capital 6,832,300.50 ORD 9,109,734 9,109,734
raising announced 29
Jan 2026
16/03/2026 JPMorgan Nominees Australia Ltd BUY 7,506.85 ORD 13,800 13,800
"""


def test_trade_description_above_the_date_line():
    s = insight.parse_trades([HOSTPLUS_LAYOUT], "2025-10-01", "change")["summary"]
    assert s["n"] == 4 and s["n_unknown"] == 0
    assert s["buy_qty"] == 143 + 25_682 + 20_360 and s["sell_qty"] == 1_626


def test_split_placement_description_and_date_only_line():
    res = insight.parse_trades([AUSSUPER_LAYOUT], "2026-03-20", "change")
    s = res["summary"]
    assert s["n_unknown"] == 0 and s["kinds"].get("placement") == 7_971_016 + 9_109_734
    assert all(r["qty"] for r in res["rows"])                     # “29 Jan 2026” 那行不算一笔交易
    assert s["buy_qty"] == 7_971_016 + 3_730 + 84_983 + 9_109_734 + 13_800 and s["sell_qty"] == 32_862 + 67_914


def test_history_records_reread_with_new_reader(sandbox, monkeypatch):
    monkeypatch.setitem(PDFS, "1001", TRADE_604)
    monkeypatch.setitem(PDFS, "02001234", OLD_604)
    hist = {"pos": {"Paradice Investment Management|SLC": [
        {"id": "mk-2924-02001234-2A9", "date": "2026-09-01", "time": "16:00", "type": "change", "cd": "2026-08-31", "vp": [5.2, 6.1],
         "votes": [8_500_000, 10_000_000], "buy_qty": 1, "sell_qty": 0, "fwd": {"1m": 0.05}, "bf": 1},
        {"id": "x-no-number", "date": "2026-08-01", "type": "change", "vp": [5.1, 5.2], "votes": []}]},
        "meta": {"Paradice Investment Management|SLC": {"done": "start"}}, "schema": 1}
    (sandbox / "data" / "history.json").write_text(json.dumps(hist), encoding="utf-8")
    fake = FakeASX(prices={"SLC": falling_prices()})
    monkeypatch.setattr(scrape, "fetch", fake)
    assert scrape.main() == 0
    rows = read(sandbox / "data" / "history.json")["pos"]["Paradice Investment Management|SLC"]
    old = next(r for r in rows if r["id"] == "mk-2924-02001234-2A9")
    assert old["buy_qty"] == 1_500_000 and old["dv"] == scrape.INSIGHT_VERSION and "1m" in old["fwd"] and old["bf"] == 1
    assert next(r for r in rows if r["id"] == "x-no-number")["dv_tries"] == 9
    assert next(r for r in rows if r["id"] == "1001")["dv"] == scrape.INSIGHT_VERSION
    assert read(sandbox / "data" / "notices.json")["last_run"]["insight"]["history_refreshed"] == 1
    before = len(fake.pdf_downloads)
    scrape.main()
    assert len(fake.pdf_downloads) == before                                   # 都是新读法了：不再重读


def test_detail_retry_counter_resets_on_new_reader_version(sandbox, monkeypatch):
    monkeypatch.setitem(PDFS, "1001", TRADE_604)
    fake = FakeASX(prices={"SLC": falling_prices()})
    monkeypatch.setattr(scrape, "fetch", fake)
    scrape.main()
    store = read(sandbox / "data" / "all_notices.json")
    e = store["entries"]["1001"]
    e["detail_v"], e["detail_tries"] = scrape.INSIGHT_VERSION - 1, 3           # 旧版读法下已经试满了
    e.pop("detail_try_v", None)
    (sandbox / "data" / "all_notices.json").write_text(json.dumps(store), encoding="utf-8")
    scrape.main()
    e = read(sandbox / "data" / "all_notices.json")["entries"]["1001"]
    assert e["detail_v"] == scrape.INSIGHT_VERSION and e["detail_try_v"] == scrape.INSIGHT_VERSION


# ---------------------------------------------------------------- 第十四轮：逐条审查后修的判断

@pytest.mark.parametrize("headline,code,want", [
    ("RWC signs Scheme Implementation Deed with Brookfield", "RWC", True),
    ("Dispatch of Scheme Booklet", "TTT", True),
    ("Perpetual rejects non-binding indicative proposal", "PPT", True),
    ("Takeover proposal from TPG Global", "EQT", True),
    ("ENERGY ONE RESPONSE TO NON-BINDING PROPOSAL", "EOL", True),
    ("BET: BetMakers enters Scheme Implementation Deed with TAH", "TAH", False),
    ("Proposed acquisition of XYZ Limited", "ABC", False),
    ("Merger update", "ABC", None),
])
def test_target_by_headline(headline, code, want):
    assert insight.target_by_headline(headline, code) is want


def test_takeover_target_by_headline_without_price_jump():
    sid = [{"d": "2026-09-16", "k": "takeover", "h": "XYZ signs Scheme Implementation Deed with Brookfield", "s": True, "j": 0.01}]
    r = insight.judge(dict(_buy(ev=sid, style="hedge"), code="XYZ"))
    assert r["action"] == "arb" and any("从标题看这家公司是被收购的一方" in x for x in r["reasons"])
    bidder = [{"d": "2026-09-16", "k": "takeover", "h": "BET: BetMakers enters Scheme Implementation Deed with XYZ", "s": True, "j": 0.01}]
    r = insight.judge(dict(_buy(ev=bidder, style="hedge"), code="XYZ"))
    assert r["action"] == "add" and any("出手收购的一方" in x for x in r["reasons"])


def test_deal_exit_when_selling_at_a_loss_after_takeover():
    tk = [{"d": "2026-10-19", "k": "takeover", "h": "XYZ enters binding Scheme Implementation Deed", "s": True, "j": 0.85}]
    n = _sell(ev=tk, code="XYZ", cost={"avg": 2.4, "cov": 1.0, "since": "2024-04-10", "n": 3})
    n["px"].update(r1m=0.86, r3m=0.49, r6m=0.66, r12m=-0.16, pos=0.7)
    r = insight.judge(n)
    assert r["action"] == "trim_deal" and r["purposes"][0]["k"] == "deal" and "分不出主次" not in " ".join(r["reasons"])
    assert any("亏钱卖" in x for x in r["reasons"])


def test_own_product_is_not_a_stock_pick():
    assert scrape.is_own_product("Regal Funds Management", "RG8", "Regal Asian Investments Limited", "hedge")
    assert scrape.is_own_product("L1 Capital", "GLS", "L1 Global Long Short Fund Limited", "hedge")
    assert scrape.is_own_product("L1 Capital", "ZZZ", "L1 Long Short Fund", "hedge")                     # 按名字也认得出
    assert not scrape.is_own_product("Australian Retirement Trust", "ACL", "Australian Clinical Labs", "super")
    assert not scrape.is_own_product("Perennial", "PPT", "Perpetual Limited", "active")
    r = insight.judge(dict(_sell(), own=True))
    assert r["action"] == "own_product" and r["tone"] == "neutral" and not r.get("purposes")
    assert any("自己旗下" in x for x in r["reasons"]) and r["reasons"][0].startswith("持股比例")


def test_long_603_window_wording_and_recent_share():
    n = {"type": "becoming", "voting_power": [6.46], "votes": [17_630_000], "style": "active", "date": "2026-11-10",
         "change_date": "2026-11-07",
         "trades": {"n": 109, "n_buy": 100, "n_sell": 9, "buy_qty": 18_870_000, "sell_qty": 1_240_000, "net_qty": 17_630_000,
                    "first": "2024-11-07", "last": "2026-11-07", "days": 78, "buy_price": 0.54, "sell_price": 0.31, "kinds": {"other": 20_110_000},
                    "daily": [["2025-03-01", 9_000_000], ["2026-01-05", 4_000_000], ["2026-08-01", 3_000_000], ["2026-10-20", 1_630_000]]},
         "px": {"p": 0.29, "r1m": -0.09, "r3m": -0.08, "r6m": 0.16, "r12m": -0.54, "pos": 0.19, "hi": 0.64, "lo": 0.21, "t_lo": 0.2, "t_hi": 0.7}}
    r = insight.judge(n)
    text = " ".join(r["reasons"])
    assert "这份 603 列出的明细（比规定的 4 个月更长）" in text and "成为大股东前 4 个月" not in text
    assert "从零建起来的" in text and "属于快速建仓" not in text and "早就有底仓" in text      # 最近 4 个月只买了 463 万股（26%）


def test_previous_notice_falls_back_to_history():
    hist = {"pos": {"A|EOS": [{"id": "1", "date": "2026-10-16", "time": "16:00", "type": "becoming", "vp": [5.047], "votes": [11_189_140]}]}}
    e = {"id": "2", "date": "2026-11-06", "time": "17:00", "type": "ceasing"}
    assert scrape.prev_from_history(hist, "A", "EOS", e) == {"vp": 5.047, "votes": 11_189_140, "type": "becoming", "date": "2026-10-16"}
    assert scrape.prev_from_history(hist, "A", "EOS", {"id": "1", "date": "2026-10-16", "time": "16:00"}) is None
    assert scrape.prev_from_history({"pos": {"A|EOS": ["bad"]}}, "A", "EOS", e) is None and scrape.prev_from_history(None, "A", "EOS", e) is None


# ---------------------------------------------------------------- 第十五轮：倒手（找到卖出的另一头 / 接盘方）

def test_clean_holder_name():
    c = scrape.clean_holder_name
    assert c("in Annexure A (collectively, the Landchecker Shareholder Entities) and each entity listed in Annexure B (collectively,") \
        == "Landchecker Shareholder Entities"
    assert c("Australian Retirement Trust Pty LTD ATF Australian Retirement Trust") == "Australian Retirement Trust Pty LTD"
    assert c("JPMorgan Chase & Co. and its affiliates") == "JPMorgan Chase & Co."
    assert c("State Street Corporation and subsidiaries named in paragraph 7") == "State Street Corporation"
    assert c("Pinnacle Investment Management Group Limited (ACN 100 325 184)") == "Pinnacle Investment Management Group Limited"
    for bad in ("", "   ", "in Annexure A", "each entity listed in Annexure B", "Refer to Annexure A", "12 34"):
        assert c(bad) == ""
    long = c("Very Long Name " * 10)
    assert long.endswith("…") and len(long) <= 72


def test_holding_move():
    m = scrape.holding_move
    assert m({"type": "becoming", "voting_power": [5.4]}) == {"side": "buy", "pre": None, "post": 5.4, "d": None, "exact": False}
    assert m({"type": "ceasing", "voting_power": []}, 8.0) == {"side": "sell", "pre": 8.0, "post": None, "d": None, "exact": False}
    assert m({"type": "ceasing"}, 3.0)["pre"] is None and m({"type": "ceasing"}, "x")["pre"] is None
    assert m({"type": "change", "voting_power": [18.58, 8.58]}) == {"side": "sell", "pre": 18.58, "post": 8.58, "d": 10.0, "exact": True}
    # 股数增加但比例下降（增发摊薄）：看不出真实买卖，不配对
    assert m({"type": "change", "voting_power": [9.4, 8.15], "votes": [3_000_000, 3_211_000]}) == {}
    for bad in ({"type": "change", "voting_power": [5.0, 5.0]}, {"type": "change", "voting_power": [6.0]}, {"type": "becoming"},
                {"type": "change", "voting_power": ["x", 6]}, {"type": "change", "voting_power": [0, 6]}, {"type": "other", "voting_power": [6]}):
        assert m(bad) == {}


def _cp_entries(**over):
    """AMX 的真实情况：Perennial 11/9 卖掉 950 万股（18.58% → 8.58%，一天之内），同一天 Landchecker 一方成为大股东，持股 18.58%。"""
    seller = {"id": "S1", "code": "AMX", "date": "2026-11-10", "time": "15:41", "headline": "Change in substantial holding", "status": "parsed",
              "type": "change", "holder_name": "Perennial Value Management Limited (PVM)", "voting_power": [18.58, 8.58],
              "votes": [17_644_814, 8_150_859], "change_date": "2026-11-09", "url": "https://www.asx.com.au/s1",
              "trades": {"n": 4, "buy_qty": 5109, "sell_qty": 9_499_064, "net_qty": -9_493_955,
                         "daily": [["2026-11-03", 3819], ["2026-11-04", 1250], ["2026-11-09", -9_499_064]]},
              "px": {"p": 0.37, "adv": 217_000, "t_vol": 10_200_000}}
    buyer = {"id": "B1", "code": "AMX", "date": "2026-11-11", "time": "08:16", "headline": "Becoming a substantial holder", "status": "parsed",
             "type": "becoming", "holder_name": "in Annexure A (collectively, the Landchecker Shareholder Entities) and each entity",
             "voting_power": [18.58], "change_date": "2026-11-09", "url": "https://www.asx.com.au/b1"}
    seller.update(over.get("seller", {}))
    buyer.update(over.get("buyer", {}))
    return seller, buyer


def _find(seller, others, holder="Perennial", prev_vp=None):
    holders, _ = scrape.load_watchlist()
    return scrape.find_counterparty(seller, seller["code"], holder, prev_vp, {seller["code"]: [seller] + others}, holders)


def test_find_counterparty_whole_transfer():
    s, b = _cp_entries()
    cp = _find(s, [b])
    assert cp["kind"] == "whole" and cp["name"] == "Landchecker Shareholder Entities" and cp["in_list"] is False
    assert cp["gap"] == 0 and cp["type"] == "becoming" and cp["vp"] == [18.58] and cp["url"] == "https://www.asx.com.au/b1"
    assert cp["me"]["side"] == "sell" and cp["me"]["post"] == 8.58 and cp["more"] == 0
    # 买方的公告晚两天、变动日差 3 天：还算；差 4 天：不算
    assert _find(s, [dict(b, change_date="2026-11-12")])["gap"] == 3
    assert _find(s, [dict(b, change_date="2026-11-13")]) == {}
    # 没有变动日（任何一边）、没读成功、同一只股票之外：都不配
    assert _find(dict(s, change_date=""), [b]) == {} and _find(s, [dict(b, change_date=None)]) == {}
    assert _find(s, [dict(b, status="unreadable")]) == {}
    # 卖方从买方那里看：买方是 603，卖方 605（之前 18.58%，靠上一份公告知道）
    cease = dict(s, type="ceasing", voting_power=[], votes=[])
    assert _find(cease, [b], prev_vp=18.58)["kind"] == "whole"
    assert _find(cease, [b], prev_vp=None) == {}                    # 不知道它之前有多少：宁缺毋错


def test_find_counterparty_rejects_coincidences():
    s, b = _cp_entries()
    # 卖方原来只在 5% 附近：任何新越过 5% 的人都“对得上”，不算（KAR、ZIP 的真实情况）
    small = dict(s, voting_power=[5.04, 4.2], votes=[], type="change")
    assert _find(small, [dict(b, voting_power=[5.04])]) == {}
    assert _find(dict(s, type="ceasing", voting_power=[]), [dict(b, voting_power=[5.0])], prev_vp=5.0) == {}
    # 同方向（对方也在卖）不是对手方
    assert _find(s, [dict(b, type="change", voting_power=[20.0, 18.58])]) == {}
    # 同一家机构换个名字申报（名单里的 Perennial 两个实体；名单外的 JPMorgan 两份）：不是对手方
    assert _find(s, [dict(b, holder_name="Perennial Value Management Limited")]) == {}
    jp = dict(s, holder_name="JPMorgan Chase & Co. and its affiliates")
    assert _find(jp, [dict(b, holder_name="JPMorgan Chase & Co. and its affiliates")], holder="?") == {}


def test_find_counterparty_block_needs_one_day_concentration():
    s, _ = _cp_entries()
    s = dict(s, voting_power=[13.13, 14.23], votes=[], type="change")         # 买方：AustralianSuper 加了 1.1 个百分点
    jpm = {"id": "J", "code": "AMX", "status": "parsed", "type": "change", "holder_name": "JPMorgan Chase & Co. and its affiliates",
           "voting_power": [8.76, 7.59], "change_date": "2026-11-08", "date": "2026-11-11", "url": "https://www.asx.com.au/j"}
    spread = dict(s, trades={"buy_qty": 3_000_000, "sell_qty": 0, "daily": [[f"2026-10-{d:02d}", 300_000] for d in range(20, 30)]},
                  px={"adv": 2_000_000, "t_vol": 40_000_000})
    assert _find(spread, [jpm], holder="AustralianSuper") == {}      # 分 10 天在市场上买：百分点碰巧接近不说明什么（PLS 的真实情况）
    block = dict(s, trades={"buy_qty": 3_000_000, "sell_qty": 0, "daily": [["2026-11-08", 3_000_000]]}, px={"adv": 200_000})
    cp = _find(block, [jpm], holder="AustralianSuper")
    assert cp["kind"] == "block" and cp["me"]["side"] == "buy" and cp["name"] == "JPMorgan Chase & Co."
    # 两家都“差不多对得上”：说不准是哪家，不说
    ss = dict(jpm, id="SS", holder_name="State Street Corporation and subsidiaries", voting_power=[13.8, 12.66])
    assert _find(block, [jpm, ss], holder="AustralianSuper") == {}
    # 差得太多不算
    assert _find(block, [dict(jpm, voting_power=[8.76, 8.2])], holder="AustralianSuper") == {}
    # 两边持股数都有、差不到 3%：不需要“集中在一天”也算
    s2 = dict(s, votes=[13_000_000, 14_100_000], trades={}, px={})
    j2 = dict(jpm, votes=[9_000_000, 7_910_000])
    assert _find(s2, [j2], holder="AustralianSuper")["kind"] == "shares"
    # 买方是 603：它的全部持股 ≈ 卖方这次卖掉的
    s3, b3 = _cp_entries()
    s3 = dict(s3, voting_power=[18.58, 12.5])
    assert _find(s3, [dict(b3, voting_power=[6.0])])["kind"] == "block"
    assert _find(dict(s3, trades={}, px={}), [dict(b3, voting_power=[6.0])]) == {}


def test_add_counterparty_relabels_sell_and_buy():
    s, b = _cp_entries()
    cp = _find(s, [b])
    tr = dict(_sell()["trades"], sell_qty=9_499_064, net_qty=-9_493_955, buy_qty=5109)
    n = _sell(voting_power=[18.58, 8.58], votes=[17_644_814, 8_150_859], trades=tr,
              px={"p": 0.37, "r1m": 0.12, "r3m": 0.25, "r6m": 0.37, "r12m": 0.32, "pos": 0.95, "hi": 0.37, "lo": 0.2, "t_lo": 0.29, "t_hi": 0.37})
    ins = insight.judge(n)
    assert ins["action"] == "trim_profit"
    insight.add_counterparty(ins, cp, "Perennial")
    assert ins["action"] == "trim_hand" and ins["tone"] == "neutral" and ins["tags"][0] == "倒手"
    assert ins["purposes"][0]["k"] == "hand" and ins["purposes"][1]["k"] == "profit"     # 获利了结仍在，排第二（两者不冲突）
    assert ins["cp"] == {"name": "Landchecker Shareholder Entities", "date": "2026-11-11", "type": "becoming",
                         "url": "https://www.asx.com.au/b1", "kind": "whole", "side": "buy"}
    line = next(r for r in ins["reasons"] if r.startswith("接盘方："))
    assert "11/9（和这次变动同一天）「Landchecker Shareholder Entities」公告成为大股东，持股 18.58%" in line
    assert "正好等于Perennial卖出前的全部持股（18.58%）" in line and "剩下的 8.58%" in line
    assert not any("分不出主次" in r for r in ins["reasons"])
    insight.add_counterparty(ins, cp, "Perennial")                       # 重复调用不重复加小标签
    assert ins["tags"].count("倒手") == 1
    # 买方一边
    buy = insight.judge(_buy())
    cpb = dict(cp, me={"side": "buy", "pre": 6.0, "post": 8.0, "d": 2.0, "exact": True},
               other={"side": "sell", "pre": 8.0, "post": 6.0, "d": 2.0, "exact": True}, kind="block", type="change", vp=[8.0, 6.0],
               name="Some Fund", gap=1)
    insight.add_counterparty(buy, cpb, "L1 Capital")
    assert buy["action"] == "add_take" and buy["tone"] == "bull" and buy["cp"]["side"] == "sell"
    assert any(r.startswith("卖给它的一方：") and "对方变动的约 2% 和它这次买入的约 2% 对得上" in r for r in buy["reasons"])


def test_add_counterparty_edge_cases():
    # 提前定性的（比如“刚好跌破 5% 线”）没有打分数据：只加理由和小标签，标签不变
    ins = {"action": "slip", "label": insight.LABELS["slip"], "reasons": ["a", "主动选股基金：x"], "style": "主动选股基金", "tags": []}
    cp = {"kind": "whole", "name": "", "gap": 2, "type": "becoming", "vp": [9.0], "cd": "2026-11-02", "date": "2026-11-04", "url": "",
          "me": {"side": "sell", "pre": 9.0, "post": None}, "other": {"side": "buy"}, "more": 1}
    insight.add_counterparty(ins, cp, "X")
    assert ins["action"] == "slip" and ins["tags"] == ["倒手"] and ins["reasons"][-1].startswith("主动选股基金")
    assert "另一家机构（名字没读准）" in ins["reasons"][1] and "同期还有 1 家" in ins["reasons"][1] and "交给了对方" in ins["reasons"][1]
    # 坏的输入：什么都不做
    for bad in (None, {}, {"kind": "maybe"}, "x"):
        before = json.dumps(ins, ensure_ascii=False)
        insight.add_counterparty(ins, bad, "X")
        assert json.dumps(ins, ensure_ascii=False) == before
    # 新目的、新标签都在表里
    for a in ("trim_hand", "exit_hand", "add_take", "build_take"):
        assert a in insight.LABELS and a in insight.TONE and (a in insight.BUY_ACTIONS or a in insight.SELL_ACTIONS)
    assert {"hand"} <= {p["k"] for p in insight.score_purposes(insight.judge(_sell())["_f"])}
    assert {"take"} <= {p["k"] for p in insight.score_purposes(insight.judge(_buy())["_f"])}
    # 没有对手方时，新目的的概率很低，不会抢标签
    assert insight.judge(_sell())["action"] == "trim" and insight.judge(_buy())["action"] != "add_take"


def test_build_notices_finds_counterparty_outside_watchlist():
    holders, _ = scrape.load_watchlist()
    s, b = _cp_entries()
    s["trades"].update(n_buy=3, n_sell=1, first="2026-11-03", last="2026-11-09", days=4, sell_price=0.339, buy_price=0.296,
                       kinds={"on_market": 9_504_173})
    out = scrape.build_notices({"S1": s, "B1": b}, holders, None)
    assert len(out) == 1 and out[0]["holder"] == "Perennial"         # 接盘方不在名单里，列表里不显示它
    ins = out[0]["insight"]
    assert ins["tags"][0] == "倒手" and ins["cp"]["name"] == "Landchecker Shareholder Entities" and "_f" not in ins
    assert ins["action"] in ("trim_hand", "exit_hand") or ins["purposes"][0]["k"] == "hand"
    # 接盘方的公告还没出来时，照旧
    alone = scrape.build_notices({"S1": s}, holders, None)[0]["insight"]
    assert "cp" not in alone and "倒手" not in (alone.get("tags") or [])


def test_weekend_listing_page_is_recognised():
    html = (FIXTURES / "todayAnns_weekend.html").read_text(encoding="utf-8")
    rows, recognised = scrape.parse_listing(html, dt.date(2026, 11, 14))
    assert rows == [] and recognised                    # 周末没有公告不是“ASX 改版了”
    assert scrape.parse_listing("<html><body>Something else entirely</body></html>", dt.date(2026, 11, 14)) == ([], False)


# ---------------------------------------------------------------- 第十六轮：标准答案、可检验的预言、超额收益

def test_benchmark_small_caps_use_small_ords():
    assert scrape.benchmark_of({"adv": 100_000, "p": 1.5}) == "XSO"          # 每天成交 15 万澳元：小盘
    assert scrape.benchmark_of({"adv": 3_000_000, "p": 20}) == "XJO"
    for bad in ({}, {"adv": None, "p": 1}, {"adv": "x", "p": 1}, {"adv": 0, "p": 1}, None, "x"):
        assert scrape.benchmark_of(bad or {}) == "XJO" if not isinstance(bad, str) else scrape.benchmark_of(bad) == "XJO"


def test_forward_excess_against_index(monkeypatch):
    day = lambda d: int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() * 1000)
    idx = [[day(dt.date(2025, 1, 1) + dt.timedelta(days=i)), 0, 0, 0, 1000 * (1 + 0.001 * i), 1] for i in range(500)]
    fetched = []

    def fake_series(code):
        fetched.append(code)
        if code == "XSO":
            raise scrape.FetchError("down")
        return idx
    monkeypatch.setattr(scrape, "price_series", fake_series)
    monkeypatch.setattr(scrape, "_INDEX", {})
    big = {"id": "1", "date": "2025-03-01", "px": {"adv": 5_000_000, "p": 10}, "fwd": {"1m": 0.10, "3m": 0.20}}
    small = {"id": "2", "date": "2025-03-01", "px": {"adv": 1000, "p": 1}, "fwd": {"3m": 0.05}}
    junk = {"id": "3", "date": "", "px": {}, "fwd": {"3m": "x"}}
    h = {"pos": {"A|XYZ": [big, small, junk]}}
    assert scrape.add_forward_excess(h) == 1
    ifwd = insight.forward_returns(scrape.index_series("XJO"), "2025-03-01")
    assert big["bm"] == "XJO" and big["fwd_x"]["3m"] == round(0.20 - ifwd["3m"], 3) and big["fwd_x"]["3m"] < 0.20
    assert "fwd_x" not in small and "fwd_x" not in junk                     # 小盘指数取不到：不乱用别的指数
    assert fetched.count("XJO") == 1                                        # 同一个指数一次运行只取一次
    assert scrape.add_forward_excess(h) == 0 or "fwd_x" in big               # 已经补过的不重复补


def test_outcome_checks_only_look_forward():
    today = dt.date(2026, 11, 14)
    r = {"date": "2026-01-10", "fwd_x": {"3m": -0.04}}
    later = [{"date": "2026-02-20", "type": "change", "vp": [7, 6], "votes": [7_000_000, 6_000_000]}]
    c = scrape.outcome_checks(r, "buy", later, today)
    assert c == {"next_same": False, "rev90": True, "beat3": False}
    c = scrape.outcome_checks(r, "sell", [{"date": "2026-03-01", "type": "change", "vp": [6, 5.5], "votes": [6_000_000, 5_500_000]}], today)
    assert c["next_same"] and c["no_big_cut"] and c["rev90"] is False and c["nobuy180"] is True
    c = scrape.outcome_checks(r, "sell", [{"date": "2026-03-01", "type": "ceasing", "vp": [5, 0], "votes": []}], today)
    assert c["no_big_cut"] is False
    # 刚发生不久、之后什么都没有：90 天、半年都还看不出来，不下结论
    assert scrape.outcome_checks({"date": "2026-11-01"}, "sell", [], today) == {}
    assert scrape.outcome_checks({"date": "bad"}, "buy", later, today) == {}


def test_calibrate_reports_excess_and_checks():
    holders, _ = scrape.load_watchlist()
    name = holders[0].name
    px = _px()
    rows = [{"id": str(i), "date": f"2025-{m:02d}-10", "time": "", "type": "change", "cd": f"2025-{m:02d}-08", "vp": [6.0 + i, 7.0 + i],
             "votes": [(6 + i) * 1_000_000, (7 + i) * 1_000_000], "px": px, "fwd": {"3m": 0.05}, "fwd_x": {"3m": 0.02 if i % 2 else -0.01}}
            for i, m in enumerate(range(1, 11))]
    cal = scrape.calibrate({"pos": {scrape.pos_key(name, "XYZ"): rows}}, holders, dt.date(2026, 11, 14))
    buys = [v for k, v in cal.items() if k.startswith("buy:")]
    n = sum(v["n3x"] for v in buys)
    assert n == 10 and abs(sum(v["avg3x"] * v["n3x"] for v in buys) / n - 0.005) < 0.001
    assert round(sum(v["up3x"] * v["n3x"] for v in buys)) == 5
    for k, v in cal.items():
        if "check" in v:
            assert scrape.CHECK_OF[k] and v["check_n"] >= 1 and 0 <= v["check"] <= 1 and v["check_base"] is not None and v["check_name"]
    assert any("check" in v for v in buys)
    # 坏数据不出错
    rows[0]["fwd_x"] = "x"
    rows[1]["date"] = None
    scrape.calibrate({"pos": {scrape.pos_key(name, "XYZ"): rows}}, holders, dt.date(2026, 11, 14))


def test_gold_file_is_well_formed():
    g = json.loads((Path(__file__).parent / "fixtures" / "gold_purposes.json").read_text("utf-8"))
    keys = set(insight.PURPOSE_NAMES) | {k for _, k in insight.PURPOSE_ACTION} | {"base", "passive", "lending", "transfer", "own", "restructure", "line", "rotate"}
    ids = set()
    for it in g["items"]:
        assert it["src"] in ("history", "live") and 1 <= it["sure"] <= 3 and 1 <= len(it["why"]) <= 2
        assert set(it["why"]) <= keys, it
        assert it["id"] not in ids
        ids.add(it["id"])
    assert len(g["items"]) >= 100


# ---------------------------------------------------------------- 第十七轮：自身波动、小盘对照、变动日读错

def test_price_ref_falls_back_when_change_date_is_wrong():
    tr = {"first": "2026-04-02", "last": "2026-10-26"}
    assert scrape.price_ref("2026-01-14", tr, "2026-10-27") == "2026-10-26"      # MYG：读成了上一份公告的日期
    assert scrape.price_ref("2026-10-29", {"last": "2026-11-02"}, "2026-11-02") == "2026-10-29"   # 列交收日，早 4 天：照旧
    assert scrape.price_ref("", tr, "2026-10-27") == "2026-10-26"
    assert scrape.price_ref("", {}, "2026-10-27") == "2026-10-27"
    assert scrape.price_ref("bad", tr, "x") == "bad" and scrape.price_ref(None, None, None) == ""


def test_price_context_has_own_volatility():
    day = lambda i: int(dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000) + i * 86_400_000
    calm = [[day(i), 10 * (1.01 if i % 2 else 1.0), 1000] for i in range(400)]
    wild = [[day(i), 10 * (1.10 if i % 2 else 1.0), 1000] for i in range(400)]
    a, b = insight.price_context(calm, "2026-01-20"), insight.price_context(wild, "2026-01-20")
    assert 0.008 < a["vol"] < 0.012 and 0.08 < b["vol"] < 0.11
    assert "vol" not in insight.price_context(calm[:40], "2025-02-05")              # 不到 60 天：不算


def test_refresh_px_recomputes_history_without_pdfs(monkeypatch):
    holders, _ = scrape.load_watchlist()
    h = {"pos": {"A|SLC": [{"id": "1", "date": "2026-10-20", "cd": "2026-01-14", "tr": {"first": "2026-10-01", "last": "2026-10-16"},
                            "px": {"p": 1.0, "m": {"m3": 0.1}}, "px_v": 5},
                           {"id": "2", "date": "2026-10-20", "px": {"p": 1.0}, "px_v": scrape.PX_VERSION},
                           {"id": "3", "date": "2026-10-20"}]}}
    calls = []
    monkeypatch.setattr(scrape, "price_series", lambda c: calls.append(c) or falling_prices())
    monkeypatch.setattr(scrape, "PAUSE", 0)
    bf = scrape.Backfill(h, holders, dt.date(2026, 11, 14))
    assert bf.refresh_px() == 1 and calls == ["SLC"]
    r = h["pos"]["A|SLC"][0]
    assert r["px_v"] == scrape.PX_VERSION and r["px"]["ref"] <= "2026-10-16" and r["px"]["ref"] >= "2026-10-06" and "m" not in r["px"]
    assert h["pos"]["A|SLC"][1]["px"] == {"p": 1.0} and "px" not in h["pos"]["A|SLC"][2]
    monkeypatch.setattr(scrape, "price_series", lambda c: (_ for _ in ()).throw(scrape.FetchError("down")))
    h["pos"]["A|SLC"][0]["px_v"] = 1
    bf = scrape.Backfill(h, holders, dt.date(2026, 11, 14))
    assert bf.refresh_px() == 0 and h["pos"]["A|SLC"][0]["px_tries"] == 1


def test_small_cap_reason_names_small_ords():
    n = _buy()
    n["px"].update(r3m=-0.22, r6m=-0.22, r12m=-0.22, pos=0.5, m={"m1": -0.05, "m3": -0.20, "m6": -0.20, "m12": -0.20}, mb="XSO")
    assert any("同期小盘股指数（Small Ordinaries）3 个月 -20%" in x for x in insight.judge(n)["reasons"])


def test_trend_threshold_scales_with_own_volatility():
    assert insight._trend(-0.24, None, None, -1) == 1.2                          # 固定门槛：3 个月跌 24% 算明显
    assert insight._trend(-0.24, None, None, -1, 0.05) < 1                       # 一天波动 5% 的股票：3 个月正常起伏约 40%
    assert insight._trend(-0.24, None, None, -1, 0.01) == 1.2                    # 蓝筹不会因此更宽松
    for bad in (None, "x", True, 0, -1, 5, float("nan")):
        assert insight._trend(-0.24, None, None, -1, bad) == 1.2
    n = _buy()
    n["px"].update(r3m=-0.24, r6m=-0.10, r12m=-0.10, pos=0.5, vol=0.05)
    r = insight.judge(n)
    assert r["action"] != "add_dip" and any("一天约 ±5.0%" in x and "正常起伏" in x for x in r["reasons"])
    n["px"]["vol"] = 0.01
    assert not any("正常起伏" in x for x in insight.judge(n)["reasons"])


def test_ceasing_estimates_what_is_left():
    prev = {"vp": 8.0, "votes": 8_000_000, "type": "change", "date": "2026-08-01"}
    r = insight.judge(_sell(type="ceasing", voting_power=[], votes=[], prev=prev,
                            trades={**_sell()["trades"], "sell_qty": 8_000_000, "net_qty": -8_000_000}))
    assert "清仓" in r["tags"] and any("基本卖光了" in x for x in r["reasons"])
    assert not any("是否清仓看不出来" in x for x in r["reasons"])
    r = insight.judge(_sell(type="ceasing", voting_power=[], votes=[], prev=prev,
                            trades={**_sell()["trades"], "sell_qty": 4_000_000, "net_qty": -4_000_000}))
    assert "清仓" not in r["tags"] and any("估计还剩约 400 万股（约 4.0%" in x for x in r["reasons"])
    r = insight.judge(_sell(type="ceasing", voting_power=[], votes=[], prev=prev))              # 估出来 6%：和“降到 5% 以下”矛盾，不说
    assert not any("估计还剩" in x for x in r["reasons"])
    assert not any("估计还剩" in x for x in insight.judge(_sell(type="ceasing", voting_power=[], votes=[]))["reasons"])  # 不知道上一份：不估
    r = insight.judge(_sell(type="ceasing", voting_power=[], votes=[], prev={**prev, "vp": None}))
    assert not any("估计还剩" in x for x in r["reasons"])


def test_mismatched_trades_are_not_used_as_evidence():
    tl = {"n": 8, "down_share": 0.9, "base_down": 0.5, "r_win": -0.15}
    good = _buy(tl=tl)
    bad = _buy(tl=tl, trades={**_buy()["trades"], "buy_qty": 900_000, "net_qty": 900_000})   # 持股数 +200 万，明细只读到 90 万
    rg, rb = insight.judge(good), insight.judge(bad)
    assert not any("对不上" in x for x in rg["reasons"]) and any("逐笔看" in x for x in rg["reasons"])
    assert rb["reasons"][0].startswith("注意：这份公告的逐笔明细加总和持股数的变化对不上")
    assert not any("逐笔看" in x or "均价约" in x for x in rb["reasons"])
    assert rb["confidence"] in ("中", "低")


def test_reconcile_stats():
    entries = {"1": {"votes": [100, 200], "trades": {"n": 2, "net_qty": 100}}, "2": {"votes": [100, 200], "trades": {"n": 2, "net_qty": 30}},
               "3": {"votes": [], "trades": {"n": 2, "net_qty": 30}}, "4": {"votes": [100, 200], "trades": {"n": 1, "net_qty": "x"}}}
    notices = [{"id": k, "type": "change"} for k in entries] + [{"id": "9", "type": "ceasing"}]
    assert scrape.reconcile_stats(notices, entries) == {"ok": 1, "bad": 1}
    assert scrape.reconcile_stats([], {}) == {"ok": 0, "bad": 0}


def test_empty_price_series_keeps_previous_context(monkeypatch):
    monkeypatch.setattr(scrape, "price_series", lambda c: [])
    monkeypatch.setattr(scrape, "company_events", lambda c: [])
    monkeypatch.setattr(scrape, "company_sector", lambda c: "")
    monkeypatch.setattr(scrape, "PAUSE", 0)
    old = {"p": 2.96, "r3m": 0.23}
    e = {"id": "1", "code": "ACL", "date": "2026-10-28", "change_date": "2026-10-27", "px": dict(old), "px_v": 5, "trades": {}}
    fresh = {"id": "2", "code": "ACL", "date": "2026-10-28", "change_date": "2026-10-27", "trades": {}}
    scrape.add_prices([e, fresh])
    assert e["px"] == old and e["px_v"] == 5 and e["px_tries"] == 1
    assert fresh["px"] == {} and fresh["px_v"] == scrape.PX_VERSION


# ---------------------------------------------------------------- 第十八轮：配售指纹、拆“像调仓”、指数调整

def test_round_block_fingerprint():
    assert insight.round_block({"n_buy": 1, "buy_qty": 5_882_353, "buy_price": 1.36})            # $8,000,000.08
    assert insight.round_block({"n_buy": 1, "buy_qty": 15_795_000, "buy_price": 1.0})
    assert not insight.round_block({"n_buy": 3, "buy_qty": 5_882_353, "buy_price": 1.36})        # 多笔的均价不认
    assert not insight.round_block({"n_buy": 1, "buy_qty": 5_882_353, "buy_price": 1.3611})
    assert not insight.round_block({"n_buy": 1, "buy_qty": 100_000, "buy_price": 1.0})           # 太小
    for bad in ({}, {"n_buy": 1, "buy_qty": "x", "buy_price": 1}, {"n_buy": 1, "buy_qty": -5, "buy_price": 1}, {"n_buy": True}):
        assert not insight.round_block(bad)


def test_buy_sell_order():
    assert insight.buy_sell_order([["2026-01-01", 100], ["2026-03-01", -100]]) == 1.0
    assert insight.buy_sell_order([["2026-03-01", 100], ["2026-01-01", -100]]) == -1.0
    assert insight.buy_sell_order([["2026-01-01", 100], ["2026-02-01", -100], ["2026-03-01", 100]]) == 0.0
    assert insight.buy_sell_order([["2026-01-01", 100]]) is None and insight.buy_sell_order(None) is None
    assert insight.buy_sell_order([["x"], [None, "a"], "zz"]) is None


def _two_way(**kw):
    tr = {"n": 20, "n_buy": 10, "n_sell": 10, "buy_qty": 1_000_000, "sell_qty": 1_100_000, "net_qty": -100_000, "first": "2026-01-05",
          "last": "2026-10-30", "days": 20, "buy_price": 2.0, "sell_price": 2.1, "kinds": {"on_market": 2_100_000}}
    tr.update(kw)
    return _sell(style="hedge", voting_power=[8.0, 7.9], votes=[8_000_000, 7_900_000], trades=tr)


def test_two_way_trading_is_split():
    early_buys = [[f"2026-0{m}-10", 100_000] for m in range(1, 6)] + [[f"2026-0{m}-10", -110_000] for m in range(6, 10)]
    r = insight.judge(_two_way(daily=early_buys))
    assert r["action"] == "flip" and any("先买进再卖出" in x and "高 5.0%" in x for x in r["reasons"])
    r = insight.judge(_two_way(daily=[[d, -q] for d, q in early_buys]))
    assert r["action"] == "flip_back"
    mixed = [[f"2026-0{m}-10", 100_000 if m % 2 else -110_000] for m in range(1, 10)]
    r = insight.judge(_two_way(daily=mixed))
    assert r["action"] == "churn" and any("穿插着做" in x and "对冲基金的日常交易" in x for x in r["reasons"])
    r = insight.judge(_two_way())                                                # 没有逐日数字：不说“穿插”
    assert r["action"] == "churn" and not any("穿插" in x for x in r["reasons"])
    r = insight.judge(_two_way(ord=0.8))                                         # 历史记录只留了先后的数
    assert r["action"] == "flip"
    r = insight.judge(_two_way(n_buy=1, buy_qty=1_000_000, buy_price=1.5, daily=early_buys))   # 一笔 $150 万整的认购，之后卖出
    assert r["action"] == "flip_plc" and "参与融资" in r["tags"] and any("认购配售新股" in x for x in r["reasons"])
    r = insight.judge(_two_way(daily=early_buys, buy_qty=500_000, net_qty=-600_000))           # 明细对不上：不判先后
    assert r["action"] in ("churn", "rotate") or r["action"].startswith("trim")


def test_round_block_buy_points_to_placement():
    base = _buy(trades={**_buy()["trades"], "n_buy": 1, "buy_qty": 2_000_000, "buy_price": 1.25})   # $250 万整
    r = insight.judge(base)
    assert "整数金额" in r["tags"] and any("谈好价格的一笔大宗交易" in x for x in r["reasons"])
    r = insight.judge({**base, "ev": RAISE})
    assert r["action"] == "add_placement" and any("配合公司同期的融资公告" in x for x in r["reasons"])


def test_history_record_keeps_buy_sell_order():
    e = {"id": "1", "type": "change", "votes": [1, 2], "trades": {"n": 2, "daily": [["2026-01-01", 5], ["2026-02-01", -3]]}}
    rec = insight.history_record(e)
    assert rec["tr"]["ord"] == 1.0 and "daily" not in rec["tr"]


def test_rebalance_window_and_index_note():
    assert scrape.rebalance_day(dt.date(2026, 12, 15)) == dt.date(2026, 12, 18)
    assert scrape.rebalance_day(dt.date(2026, 3, 30)) == dt.date(2026, 3, 20)
    assert scrape.rebalance_day(dt.date(2026, 11, 2)) is None
    vg = {"status": "parsed", "holder_name": "The Vanguard Group", "headline": "Becoming a substantial holder", "type": "becoming", "change_date": "2026-12-16"}
    ss = {"status": "parsed", "holder_name": "State Street Corporation", "headline": "Change in substantial holding", "type": "change",
          "voting_power": [5.5, 6.1], "change_date": "2026-12-19"}
    e = {"change_date": "2026-12-17"}
    ins = {"style": "养老金", "side": "buy", "reasons": ["a", "最后"], "tags": []}
    scrape.note_index_moves(ins, e, "XYZ", {"XYZ": [vg, ss]})
    assert "指数调整期" in ins["tags"] and "Vanguard" in ins["reasons"][1] and ins["reasons"][-1] == "最后"
    for bad_ins, bad_e, groups in (({**ins, "style": "主动选股基金", "tags": []}, e, {"XYZ": [vg, ss]}),
                                   ({**ins, "tags": []}, {"change_date": "2026-11-02"}, {"XYZ": [vg, ss]}),
                                   ({**ins, "tags": []}, e, {"XYZ": [vg]}), ({**ins, "side": "sell", "tags": []}, e, {"XYZ": [vg, ss]}),
                                   ({**ins, "tags": []}, {"change_date": "x"}, {}), ({}, {}, {})):
        scrape.note_index_moves(bad_ins, bad_e, "XYZ", groups)
        assert "指数调整期" not in (bad_ins.get("tags") or [])


def test_slip_keeps_label_but_gets_purposes():
    prev = {"vp": 5.1, "votes": 5_100_000, "type": "change", "date": "2026-06-01"}
    tr = {**_sell()["trades"], "sell_qty": 150_000, "net_qty": -150_000}
    r = insight.judge(_sell(type="ceasing", voting_power=[], votes=[], prev=prev, trades=tr, px=_px(r6m=0.6, r12m=0.9, pos=0.95)))
    assert r["action"] == "slip" and r["label"] == "小幅卖出，刚好跌破 5% 线"
    assert r["purposes"] and r["purposes"][0]["k"] == "profit"
    assert any(x.startswith("形式上只是刚好跌破 5% 线；综合打分：") for x in r["reasons"])
    # 来回交易也不会盖过“刚好跌破”
    tr2 = {**tr, "buy_qty": 120_000, "n_buy": 3, "buy_price": 2.0, "net_qty": -30_000, "sell_qty": 150_000}
    assert insight.judge(_sell(type="ceasing", voting_power=[], votes=[], prev=prev, trades=tr2))["action"] == "slip"


# ---------------------------------------------------------------- 第十九轮：公司的发行文件（Appendix 3B / 2A）

def _form(name):
    return (FIXTURES / "issue_forms" / f"{name}.txt").read_text(encoding="utf-8")


def test_parse_issue_forms_from_real_samples():
    r = insight.parse_issue_form(_form("TTT_03134624"), "TTT")                    # 配售：股票 + 给券商的期权，只取普通股
    assert r == {**r, "f": "3B", "kind": "plc", "p": 0.13, "q": 126_923_078, "d": "2026-09-08"} and "Huntsville" in r["why"]
    assert insight.parse_issue_form(_form("PPC_03132672"), "PPC")["kind"] == "emp"  # 勾的是“placement or other”，用途是员工持股信托
    assert insight.parse_issue_form(_form("PPC_03135368"), "PPC")["kind"] == "emp"
    assert insight.parse_issue_form(_form("RIV_03160125"), "RIV") == {**insight.parse_issue_form(_form("RIV_03160125"), "RIV"),
                                                                      "f": "2A", "kind": "drp", "p": 1.2225, "q": 136_450}
    r = insight.parse_issue_form(_form("MP1_03100361"), "MP1")                    # 配股：股数在汇总表、价格叫 offer price
    assert r["kind"] == "pro" and r["p"] == 14.3 and r["q"] == 57_856_541
    assert insight.parse_issue_form(_form("GTI_03116463"), "GTI")["kind"] == "noncash"   # 收购换股，不是现金
    assert "p" not in insight.parse_issue_form(_form("DXN_03130711"), "DXN")
    r = insight.parse_issue_form(_form("TOR_03152935"), "TOR")
    assert r["f"] == "2A" and r["p"] == 0.19 and r["kind"] in insight.ISSUE_NAMES
    for bad in ("", None, 5, "Change in substantial holding", "Appendix 3B - Proposed issue of securities\nnothing"):
        r = insight.parse_issue_form(bad, "X")
        assert r == {} or "p" not in r


def _iss(d, p, q=10_000_000, kind="plc"):
    return {"d": d, "k": "raise", "h": "Proposed issue of securities", "u": "1", "iss": {"f": "3B", "kind": kind, "p": p, "q": q, "d": d, "disc": -0.08}}


def test_issue_check_confirms_and_refutes():
    tr = {"buy_price": 0.13, "first": "2026-09-01", "last": "2026-09-30", "daily": [["2026-09-07", 3_000_000]]}
    hit, ref = insight.issue_check([_iss("2026-09-08", 0.13)], tr, True, "2026-10-10")
    assert hit and not ref
    assert insight.issue_check([_iss("2026-09-08", 0.20)], tr, True, "2026-10-10") == (None, True)        # 价格对不上：否定
    spread = {**tr, "daily": [[f"2026-09-{d:02d}", 1_000_000] for d in (1, 15, 20, 25, 28, 30)]}               # 量没集中在发行前后：不算
    assert insight.issue_check([_iss("2026-09-08", 0.13)], spread, False, "2026-10-10") == (None, False)
    assert insight.issue_check([_iss("2026-09-08", 0.13)], {**tr, "daily": None, "n_buy": 1}, False, "2026-10-10")[0]
    assert insight.issue_check([_iss("2026-09-08", 0.13, kind="emp")], tr, True, "2026-10-10") == (None, True)
    assert insight.issue_check([], tr, False, "2026-10-10") == (None, False)                               # 没读过文件：不否定
    assert insight.issue_check([], {**tr, "daily": [["2025-01-07", 5]]}, True, "2026-10-10") == (None, False)   # 太早，看不到
    assert insight.issue_check([], {**tr, "daily": [[f"2026-09-0{i}", 5] for i in range(1, 6)]}, True, "2026-10-10") == (None, False)
    assert insight.issue_check([{"iss": "x"}, {"iss": {"p": "a"}}], {**tr, "buy_price": None}, True, "x") == (None, False)


def test_judge_uses_issue_forms():
    trades = {**_buy()["trades"], "n_buy": 1, "buy_qty": 2_000_000, "buy_price": 1.25, "daily": [["2026-10-30", 2_000_000]]}
    n = _buy(trades=trades, ev=[_iss("2026-10-30", 1.25)], iss_ok=True)
    r = insight.judge(n)
    assert r["action"] == "add_placement" and r["purposes"][0]["p"] > 0.7
    assert any(x.startswith("公司的发行文件对得上：10/30 按 $1.25 发行 1,000 万股新股（配售，Appendix 3B），发行价比公告前一天的收盘价低 8%") for x in r["reasons"])
    # PPC 那种：一笔整数金额，但同期只有员工持股的发行
    r = insight.judge(_buy(trades=trades, ev=[_iss("2026-08-28", 1.97, kind="emp")], iss_ok=True))
    assert r["action"] != "add_placement" and "整数金额" not in r["tags"] and any("不是认购配售" in x for x in r["reasons"])
    # 员工发行的“Proposed issue of securities”不再算融资
    emp = [_iss("2026-10-31", 1.97, kind="emp")]
    assert not any("公告过融资" in x or "公告了融资" in x for x in insight.judge(_buy(ev=emp, iss_ok=True))["reasons"])


def test_company_events_keep_issue_form_links():
    html = """<table><tr><td>30/10/2026</td><td></td><td><a href="/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId=03160125">Application for quotation of securities - RIV</a></td></tr>
    <tr><td>30/10/2026</td><td></td><td><a href="/x?idsId=03160126">Application for quotation of securities - RIV</a></td></tr>
    <tr><td>05/10/2026</td><td></td><td><a href="/x?idsId=03148365">Cancel - Proposed issue of securities - YRL</a></td></tr>
    <tr><td>02/09/2026</td><td></td><td><a href="/x?idsId=03134624">Proposed issue of securities - TTT</a></td></tr></table>"""
    evs = scrape.parse_company_events(html)
    assert [(e["k"], e.get("u")) for e in evs] == [("raise", "03134624"), ("quote", "03160125"), ("quote", "03160126")]
    many = [{"d": f"2026-10-{i:02d}", "k": "quote"} for i in range(1, 25)] + [{"d": f"2026-10-{i:02d}", "k": "results"} for i in range(1, 25)]
    near = scrape.events_near(many, {"date": "2026-10-30", "change_date": "2026-10-01"})
    assert sum(x["k"] == "quote" for x in near) == 8 and sum(x["k"] == "results" for x in near) == 20


def test_read_issue_forms(monkeypatch):
    monkeypatch.setattr(scrape, "download_pdf", lambda url: (b"%PDF", url))
    monkeypatch.setattr(scrape, "pdf_texts", lambda data, pages=8: [_form("TTT_03134624")])
    monkeypatch.setattr(scrape, "PAUSE", 0)
    monkeypatch.setattr(scrape, "_ISSUES", {})
    day = lambda d: int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    series = [[day("2026-09-01"), 0.15, 1], [day("2026-09-02"), 0.14, 1]]
    evs = [{"d": "2026-09-02", "k": "raise", "u": "03134624"}, {"d": "2026-09-03", "k": "results"}]
    scrape.read_issue_forms("TTT", evs, series)
    assert evs[0]["iss"]["p"] == 0.13 and evs[0]["iss"]["disc"] == round(0.13 / 0.15 - 1, 3) and "iss" not in evs[1]
    monkeypatch.setattr(scrape, "download_pdf", lambda url: (_ for _ in ()).throw(scrape.FetchError("x")))
    monkeypatch.setattr(scrape, "_ISSUES", {})
    evs = [{"d": "2026-09-02", "k": "raise", "u": "9"}]
    scrape.read_issue_forms("TTT", evs, series)
    assert "iss" not in evs[0]
