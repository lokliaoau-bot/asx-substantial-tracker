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
| `scraper/scrape.py`（约 1700 行） | 抓取主程序，见下 |
| `scraper/insight.py`（约 850 行） | 操作解读：`parse_trades`（逐笔明细）、`parse_votes`（第二节持股数）、`price_context`（股价背景）、`event_moves`、`cost_basis`、`judge`（收集证据）、`score_purposes` / `finalize`（目的打分）、`add_context`（横向比较后重新打分） |
| `scraper/watchlist.json` | 关注名单：`name / group（主动基金·养老金）/ style（active·hedge·strategic·super·platform）/ aliases / hints`，以及 `noise_hints` |
| `index.html` | 单文件网页（无构建）。读 `data/notices.json`，按钮调 GitHub API 触发抓取并轮询 |
| `data/all_notices.json` | 内部存储：所有大股东公告的解析结果（35 天） |
| `data/history.json` | 持仓历史：`pos["基金|代码"] = [每份公告的买卖要点]`，留 3 年，不随 35 天清理 |
| `data/fund_reports.json` | 名单基金上市产品的月报正文（留 200 天） |
| `data/notices.json` | 网页用：只含命中名单的公告 + `insight` |
| `tests/test_scrape.py` | 离线测试（假 ASX：`FakeASX`；真实样本在 `tests/fixtures/`） |
| `tests/frontend_check.py` | Playwright 网页检查（用 `/usr/local/bin/python3` 跑） |
| `tools/replay.py` | 离线回放：改了判断规则后，用仓库里的真实数据和旧代码逐条对比 |

`scrape.py` 的流程（`main()`）：
1. 读两个列表页（当天、上一交易日）`parse_listing` + 全市场接口 `market_rows`（增量，`api_since`）→ 去重 `dedupe_keys`
2. 标题带噪音缩写的跳过 `is_noise`；其余进 `work`，4 路并行 `process_entry`：
   `download_pdf` → `pdf_texts`（版面 + 默认两种文字）→ `analyse_texts`（申报方 `parse_holder_section`、比例 `pick_voting_power`、
   变动日 `parse_date`、其他格式 `parse_other_forms`）→ 读不出时 OCR（`ocr_texts`，有名额 `OCRBudget`）
   → 命中名单的顺手 `add_detail`（持股数 + 交易明细）
3. 命中名单但没读过明细的旧公告：`enrich_entry` 补读；然后 `add_prices`（每只股票一次）取 ASX 日线算股价背景、公司近 6 个月的大事 `ev`、行业 `sector`
4. `update_history`：命中名单的公告要点记进 `data/history.json`（长期保存，算成本用）
5. `build_notices`：`match_entry`（高 = 名称命中别名，中 = 第一节原文命中，低 = 标题 from 缩写）→ `insight.judge`（带 `cost`、`ev`）→ `drop_superseded` → `insight.add_context`（横向比较）

`entry` 的关键字段：`status`（new/parsed/noise/unreadable/error/skipped）、`parser`、`type`、`holder_name`、`region`、
`voting_power`、`change_date`、`ocr`、`votes`、`trades`、`detail_v`、`px`（含 `t_vol`）、`px_v`、`ev`、`sector`。

## 本地怎么跑

```bash
S=/tmp/claude-0/-home-claude/<session>/scratchpad      # 有就用，没有就重建
python3 -m venv $S/venv && . $S/venv/bin/activate && pip install -r requirements-dev.txt   # 需要系统有 tesseract
python -m pytest -q -p no:warnings                      # 全部离线
/usr/local/bin/python3 tests/frontend_check.py          # 网页检查
```

## 当前进度（每次更新这里）

**用户说“继续”时：读这一节，从“进行中”的第一项接着干，不用重读代码。**

### 第十三轮（2026-11-12，已完成并合并到 `main`）：逐笔明细读准

