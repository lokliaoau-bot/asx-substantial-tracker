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
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).resolve().parent))
import insight  # noqa: E402  操作解读：交易明细、股价背景、判断规则

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
PARSER_VERSION = 8   # 解析规则改进后加 1，已解析的旧记录会在下次运行时按新规则重读一次
OCR_DPI = 400                                             # 扫描件按 400dpi 渲染再识别（300dpi 会把 Name 认成 tame）
OCR_PAGES = 3                                             # 扫描件可能第一页是封面信，多看一页
OCR_TIMEOUT = 90                                          # 单页识别超时（秒）
OCR_MAX_SIDE = 5000                                       # 渲染图片最长边像素上限，防止页面尺寸异常的 PDF 撑爆内存
MAX_OCR = int(os.environ.get("MAX_OCR", "60"))            # 单次运行最多识别多少份扫描件，其余留到下次
OCR_TIME_BUDGET = float(os.environ.get("OCR_TIME_BUDGET", "720"))   # 单次运行文字识别总时长上限（秒），工作流 30 分钟超时
DAY_COMPLETE_AT = "21:00"   # 悉尼时间。ASX 晚上 8 点多还会发公告，这之后读到的“今日公告”才算完整
WORKERS = int(os.environ.get("WORKERS", "4"))             # 同时下载、解析几份 PDF（GitHub 的机器是 4 核）
API_OVERLAP_HOURS = 6                                     # 增量补抓时，往上次同步点之前多翻几个小时，防止接口里顺序略有错位
API_FULL_EVERY_DAYS = 7                                   # 每隔几天把整个补抓窗口重新翻一遍，查漏补缺
INSIGHT_VERSION = 5                                       # 交易明细的读法改进后加 1，命中名单的旧公告会重新下载读一次明细
TRADE_PAGES = int(os.environ.get("TRADE_PAGES", "40"))    # 交易明细最多读前多少页（附件可能很长）
TRADE_SECONDS = 25                                        # 一份公告读明细最多花多少秒
DETAIL_TRIES = 3                                          # 明细读取失败最多重试几次
COMPANY_ANNS = BASE + "/asx/v2/statistics/announcements.do?by=asxCode&asxCode={code}&timeframe=D&period=M6"   # 这家公司近 6 个月的公告
COMPANY_HEADER = "https://asx.api.markitdigital.com/asx-research/1.0/companies/{code}/header"                  # 行业、市值
PRICE_API = BASE + "/asx/1/chart/highcharts?asx_code={code}&years=2"   # ASX 日线：[[毫秒, 收盘价, 成交量], ...]

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
NOTICES_FILE = DATA_DIR / "notices.json"      # 网页读取（只含关注名单的记录）
STORE_FILE = DATA_DIR / "all_notices.json"    # 内部存储（全部大股东公告）
LEGACY_SEEN_FILE = DATA_DIR / "seen.json"     # 第一版的文件，迁移后删除
HISTORY_FILE = DATA_DIR / "history.json"      # 命中名单的公告要点，长期保存（算买入成本用），不随 KEEP_DAYS 清掉
COMPANY_YEAR = BASE + "/asx/v2/statistics/announcements.do?by=asxCode&asxCode={code}&timeframe=Y&year={year}"   # 某公司某一年的全部公告
BACKFILL_PDFS = int(os.environ.get("BACKFILL_PDFS", "150"))      # 回补持仓历史：单次运行最多读多少份旧公告
BACKFILL_SECONDS = float(os.environ.get("BACKFILL_SECONDS", "480"))
CALIBRATION_FILE = DATA_DIR / "calibration.json"
INDEX_CODE = "XJO"                                   # 大盘：S&P/ASX 200（同一个日线接口，行是 [时间, 开, 高, 低, 收, 量]）
REPORTS_FILE = DATA_DIR / "fund_reports.json"        # 名单里有上市产品的基金，它们月报的正文（找“基金自己怎么说这只股票”）
REPORT_RE = re.compile(r"investment\s+update|monthly\s+report|unitholder\s+monthly", re.I)
REPORT_KEEP_DAYS = 200
REPORT_MAX_NEW = int(os.environ.get("REPORT_MAX_NEW", "30"))
REPORT_CHARS = 14000
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "1100"))   # 持仓历史留 3 年
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

    # 周末和公众假期页面写的是 “No company announcements have been published by ASX on …”：认得出，只是当天没有公告
    recognised = bool(rows) or bool(re.search(r"ASX\s*Code|no\s+(?:company\s+)?announcements", html, re.I))
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


def market_rows(now: dt.datetime, since: dt.datetime | None = None) -> tuple[list[dict], dict]:
    """从全市场公告接口往前翻，挑出大股东公告。返回 (公告列表, 覆盖信息)。
    since：上次已经完整同步到的时间点。给了就只翻到它之前几个小时为止（增量），不给就翻完整个补抓窗口。
    覆盖信息：oldest = 翻到的最早一条的悉尼时间；newest = 最新一条的时间；reached = 是否翻到了预定的终点；
    days = 翻到的所有公告日期（不只大股东公告，用来判断哪些交易日有数据）。"""
    window_start = (now - dt.timedelta(days=BACKFILL_DAYS)).date()
    stop_at = since - dt.timedelta(hours=API_OVERLAP_HOURS) if since else None
    rows, days, oldest, newest, reached, exhausted, warning = [], set(), None, None, False, False, ""
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
            exhausted = True
            break
        for it in items:
            try:
                t = dt.datetime.fromisoformat(str(it.get("date", "")).replace("Z", "+00:00")).astimezone(SYD)
            except ValueError:
                continue
            oldest = t
            newest = newest or t
            days.add(t.date().isoformat())
            if t.date() < window_start:
                continue                       # 窗口以外的不补：太旧的会被保留期清掉，下次又补，白白重复下载
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
        if oldest and (oldest.date() < window_start or (stop_at and oldest < stop_at)):
            reached = True
            break
        time.sleep(PAUSE)
    # complete：这次翻到的范围是连续完整的（翻到了终点，或者接口里已经没有更早的了），可以作为下次增量的起点
    return rows, {"oldest": oldest, "newest": newest, "reached": reached, "days": days, "warning": warning,
                  "incremental": bool(stop_at), "complete": bool(oldest) and not warning and (reached or exhausted)}


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
        # 接口下载的 PDF 多为 AES 加密（只限制编辑，空密码能打开），解密需要 cryptography
        if reader.is_encrypted and not reader.decrypt(""):
            return pdfium_texts(data, pages) or []      # 真要密码才能打开的：按读不出处理，不当成下载失败
        plist = list(reader.pages[:pages])
    except Exception as e:  # noqa: BLE001
        alt = pdfium_texts(data, pages)          # pypdf 打不开时换 pdfium 再试
        if alt is not None:
            log(f"    pypdf 打不开（{type(e).__name__}），已改用 pdfium 读取")
            return alt
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


PDFIUM_LOCK = threading.Lock()      # pdfium 不是线程安全的，同一时间只让一个线程用


def pdfium_texts(data: bytes, pages: int = PDF_PAGES) -> list[str] | None:
    """备用：用 pdfium 提取文字层。pdfium 打不开或没装时返回 None。"""
    with PDFIUM_LOCK:
        try:
            import pypdfium2 as pdfium
            doc = pdfium.PdfDocument(data)
        except Exception:  # noqa: BLE001
            return None
        try:
            parts = []
            for i in range(min(pages, len(doc))):
                tp = doc[i].get_textpage()
                parts.append(tp.get_text_range() or "")
                tp.close()
        except Exception:  # noqa: BLE001
            return None
        finally:
            doc.close()
    text = clean_text("\n".join(parts).replace("\r\n", "\n").replace("\r", "\n"))
    return [text] if text else []


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


_ocr_pool = None
_ocr_pool_lock = threading.Lock()


def ocr_pool() -> ThreadPoolExecutor:
    """全程序共用的 tesseract 线程池：同时跑的 tesseract 进程数不超过 CPU 核数。"""
    global _ocr_pool
    with _ocr_pool_lock:
        if _ocr_pool is None:
            _ocr_pool = ThreadPoolExecutor(max_workers=max(1, os.cpu_count() or 2), thread_name_prefix="ocr")
        return _ocr_pool


def tesseract_page(png: bytes, page_no: int) -> tuple[str, str]:
    """识别一页。返回 (状态, 文字)，状态：ok / timeout / failed。"""
    try:
        # OMP_THREAD_LIMIT=1：tesseract 自带的多线程在多份同时跑时反而更慢，每个进程只用一个核
        r = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "6", "-l", "eng"], input=png, capture_output=True,
                           timeout=OCR_TIMEOUT, env={**os.environ, "OMP_THREAD_LIMIT": "1"})
    except subprocess.TimeoutExpired:
        log(f"    第 {page_no} 页文字识别超时，跳过")
        return "timeout", ""
    except OSError as e:
        log(f"    第 {page_no} 页文字识别失败：{e}")
        return "failed", ""
    if r.returncode != 0:
        log(f"    第 {page_no} 页文字识别失败：{r.stderr.decode('utf-8', 'replace')[:200]}")
        return "failed", ""
    return "ok", r.stdout.decode("utf-8", "replace")


def ocr_texts(data: bytes, pages: int = OCR_PAGES) -> list[str]:
    """扫描件：把前几页渲染成图片，用 tesseract 识别文字（几页同时识别）。返回 [文字] 或 []。
    每一页 tesseract 都报错时抛 OCREnvError（环境问题，不是这份 PDF 的问题）。"""
    import pypdfium2 as pdfium
    pngs = []
    with PDFIUM_LOCK:
        try:
            doc = pdfium.PdfDocument(data)
        except Exception as e:  # noqa: BLE001  pypdf 能打开而这里打不开：多半是加了密码，重试也没用
            log(f"    扫描件渲染失败（{type(e).__name__}: {e}），按读不出处理")
            return []
        try:
            for i in range(min(pages, len(doc))):
                page = doc[i]
                w, h = page.get_size()                                   # 单位：点（1/72 英寸）
                scale = min(OCR_DPI / 72, OCR_MAX_SIDE / max(w, h, 1))
                img = page.render(scale=scale).to_pil().convert("L")
                buf = io.BytesIO()
                img.save(buf, "PNG", compress_level=1)                   # 压缩级别低一点，编码快很多
                pngs.append(buf.getvalue())
        finally:
            doc.close()
    results = list(ocr_pool().map(tesseract_page, pngs, range(1, len(pngs) + 1)))
    if results and all(st == "failed" for st, _ in results):
        raise OCREnvError("tesseract 每一页都报错")
    text = clean_text("\n".join(t for st, t in results if st == "ok"))
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
        first = clean_name(nm.group(1))
        if i > 0 and re.match(r"(?:and|&)\b", first, re.I) and looks_like_name(clean_name(lines[0])):
            # 名称有好几行，“Name” 标签排在中间：“Ackland … (Ackland)\n…\nName and Goodman Logistics …”
            return clean_name(" ".join(lines[:i] + [first]))[:200], region
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


