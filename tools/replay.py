"""离线回放：用仓库里现成的 data/history.json 和 data/all_notices.json，把每份公告按“当前代码”的规则重新判断一遍，
不联网、不改任何文件。改了 insight.py 的判断规则以后，用它和旧代码逐条对比（三轮审查的第一轮）。

用法：
    python tools/replay.py out_new.json                       # 当前代码跑一遍，结果存下来
    python tools/replay.py out_old.json --root ../旧代码目录    # 用另一份代码（比如 main 的检出）跑一遍
    python tools/replay.py --diff out_old.json out_new.json   # 逐条列出标签变了的公告

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
                "purposes": [(p["k"], p["p"]) for p in ins.get("purposes") or []], "next": nxt, "fwd3": (r.get("fwd") or {}).get("3m")}
    for x in scrape.build_notices(store.get("entries", {}), holders, history):
        ins = x.get("insight") or {}
        out["live"][str(x.get("id"))] = {"who": f"{x.get('holder')}|{x.get('code')}", "date": x.get("date"), "action": ins.get("action"),
                                         "label": ins.get("label"), "side": ins.get("side"),
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", nargs="?")
    ap.add_argument("--root", default=str(HERE), help="用哪一份代码（目录里要有 scraper/）；数据始终用本仓库的 data/")
    ap.add_argument("--diff", nargs=2, metavar=("OLD", "NEW"))
    args = ap.parse_args()
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
