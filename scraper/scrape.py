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
import shutil
import subprocess
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
# ASX 网站自己用的全市场公告接口（Markit Digital 提供）：按时间倒序，最多能翻约 1 万条（大约 2–3 周）。
# 用来补回没点按钮那几天的公告。接口没有公开文档，读不到时只记提醒、不影响列表页的抓取。
MARKET_API = "https://asx.api.markitdigital.com/asx-research/1.0/markets/announcements"
FILE_API = "https://asx.api.markitdigital.com/asx-research/1.0/file/"
API_PAGE_SIZE = 1000
API_MAX_PAGES = int(os.environ.get("API_MAX_PAGES", "12"))
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "16"))   # 网页最多看 14 天，多补两天
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
PARSER_VERSION = 6   # 解析规则改进后加 1，已解析的旧记录会在下次运行时按新规则重读一次
OCR_DPI = 400                                             # 扫描件按 400dpi 渲染再识别（300dpi 会把 Name 认成 tame）
OCR_PAGES = 3                                             # 扫描件可能第一页是封面信，多看一页
OCR_TIMEOUT = 90                                          # 单页识别超时（秒）
OCR_MAX_SIDE = 5000                                       # 渲染图片最长边像素上限，防止页面尺寸异常的 PDF 撑爆内存
MAX_OCR = int(os.environ.get("MAX_OCR", "60"))            # 单次运行最多识别多少份扫描件，其余留到下次
OCR_TIME_BUDGET = float(os.environ.get("OCR_TIME_BUDGET", "720"))   # 单次运行文字识别总时长上限（秒），工作流 30 分钟超时
DAY_COMPLETE_AT = "21:00"   # 悉尼时间。ASX 晚上 8 点多还会发公告，这之后读到的“今日公告”才算完整

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


class BadPDF(FetchError):
    """PDF 打不开，多半是下载不完整，下次运行重试。"""


class Deferred(Exception):
    """本次运行的 OCR 名额或时间用完，留到下次，不算失败。"""


class OCREnvError(Exception):
    """tesseract 本身出错（多半是没装好），不是这份 PDF 的问题；OCR 恢复后会重读。"""


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
                last = f"HTTP {r.status_code}"
                if r.status_code in (404, 410):
                    break            # 刚发布的公告偶尔还没生成 PDF，不立刻重试，留给下次运行（最多 3 次）
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


TARGET_RE = re.compile(r"\bfor\s+([A-Z0-9]{3,6})\b")   # 区分大小写，避免把 “for the” 当成代码


def headline_target(headline: str, code: str) -> str:
    """上市公司自己申报时，ASX 会把公告挂在申报方自己的代码下，标题写成 “... for EOL”。
    返回真正的标的代码；标题里没有、或者就是本身代码时返回空串。"""
    m = TARGET_RE.search(headline or "")
    return m.group(1) if m and m.group(1) != code else ""


API_TYPES = {"becoming a substantial holder": "becoming", "change in substantial holding": "change",
             "ceasing to be a substantial holder": "ceasing"}


def market_rows(now: dt.datetime) -> tuple[list[dict], dict]:
    """从全市场公告接口往前翻，挑出大股东公告。返回 (公告列表, 覆盖信息)。
    覆盖信息：oldest = 翻到的最早一条的悉尼时间；reached = 是否已经翻过补抓窗口的起点；
    days = 翻到的所有公告日期（不只大股东公告，用来判断哪些交易日有数据）。"""
    window_start = (now - dt.timedelta(days=BACKFILL_DAYS)).date()
    rows, days, oldest, reached, warning = [], set(), None, False, ""
    for page in range(API_MAX_PAGES):
        try:
            r = fetch(f"{MARKET_API}?page={page}&itemsPerPage={API_PAGE_SIZE}", timeout=60)
            try:
                items = ((json.loads(r.text) or {}).get("data") or {}).get("items") or []
            except (ValueError, AttributeError) as e:
                raise FetchError(f"接口返回的不是预期的 JSON（{e}）") from e
        except FetchError as e:
            if page == 0:
                raise
            warning = f"补抓只翻到第 {page} 页（{e}），更早的下次再补"     # 前面几页已经拿到的照样用
            break
        if not items:
            break
        for it in items:
            try:
                t = dt.datetime.fromisoformat(str(it.get("date", "")).replace("Z", "+00:00")).astimezone(SYD)
            except ValueError:
                continue
            oldest = t
            days.add(t.date().isoformat())
            headline = (it.get("headline") or "").strip()
            key = (it.get("documentKey") or "").strip()
            code = (it.get("symbol") or "").strip().upper()
            if not (SUBSTANTIAL_RE.search(headline) and key and re.fullmatch(r"[A-Z0-9]{3,6}", code)
                    and re.fullmatch(r"[\w.\-]+", key)):
                continue
            types = [API_TYPES.get(str(x).strip().lower()) for x in (it.get("announcementTypes") or [])]
            rows.append({
                "id": "mk-" + key, "code": code, "date": t.date().isoformat(), "time": t.strftime("%H:%M"),
                "headline": headline, "url": FILE_API + key, "pages": None,
                "price_sensitive": bool(it.get("isPriceSensitive")), "source": "api",
                "api_type": next((x for x in types if x), ""),
            })
        if oldest and oldest.date() < window_start:
            reached = True
            break
        time.sleep(PAUSE)
    return rows, {"oldest": oldest, "reached": reached, "days": days, "warning": warning}