VP_RE = re.compile(r"voting\s*pow\s*e\s*r", re.I)                 # 有的 PDF 写成 “Votingpower”“voting powe r”
SEC2_RE = re.compile(r"(?:^|\n)\s*2\s*\.?\s*(?:Details\s+of\s+voting\s*pow\s*e\s*r|Previous\s+and\s+present\s+voting\s*pow\s*e\s*r)", re.I)
SEC3_RE = re.compile(r"\n\s*3\s*\.\s*[A-Za-z]")
PCT_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,8})?)\s?%")              # 有的写到小数点后 6 位：5.298524%
BARE_PCT_RE = re.compile(r"(?<![\d.,$])(\d{1,3}\.\d{1,8})(?![\d,%]|\.\d)")  # 表头写 “Voting power (%)”，数字不带 %
TOTAL_RE = re.compile(r"\btotal\b", re.I)
GLUED_PCT_RE = re.compile(r"\d{1,3}(?:,\d{3})+(\d{1,2}\.\d{1,8})\s?%")   # 股数和比例粘在一起：“25,887,0385.02%”
EVENT_RE = re.compile(                                  # 词间用 \s*：扫描件识别常丢空格（“ofthe”）
    r"(?:became|ceased\s*to\s*be)\s*a\s*substantial\s*(?:share)?holder\s*on"
    r"|date\s*of\s*becoming\s*a\s*substantial\s*(?:share)?holder"
    r"|there\s*was\s*a\s*change\s*in\s*the"            # 日期可能紧跟在 “in the” 后（Hancock），也可能在 “of the” 后
    r"|change\s*in\s*the\s*interests?\s*of\s*the"      # 604 常见排版：日期夹在“of the”和“substantial holder on”之间
    r"|interests?\s*of\s*the\s*substantial\s*holder\s*on"
    r"|substantial\s*(?:share)?holder\s*on",
    re.I,
)
DATE_NUM_RE = re.compile(r"(?<!\d)(\d{1,2})\s?[/.\-]\s?(\d{1,2})\s?[/.\-]\s?(\d{4}|\d{2})(?!\d)")   # 兼容“29 /10/2026”这种多了空格的写法
DATE_SP_RE = re.compile(r"(?<!\d)(?<!\d/)(\d{1,2})\s+(\d{1,2})\s+(20\d{2})(?!\d)")   # 方格填写：“28 10 2026”“/ /05 11 2026”
DATE_8_RE = re.compile(r"(?<!\d)(\d{2})(\d{2})(20\d{2})(?!\d)")                   # 方格填写：“28102026”
DATE_TXT_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?(?:\s+of)?[\s\-/.,]*([A-Za-z]{3,9})\.?[\s,\-/.]*(\d{4})")   # 28 Oct 2026、28/October/2026、13 October2026、27th of October 2026
DATE_US_RE = re.compile(r"\b([A-Za-z]{3,9})\.?[\s/.\-]+(\d{1,2})(?:st|nd|rd|th)?,?[\s/.\-]+(\d{4})")   # October 28, 2026、October / 21 / 2026（美式）


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


SPACED_NUM_RE = re.compile(r"\d\s?\.\s+\d|\d\s+\.\s?\d")                      # “25. 9 3 %”：字间带空格的 PDF
COMMA_DEC_RE = re.compile(r"(?<![\d,.])(\d{1,2}),(\d{1,3})(?=\s?%)")            # “6,93%”：欧式小数逗号


def normalise_pct_line(ln: str) -> str:
    """把比例的怪写法改成普通写法：欧式逗号小数、数字中间带空格。"""
    if SPACED_NUM_RE.search(ln):
        ln = re.sub(r"(?<=[\d.,])\s+(?=[\d.,%])", "", ln)      # 结果里股数和比例会粘在一起，由 GLUED_PCT_RE 接手
    return COMMA_DEC_RE.sub(r"\1.\2", ln)


def pct_rows(seg: str, allow_bare: bool = True) -> list[tuple[str, list[float]]]:
    seg = "\n".join(normalise_pct_line(ln) for ln in seg.split("\n"))
    bare = allow_bare and not PCT_RE.search(seg) and not GLUED_PCT_RE.search(seg)
    rows = []
    for ln in seg.split("\n"):
        vals = [float(x) for x in (BARE_PCT_RE if bare else PCT_RE).findall(ln)]
        if not bare and allow_bare and len(vals) == 1:
            # 前后两个比例只有一个带 %：“10,671,838 7.54 9,986,838 7.04%”
            both = sorted([(m.start(), float(m.group(1))) for m in PCT_RE.finditer(ln)]
                          + [(m.start(), float(m.group(1))) for m in BARE_PCT_RE.finditer(ln)])
            if len(both) == 2 and all(4.5 <= v <= 100 for _, v in both):
                vals = [v for _, v in both]
        if not vals and not bare:
            vals = [float(x) for x in GLUED_PCT_RE.findall(ln)]
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
        return [] if (ocr or not bounded) else flat[:2]   # 扫描件、或没找到第二节边界时不跨行拼数字（会把说明文字里的比例拼进来）
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
        vp = pick_voting_power(seg, ntype, bounded, ocr)
        if ntype == "change" and len(vp) == 2 and vp[0] == vp[1] and not bounded and not SEC2_RE.search(text):
            vp = []            # 没有第二节标题、前后又一模一样：多半是公司说明信里的数字，不是表格
        out["voting_power"] = vp
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
NZ_PCT_RE = re.compile(r"total\s+percentage\s+held(?:\s+in\s+class)?\s*:?\s*(\d{1,3}(?:\.\d+)?)\s*%", re.I)   # “in class:” 可能被挤到下一行
NZ_DATE_RE = re.compile(r"(?:Date\s+of\s+relevant\s+event|Date\s+on\s+which\s+substantial\s+holding(?:\s+(?:began|ceased))?)\s*:?\s*([^\n]+)", re.I)
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
        holder = clean_name(name.group(1)) if name else ""
        if name and re.match(r"city\s+and\s+country", holder, re.I):
            # 两栏表头排在同一行，名称在下一行：“Name: City and country of registered office:\nFMR LLC Wilmington, USA”
            nxt = text[name.end():].lstrip("\n").split("\n", 1)[0]
            holder = clean_name(nxt) if looks_like_name(clean_name(nxt)) else ""
        return {"holder_name": holder[:200],
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
    style: str = "active"       # 基金类型，见 insight.STYLES


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
                              h.get("group") or "主动基金",
                              h.get("style") if h.get("style") in insight.STYLES
                              else ("super" if h.get("group") == "养老金" else "active")))
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


def pos_key(holder: str, code: str) -> str:
    return f"{holder}|{code}"


def update_history(history: dict, entries: dict, holders: list[Holder], today: dt.date) -> int:
    """把命中名单、已读过明细的公告记进持仓历史（同一份公告重复来的只记一次；重读后会覆盖旧的一行）。返回新增行数。"""
    pos = history.setdefault("pos", {})
    added = 0
    for e in sorted(entries.values(), key=lambda e: str(e.get("id", "")).startswith("mk-")):   # 列表页那条优先
        if e.get("status") != "parsed" or e.get("detail_v") != INSIGHT_VERSION or e.get("type") not in ("becoming", "change", "ceasing"):
            continue
        holder, _ = match_entry(e, holders)
        if not holder:
            continue
        rec = dict(insight.history_record(e), dv=e.get("detail_v"))
        rows = pos.setdefault(pos_key(holder, target_code(e)), [])
        same = lambda r: (r.get("type"), r.get("cd"), r.get("vp")) == (rec["type"], rec["cd"], rec["vp"]) and rec["cd"] and rec["vp"]
        idx = next((i for i, r in enumerate(rows) if r.get("id") == rec["id"]), None)
        if idx is not None:
            if rows[idx].get("fwd") and not rec.get("fwd"):
                rec["fwd"] = rows[idx]["fwd"]
            rows[idx] = rec
        elif not any(same(r) for r in rows):
            rows.append(rec)
            added += 1
    cutoff = (today - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    for k in list(pos):
        pos[k] = sorted((r for r in pos[k] if (r.get("date") or "") >= cutoff), key=lambda r: (r.get("date") or "", r.get("time") or "", str(r.get("id"))))
        if not pos[k]:
            del pos[k]
    history["schema"] = 1
    return added


def cost_for(history: dict | None, holder: str, code: str, e: dict) -> dict:
    """这份公告之前，这家基金在这只股票上累计出来的平均买入成本。"""
    rows = ((history or {}).get("pos") or {}).get(pos_key(holder, code)) or []
    me = (e.get("date") or "", e.get("time") or "", str(e.get("id")))
    before = [r for r in rows if r.get("id") != e.get("id") and (r.get("date") or "", r.get("time") or "", str(r.get("id"))) < me]
    if not before:
        return {}
    votes = e.get("votes") or []
    holding = votes[0] if e.get("type") == "change" and len(votes) == 2 else next((r["votes"][-1] for r in reversed(before) if r.get("votes")), None)
    return insight.cost_basis(before, holding)


SEQ_DAYS = 120


def _rec_dir(r: dict) -> str:
    if r.get("type") == "becoming":
        return "buy"
    if r.get("type") == "ceasing":
        return "sell"
    v, vp = r.get("votes") or [], r.get("vp") or []
    d = (v[1] - v[0]) if len(v) == 2 else (vp[1] - vp[0]) if len(vp) == 2 else (r.get("buy_qty") or 0) - (r.get("sell_qty") or 0)
    return "buy" if d > 0 else "sell" if d < 0 else ""


def sequence_for(history: dict | None, holder: str, code: str, e: dict) -> dict:
    """这家基金在这只股票上之前的公告方向：same = 近 4 个月里紧挨着这份、方向相同的连续几份；
    opp_days = 上一份方向相反的公告离现在多少天（没有就是 None）。方向用这份公告自己的数字判断。"""
    rows = ((history or {}).get("pos") or {}).get(pos_key(holder, code)) or []
    me_key = (e.get("date") or "", e.get("time") or "", str(e.get("id")))
    mine = _rec_dir({"type": e.get("type"), "votes": e.get("votes"), "vp": e.get("voting_power")})
    if not mine:
        return {}
    try:
        today = dt.date.fromisoformat(e.get("date") or "")
    except ValueError:
        return {}
    same, opp_days = 0, None
    counting = True
    for r in sorted((r for r in rows if r.get("id") != e.get("id")
                     and (r.get("date") or "", r.get("time") or "", str(r.get("id"))) < me_key),
                    key=lambda r: (r.get("date") or "", r.get("time") or ""), reverse=True):
        try:
            age = (today - dt.date.fromisoformat(r.get("date") or "")).days
        except ValueError:
            continue
        d = _rec_dir(r)
        if not d:
            continue
        if d == mine and counting and age <= SEQ_DAYS:
            same += 1
        elif d != mine:
            if opp_days is None:
                opp_days = age
            counting = False
        if age > SEQ_DAYS:
            break
    return {"same": same, "opp_days": opp_days}


def _row_key(r: dict) -> tuple:
    return (r.get("date") or "", r.get("time") or "", str(r.get("id")))


def _rows_before(history: dict | None, holder: str, code: str, e: dict) -> list[dict]:
    rows = ((history or {}).get("pos") or {}).get(pos_key(holder, code)) or []
    me = _row_key(e)
    return sorted((r for r in rows if r.get("id") != e.get("id") and _row_key(r) < me), key=_row_key)


BAD_DATA = (TypeError, ValueError, AttributeError, KeyError, IndexError, ZeroDivisionError, OverflowError)


def stage_for(history: dict | None, holder: str, code: str, e: dict) -> dict:
    """见 _stage_for。持仓历史文件里有坏数据时返回 {}（宁可不用这条证据，也不能让解读出错）。"""
    try:
        return _stage_for(history, holder, code, e)
    except BAD_DATA:
        return {}


def habits(history: dict | None, as_of: str = "") -> dict:
    """见 _habits。持仓历史文件里有坏数据时返回 {}。"""
    try:
        return _habits(history, as_of)
    except BAD_DATA:
        return {}


def _stage_for(history: dict | None, holder: str, code: str, e: dict) -> dict:
    """持仓阶段（第十一轮）：这家基金在这只股票上走到哪一步了。只用这份公告之前的记录。
    返回 {age: 从起点到现在的天数（起点：知道建仓时间就用第一笔买入，否则用最早一份公告的变动日）, known_start: 最早一份是不是 603, fresh: 那份 603 是不是“近 4 个月买出来的”（知道建仓时间）,
           peak: 到过的最高持股比例, off: 这次之后比最高点少了多少（0–1）, off_before: 这次之前比最高点少了多少,
           by: 用什么量的（"股数" 或 "比例"）, n_buy / n_sell: 之前公告过几次加仓、减仓}；没有更早的记录返回 {}。
    最高点优先用持股数量（不受增发摊薄影响）；中间遇到股份合并/拆分（倒推总股本差一倍以上）就从那之后重新算。"""
    before = _rows_before(history, holder, code, e)
    last_exit = max((i for i, r in enumerate(before) if r.get("type") == "ceasing"), default=-1)
    before = before[last_exit + 1:]              # 中途退出过（605）又回来的，只看回来之后这一段
    if not before:
        return {}
    first = before[0]
    fv = first.get("votes") or []
    # 603 只说明“哪天越过 5%”，不等于“哪天开始买”。只有最早那份 603 里近 4 个月净买入占了当时持仓一半以上，才算知道它是什么时候建的仓
    fresh = bool(first.get("type") == "becoming" and fv and fv[0] > 0
                 and ((first.get("buy_qty") or 0) - (first.get("sell_qty") or 0)) >= 0.5 * fv[0])
    try:
        today = dt.date.fromisoformat(e.get("date") or "")
        start = dt.date.fromisoformat((first.get("first") if fresh else "") or first.get("cd") or first.get("date") or "")
    except ValueError:
        return {}
    cur_vp = [float(x) for x in (e.get("voting_power") or [])]
    cur_votes = [int(x) for x in (e.get("votes") or [])]
    typ = e.get("type")
    points: list[tuple[float | None, int | None]] = []          # (比例, 股数)，按时间顺序；每份公告最多两个点（之前、之后）
    for r in before:
        vp, votes = r.get("vp") or [], r.get("votes") or []
        for i in range(len(vp)):
            points.append((vp[i], votes[i] if len(votes) == len(vp) else None))
    n_hist = len(points)
    if typ == "change" and len(cur_vp) == 2:
        points.append((cur_vp[0], cur_votes[0] if len(cur_votes) == 2 else None))
        points.append((cur_vp[1], cur_votes[1] if len(cur_votes) == 2 else None))
    elif typ == "becoming" and cur_vp:
        points.append((cur_vp[0], cur_votes[0] if cur_votes else None))
    elif typ == "ceasing":
        points.append((None, None))
    else:
        return {}
    # 股份合并/拆分：相邻两个点倒推出来的总股本差一倍以上，之前的股数就不能比了
    cut, last_so = 0, None
    for i, (vp, votes) in enumerate(points):
        if vp and votes:
            so = votes / vp
            if last_so and not 0.5 <= so / last_so <= 2:
                cut = i
            last_so = so
    usable = points[cut:]
    vps = [vp for vp, _ in usable[:-1] if vp]
    shs = [sh for _, sh in usable[:-1] if sh]
    after_vp, after_sh = points[-1]
    if typ == "ceasing":
        after_vp = 5.0                                           # 只知道在 5% 以下：按“至少减了这么多”算
    prev_vp, prev_sh = next(((vp, sh) for vp, sh in reversed(usable[:-1]) if vp), (None, None))
    if not vps or not after_vp:
        return {}
    by_shares = bool(typ != "ceasing" and after_sh and shs and prev_sh and len(shs) == len(vps))
    if by_shares:
        peak, after, prev = max(shs), after_sh, prev_sh
    else:
        peak, after, prev = max(vps), after_vp, prev_vp
    dirs = [_rec_dir(r) for r in before]
    return {"age": (today - start).days, "known_start": first.get("type") == "becoming", "fresh": fresh, "peak": round(max(vps), 2),
            "off": round(max(1 - after / peak, 0.0), 3), "off_before": round(max(1 - prev / peak, 0.0), 3),
            "by": "股数" if by_shares else "比例", "n_buy": dirs.count("buy"), "n_sell": dirs.count("sell"),
            "hist": n_hist}


HABIT_K = 8               # 往“所有基金的平均水平”收缩用的假想样本数：样本少的基金主要看平均，样本多的看它自己
HABIT_REV_DAYS = 90


def _habits(history: dict | None, as_of: str = "") -> dict:
    """基金习惯（第十一轮）：从持仓历史里数每家基金过去的做法。as_of：只用这一天之前已经发生的事（回头看时防止偷看答案）。
    返回 {基金: {n: 有后续的动作数, follow: 下一份公告还是同方向的比例, rev: 90 天内反向的比例,
                  n_buy: 能看出股价强弱的买入次数, contr: 其中在股价弱的时候买的比例}}，比例都已向全体平均收缩。"""
    raw: dict[str, dict] = {}
    for key, rows in ((history or {}).get("pos") or {}).items():
        holder = key.split("|", 1)[0]
        a = raw.setdefault(holder, {"n": 0, "same": 0, "rev": 0, "n_buy": 0, "weak": 0})
        seq = [(r, _rec_dir(r)) for r in sorted(rows, key=_row_key)]
        seq = [(r, d) for r, d in seq if d and (not as_of or (r.get("date") or "") < as_of)]
        for (r, d), (nr, nd) in zip(seq, seq[1:]):
            a["n"] += 1
            a["same"] += nd == d
            gap = insight._days_between(r.get("date"), nr.get("date"))
            a["rev"] += nd != d and gap <= HABIT_REV_DAYS
        for r, d in seq:
            px = r.get("px") or {}
            pos, r3 = px.get("pos"), px.get("r3m")
            if d != "buy" or (pos is None and r3 is None):
                continue
            weak = (pos is not None and pos <= 0.4) or (r3 is not None and r3 <= -0.10)
            strong = (pos is not None and pos >= 0.6) or (r3 is not None and r3 >= 0.10)
            if weak != strong:
                a["n_buy"] += 1
                a["weak"] += weak
    tot = {k: sum(a[k] for a in raw.values()) for k in ("n", "same", "rev", "n_buy", "weak")}
    if not tot["n"]:
        return {}
    g_follow, g_rev = tot["same"] / tot["n"], tot["rev"] / tot["n"]
    g_contr = tot["weak"] / tot["n_buy"] if tot["n_buy"] else 0.5
    out = {}
    for holder, a in raw.items():
        if not a["n"] and not a["n_buy"]:
            continue
        out[holder] = {"n": a["n"], "follow": round((a["same"] + HABIT_K * g_follow) / (a["n"] + HABIT_K), 3),
                       "rev": round((a["rev"] + HABIT_K * g_rev) / (a["n"] + HABIT_K), 3),
                       "n_buy": a["n_buy"], "contr": round((a["weak"] + HABIT_K * g_contr) / (a["n_buy"] + HABIT_K), 3)}
    return out



def parse_company_rows(html: str) -> list[dict] | None:
    """某公司公告列表页的每一行：{id, date, time, headline, url}。页面认不出返回 None。"""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        a = tr.find("a", href=re.compile(r"idsId", re.I))
        if len(tds) < 3 or a is None or not a.get("href"):
            continue
        text = tds[0].get_text(" ", strip=True)
        dm, tm = DATE_RE.search(text), TIME_RE.search(text)
        idm = re.search(r"idsId=(\w+)", a["href"])
        if not dm or not idm:
            continue
        try:
            d = dt.date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))
        except ValueError:
            continue
        href = safe_url(urljoin(BASE, a["href"].strip()))
        if href:
            out.append({"id": idm.group(1), "date": d.isoformat(), "time": to_24h(*tm.groups()) if tm else "",
                        "headline": TAIL_RE.sub("", a.get_text(" ", strip=True)).strip(), "url": href})
    if not out and not re.search(r"no\s+announcements|Search\s+results", html, re.I):
        return None
    return out