- `insight.parse_trades`：说明写在日期行上面的表格（`pre` / `cont` 整份选一边）、只有日期的行不算、被数字隔开的短语拼起来认性质
- 修 bug：`detail_tries` 按读法版本分开数（`detail_tries()` / `detail_try_v`）
- history 记录带 `dv`；`Backfill.refresh_old` 按 idsId 重读旧读法的记录
- `INSIGHT_VERSION = 5`；196 个测试、39 项网页检查；`dev-r13` 真实运行两次，82 条公告 + 251 条历史全部重读；对得上 36 → 38（近期）
- 细节在 `HANDOVER.md` 第 5 节“第十三轮”

目前没有进行到一半的工作。下一步候选见 `HANDOVER.md` 第 9 节。

### 第十二轮（2026-11-12，已完成并合并到 `main`）：相对大盘 + 基金月报原话

- 相对大盘：`scrape.add_market`（XJO 日线，补 `px["m"]`）；`insight.judge` 打分用“股票涨跌 − 大盘涨跌”
- 基金月报：`watchlist.json` 的 `report_codes` → `update_fund_reports`（`data/fund_reports.json`）→ `fund_quotes` → `insight.quotes`；不参与打分
- **月报 PDF 必须在子进程里读（`report_text`）**：真实运行时有月报把内存吃光，任务被杀（exit 137）
- 192 个测试、39 项网页检查；`dev-r12` 真实运行通过；标签和上一版一致（大盘横盘），4 条公告有基金原话
- 细节在 `HANDOVER.md` 第 5 节“第十二轮”

目前没有进行到一半的工作。下一步候选见 `HANDOVER.md` 第 9 节（相对小盘指数/行业、让月报原话参与打分、RPL 的规模月报、做空数据）。

### 第十一轮（2026-11-12，已完成并合并到 `main`）：趋势结构 + 持仓阶段 + 基金习惯 + 交易形状

代码是另一边（连不上 GitHub 的聊天环境）做的，以 zip 交回；这边核对了 diff、跑了 186 个测试和 39 项网页检查，
在 `dev-r11` 真实运行通过（82 条公告：45 条有“持仓阶段”、57 条有“基金习惯”、10 条有“交易形状”，和正式版比 5 条标签变化，和交接文件预期一致），然后合并。

做了什么（细节和权重在 `HANDOVER.md` 第 5 节“第十一轮”）：
- `insight.py`：`judge` 末尾“6b”节收集四类新证据；新目的 `pull`（回调买入）、`prog`（按计划持续建仓）、`bounce`（趁反弹减仓）、`wind`（分批减仓）；
  `COMPATIBLE`（不冲突的目的同分时不算“分不出主次”）；`block_of`；`_checked`（坏数据保护）
- `scrape.py`：`stage_for`（持仓阶段）、`habits`（基金习惯，`as_of` 防偷看）、`calibrate` 多了 `rev_next`；`build_notices` 把 `stage` / `habit` 传给 `judge`
- `index.html`：“回头看”多一句“3 个月内就掉头的占 x%”，别的没动（新标签、新小标签网页自动显示）
- `tools/replay.py`：离线回放对比工具
- 只改判断规则，版本号没动；186 个测试、39 项网页检查通过

### 第十轮（2026-11-12，已完成并合并到 `main`）：回补历史 + 逐笔时间线 + 回头看

- `scrape.Backfill`（顺着 `prev_notice` 追上一份公告，进度在 `history["meta"]`，每次最多 150 份 / 8 分钟）：68 个持仓 250 条记录，全部追完（50 个到 603、11 个超 3 年、7 个找不到）
- `insight.timeline`（`trades.daily` 对日线 → `tl`）进打分；`insight.forward_returns` → `fwd`
- `scrape.calibrate` → `data/calibration.json` + 网页“回头看”
- `INSIGHT_VERSION = 4`；173 个测试、39 项网页检查；`dev-r10` 真实运行三次
- 细节在 `HANDOVER.md` 第 5 节“第十轮”