def dedupe_keys(e: dict) -> set:
    """同一份公告可能有两条发布记录：挂在申报方代码下（“... for LM1”）和挂在标的代码下（“... from FRS”），
    发布时间相同。用 (代码或标的代码, 日期, 时间) 判断是不是同一份。"""
    if not e.get("time"):
        return {("id", e.get("id"))}
    keys = {(e.get("code"), e.get("date"), e.get("time"))}
    t = headline_target(e.get("headline", ""), e.get("code", ""))
    if t:
        keys.add((t, e.get("date"), e.get("time")))
    return keys


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
        raise BadPDF(f"PDF 打不开（{type(e).__name__}: {e}）") from e
    out = []
    for mode in ("layout", "plain"):
        parts = []
        for p in plist:
            try:
                parts.append((p.extract_text(extraction_mode=mode) if mode == "layout" else p.extract_text()) or "")
            except Exception as e:  # noqa: BLE001
                log(f"    PDF {mode} 模式提取失败: {e}")
        text = clean_text("\n".join(parts))
        if text and text not in out:
            out.append(text)
    return out


def clean_text(text: str) -> str:
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    return "\n".join(ln.strip() for ln in text.split("\n") if ln.strip() and not WATERMARK_RE.match(ln.strip()))


_ocr_ok = None


def ocr_available() -> bool:
    """需要 tesseract 程序、pypdfium2 和 Pillow（GitHub Actions 里由工作流安装）。缺了就跳过 OCR，不报错。"""
    global _ocr_ok
    if _ocr_ok is None:
        try:
            import PIL  # noqa: F401  pypdfium2 渲染成图片要用
            import pypdfium2  # noqa: F401
            _ocr_ok = shutil.which("tesseract") is not None
        except ImportError:
            _ocr_ok = False
        if not _ocr_ok:
            log("提示：没有找到 tesseract、pypdfium2 或 Pillow，扫描件不做文字识别")
    return _ocr_ok


def ocr_texts(data: bytes, pages: int = OCR_PAGES) -> list[str]:
    """扫描件：把前几页渲染成图片，用 tesseract 识别文字。返回 [文字] 或 []。
    每一页 tesseract 都报错时抛 OCREnvError（环境问题，不是这份 PDF 的问题）。"""
    import pypdfium2 as pdfium
    try:
        doc = pdfium.PdfDocument(data)
    except Exception as e:  # noqa: BLE001  pypdf 能打开而这里打不开：多半是加了密码，重试也没用
        log(f"    扫描件渲染失败（{type(e).__name__}: {e}），按读不出处理")
        return []
    parts, failed, tried = [], 0, 0
    try:
        for i in range(min(pages, len(doc))):
            page = doc[i]
            w, h = page.get_size()                                   # 单位：点（1/72 英寸）
            scale = min(OCR_DPI / 72, OCR_MAX_SIDE / max(w, h, 1))
            img = page.render(scale=scale).to_pil().convert("L")
            buf = io.BytesIO()
            img.save(buf, "PNG")
            tried += 1
            try:
                r = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "6", "-l", "eng"],
                                   input=buf.getvalue(), capture_output=True, timeout=OCR_TIMEOUT)
            except subprocess.TimeoutExpired:
                log(f"    第 {i + 1} 页文字识别超时，跳过")
                continue
            except OSError as e:
                failed += 1
                log(f"    第 {i + 1} 页文字识别失败：{e}")
                continue
            if r.returncode != 0:
                failed += 1
                log(f"    第 {i + 1} 页文字识别失败：{r.stderr.decode('utf-8', 'replace')[:200]}")
                continue
            parts.append(r.stdout.decode("utf-8", "replace"))
    finally:
        doc.close()
    if tried and failed == tried:
        raise OCREnvError("tesseract 每一页都报错")
    text = clean_text("\n".join(parts))
    return [text] if len(re.findall(r"[A-Za-z]", text)) >= 40 else []


def pdf_text(data: bytes, pages: int = PDF_PAGES) -> str:
    """调试用：返回第一种读得出来的文字。"""
    texts = pdf_texts(data, pages)
    return texts[0] if texts else ""


SECTION1_RE = re.compile(
    r"Details\s+of\s+substantial\s+(?:share)?holder(?:\s*\(\s*1\s*\))?"
    r"|substantial\s+holder\s*\(\s*1\s*\)"
    # 扫描件常把 “holder” 识别到下一行或认错；这一支也要吃掉 shareholder/holder，否则会把它当成名称
    r"|(?:^|\n)\s*1\s*\.\s*Details\s+of\s+substantial(?!\s+holding)(?:\s+(?:share)?holder\b)?(?:\s*\(\s*1\s*\))?",
    re.I)
COMPANY_RE = re.compile(
    r"(?:To\s*:?\s*)?Company(?:\s+Name)?\s*/\s*(?:regi\w*\s*scheme\s*/\s*notified\s*foreign\s*passport(?:\s+fund\s*n\s*ame)?|Scheme)"
    r"\s*[:\-]?[ \t]*(.*)", re.I)
TO_LINE_RE = re.compile(r"^To\s*:\s*(\S.*)$", re.I | re.M)      # 自制表格：“To: HELLOWORLD TRAVEL LIMITED”
SECTION2_RE = re.compile(r"\n\s*2\s*\.\s+\S"                                   # 第二节标题
                         r"|\n[^\n\w]{0,4}(?:\w{1,2}\)\s*)?2\s*[.,]\s*(?:Details\s+of\s+voting|P\w{3,5}ous\s+and\s+present)", re.I)  # 扫描件：“> 2. …”“aD) 2, Provious …”