class Backfill:
    """回补持仓历史。每个（基金, 股票）从已知最早的一份公告出发，按它写的“上一份公告日期”到公司的全年公告页找上一份，
    读出来确认是同一家基金就记下，再接着往前，直到 603（新进）、找不到、或超出保留期。有总量和时间上限，没追完的下次接着追。"""

    def __init__(self, history: dict, holders: list[Holder], today: dt.date, budget: int | None = None, seconds: float | None = None):
        self.h, self.holders, self.today = history, holders, today
        self.budget = BACKFILL_PDFS if budget is None else budget
        self.deadline = time.monotonic() + (BACKFILL_SECONDS if seconds is None else seconds)
        self.lists: dict[tuple, list] = {}
        self.series: dict[str, list] = {}
        self.read = self.added = self.refreshed = 0
        self.cutoff = (today - dt.timedelta(days=HISTORY_DAYS)).isoformat()

    def out_of_budget(self) -> bool:
        return self.read >= self.budget or time.monotonic() > self.deadline

    def year_rows(self, code: str, year: int) -> list[dict]:
        if (code, year) not in self.lists:
            rows = parse_company_rows(fetch(COMPANY_YEAR.format(code=code, year=year), timeout=40, tries=2).text)
            if rows is None:
                raise FetchError("公司年度公告页认不出来")
            self.lists[(code, year)] = [r for r in rows if SUBSTANTIAL_RE.search(r["headline"])]
            time.sleep(PAUSE)
        return self.lists[(code, year)]

    def prices(self, code: str) -> list:
        if code not in self.series:
            try:
                self.series[code] = price_series(code)
            except FetchError:
                self.series[code] = []
            time.sleep(PAUSE)
        return self.series[code]

    def read_notice(self, row: dict, code: str) -> dict | None:
        e = {**row, "code": code, "pages": None, "price_sensitive": False, "type": classify(row["headline"]),
             "hint": headline_hint(row["headline"]), "company": "", "holder_name": "", "region": "", "change_date": "",
             "voting_power": [], "pdf_url": "", "status": "new", "attempts": 0}
        self.read += 1
        try:
            process_entry(e, None, lambda _e: True)
        except Exception as ex:  # noqa: BLE001
            log(f"  回补读取失败 {code} {row['date']}：{type(ex).__name__}: {ex}")
            return None
        finally:
            time.sleep(PAUSE)
        return e if e.get("status") == "parsed" else None

    def chase(self, key: str) -> None:
        holder, code = key.split("|", 1)
        meta = self.h.setdefault("meta", {}).setdefault(key, {})
        rows = self.h["pos"][key]
        while not meta.get("done") and not self.out_of_budget():
            first = rows[0]
            target = first.get("prev_notice") or ""
            if first.get("type") == "becoming" or not target or target < self.cutoff or target >= (first.get("date") or ""):
                meta["done"] = "start" if first.get("type") == "becoming" else "no_prev" if not target else "old"
                return
            t = dt.date.fromisoformat(target)
            lo, hi = (t - dt.timedelta(days=3)).isoformat(), (t + dt.timedelta(days=6)).isoformat()
            known = {r.get("id") for r in rows} | set(meta.get("tried", []))
            years = sorted({t.year, (t + dt.timedelta(days=6)).year})
            cands = [r for y in years for r in self.year_rows(code, y)
                     if lo <= r["date"] <= hi and r["id"] not in known and r["date"] < (first.get("date") or "9")]
            cands = [r for r in cands if not is_noise(headline_hint(r["headline"]), self.holders, [])]
            cands.sort(key=lambda r: abs((dt.date.fromisoformat(r["date"]) - t).days))
            hit = None
            for r in cands[:4]:
                if self.out_of_budget():
                    return
                meta.setdefault("tried", []).append(r["id"])
                e = self.read_notice(r, code)
                if not e:
                    continue
                who, _ = match_entry(e, self.holders)
                if who and target_code(e) == code:
                    series = self.prices(code)
                    tr = e.get("trades") or {}
                    if series:
                        e["px"] = insight.price_context(series, e.get("change_date") or tr.get("last") or e["date"], tr.get("first", ""), tr.get("last", ""))
                        add_timeline(e, series)
                    rec = dict(insight.history_record(e), bf=1, dv=INSIGHT_VERSION)
                    dest = self.h["pos"].setdefault(pos_key(who, code), [])
                    if not any(x.get("id") == rec["id"] for x in dest):
                        dest.append(rec)
                        dest.sort(key=lambda x: (x.get("date") or "", x.get("time") or "", str(x.get("id"))))
                        self.added += 1
                    if who == holder:
                        hit = rec
                        break
            if not hit:
                meta["done"] = "not_found"
                return
            meta["tried"] = meta.get("tried", [])[-12:]

    def refresh_old(self) -> int:
        """用旧读法（INSIGHT_VERSION 更早）读的历史记录，重新下载按现在的读法读一遍。返回重读了几份。"""
        heads = {"becoming": "Becoming a substantial holder", "ceasing": "Ceasing to be a substantial holder"}
        done = 0
        for key in sorted(self.h.get("pos", {}), key=lambda k: self.h["pos"][k][-1].get("date") or "", reverse=True):
            holder, code = key.split("|", 1)
            rows = self.h["pos"][key]
            for idx, r in enumerate(rows):
                if self.out_of_budget():
                    return done
                if r.get("dv") == INSIGHT_VERSION or r.get("dv_tries", 0) >= 2:
                    continue
                m = re.search(r"(?:^|-)(\d{8})(?:-|$)", str(r.get("id") or ""))
                if not m:
                    r["dv_tries"] = 9
                    continue
                url = BASE + "/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId=" + m.group(1)
                row = {"id": r["id"], "date": r.get("date") or "", "time": r.get("time") or "", "url": url,
                       "headline": heads.get(r.get("type"), "Change in substantial holding")}
                e = self.read_notice(row, code)
                who = match_entry(e, self.holders)[0] if e else None
                if not e or who != holder or target_code(e) != code:
                    r["dv_tries"] = r.get("dv_tries", 0) + 1
                    continue
                series = self.prices(code)
                tr = e.get("trades") or {}
                if series:
                    e["px"] = insight.price_context(series, e.get("change_date") or tr.get("last") or e["date"], tr.get("first", ""), tr.get("last", ""))
                    add_timeline(e, series)
                new = dict(insight.history_record(e), dv=INSIGHT_VERSION)
                for k in ("bf", "fwd"):
                    if r.get(k) and not new.get(k):
                        new[k] = r[k]
                if isinstance(r.get("px"), dict) and r["px"].get("m") and isinstance(new.get("px"), dict):
                    new["px"]["m"] = r["px"]["m"]
                rows[idx] = new
                done += 1
                self.refreshed += 1
        return done

    def run(self) -> None:
        # 先追最近有动作的持仓
        keys = sorted(self.h.get("pos", {}), key=lambda k: self.h["pos"][k][-1].get("date") or "", reverse=True)
        for key in keys:
            if self.out_of_budget():
                break
            try:
                self.chase(key)
            except FetchError as ex:
                log(f"  回补 {key} 中断：{ex}")
                meta = self.h.setdefault("meta", {}).setdefault(key, {})
                meta["err"] = meta.get("err", 0) + 1
                if meta["err"] >= 4:                 # 连着几次都取不到这家公司的公告页：不再试
                    meta["done"] = "error"
            except Exception as ex:  # noqa: BLE001
                log(f"  回补 {key} 出错：{type(ex).__name__}: {ex}")
                self.h.setdefault("meta", {}).setdefault(key, {})["done"] = "error"

    def refresh_forward(self, max_codes: int = 40) -> None:
        """旧记录的“公告后 1/3/6 个月涨跌”到期了就补上。"""
        need: dict[str, list] = {}
        for key, rows in self.h.get("pos", {}).items():
            code = key.split("|", 1)[1]
            for r in rows:
                due = [k for k, n in (("1m", 30), ("3m", 91), ("6m", 182))
                       if k not in (r.get("fwd") or {}) and insight._shift(r.get("date") or "", n + 3) <= self.today.isoformat()]
                if due and r.get("fwd_tries", 0) < 3:
                    need.setdefault(code, []).append(r)
        for code in sorted(need, key=lambda c: c not in self.series)[:max_codes]:
            series = self.prices(code)
            for r in need[code]:
                fwd = insight.forward_returns(series, r.get("date") or "") if series else {}
                if len(fwd) > len(r.get("fwd") or {}):
                    r["fwd"] = fwd
                else:
                    r["fwd_tries"] = r.get("fwd_tries", 0) + 1


