"""
ASX 大股东公告雷达：抓取脚本（在 GitHub Actions 里运行）

流程
1. 读取 ASX 全市场公告列表（今日 + 上一交易日）。
2. 挑出标题含 substantial holder / holding 的公告。
3. 新公告：下载 PDF，读前两页，解析申报方名称、目标公司、持股比例、变动日。
4. 全部大股东公告和解析结果存进 data/all_notices.json（保留 KEEP_DAYS 天）。
5. 每次运行都用当前关注名单重新匹配，生成网页读取的 data/notices.json。
   所以改了 watchlist.json 之后，保留期内的旧公告也会按新名单重新匹配。

ASX 公开的全市场列表只提供当天和上一交易日，所以“近两周”的数据靠每天运行累积。
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

logging.getLogger("pypdf").setLevel(logging.ERROR)

SYD = ZoneInfo("Australia/Sydney")
BASE = "https://www.asx.com.au"
LIST_PAGES = (
    BASE + "/asx/v2/statistics/todayAnns.do",
    BASE + "/asx/v2/statistics/prevBusDayAnns.do",
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}

KEEP_DAYS = int(os.environ.get("KEEP_DAYS", "35"))       # 网页最多看 30 天，多留几天余量
PDF_PAGES = 2                                             # 申报方、比例都在前两页
PAUSE = float(os.environ.get("PAUSE_SECONDS", "0.6"))     # 两次下载之间的间隔，别给 ASX 压力
MAX_PDFS = int(os.environ.get("MAX_PDFS", "300"))         # 单次运行最多下载多少份 PDF
MAX_PDF_BYTES = 25 * 1024 * 1024                          # 超过 25MB 的 PDF 不下载
MAX_ATTEMPTS = 3                                          # 下载失败的公告最多重试几次
SCHEMA = 2
PARSER_VERSION = 2   # 解析规则改进后加 1，已解析的旧记录会在下次运行时按新规则重读一次

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
NOTICES_FILE = DATA_DIR / "notices.json"      # 网页读取（只含关注名单的记录）
STORE_FILE = DATA_DIR / "all_notices.json"    # 内部存储（全部大股东公告）
LEGACY_SEEN_FILE = DATA_DIR / "seen.json"     # 第一版的文件，迁移后删除
WATCH_FILE = ROOT / "scraper" / "watchlist.json"

SUBSTANTIAL_RE = re.compile(r"substantial\s+(?:holder|holding|shareholder)", re.I)


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 网络

class FetchError(RuntimeError):
    """临时性失败，下次运行会重试。"""


class PermanentFetchError(FetchError):
    """重试也没用的失败（404、文件过大），不再重试。"""


@dataclass
class Fetched:
    url: str
    content: bytes
    content_type: str = ""

    @property
    def text(self) -> str:
        m = re.search(r"charset=([\w-]+)", self.content_type, re.I)
        enc = m.group(1) if m else "utf-8"
        try:
            return self.content.decode(enc, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


session = requests.Session()
session.headers.update(HEADERS)


def fetch(url: str, *, max_bytes: int | None = None, tries: int = 3, timeout: int = 60) -> Fetched:
    last = "未知错误"
    for attempt in range(1, tries + 1):
        try:
            with session.get(url, timeout=timeout, stream=True, allow_redirects=True) as r:
                if r.status_code == 200:
                    length = r.headers.get("Content-Length", "")
                    if max_bytes and length.isdigit() and int(length) > max_bytes:
                        raise PermanentFetchError(f"文件太大（{int(length) // 1048576}MB），已跳过")
                    buf = bytearray()
                    for chunk in r.iter_content(chunk_size=65536):
                        buf.extend(chunk)
                        if max_bytes and len(buf) > max_bytes:
                            raise PermanentFetchError("文件太大，已跳过")
                    return Fetched(r.url, bytes(buf), r.headers.get("Content-Type", ""))
                if r.status_code in (404, 410):
                    raise PermanentFetchError(f"HTTP {r.status_code}")
                last = f"HTTP {r.status_code}"
        except PermanentFetchError:
            raise
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < tries:
            time.sleep(2 * attempt)
    raise FetchError(f"{last}（{url}）")


def safe_url(u: str) -> str:
    return u if re.match(r"https?://", u or "", re.I) else ""


# ---------------------------------------------------------------- 列表页

DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([AP])\.?\s*M\.?", re.I)
TAIL_RE = re.compile(r"\s*\d+\s+pages?(?:\s+[\d.,]+\s*[KMG]?B)?\s*$", re.I)
PAGES_RE = re.compile(r"(\d+)\s+pages?", re.I)
PRICESENS_RE = re.compile("pricesens", re.I)


def to_24h(h: str, m: str, ap: str) -> str:
    hour = int(h) % 12 + (12 if ap.upper() == "P" else 0)
    return f"{hour:02d}:{m}"


def is_price_sensitive(tr, tds) -> bool:
    el = tr.find(class_=PRICESENS_RE)
    if el is not None:
        return el.name == "img" or el.find("img") is not None
    return len(tds) >= 4 and tds[2].find("img") is not None


def parse_listing(html: str, today: dt.date) -> tuple[list[dict], bool]:
    """返回 (公告列表, 是否认出了页面结构)。"""
    soup = BeautifulSoup(html, "html.parser")
    page_date = None
    m = re.search(r"published on\s+(\d{1,2}\s+\w+\s+\d{4})", soup.get_text(" "), re.I)
    if m:
        try:
            page_date = dt.datetime.strptime(m.group(1), "%d %B %Y").date()
        except ValueError:
            page_date = None

    rows = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        a = tr.find("a", href=re.compile(r"idsId|displayAnnouncement|asxpdf", re.I))
        if len(tds) < 3 or a is None or not a.get("href"):
            continue
        code = tds[0].get_text(strip=True).upper()
        if not re.fullmatch(r"[A-Z0-9]{3,6}", code):
            continue

        date_text = tds[1].get_text(" ", strip=True)
        dm = DATE_RE.search(date_text)
        tm = TIME_RE.search(date_text)
        d = page_date or today
        if dm:
            try:
                d = dt.date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))
            except ValueError:
                pass

        full = a.get_text(" ", strip=True)
        pm = PAGES_RE.findall(full)
        headline = TAIL_RE.sub("", full).strip()
        href = safe_url(urljoin(BASE, a["href"].strip()))
        if not href:
            continue
        idm = re.search(r"idsId=(\w+)", href)

        rows.append({
            "id": idm.group(1) if idm else href,
            "code": code,
            "date": d.isoformat(),
            "time": to_24h(*tm.groups()) if tm else "",
            "headline": headline,
            "url": href,
            "pages": int(pm[-1]) if pm else None,
            "price_sensitive": is_price_sensitive(tr, tds),
        })

    recognised = bool(rows) or bool(re.search(r"ASX\s*Code|no announcements", html, re.I))
    return rows, recognised


def classify(headline: str) -> str:
    h = headline.lower()
    if "ceas" in h:
        return "ceasing"
    if "becoming" in h or "became" in h or "initial" in h:
        return "becoming"
    if "change" in h:
        return "change"
    return "other"


def headline_hint(headline: str) -> str:
    m = re.search(r"\bfrom\s+(.+)$", headline, re.I)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------- PDF

def resolve_pdf_link(html: str, base_url: str) -> str:
    """ASX 有时先返回一个“同意条款”的中间页，真正的 PDF 地址藏在里面。"""
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    inp = soup.find("input", attrs={"name": re.compile(r"pdfurl", re.I)})
    if inp and inp.get("value"):
        candidates.append(inp["value"])
    m = re.search(r"((?:https?://[^\s\"'<>]+)?/asxpdf/[^\s\"'<>]+?\.pdf)", html, re.I)
    if m:
        candidates.append(m.group(1))
    a = soup.find("a", href=re.compile(r"\.pdf(?:[?#]|$)", re.I))   # 最后才用普通 PDF 链接，避免拿到条款文件
    if a:
        candidates.append(a["href"])
    for c in candidates:
        u = safe_url(urljoin(base_url, c.strip()))
        if u:
            return u
    return ""


def download_pdf(url: str) -> tuple[bytes, str]:
    r = fetch(url, max_bytes=MAX_PDF_BYTES)
    if "pdf" in r.content_type.lower() or r.content[:5] == b"%PDF-":
        return r.content, r.url
    target = resolve_pdf_link(r.text, r.url or BASE)
    if not target:
        raise FetchError("中间页里找不到 PDF 地址")
    r2 = fetch(target, max_bytes=MAX_PDF_BYTES)
    if "pdf" not in r2.content_type.lower() and r2.content[:5] != b"%PDF-":
        raise FetchError("下载到的不是 PDF")
    return r2.content, r2.url


WATERMARK_RE = re.compile(r"^for\s+personal\s+use\s+only$", re.I)   # ASX 给每页加的水印；扫描件只剩这行


def pdf_texts(data: bytes, pages: int = PDF_PAGES) -> list[str]:
    """同一份 PDF 用两种方式提取文字：[按版面位置, 默认顺序]，去掉空的和重复的。

    ASX 很多 603/604 是在模板上“填”出来的：默认提取会先吐出模板标签，再把填写的内容
    乱序堆在后面，“Name”和申报方名称就分家了；按版面位置提取能让同一行的标签和内容排在一起。
    但版面模式有时会把字拆开（“Nam e M ercer”），或者名称排在标签上一行，这时默认模式反而读得对。
    所以两种都读，每个字段取第一个解析得出的结果（真实样本见 tests/fixtures）。
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001
                return []
        plist = list(reader.pages[:pages])
    except Exception as e:  # noqa: BLE001
        log(f"    PDF 解析失败: {e}")
        return []
    out = []
    for mode in ("layout", "plain"):
        parts = []
        for p in plist:
            try:
                parts.append((p.extract_text(extraction_mode=mode) if mode == "layout" else p.extract_text()) or "")
            except Exception as e:  # noqa: BLE001
                log(f"    PDF {mode} 模式提取失败: {e}")
        text = re.sub(r"[ \t\u00a0]+", " ", "\n".join(parts))
        text = "\n".join(ln.strip() for ln in text.split("\n") if ln.strip() and not WATERMARK_RE.match(ln.strip()))
        if text and text not in out:
            out.append(text)
    return out