LABEL_RE = re.compile(
    r"^(?:the\s+(?:holder|previous)\b|there\s+was\b|acn\b|arsn\b|abn\b|nfpfrn\b|apfrn\b|name\b|details\b|previous\b|(?:share)?holder\b|[(\d])"
    r"|substantial\s+holder|if\s+applicable|notice\s+was|^not\s+applicable$"   # NFPFRN：2018 版表格的栏目名
    r"|^(?:see|refer(?:\s+to)?|as\s+(?:per|set\s+out\s+in))\b.{0,20}annexure|^annexure\s+[a-z0-9]\b"   # “See Annexure A”
    r"|passport\s+fund|scheme\s*/\s*notified|registered\s+scheme",
    re.I,
)
NAME_LINE_RE = re.compile(r"^(?:\(\s*1\s*\)\s*)?Name\b\s*(?:\(if applicable\))?\s*[:\-]?\s*(.*)$", re.I)
# OCR 常把扫描件左边缘的 “Name” 认成 “tame”“Narne”“m==Name”
NAME_LINE_OCR_RE = re.compile(r"^\W{0,4}(?:[A-Za-z]{0,2}\W{0,2})?(?:[NnTt]a(?:m|rn)e|Nome)\b\s*[:\-]?\s*(.*)$")
ID_TAIL_RE = re.compile(r"\s*(?:\b(?:ACN|ARSN|ABN)\b|\(if applicable\)).*$", re.I)


def parse_company(text: str) -> str:
    m = COMPANY_RE.search(text)
    if m:
        val = m.group(1).strip()
        if len(val) < 2:
            val = text[m.end():].lstrip("\n").split("\n", 1)[0].strip()
    else:
        s1 = SECTION1_RE.search(text)
        m = TO_LINE_RE.search(text, 0, s1.start() if s1 else len(text))
        if not m:
            return ""
        val = m.group(1)
    return ID_TAIL_RE.sub("", val).strip(" :-")[:120]


def clean_company(s: str) -> str:
    """网页显示用：去掉 “(ASX: EOL)”、“(Zenith)”、“NWH.AX” 这类尾巴，以及扫描件识别出的开头杂字符。"""
    s = re.sub(r"^[^A-Za-z0-9]+", "", (s or "").strip())
    s = re.sub(r"\s+[A-Z0-9]{3,6}\.AX\.?$", "", s)
    s = re.sub(r"\s*\([^()]*\)?\s*$", "", s)        # 末尾括号（包括被截断、没有右括号的）
    return s.strip(" ,;:-") or (s or "")


def clean_name(s: str) -> str:
    s = re.sub(r"^[^\w(“\"']+", "", s or "")          # 扫描件识别出的开头杂字符：“> ”“| ”
    return ID_TAIL_RE.sub("", s).strip(" :-")


def looks_like_name(s: str) -> bool:
    """排除表格标签（The holder became…、ACN/ARSN…）和纯数字，避免把标签当成申报方名称。"""
    s = s.strip()
    return 3 <= len(s) <= 150 and len(re.findall(r"[A-Za-z]", s)) >= 3 and not LABEL_RE.search(s)


def parse_holder_section(text: str, first_line: bool = True, ocr: bool = False) -> tuple[str, str]:
    """返回 (申报方名称, 第一节原文)。名称解析不出来时，靠第一节原文做中等可信度匹配。"""
    m = SECTION1_RE.search(text)
    if not m:
        return "", ""
    head_rest = text[m.end():].split("\n", 1)[0].strip()   # 扫描件常把名称识别到标题同一行
    region = text[m.end(): m.end() + 700]
    s2 = SECTION2_RE.search(region)
    if s2:
        region = region[: s2.start()]          # 只看第一节，避免读到后面提到的目标公司
    lines = [ln.strip() for ln in region.split("\n") if ln.strip() and not COMPANY_RE.search(ln)]
    region = "\n".join(lines)
    for i, line in enumerate(lines):
        nm = NAME_LINE_RE.match(line) or (NAME_LINE_OCR_RE.match(line) if ocr else None)
        if not nm:
            continue
        # 名称可能在同一行，也可能因为 PDF 按列排版被挤到后面几行，或者被识别到标题那一行
        for cand in [nm.group(1)] + lines[i + 1: i + 5] + [head_rest]:
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
SEC2_RE = re.compile(r"(?:^|\n)\s*2\s*\.?\s*(?:Details\s+of\s+voting\s+power|Previous\s+and\s+present\s+voting\s+power)", re.I)
SEC3_RE = re.compile(r"\n\s*3\s*\.\s*[A-Za-z]")
PCT_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,8})?)\s?%")              # 有的写到小数点后 6 位：5.298524%
BARE_PCT_RE = re.compile(r"(?<![\d.,$])(\d{1,3}\.\d{1,8})(?![\d,%]|\.\d)")  # 表头写 “Voting power (%)”，数字不带 %
TOTAL_RE = re.compile(r"\btotal\b", re.I)
EVENT_RE = re.compile(                                  # 词间用 \s*：扫描件识别常丢空格（“ofthe”）
    r"(?:became|ceased\s*to\s*be)\s*a\s*substantial\s*(?:share)?holder\s*on"
    r"|date\s*of\s*becoming\s*a\s*substantial\s*(?:share)?holder"
    r"|change\s*in\s*the\s*interests?\s*of\s*the"      # 604 常见排版：日期夹在“of the”和“substantial holder on”之间
    r"|interests?\s*of\s*the\s*substantial\s*holder\s*on"
    r"|substantial\s*(?:share)?holder\s*on",
    re.I,
)
DATE_NUM_RE = re.compile(r"(?<!\d)(\d{1,2})\s?[/.\-]\s?(\d{1,2})\s?[/.\-]\s?(\d{4}|\d{2})(?!\d)")   # 兼容“29 /10/2026”这种多了空格的写法
DATE_SP_RE = re.compile(r"(?<![\d/])(\d{1,2})\s+(\d{1,2})\s+(20\d{2})(?!\d)")     # 方格填写：“28 10 2026”
DATE_8_RE = re.compile(r"(?<!\d)(\d{2})(\d{2})(20\d{2})(?!\d)")                   # 方格填写：“28102026”
DATE_TXT_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?[\s\-/.,]+([A-Za-z]{3,9})\.?[\s,\-/.]+(\d{4})")   # 28 Oct 2026、28/October/2026
DATE_US_RE = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})")              # October 28, 2026（美式）