SMALL_INDEX = "XSO"                 # 小盘股对照 S&P/ASX Small Ordinaries
SMALL_VALUE = 2_000_000             # 每天成交额（日均成交量 × 股价）低于这个数（澳元）算小盘股
_INDEX: dict[str, list] = {}        # 本次运行里取过的指数日线


def benchmark_of(px: dict) -> str:
    """这只股票该和哪个指数比：小盘股（日均成交额 < SMALL_VALUE）用小盘指数，其余用 ASX 200；不知道成交额时用 ASX 200。"""
    try:
        value = float(px.get("adv")) * float(px.get("p"))
    except (TypeError, ValueError, AttributeError):
        return INDEX_CODE
    return SMALL_INDEX if 0 < value < SMALL_VALUE else INDEX_CODE


def index_series(code: str = INDEX_CODE) -> list:
    """指数日线，整理成和股票一样的 [[毫秒, 收盘], ...]（指数的行是 [时间, 开, 高, 低, 收, 量]）。每次运行每个指数只取一次。"""
    if code not in _INDEX:
        rows = []
        for row in price_series(code):
            try:
                rows.append([row[0], float(row[4] if len(row) >= 5 else row[1])])
            except (TypeError, ValueError, IndexError):
                continue
        _INDEX[code] = rows
    return _INDEX[code]


def add_forward_excess(history: dict) -> int:
    """给持仓历史里的“公告后 1/3/6 个月涨跌”（fwd）配上“比同期指数多涨多少”（fwd_x），以及用的是哪个指数（bm）。返回补了几条。"""
    todo = []
    for rows in (history.get("pos") or {}).values():
        for r in rows:
            fwd = r.get("fwd") if isinstance(r.get("fwd"), dict) else {}
            if fwd and set(fwd) - set(r["fwd_x"] if isinstance(r.get("fwd_x"), dict) else {}):
                todo.append(r)
    done = 0
    for r in todo:
        code = benchmark_of(r.get("px") or {})
        try:
            ser = index_series(code)
        except FetchError:
            continue
        ifwd = insight.forward_returns(ser, r.get("date") or "")
        fx = {k: round(v - ifwd[k], 3) for k, v in r["fwd"].items() if k in ifwd and isinstance(v, (int, float))}
        if fx:
            r["fwd_x"], r["bm"] = fx, code
            done += 1
    return done


def index_points() -> list[tuple[str, float]]:
    pts = []
    for row in price_series(INDEX_CODE):
        try:
            d = dt.datetime.fromtimestamp(row[0] / 1000, dt.timezone.utc).date().isoformat()
            p = float(row[4] if len(row) >= 5 else row[1])
        except (TypeError, ValueError, IndexError, OverflowError, OSError):
            continue
        if p > 0:
            pts.append((d, p))
    pts.sort()
    return pts


def add_market(entries: dict, history: dict) -> int:
    """给还没有大盘对照的股价背景补上 px["m"]（大盘同期涨跌）。只在有需要时取一次指数日线。返回补了几条。"""
    todo = []
    for e in entries.values():
        px = e.get("px")
        if isinstance(px, dict) and px.get("p") and "m" not in px:
            todo.append((px, e.get("change_date") or (e.get("trades") or {}).get("last") or e.get("date") or ""))
    for rows in (history.get("pos") or {}).values():
        for r in rows:
            px = r.get("px")
            if isinstance(px, dict) and px.get("p") and "m" not in px:
                todo.append((px, r.get("cd") or r.get("date") or ""))
    if not todo:
        return 0
    pts = index_points()
    if len(pts) < 50:
        raise FetchError("指数日线太短")
    for px, ref in todo:
        px["m"] = insight.market_returns(pts, ref)
    return len(todo)


def report_codes() -> dict[str, list[str]]:
    """watchlist.json 里每家基金的 report_codes：它旗下在 ASX 上市、每月发月报的产品代码。"""
    try:
        raw = json.loads(WATCH_FILE.read_text(encoding="utf-8"))
        return {h["name"]: [str(c).upper() for c in h.get("report_codes") or [] if re.fullmatch(r"[A-Za-z0-9]{3,4}", str(c))]
                for h in raw.get("holders", []) if h.get("report_codes")}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


REPORT_CHILD = """
import io, sys
from pypdf import PdfReader
r = PdfReader(io.BytesIO(sys.stdin.buffer.read()))
if r.is_encrypted:
    r.decrypt("")
out = []
for page in r.pages[:6]:
    try:
        out.append(page.extract_text() or "")
    except Exception:
        pass
sys.stdout.buffer.write("\\n".join(out).encode("utf-8", "replace"))
"""


def report_text(data: bytes) -> str:
    """读月报前 6 页的文字。放在单独的进程里读，限内存、限时间：月报里图表多，个别文件会把内存吃光
    （第十二轮真实运行时整个任务因此被系统杀掉过），读不出就返回空。"""
    def limit():
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (1_500_000_000, 1_500_000_000))
        except Exception:  # noqa: BLE001
            pass
    try:
        p = subprocess.run([sys.executable, "-c", REPORT_CHILD], input=data, capture_output=True, timeout=45,
                           preexec_fn=limit if os.name == "posix" else None)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return p.stdout.decode("utf-8", "replace") if p.returncode == 0 else ""


def update_fund_reports(store: dict, today: dt.date) -> int:
    """把名单基金最近几个月的月报正文存下来（每份只下载一次）。返回新读了几份。"""
    reports = [r for r in store.get("reports") or [] if isinstance(r, dict) and r.get("id")]
    keep_from = (today - dt.timedelta(days=REPORT_KEEP_DAYS)).isoformat()
    reports = [r for r in reports if (r.get("date") or "") >= keep_from]
    seen = {r["id"] for r in reports}
    new = 0
    for holder, codes in report_codes().items():
        for code in codes:
            if new >= REPORT_MAX_NEW:
                break
            try:
                rows = parse_company_rows(fetch(COMPANY_ANNS.format(code=code), timeout=40, tries=2).text) or []
            except FetchError as ex:
                log(f"  月报列表没取到 {code}：{ex}")
                continue
            time.sleep(PAUSE)
            for row in rows:
                if new >= REPORT_MAX_NEW:
                    break
                if row["id"] in seen or row["date"] < keep_from or not REPORT_RE.search(row["headline"]):
                    continue
                seen.add(row["id"])
                rec = {"id": row["id"], "holder": holder, "fund": code, "date": row["date"], "title": row["headline"][:80], "url": row["url"], "text": ""}
                try:
                    data, _ = download_pdf(row["url"])
                    rec["text"] = " ".join(report_text(data).split())[:REPORT_CHARS]
                except Exception as ex:  # noqa: BLE001
                    log(f"  月报没读出 {code} {row['date']}：{type(ex).__name__}: {ex}")
                reports.append(rec)          # 读不出的也记下（text 为空），免得每次都重试
                new += 1
                time.sleep(PAUSE)
    store["reports"] = sorted(reports, key=lambda r: (r.get("date") or "", r["id"]))
    return new


BOILER_RE = re.compile(r"subsidiary|responsible\s+entity|\bABN\b|\bAFSL?\b|disclaimer|past\s+performance|investment\s+manager|trustee"
                       r"|fund\s+information|management\s+fee|registered\s+office|unit\s+registry", re.I)
NAME_DROP_RE = re.compile(r"\b(limited|ltd|group|holdings?|corporation|corp|company|co|nl|plc|inc|the|australia|australian)\b\.?", re.I)


def name_key(company: str) -> str:
    """公司名里用来在月报里搜的那一截（去掉 Limited / Group 之类）。太短太泛的不用。"""
    words = [w for w in re.sub(r"[^A-Za-z0-9#&' ]", " ", NAME_DROP_RE.sub(" ", company or "")).split() if w]
    key = " ".join(words[:2])
    return key if len(key) >= 7 else ""


def fund_quotes(store: dict | None, holder: str, code: str, company: str, date: str, limit: int = 2) -> list[dict]:
    """这家基金自己的月报里提到这只股票的句子（公告前 100 天到后 60 天之间发的月报）。"""
    out, table_only = [], []
    if not (store and code and date):
        return out
    if holder.split()[0].lower() in (company or "").lower():       # 这只“股票”就是基金自己家的产品：月报里到处是它的名字
        return out
    lo, hi = insight._shift(date, -100), insight._shift(date, 60)
    tick = re.compile(rf"\(\s*ASX\s*:\s*{re.escape(code)}\s*\)", re.I)
    key = name_key(company)
    name = re.compile(rf"(?<![A-Za-z]){re.escape(key)}(?![a-z])", re.I) if key else None
    fw = next((w for w in re.sub(r"[^A-Za-z0-9#& ]", " ", NAME_DROP_RE.sub(" ", company or "")).split() if len(w) >= 3), "")
    alts = "|".join(re.escape(w) for w in dict.fromkeys([fw, code]) if w)
    first_word = re.compile(rf"(?<![A-Za-z0-9])(?:{alts})(?![A-Za-z0-9])") if alts else None
    for r in sorted((r for r in store.get("reports") or [] if isinstance(r, dict)), key=lambda r: r.get("date") or "", reverse=True):
        if r.get("holder") != holder or not (lo <= (r.get("date") or "") <= hi) or not r.get("text"):
            continue
        sents = re.split(r"(?<=[.!?])\s+(?=[A-Z])", r["text"])

        def prose(x: str) -> bool:            # 表格、图表坐标轴被读成的“句子”不要
            return 25 <= len(x) <= 600 and sum(c.isdigit() for c in x) <= 0.12 * len(x) and not BOILER_RE.search(x)

        ticked = [i for i, x in enumerate(sents) if tick.search(x) and prose(x)]
        named = [i for i, x in enumerate(sents) if name and name.search(x) and prose(x)]
        hits = sorted(set(ticked) | set(named))
        if ticked and first_word:             # 带代码的那句之后，接着用简称讲这只股票的句子也带上
            start = ticked[0]
            hits = sorted(set(hits) | {i for i in range(start + 1, min(start + 6, len(sents))) if first_word.search(sents[i]) and prose(sents[i])})
        if not hits:
            if not table_only and any((tick.search(x) or (name and name.search(x))) and not BOILER_RE.search(x) for x in sents):
                table_only.append({"fund": r.get("fund"), "title": r.get("title"), "date": r.get("date"), "url": safe_url(r.get("url") or ""), "text": ""})
            continue
        text = " ".join(sents[i].strip() for i in hits[:3])
        if len(text) > 520:
            text = text[:517].rsplit(" ", 1)[0] + "…"
        out.append({"fund": r.get("fund"), "title": r.get("title"), "date": r.get("date"), "url": safe_url(r.get("url") or ""), "text": text})
        if len(out) >= limit:
            break
    return out or table_only          # 只在持仓表里出现过：只提最新的一份


