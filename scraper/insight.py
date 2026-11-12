"""
操作判断：从公告 PDF 的交易明细、持股数量变化、基金类型和股价位置，推断申报方这次在干什么。

三步：
1. parse_trades()：读 604 第 3 节 / 603 第 5 节 / 605 第 2 节以及附件里的逐笔交易表，汇总成买卖统计。
2. （可选）股价背景：由 scrape.py 传入，见 price_context()。
3. judge()：一组写死的规则，输出 {kind, label, tone, confidence, reasons}。

这里只做规则判断，不调用任何 AI。规则给的是“最可能的解释”和依据，不是事实——基金为什么买卖，公告里不会写。
"""

from __future__ import annotations

import bisect
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
    for idx, ln in enumerate(lines):
        m = row_start(ln)
        if not m or NOT_TRADE_RE.search(ln):
            continue
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
        for nxt in lines[idx + 1: idx + 4]:           # 折行的性质说明
            if row_start(nxt) or CONT_STOP_RE.match(nxt):
                break
            cont.append(nxt)
        raw.append({"tok": m.group(1), "cands": cands, "rest": rest, "cont": " ".join(cont), "lead": lead})
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
        text = r["lead"] + " " + r["rest"] + " " + r["cont"]
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
            kind = _kind(text)                      # 这一行自己没写性质，再看折到下一行的说明
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
    "add_placement": "参与配售加仓", "trim_profit": "高位减仓，像获利了结", "trim_loss": "低位减仓，像止损",
    "trim": "减仓", "exit_profit": "减到 5% 以下，像获利了结", "exit_loss": "减到 5% 以下，像止损离场",
    "exit": "减到 5% 以下", "slip": "小幅卖出，刚好跌破 5% 线", "back": "又回到 5% 以上（在线附近来回）",
    "rotate": "边买边卖，像调仓", "passive_dilute": "被动稀释（没怎么卖）",
    "passive_up": "被动上升（没怎么买）", "takeover": "收购/并购方案引起", "lending": "证券借贷/抵押变动",
    "transfer": "划转或换管理人", "arb": "收购消息后买入，像并购套利", "restructure": "股份合并/拆分", "unknown": "看不出来",
}
TONE = {"build": "bull", "cross_up": "bull", "build_placement": "bull", "add_dip": "bull", "add_chase": "bull", "add": "bull",
        "add_placement": "bull", "trim_profit": "bear", "trim_loss": "bear", "trim": "bear", "exit_profit": "bear",
        "exit_loss": "bear", "exit": "bear"}


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
    return {"id": e.get("id"), "date": e.get("date") or "", "time": e.get("time") or "", "type": e.get("type"),
            "cd": e.get("change_date") or "", "vp": list(e.get("voting_power") or []), "votes": list(votes),
            "buy_qty": int(buy), "sell_qty": int(sell), "buy_price": bp, "sell_price": sp, "p": px.get("p"),
            "first": tr.get("first") or ""}