def plausible(d: dt.date) -> bool:
    """扫描件会把 2026 认成 2626；离谱的年份不要。"""
    return 2000 <= d.year <= dt.date.today().year + 1


def _num_date(d: int, mo: int, y: int) -> dt.date | None:
    y = y + 2000 if y < 100 else y
    if mo > 12 >= d:
        d, mo = mo, d                      # 美式 月/日/年（Morgan Stanley 等）
    try:
        got = dt.date(y, mo, d)
    except ValueError:
        return None
    return got if plausible(got) else None


def _txt_date(day: str, month: str, year: str) -> dt.date | None:
    month = month[:3] if month.lower().startswith("sept") else month
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            got = dt.datetime.strptime(f"{day} {month} {year}", fmt).date()
            return got if plausible(got) else None
        except ValueError:
            continue
    return None


def parse_date(seg: str) -> str:
    """在一小段文字里找最先出现的、合理的日期。支持 28/10/2026、28 Oct 2026、28/October/2026、
    October 28, 2026、方格填写的 “28 10 2026” 和 “28102026”。"""
    found = []
    for rx, conv in ((DATE_NUM_RE, lambda m: _num_date(*(int(x) for x in m.groups()))),
                     (DATE_SP_RE, lambda m: _num_date(*(int(x) for x in m.groups()))),
                     (DATE_8_RE, lambda m: _num_date(*(int(x) for x in m.groups()))),
                     (DATE_TXT_RE, lambda m: _txt_date(m.group(1), m.group(2), m.group(3))),
                     (DATE_US_RE, lambda m: _txt_date(m.group(2), m.group(1), m.group(3)))):
        for m in rx.finditer(seg):
            got = conv(m)
            if got:
                found.append((m.start(), got))
                break
    return min(found)[1].isoformat() if found else ""


def voting_section(text: str) -> tuple[str, bool]:
    """返回 (第二节“持股比例”原文, 是否确定截到了第三节之前)。"""
    m = SEC2_RE.search(text) or VP_RE.search(text)
    if not m:
        return "", False
    seg = text[m.start(): m.start() + 2500]
    s3 = SEC3_RE.search(seg, 1)
    return (seg[: s3.start()], True) if s3 else (seg[:1500], False)


def pct_rows(seg: str, allow_bare: bool = True) -> list[tuple[str, list[float]]]:
    bare = allow_bare and not PCT_RE.search(seg)
    rows = []
    for ln in seg.split("\n"):
        vals = [float(x) for x in (BARE_PCT_RE if bare else PCT_RE).findall(ln)]
        vals = [v for v in vals if 0 < v <= 100]
        if vals:
            rows.append((ln, vals))
    return rows


def pick_voting_power(seg: str, ntype: str, bounded: bool, ocr: bool = False) -> list[float]:
    """持股比例表可能分好几行（不同实体或类别）再加一行合计，例如
    0.06% / 4.94% / GRAND TOTAL 5.00%。优先用合计行；没有合计行时：
    变动类取比例最大的那一行（前后两个值），新进类取最大值。
    扫描件（ocr=True）的数字容易认错：只认带 % 的数字，且低于 5% 的整组不要（宁缺毋错）。"""
    vp = _pick(pct_rows(seg, allow_bare=not ocr), ntype, bounded, ocr)
    if ntype == "becoming" and vp and vp[0] < 4.95:
        return []              # 新进大股东必然 ≥5%，低于这个数一定是读错了行
    if ocr and any(v < 4.95 for v in vp):
        return []
    return vp


def _pick(rows: list[tuple[str, list[float]]], ntype: str, bounded: bool, ocr: bool = False) -> list[float]:
    if not rows:
        return []
    totals = [v for ln, v in rows if TOTAL_RE.search(ln)]
    flat = [x for _, v in rows for x in v]
    if ntype == "change":
        if totals and len(totals[-1]) >= 2:
            return totals[-1][:2]
        pairs = [v for _, v in rows if len(v) >= 2]
        if pairs:
            return max(pairs, key=lambda v: max(v[:2]))[:2]
        return [] if ocr else flat[:2]     # 扫描件不跨行拼数字（会把说明文字里的比例拼进来）
    if totals:
        return [totals[-1][-1]]
    return [max(flat)] if bounded else flat[:1]   # 没截到第三节时不取最大值，免得读到后面的 “100%”