# 每个目的判对了的话，之后应该发生什么（第十六轮“可检验的预言”）。只用之后的记录检验，判断时不用（as_of）。
CHECK_OF = {"prog": "next_same", "wind": "next_same", "short": "rev90", "stop": "nobuy180", "rebal": "no_big_cut",
            "dip": "beat3", "pull": "beat3", "mom": "beat3", "base": "beat3"}
CHECK_NAMES = {"next_same": "下一份公告还是同方向", "rev90": "90 天内反向", "nobuy180": "半年内没有买回",
               "no_big_cut": "下一份不是大幅减仓（< 20%）", "beat3": "之后 3 个月跑赢指数"}


def outcome_checks(r: dict, side: str, later: list[dict], today: dt.date) -> dict:
    """一条记录之后发生了什么：{检验名: True/False}；还看不出来的不放。later：同一持仓之后的记录（按时间）。"""
    out = {}
    try:
        d0 = dt.date.fromisoformat(r.get("date") or "")
    except ValueError:
        return out
    moves = [(x, _rec_dir(x)) for x in later if _rec_dir(x)]
    if moves:
        out["next_same"] = moves[0][1] == side
        nx = moves[0][0]
        if side == "sell":
            v = nx.get("votes") or []
            if nx.get("type") == "ceasing":
                out["no_big_cut"] = False
            elif len(v) == 2 and v[0] > 0:
                out["no_big_cut"] = not (v[1] < v[0] * 0.8)
    age = lambda x: (dt.date.fromisoformat(x.get("date") or "9999-01-01") - d0).days if re.fullmatch(r"\d{4}-\d{2}-\d{2}", x.get("date") or "") else 99999
    opp = [x for x, dirn in moves if dirn != side]
    if opp and age(opp[0]) <= 90:
        out["rev90"] = True
    elif (today - d0).days >= 90 or any(age(x) > 90 for x in later):
        out["rev90"] = False
    if side == "sell":
        buys = [x for x, dirn in moves if dirn == "buy"]
        if buys and age(buys[0]) <= 180:
            out["nobuy180"] = False
        elif (today - d0).days >= 180 or any(age(x) > 180 for x in later):
            out["nobuy180"] = True
    f3 = r["fwd_x"].get("3m") if isinstance(r.get("fwd_x"), dict) else None
    if isinstance(f3, (int, float)):
        out["beat3"] = f3 > 0
    return out


def calibrate(history: dict, holders: list[Holder], today: dt.date | None = None) -> dict:
    """回头看：把持仓历史里每份公告按现在的公式重新判断一遍（只用单只股票的证据），再对照之后发生的事——
    公告后 3 个月股价涨跌、它自己下一份公告是同向还是反向。按“方向:目的”汇总。只是统计，不会自动改权重。"""
    style_of = {h.name: h.style for h in holders}
    agg: dict[str, dict] = {}
    base_ck: dict[str, list] = {}
    today = today or dt.date.today()
    habit_cache: dict[str, dict] = {}

    def habit_at(day: str) -> dict:           # 只用那一天之前的记录算习惯，不偷看之后的事
        if day not in habit_cache:
            habit_cache[day] = habits(history, as_of=day)
        return habit_cache[day]

    for key, rows in (history.get("pos") or {}).items():
        holder = key.split("|", 1)[0]
        for i, r in enumerate(rows):
            if not r.get("px") and not r.get("tr"):
                continue
            e = {"id": r.get("id"), "date": r.get("date"), "time": r.get("time"), "type": r.get("type"), "votes": r.get("votes"),
                 "voting_power": r.get("vp")}
            before = rows[:i]
            prev = before[-1] if before else None
            holding = (r["votes"][0] if r.get("type") == "change" and len(r.get("votes") or []) == 2
                       else next((x["votes"][-1] for x in reversed(before) if x.get("votes")), None))
            try:
                ins = insight.judge({
                    "type": r.get("type"), "voting_power": r.get("vp") or [], "votes": r.get("votes") or [], "trades": r.get("tr") or {},
                    "px": r.get("px") or {}, "ocr": r.get("ocr"), "style": style_of.get(holder, "active"), "date": r.get("date"),
                    "change_date": r.get("cd"), "tl": r.get("tl") or {}, "cost": insight.cost_basis(before, holding) if before else {},
                    "seq": sequence_for(history, holder, key.split("|", 1)[1], e),
                    "stage": stage_for(history, holder, key.split("|", 1)[1], e), "blk": r.get("blk") or {},
                    "habit": habit_at(r.get("date") or "").get(holder) or {},
                    "prev": {"vp": (prev.get("vp") or [None])[-1], "votes": (prev.get("votes") or [None])[-1], "type": prev.get("type"),
                             "date": prev.get("date")} if prev else None})
            except Exception:  # noqa: BLE001
                continue
            if not ins or not ins.get("_f"):
                continue
            side, top = ins["_f"]["side"], ins["purposes"][0]["k"]
            a = agg.setdefault(f"{side}:{top}", {"n": 0, "n3": 0, "sum3": 0.0, "up3": 0, "n_next": 0, "same_next": 0, "rev": 0,
                                                 "n3x": 0, "sum3x": 0.0, "up3x": 0, "ck": 0, "ck_ok": 0})
            a["n"] += 1
            f3 = (r.get("fwd") or {}).get("3m")
            if f3 is not None:
                a["n3"] += 1
                a["sum3"] += f3
                a["up3"] += f3 > 0
            f3x = r["fwd_x"].get("3m") if isinstance(r.get("fwd_x"), dict) else None
            if isinstance(f3x, (int, float)):
                a["n3x"] += 1
                a["sum3x"] += f3x
                a["up3x"] += f3x > 0
            checks = outcome_checks(r, side, rows[i + 1:], today)
            for name, ok in checks.items():          # 不分目的的全体平均，做对照
                b = base_ck.setdefault(f"{side}:{name}", [0, 0])
                b[0] += 1
                b[1] += ok
            name = CHECK_OF.get(top)
            if name and name in checks:
                a["ck"] += 1
                a["ck_ok"] += checks[name]
            nxt_row = next((x for x in rows[i + 1:] if _rec_dir(x)), None)
            if nxt_row:
                nxt = _rec_dir(nxt_row)
                a["n_next"] += 1
                a["same_next"] += nxt == side
                a["rev"] += nxt != side and insight._days_between(r.get("date"), nxt_row.get("date")) <= HABIT_REV_DAYS
    out = {}
    for k, a in sorted(agg.items()):
        out[k] = {"n": a["n"], "n3": a["n3"], "avg3": round(a["sum3"] / a["n3"], 3) if a["n3"] else None,
                  "up3": round(a["up3"] / a["n3"], 3) if a["n3"] else None, "n_next": a["n_next"],
                  "same_next": round(a["same_next"] / a["n_next"], 3) if a["n_next"] else None,
                  "rev_next": round(a["rev"] / a["n_next"], 3) if a["n_next"] else None,
                  "n3x": a["n3x"], "avg3x": round(a["sum3x"] / a["n3x"], 3) if a["n3x"] else None,
                  "up3x": round(a["up3x"] / a["n3x"], 3) if a["n3x"] else None}
        name = CHECK_OF.get(k.split(":", 1)[1])
        if name and a["ck"]:
            b = base_ck.get(f"{k.split(':', 1)[0]}:{name}") or [0, 0]
            out[k].update(check=round(a["ck_ok"] / a["ck"], 3), check_n=a["ck"], check_name=CHECK_NAMES[name],
                          check_base=round(b[1] / b[0], 3) if b[0] else None)
    return out


GENERIC_BRAND = {"australian", "australia", "colonial", "the", "first", "national", "united"}


def own_codes() -> dict[str, set[str]]:
    """watchlist.json 里每家基金自己旗下（或关联）的上市代码：report_codes + own_codes。"""
    try:
        raw = json.loads(WATCH_FILE.read_text(encoding="utf-8"))
        return {h["name"]: {str(c).upper() for c in (h.get("report_codes") or []) + (h.get("own_codes") or [])}
                for h in raw.get("holders", []) if h.get("report_codes") or h.get("own_codes")}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def is_own_product(holder: str, code: str, company: str, style: str = "active") -> bool:
    """这只“股票”是不是这家基金公司自己旗下的产品（例如 Regal 持有 RG1、L1 持有 GLS）。"""
    if code.upper() in own_codes().get(holder, set()):
        return True
    brand = (holder.split() or [""])[0]
    if style not in ("active", "hedge") or len(brand) < 2 or brand.lower() in GENERIC_BRAND:
        return False
    return bool(re.match(rf"\s*{re.escape(brand)}(?![A-Za-z0-9])", company or "", re.I))


def prev_from_history(history: dict | None, holder: str, code: str, e: dict) -> dict | None:
    """近期公告里找不到同一家在同一只股票上的上一份时，从持仓历史里找（比如 603 已经超过近期公告的保留期）。"""
    rows = ((history or {}).get("pos") or {}).get(pos_key(holder, code)) or []
    me = (e.get("date") or "", e.get("time") or "", str(e.get("id")))
    before = [r for r in rows if isinstance(r, dict) and r.get("id") != e.get("id")
              and (r.get("date") or "", r.get("time") or "", str(r.get("id"))) < me]
    if not before:
        return None
    r = before[-1]
    vp, votes = r.get("vp") or [], r.get("votes") or []
    return {"vp": vp[-1] if vp else None, "votes": votes[-1] if votes else None, "type": r.get("type"), "date": r.get("date")}


# ---------------------------------------------------------------- 倒手：卖出的一方和接盘的一方（第十五轮）

CP_DAYS = 3                # 两份公告的“变动日”最多差几天，才算同一笔交易的两头（大宗交易 T+2 交割，各家写的日期可能差一两天）
CP_MIN_DELTA = 0.5         # 按“变动了多少个百分点”配对时，至少要动这么多（太小的变动很容易碰巧相等）
CP_WHOLE_MIN = 6.0         # “整块转让”要求卖方原来的持股至少这么多：刚好在 5% 附近时，任何新越过 5% 的人都会“对得上”
_CP_GARBAGE = re.compile(r"^(?:in|each|the\s+entities|entities|and|of|annexure|refer|see|as\s+set)\b", re.I)
_CP_COLLECT = re.compile(r"collectively,?\s+(?:the\s+)?[\"“']?([A-Za-z0-9&.,'’\- ]{3,80}?)[\"”']?\s*\)", re.I)
_CP_TAIL = re.compile(r"\s+(?:ATF|as trustee for|and (?:its|their) (?:related bodies corporate|subsidiaries|associates|affiliates|controlled entities)"
                      r"|and (?:each of )?(?:the )?(?:entities|subsidiaries|related bodies)\b|and associates\b|\(ACN|\(ABN|ACN\b|ABN\b"
                      r"|\($|\(\s*$).*$", re.I)


