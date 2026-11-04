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
                 api_items=(), api_down=False, api_pdfs=None):
        self.listing, self.fail_ids, self.lists_down = listing, set(fail_ids), lists_down
        self.prev_listing, self.broken_ids = prev_listing, set(broken_ids)
        self.api_items, self.api_down, self.api_pdfs = list(api_items), api_down, api_pdfs or {}
        self.pdf_downloads = []
        self.api_pages = []

    def __call__(self, url, *, max_bytes=None, tries=3, timeout=60):
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
