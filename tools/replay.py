"""离线回放：用仓库里现成的 data/history.json 和 data/all_notices.json，把每份公告按“当前代码”的规则重新判断一遍，
不联网、不改任何文件。改了 insight.py 的判断规则以后，用它和旧代码逐条对比（三轮审查的第一轮）。

用法：
    python tools/replay.py out_new.json                       # 当前代码跑一遍，结果存下来
    python tools/replay.py out_old.json --root ../旧代码目录    # 用另一份代码（比如 main 的检出）跑一遍
    python tools/replay.py --diff out_old.json out_new.json   # 逐条列出标签变了的公告
    python tools/replay.py --gold out_new.json                # 和人工标准答案（tests/fixtures/gold_purposes.json）对比打分

回头看（history）部分和 scrape.calibrate 一样只用“那一天之前”的信息；最近公告（live）部分和网页上看到的一致。
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def run(root: Path) -> dict:
    sys.path.insert(0, str(root / "scraper"))
    import insight
    import scrape

    history = json.loads((HERE / "data" / "history.json").read_text("utf-8"))
    store = json.loads((HERE / "data" / "all_notices.json").read_text("utf-8"))
    holders, _ = scrape.load_watchlist()
    style_of = {h.name: h.style for h in holders}
    new = hasattr(scrape, "stage_for")
    habit_cache: dict[str, dict] = {}
    out = {"history": {}, "live": {}}
    for key, rows in history.get("pos", {}).items():
        holder, code = key.split("|", 1)
        for i, r in enumerate(rows):
            if not r.get("px") and not r.get("tr"):
                continue
            e = {"id": r.get("id"), "date": r.get("date"), "time": r.get("time"), "type": r.get("type"), "votes": r.get("votes"),
                 "voting_power": r.get("vp")}
            before = rows[:i]
            prev = before[-1] if before else None
            holding = (r["votes"][0] if r.get("type") == "change" and len(r.get("votes") or []) == 2
                       else next((x["votes"][-1] for x in reversed(before) if x.get("votes")), None))
            n = {"type": r.get("type"), "voting_power": r.get("vp") or [], "votes": r.get("votes") or [], "trades": r.get("tr") or {},
                 "px": r.get("px") or {}, "ocr": r.get("ocr"), "style": style_of.get(holder, "active"), "date": r.get("date"),
                 "change_date": r.get("cd"), "tl": r.get("tl") or {}, "cost": insight.cost_basis(before, holding) if before else {},
                 "seq": scrape.sequence_for(history, holder, code, e),
                 "prev": {"vp": (prev.get("vp") or [None])[-1], "votes": (prev.get("votes") or [None])[-1], "type": prev.get("type"),
                          "date": prev.get("date")} if prev else None}
            if new:
                day = r.get("date") or ""
                if day not in habit_cache:
                    habit_cache[day] = scrape.habits(history, as_of=day)
                n.update(stage=scrape.stage_for(history, holder, code, e), habit=habit_cache[day].get(holder) or {}, blk=r.get("blk") or {})
            ins = insight.judge(n)
            if not ins:
                continue
            nxt = [(scrape._rec_dir(x), x.get("date")) for x in rows[i + 1:] if scrape._rec_dir(x)][:1]
            out["history"][f"{key}|{r.get('id')}"] = {
                "who": key, "date": r.get("date"), "action": ins["action"], "label": ins["label"], "side": (ins.get("_f") or {}).get("side"),
                "confidence": ins.get("confidence"),
                "purposes": [(p["k"], p["p"]) for p in ins.get("purposes") or []], "next": nxt, "fwd3": (r.get("fwd") or {}).get("3m")}
    for x in scrape.build_notices(store.get("entries", {}), holders, history):
        ins = x.get("insight") or {}
        out["live"][str(x.get("id"))] = {"who": f"{x.get('holder')}|{x.get('code')}", "date": x.get("date"), "action": ins.get("action"),
                                         "label": ins.get("label"), "side": ins.get("side"), "confidence": ins.get("confidence"),
                                         "purposes": [(p["k"], p["p"]) for p in ins.get("purposes") or []]}
    return out


def diff(a: dict, b: dict) -> None:
    for part in ("history", "live"):
        old, new = a.get(part, {}), b.get(part, {})
        changed = [k for k in old if k in new and old[k]["action"] != new[k]["action"]]
        print(f"== {part}：{len(old)} 条里标签变了 {len(changed)} 条")
        for k in sorted(changed, key=lambda k: new[k]["date"] or ""):
            print(f"  {new[k]['who']} {new[k]['date']}：{old[k]['label']} → {new[k]['label']}  {new[k]['purposes'][:3]}")
        print("   新标签分布：", collections.Counter(v["label"] for v in new.values() if v.get("side")).most_common())
    # 行为检验：每种标签之后，它自己下一份公告是不是同方向
    agg: dict[str, list] = collections.defaultdict(lambda: [0, 0])
    for v in b.get("history", {}).values():
        if v.get("side") and v.get("next"):
            agg[v["label"]][0] += 1
            agg[v["label"]][1] += v["next"][0][0] == v["side"]
    print("== 行为检验（新规则；标签 → 有后续的次数 / 下一份同方向的次数）")
    for k, (n, same) in sorted(agg.items(), key=lambda kv: -kv[1][0]):
        print(f"  {k}：{n} / {same}")


# 不打分、直接定性的标签 → 标准答案里用的键
FIXED_KEY = {"passive_dilute": "passive", "passive_up": "passive", "lending": "lending", "transfer": "transfer", "own_product": "own",
             "restructure": "restructure", "slip": "line", "back": "line", "rotate": "rotate", "takeover": "deal", "unknown": "base"}
BASE_ACTIONS = {"add", "build", "cross_up", "trim", "exit"}


def claimed(v: dict, action_key: dict) -> tuple[str, list[str], float | None]:
    """程序“说的”是哪个目的：(第一名, 前两名, 第一名的概率)。用户看到的是标签，所以第一名按标签算（“普通加仓”就是 base），不按概率表。"""
    a, ps = v.get("action") or "", [(k, p) for k, p in v.get("purposes") or []]
    if a in FIXED_KEY:
        top = FIXED_KEY[a]
    elif a in BASE_ACTIONS:
        top = "base"
    else:
        top = action_key.get(a) or (ps[0][0] if ps else "base")
    rest = [k for k, _ in ps if k != top]
    prob = next((p for k, p in ps if k == top), None)
    return top, [top] + rest[:1], prob


def gold_score(res: dict, path: Path, root: Path) -> dict:
    """和人工标准答案对比。只算 sure ≥ 2 的条目；同一份公告在 history 和 live 里各有一条时只算 live 那条。"""
    sys.path.insert(0, str(root / "scraper"))
    import insight
    action_key = {act: k for (side, k), m in insight.PURPOSE_ACTION.items() for act in m.values()}
    items = json.loads(path.read_text("utf-8"))["items"]
    live_ids = {g["id"] for g in items if g["src"] == "live"}
    rows = []
    for g in items:
        if g["sure"] < 2 or (g["src"] == "history" and g["id"].rsplit("|", 1)[-1] in live_ids):
            continue
        v = res.get(g["src"], {}).get(g["id"])
        if not v:
            continue
        top, top2, prob = claimed(v, action_key)
        rows.append({"g": g, "top": top, "top2": top2, "prob": prob, "conf": v.get("confidence") or "",
                     "strict": top == g["why"][0], "loose": top in g["why"], "two": bool(set(top2) & set(g["why"]))})
    n = len(rows)
    pct = lambda xs: round(100 * sum(xs) / len(xs), 1) if xs else None
    by_conf = {c: (len(rs), pct([r["loose"] for r in rs])) for c in ("高", "中", "低") for rs in [[r for r in rows if r["conf"] == c]]}
    scored = [r for r in rows if r["prob"] is not None]
    brier = round(sum((r["prob"] - r["loose"]) ** 2 for r in scored) / len(scored), 3) if scored else None
    out = {"n": n, "strict": pct([r["strict"] for r in rows]), "loose": pct([r["loose"] for r in rows]), "top2": pct([r["two"] for r in rows]),
           "by_conf": by_conf, "brier": brier, "brier_n": len(scored), "definite": pct([r["top"] != "base" for r in rows]),
           "misses": [(r["g"]["holder"], r["g"]["code"], r["g"]["date"], r["top"], r["g"]["why"]) for r in rows if not r["loose"]]}
    return out


def print_gold(sc: dict) -> None:
    print(f"== 标准答案对比（sure ≥ 2，共 {sc['n']} 条）")
    print(f"  第一名命中：严格 {sc['strict']}%（和标准答案的第一名一样），宽松 {sc['loose']}%（在标准答案的两个可接受目的里）")
    print(f"  前两名命中：{sc['top2']}%；给出明确目的（不是“普通加仓/减仓”）的比例：{sc['definite']}%")
    print("  按把握分：" + "，".join(f"{c} {n} 条 → {p}%" for c, (n, p) in sc["by_conf"].items()))
    print(f"  概率的准不准（Brier，越小越好，打分的 {sc['brier_n']} 条）：{sc['brier']}")
    print(f"  没命中的 {len(sc['misses'])} 条：")
    for m in sc["misses"]:
        print(f"    {m[0]} {m[1]} {m[2]}：程序说 {m[3]}，标准答案 {m[4]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", nargs="?")
    ap.add_argument("--root", default=str(HERE), help="用哪一份代码（目录里要有 scraper/）；数据始终用本仓库的 data/")
    ap.add_argument("--diff", nargs=2, metavar=("OLD", "NEW"))
    ap.add_argument("--gold", metavar="OUT", help="拿一份已经跑出来的结果和人工标准答案对比")
    ap.add_argument("--gold-file", default=str(HERE / "tests" / "fixtures" / "gold_purposes.json"))
    args = ap.parse_args()
    if args.gold:
        print_gold(gold_score(json.loads(Path(args.gold).read_text("utf-8")), Path(args.gold_file), Path(args.root).resolve()))
        return 0
    if args.diff:
        diff(*(json.loads(Path(p).read_text("utf-8")) for p in args.diff))
        return 0
    if not args.out:
        ap.error("要给一个输出文件名，或者用 --diff")
    res = run(Path(args.root).resolve())
    Path(args.out).write_text(json.dumps(res, ensure_ascii=False), "utf-8")
    print(f"history {len(res['history'])} 条，live {len(res['live'])} 条 → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