def clean_holder_name(name: str) -> str:
    """网页显示用的申报方名字。读出来的名字有时是半句话（“in Annexure A (collectively, the Landchecker Shareholder Entities) and ...”），
    能认出“(collectively, the XXX)”就用 XXX；去掉 “ATF …”“and its related bodies corporate” 这类尾巴；认不出返回空串（宁缺毋错）。"""
    s = re.sub(r"\s+", " ", name or "").strip()
    if not s:
        return ""
    m = _CP_COLLECT.search(s)
    if m:
        return m.group(1).strip(" ,.")
    if _CP_GARBAGE.match(s) or "annexure" in s.lower()[:40]:
        return ""
    s = _CP_TAIL.sub("", s).strip(" ,;:-(")
    if len(s) > 70:
        s = s[:70].rsplit(" ", 1)[0] + "…"
    return s if len(s) >= 3 and re.search(r"[A-Za-z]{3}", s) else ""


def _vp_of(e: dict) -> list[float]:
    out = []
    raw = e.get("voting_power") if isinstance(e, dict) else None
    for x in raw if isinstance(raw, list) else []:
        try:
            v = float(x)
        except (TypeError, ValueError):
            return []
        if not 0 < v <= 100:
            return []
        out.append(v)
    return out


def holding_move(e: dict, prev_vp: float | None = None) -> dict:
    """一份公告里这一方的持股怎么动的（单位：百分点）。返回 {side, pre, post, d, exact}：
    pre / post：之前 / 之后的持股比例（不知道是 None）；d：变动了多少（不知道是 None）；exact：d 是不是确切的数。
    603：之前在 5% 以下，所以不知道确切变动；605：之后在 5% 以下，之前的比例要靠上一份公告（prev_vp）。看不出方向返回 {}。"""
    t, vp = e.get("type"), _vp_of(e)
    if t == "becoming" and vp:
        return {"side": "buy", "pre": None, "post": vp[0], "d": None, "exact": False}
    if t == "ceasing":
        pre = prev_vp if isinstance(prev_vp, (int, float)) and 5 <= prev_vp <= 100 else None
        return {"side": "sell", "pre": pre, "post": None, "d": None, "exact": False}
    if t == "change" and len(vp) == 2 and vp[0] != vp[1]:
        votes = e.get("votes") or []
        side = "buy" if vp[1] > vp[0] else "sell"
        if len(votes) == 2 and all(isinstance(v, int) and v > 0 for v in votes) and votes[0] != votes[1]:
            if (votes[1] > votes[0]) != (vp[1] > vp[0]):
                return {}                                 # 股数和比例方向相反（增发摊薄之类）：看不出真实买卖，不配对
        return {"side": side, "pre": vp[0], "post": vp[1], "d": round(abs(vp[1] - vp[0]), 3), "exact": True}
    return {}