def parse_details(text: str, ntype: str, ocr: bool = False) -> dict:
    out = {"change_date": "", "voting_power": []}
    m = EVENT_RE.search(text)
    if m:
        out["change_date"] = parse_date(text[m.end(): m.end() + 250])
    if ntype != "ceasing":
        seg, bounded = voting_section(text)
        out["voting_power"] = pick_voting_power(seg, ntype, bounded, ocr)
    return out


# 自制格式：没有 “1. Details of substantial holder” 标题，只有 “Name: …” 或 “Name of Substantial Holder: …”
ALT_NAME_RE = re.compile(r"^Name\s+of\s+(?:the\s+)?substantial\s+(?:share)?holder\s*[:\-]\s*(\S.*)$", re.I | re.M)
PLAIN_NAME_RE = re.compile(r"^Name\s*:\s*(\S.*)$", re.I | re.M)
FORM_HEADER_RE = re.compile(r"Form\s*60[345]\b|Notice\s+of\s+(?:initial\s+substantial|change\s+of\s+interests|ceasing\s+to\s+be)", re.I)


def fallback_name(text: str) -> str:
    """只在第二节（持股比例）之前找，免得读到签名栏的 “Name:”；普通 “Name:” 还要求出现在表格标题之后，
    免得读到封面信。"""
    end = SEC2_RE.search(text) or VP_RE.search(text)
    limit = end.start() if end else min(len(text), 2500)
    m = ALT_NAME_RE.search(text, 0, limit)
    if not m:
        head = FORM_HEADER_RE.search(text, 0, limit)
        m = PLAIN_NAME_RE.search(text, head.end(), limit) if head else None
    if m:
        val = clean_name(m.group(1))
        if looks_like_name(val):
            return val[:200]
    return ""


# 其他格式：新西兰双重上市公司用 FMC Act 表格，爱尔兰公司（James Hardie）用 TR-1 表格，美国公司 CDI 用 SEC 13G/13D
NZ_RE = re.compile(r"Substantial\s+product\s+holder\(s\)\s+giving\s+disclosure", re.I)
NZ_NAME_RE = re.compile(r"Full\s+name\(s\)\s*:\s*(.+)", re.I)
NZ_PCT_RE = re.compile(r"total\s+percentage\s+held\s+in\s+class\s*:\s*(\d{1,3}(?:\.\d+)?)\s*%", re.I)
NZ_DATE_RE = re.compile(r"Date\s+of\s+relevant\s+event\s*:\s*([^\n]+)", re.I)
NZ_COMPANY_RE = re.compile(r"\nand\s*\nTo\s+(.+)", re.I)
TR1_RE = re.compile(r"NOTIFICATION\s+OF\s+MAJOR\s+HOLDINGS", re.I)
TR1_NAME_RE = re.compile(r"person\s+subject\s+to\s+the\s+notification\s+obligation\w*\s*:?\s*\n?\s*Name\s*:\s*(.+?)"
                         r"(?:\s+City\s+and\s+country.*)?$", re.I | re.M)
TR1_RESULT_RE = re.compile(r"Resulting\s+situation[\s\S]{0,200}?(\d{1,3}(?:\.\d+)?)\s*%", re.I)
TR1_PREV_RE = re.compile(r"Position\s+of\s+previous[\s\S]{0,200}?(\d{1,3}(?:\.\d+)?)\s*%", re.I)
TR1_DATE_RE = re.compile(r"threshold\s+was\s+crossed\s+or\s+reached\w*\s*:?\s*([^\n]*\n?[^\n]*)", re.I)
TR1_COMPANY_RE = re.compile(r"Identity\s+of\s+the\s+issuer.*?:\s*\n?\s*([^\n]+)", re.I | re.S)
SEC13_RE = re.compile(r"SCHEDULE\s+13[GD]\b", re.I)
SEC13_NAME_RE = re.compile(r"Names?\s+of\s+Reporting\s+Persons?\s*\n\s*([^\n]+)", re.I)
SEC13_PCT_RE = re.compile(r"Percent\s+of\s+class\s+represented\s+by\s+amount\s+in\s+row\s*\(\s*\d+\s*\)\s*\n?\s*(\d{1,3}(?:\.\d+)?)\s*%", re.I)
SEC13_COMPANY_RE = re.compile(r"\n([^\n]+)\n\(Name\s+of\s+Issuer\)", re.I)
SEC13_DATE_RE = re.compile(r"\n([^\n]+)\n\(Date\s+of\s+Event", re.I)