目前没有进行到一半的工作。下一步候选（`HANDOVER.md` 第 9 节）：基金月报原话（标题已探明，见下）、ASIC 做空数据、样本攒多后校准权重。

基金月报标题（探路结果，`debug-r10-out`）：WAM/WMI/WAX “<月份> Investment Update”、LSF “Monthly Report - <月份>”、RF1 “Unitholder Monthly Report for <月份>”、
PIC “PIC Monthly Investment Update - <月份>”、PL8 “<月份> Investment update”、RPL “<月份> Funds Under Management Update”。正文还没读过。

### 第九轮（2026-11-12，已完成并合并到 `main`）：目的打分公式

- `insight.score_purposes`（先验 + 证据加减分 → softmax 概率）、`finalize`（标签/把握/概率/力度；`judge` 调一次，`add_context` 带横向证据再调一次）、`strength_of`
- `scrape.sequence_for`：连续同向次数、距上次反向天数（来自 history.json）
- 输出 `insight.purposes`、`insight.strength`；网页显示概率条和力度
- 公式和权重表在 `HANDOVER.md` 第 5 节。权重是手调的，**还没回测**——这是下一步最值得做的事
- 168 个测试、38 项网页检查；`dev-r9` 真实运行通过

目前没有进行到一半的工作。

### 第八轮（2026-11-12，已完成并合并到 `main`）：让“操作解读”更能说清目的

- 公司同期大事 `ev`（融资/业绩/收购/回购，`company_events` + `insight.event_moves`）、行业 `sector`、交易期间成交量 `px.t_vol`
- 成本价：`data/history.json`（`update_history` / `cost_for` / `insight.cost_basis`）
- 横向比较 `insight.add_context` + 网页小标签 `insight.tags`
- `INSIGHT_VERSION = 3`；157 个测试、37 项网页检查；`dev-r8` 真实运行三次，和正式版逐条对比 79 条里只有 3 条标签变化（都合理）
- 判断规则的细节在 `HANDOVER.md` 第 5 节“第八轮加的判据”

目前没有进行到一半的工作。下一步候选（`HANDOVER.md` 第 9 节）：用全年公告页回补持仓历史、ASIC 做空数据、董事买卖、回测。

已验证可用的接口（探路结果在 `debug-r8-out`）：
- 公司近 6 个月公告：`/asx/v2/statistics/announcements.do?by=asxCode&asxCode=XXX&timeframe=D&period=M6`（HTML；`timeframe=Y&year=2026` 是全年，里面也有更早的大股东公告，以后可用来回补持仓历史）
- 行业/市值：`asx.api.markitdigital.com/asx-research/1.0/companies/XXX/header`
- ASIC 每日做空：`https://download.asic.gov.au/short-selling/RRyyyymmdd-001-SSDailyAggShortPos.csv`（T+4，还没接入，留作下一轮）

### 第七轮（2026-11-11，已完成并合并到 `main`）

- 提速：4 路并行处理 PDF（`WORKERS`）、OCR 多页同时识别、补抓改增量（`api_synced_to`，每 7 天全量一次）。实测 300 份 PDF 约 5 分钟（以前约 15 分钟）
- 准确率：`PARSER_VERSION = 8`（日期和比例的怪写法、多行名称、TR-1 名称）
- 操作解读：`insight.py` + `add_detail / enrich_entry / add_prices` + 网页“操作解读”列；`INSIGHT_VERSION = 1`
- 验证：129 个测试、36 项网页检查；`dev-fable` 上真实运行三批（743 份全部重读，79 条命中全部有解读，81 只股票股价全部取到）；
  和正式版逐条对比只有预期内的变化
- 交接文件：`HANDOVER.md`

下一步候选见 `HANDOVER.md` 第 9 节（回测判断规则、多家基金同向、公告后走势……）。目前没有进行到一半的工作。

临时分支（可删，用户需在 GitHub 网页上删）：`dev-*`、`debug-*`。