def _org_key(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    words = [w for w in words if w not in ("the", "in", "each", "and", "of")]
    return " ".join(words[:2])


def _same_org(a: str, b: str) -> bool:
    """名字的前两个词一样（“JPMorgan Chase …”和“JPMorgan Chase & Co. and its affiliates”）：同一家机构。"""
    ka, kb = _org_key(a), _org_key(b)
    return bool(ka and len(ka) >= 6 and ka == kb)


def _close(a: float, b: float, rel: float, floor: float) -> bool:
    return abs(a - b) <= max(floor, rel * max(abs(a), abs(b)))


def find_counterparty(e: dict, code: str, holder: str, prev_vp: float | None, by_code: dict, holders: list[Holder]) -> dict:
    """见 _find_counterparty。存储里有坏数据时返回 {}（宁可不说倒手，也不能让解读出错）。"""
    try:
        return _find_counterparty(e, code, holder, prev_vp, by_code, holders)
    except BAD_DATA:
        return {}


def _find_counterparty(e: dict, code: str, holder: str, prev_vp: float | None, by_code: dict, holders: list[Holder]) -> dict:
    """在所有大股东公告（不只名单里的）里，找这份公告的“另一头”：同一只股票、方向相反、变动日相差不超过 CP_DAYS 天，而且数量对得上。
    对得上的三种情况（按可信度从高到低）：
      whole：买方之后的持股 ≈ 卖方之前的全部持股（整块转让；比如卖方先卖一部分，剩下的通过协议也算到买方名下）
      shares：两边的持股数变化差不到 3%
      block：两边变动的百分点差不多（两边都是 604）；或者买方是 603、它的全部持股 ≈ 卖方这次卖掉的
    返回 {name, in_list, id, date, cd, type, vp, url, kind, gap, me: {...}, other: {...}}；找不到返回 {}。宁缺毋错：日期或数字不全的不配。"""
    me = holding_move(e, prev_vp)
    cd = e.get("change_date") or ""
    tr = e.get("trades") if isinstance(e.get("trades"), dict) else {}
    px = e.get("px") if isinstance(e.get("px"), dict) else {}
    qty = max(tr.get("buy_qty") or 0, tr.get("sell_qty") or 0)
    # 这次的量是不是集中在一天（或者比全市场成交量还多）：只有这样，“百分点差不多”才说明是同一笔交易。
    # 大盘股天天有指数基金进进出出，变动的百分点碰巧接近很常见（PLS：AustralianSuper 分 10 天买，同期 JPMorgan、State Street 各卖了差不多的量）
    conc = bool((me or {}).get("side") and insight.block_of(tr.get("daily"), px.get("adv"), me["side"])) or \
        bool(px.get("t_vol") and qty and qty >= px["t_vol"])
    try:
        d0 = dt.date.fromisoformat(cd)
    except ValueError:
        return {}
    if not me:
        return {}
    rank = {"whole": 0, "shares": 1, "block": 2}
    best = []
    for o in by_code.get(code, []):
        if o is e or o.get("id") == e.get("id") or o.get("status") != "parsed":
            continue
        try:
            gap = abs((dt.date.fromisoformat(o.get("change_date") or "") - d0).days)
        except ValueError:
            continue
        if gap > CP_DAYS:
            continue
        o_holder, _ = match_entry(o, holders)
        if o_holder == holder or _same_org(o.get("holder_name") or "", e.get("holder_name") or ""):
            continue                                      # 同一家机构换了个实体名申报，不是对手方
        other = holding_move(o)
        if not other or other["side"] == me["side"]:
            continue
        seller, buyer = (me, other) if me["side"] == "sell" else (other, me)
        kind = ""
        if (seller["pre"] and buyer["post"] and seller["pre"] >= CP_WHOLE_MIN and _close(seller["pre"], buyer["post"], 0.015, 0.06)
                and (seller["post"] is None or seller["post"] <= seller["pre"] - CP_MIN_DELTA)):
            kind = "whole"
        if not kind and me["exact"] and other["exact"]:
            mv, ov = e.get("votes") or [], o.get("votes") or []
            if (len(mv) == 2 and len(ov) == 2 and all(isinstance(v, int) and v > 0 for v in mv + ov)
                    and _close(abs(mv[1] - mv[0]), abs(ov[1] - ov[0]), 0.03, 0) and abs(mv[1] - mv[0]) > 0):
                kind = "shares"
            elif conc and me["d"] >= CP_MIN_DELTA and _close(me["d"], other["d"], 0.10, 0.15):
                kind = "block"
        if not kind and conc and seller["exact"] and buyer["pre"] is None and buyer["post"] and seller["d"] >= CP_MIN_DELTA \
                and _close(seller["d"], buyer["post"], 0.10, 0.2):
            kind = "block"                                # 买方是 603，它的全部持股差不多就是卖方这次卖掉的
        if kind:
            best.append((rank[kind], gap, o, o_holder, kind, other))
    if not best:
        return {}
    best.sort(key=lambda x: (x[0], x[1]))
    _, gap, o, o_holder, kind, other = best[0]
    name = o_holder or clean_holder_name(o.get("holder_name") or "")
    others = {id(x[2]) for x in best[1:] if x[0] == best[0][0]}
    if others and kind == "block":
        return {}                                         # 好几家都“差不多对得上”：说不准是哪一家，宁可不说
    txt = lambda v: v if isinstance(v, str) else ""
    return {"name": name, "in_list": bool(o_holder), "id": txt(o.get("id")), "date": txt(o.get("date")), "cd": txt(o.get("change_date")),
            "type": o.get("type"), "vp": _vp_of(o), "url": safe_url(o.get("url") or ""), "kind": kind, "gap": gap,
            "me": me, "other": other, "more": len(others)}


def build_notices(entries: dict, holders: list[Holder], history: dict | None = None, reports: dict | None = None) -> list[dict]:
    out = []
    group_of = {h.name: h.group for h in holders}
    style_of = {h.name: h.style for h in holders}
    last_seen: dict[tuple, dict] = {}        # (申报方, 股票) → 上一份公告的要点，给“操作解读”做参照
    try:
        habit_of = habits(history)
    except Exception as ex:  # noqa: BLE001  习惯统计出错不能影响公告本身
        log(f"  基金习惯统计出错：{type(ex).__name__}: {ex}")
        habit_of = {}
    by_code: dict[str, list[dict]] = {}                 # 所有大股东公告（不只名单里的）按股票分组，给倒手识别用
    for e in entries.values():
        c = headline_target(e.get("headline", ""), e.get("code", "")) or e.get("code", "")
        if c:
            by_code.setdefault(c, []).append(e)
    for e in sorted(entries.values(), key=lambda e: (e.get("date") or "", e.get("time") or "", str(e.get("id")))):
        holder, conf = match_entry(e, holders)
        if holder:
            item = {k: e.get(k) for k in PUBLIC_FIELDS}
            target = headline_target(e.get("headline", ""), e.get("code", ""))
            item["code"], item["lodged_code"] = (target, e.get("code", "")) if target else (e.get("code", ""), "")
            item["company"] = clean_company(item.get("company") or "")
            item["url"] = safe_url(item.get("url") or "")
            item["pdf_url"] = safe_url(item.get("pdf_url") or "") or item["url"]
            item.update(holder=holder, group=group_of.get(holder, ""), confidence=conf, ocr=bool(e.get("ocr")),
                        sector=e.get("sector") or "")
            key = (holder, item["code"])
            prev = last_seen.get(key) or prev_from_history(history, holder, item["code"], e)
            try:
                item["insight"] = insight.judge({
                    "type": e.get("type"), "voting_power": e.get("voting_power") or [], "votes": e.get("votes") or [],
                    "trades": e.get("trades") or {}, "px": e.get("px") or {}, "ocr": e.get("ocr"),
                    "style": style_of.get(holder, "active"), "prev": prev,
                    "date": e.get("date"),
                    "cost": cost_for(history, holder, item["code"], e), "ev": e.get("ev") or [],
                    "change_date": e.get("change_date"), "seq": sequence_for(history, holder, item["code"], e),
                    "tl": e.get("tl") or {}, "stage": stage_for(history, holder, item["code"], e),
                    "habit": habit_of.get(holder) or {}, "code": item["code"],
                    "own": is_own_product(holder, item["code"], e.get("company") or "", style_of.get(holder, "active")),
                })
            except Exception as ex:  # noqa: BLE001  解读出错不能影响公告本身的显示
                log(f"  {item['code']} 操作解读出错：{type(ex).__name__}: {ex}")
                item["insight"] = None
            if item.get("insight"):
                try:
                    cp = find_counterparty(e, item["code"], holder, (prev or {}).get("vp"), by_code, holders)
                    if cp:
                        insight.add_counterparty(item["insight"], cp, holder)
                except Exception as ex:  # noqa: BLE001  倒手识别出错不能影响公告本身
                    log(f"  {item['code']} 倒手识别出错：{type(ex).__name__}: {ex}")
            vp, votes = e.get("voting_power") or [], e.get("votes") or []
            last_seen[key] = {"vp": vp[-1] if vp else None, "votes": votes[-1] if votes else None,
                              "type": e.get("type"), "date": e.get("date")}
            out.append(item)
    out.sort(key=lambda n: (n.get("date") or "", n.get("time") or "", n.get("id") or ""), reverse=True)
    # 保险：同一份公告从列表页和接口各来一条、时间又差了一分钟时，按内容再去重一次（优先保留列表页那条）
    seen, uniq = set(), []
    for n in sorted(out, key=lambda n: str(n.get("id", "")).startswith("mk-")):
        key = (n["holder"], n.get("code"), n.get("type"), n.get("change_date"), tuple(n.get("voting_power") or ()))
        if n.get("change_date") and n.get("voting_power") and key in seen:
            continue
        seen.add(key)
        uniq.append(n)
    uniq.sort(key=lambda n: (n.get("date") or "", n.get("time") or "", n.get("id") or ""), reverse=True)
    final = drop_superseded(uniq)
    try:
        insight.add_context(final)
    except Exception as ex:  # noqa: BLE001  横向比较出错不能影响公告本身
        log(f"  横向比较出错：{type(ex).__name__}: {ex}")
    for n in final:
        if n.get("insight") and reports:
            try:
                quotes = fund_quotes(reports, n.get("holder") or "", n.get("code") or "", n.get("company") or "", n.get("date") or "")
            except Exception as ex:  # noqa: BLE001
                log(f"  月报摘句出错：{type(ex).__name__}: {ex}")
                quotes = []
            if quotes:
                ins = n["insight"]
                ins["quotes"] = quotes
                if any(q["text"] for q in quotes):
                    ins.setdefault("tags", []).append("有基金原话")
                for q in quotes:
                    if q["text"]:
                        insight._add_reason(ins, f"基金自己的说法（{q['fund']}《{q['title']}》，{q['date']}，英文原文）：“{q['text']}”")
                    else:
                        insight._add_reason(ins, f"这只股票出现在它的月报（{q['fund']}《{q['title']}》，{q['date']}）的持仓表里，但没有文字说明。")
    for n in final:
        if n.get("insight"):
            f = n["insight"].pop("_f", None)      # 打分用的中间数据不输出
            if f:
                n["insight"]["side"] = f["side"]
    return final


AMENDED_RE = re.compile(r"\b(?:amend\w*|correct\w*|replace\w*|revised|updated)\b", re.I)


def drop_superseded(notices: list[dict]) -> list[dict]:
    """更正版（标题带 Amended / Correction / Replacement）发出后，隐藏被它更正的原件：
    同一申报方、同一股票、同一类型、比例相同，原件在更正版之前 3 天以内。"""
    drop = set()
    for a in notices:
        if not AMENDED_RE.search(a.get("headline") or ""):
            continue
        for b in notices:
            if b is a or b["id"] in drop or AMENDED_RE.search(b.get("headline") or ""):
                continue
            if (b["holder"], b.get("code"), b.get("type")) != (a["holder"], a.get("code"), a.get("type")):
                continue
            if (b.get("voting_power") or []) != (a.get("voting_power") or []):
                continue
            try:
                gap = (dt.date.fromisoformat(a["date"]) - dt.date.fromisoformat(b["date"])).days
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= gap <= 3 and (b.get("date"), b.get("time") or "") <= (a.get("date"), a.get("time") or ""):
                drop.add(b["id"])
    return [n for n in notices if n["id"] not in drop]


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
        self.lock = threading.Lock()

    def take(self) -> None:
        with self.lock:
            if self.left <= 0 or time.monotonic() > self.deadline:
                raise Deferred()
            self.left -= 1
            self.used += 1


def blank(result: dict | None) -> bool:
    """什么都没认出来：没有申报方名称，也没找到第一节。"""
    return result is None or not (result.get("holder_name") or result.get("region"))


def process_entry(e: dict, ocr_budget: OCRBudget | None = None, want_detail=None) -> None:
    """下载并解析一份公告。文字层什么都认不出来时（扫描件，或“打字封面信 + 扫描表格”），
    用 OCR 再读一遍。ocr_budget 为 None 时不做 OCR；名额或时间用完时抛 Deferred。
    want_detail(e) 返回真时（命中关注名单），顺手把交易明细也读了，省得再下载一次。"""
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
    for k in ("votes", "trades", "detail_v", "px", "px_v"):      # 重新解析后，旧的明细和股价背景作废
        e.pop(k, None)
    if want_detail is not None and want_detail(e):
        try:
            add_detail(e, data, texts)
        except Exception as ex:  # noqa: BLE001  明细读不出不能影响主体解析
            log(f"    {e.get('code')} 交易明细读取出错：{type(ex).__name__}: {ex}")


def trade_pages(data: bytes, max_pages: int | None = None) -> list[str]:
    """交易明细用：前 N 页的版面文字。只在命中关注名单时才读（附件长，读起来慢）。"""
    max_pages = TRADE_PAGES if max_pages is None else max_pages
    t0 = time.monotonic()
    out = []
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            return []
        for page in reader.pages[:max_pages]:
            try:
                out.append(clean_text(page.extract_text(extraction_mode="layout") or ""))
            except Exception:  # noqa: BLE001  某一页读不出不影响其他页
                out.append("")
            if time.monotonic() - t0 > TRADE_SECONDS:
                break
    except Exception as e:  # noqa: BLE001
        log(f"    读交易明细失败（{type(e).__name__}: {e}）")
    return out


def add_detail(e: dict, data: bytes, texts: list[str] | None = None) -> None:
    """命中关注名单的公告：再读持股数量（第二节）和逐笔交易明细，供“操作解读”用。"""
    ntype, vp = e.get("type", "other"), e.get("voting_power") or []
    votes = []
    if not e.get("ocr"):
        for t in (texts if texts is not None else pdf_texts(data)):
            votes = insight.parse_votes(voting_section(t)[0], vp, ntype)
            if votes:
                break
    e["votes"] = votes
    # 扫描件的逐笔数字靠 OCR 认，错一位就差很远，不读明细
    e["trades"] = {} if e.get("ocr") else insight.parse_trades(trade_pages(data), e.get("date", ""), ntype)["summary"]
    e["prev_notice"] = next((d for d in (parse_prev_notice(t) for t in (texts if texts is not None else pdf_texts(data))) if d), "")
    e["detail_v"] = INSIGHT_VERSION


PREV_NOTICE_RE = re.compile(r"previous\s+notice\s+was\s+(given\s+to\s+the\s+company\s+on|dated)\s*:?\s*([^\n]{4,40})", re.I)


def parse_prev_notice(text: str) -> str:
    """604/605 开头写的“上一份公告是哪天交给公司的”（没有就用“上一份公告的日期”）。回补持仓历史时顺着它往前找。"""
    found = {}
    for m in PREV_NOTICE_RE.finditer(text or ""):
        d = parse_date(m.group(2))
        if d:
            found.setdefault("given" if m.group(1).lower().startswith("given") else "dated", d)
    return found.get("given") or found.get("dated") or ""


def detail_tries(e: dict) -> int:
    """这一版读法下补读过几次。读法升级后从 0 重新数（以前不清零，升级几次后就再也不重读了）。"""
    return e.get("detail_tries", 0) if e.get("detail_try_v") == INSIGHT_VERSION else 0


def enrich_entry(e: dict) -> None:
    """给已经解析过的公告补读明细（新加进关注名单、或明细读法升级时）。"""
    data, _ = download_pdf(e["url"])
    add_detail(e, data)


def target_code(e: dict) -> str:
    """公告涉及的股票代码（申报方自己上市时，标的写在标题的 “for XXX” 里）。"""
    return headline_target(e.get("headline", ""), e.get("code", "")) or e.get("code", "")


def price_series(code: str) -> list:
    r = fetch(PRICE_API.format(code=code), timeout=40, tries=2)
    try:
        data = json.loads(r.text)
    except ValueError as e:
        raise FetchError(f"股价接口返回的不是 JSON（{e}）") from e
    if not isinstance(data, list):
        raise FetchError("股价接口返回的格式不对")
    return data


EVENT_KINDS = (     # 顺序有意义：先认更具体的
    ("buyback", re.compile(r"buy[- ]?back", re.I)),
    ("takeover", re.compile(r"takeover|take-over|scheme\s+(?:of\s+arrangement|booklet|meeting|implementation|consideration|becomes)"
                            r"|bidder'?s?\s+statement|target'?s?\s+statement|(?:non[- ]binding|indicative|unsolicited|conditional|revised)\s+"
                            r"(?:indicative\s+)?(?:proposal|offer)|proposal\s+(?:from|to\s+acquire|received)|acquisition\s+proposal"
                            r"|off[- ]market\s+(?:takeover\s+)?bid|\bmerger\b|compulsory\s+acquisition", re.I)),
    ("raise", re.compile(r"placement|capital\s+raising|equity\s+raising|entitlement\s+offer|rights\s+issue|share\s+purchase\s+plan|\bSPP\b"
                         r"|proposed\s+issue\s+of\s+securities|institutional\s+offer|retail\s+offer|\braises?\s+\$|\braising\b"
                         r"|prospectus|recapitali[sz]ation|convertible\s+note", re.I)),
    ("results", re.compile(r"appendix\s+4[DE]\b|half[- ]?year(?:ly)?\s+(?:results?|report|accounts|financial)|full[- ]?year\s+(?:results?|financial)"
                           r"|annual\s+results?|\b[FH1-4QY]{1,2}Y?\d{2,4}\s+(?:results?|financial\s+results?)|quarterly\s+(?:activit|report|cash|update)"
                           r"|appendix\s+4C\b|appendix\s+5B\b|trading\s+update|guidance|profit\s+(?:update|downgrade|upgrade|warning)"
                           r"|earnings\s+(?:update|guidance)|business\s+update|results?\s+announcement", re.I)),
)
EVENT_SKIP_RE = re.compile(r"results?\s+of\s+(?:annual\s+general\s+)?meeting|results?\s+briefing|results?\s+(?:call|webcast|date)|date\s+of|notice\s+of"
                           r"|presentation\s+date|investor\s+call\s+details|\badvisory\b|\bto\s+be\s+released", re.I)
MAX_EVENTS = 60


def classify_event(headline: str) -> str:
    if EVENT_SKIP_RE.search(headline) or SUBSTANTIAL_RE.search(headline):
        return ""
    return next((k for k, rx in EVENT_KINDS if rx.search(headline)), "")


def parse_company_events(html: str) -> list[dict] | None:
    """读“某公司近 6 个月公告”页面，挑出融资、业绩、收购、回购这几类。返回 [{d, k, h, s}]（按日期升序）；页面认不出返回 None。"""
    soup = BeautifulSoup(html, "html.parser")
    out, rows = [], 0
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        a = tr.find("a", href=re.compile(r"idsId|displayAnnouncement|asxpdf", re.I))
        if len(tds) < 3 or a is None:
            continue
        dm = DATE_RE.search(tds[0].get_text(" ", strip=True))
        if not dm:
            continue
        try:
            d = dt.date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))
        except ValueError:
            continue
        rows += 1
        headline = TAIL_RE.sub("", a.get_text(" ", strip=True)).strip()
        k = classify_event(headline)
        if k:
            out.append({"d": d.isoformat(), "k": k, "h": headline[:90], "s": is_price_sensitive(tr, tds)})
    if not rows and not re.search(r"no\s+announcements|Search\s+results", html, re.I):
        return None
    # 回购期间每天一份“Update - Notification of buy-back”：同一类同一天只留一条，回购只留头尾
    seen, uniq = set(), []
    for ev in sorted(out, key=lambda x: (x["d"], not x["s"])):
        if (ev["d"], ev["k"]) in seen:
            continue
        seen.add((ev["d"], ev["k"]))
        uniq.append(ev)
    bb = [e for e in uniq if e["k"] == "buyback"]
    if len(bb) > 2:
        keep = {id(bb[0]), id(bb[-1])}
        bb[-1]["n"] = len(bb)
        uniq = [e for e in uniq if e["k"] != "buyback" or id(e) in keep]
    return uniq[-MAX_EVENTS:]


def company_events(code: str) -> list[dict]:
    r = fetch(COMPANY_ANNS.format(code=code), timeout=40, tries=2)
    evs = parse_company_events(r.text)
    if evs is None:
        raise FetchError("公司公告页面认不出来")
    return evs


def company_sector(code: str) -> str:
    r = fetch(COMPANY_HEADER.format(code=code), timeout=30, tries=2)
    try:
        return str(((json.loads(r.text) or {}).get("data") or {}).get("sector") or "")[:40]
    except (ValueError, AttributeError) as e:
        raise FetchError(f"公司信息接口返回的不是 JSON（{e}）") from e