def parse_other_forms(text: str, ntype: str) -> dict:
    """解析非澳洲 603/604/605 格式。认不出时返回空 dict。"""
    def vp(cur, prev):
        if ntype == "ceasing" or cur is None:
            return []
        return [prev, cur] if ntype == "change" and prev is not None else [cur]

    if NZ_RE.search(text):
        name = NZ_NAME_RE.search(text)
        pcts = [float(x) for x in NZ_PCT_RE.findall(text)]
        date = NZ_DATE_RE.search(text)
        comp = NZ_COMPANY_RE.search(text)
        return {"holder_name": clean_name(name.group(1))[:200] if name else "",
                "voting_power": vp(pcts[0] if pcts else None, pcts[1] if len(pcts) > 1 else None),
                "change_date": parse_date(date.group(1)) if date else "",
                "company": comp.group(1).strip()[:120] if comp else ""}
    if TR1_RE.search(text):
        name = TR1_NAME_RE.search(text)
        cur, prev = TR1_RESULT_RE.search(text), TR1_PREV_RE.search(text)
        date = TR1_DATE_RE.search(text)
        comp = TR1_COMPANY_RE.search(text)
        return {"holder_name": clean_name(name.group(1))[:200] if name else "",
                "voting_power": vp(float(cur.group(1)) if cur else None, float(prev.group(1)) if prev else None),
                "change_date": parse_date(date.group(1)) if date else "",
                "company": comp.group(1).strip()[:120] if comp else ""}
    if SEC13_RE.search(text):
        name, pct = SEC13_NAME_RE.search(text), SEC13_PCT_RE.search(text)
        date, comp = SEC13_DATE_RE.search(text), SEC13_COMPANY_RE.search(text)
        return {"holder_name": clean_name(name.group(1))[:200] if name else "",
                "voting_power": vp(float(pct.group(1)) if pct else None, None),
                "change_date": parse_date(date.group(1)) if date else "",
                "company": comp.group(1).strip()[:120] if comp else ""}
    return {}


FORM_TYPES = (
    (re.compile(r"\bForm\s*605\b|Notice\s+of\s+ceasing\s+to\s+be|ceasing\s+to\s+have\s+substantial\s+holding", re.I), "ceasing"),
    (re.compile(r"\bForm\s*604\b|Notice\s+of\s+change\s+of\s+interests|Movement\s+of\s+1%\s+or\s+more", re.I), "change"),
    (re.compile(r"\bForm\s*603\b|Notice\s+of\s+initial\s+substantial|beginning\s+to\s+have\s+substantial\s+holding", re.I), "becoming"),
)


def infer_type(text: str) -> str:
    """标题看不出类型（例如 “Substantial Holder Notice”）时，按 PDF 里最先出现的表格编号判断。"""
    hits = [(m.start(), t) for rx, t in FORM_TYPES for m in [rx.search(text)] if m]
    return min(hits)[1] if hits else ""


STRICT_FORM_RE = re.compile(r"^\s*Form\s*60([345])\s*$", re.I | re.M)


def strict_form_type(text: str) -> str:
    """PDF 开头单独一行写着 “Form 603/604/605” 时，以表格为准（标题偶尔写错，例如 604 表格配“退出”标题）。"""
    m = STRICT_FORM_RE.search(text[:1200])
    return {"3": "becoming", "4": "change", "5": "ceasing"}[m.group(1)] if m else ""


# ---------------------------------------------------------------- 关注名单与匹配

@dataclass
class Holder:
    name: str
    aliases: list
    hints: list
    group: str = "主动基金"


def is_abbrev(s: str) -> bool:
    return len(s) <= 6 and s.upper() == s and any(c.isalpha() for c in s)


def word_re(s: str, abbrev_case: bool = False) -> re.Pattern:
    """整词匹配。abbrev_case=True 时，全大写的缩写（MS、TOP、SOL）区分大小写，
    免得 “from Ms J Smith” 被当成 Morgan Stanley、“Top End Pty Ltd” 被当成 Thorney。"""
    s = s.strip()
    flags = 0 if abbrev_case and is_abbrev(s) else re.I
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(s) + r"(?![A-Za-z0-9])", flags)


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
        holders.append(Holder(h["name"], aliases, [word_re(x, True) for x in h.get("hints", []) if x and x.strip()],
                              h.get("group") or "主动基金"))
    noise = [word_re(x, True) for x in raw.get("noise_hints", []) if x and x.strip()]
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
        if not e.get("ocr"):
            return None, ""      # 名称解析出来了但不在名单里，不再用其他线索猜（扫描件的名称可能认错，继续往下看）
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
        if hint_hit(h, hint):
            return h.name, "低"
    return None, ""


def hint_hit(h: Holder, hint: str) -> bool:
    return any_hit(h.hints, hint) or any_hit(h.aliases, hint)


def is_noise(hint: str, holders: list[Holder], noise: list[re.Pattern]) -> bool:
    if not any_hit(noise, hint):
        return False
    return not any(hint_hit(h, hint) for h in holders)


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
# code：公告涉及的股票；lodged_code：公告实际挂在哪个代码下（申报方自己是上市公司时才有）


def build_notices(entries: dict, holders: list[Holder]) -> list[dict]:
    out = []
    group_of = {h.name: h.group for h in holders}
    for e in entries.values():
        holder, conf = match_entry(e, holders)
        if holder:
            item = {k: e.get(k) for k in PUBLIC_FIELDS}
            target = headline_target(e.get("headline", ""), e.get("code", ""))
            item["code"], item["lodged_code"] = (target, e.get("code", "")) if target else (e.get("code", ""), "")
            item["company"] = clean_company(item.get("company") or "")
            item["url"] = safe_url(item.get("url") or "")
            item["pdf_url"] = safe_url(item.get("pdf_url") or "") or item["url"]
            item.update(holder=holder, group=group_of.get(holder, ""), confidence=conf, ocr=bool(e.get("ocr")))
            out.append(item)
    out.sort(key=lambda n: (n.get("date") or "", n.get("time") or "", n.get("id") or ""), reverse=True)
    return out


# ---------------------------------------------------------------- 主流程

