"""
操作判断：从公告 PDF 的交易明细、持股数量变化、基金类型和股价位置，推断申报方这次在干什么。

三步：
1. parse_trades()：读 604 第 3 节 / 603 第 5 节 / 605 第 2 节以及附件里的逐笔交易表，汇总成买卖统计。
2. （可选）股价背景：由 scrape.py 传入，见 price_context()。
3. judge()：一组写死的规则，输出 {kind, label, tone, confidence, reasons}。

这里只做规则判断，不调用任何 AI。规则给的是“最可能的解释”和依据，不是事实——基金为什么买卖，公告里不会写。
"""

from __future__ import annotations

import datetime as dt
import re
import statistics

# ---------------------------------------------------------------- 逐笔交易表

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
D_NUM = r"\d{1,2}\s?[/.\-]\s?\d{1,2}\s?[/.\-]\s?(?:\d{4}|\d{2})"
D_MON = r"\d{1,2}(?:st|nd|rd|th)?[\s\-/.]+[A-Za-z]{3,9}\.?,?[\s\-/.]+(?:\d{4}|\d{2})"
D_US = r"[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
D_ISO = r"\d{4}-\d{2}-\d{2}"
DATE_TOKEN_RE = re.compile(rf"(?<![\d/.\-])(?:{D_ISO}|{D_NUM}|{D_MON}|{D_US})(?![\d/])")
ROW_START_RE = re.compile(rf"^\W{{0,3}}({D_ISO}|{D_NUM}|{D_MON}|{D_US})(?![\d/])")

# 这些行里的日期不是交易日期
NOT_TRADE_RE = re.compile(
    r"previous\s*notice|substantial\s*(?:share)?holder\s*on|sign\s*here|\bpage\s+\d+\s+of\b|15\s+July\s+2001"
    r"|this\s+is\s+annexure|annexure\s+.{0,40}\s+of\s+\d+\s+pages?|\bdated\b|capacity|print\s*name"
    r"|date\s+of\s+(?:change|acquisition)\b.*(?:nature|consideration|person)", re.I)

BUY_RE = re.compile(
    r"\b(?:buy|bought|purchas\w*|acqui\w*|subscri\w*|allot\w*|appl(?:y|ied|ication)|increase[ds]?|placement|entitlement"
    r"|rights\s+issue|\bspp\b|share\s+purchase\s+plan|transfer(?:red)?\s+in|in[- ]?specie\s+(?:transfer\s+)?in|\bbot\b)\b", re.I)
SELL_RE = re.compile(
    r"\b(?:sell|sold|sale[s]?|dispos\w*|decrease[ds]?|redemption|redeem\w*|reduc\w*|transfer(?:red)?\s+out"
    r"|in[- ]?specie\s+(?:transfer\s+)?out|\bsld\b)\b", re.I)
KIND_RES = (            # 顺序有意义：先认特殊性质，再认普通场内交易
    ("lending", re.compile(r"securit(?:y|ies)\s+lend|stock\s+lend|\bborrow|\bloan(?:ed)?\b|\blent\b|collateral|recall|rehypothec|prime\s+brok", re.I)),
    ("takeover", re.compile(r"takeover|bidder|\boffer\s+(?:acceptance|period)|acceptance\s+of|scheme\s+of\s+arrangement|implementation\s+of\s+the\s+scheme|compulsor", re.I)),
    ("placement", re.compile(r"placement|entitlement|rights\s+issue|\bspp\b|share\s+purchase\s+plan|subscri\w+|allot\w+|\bipo\b|initial\s+public|capital\s+rais|conversion|exercise\s+of|issue\s+of\s+(?:new\s+)?(?:shares|securities)", re.I)),
    ("transfer", re.compile(r"in[- ]?specie|transfer|transition|change\s+of\s+(?:manager|custodian|trustee|mandate|responsible)|novation|restructur|appointment|mandate", re.I)),
    ("dilution", re.compile(r"dilut\w+|diloution|issue\s+of\s+shares\s+by\s+the\s+company|increase\s+in\s+(?:issued|share)\s+capital|change\s+in\s+(?:issued|share)\s+capital|buy[- ]?back", re.I)),
    ("off_market", re.compile(r"off[- ]?market|block\s+trade|crossing|special\s+crossing", re.I)),
    ("on_market", re.compile(r"on[- ]?market|on\s+the\s+market|market\s+(?:purchase|sale|trade)|\bbuy\b|\bsell\b|\bsale\b|\bsold\b|purchas|\bbot\b|\bsld\b", re.I)),
)
MONEY_RE = re.compile(r"(?:AUD|A\$|US\$|\$)\s*\(?-?\s*(\d{1,3}(?:,\s?\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\)?", re.I)
NUM_RE = re.compile(r"(?<![\w.,/])(\(?-?\s?)(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(\)?)(?![\w/]|[.,]\d)")
MAX_TRADE_ROWS = 2000