def events_near(evs: list[dict], e: dict) -> list[dict]:
    """只留这份公告用得上的事件：从最早一笔交易前 45 天，到公告当天。"""
    tr = e.get("trades") or {}
    try:
        end = dt.date.fromisoformat(e.get("date") or "")
        start = dt.date.fromisoformat(tr.get("first") or e.get("change_date") or e.get("date") or "")
    except ValueError:
        return []
    lo, hi = (start - dt.timedelta(days=45)).isoformat(), end.isoformat()
    return [x for x in evs if lo <= x["d"] <= hi][-20:]


def entry_side(e: dict) -> str:
    return _rec_dir({"type": e.get("type"), "votes": e.get("votes"), "vp": e.get("voting_power")})


def add_timeline(e: dict, series: list) -> None:
    """逐笔时间线和公告后的涨跌（都要用日线）。"""
    tr = e.get("trades") or {}
    side = entry_side(e)
    e["tl"] = insight.timeline(series, tr.get("daily") or [], side) if side else {}
    e["fwd"] = insight.forward_returns(series, e.get("date") or "")


def add_prices(entries: list[dict]) -> tuple[int, int]:
    """给这些公告补上变动时的股价背景、公司同期的大事（融资/业绩/收购/回购）和所属行业（每只股票只取一次）。
    返回 (取到的股票数, 失败的股票数)。大事和行业取不到不算失败，只是解读里少一条依据。"""
    by_code: dict[str, list[dict]] = {}
    for e in entries:
        by_code.setdefault(target_code(e), []).append(e)

    def one(code: str) -> bool:
        try:
            series = price_series(code)
        except FetchError as ex:
            log(f"  股价没取到 {code}：{ex}")
            for e in by_code[code]:
                e["px_tries"] = e.get("px_tries", 0) + 1
            return False
        for e in by_code[code]:
            e.pop("ev", None)
            tr = e.get("trades") or {}
            ref = e.get("change_date") or tr.get("last") or e.get("date", "")
            e["px"] = insight.price_context(series, ref, tr.get("first", ""), tr.get("last", ""))
            e["px_v"] = INSIGHT_VERSION
            add_timeline(e, series)
        time.sleep(PAUSE)
        try:
            evs = company_events(code)
            insight.event_moves(series, evs)
            for e in by_code[code]:
                e["ev"] = events_near(evs, e)
        except Exception as ex:  # noqa: BLE001
            log(f"  公司公告没取到 {code}：{type(ex).__name__}: {ex}")
        time.sleep(PAUSE)
        try:
            sector = company_sector(code)
            for e in by_code[code]:
                e["sector"] = sector
        except Exception as ex:  # noqa: BLE001
            log(f"  行业没取到 {code}：{type(ex).__name__}: {ex}")
        time.sleep(PAUSE)
        return True

    codes = sorted(c for c in by_code if re.fullmatch(r"[A-Z0-9]{3,6}", c or ""))
    if not codes:
        return 0, 0
    with ThreadPoolExecutor(max_workers=max(1, min(WORKERS, 4)), thread_name_prefix="px") as pool:
        res = list(pool.map(one, codes))
    return sum(res), len(res) - sum(res)


def hinted(e: dict, holders: list[Holder]) -> bool:
    return any(hint_hit(h, e.get("hint", "")) for h in holders)


def now_syd() -> dt.datetime:
    return dt.datetime.now(SYD)


def api_since(store: dict, now: dt.datetime) -> dt.datetime | None:
    """增量补抓的起点：上次完整同步到的时间。以下情况返回 None（把整个窗口重新翻一遍）：
    从没同步过、同步点已经在窗口以外、或者距离上次整窗口翻查超过 API_FULL_EVERY_DAYS 天。"""
    try:
        synced = dt.datetime.fromisoformat(store.get("api_synced_at") or "")
        full = dt.datetime.fromisoformat(store.get("api_full_at") or "")
    except (TypeError, ValueError):
        return None
    if synced.tzinfo is None or full.tzinfo is None or synced > now:
        return None
    if now - full > dt.timedelta(days=API_FULL_EVERY_DAYS):
        return None
    if synced.date() <= (now - dt.timedelta(days=BACKFILL_DAYS)).date():
        return None
    return synced


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
    since = api_since(store, now)
    try:
        api_rows, cov = market_rows(now, since)
        rows.extend(api_rows)
        backfill["ok"] = True
        backfill["incremental"] = cov["incremental"]
        if cov["oldest"]:
            window_start = (now - dt.timedelta(days=BACKFILL_DAYS)).date()
            if cov["incremental"] and cov["reached"]:
                # 增量：上次已经同步到 since，这次从 since 之前接着翻到现在，两段连起来是完整的。
                # since 那天上次只拿到一部分，现在补全了；更早的日子上次已经记过
                first_full = since.date()
                backfill["from"] = store.get("api_from") or first_full.isoformat()
            else:
                # 接口按时间倒序，翻页在最早那天的中间停下，那天只拿到一部分；从第二天起才是完整的
                first_full = max(cov["oldest"].date() + dt.timedelta(days=1), window_start)
                backfill["from"] = first_full.isoformat()
            if cov["complete"]:
                store["api_synced_at"] = cov["newest"].isoformat(timespec="seconds")
                store["api_from"] = backfill["from"]
                if not cov["incremental"]:
                    store["api_full_at"] = now.isoformat(timespec="seconds")
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

    skipped = deferred = 0
    ocr_budget = OCRBudget()
    work = []
    for e in todo:
        if is_noise(e.get("hint", ""), holders, noise):
            e["status"] = "noise"
            skipped += 1
        elif len(work) >= MAX_PDFS:
            deferred += 1
        else:
            work.append(e)

    def is_matched(e: dict) -> bool:
        return bool(match_entry(e, holders)[0])

    # 命中关注名单、但还没读过交易明细的旧公告（新加进名单的申报方，或明细读法升级后）：补读一次
    in_work = {id(e) for e in work}
    enrich = [e for e in entries.values()
              if e.get("status") == "parsed" and id(e) not in in_work and e.get("detail_v") != INSIGHT_VERSION
              and detail_tries(e) < DETAIL_TRIES and is_matched(e)]
    enrich.sort(key=lambda e: (e.get("date", ""), e.get("time", "")), reverse=True)
    room = max(MAX_PDFS - len(work), 0)
    deferred += max(len(enrich) - room, 0)
    enrich = enrich[:room]

    def handle_enrich(e: dict) -> bool:
        e["detail_tries"] = detail_tries(e) + 1
        e["detail_try_v"] = INSIGHT_VERSION
        try:
            enrich_entry(e)
            return True
        except Exception as ex:  # noqa: BLE001  补读失败只影响“操作解读”，下次再试
            log(f"  明细补读失败 {e.get('code')} {e.get('headline')}：{type(ex).__name__}: {ex}")
            return False
        finally:
            time.sleep(PAUSE)

    def handle(e: dict) -> str:
        """处理一份公告，返回 done / failed / deferred。每个线程只改自己手上的那条记录。"""
        e["attempts"] = e.get("attempts", 0) + 1
        result = "done"
        try:
            process_entry(e, ocr_budget, is_matched)
        except Deferred:
            e["attempts"] -= 1                   # 不算一次失败，下次运行再识别
            return "deferred"
        except PermanentFetchError as ex:
            e["status"], e["error"] = "skipped", str(ex)
        except FetchError as ex:
            e["status"], e["error"] = "error", str(ex)
            result = "failed"
            log(f"  ! {e['code']} {e['headline']}：{ex}")
        except Exception as ex:  # noqa: BLE001  单份公告解析出错不能拖垮整次运行
            e["status"], e["error"] = "error", f"{type(ex).__name__}: {ex}"
            result = "failed"
            log(f"  ! {e['code']} {e['headline']}：解析出错\n{traceback.format_exc()}")
        e["checked_at"] = dt.datetime.now(SYD).isoformat(timespec="seconds")
        time.sleep(PAUSE)
        return result

    t_pdf = time.monotonic()
    if WORKERS > 1 and len(work) + len(enrich) > 1:
        with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="pdf") as pool:
            results = list(pool.map(handle, work))
            enriched = sum(pool.map(handle_enrich, enrich))
    else:
        results = [handle(e) for e in work]
        enriched = sum(handle_enrich(e) for e in enrich)
    checked = sum(r != "deferred" for r in results)
    failed = results.count("failed")
    deferred += results.count("deferred")
    log(f"处理 {checked} 份 PDF 用时 {time.monotonic() - t_pdf:.0f} 秒（{WORKERS} 路并行）")

    if failed:
        errors.append(f"{failed} 份公告 PDF 下载失败，下次运行会自动重试（每份最多 {MAX_ATTEMPTS} 次）")
    if deferred:
        log(f"本次已达下载或文字识别上限，剩余 {deferred} 份留到下次运行")
    log(f"文字识别（OCR）{ocr_budget.used} 份扫描件")

    # 操作解读要用的股价背景：只给命中名单、已读过明细、还没有股价的公告取
    t_px = time.monotonic()
    need_px = [e for e in entries.values()
               if e.get("status") == "parsed" and e.get("detail_v") == INSIGHT_VERSION
               and e.get("px_v") != INSIGHT_VERSION and e.get("px_tries", 0) < DETAIL_TRIES and is_matched(e)]
    px_ok, px_failed = add_prices(need_px)
    if need_px:
        log(f"股价背景：{len(need_px)} 条公告、{px_ok + px_failed} 只股票，取到 {px_ok} 只，用时 {time.monotonic() - t_px:.0f} 秒")

    history = load_json(HISTORY_FILE, {})
    if not isinstance(history, dict) or not isinstance(history.get("pos"), dict):
        history = {"pos": {}}
    hist_added = update_history(history, entries, holders, today)
    bf = Backfill(history, holders, today)
    t_bf = time.monotonic()
    try:
        bf.run()
        bf.refresh_old()
        bf.refresh_forward()
    except Exception as ex:  # noqa: BLE001  回补出错不能影响当天的抓取
        log(f"回补持仓历史出错：{type(ex).__name__}: {ex}")
    pending = sum(1 for k in history["pos"] if not (history.get("meta", {}).get(k) or {}).get("done"))
    if bf.read:
        log(f"回补持仓历史：读了 {bf.read} 份旧公告，补进 {bf.added} 条，按新读法重读 {bf.refreshed} 条，还有 {pending} 个持仓没追完，用时 {time.monotonic() - t_bf:.0f} 秒")
    try:
        n_mkt = add_market(entries, history)
        if n_mkt:
            log(f"大盘对照：补了 {n_mkt} 条")
    except Exception as ex:  # noqa: BLE001  取不到指数就照旧用股票自己的涨跌
        log(f"大盘指数没取到：{type(ex).__name__}: {ex}")
    reports = load_json(REPORTS_FILE, {})
    if not isinstance(reports, dict):
        reports = {}
    try:
        n_rep = update_fund_reports(reports, today)
        if n_rep:
            log(f"基金月报：新读了 {n_rep} 份，共存 {len(reports.get('reports') or [])} 份")
    except Exception as ex:  # noqa: BLE001
        log(f"基金月报出错：{type(ex).__name__}: {ex}")
    try:
        n_x = add_forward_excess(history)
        if n_x:
            log(f"超额涨跌：补了 {n_x} 条")
    except Exception as ex:  # noqa: BLE001
        log(f"超额涨跌出错：{type(ex).__name__}: {ex}")
    try:
        calibration = calibrate(history, holders, today)
    except Exception as ex:  # noqa: BLE001
        log(f"回测统计出错：{type(ex).__name__}: {ex}")
        calibration = {}

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

    notices = build_notices(entries, holders, history, reports)
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
            "insight": {"detail_added": enriched + sum(1 for e in work if e.get("detail_v") == INSIGHT_VERSION),
                        "prices_ok": px_ok, "prices_failed": px_failed,
                        "history_added": hist_added, "positions": len(history["pos"]),
                        "backfill_read": bf.read, "backfill_added": bf.added, "history_refreshed": bf.refreshed, "backfill_pending": pending},
            "errors": errors[:10],
        },
        "watchlist": [h.name for h in holders],
        "styles": {h.name: insight.STYLES[h.style][0] for h in holders},
        "groups": {h.name: h.group for h in holders},
        "calibration": calibration,
        "notices": notices,
    }
    write_json(STORE_FILE, store)
    write_json(HISTORY_FILE, history)
    write_json(REPORTS_FILE, reports)
    write_json(CALIBRATION_FILE, calibration, pretty=True)
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