def analyse_texts(texts: list[str], ntype: str, ocr: bool = False) -> dict:
    """在多种提取结果里，每个字段取第一个解析得出的值。"""
    out = {"company": "", "holder_name": "", "region": "", "change_date": "", "voting_power": []}
    regions = []
    want_vp = 2 if ntype == "change" else 1
    for t in texts:
        name, region = parse_holder_section(t, first_line=False, ocr=ocr)   # 第一轮只认“Name”标签
        if region:
            regions.append(region[:500])
        out["holder_name"] = out["holder_name"] or name
        out["company"] = out["company"] or parse_company(t)
        d = parse_details(t, ntype, ocr)
        out["change_date"] = out["change_date"] or d["change_date"]
        if len(d["voting_power"]) > len(out["voting_power"]) and len(out["voting_power"]) < want_vp:
            out["voting_power"] = d["voting_power"]
    if not out["holder_name"]:
        # 第二轮：没有“Name”标签的自定义格式，取第一节标题下的第一行。
        # 放在第二轮，是因为版面模式偶尔把“Name”拆成“Nam e”，首行会是一行乱码，不能抢在正确结果前面。
        for t in texts:
            out["holder_name"] = parse_holder_section(t, ocr=ocr)[0]
            if out["holder_name"]:
                break
    if not out["holder_name"]:
        for t in texts:                                  # 自制格式：“Name: …” / “Name of Substantial Holder: …”
            out["holder_name"] = fallback_name(t)
            if out["holder_name"]:
                break
    if not out["holder_name"] and not regions:
        for t in texts:                                  # 新西兰 / TR-1 / 13G 等其他格式
            alt = parse_other_forms(t, ntype)
            if alt:
                for k, v in alt.items():
                    out[k] = out[k] or v
                break
    if not out["holder_name"] or ocr:
        out["region"] = "\n".join(regions)[:1000]   # 名称解析不出来（或是扫描件、名称可能认错）时，留原文做中等可信度匹配
    return out


class OCRBudget:
    """单次运行的 OCR 名额和总时长。"""

    def __init__(self, count: int = None, seconds: float = None):
        self.left = MAX_OCR if count is None else count
        self.deadline = time.monotonic() + (OCR_TIME_BUDGET if seconds is None else seconds)
        self.used = 0

    def take(self) -> None:
        if self.left <= 0 or time.monotonic() > self.deadline:
            raise Deferred()
        self.left -= 1
        self.used += 1


def blank(result: dict | None) -> bool:
    """什么都没认出来：没有申报方名称，也没找到第一节。"""
    return result is None or not (result.get("holder_name") or result.get("region"))


def process_entry(e: dict, ocr_budget: OCRBudget | None = None) -> None:
    """下载并解析一份公告。文字层什么都认不出来时（扫描件，或“打字封面信 + 扫描表格”），
    用 OCR 再读一遍。ocr_budget 为 None 时不做 OCR；名额或时间用完时抛 Deferred。"""
    data, pdf_url = download_pdf(e["url"])
    e["pdf_url"] = safe_url(pdf_url)
    texts = pdf_texts(data)
    ntype = e.get("type", "other")
    if texts:
        ntype = strict_form_type(texts[0]) or (ntype if ntype != "other" else (infer_type(texts[0]) or "other"))
    result = analyse_texts(texts, ntype) if texts else None
    e["ocr"] = e["ocr_tried"] = False
    # 文字层什么都认不出（扫描件、封面信 + 扫描表格），或者认不出申报方（填写内容是贴上去的图片），再用 OCR 读
    if (blank(result) or not result["holder_name"]) and ocr_budget is not None and ocr_available():
        ocr_budget.take()
        try:
            otexts = ocr_texts(data)
            e["ocr_tried"] = True
        except OCREnvError as ex:
            log(f"    文字识别环境出错（{ex}），OCR 恢复后会重读")
            otexts = []
        if otexts:
            otype = strict_form_type(otexts[0]) or (ntype if ntype != "other" else (infer_type(otexts[0]) or "other"))
            oresult = analyse_texts(otexts, otype, ocr=True)
            if result is None or (blank(result) and not blank(oresult)):
                result, ntype, e["ocr"] = oresult, otype, True
            elif not result["holder_name"] and oresult["holder_name"]:
                merged = dict(result, holder_name=oresult["holder_name"])   # 文字层的数字更可靠，缺的才用 OCR 补
                for k in ("company", "change_date", "region"):
                    merged[k] = merged[k] or oresult[k]
                merged["voting_power"] = merged["voting_power"] or oresult["voting_power"]
                result, e["ocr"] = merged, True
    e["type"] = ntype
    if result is None:
        e["status"], e["parser"] = "unreadable", PARSER_VERSION
        return
    e.update(result)
    e["status"], e["parser"] = "parsed", PARSER_VERSION


def hinted(e: dict, holders: list[Holder]) -> bool:
    return any(hint_hit(h, e.get("hint", "")) for h in holders)


def now_syd() -> dt.datetime:
    return dt.datetime.now(SYD)


def mark_days(days: set, today: dt.date, hhmm: str, scanned: set, partial: dict) -> None:
    """记录覆盖到的日子。今天在晚上 9 点前读到的只算一部分（之后还会发公告）；之前的日子都算完整。
    （以前按“今日公告页”整页判断，周末打开时会把周五误记成只抓到一部分。）"""
    today_iso = today.isoformat()
    for d in days:
        if d == today_iso and hhmm < DAY_COMPLETE_AT:
            if d not in scanned:
                partial[d] = hhmm
        elif d <= today_iso:
            scanned.add(d)