def pdf_text(data: bytes, pages: int = PDF_PAGES) -> str:
    """调试用：返回第一种读得出来的文字。"""
    texts = pdf_texts(data, pages)
    return texts[0] if texts else ""


SECTION1_RE = re.compile(r"Details\s+of\s+substantial\s+(?:share)?holder|substantial\s+holder\s*\(\s*1\s*\)", re.I)
COMPANY_RE = re.compile(r"To\s*:?\s*Company\s+Name\s*/\s*Scheme\s*[:\-]?[ \t]*(.*)", re.I)
SECTION2_RE = re.compile(r"\n\s*2\s*\.\s+\S")
LABEL_RE = re.compile(
    r"^(?:the\s+(?:holder|previous)\b|there\s+was\b|acn\b|arsn\b|abn\b|name\b|details\b|previous\b|holder\b|[(\d])"
    r"|substantial\s+holder|if\s+applicable|notice\s+was",
    re.I,
)
NAME_LINE_RE = re.compile(r"^(?:\(\s*1\s*\)\s*)?Name\b\s*(?:\(if applicable\))?\s*[:\-]?\s*(.*)$", re.I)
ID_TAIL_RE = re.compile(r"\s*(?:\b(?:ACN|ARSN|ABN)\b|\(if applicable\)).*$", re.I)