def _year(y: int) -> int:
    return y + 2000 if y < 100 else y


def _mk(y: int, m: int, d: int) -> dt.date | None:
    try:
        return dt.date(_year(y), m, d)
    except ValueError:
        return None


def date_candidates(tok: str) -> list[dt.date]:
    """一个日期写法可能的解释：[日/月/年, 月/日/年]（文字月份和 ISO 只有一种）。"""
    tok = tok.strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", tok)
    if m:
        return [x for x in [_mk(int(m[1]), int(m[2]), int(m[3]))] if x]
    m = re.fullmatch(r"(\d{1,2})\s?[/.\-]\s?(\d{1,2})\s?[/.\-]\s?(\d{2,4})", tok)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        return [x for x in (_mk(y, b, a), _mk(y, a, b)) if x]
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?[\s\-/.]+([A-Za-z]{3,9})\.?,?[\s\-/.]+(\d{2,4})", tok)
    if m and m[2][:3].lower() in MONTHS:
        return [x for x in [_mk(int(m[3]), MONTHS[m[2][:3].lower()], int(m[1]))] if x]
    m = re.fullmatch(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", tok)
    if m and m[1][:3].lower() in MONTHS:
        return [x for x in [_mk(int(m[3]), MONTHS[m[1][:3].lower()], int(m[2]))] if x]
    return []


def _num(s: str) -> float:
    return float(s.replace(",", "").replace(" ", ""))


def _row_numbers(rest: str) -> tuple[list[float], list[tuple[float, bool]]]:
    """返回 (带 $ 的金额列表, 其他数字列表[(值, 是否负数)])。"""
    money = [_num(m.group(1)) for m in MONEY_RE.finditer(rest)]
    stripped = MONEY_RE.sub(" ", rest)
    nums = []
    for m in NUM_RE.finditer(stripped):
        neg = "-" in m.group(1) or (m.group(1).startswith("(") and m.group(3) == ")")
        nums.append((_num(m.group(2)), neg))
    return money, nums


def _kind(text: str) -> str:
    for k, rx in KIND_RES:
        if rx.search(text):
            return k
    return "other"


def parse_trades(pages: list[str], notice_date: str = "", ntype: str = "") -> dict:
    """pages：每页的版面文字。返回 {"rows": [...], "summary": {...}}；认不出交易表时 rows 为空。

    一行交易 = 以日期开头的一行（性质说明可能折到后面一两行）。每行取：日期、买/卖、性质、数量、对价。
    """
    try:
        ndate = dt.date.fromisoformat(notice_date)
    except (TypeError, ValueError):
        ndate = dt.date.today()
    lines = []
    for pg in pages:
        lines.extend(ln for ln in (pg or "").split("\n"))
    raw = []
    for idx, ln in enumerate(lines):
        m = ROW_START_RE.match(ln)
        if not m or NOT_TRADE_RE.search(ln):
            continue
        cands = [d for d in date_candidates(m.group(1)) if ndate - dt.timedelta(days=1100) <= d <= ndate + dt.timedelta(days=1)]
        if not cands:
            continue
        rest = ln[m.end():]
        # 日期范围 “13/10/26 - 29/10/26”：把第二个日期也去掉
        m2 = re.match(r"\s*(?:-|–|to)\s*(" + DATE_TOKEN_RE.pattern + ")", rest)
        if m2:
            rest = rest[m2.end():]
        cont = []
        for nxt in lines[idx + 1: idx + 4]:           # 折行的性质说明
            if ROW_START_RE.match(nxt) or re.match(r"\s*\d+\s*\.\s+[A-Z]", nxt):
                break
            cont.append(nxt)
        raw.append({"tok": m.group(1), "cands": cands, "rest": rest, "cont": " ".join(cont)})
        if len(raw) >= MAX_TRADE_ROWS:
            break
    if not raw:
        return {"rows": [], "summary": {}}

    # 整份公告用同一种日期写法：只要有一行只能按 月/日 解释，就全部按 月/日
    us = any(len(r["cands"]) == 1 and re.match(r"\d{1,2}\s?[/.\-]\s?(1[3-9]|2\d|3[01])\s?[/.\-]", r["tok"]) for r in raw)
    rows = []
    for r in raw:
        c = r["cands"]
        date = c[-1] if (us and len(c) == 2) else c[0]
        text = r["rest"] + " " + r["cont"]
        money, nums = _row_numbers(r["rest"])
        ints = [(v, neg) for v, neg in nums if v >= 1 and float(v).is_integer()]
        qty = neg = None
        if ints:
            qty, neg = ints[-1]
            # “数量”和“表决权数”两列通常相等；取最后一个整数
        b, s_ = bool(BUY_RE.search(r["rest"])), bool(SELL_RE.search(r["rest"]))
        if not (b or s_):
            b, s_ = bool(BUY_RE.search(text)), bool(SELL_RE.search(text))
        side = "sell" if (neg or (s_ and not b)) else "buy" if (b and not s_) else ""
        cons = money[0] if money else None
        if cons is None:
            # 没有 $ 的对价：数量前面的那个数（带小数，或者和数量不同的数）
            before = [v for v, _ in nums[:-1]] if nums else []
            dec = [v for v in before if not float(v).is_integer()]
            if dec:
                cons = dec[-1]
            elif len(before) >= 1 and qty is not None and before[-1] != qty:
                cons = before[-1]
            elif len(before) >= 2 and before[-1] == qty and before[-2] != qty:
                cons = before[-2]
        rows.append({"date": date.isoformat(), "side": side, "kind": _kind(text), "qty": qty, "cons": cons})
    return {"rows": rows, "summary": summarise(rows, ntype)}


def _per_share(rows: list[dict]) -> bool | None:
    """对价一栏填的是“每股价格”还是“总金额”？用哪种解释下各行价格更一致来判断。认不出返回 None。"""
    pairs = [(r["cons"], r["qty"]) for r in rows if r.get("cons") and r.get("qty")]
    if not pairs:
        return None
    if any(c > 1000 for c, _ in pairs):          # ASX 没有每股上千澳元的股票
        return False
    if all(c / q < 0.0005 for c, q in pairs):     # 按总金额算每股不到 0.05 分：只能是每股价格
        return True
    if len(pairs) >= 3:
        def cv(v):
            m = statistics.fmean(v)
            return statistics.pstdev(v) / m if m else 9e9
        return cv([c for c, _ in pairs]) < cv([c / q for c, q in pairs])
    return None


def summarise(rows: list[dict], ntype: str = "") -> dict:
    usable = [r for r in rows if r.get("qty")]
    if not usable:
        return {}
    # 603 第 5 节列的是“成为大股东前 4 个月买入的对价”，没写买卖方向的按买入算
    for r in usable:
        if not r["side"] and ntype == "becoming" and r["kind"] in ("other", "on_market", "placement"):
            r["side"] = "buy"
    per_share = _per_share(usable)
    buys = [r for r in usable if r["side"] == "buy"]
    sells = [r for r in usable if r["side"] == "sell"]

    def vwap(rs):
        if per_share is None:
            return None
        tot_q = sum(r["qty"] for r in rs if r.get("cons"))
        if not tot_q:
            return None
        tot_v = sum((r["cons"] * r["qty"]) if per_share else r["cons"] for r in rs if r.get("cons"))
        p = tot_v / tot_q
        return round(p, 4) if 0.0005 <= p <= 1000 else None

    kinds = {}
    for r in usable:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + r["qty"]
    dates = sorted({r["date"] for r in usable})
    out = {
        "n": len(usable), "n_buy": len(buys), "n_sell": len(sells), "n_unknown": len(usable) - len(buys) - len(sells),
        "buy_qty": int(sum(r["qty"] for r in buys)), "sell_qty": int(sum(r["qty"] for r in sells)),
        "first": dates[0], "last": dates[-1], "days": len(dates),
        "buy_price": vwap(buys), "sell_price": vwap(sells),
        "kinds": {k: int(v) for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])},
    }
    out["net_qty"] = out["buy_qty"] - out["sell_qty"]
    return out