def main() -> int:
    now = now_syd()
    today = now.date()
    holders, noise = load_watchlist()
    store = load_store()
    entries: dict = store["entries"]
    scanned = set(store.get("scanned_days", []))           # 完整抓到的日子
    partial = dict(store.get("partial_days", {}) or {})    # 只抓到一部分的日子：{日期: 抓取时间}
    hhmm = now.strftime("%H:%M")
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
        mark_days({r["date"] for r in got}, today, hhmm, scanned, partial)

    backfill = {"ok": False, "from": None, "added": 0}
    try:
        api_rows, cov = market_rows(now)
        rows.extend(api_rows)
        backfill["ok"] = True
        if cov["oldest"]:
            # 接口按时间倒序，翻页在最早那天的中间停下，那天只拿到一部分；从第二天起才是完整的
            first_full = cov["oldest"].date() + dt.timedelta(days=1)
            backfill["from"] = first_full.isoformat()
            d = first_full
            covered = set()
            while d <= today:
                if d.weekday() < 5 or d.isoformat() in cov["days"]:
                    covered.add(d.isoformat())
                d += dt.timedelta(days=1)
            mark_days(covered, today, hhmm, scanned, partial)
        if cov["warning"]:
            errors.append(cov["warning"])
        log(f"全市场接口：{len(api_rows)} 条大股东公告，覆盖到 {backfill['from']}")
    except FetchError as e:
        errors.append(f"补抓接口读取失败（只影响补回之前漏掉的日子）：{e}")
        log(errors[-1])
    partial = {d: t for d, t in partial.items() if d not in scanned}

    seen_keys = {}
    for e in entries.values():
        for k in dedupe_keys(e):
            seen_keys.setdefault(k, e["id"])
    found_ids = set()
    for r in rows:                     # 列表页在前、接口在后：同一份公告优先保留列表页那条
        if not SUBSTANTIAL_RE.search(r["headline"]):
            continue
        keys = dedupe_keys(r)
        same = next((seen_keys[k] for k in keys if k in seen_keys), None)
        found_ids.add(same or r["id"])
        if same or r["id"] in entries:
            continue
        ntype = classify(r["headline"])
        if ntype == "other" and r.get("api_type"):
            ntype = r["api_type"]
        entries[r["id"]] = {
            **{k: v for k, v in r.items() if k != "api_type"}, "type": ntype, "hint": headline_hint(r["headline"]),
            "company": "", "holder_name": "", "region": "", "change_date": "", "voting_power": [],
            "pdf_url": "", "status": "new", "attempts": 0,
        }
        if r.get("source") == "api":
            backfill["added"] += 1
        for k in keys:
            seen_keys.setdefault(k, r["id"])
    found = len(found_ids)
    log(f"大股东相关公告 {found} 条")

    for e in entries.values():
        if e.get("status") in ("parsed", "unreadable") and e.get("parser", 1) < PARSER_VERSION:
            e["status"], e["attempts"] = "new", 0      # 用新的解析规则重读一次
        elif e.get("status") == "noise" and not is_noise(e.get("hint", ""), holders, noise):
            e["status"], e["attempts"] = "new", 0      # 改了名单后不再算噪音的，补下载
        elif (e.get("status") in ("parsed", "unreadable") and not e.get("ocr_tried")
              and (e.get("status") == "unreadable" or not e.get("holder_name")) and ocr_available()):
            e["status"], e["attempts"] = "new", 0      # 之前没做成 OCR（例如那次 tesseract 没装上），现在补上
    todo = [e for e in entries.values()
            if e.get("status") in ("new", "error") and e.get("attempts", 0) < MAX_ATTEMPTS]
    todo.sort(key=lambda e: (e.get("date", ""), e.get("time", "")), reverse=True)   # 新的优先
    todo.sort(key=lambda e: not hinted(e, holders))                                   # 标题带关注名单缩写的更优先

    checked = skipped = deferred = failed = 0
    ocr_budget = OCRBudget()
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
            process_entry(e, ocr_budget)
        except Deferred:
            e["attempts"] -= 1                   # 不算一次失败，下次运行再识别
            checked -= 1
            deferred += 1
            continue
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
        log(f"本次已达下载或文字识别上限，剩余 {deferred} 份留到下次运行")
    log(f"文字识别（OCR）{ocr_budget.used} 份扫描件")

    cutoff = (today - dt.timedelta(days=KEEP_DAYS)).isoformat()
    entries = {k: v for k, v in entries.items() if (v.get("date") or "") >= cutoff}
    store.update(
        schema=SCHEMA,
        entries=entries,
        scanned_days=sorted(d for d in scanned if d >= cutoff),
        partial_days={d: t for d, t in sorted(partial.items()) if d >= cutoff},
    )
    if pages_ok:
        store["last_success_at"] = now.isoformat(timespec="seconds")

    notices = build_notices(entries, holders)
    out = {
        "schema": SCHEMA,
        "updated_at": dt.datetime.now(SYD).isoformat(timespec="seconds"),
        "last_success_at": store.get("last_success_at"),
        "scanned_days": sorted(set(store["scanned_days"]) | set(store["partial_days"])),
        "partial_days": store["partial_days"],
        "last_run": {
            "ok": pages_ok > 0,
            "list_pages_ok": pages_ok,
            "substantial_found": found,
            "pdf_checked": checked,
            "skipped_noise": skipped,
            "deferred": deferred,
            "backfill": backfill,
            "errors": errors[:10],
        },
        "watchlist": [h.name for h in holders],
        "groups": {h.name: h.group for h in holders},
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