def parse_company(text: str) -> str:
    m = COMPANY_RE.search(text)
    if not m:
        return ""
    val = m.group(1).strip()
    if len(val) < 2:
        val = text[m.end():].lstrip("\n").split("\n", 1)[0].strip()
    return ID_TAIL_RE.sub("", val).strip(" :-")[:120]


def clean_name(s: str) -> str:
    return ID_TAIL_RE.sub("", s).strip(" :-")


def looks_like_name(s: str) -> bool:
    """排除表格标签（The holder became…、ACN/ARSN…）和纯数字，避免把标签当成申报方名称。"""
    s = s.strip()
    return 3 <= len(s) <= 150 and len(re.findall(r"[A-Za-z]", s)) >= 3 and not LABEL_RE.search(s)


def parse_holder_section(text: str, first_line: bool = True) -> tuple[str, str]:
    """返回 (申报方名称, 第一节原文)。名称解析不出来时，靠第一节原文做中等可信度匹配。"""
    m = SECTION1_RE.search(text)
    if not m:
        return "", ""
    region = text[m.end(): m.end() + 700]
    s2 = SECTION2_RE.search(region)
    if s2:
        region = region[: s2.start()]          # 只看第一节，避免读到后面提到的目标公司
    lines = [ln.strip() for ln in region.split("\n") if ln.strip() and not COMPANY_RE.search(ln)]
    region = "\n".join(lines)
    for i, line in enumerate(lines):
        nm = NAME_LINE_RE.match(line)
        if not nm:
            continue
        # 名称可能在同一行，也可能因为 PDF 按列排版被挤到后面几行
        for cand in [nm.group(1)] + lines[i + 1: i + 5]:
            val = clean_name(cand)
            if looks_like_name(val):
                return val[:200], region
        return "", region
    # 没有“Name”标签的自定义格式（例如花旗）：名称直接写在第一节标题下的第一行
    if first_line and lines:
        val = clean_name(lines[0])
        if looks_like_name(val):
            return val[:200], region
    return "", region


