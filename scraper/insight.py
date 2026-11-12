"""
操作判断：从公告 PDF 的交易明细、持股数量变化、基金类型和股价位置，推断申报方这次在干什么。

三步：
1. parse_trades()：读 604 第 3 节 / 603 第 5 节 / 605 第 2 节以及附件里的逐笔交易表，汇总成买卖统计。
2. （可选）股价背景：由 scrape.py 传入，见 price_context()。
3. judge()：一组写死的规则，输出 {kind, label, tone, confidence, reasons}。

第十一轮加了四类证据（都在 judge 末尾的 6b 节）：趋势结构（长期和短期方向相反）、持仓阶段（stage，来自持仓历史）、
基金习惯（habit，来自持仓历史）、交易形状（block_of，一天之内的大宗）。

这里只做规则判断，不调用任何 AI。规则给的是“最可能的解释”和依据，不是事实——基金为什么买卖，公告里不会写。
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
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
ROW_DATE_RE = re.compile(rf"(?<![\d/.\-])({D_ISO}|{D_NUM}|{D_MON}|{D_US})(?![\d/])")
MAX_PREFIX = 130        # 日期前面最多允许这么长的文字（603 附件常把持有人名称写在日期前面）

# 这些行里的日期不是交易日期
NOT_TRADE_RE = re.compile(
    r"previous\s*notice|substantial\s*(?:share)?holder\s*on|sign\s*here|\bpage\s+\d+\s+of\b|15\s+July\s+2001"
    r"|this\s+is\s+annexure|annexure\s+.{0,40}\s+of\s+\d+\s+pages?|\bdated\b|\bdate\s*:|capacity|print\s*name"
    r"|became\s+aware|as\s+at\b|based\s+on|issued\s+capital\s+of|last\s+disclosure|announced\s+on"
    r"|(?:opening|closing)\s+(?:balance|position|holding)|\bbalance\b"
    r"|date\s+of\s+(?:change|acquisition)\b.*(?:nature|consideration|person)", re.I)

BUY_RE = re.compile(
    r"\b(?:buy|bought|purchas\w*|acqui\w*|subscri\w*|allot\w*|appl(?:y|ied|ication)|increase[ds]?|placement|entitlement"
    r"|rights\s+issue|\bspp\b|share\s+purchase\s+plan|transfer(?:red)?\s+in|in[- ]?specie\s+(?:transfer\s+)?in|\bbot\b)\b", re.I)
SELL_RE = re.compile(
    r"\b(?:sell|sold|sale[s]?|dispos\w*|decrease[ds]?|redemption|redeem\w*|reduc\w*|transfer(?:red)?\s+out"
    r"|in[- ]?specie\s+(?:transfer\s+)?out|\bsld\b)\b", re.I)
# 公司自己的股本变化（不是申报方的买卖）：这种行里的数字是公司发了多少股，不能算进申报方的买卖量
ISSUER_RE = re.compile(r"dilut\w+|diloution|issue\s+of\s+[\d,]+\s|total\s+shares\s+on\s+issue|increase\s+in\s+(?:the\s+)?(?:company'?s\s+)?"
                       r"(?:issued|share)\s+capital|change\s+in\s+(?:issued|share)\s+capital|issue\s+of\s+shares\s+by\s+the\s+company", re.I)
GLUED_SIDE_RE = re.compile(r"(?<=[a-z])(Buy|Sell|Sale|Purchase)\b")
CONT_STOP_RE = re.compile(r"^\W*(?:annexure|this\s+is|signature|print\s*name|sign\s*here|page\s+\d|\d+\s*\.\s+[A-Z])", re.I)
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
NUM_RE = re.compile(r"(?<![\w.,/])(\(?-?\s?)(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(\)?-?)(?![\w/]|[.,]\d)")   # 负数：-12,000 / (12,000) / 12,000-
MAX_TRADE_ROWS = 2000
MAX_DAILY = 150


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
        neg = "-" in m.group(1) or m.group(3).endswith("-") or (m.group(1).startswith("(") and m.group(3).startswith(")"))
        nums.append((_num(m.group(2)), neg))
    return money, nums


def _kind(text: str) -> str:
    for k, rx in KIND_RES:
        if rx.search(text):
            return k
    return "other"


def row_start(ln: str):
    """这一行是不是一笔交易：返回日期的匹配对象，否则 None。日期在行首，或者前面只有一段不含长数字的文字。"""
    m = ROW_START_RE.match(ln)
    if m:
        return m
    m = ROW_DATE_RE.search(ln)
    if m and m.start() <= MAX_PREFIX and not re.search(r"\d{3}", ln[:m.start()]) and re.search(r"\d", ln[m.end():]):
        return m
    return None


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
    last_row = -1
    for idx, ln in enumerate(lines):
        m = row_start(ln)
        if not m or NOT_TRADE_RE.search(ln):
            continue
        if not ln[m.end():].strip() and m.start() <= 3:
            continue                                  # 单独一行只有日期（折行说明里的“29 Jan 2026”）：不是交易行
        cands = [d for d in date_candidates(m.group(1)) if ndate - dt.timedelta(days=1100) <= d <= ndate + dt.timedelta(days=1)]
        if not cands:
            continue
        rest = ln[m.end():]
        lead = ln[:m.start()] if m.start() > 3 else ""      # 日期前面的文字（可能含“Sale”“Purchase”）
        # 日期范围 “13/10/26 - 29/10/26”：把第二个日期也去掉
        m2 = re.match(r"\s*(?:-|–|to)\s*(" + DATE_TOKEN_RE.pattern + ")", rest)
        if m2:
            rest = rest[m2.end():]
        cont = []
        for nxt in lines[idx + 1: idx + 4]:           # 折行的性质说明（写在日期行下面）
            if row_start(nxt) or CONT_STOP_RE.match(nxt):
                break
            cont.append(nxt)
        pre = []
        for back in range(idx - 1, max(last_row, idx - 6), -1):   # 有的表格把说明写在日期行上面（单元格顶端对齐）
            prv = lines[back]
            if row_start(prv) or CONT_STOP_RE.match(prv) or NOT_TRADE_RE.search(prv):
                break
            pre.insert(0, prv)
        last_row = idx
        raw.append({"tok": m.group(1), "cands": cands, "rest": rest, "cont": " ".join(cont), "pre": " ".join(pre), "lead": lead})
        if len(raw) >= MAX_TRADE_ROWS:
            break
    if not raw:
        return {"rows": [], "summary": {}}

    # 整份公告用同一种日期写法：只要有一行只能按 月/日 解释，就全部按 月/日
    us = any(len(r["cands"]) == 1 and re.match(r"\d{1,2}\s?[/.\-]\s?(1[3-9]|2\d|3[01])\s?[/.\-]", r["tok"]) for r in raw)
    # 说明写在日期行的上面还是下面：看哪一边能给更多“自己这行没写买卖”的行找到买卖方向，整份公告用同一种
    side_rx = lambda x: bool(BUY_RE.search(x) or SELL_RE.search(x))
    bare = [r for r in raw if not side_rx(r["lead"] + " " + r["rest"])]
    above = sum(side_rx(r["pre"]) for r in bare) > sum(side_rx(r["cont"]) for r in bare)
    rows = []
    for r in raw:
        c = r["cands"]
        date = c[-1] if (us and len(c) == 2) else c[0]
        text = (r["pre"] + " " + r["lead"] + " " + r["rest"]) if above else (r["lead"] + " " + r["rest"] + " " + r["cont"])
        money, nums = _row_numbers(r["rest"])
        ints = [(v, neg) for v, neg in nums if v >= 1 and float(v).is_integer()]
        qty = neg = None
        if ints:
            qty, neg = ints[-1]
            # “数量”和“表决权数”两列通常相等；取最后一个整数
        b, s_ = bool(BUY_RE.search(r["rest"])), bool(SELL_RE.search(r["rest"]))
        if not (b or s_):
            b, s_ = bool(BUY_RE.search(text)), bool(SELL_RE.search(text))
        if not (b or s_):                           # “SecuritiesBuy”：两栏粘在一起
            g = GLUED_SIDE_RE.search(r["rest"])
            b, s_ = bool(g and g.group(1) in ("Buy", "Purchase")), bool(g and g.group(1) in ("Sell", "Sale"))
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
        own = r["lead"] + " " + r["rest"]
        kind = _kind(own)
        if kind == "other":
            kind = _kind(text)                      # 这一行自己没写性质，再看折行的说明
        if kind == "other" and r["cont"]:
            # 一句话被拆成两行，中间还夹着金额和股数：“…pursuant of the Capital 6,832,300.50 ORD 9,109,734 / raising announced …”
            words = re.sub(r"(?<!\w)[-$]*[\d,.]+%?(?!\w)|\b(?:ORD|FPO|FPOS|ORDINARY)\b", " ", own, flags=re.I)
            kind = _kind(" ".join(words.split()) + " " + r["cont"][:60])
        if ISSUER_RE.search(text):
            kind, qty, cons, side = "dilution", None, None, ""
        rows.append({"date": date.isoformat(), "side": side, "kind": kind, "qty": qty, "cons": cons})
    summary = summarise(rows, ntype)
    kind_rows = {}
    for r in rows:
        kind_rows[r["kind"]] = kind_rows.get(r["kind"], 0) + 1
    summary["kind_rows"] = kind_rows          # 每种性质有几行（含没写数量的行，例如 “Dilution … Nil”）
    return {"rows": rows, "summary": summary}


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
        return {"n": 0}
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
    # 每天的净买卖量（买为正、卖为负），给“逐笔时间线”用；只留最近 MAX_DAILY 天
    daily: dict[str, int] = {}
    for r in buys:
        daily[r["date"]] = daily.get(r["date"], 0) + int(r["qty"])
    for r in sells:
        daily[r["date"]] = daily.get(r["date"], 0) - int(r["qty"])
    out["daily"] = [[d, q] for d, q in sorted(daily.items()) if q][-MAX_DAILY:]
    return out


# ---------------------------------------------------------------- 第二节：持股数量

BIG_INT_RE = re.compile(r"(?<![\d.,$])(\d{1,3}(?:,\d{3})+|\d{4,})(?![\d,]*\.\d)(?![\d,])")
ANY_PCT_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,8})?)\s?%?")


def parse_votes(seg: str, vp: list[float], ntype: str) -> list[int]:
    """从第二节“持股比例”原文里取和比例同一行的持股数量（Person's votes）。
    变动类返回 [之前, 现在]，新进类返回 [现在]；认不出返回 []。

    只认“数量紧挨在比例前面”的写法：`76,406,237 11.36% 64,019,970 9.52%`。
    """
    if not seg or not vp:
        return []
    want = vp[:2] if ntype == "change" else vp[:1]
    for ln in seg.split("\n"):
        toks = []                                             # (位置, 类型, 值)
        for m in BIG_INT_RE.finditer(ln):
            toks.append((m.start(), "n", int(m.group(1).replace(",", ""))))
        for m in ANY_PCT_RE.finditer(ln):
            v = float(m.group(1))
            if any(abs(v - w) < 1e-9 for w in want):
                toks.append((m.start(), "p", v))
        toks.sort()
        out, pos = [], 0
        for w in want:
            idx = next((i for i in range(pos, len(toks)) if toks[i][1] == "p" and abs(toks[i][2] - w) < 1e-9), None)
            if idx is None:
                break
            prev_n = next((toks[i][2] for i in range(idx - 1, pos - 1, -1) if toks[i][1] == "n"), None)
            if prev_n is None:
                break
            out.append(prev_n)
            pos = idx + 1
        if len(out) == len(want):
            return out
    return []


def shares_outstanding(votes: int | None, pct: float | None) -> float | None:
    """由“持股数量 ÷ 持股比例”倒推公司总股本。"""
    if not votes or not pct:
        return None
    return votes / (pct / 100.0)


# ---------------------------------------------------------------- 股价背景

def price_context(series: list, ref: str, first: str = "", last: str = "") -> dict:
    """series：[[毫秒时间戳, 收盘价, 成交量], ...]（ASX 日线，按时间升序）。
    ref：参考日（变动日）。返回参考日的收盘价、之前 1/3/6/12 个月涨跌幅、在一年高低区间里的位置（0=最低，1=最高），
    以及交易明细那段时间（first–last）的最低/最高收盘价和日均成交量。数据不够时相应字段为 None。"""
    pts = []
    for row in series or []:
        try:
            d = dt.datetime.fromtimestamp(row[0] / 1000, dt.timezone.utc).date()
            p = float(row[1])
        except (TypeError, ValueError, IndexError, OverflowError, OSError):
            continue
        if p > 0:
            pts.append((d, p, float(row[2]) if len(row) > 2 and row[2] else 0.0))
    pts.sort()
    try:
        rd = dt.date.fromisoformat(ref)
    except (TypeError, ValueError):
        return {}
    upto = [x for x in pts if x[0] <= rd]
    if not upto or (rd - upto[-1][0]).days > 10:
        return {}
    p = upto[-1][1]

    def back(days: int):
        cut = rd - dt.timedelta(days=days)
        older = [x for x in upto if x[0] <= cut]
        if not older or (cut - older[-1][0]).days > 10:
            return None
        return round(p / older[-1][1] - 1, 4)

    year = [x[1] for x in upto if x[0] >= rd - dt.timedelta(days=365)]
    hi, lo = max(year), min(year)
    out = {"ref": upto[-1][0].isoformat(), "p": round(p, 4), "r1m": back(30), "r3m": back(91), "r6m": back(182),
           "r12m": back(365), "hi": round(hi, 4), "lo": round(lo, 4),
           "pos": round((p - lo) / (hi - lo), 3) if hi > lo and len(year) >= 60 else None}
    vols = [x[2] for x in upto[-60:] if x[2] > 0]
    out["adv"] = int(sum(vols) / len(vols)) if len(vols) >= 20 else None       # 近 60 个交易日的日均成交量
    try:
        f, l = dt.date.fromisoformat(first), dt.date.fromisoformat(last)
        win = [x[1] for x in pts if f - dt.timedelta(days=3) <= x[0] <= l + dt.timedelta(days=3)]
        if win:
            out["t_lo"], out["t_hi"] = round(min(win), 4), round(max(win), 4)
        vol = [x[2] for x in pts if f <= x[0] <= l]
        if vol and pts[0][0] <= f and sum(vol) > 0:
            out["t_vol"] = int(sum(vol))          # 交易明细那段时间全市场的成交量（股）
    except (TypeError, ValueError):
        pass
    return out


def _points(series: list) -> list[tuple[str, float]]:
    pts = []
    for row in series or []:
        try:
            d = dt.datetime.fromtimestamp(row[0] / 1000, dt.timezone.utc).date().isoformat()
            p = float(row[1])
        except (TypeError, ValueError, IndexError, OverflowError, OSError):
            continue
        if p > 0:
            pts.append((d, p))
    pts.sort()
    return pts


def timeline(series: list, daily: list, side: str) -> dict:
    """逐笔时间线：把每天的净买卖量和当天的股价涨跌对起来看。side = buy/sell，只看这个方向的交易日。
    返回 {n: 用上的交易日数, down_share: 这个方向的量有多少发生在股价下跌的日子, base_down: 同期下跌日占比（对照），
           r_win: 从第一笔到最后一笔股价的涨跌, accel: 后一半的量 ÷ 前一半的量}；不到 4 个交易日返回 {}。"""
    pts = _points(series)
    if len(pts) < 3:
        return {}
    idx = {d: i for i, (d, _) in enumerate(pts)}
    sign = 1 if side == "buy" else -1
    rows = []
    for item in daily or []:
        try:
            d, q = item[0], float(item[1]) * sign
        except (TypeError, ValueError, IndexError):
            continue
        i = idx.get(d)
        if q > 0 and i:                       # i == 0（没有前一天）也不用
            rows.append((d, q, pts[i][1] / pts[i - 1][1] - 1))
    if len(rows) < 4:
        return {}
    tot = sum(q for _, q, _ in rows)
    i0, i1 = idx[rows[0][0]], idx[rows[-1][0]]
    moves = [pts[i][1] / pts[i - 1][1] - 1 for i in range(max(i0, 1), i1 + 1)]
    half = len(rows) // 2
    first, second = sum(q for _, q, _ in rows[:half]), sum(q for _, q, _ in rows[half:])
    return {"n": len(rows), "down_share": round(sum(q for _, q, m in rows if m < 0) / tot, 3),
            "base_down": round(sum(1 for m in moves if m < 0) / len(moves), 3) if moves else None,
            "r_win": round(pts[i1][1] / pts[max(i0 - 1, 0)][1] - 1, 3),
            "accel": round(_clip(second / first, 0, 9.99), 2) if first > 0 else None}


def market_returns(pts: list[tuple[str, float]], ref: str) -> dict:
    """大盘在 ref 之前 1/3/6/12 个月的涨跌。pts：按日期排好的 (日期, 收盘)。算不出的不给。"""
    days = [d for d, _ in pts]
    i = bisect.bisect_right(days, ref or "") - 1
    if i < 0 or _days_between(days[i], ref) > 10:
        return {}
    out = {}
    for k, n in (("m1", 30), ("m3", 91), ("m6", 182), ("m12", 365)):
        j = bisect.bisect_right(days, _shift(ref, -n)) - 1
        if j >= 0 and _days_between(days[j], _shift(ref, -n)) <= 10:
            out[k] = round(pts[i][1] / pts[j][1] - 1, 3)
    return out


def forward_returns(series: list, ref: str) -> dict:
    """ref 之后 1/3/6 个月股价涨跌（还没到的不给）。"""
    pts = _points(series)
    days = [d for d, _ in pts]
    i = bisect.bisect_right(days, ref or "") - 1
    if i < 0 or _days_between(days[i], ref) > 10:
        return {}
    out = {}
    for k, n in (("1m", 30), ("3m", 91), ("6m", 182)):
        target = _shift(ref, n)
        if days[-1] < target:
            continue
        j = bisect.bisect_right(days, target) - 1
        out[k] = round(pts[j][1] / pts[i][1] - 1, 3)
    return out


def event_moves(series: list, evs: list[dict]) -> None:
    """给每件公司大事补上“消息出来后股价动了多少”（原地加 j 字段）：公告前一个交易日的收盘 → 公告日之后第二个收盘
    （盘后才发的公告，反应在第二天）。日线里找不到对应日期的不加。"""
    pts = []
    for row in series or []:
        try:
            d = dt.datetime.fromtimestamp(row[0] / 1000, dt.timezone.utc).date().isoformat()
            p = float(row[1])
        except (TypeError, ValueError, IndexError, OverflowError, OSError):
            continue
        if p > 0:
            pts.append((d, p))
    pts.sort()
    days = [d for d, _ in pts]
    for ev in evs:
        i = bisect.bisect_left(days, ev.get("d") or "")          # 公告日当天或之后的第一个交易日
        if i == 0 or i >= len(pts) or _days_between(days[i], ev["d"]) > 5:
            continue
        after = pts[min(i + 1, len(pts) - 1)][1]
        ev["j"] = round(after / pts[i - 1][1] - 1, 3)


# ---------------------------------------------------------------- 判断

STYLES = {
    "active": ("主动选股基金", "基金经理自己挑股票，增减持通常反映对这家公司的看法"),
    "hedge": ("多空对冲基金", "既做多也做空，仓位变化快，一部分买卖可能是对冲或短线交易"),
    "strategic": ("战略/家族投资者", "用自有资金长期持有，有时会谋求话语权，增减持的分量比较重"),
    "super": ("养老金", "钱来自会员供款，持仓常是多个组合加在一起，不少买卖只是资产配置或资金进出，不一定代表看法"),
    "platform": ("平台/多经理", "把钱分给很多外部经理和指数组合，合计持仓的变化信号最弱"),
}
LABELS = {
    "build": "新进建仓", "cross_up": "加仓越过 5% 线", "build_placement": "参与配售建仓", "add_dip": "逢低加仓", "add_chase": "顺势加仓", "add": "加仓",
    "add_placement": "参与配售加仓", "trim_profit": "减仓，像获利了结", "trim_loss": "减仓，像止损",
    "trim": "减仓", "exit_profit": "减到 5% 以下，像获利了结", "exit_loss": "减到 5% 以下，像止损离场",
    "exit": "减到 5% 以下", "slip": "小幅卖出，刚好跌破 5% 线", "back": "又回到 5% 以上（在线附近来回）",
    "rotate": "边买边卖，像调仓", "passive_dilute": "被动稀释（没怎么卖）",
    "passive_up": "被动上升（没怎么买）", "takeover": "收购/并购方案引起", "lending": "证券借贷/抵押变动",
    "transfer": "划转或换管理人", "arb": "收购消息后买入，像并购套利",
    "add_flow": "加仓（多只股票一起加，像有新资金）", "swap_in": "换股调入（卖别的、买这只）", "short_in": "卖出不久又买回，像短线来回",
    "trim_flow": "减仓（多只股票一起减，像应付赎回）", "exit_flow": "减到 5% 以下（多只一起减，像应付赎回）",
    "swap_out": "换股调出（卖这只、买别的）", "trim_rebal": "涨多了减一点，像仓位管理", "short_out": "买入不久就卖出，像短线交易",
    "add_pull": "回调时加仓（长期在涨）", "add_prog": "持续加仓，像按计划建仓", "trim_bounce": "趁反弹减仓", "exit_bounce": "趁反弹减到 5% 以下",
    "trim_wind": "分批减仓，已减掉三成以上", "exit_wind": "分几次减到 5% 以下", "restructure": "股份合并/拆分", "unknown": "看不出来",
}
TONE = {"build": "bull", "cross_up": "bull", "build_placement": "bull", "add_dip": "bull", "add_chase": "bull", "add": "bull",
        "add_placement": "bull", "trim_profit": "bear", "trim_loss": "bear", "trim": "bear", "exit_profit": "bear",
        "exit_loss": "bear", "exit": "bear", "add_flow": "bull", "swap_in": "bull", "swap_out": "bear",
        "add_pull": "bull", "add_prog": "bull", "trim_bounce": "bear", "exit_bounce": "bear", "trim_wind": "bear", "exit_wind": "bear"}


def fq(n: float) -> str:
    """股数的中文写法：1,239 万股 / 8.5 万股 / 9,499 股。"""
    n = abs(n)
    if n >= 1e8:
        return f"{n / 1e8:.2f} 亿股"
    if n >= 1e6:
        return f"{n / 1e4:,.0f} 万股"
    if n >= 1e4:
        return f"{n / 1e4:.1f} 万股"
    return f"{n:,.0f} 股"


def fp(p: float) -> str:
    return f"${p:,.3f}" if p < 1 else f"${p:,.2f}"


def fr(r: float) -> str:
    return f"{r * 100:+.0f}%"


def md(iso: str, year: bool = False) -> str:
    try:
        d = dt.date.fromisoformat(iso)
        return f"{d.year}/{d.month}/{d.day}" if year else f"{d.month}/{d.day}"
    except (TypeError, ValueError):
        return iso or ""


def span_text(first: str, last: str) -> str:
    if not first or first == last:
        return md(last or first)
    y = first[:4] != last[:4]
    return f"{md(first, y)}–{md(last, y)}"


def pos_word(pos: float) -> str:
    return "低位" if pos <= 0.25 else "中低位" if pos <= 0.45 else "中间" if pos < 0.6 else "中高位" if pos < 0.8 else "高位"


def _shift(iso: str, days: int) -> str:
    try:
        return (dt.date.fromisoformat(iso) + dt.timedelta(days=days)).isoformat()
    except (TypeError, ValueError):
        return iso or ""


def _days_between(a: str | None, b: str | None) -> int:
    try:
        return abs((dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days)
    except (TypeError, ValueError):
        return 10 ** 6


TARGET_JUMP = 0.08        # 并购消息后两天内股价涨 8% 以上，才认为公司是被收购的一方
COST_MIN_COVER = 0.4      # 已知成本的股数至少占持仓的四成，才拿来判断赚还是亏


def cost_basis(records: list[dict], holding: int | None = None) -> dict:
    """由同一（申报方, 股票）之前各份公告里的买卖，按“移动平均成本”推算平均买入价。
    records：按时间升序，每条 {date, buy_qty, sell_qty, buy_price, sell_price, p（变动日收盘价）, votes}。
    没写价格的买入用当时的收盘价顶替。holding：这次变动之前的持股数，用来算“已知成本的部分占多少”。
    返回 {avg, cov, since, n}；一笔有效买入都没有时返回 {}。"""
    shares = cost = 0.0
    since, used = "", 0
    for r in records:
        bq, sq = r.get("buy_qty") or 0, r.get("sell_qty") or 0
        price = r.get("buy_price") or r.get("p")
        if bq > 0 and price and price > 0:
            cost += bq * price
            shares += bq
            since = since or (r.get("first") or r.get("date") or "")
            used += 1
        if sq > 0 and shares > 0:
            keep = max(1 - sq / shares, 0.0)        # 卖出按平均成本同比例减掉
            shares, cost = shares * keep, cost * keep
        v = r.get("votes") or []
        if v and shares > v[-1] * 1.05:             # 累计出来的股数比披露的持仓还多：中间有没看到的卖出，按比例压回去
            k = v[-1] / shares
            shares, cost = shares * k, cost * k
    if shares < 1 or not used:
        return {}
    cov = min(shares / holding, 1.0) if holding and holding > 0 else None
    return {"avg": round(cost / shares, 4), "cov": round(cov, 3) if cov is not None else None, "since": since, "n": used}


BLOCK_SHARE = 0.5         # 一天的量占这个方向总量的一半以上
BLOCK_ADV = 5.0           # 而且是平时日均成交量的 5 倍以上：算“一次性大宗”


def block_of(daily: list, adv, side: str) -> dict:
    """交易形状（第十一轮）：这个方向的量是不是集中在一天、而且远超平时的日成交量。
    daily：[[日期, 当天净买卖量], ...]；adv：近 60 个交易日的日均成交量。返回 {d, top: 占比, x: 是日均量的几倍}，不是大宗返回 {}。"""
    sign = 1 if side == "buy" else -1
    rows = []
    for item in daily if isinstance(daily, list) else []:
        try:
            q = float(item[1]) * sign
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if 0 < q < 1e15:
            rows.append((q, str(item[0])))
    if not rows or not _is_num(adv) or adv <= 0:
        return {}
    tot = sum(q for q, _ in rows)
    q, d = max(rows)
    if q / tot >= BLOCK_SHARE and q / adv >= BLOCK_ADV:
        return {"d": d, "top": round(q / tot, 3), "x": round(min(q / adv, 999.0), 1)}
    return {}


def history_record(e: dict) -> dict:
    """把一条公告压成“持仓历史”里的一行（长期保存，用来累计买入成本）。读得不可靠的买卖数字不记。"""
    tr, px, votes = dict(e.get("trades") or {}), e.get("px") or {}, e.get("votes") or []
    buy, sell = tr.get("buy_qty") or 0, tr.get("sell_qty") or 0
    if e.get("type") == "becoming" and votes and buy - sell > votes[0] * 1.05:
        buy = sell = 0
    bp, sp = tr.get("buy_price"), tr.get("sell_price")
    if px.get("t_lo") and px.get("t_hi"):
        ok = lambda v: v is not None and px["t_lo"] * 0.6 <= v <= px["t_hi"] * 1.6
        bp, sp = (bp if ok(bp) else None), (sp if ok(sp) else None)
    rec = {"id": e.get("id"), "date": e.get("date") or "", "time": e.get("time") or "", "type": e.get("type"),
           "cd": e.get("change_date") or "", "vp": list(e.get("voting_power") or []), "votes": list(votes),
           "buy_qty": int(buy), "sell_qty": int(sell), "buy_price": bp, "sell_price": sp, "p": px.get("p"),
           "first": tr.get("first") or ""}
    # 下面这些是为了以后能对旧公告重新打分、做回测
    net = (votes[1] - votes[0]) if e.get("type") == "change" and len(votes) == 2 else buy - sell
    side = "buy" if e.get("type") == "becoming" else "sell" if e.get("type") == "ceasing" else "buy" if net > 0 else "sell" if net < 0 else ""
    blk = block_of(tr.get("daily"), px.get("adv"), side) if side and (buy or sell) else {}
    if blk:
        rec["blk"] = blk
    tr.pop("daily", None)
    if tr:
        rec["tr"] = tr
    if px:
        rec["px"] = px
    for k in ("tl", "fwd", "prev_notice"):
        if e.get(k):
            rec[k] = e[k]
    if e.get("ocr"):
        rec["ocr"] = True
    return rec


BUY_ACTIONS = {"build", "cross_up", "build_placement", "add_dip", "add_chase", "add", "add_placement", "add_pull", "add_prog"}
SELL_ACTIONS = {"trim_profit", "trim_loss", "trim", "exit_profit", "exit_loss", "exit", "trim_bounce", "exit_bounce", "trim_wind", "exit_wind"}
CTX_DAYS = 21             # “同期”：前后三周
PEER_DAYS = 30


def _add_reason(ins: dict, text: str, tag: str = "") -> None:
    """把一条理由插到“基金类型”那句前面，并记一个小标签给网页用。"""
    rs = ins["reasons"]
    idx = next((i for i, r in enumerate(rs) if r.startswith(ins.get("style", "\0") + "：")), len(rs))
    rs.insert(idx, text)
    if tag:
        ins.setdefault("tags", []).append(tag)


def _names(xs: list[str], limit: int = 4, unit: str = "只") -> str:
    xs = sorted(set(xs))
    return "、".join(xs[:limit]) + (f"（共 {len(xs)} {unit}）" if len(xs) > limit else "")


SECTORS = {"Materials": "原材料/矿业", "Energy": "能源", "Financials": "金融", "Industrials": "工业", "Health Care": "医疗",
           "Information Technology": "科技", "Consumer Discretionary": "可选消费", "Consumer Staples": "必需消费",
           "Communication Services": "通信传媒", "Real Estate": "房地产", "Utilities": "公用事业"}
SINGLE_VIEW = ("主动选股基金", "多空对冲基金", "战略/家族投资者")      # 这几类的持仓是一个团队管的，“换股”才说得通


def _main_sector(ns: list[dict]) -> str:
    """这些公告里（按股票去重）占多数的行业；不够集中返回空。"""
    by_code = {n["code"]: n.get("sector") or "" for n in ns}
    secs = [v for v in by_code.values() if v]
    if len(secs) < 2:
        return ""
    top = max(set(secs), key=secs.count)
    return top if secs.count(top) * 2 > len(by_code) else ""


def add_context(notices: list[dict]) -> None:
    """横向比较（原地修改 notices 里的 insight）：
    1. 同一家基金前后三周在别的股票上做了什么——都在卖像整体降仓/应付赎回，有买有卖像换股；
    2. 同一只股票上，名单里别的基金最近是同向还是反向。"""
    def side(n):
        ins = n.get("insight") or {}
        if ins.get("_f"):
            return ins["_f"]["side"]
        a = ins.get("action")
        return "buy" if a in BUY_ACTIONS else "sell" if a in SELL_ACTIONS else ""

    def gap(a, b):
        return _days_between(a.get("date"), b.get("date"))

    def sec(name):
        return SECTORS.get(name, name)

    dated = [n for n in notices if n.get("insight") and side(n) and n.get("date") and n.get("code")]
    plan = []                                     # 先全部算完再写，避免前面加的理由影响后面的判断
    ctxs = []
    for n in dated:
        me, ins = side(n), n["insight"]
        ctx = {}
        ctxs.append((ins, ctx))
        single = ins.get("style") in SINGLE_VIEW
        others = [o for o in dated if o is not n and o["holder"] == n["holder"] and o["code"] != n["code"] and gap(n, o) <= CTX_DAYS]
        buys = [o for o in others if side(o) == "buy"]
        ob = sorted({o["code"] for o in buys})
        sells = [o for o in others if side(o) == "sell" and o["code"] not in ob]
        os_ = sorted({o["code"] for o in sells})
        mine = n.get("sector") or ""
        ctx.update(nb=len(ob), ns=len(os_))
        other_sec = _main_sector(buys if me == "sell" else sells)
        ctx["rot"] = bool(single and mine and other_sec and other_sec != mine)
        if me == "sell":
            if len(os_) >= 2 and not ob:
                plan.append((ins, f"前后三周它在 {_names(os_)} 也在减仓，没看到加仓：更像整体降仓或应付赎回，不一定是单独看空这一只。", "整体减仓"))
            elif ob:
                to = _main_sector(buys)
                rot = "" if not single else (f"卖的是{sec(mine)}、买的多是{sec(to)}，像行业轮动。" if mine and to and to != mine
                                             else "而且买的多是同一个行业的，像是在行业里挑了更看好的。" if mine and to == mine else "")
                plan.append((ins, f"前后三周它在 {_names(ob)} 加仓：这边卖、那边买，" + ("像是换股（调仓）。" if single else "整体并没有在撤。") + rot,
                             "换股" if single else ""))
        else:
            if len(ob) >= 2 and not os_:
                plan.append((ins, f"前后三周它在 {_names(ob)} 也在加仓，没看到减仓：可能有新资金进来，在整体加仓。", "整体加仓"))
            elif os_:
                frm = _main_sector(sells)
                rot = "" if not single else (f"卖的多是{sec(frm)}、买的是{sec(mine)}，像行业轮动。" if mine and frm and frm != mine
                                             else "而且卖的多是同一个行业的，像是在行业里挑了更看好的这只。" if mine and frm == mine else "")
                plan.append((ins, f"前后三周它在 {_names(os_)} 减仓：" + ("像是把钱从那边挪到这只（调仓）。" if single else "有买有卖，属于正常调整。") + rot,
                             "换股" if single else ""))
        peers = [o for o in dated if o is not n and o["holder"] != n["holder"] and o["code"] == n["code"] and gap(n, o) <= PEER_DAYS]
        same = sorted({o["holder"] for o in peers if side(o) == me})
        opp = sorted({o["holder"] for o in peers if side(o) != me} - set(same))
        word = "加仓" if me == "buy" else "减仓"
        ctx.update(peer_same=len(same), peer_opp=len(opp))
        if same:
            plan.append((ins, f"同一只股票上，名单里的 {_names(same, 3, '家')} 最近一个月也在{word}：多家同向，信号更强。", "多家同向"))
        if opp:
            plan.append((ins, f"同一只股票上，名单里的 {_names(opp, 3, '家')} 最近一个月却在{'减仓' if me == 'buy' else '加仓'}：机构之间看法不一。", "有人反向"))
    for ins, text, tag in plan:
        _add_reason(ins, text, tag)
    for ins, ctx in ctxs:                         # 有了横向证据，重新打一次分
        finalize(ins, ctx)


def judge(n: dict) -> dict | None:
    """n：一条公告的字段——type, voting_power, votes, trades(summary), px, ocr, style, prev（上一份公告的 [比例, 持股数]，
    只有退出类用得上）。返回 {"action","label","tone","confidence","reasons":[...],"style":...}；信息太少时返回 None。

    规则只能给出“最可能的解释”。公告里不会写买卖的原因。
    """
    t = n.get("type")
    if t not in ("becoming", "change", "ceasing"):
        return None
    vp = [float(x) for x in (n.get("voting_power") or [])]
    votes = [int(x) for x in (n.get("votes") or [])]
    tr = n.get("trades") or {}
    px = n.get("px") or {}
    style = n.get("style") or "active"
    ocr = bool(n.get("ocr"))
    prev = n.get("prev") or {}            # 同一申报方在同一只股票上的上一份公告：{vp, votes, type, date}
    cost = n.get("cost") or {}            # 之前公告里累计出来的平均买入成本：{avg, cov, since, n}（见 cost_basis）
    reasons: list[str] = []
    tags: list[str] = []
    score = 0
    ev = [x for x in (n.get("ev") or []) if isinstance(x, dict) and x.get("d") and x.get("k")]

    rows_ok = bool(tr.get("n"))
    buy, sell = (tr.get("buy_qty") or 0), (tr.get("sell_qty") or 0)
    if rows_ok and t == "becoming" and votes and buy - sell > votes[0] * 1.05:
        rows_ok = False          # 4 个月净买入比现在的总持仓还多：明细没读准，整段不用
        tr = {}
        buy = sell = 0
    kinds = tr.get("kinds") or {}
    gross = sum(kinds.values()) or 0
    share = (lambda k: kinds.get(k, 0) / gross) if gross else (lambda k: 0.0)
    kind_rows = tr.get("kind_rows") or {}
    n_rows = sum(kind_rows.values())
    row_share = (lambda k: kind_rows.get(k, 0) / n_rows) if n_rows else (lambda k: 0.0)

    # ---- 硬数字：持股比例和持股数量
    dvp = vp[1] - vp[0] if t == "change" and len(vp) == 2 else None
    dsh = so_chg = active_pp = chg_frac = None
    if t == "change" and len(vp) == 2 and len(votes) == 2 and votes[0] > 0 and votes[1] > 0:
        dsh = votes[1] - votes[0]
        so0, so1 = votes[0] / vp[0] * 100, votes[1] / vp[1] * 100
        so_chg = so1 / so0 - 1
        active_pp = dsh / so1 * 100               # 自己买卖带来的百分点变化
        chg_frac = dsh / votes[0]
    reconciled = bool(rows_ok and dsh is not None and abs(tr.get("net_qty", 0) - dsh) <= max(abs(dsh) * 0.05, 10))
    # 明细里的均价和当时的市场价差太远：多半是把总金额或别的数字当成了价格，不用
    bp, sp = tr.get("buy_price"), tr.get("sell_price")
    if px.get("t_lo") and px.get("t_hi"):
        ok = lambda v: v is not None and px["t_lo"] * 0.6 <= v <= px["t_hi"] * 1.6
        bp, sp = (bp if ok(bp) else None), (sp if ok(sp) else None)
    elif not px:
        pass                                       # 没有市场价可比，照用，但不拿来下“获利/止损”的结论

    if t == "change" and dvp is not None:
        line = f"持股比例 {vp[0]:g}% → {vp[1]:g}%"
        if dsh is not None:
            line += f"，持股数{'增加' if dsh > 0 else '减少' if dsh < 0 else '没变'}" + (f" {fq(dsh)}" if dsh else "")
            if dsh and abs(chg_frac) >= 0.005:
                line += f"（相当于原持仓的 {abs(chg_frac) * 100:.0f}%）"
        reasons.append(line + "。")
    elif t == "becoming" and vp:
        reasons.append(f"持股比例首次达到 {vp[0]:g}%" + (f"，共 {fq(votes[0])}" if votes else "") + "。")
    elif t == "ceasing":
        reasons.append("持股比例降到 5% 以下" + (f"（上一次披露是 {prev['vp']:g}%）" if prev.get("vp") else "")
                       + "。之后的买卖不用再公告，是否清仓看不出来。")

    # 这份公告覆盖的时间段：第一笔交易 → 变动日
    w0 = tr.get("first") or n.get("change_date") or n.get("date") or ""
    w1 = max(n.get("change_date") or "", tr.get("last") or "") or n.get("date") or ""

    def evs(kind: str, before: int, after: int) -> list[dict]:
        if not (w0 and w1):
            return []
        lo, hi = _shift(w0, -before), _shift(w1, after)
        return [x for x in ev if x["k"] == kind and lo <= x["d"] <= hi]

    def ev_txt(x: dict) -> str:
        return f"{md(x['d'])}「{x.get('h', '')}」"

    def done(action: str, conf: str | None = None) -> dict:
        nonlocal score
        if not vp and not rows_ok:
            score -= 2                      # 比例和明细都没读到（多半是 PDF 读不出），只能按公告类型说个大概
        if style in ("active", "strategic"):
            score += 1
        if style == "platform" or ocr:
            score -= 1
        c = conf or ("高" if score >= 3 else "低" if score <= 0 else "中")
        name, note = STYLES.get(style, STYLES["active"])
        rs = reasons + [f"{name}：{note}。"]
        if ocr:
            rs.append("这份是扫描件，数字靠文字识别，可能有错。")
        return {"action": action, "label": LABELS[action], "tone": TONE.get(action, "neutral"), "confidence": c,
                "reasons": rs, "style": name, "tags": tags}

    # ---- 1. 不是真买卖的情况
    if so_chg is not None and (so_chg > 2 or so_chg < -0.66):
        reasons.append("倒推出来的公司总股本前后差了几倍，应该是股份合并或拆分，前后的股数不能直接比。")
        return done("restructure", "中")
    if dsh is not None and dvp is not None and abs(dvp) >= 0.5 and abs(active_pp) <= max(0.25 * abs(dvp), 0.05):
        if dvp < 0:
            reasons.append(f"持股数几乎没动，比例却掉了 {abs(dvp):.2f} 个百分点：公司总股本变大了约 {so_chg * 100:.0f}%"
                           "（增发、配售或期权行权），它没有跟着买，所以被摊薄。")
            x = evs("raise", 45, 5)
            if x:
                reasons.append(f"对得上：公司在 {ev_txt(x[-1])}公告了融资。")
            return done("passive_dilute", "高")
        reasons.append(f"持股数几乎没动，比例却升了 {dvp:.2f} 个百分点：公司总股本变小了约 {abs(so_chg) * 100:.0f}%（回购或注销）。")
        x = evs("buyback", 200, 5)
        if x:
            reasons.append(f"对得上：公司这段时间在回购股票（{ev_txt(x[-1])}）。")
        return done("passive_up", "高")
    if dsh is None and not rows_ok and row_share("dilution") >= 0.5:
        reasons.append("公告里写的变动原因是公司股本变化（Dilution），不是它自己买卖。")
        return done("passive_dilute" if (dvp or -1) < 0 else "passive_up", "中")
    for kind, action, text in (
            ("takeover", "takeover", "明细里大部分是收购要约或并购方案相关的变动，不是普通的场内买卖。"),
            ("lending", "lending", "明细里大部分是证券借贷、抵押品的进出，不是真正的买入卖出。"),
            ("transfer", "transfer", "明细里大部分是划转（换托管人、换管理人或实物转让），不代表买卖意图。")):
        if (gross and share(kind) >= 0.5) or (not gross and row_share(kind) >= 0.5):
            reasons.append(text)
            return done(action, "中")

    # ---- 2. 方向
    if t == "becoming":
        direction = "buy"
    elif t == "ceasing":
        direction = "sell"
    elif dsh:
        direction = "buy" if dsh > 0 else "sell"
    elif dvp:
        direction = "buy" if dvp > 0 else "sell"
    else:
        return done("unknown", "低") if reasons else None
    if dsh is not None and dvp is not None and dsh * dvp < 0:
        reasons.append("注意：持股数和持股比例变动方向相反，说明公司总股本同时变了，这里按持股数的变化来判断。")

    # ---- 3. 交易明细
    if rows_ok:
        span = span_text(tr.get("first", ""), tr.get("last", ""))
        parts = []
        if buy:
            parts.append(f"买入 {fq(buy)}" + (f"（均价约 {fp(bp)}）" if bp else ""))
        if sell:
            parts.append(f"卖出 {fq(sell)}" + (f"（均价约 {fp(sp)}）" if sp else ""))
        if parts:
            what = "成为大股东前 4 个月" if t == "becoming" else "这次披露的明细里"
            line = f"{what}（{span}，{tr['n']} 笔）：" + "，".join(parts) + "。"
            if t == "change" and not reconciled:
                line += "明细加总和持股数的变化对不上（可能没读全），方向和幅度以持股数为准。"
            reasons.append(line)
        if reconciled or (t != "change" and tr["n"] >= 2):
            score += 1
    # 买卖量和同期全市场成交量比：看它急不急、是不是在场内做的
    offmkt = False
    t_vol, days, big = px.get("t_vol"), tr.get("days") or 0, max(buy, sell)
    vol_part = None
    if rows_ok and t_vol and big and (reconciled or t != "change"):
        vol_part = part = big / t_vol
        verb = "买入" if buy >= sell else "卖出"
        if part > 1.0:
            offmkt = True
            reasons.append(f"它{verb}的量是同期整个市场成交量的 {min(part, 99):.1f} 倍：不可能都是在场内成交的，"
                           + ("大部分应该是认购新股（配售）、大宗交易或场外转让。" if buy >= sell else "大部分应该是大宗交易或场外转让（一次性卖给了接盘方）。"))
        elif part >= 0.25:
            reasons.append(f"它{verb}的量占了同期全市场成交量的 {part * 100:.0f}%：是这段时间最主要的{'买家' if buy >= sell else '卖家'}之一，"
                           f"{'买' if buy >= sell else '卖'}得很坚决。")
        elif part <= 0.03 and days >= 5:
            reasons.append(f"分 {days} 天慢慢{verb}，只占同期成交量的 {max(part * 100, 0.1):.1g}% 左右，不着急。")
    churn = min(buy, sell) / max(buy, sell) if rows_ok and max(buy, sell) else 0.0
    placement = rows_ok and buy and share("placement") * gross >= 0.5 * buy

    # ---- 4. 股价位置
    placement_said = False
    r3, r6, r12, pos = px.get("r3m"), px.get("r6m"), px.get("r12m"), px.get("pos")
    if px.get("p"):
        bits = [f"{lab} {fr(v)}" for lab, v in (("近 1 个月", px.get("r1m")), ("3 个月", r3), ("6 个月", r6), ("12 个月", r12)) if v is not None]
        line = f"变动时股价约 {fp(px['p'])}"
        if bits:
            line += "，之前" + "、".join(bits)
        if pos is not None:
            line += f"，处在一年区间（{fp(px['lo'])}–{fp(px['hi'])}）的{pos_word(pos)}"
        reasons.append(line + "。")
    # 第十二轮：判断“涨了/跌了”时扣掉大盘同期的涨跌——大盘一起跌 20% 时买，不算这只股票的“逢低”
    mk = px.get("m") if isinstance(px.get("m"), dict) else {}
    mk = {k: v for k, v in mk.items() if isinstance(v, (int, float)) and not isinstance(v, bool) and -0.9 < v < 5}
    r1x = px.get("r1m")
    if mk:
        r3 = r3 - mk["m3"] if r3 is not None and "m3" in mk else r3
        r6 = r6 - mk["m6"] if r6 is not None and "m6" in mk else r6
        r12 = r12 - mk["m12"] if r12 is not None and "m12" in mk else r12
        r1x = r1x - mk["m1"] if r1x is not None and "m1" in mk else r1x
        if abs(mk.get("m3", 0)) >= 0.06 or abs(mk.get("m12", 0)) >= 0.12:
            bits = [f"{lab} {fr(mk[k])}" for lab, k in (("3 个月", "m3"), ("12 个月", "m12")) if k in mk]
            reasons.append("同期大盘（ASX 200）" + "、".join(bits) + "：下面说这只股票“涨了/跌了”，看的是它比大盘多涨或多跌的部分。")
    up = (r6 is not None and r6 >= 0.25) or (r12 is not None and r12 >= 0.40) or (r3 is not None and r3 >= 0.20)
    down = (r6 is not None and r6 <= -0.25) or (r3 is not None and r3 <= -0.20) or (r12 is not None and r12 <= -0.35)
    high = pos is not None and pos >= 0.65
    low = pos is not None and pos <= 0.3

    # ---- 4b. 公司同期的大事（来自这家公司的公告标题）
    tk_sell = False
    res_j = None
    raise_ev = evs("raise", 10, 5)
    if direction == "buy" and raise_ev:
        x = raise_ev[-1]
        if placement:
            reasons.append(f"对得上：公司在 {ev_txt(x)}公告了融资。")
            tags.append("参与融资")
        elif offmkt or (so_chg is not None and so_chg >= 0.03):
            placement = True
            placement_said = True
            reasons.append(f"公司在 {ev_txt(x)}公告了融资" + (f"，同期总股本增加了约 {so_chg * 100:.0f}%" if so_chg and so_chg >= 0.03 else "")
                           + "：这次买入很可能主要是认购新股（参与融资，价格通常比市价低），不是在市场上抢筹。")
            tags.append("参与融资")
        else:
            reasons.append(f"期间公司公告过融资（{ev_txt(x)}），一部分买入可能来自认购新股。")
    tk_all = evs("takeover", 60, 0)
    # 消息一出股价跳涨：公司是被收购的一方。近期只有走流程的公告（寄出方案手册之类）时，往前找当初那条让股价跳涨的消息
    tk_ev = [x for x in ev if x["k"] == "takeover" and x["d"] <= (w1 or "9") and (x.get("j") or 0) >= TARGET_JUMP] if tk_all else []
    if tk_ev and churn < 0.6:
        x = tk_ev[-1]
        what = f"{ev_txt(x)}，消息出来后股价 {fr(x['j'])}"
        tags.append("收购相关")
        if direction == "buy":
            if style == "hedge":
                reasons.append(f"买入发生在收购消息（{what}）之后：对冲基金这时候买，多半是做并购套利"
                               "（赚收购价和市价之间的差价），不代表看好公司本身。")
            else:
                reasons.append(f"买入发生在收购消息（{what}）之后：可能在赌收购成功，或者赌有人出更高的价。")
        else:
            tk_sell = True
            reasons.append(f"卖出发生在收购消息（{what}）之后：像是借收购带来的涨价退出，不等最终结果。")
    elif tk_all:
        reasons.append(f"期间公司有并购相关的公告（{ev_txt(tk_all[-1])}），但股价当时没有大涨，看不出它是被收购的一方，这里不据此下结论。")
    if w0 and w1 and _days_between(w0, w1) <= 30:
        res_ev = [x for x in ev if x.get("k") == "results" and _shift(w0, -6) <= x.get("d", "") <= _shift(w0, 2)]
        if res_ev:
            x = res_ev[-1]
            move = f"（公布后股价 {fr(x['j'])}）" if abs(x.get("j") or 0) >= 0.03 else ""
            reasons.append(f"{'买入' if direction == 'buy' else '卖出'}紧跟在公司 {ev_txt(x)}{move}之后：像是对这份业绩的反应。")
            tags.append("业绩后")
            res_j = x.get("j") or 0.0

    # ---- 5. 结论
    # 在 5% 披露线附近来回：这份公告本身只是很小的一步
    if t == "ceasing" and prev.get("vp") and prev["vp"] <= 5.6 and rows_ok:
        net_sold = sell - buy
        if (prev.get("votes") and 0 <= net_sold <= 0.05 * prev["votes"]) or (not prev.get("votes") and prev["vp"] <= 5.3):
            reasons.append("上一次披露时就只比 5% 高一点，这次只卖了很少一部分就跌破了披露线，谈不上清仓。")
            return done("slip")
    if t == "becoming" and prev.get("type") == "ceasing" and _days_between(prev.get("date"), n.get("date")) <= 45:
        reasons.append(f"它在 {md(prev.get('date'))} 刚公告过跌破 5%，现在又回到 5% 以上：持仓一直在披露线附近，"
                       "来回越线不代表大动作。")
        return done("back")
    two_way = rows_ok and (churn >= 0.6 or (churn >= 0.45 and chg_frac is not None and abs(chg_frac) < 0.10))
    if two_way and t != "becoming" and not (chg_frac is not None and abs(chg_frac) >= 0.25):
        reasons.append(f"买和卖的量差不多（少的一边是多的一边的 {churn * 100:.0f}%），净变动只是来回交易后的差额，"
                       f"更像日常调仓，{'净买入' if direction == 'buy' else '净卖出'}的信号不强。")
        return done("rotate")
    if two_way and t == "becoming":
        reasons.append(f"这 4 个月里也卖了不少（卖出量是买入量的 {sell / buy * 100:.0f}%），是边买边卖做上来的，"
                       "不是一路只买。" if buy >= sell else "这 4 个月里卖的比买的还多，能到 5% 靠的是更早的底仓。")
        score -= 1

    # ---- 6. 打分：把上面的证据汇总成“最可能的目的”
    seq = n.get("seq") or {}
    verb = "加仓" if direction == "buy" else "减仓"
    if seq.get("same"):
        reasons.append(f"这是它近 4 个月在这只股票上连续第 {seq['same'] + 1} 次{verb}。")
        tags.append("连续" + verb)
    if seq.get("opp_days") is not None and seq["opp_days"] <= SHORT_DAYS:
        reasons.append(f"它 {seq['opp_days']} 天前的公告还是{'减仓' if direction == 'buy' else '加仓'}，这么快就掉头。")
    f = {"side": direction, "t": t, "style": style, "size": None, "part": None if offmkt else vol_part, "offmkt": offmkt,
         "f_up": _trend(r3, r6, r12, 1), "f_down": _trend(r3, r6, r12, -1), "u3": _clip((r3 or 0) / 0.15, 0, 1.3),
         "lowpos": _clip((0.45 - pos) / 0.3, 0, 1) if pos is not None else 0.0,
         "highpos": _clip((pos - 0.5) / 0.3, 0, 1) if pos is not None else 0.0,
         "plc": 0.0, "raise": bool(raise_ev), "so_up": bool(so_chg is not None and so_chg >= 0.03), "plc_inferred": placement_said,
         "target": bool(tk_ev and churn < 0.6), "res_j": res_j, "cost_d": None, "gain": None, "own_gain": None,
         "seq_same": int(seq.get("same") or 0), "opp_days": seq.get("opp_days"), "base": "", "said_plc": placement_said}
    tl = n.get("tl") or {}
    if tl.get("n") and tl.get("down_share") is not None:
        f["tl"] = tl
        act = "买入" if direction == "buy" else "卖出"
        line = f"逐笔看（{tl['n']} 个交易日）：{act}的量有 {tl['down_share'] * 100:.0f}% 发生在股价下跌的日子"
        if tl.get("base_down") is not None:
            line += f"（同期下跌的日子占 {tl['base_down'] * 100:.0f}%）"
        line += f"，这段时间股价 {fr(tl['r_win'])}"
        if tl.get("accel") is not None and tl["n"] >= 6 and (tl["accel"] >= 1.8 or tl["accel"] <= 0.55):
            times = "10 倍以上" if tl["accel"] >= 9.9 else f"{tl['accel']:.1f} 倍"
            line += f"；后半段的量是前半段的 {times}，" + ("越做越多" if tl["accel"] >= 1.8 else "越做越少")
        reasons.append(line + "。")
    if rows_ok and buy and gross:
        f["plc"] = _clip(share("placement") * gross / buy, 0, 1)
    if chg_frac is not None and t == "change":
        f["size"] = abs(chg_frac)

    if direction == "buy":
        f["base"] = "build" if t == "becoming" else "add"
        if t == "becoming" and rows_ok and votes and buy:
            built = max(buy - sell, 0) / votes[0]
            f["size"] = min(built, 1.0)
            if built >= 0.6:
                reasons.append(f"现有持仓的 {min(built, 1) * 100:.0f}% 是最近 4 个月净买入的，属于快速建仓。")
            elif built <= 0.35:
                reasons.append(f"最近 4 个月净买入的只占现有持仓的 {built * 100:.0f}%：早就有底仓，这次只是加到了 5% 披露线以上。")
                f["base"] = "cross_up"
            else:
                reasons.append(f"现有持仓里约 {built * 100:.0f}% 是最近 4 个月净买入的。")
        buy_p = bp or px.get("p")
        if t == "change" and cost.get("avg") and buy_p and (cost.get("cov") or 0) >= COST_MIN_COVER:
            d = buy_p / cost["avg"] - 1
            if -0.9 < d < 4 and abs(d) >= 0.10:
                f["cost_d"] = round(d, 3)
                reasons.append(f"它之前的平均买入成本约 {fp(cost['avg'])}，这次买入价约 {fp(buy_p)}，"
                               + (f"比成本低 {abs(d) * 100:.0f}%：在摊低成本。" if d < 0 else f"比成本高 {d * 100:.0f}%：已经赚钱了还在加，是加码。"))
    else:
        f["base"] = "exit" if t == "ceasing" else "trim"
        if t == "ceasing" and rows_ok and prev.get("votes") and sell > buy:
            f["size"] = min((sell - buy) / prev["votes"], 1.0)
        if chg_frac is not None and t == "change":
            if abs(chg_frac) >= 0.4:
                reasons.append("一次减掉了四成以上的持仓，幅度很大。")
            elif abs(chg_frac) <= 0.12:
                reasons.append("减掉的不到原持仓的一成多，属于小幅减仓。")
        if bp and sp and buy >= 0.2 * sell:
            f["own_gain"] = round(sp / bp - 1, 3)
            if f["own_gain"] >= 0.15:
                reasons.append(f"同一份明细里，卖出均价比买入均价高 {f['own_gain'] * 100:.0f}%，是赚钱卖的。")
        # 之前的公告里累计出来的买入成本（只在已知成本的部分够多时才用）
        sale_p = sp or px.get("p")
        if cost.get("avg") and sale_p and (cost.get("cov") or 0) >= COST_MIN_COVER:
            gain = sale_p / cost["avg"] - 1
            if -0.9 < gain < 4:
                f["gain"] = round(gain, 3)
                known = f"（按 {md(cost.get('since'), True)} 以来的公告累计，覆盖了卖出前持仓的 {min(cost['cov'], 1) * 100:.0f}%）"
                head = f"它之前的平均买入成本约 {fp(cost['avg'])}{known}，这次卖出价约 {fp(sale_p)}，"
                reasons.append(head + (f"高出 {gain * 100:.0f}%：是赚钱卖的。" if gain >= 0.10 else
                                       f"低了 {abs(gain) * 100:.0f}%：是亏钱卖的。" if gain <= -0.10 else
                                       "和成本差不多：不像获利了结，也谈不上止损。"))
        f["tk_sell"] = tk_sell
    # ---- 6b. 第十一轮的四类新证据：趋势结构、持仓阶段、基金习惯、交易形状
    r1 = r1x
    lt_up = _ratio_max(((r12, 0.40), (r6, 0.25)))
    lt_dn = _ratio_max(((r12, -0.30), (r6, -0.25)))
    st_dn = _ratio_max(((r3, -0.15), (r1, -0.15)))
    st_up = _ratio_max(((r3, 0.15), (r1, 0.15)))
    f["pull"] = round(min(lt_up, st_dn), 2) if direction == "buy" and lt_up >= 1 and st_dn >= 0.8 and pos is not None and pos >= 0.35 else 0.0
    f["bounce"] = round(min(lt_dn, st_up), 2) if direction == "sell" and lt_dn >= 1 and st_up >= 0.8 else 0.0
    long_txt = f"近 12 个月 {fr(r12)}" if r12 is not None and abs(r12) >= abs(r6 or 0) else f"近 6 个月 {fr(r6)}" if r6 is not None else ""
    short_txt = (f"近 3 个月 {fr(r3)}" if r3 is not None and abs(r3) >= abs(r1 or 0) else f"近 1 个月 {fr(r1)}" if r1 is not None else "")
    if f["pull"]:
        reasons.append(f"长短期方向不一样：长期是涨的（{long_txt}），最近才回落（{short_txt}），股价也不在一年的低位——是在上涨途中的回调里买，和“跌了很久去抄底”不是一回事。")
    if f["bounce"]:
        reasons.append(f"长短期方向不一样：长期跌了很多（{long_txt}），最近在反弹（{short_txt}）——是趁反弹卖。"
                       "不知道它的成本时，说不准是赚是亏，但不是“高位获利了结”，也不是“越跌越卖”。")

    stage = _checked(n.get("stage"), ("off", "off_before", "n_buy", "n_sell", "hist"), ("age", "peak"))
    if stage and stage["peak"] > 0 and stage["age"] >= 0:
        stage["off"], stage["off_before"] = _clip(stage["off"], 0, 1), _clip(stage["off_before"], 0, 1)
        stage["known_start"], stage["fresh"] = stage.get("known_start") is True, stage.get("fresh") is True
        stage["by"] = "股数" if stage.get("by") == "股数" else "比例"
        f["stage"] = stage
        months = max(round(stage["age"] / 30.4), 1)
        head = "从建仓算起" if stage.get("fresh") else "从首次越过 5% 披露线算起" if stage.get("known_start") else "从能查到的最早一份公告算起"
        line = f"持仓阶段：{head}约 {months} 个月，持股比例最高到过 {stage['peak']:g}%"
        off, off0 = stage.get("off") or 0.0, stage.get("off_before") or 0.0
        if direction == "sell" and off >= 0.10:
            line += (f"；这次之后比最高点{'至少' if t == 'ceasing' else ''}少了 {off * 100:.0f}%"
                     + ("（按股数算）" if stage.get("by") == "股数" else "（按持股比例算，被增发摊薄的部分也算在里面）"))
        elif direction == "buy" and off0 >= 0.15 and (stage.get("n_sell") or 0) >= 1:
            line += f"；之前已经从最高点减掉过 {off0 * 100:.0f}%，这次是往回买"
        elif direction == "buy" and off < 0.02 and t == "change":
            line += "；这次之后是它在这只股票上持仓的新高"
        nb0, ns0 = stage.get("n_buy") or 0, stage.get("n_sell") or 0
        line += f"。之前公告过 {nb0} 次买入、{ns0} 次减仓。"
        if direction == "sell" and ns0 == 0 and nb0 >= 1:
            line += "这是它" + ("建仓以来" if stage.get("fresh") else "成为大股东以来" if stage.get("known_start") else "有记录以来") + "第一次减仓。"
        reasons.append(line)
        if direction == "sell" and off >= 0.30 and (ns0 >= 1 or f["seq_same"] >= 1):
            tags.append("已减三成")
    habit = _checked(n.get("habit"), ("n_buy",), ("n", "follow", "rev", "contr"))
    if habit and habit["n"] >= HABIT_MIN and all(0 <= habit[k] <= 1 for k in ("follow", "rev", "contr")):
        habit = {k: habit[k] for k in ("n", "follow", "rev", "n_buy", "contr")}
        f["habit"] = habit
        line = (f"这家基金的习惯（按它过去 {habit['n']} 次有后续的动作统计）：约 {habit['follow'] * 100:.0f}% 的情况下一份公告还是同方向，"
                f"约 {habit['rev'] * 100:.0f}% 会在 3 个月内掉头")
        line += "——" + ("样本还少，仅供参考。" if habit["n"] < 12 else "一旦开始就倾向一路做下去。" if habit["follow"] >= 0.8
                       else "经常做一段就反过来，单独一份公告的分量要打折。" if habit["rev"] >= 0.2 or habit["follow"] <= 0.6 else "属于一般水平。")
        reasons.append(line)
    blk = _checked(n.get("blk"), (), ("top", "x")) or (block_of(tr.get("daily"), px.get("adv"), direction) if rows_ok else {})
    if blk and not offmkt:
        blk = {"d": str(blk.get("d") or ""), "top": blk["top"], "x": blk["x"]}
        f["blk"] = blk
        verb = "买" if direction == "buy" else "卖"
        reasons.append(f"交易形状：这次{verb}的量有 {blk['top'] * 100:.0f}% 集中在 {md(str(blk.get('d') or ''))} 一天，是平时日成交量的 {blk['x']:.0f} 倍——"
                       + ("是一次性拿下一大笔（接了别人的大宗，或认购新股），不是在市场上慢慢买。" if direction == "buy"
                          else "是一次性卖给了接盘方（大宗交易），不是在市场上慢慢卖：卖得很干脆。"))
        tags.append("大宗")
    # 数据质量分（和目的无关）：明细对得上、基金类型、扫描件
    q = score + (1 if style in ("active", "strategic") else 0) - (1 if style == "platform" or ocr else 0) - (2 if not vp and not rows_ok else 0)
    f["q"] = q
    ins = done(f["base"])
    ins["_f"] = f
    finalize(ins)
    return ins


# ---------------------------------------------------------------- 目的打分
#
# 每个候选目的一个分数：分数 = 先验（基金类型）+ Σ 证据的加减分。分数过 softmax 变成概率。
# “普通加仓/减仓”是基准线（BASELINE）；别的目的要比基准线高出 MARGIN 才会成为标签。
# 权重是按 2026 年 10–11 月的 79 条真实公告、对照旧规则调的，还没有回测过——改权重只改这里。

BASELINE = 0.7
MARGIN = 0.05
TIE = 0.15
SHORT_DAYS = 60
PURPOSE_NAMES = {
    "buy": {"base": "普通加仓", "dip": "逢低买入", "mom": "顺势追加", "plc": "参与融资", "arb": "并购套利",
            "flow": "整体加仓（资金流入）", "swap": "换股调入", "short": "短线来回",
            "pull": "回调买入（长期在涨）", "prog": "按计划持续建仓"},
    "sell": {"base": "普通减仓", "profit": "获利了结", "stop": "止损或看法变差", "flow": "整体减仓（应付赎回）",
             "swap": "换股调出", "rebal": "仓位管理（涨多了减一点）", "short": "短线交易",
             "bounce": "趁反弹减仓", "wind": "分批减仓（一路在减）"},
}
PURPOSE_ACTION = {      # (方向, 目的) → 标签代码；None 表示沿用基础标签（新进建仓 / 加仓越过 5% 线 / 加仓 / 减仓 / 减到 5% 以下）
    ("buy", "dip"): {"add": "add_dip"}, ("buy", "mom"): {"add": "add_chase"},
    ("buy", "plc"): {"add": "add_placement", "build": "build_placement", "cross_up": "build_placement"},
    ("buy", "arb"): {"add": "arb", "build": "arb", "cross_up": "arb"},
    ("buy", "flow"): {"add": "add_flow"}, ("buy", "swap"): {"add": "swap_in", "build": "swap_in"},
    ("buy", "short"): {"add": "short_in", "build": "short_in", "cross_up": "short_in"},
    ("sell", "profit"): {"trim": "trim_profit", "exit": "exit_profit"}, ("sell", "stop"): {"trim": "trim_loss", "exit": "exit_loss"},
    ("sell", "flow"): {"trim": "trim_flow", "exit": "exit_flow"}, ("sell", "swap"): {"trim": "swap_out", "exit": "swap_out"},
    ("sell", "rebal"): {"trim": "trim_rebal"}, ("sell", "short"): {"trim": "short_out", "exit": "short_out"},
    ("buy", "pull"): {"add": "add_pull"}, ("buy", "prog"): {"add": "add_prog"},
    ("sell", "bounce"): {"trim": "trim_bounce", "exit": "exit_bounce"}, ("sell", "wind"): {"trim": "trim_wind", "exit": "exit_wind"},
}


# 这些成对的目的说的是两件不冲突的事（一个讲“怎么做”，一个讲“为什么”），可以同时成立：几乎同分时不算“分不出主次”，标分数高的那个
COMPATIBLE = {frozenset(x) for x in (("prog", "dip"), ("prog", "mom"), ("prog", "pull"), ("wind", "profit"), ("wind", "stop"),
                                     ("wind", "bounce"), ("bounce", "profit"), ("bounce", "stop"))}


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _ratio_max(pairs) -> float:
    """[(涨跌幅, 门槛), ...] 里“涨跌幅 ÷ 门槛”最大的一个，截到 0–1.3；门槛为负表示看跌幅。都没有数据时是 0。"""
    vals = [r / c for r, c in pairs if r is not None]
    return _clip(max(vals), 0.0, 1.3) if vals else 0.0


HABIT_MIN = 6             # 一家基金至少有这么多次“有后续的动作”，才谈得上习惯


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) < 1e12


def _checked(d, nums: tuple, required: tuple) -> dict:
    """持仓历史文件万一坏了，新证据宁可不用，也不能让整条解读出错：不是字典、必需的数字缺了或不是数字，就当没有。
    其余数字字段不是数字的按 0 算。"""
    if not isinstance(d, dict) or not all(_is_num(d.get(k)) for k in required):
        return {}
    out = dict(d)
    for k in nums:
        if not _is_num(out.get(k)):
            out[k] = 0
    return out


def _trend(r3, r6, r12, sign: int) -> float:
    """之前涨（sign=1）或跌（sign=-1）得有多明显：1.0 = 刚好达到“明显”的门槛（3 个月 20% / 6 个月 25% / 12 个月 40%·35%），最多 1.3。"""
    cuts = (0.20, 0.25, 0.40 if sign > 0 else 0.35)
    vals = [sign * r / c for r, c in zip((r3, r6, r12), cuts) if r is not None]
    return _clip(max(vals), 0.0, 1.3) if vals else 0.0


def score_purposes(f: dict, ctx: dict | None = None) -> list[dict]:
    """f：单只股票的证据（judge 里算的）；ctx：横向证据（add_context 里算的，可以没有）。
    返回按概率从高到低排的 [{k, name, s: 分数, p: 概率, why: [加分的依据]}]。"""
    ctx = ctx or {}
    side, style = f["side"], f.get("style") or "active"
    single = style in ("active", "hedge", "strategic")
    S: dict[str, float] = {}
    why: dict[str, list[tuple[float, str]]] = {}

    def add(k: str, pts: float, text: str = "") -> None:
        if not pts:
            return
        S[k] = S.get(k, 0.0) + pts
        if text and pts > 0:
            why.setdefault(k, []).append((pts, text))

    size, part = f.get("size"), f.get("part")
    tl = f.get("tl") or {}
    nb, ns = ctx.get("nb", 0), ctx.get("ns", 0)
    opp = f.get("opp_days")
    stage, habit, blk = f.get("stage") or {}, f.get("habit") or {}, f.get("blk") or {}
    follow_adj = _clip((habit["follow"] - 0.72) * 4, -0.5, 0.6) if habit else 0.0     # 0.72 ≈ 全体基金“下一次同方向”的平均比例
    rev_adj = _clip((habit["rev"] - 0.10) * 4, -0.4, 0.5) if habit else 0.0           # 0.10 ≈ 全体“3 个月内掉头”的平均比例
    edge = (tl["down_share"] - (tl.get("base_down") if tl.get("base_down") is not None else 0.5)) if tl.get("n") else None
    S["base"] = BASELINE
    if side == "buy":
        for k, prior in (("dip", -0.3), ("mom", -0.3), ("plc", -1.5), ("arb", -2.0), ("flow", -1.0), ("swap", -1.0), ("short", -1.2)):
            S[k] = prior
        add("dip", 1.2 * f["f_down"], "买之前股价跌了一轮")
        add("dip", 1.0 * f["lowpos"], "股价在一年区间的低位")
        add("mom", 1.0 * f["u3"], "近 3 个月股价在涨")
        add("mom", 1.0 * f["highpos"] if f["u3"] > 0 else 0, "股价在一年区间的高位")
        if f.get("cost_d") is not None:
            add("dip", 0.8 if f["cost_d"] < 0 else -0.5, "买入价低于自己的成本")
            add("mom", 0.4 if f["cost_d"] > 0 else -0.4, "已经赚钱了还在加")
        if f.get("res_j") is not None:
            add("dip", 0.6 if f["res_j"] <= -0.08 else 0, "业绩后股价大跌时买入")
            add("mom", 0.6 if f["res_j"] >= 0.08 else 0, "业绩后股价大涨时买入")
        add("plc", 4.5 * f.get("plc", 0.0), "明细里写的是配售/认购新股")
        if f.get("plc", 0.0) >= 0.5:
            add("dip", -1.0)
            add("mom", -1.0)
        if f.get("raise"):
            strong = f.get("offmkt") or f.get("so_up")
            add("plc", 3.4 if strong else 0.6, "公司同期公告了融资" + ("，而且买入量超过场内成交量或总股本明显增加" if strong else ""))
            if strong:                       # 认购新股的价格是公司定的，谈不上“逢低”或“追涨”
                add("dip", -1.5)
                add("mom", -1.5)
        elif f.get("offmkt"):
            add("plc", 0.5, "买入量超过同期场内成交量")
        if f.get("target"):
            add("arb", 3.2 if style == "hedge" else 1.6, "买入发生在公司被收购的消息之后")
        if tl.get("n"):
            edge = tl["down_share"] - (tl.get("base_down") if tl.get("base_down") is not None else 0.5)
            add("dip", 0.7 if edge >= 0.15 else 0, "买入的量集中在股价下跌的日子")
            add("mom", 0.7 if edge <= -0.15 else 0, "买入的量集中在股价上涨的日子")
            add("dip", 0.4 if tl["r_win"] <= -0.10 else 0, "买的过程中股价一直在跌，它没有停")
            add("mom", 0.4 if tl["r_win"] >= 0.10 else 0, "买的过程中股价一直在涨，它还在追")
        if nb >= 2 and ns == 0:
            add("flow", 1.0 + 0.3 * (min(nb, 5) - 2), f"同期在另外 {nb} 只股票上也只买不卖")
            add("flow", {"super": 0.3, "platform": 0.5}.get(style, 0.0))
            add("flow", 0.3 if size is not None and size < 0.10 else 0, "这次加的幅度不大")
        if ns >= 1:
            add("swap", (1.2 if single else 0.5) + 0.3 * (min(ns, 4) - 1), f"同期在另外 {ns} 只股票上减仓")
            add("swap", 0.3 if ctx.get("rot") else 0, "卖的和买的不在一个行业")
        if opp is not None and opp <= SHORT_DAYS:
            add("short", 2.0 + (0.4 if style == "hedge" else 0), f"{opp} 天前还在减仓")
        # -- 第十一轮
        S["pull"], S["prog"] = -0.6, -1.0
        if f.get("pull"):
            add("pull", 1.5 * f["pull"], "长期在涨、最近回落，股价不在低位")
            add("pull", 0.2 if f["highpos"] > 0 else 0)
            add("dip", -0.8)
            add("mom", -0.5)
        else:
            S["pull"] = -3.0
        add("prog", 0.7 * min(f.get("seq_same", 0), 2), f"近 4 个月连续第 {f.get('seq_same', 0) + 1} 次加仓")
        add("prog", follow_adj, "这家基金一旦开始加仓，通常会一路加下去")
        if tl.get("n", 0) >= 8:
            add("prog", 0.3 if part is not None and part <= 0.10 else 0, "分很多天慢慢买")
            add("prog", 0.3 if edge is not None and abs(edge) < 0.10 else 0, "涨的日子跌的日子都在买，不挑价格")
        add("prog", 0.2 if stage.get("hist", 0) >= 2 and (stage.get("off") or 0) < 0.02 else 0, "持仓创了新高")
        if f.get("plc", 0.0) >= 0.5 or f.get("t") == "becoming":
            add("prog", -1.0)
        if habit and (habit.get("n_buy") or 0) >= 8:
            tilt = _clip((habit["contr"] - 0.6) * 1.5, -0.3, 0.3)
            add("dip", tilt, "这家基金习惯在股价弱的时候买")
            add("mom", -tilt, "这家基金习惯在股价强的时候买")
        add("short", rev_adj, "这家基金经常买了不久就反手")
        if blk:
            add("plc", 1.2 if f.get("raise") else 0.8, "大部分是一天之内一次性拿下的")
            for k in ("dip", "mom"):
                add(k, -0.5)
            add("prog", -0.8)
    else:
        for k, prior in (("profit", -0.3), ("stop", -0.3), ("flow", -1.0), ("swap", -1.0), ("rebal", -0.8), ("short", -1.2)):
            S[k] = prior
        add("profit", 1.0 * f["f_up"], "卖之前股价涨了一大段")
        add("profit", 1.0 * f["highpos"] if f["f_up"] > 0 else 0, "股价在一年区间的高位")
        add("stop", 1.2 * f["f_down"], "卖之前股价跌了不少")
        add("stop", 1.0 * f["lowpos"] if f["f_down"] > 0 else 0, "股价在一年区间的低位")
        g = f.get("gain")
        if g is not None:
            if g >= 0.10:
                add("profit", 1.2 + _clip((g - 0.1) / 0.3, 0, 1), "卖出价高于自己的买入成本")
                add("stop", -2.0)
            elif g <= -0.10:
                add("stop", 1.2 + _clip((-g - 0.1) / 0.3, 0, 1), "卖出价低于自己的买入成本")
                add("profit", -2.0)
            else:
                add("profit", -1.0)
                add("stop", -1.0)
        if (f.get("own_gain") or 0) >= 0.15:
            add("profit", 1.5, "同一份明细里卖出均价比买入均价高")
        if f.get("tk_sell"):
            add("profit", 1.5, "借收购消息带来的涨价卖出")
        if f.get("res_j") is not None and f["res_j"] <= -0.08:
            add("stop", 0.6, "业绩后股价大跌时卖出")
        if size is not None and size >= 0.4:
            add("stop", 0.3 if f["f_down"] > 0 else 0, "一次减掉四成以上")
        if tl.get("n"):
            edge = tl["down_share"] - (tl.get("base_down") if tl.get("base_down") is not None else 0.5)
            add("profit", 0.5 if edge <= -0.15 else 0, "卖出的量集中在股价上涨的日子（逢高卖）")
            add("stop", 0.5 if edge >= 0.15 and tl["r_win"] <= -0.05 else 0, "股价越跌它卖得越多")
        if ns >= 2 and nb == 0:
            add("flow", 1.2 + 0.3 * (min(ns, 5) - 2), f"同期在另外 {ns} 只股票上也只卖不买")
            add("flow", {"super": 0.3, "platform": 0.5}.get(style, 0.0))
            add("flow", 0.3 if size is not None and size < 0.15 else 0, "这次减的幅度不大")
        if nb >= 1:
            add("swap", (1.2 if single else 0.5) + 0.3 * (min(nb, 4) - 1), f"同期在另外 {nb} 只股票上加仓")
            add("swap", 0.3 if ctx.get("rot") else 0, "卖的和买的不在一个行业")
        if size is not None and size <= 0.15 and f["f_up"] > 0:
            add("rebal", 0.8, "只减了一小部分")
            add("rebal", 0.6 * min(f["f_up"], 1.0), "股价涨过一段")
            add("rebal", 0.3 if style in ("super", "platform") else 0)
            add("rebal", 0.3 if part is not None and part <= 0.05 else 0, "卖得很慢")
        if opp is not None and opp <= SHORT_DAYS:
            add("short", 2.0 + (0.4 if style == "hedge" else 0), f"{opp} 天前还在加仓")
        # -- 第十一轮
        S["bounce"], S["wind"] = -0.6, -1.2
        if f.get("bounce"):
            add("bounce", 1.5 * f["bounce"], "长期跌了很多、最近反弹时卖")
            if g is None:                      # 不知道成本：单看最近的反弹，既不能算“获利”，也不能算“越跌越卖”
                add("profit", -0.8)
                add("stop", -0.8)
            elif g >= 0.10:
                add("bounce", -1.5)
            elif g <= -0.10:
                add("bounce", 0.3, "按它的成本算还是亏的")
        else:
            S["bounce"] = -3.0
        off, sold_before = stage.get("off") or 0.0, (stage.get("n_sell") or 0) >= 1 or f.get("seq_same", 0) >= 1
        if off >= 0.30:
            add("wind", 1.0 + 0.8 * _clip((off - 0.30) / 0.30, 0, 1), f"已经比最高点少了 {off * 100:.0f}%")
            add("rebal", -1.5)                 # 都减掉三成以上了，谈不上“涨多了减一点”
        add("wind", 0.5 * min(f.get("seq_same", 0), 2), f"近 4 个月连续第 {f.get('seq_same', 0) + 1} 次减仓")
        add("wind", 0.5 if f.get("t") == "ceasing" and (stage.get("n_sell") or 0) >= 1 else 0, "之前已经减过，这次减到了 5% 以下")
        add("wind", follow_adj if sold_before else 0, "这家基金一旦开始减仓，通常会一路减下去")
        if not sold_before:
            add("wind", -1.0)                  # 之前没减过：一次卖出谈不上“分批”
        first_sell = bool(stage) and (stage.get("n_sell") or 0) == 0 and (stage.get("n_buy") or 0) >= 1
        age = stage.get("age")
        if first_sell and age is not None and stage.get("known_start"):
            if age <= 150 and stage.get("fresh"):                 # 只有确实是近几个月才建的仓，才谈得上“刚买就卖”
                if not (opp is not None and opp <= SHORT_DAYS):   # 上面已经按“刚加仓就掉头”加过分的，不重复加
                    add("short", 1.6, f"建仓才 {max(round(age / 30.4), 1)} 个月就开始卖")
                add("stop", 0.7 if f["f_down"] > 0 or (g is not None and g <= -0.10) else 0, "建仓没多久、股价不如意就卖，像认错")
            elif age >= 270 and size is not None and size <= 0.15:
                add("rebal", 0.4, "成为大股东以来第一次减，而且只减了一点")
        if age is not None and age >= 540 and f["f_up"] > 0 and (g is None or g >= 0.10):
            add("profit", 0.3, "已经拿了一年半以上")
        add("short", rev_adj, "这家基金经常卖了不久就反手")
        if blk:
            add("rebal", -1.5)                 # 一次性大宗卖出不是“慢慢减一点”
            add("flow", -0.5)
    z = sum(math.exp(v) for v in S.values())
    out = [{"k": k, "name": PURPOSE_NAMES[side][k], "s": round(v, 2), "p": round(math.exp(v) / z, 3),
            "why": [t for _, t in sorted(why.get(k, []), reverse=True)]} for k, v in S.items()]
    out.sort(key=lambda x: -x["s"])
    return out


def strength_of(f: dict, ctx: dict | None = None) -> int | None:
    """这次动作的力度（0–100）：幅度 40%、占同期成交量 30%、连续同向 20%、别的名单基金同向 10%。没有的项不算，按剩下的折算。"""
    ctx = ctx or {}
    parts = []
    if f.get("size") is not None:
        parts.append((0.4, _clip(f["size"] / 0.3, 0, 1)))
    if f.get("offmkt"):
        parts.append((0.3, 1.0))
    elif f.get("part") is not None:
        parts.append((0.3, _clip(f["part"] / 0.3, 0, 1)))
    parts.append((0.2, _clip(f.get("seq_same", 0) / 2, 0, 1)))
    if "peer_same" in ctx:
        parts.append((0.1, 1.0 if ctx["peer_same"] else 0.0))
    w = sum(a for a, _ in parts)
    if f.get("size") is None:         # 不知道这次动了持仓的多大一部分：不给
        return None
    return round(100 * sum(a * b for a, b in parts) / w)


def finalize(ins: dict, ctx: dict | None = None) -> None:
    """按当前证据（可带横向证据）给出最终标签、把握、目的概率和力度。judge 结束时调一次，add_context 有了横向证据后再调一次。"""
    f = ins.get("_f")
    if not f:
        return
    ranked = score_purposes(f, ctx)
    top = ranked[0]
    base = next(x for x in ranked if x["k"] == "base")
    close = False
    if top["k"] != "base" and top["s"] < base["s"] + MARGIN:
        top = base
    elif top["k"] != "base" and any(x["k"] not in ("base", top["k"]) and x["s"] >= top["s"] - TIE
                                    and frozenset((x["k"], top["k"])) not in COMPATIBLE for x in ranked):
        close = True                                       # 有另一种目的和它几乎同分：分不出主次，不硬贴标签
    action = f["base"] if close else PURPOSE_ACTION.get((f["side"], top["k"]), {}).get(f["base"], f["base"])
    pts = f.get("q", 0) + (1 if top["k"] != "base" and top["p"] >= 0.45 else 0) + (1 if top["k"] != "base" and top["p"] >= 0.7 else 0)
    conf = "高" if pts >= 3 else "低" if pts <= 0 else "中"
    shown = [top] + [x for x in ranked if x is not top][:2]
    word = "加仓" if f["side"] == "buy" else "减仓"
    if close:
        line = f"综合打分：几种目的的可能性接近，分不出主次，先按普通{word}看。最高的是「{top['name']}」（{top['p'] * 100:.0f}%）"
    elif top["k"] == "base":
        line = f"综合打分：没有哪种特别的目的明显占上风，按普通{word}看（{top['p'] * 100:.0f}%）"
    else:
        line = f"综合打分：最像「{top['name']}」（{top['p'] * 100:.0f}%）" + (f"，依据：{'；'.join(top['why'][:3])}" if top["why"] else "")
    rest = [f"「{x['name']}」{x['p'] * 100:.0f}%" for x in shown[1:] if x["p"] >= 0.08]
    line += ("。其次是" + "、".join(rest) if rest else "") + "。"
    rs = ins["reasons"]
    idx = next((i for i, r in enumerate(rs) if r.startswith("综合打分：")), None)
    if idx is not None:
        rs[idx] = line
    else:
        rs.insert(next((i for i, r in enumerate(rs) if r.startswith(ins.get("style", "\0") + "：")), len(rs)), line)
    ins.update(action=action, label=LABELS[action], tone=TONE.get(action, "neutral"), confidence=conf,
               purposes=[{"k": x["k"], "name": x["name"], "p": x["p"]} for x in shown], strength=strength_of(f, ctx))
