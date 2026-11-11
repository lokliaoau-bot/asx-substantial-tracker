# 大股东公告雷达 — 给 AI 的工作笔记

**开工先读这一份，不用从头读代码。** 每做完一步就更新最下面的“当前进度”，并提交推送。
更完整的背景、历史 bug 和数据格式见 `HANDOVER.md`（交接文件）和 `DEPLOY.md`（历史记录）。

## 这是什么

追踪关注名单（10 家主动基金 + 13 家养老金）在 ASX 的大股东公告（603 新进 / 604 变动 / 605 退出），
并对每条公告给出“操作解读”（加仓？获利了结？被动稀释？）。用户 Lok 不写代码，用中文大白话沟通。

- 仓库：`lokliaoau-bot/asx-substantial-tracker`（公开），网页：GitHub Pages，根目录 `index.html`
- 抓取只在用户点网页按钮时运行（`workflow_dispatch` → `.github/workflows/scrape.yml`），**不要加定时任务**
- 抓取脚本跑在 GitHub Actions 上，不用任何 AI

## 干活的规矩（用户明确要求过）

1. 改动先在 `dev-*` 分支做，**测试 + 审查三轮（正确性 / 异常情况 / GitHub 真实环境）通过后**才 fast-forward 合并到 `main`。
2. 沙箱里连不上 ASX。要看真实数据：建 `debug-*` 分支，放一个 `on: push` 的临时工作流，把结果推到 `*-out` 分支再拉下来。
   （`workflow_dispatch` 只对 main 上已有的工作流有效：`scrape.yml` 可以用 `{"ref":"dev-xxx"}` 在测试分支上跑。）
3. 触发工作流：`curl -X POST https://api.github.com/repos/lokliaoau-bot/asx-substantial-tracker/actions/workflows/scrape.yml/dispatches -d '{"ref":"main"}'`（沙箱代理自带授权；Pages 设置、删分支的接口不通）。
4. 改了解析规则要加 `PARSER_VERSION`（全部公告重读，约 700 份，要在测试分支上先跑完再合并）；
   只改交易明细读法加 `INSIGHT_VERSION`（只重读命中名单的）；只改判断规则不用加版本（每次运行现算）。
5. 用户发过一个 GitHub 令牌在聊天里：不要用、不要写进任何文件。

## 代码地图

| 文件 | 内容 |
| --- | --- |
| `scraper/scrape.py`（约 1500 行） | 抓取主程序，见下 |
| `scraper/insight.py`（约 600 行） | 操作解读：`parse_trades`（逐笔明细）、`parse_votes`（第二节持股数）、`price_context`（股价背景）、`judge`（判断规则，纯函数） |
| `scraper/watchlist.json` | 关注名单：`name / group（主动基金·养老金）/ style（active·hedge·strategic·super·platform）/ aliases / hints`，以及 `noise_hints` |
| `index.html` | 单文件网页（无构建）。读 `data/notices.json`，按钮调 GitHub API 触发抓取并轮询 |
| `data/all_notices.json` | 内部存储：所有大股东公告的解析结果（35 天） |
| `data/notices.json` | 网页用：只含命中名单的公告 + `insight` |
| `tests/test_scrape.py` | 离线测试（假 ASX：`FakeASX`；真实样本在 `tests/fixtures/`） |
| `tests/frontend_check.py` | Playwright 网页检查（用 `/usr/local/bin/python3` 跑） |

`scrape.py` 的流程（`main()`）：
1. 读两个列表页（当天、上一交易日）`parse_listing` + 全市场接口 `market_rows`（增量，`api_since`）→ 去重 `dedupe_keys`
2. 标题带噪音缩写的跳过 `is_noise`；其余进 `work`，4 路并行 `process_entry`：
   `download_pdf` → `pdf_texts`（版面 + 默认两种文字）→ `analyse_texts`（申报方 `parse_holder_section`、比例 `pick_voting_power`、
   变动日 `parse_date`、其他格式 `parse_other_forms`）→ 读不出时 OCR（`ocr_texts`，有名额 `OCRBudget`）
   → 命中名单的顺手 `add_detail`（持股数 + 交易明细）
3. 命中名单但没读过明细的旧公告：`enrich_entry` 补读；然后 `add_prices` 取 ASX 日线算股价背景
4. `build_notices`：`match_entry`（高 = 名称命中别名，中 = 第一节原文命中，低 = 标题 from 缩写）→ `insight.judge` → `drop_superseded`

`entry` 的关键字段：`status`（new/parsed/noise/unreadable/error/skipped）、`parser`、`type`、`holder_name`、`region`、
`voting_power`、`change_date`、`ocr`、`votes`、`trades`、`detail_v`、`px`、`px_v`。

## 本地怎么跑

```bash
S=/tmp/claude-0/-home-claude/<session>/scratchpad      # 有就用，没有就重建
python3 -m venv $S/venv && . $S/venv/bin/activate && pip install -r requirements-dev.txt   # 需要系统有 tesseract
python -m pytest -q -p no:warnings                      # 全部离线
/usr/local/bin/python3 tests/frontend_check.py          # 网页检查
```

## 当前进度（每次更新这里）

**2026-11-11 第七轮（Fable 重审 + 提速 + 操作解读）：已完成并合并到 `main`。**

- 提速：4 路并行处理 PDF（`WORKERS`）、OCR 多页同时识别、补抓改增量（`api_synced_to`，每 7 天全量一次）。实测 300 份 PDF 约 5 分钟（以前约 15 分钟）
- 准确率：`PARSER_VERSION = 8`（日期和比例的怪写法、多行名称、TR-1 名称）
- 操作解读：`insight.py` + `add_detail / enrich_entry / add_prices` + 网页“操作解读”列；`INSIGHT_VERSION = 1`
- 验证：129 个测试、36 项网页检查；`dev-fable` 上真实运行三批（743 份全部重读，79 条命中全部有解读，81 只股票股价全部取到）；
  和正式版逐条对比只有预期内的变化
- 交接文件：`HANDOVER.md`

下一步候选见 `HANDOVER.md` 第 9 节（回测判断规则、多家基金同向、公告后走势……）。目前没有进行到一半的工作。

临时分支（可删，用户需在 GitHub 网页上删）：`dev-*`、`debug-*`。