VP_RE = re.compile(r"voting\s+power", re.I)
PCT_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,4})?)\s?%")
EVENT_RE = re.compile(
    r"(?:became|ceased\s+to\s+be)\s+a\s+substantial\s+holder\s+on"
    r"|change\s+in\s+the\s+interests\s+of\s+the"      # 604 常见排版：日期夹在“of the”和“substantial holder on”之间
    r"|interests\s+of\s+the\s+substantial\s+holder\s+on"
    r"|substantial\s+holder\s+on",
    re.I,
)
DATE_NUM_RE = re.compile(r"(?<!\d)(\d{1,2})\s?[/.\-]\s?(\d{1,2})\s?[/.\-]\s?(\d{4}|\d{2})(?!\d)")   # 兼容“29 /10/2026”这种多了空格的写法
DATE_TXT_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?[\s\-]+([A-Za-z]{3,9})[\s,\-]+(\d{4})")


def parse_date(seg: str) -> str:
    found = []
    m = DATE_NUM_RE.search(seg)
    if m:
        d, mo, y = (int(x) for x in m.groups())
        y = y + 2000 if y < 100 else y
        try:
            found.append((m.start(), dt.date(y, mo, d)))
        except ValueError:
            pass
    m = DATE_TXT_RE.search(seg)
    if m:
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                found.append((m.start(), dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", fmt).date()))
                break
            except ValueError:
                continue
    return min(found)[1].isoformat() if found else ""


def parse_details(text: str, ntype: str) -> dict:
    out = {"change_date": "", "voting_power": []}
    m = EVENT_RE.search(text)
    if m:
        out["change_date"] = parse_date(text[m.end(): m.end() + 250])
    if ntype != "ceasing":
        vm = VP_RE.search(text)
        seg = text[vm.start(): vm.start() + 1500] if vm else ""
        vals = [float(x) for x in PCT_RE.findall(seg) if 0 < float(x) <= 100]
        out["voting_power"] = vals[:2] if ntype == "change" else vals[:1]
    return out


# ---------------------------------------------------------------- 关注名单与匹配

@dataclass
class Holder:
    name: str
    aliases: list
    hints: list


def word_re(s: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(s.strip()) + r"(?![A-Za-z0-9])", re.I)


def load_watchlist(path: Path | None = None) -> tuple[list[Holder], list[re.Pattern]]:
    path = path or WATCH_FILE          # 运行时再取，默认参数在定义时就固定了
    raw = load_json(path, None)
    if not isinstance(raw, dict) or not raw.get("holders"):
        raise SystemExit(f"{path} 缺失或格式不对：需要一个含 holders 列表的 JSON")
    holders = []
    for h in raw["holders"]:
        aliases = [word_re(a) for a in h.get("aliases", []) if a and a.strip()]
        if not h.get("name") or not aliases:
            raise SystemExit(f"watchlist.json 里有一项缺少 name 或 aliases：{h}")
        holders.append(Holder(h["name"], aliases, [word_re(x) for x in h.get("hints", []) if x and x.strip()]))
    noise = [word_re(x) for x in raw.get("noise_hints", []) if x and x.strip()]
    return holders, noise


def any_hit(patterns, text: str) -> bool:
    return bool(text) and any(p.search(text) for p in patterns)


def match_entry(e: dict, holders: list[Holder]) -> tuple[str | None, str]:
    """高：PDF 里的申报方名称命中；中：名称没解析出来，但第一节原文命中；
    低：PDF 读不出来，只能靠标题里的 “from XXX”。"""
    name = e.get("holder_name") or ""
    if name:
        for h in holders:
            if any_hit(h.aliases, name):
                return h.name, "高"
        return None, ""          # 名称解析出来了但不在名单里，不再用其他线索猜
    region = e.get("region") or ""
    if region:
        for h in holders:
            if any_hit(h.aliases, region):
                return h.name, "中"
    legacy = e.get("legacy_holder")
    if legacy and legacy in {h.name for h in holders}:
        return legacy, e.get("legacy_confidence") or "中"
    hint = e.get("hint") or ""
    for h in holders:
        if any_hit(h.hints, hint) or any_hit(h.aliases, hint):
            return h.name, "低"
    return None, ""


def is_noise(hint: str, holders: list[Holder], noise: list[re.Pattern]) -> bool:
    if not any_hit(noise, hint):
        return False
    return not any(any_hit(h.hints, hint) or any_hit(h.aliases, hint) for h in holders)


# ---------------------------------------------------------------- 存储

def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log(f"警告：{path.name} 读取失败，按空文件处理")
    return default


def write_json(path: Path, obj, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if pretty:
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    else:
        tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def load_store() -> dict:
    store = load_json(STORE_FILE, None)
    if isinstance(store, dict) and store.get("schema") == SCHEMA:
        store.setdefault("entries", {})
        store.setdefault("scanned_days", [])
        return store
    # 从第一版迁移：沿用旧的匹配结果，名称字段留空
    store = {"schema": SCHEMA, "entries": {}, "scanned_days": [], "last_success_at": None}
    old = load_json(NOTICES_FILE, {}) or {}
    for n in old.get("notices", []) or []:
        if not n.get("id") or not n.get("holder"):
            continue
        store["entries"][n["id"]] = {
            **{k: n.get(k) for k in ("id", "code", "date", "headline", "url", "pdf_url", "price_sensitive", "type")},
            "time": n.get("time", ""), "company": "", "hint": headline_hint(n.get("headline", "")),
            "holder_name": "", "region": "", "change_date": "", "voting_power": n.get("voting_power", []),
            "status": "parsed", "attempts": 1,
            "legacy_holder": n["holder"], "legacy_confidence": n.get("confidence", "中"),
        }
    store["scanned_days"] = list(old.get("scanned_days", []) or [])
    store["last_success_at"] = old.get("updated_at")
    if store["entries"]:
        log(f"已从旧版数据迁移 {len(store['entries'])} 条记录")
    return store


PUBLIC_FIELDS = ("id", "code", "company", "date", "time", "headline", "url", "pdf_url",
                 "price_sensitive", "type", "change_date", "voting_power")


def build_notices(entries: dict, holders: list[Holder]) -> list[dict]:
    out = []
    for e in entries.values():
        holder, conf = match_entry(e, holders)
        if holder:
            item = {k: e.get(k) for k in PUBLIC_FIELDS}
            item["url"] = safe_url(item.get("url") or "")
            item["pdf_url"] = safe_url(item.get("pdf_url") or "") or item["url"]
            item.update(holder=holder, confidence=conf)
            out.append(item)
    out.sort(key=lambda n: (n.get("date") or "", n.get("time") or "", n.get("id") or ""), reverse=True)
    return out


# ---------------------------------------------------------------- 主流程

def analyse_texts(texts: list[str], ntype: str) -> dict:
    """在多种提取结果里，每个字段取第一个解析得出的值。"""
    out = {"company": "", "holder_name": "", "region": "", "change_date": "", "voting_power": []}
    regions = []
    want_vp = 2 if ntype == "change" else 1
    for t in texts:
        name, region = parse_holder_section(t, first_line=False)   # 第一轮只认“Name”标签
        if region:
            regions.append(region[:500])
        out["holder_name"] = out["holder_name"] or name
        out["company"] = out["company"] or parse_company(t)
        d = parse_details(t, ntype)
        out["change_date"] = out["change_date"] or d["change_date"]
        if len(d["voting_power"]) > len(out["voting_power"]) and len(out["voting_power"]) < want_vp:
            out["voting_power"] = d["voting_power"]
    if not out["holder_name"]:
        # 第二轮：没有“Name”标签的自定义格式，取第一节标题下的第一行。
        # 放在第二轮，是因为版面模式偶尔把“Name”拆成“Nam e”，首行会是一行乱码，不能抢在正确结果前面。
        for t in texts:
            out["holder_name"] = parse_holder_section(t)[0]
            if out["holder_name"]:
                break
    if not out["holder_name"]:
        out["region"] = "\n".join(regions)[:1000]   # 名称解析不出来时，留原文做中等可信度匹配
    return out


def process_entry(e: dict) -> None:
    data, pdf_url = download_pdf(e["url"])
    e["pdf_url"] = safe_url(pdf_url)
    texts = pdf_texts(data)
    if not texts:
        e["status"], e["parser"] = "unreadable", PARSER_VERSION
        return
    e.update(analyse_texts(texts, e.get("type", "other")))
    e["status"], e["parser"] = "parsed", PARSER_VERSION


def hinted(e: dict, holders: list[Holder]) -> bool:
    hint = e.get("hint", "")
    return any(any_hit(h.hints, hint) or any_hit(h.aliases, hint) for h in holders)


def main() -> int:
    now = dt.datetime.now(SYD)
    today = now.date()
    holders, noise = load_watchlist()
    store = load_store()
    entries: dict = store["entries"]
    scanned = set(store.get("scanned_days", []))
    errors: list[str] = []
    pages_ok = 0

    rows = []
    for url in LIST_PAGES:
        name = url.rsplit("/", 1)[-1]
        try:
            got, recognised = parse_listing(fetch(url).text, today)
        except FetchError as e:
            errors.append(f"读取 {name} 失败：{e}")
            log(errors[-1])
            continue
        log(f"{name}：{len(got)} 条公告")
        if not recognised:
            errors.append(f"{name} 的页面结构认不出来，ASX 可能改版了，需要调整 parse_listing()")
            continue
        pages_ok += 1
        rows.extend(got)
        scanned.update(r["date"] for r in got)

    found_ids = set()
    for r in rows:
        if not SUBSTANTIAL_RE.search(r["headline"]):
            continue
        found_ids.add(r["id"])
        if r["id"] not in entries:
            entries[r["id"]] = {
                **r, "type": classify(r["headline"]), "hint": headline_hint(r["headline"]),
                "company": "", "holder_name": "", "region": "", "change_date": "", "voting_power": [],
                "pdf_url": "", "status": "new", "attempts": 0,
            }
    found = len(found_ids)
    log(f"大股东相关公告 {found} 条")

    for e in entries.values():
        if e.get("status") in ("parsed", "unreadable") and e.get("parser", 1) < PARSER_VERSION:
            e["status"], e["attempts"] = "new", 0      # 用新的解析规则重读一次
    todo = [e for e in entries.values()
            if e.get("status") in ("new", "error") and e.get("attempts", 0) < MAX_ATTEMPTS]
    todo.sort(key=lambda e: (e.get("date", ""), e.get("time", "")), reverse=True)   # 新的优先
    todo.sort(key=lambda e: not hinted(e, holders))                                   # 标题带关注名单缩写的更优先

    checked = skipped = deferred = failed = 0
    for e in todo:
        if is_noise(e.get("hint", ""), holders, noise):
            e["status"] = "noise"
            skipped += 1
            continue
        if checked >= MAX_PDFS:
            deferred += 1
            continue
        checked += 1
        e["attempts"] = e.get("attempts", 0) + 1
        try:
            process_entry(e)
        except PermanentFetchError as ex:
            e["status"], e["error"] = "skipped", str(ex)
        except FetchError as ex:
            e["status"], e["error"] = "error", str(ex)
            failed += 1
            log(f"  ! {e['code']} {e['headline']}：{ex}")
        except Exception as ex:  # noqa: BLE001  单份公告解析出错不能拖垮整次运行
            e["status"], e["error"] = "error", f"{type(ex).__name__}: {ex}"
            failed += 1
            log(f"  ! {e['code']} {e['headline']}：解析出错\n{traceback.format_exc()}")
        e["checked_at"] = dt.datetime.now(SYD).isoformat(timespec="seconds")
        time.sleep(PAUSE)

    if failed:
        errors.append(f"{failed} 份公告 PDF 下载失败，下次运行会自动重试（每份最多 {MAX_ATTEMPTS} 次）")
    if deferred:
        log(f"本次已达 {MAX_PDFS} 份上限，剩余 {deferred} 份留到下次运行")

    cutoff = (today - dt.timedelta(days=KEEP_DAYS)).isoformat()
    entries = {k: v for k, v in entries.items() if (v.get("date") or "") >= cutoff}
    store.update(
        schema=SCHEMA,
        entries=entries,
        scanned_days=sorted(d for d in scanned if d >= cutoff),
    )
    if pages_ok:
        store["last_success_at"] = now.isoformat(timespec="seconds")

    notices = build_notices(entries, holders)
    out = {
        "schema": SCHEMA,
        "updated_at": dt.datetime.now(SYD).isoformat(timespec="seconds"),
        "last_success_at": store.get("last_success_at"),
        "scanned_days": store["scanned_days"],
        "last_run": {
            "ok": pages_ok > 0,
            "list_pages_ok": pages_ok,
            "substantial_found": found,
            "pdf_checked": checked,
            "skipped_noise": skipped,
            "deferred": deferred,
            "errors": errors[:10],
        },
        "watchlist": [h.name for h in holders],
        "notices": notices,
    }
    write_json(STORE_FILE, store)
    write_json(NOTICES_FILE, out, pretty=True)
    if LEGACY_SEEN_FILE.exists():
        LEGACY_SEEN_FILE.unlink()
    log(f"完成：下载 {checked} 份 PDF，跳过噪音 {skipped} 份，关注名单命中 {len(notices)} 条")
    if not pages_ok:
        log("两个公告列表都没读到，本次运行标记为失败")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