BUY_ACTIONS = {"build", "cross_up", "build_placement", "add_dip", "add_chase", "add", "add_placement"}
SELL_ACTIONS = {"trim_profit", "trim_loss", "trim", "exit_profit", "exit_loss", "exit"}
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
        a = (n.get("insight") or {}).get("action")
        return "buy" if a in BUY_ACTIONS else "sell" if a in SELL_ACTIONS else ""

    def gap(a, b):
        return _days_between(a.get("date"), b.get("date"))

    def sec(name):
        return SECTORS.get(name, name)

    dated = [n for n in notices if n.get("insight") and side(n) and n.get("date") and n.get("code")]
    plan = []                                     # 先全部算完再写，避免前面加的理由影响后面的判断
    for n in dated:
        me, ins = side(n), n["insight"]
        single = ins.get("style") in SINGLE_VIEW
        others = [o for o in dated if o is not n and o["holder"] == n["holder"] and o["code"] != n["code"] and gap(n, o) <= CTX_DAYS]
        buys = [o for o in others if side(o) == "buy"]
        ob = sorted({o["code"] for o in buys})
        sells = [o for o in others if side(o) == "sell" and o["code"] not in ob]
        os_ = sorted({o["code"] for o in sells})
        mine = n.get("sector") or ""
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
        if same:
            plan.append((ins, f"同一只股票上，名单里的 {_names(same, 3, '家')} 最近一个月也在{word}：多家同向，信号更强。", "多家同向"))
        if opp:
            plan.append((ins, f"同一只股票上，名单里的 {_names(opp, 3, '家')} 最近一个月却在{'减仓' if me == 'buy' else '加仓'}：机构之间看法不一。", "有人反向"))
    for ins, text, tag in plan:
        _add_reason(ins, text, tag)


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
    if rows_ok and t_vol and big and (reconciled or t != "change"):
        part = big / t_vol
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
    up = (r6 is not None and r6 >= 0.25) or (r12 is not None and r12 >= 0.40) or (r3 is not None and r3 >= 0.20)
    down = (r6 is not None and r6 <= -0.25) or (r3 is not None and r3 <= -0.20) or (r12 is not None and r12 <= -0.35)
    high = pos is not None and pos >= 0.65
    low = pos is not None and pos <= 0.3

    # ---- 4b. 公司同期的大事（来自这家公司的公告标题）
    tk_sell = False
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
                return done("arb")
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

    if direction == "buy":
        if placement:
            if not placement_said:
                reasons.append("买入的大部分来自配售、增发或认购新股，通常比市价便宜一些，属于参与公司融资。")
            score += 1
            return done("build_placement" if t == "becoming" else "add_placement")
        new_name = "build"
        if t == "becoming" and rows_ok and votes and buy:
            built = max(buy - sell, 0) / votes[0]
            if built >= 0.6:
                reasons.append(f"现有持仓的 {min(built, 1) * 100:.0f}% 是最近 4 个月净买入的，属于快速建仓。")
            elif built <= 0.35:
                reasons.append(f"最近 4 个月净买入的只占现有持仓的 {built * 100:.0f}%：早就有底仓，这次只是加到了 5% 披露线以上。")
                new_name = "cross_up"
            else:
                reasons.append(f"现有持仓里约 {built * 100:.0f}% 是最近 4 个月净买入的。")
        buy_p = bp or px.get("p")
        if t == "change" and cost.get("avg") and buy_p and (cost.get("cov") or 0) >= COST_MIN_COVER:
            d = buy_p / cost["avg"] - 1
            if -0.9 < d < 4 and abs(d) >= 0.10:
                reasons.append(f"它之前的平均买入成本约 {fp(cost['avg'])}，这次买入价约 {fp(buy_p)}，"
                               + (f"比成本低 {abs(d) * 100:.0f}%：在摊低成本。" if d < 0 else f"比成本高 {d * 100:.0f}%：已经赚钱了还在加，是加码。"))
        if (low and (r3 is not None and r3 <= -0.10)) or (r3 is not None and r3 <= -0.20):
            reasons.append("买入时股价处在低位或刚跌过一轮，属于越跌越买。")
            score += 1
            return done(new_name if t == "becoming" else "add_dip")
        if high and (r3 is not None and r3 >= 0.15):
            reasons.append("买入时股价在高位并且还在涨，属于顺着趋势追加。")
            score += 1
            return done(new_name if t == "becoming" else "add_chase")
        return done(new_name if t == "becoming" else "add")

    # 卖出
    own_gain = None
    if bp and sp and buy >= 0.2 * sell:
        own_gain = sp / bp - 1
    base = "exit" if t == "ceasing" else "trim"
    if chg_frac is not None and t == "change":
        if abs(chg_frac) >= 0.4:
            reasons.append("一次减掉了四成以上的持仓，幅度很大。")
        elif abs(chg_frac) <= 0.12:
            reasons.append("减掉的不到原持仓的一成多，属于小幅减仓。")
    if own_gain is not None and own_gain >= 0.15:
        reasons.append(f"同一份明细里，卖出均价比买入均价高 {own_gain * 100:.0f}%，是赚钱卖的。")
        score += 1
        return done(base + "_profit")
    # 之前的公告里累计出来的买入成本（只在已知成本的部分够多时才用）
    sale_p = sp or px.get("p")
    if cost.get("avg") and sale_p and (cost.get("cov") or 0) >= COST_MIN_COVER:
        gain = sale_p / cost["avg"] - 1
        if -0.9 < gain < 4:
            known = f"（按 {md(cost.get('since'), True)} 以来的公告累计，覆盖了卖出前持仓的 {min(cost['cov'], 1) * 100:.0f}%）"
            if gain >= 0.10:
                reasons.append(f"它之前的平均买入成本约 {fp(cost['avg'])}{known}，这次卖出价约 {fp(sale_p)}，高出 {gain * 100:.0f}%：是赚钱卖的。")
                score += 2
                return done(base + "_profit")
            if gain <= -0.10:
                reasons.append(f"它之前的平均买入成本约 {fp(cost['avg'])}{known}，这次卖出价约 {fp(sale_p)}，低了 {abs(gain) * 100:.0f}%：是亏钱卖的。")
                score += 2
                return done(base + "_loss")
            reasons.append(f"它之前的平均买入成本约 {fp(cost['avg'])}{known}，这次卖出价约 {fp(sale_p)}，和成本差不多：不像获利了结，也谈不上止损。")
            return done(base)
    if tk_sell:
        score += 1
        return done(base + "_profit")
    if up and high:
        reasons.append("卖出前股价已经涨了一大段，而且在一年区间的高位，更像是获利了结。")
        score += 1
        return done(base + "_profit")
    if down or (low and (r3 is not None and r3 <= -0.10)):
        reasons.append("卖出时股价已经跌了不少、处在低位，更像是止损或者看法变了。")
        score += 1
        return done(base + "_loss")
    reasons.append("股价既不在明显的高位也不在低位，看不出是获利了结还是止损，可能只是调仓或应付赎回。")
    return done(base)
