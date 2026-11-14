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
5. `build_notices`：`match_entry`（高 = 名称命中别名，中 = 第一节原文命中，低 = 标题 from 缩写）→ `insight.judge`（带 `cost`、`ev`）→ `find_counterparty` + `insight.add_counterparty`（倒手）→ `drop_superseded` → `insight.add_context`（横向比较）

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

### 第十七轮（2026-11-14，进行中，分支 `dev-r17`）：自身波动、小盘对照、605 剩余持仓、明细对不上

- 4.1 `price_context` 多算 `vol`（近一年日涨跌标准差）；`insight._trend(..., vol)`：门槛 = max(固定门槛, `VOL_K`(1) × vol × √交易日)，最多 `VOL_CAP`(1.5) 倍固定门槛；
  波动大时理由里说“算正常起伏”。试过：只用波动门槛（蓝筹更松，更差）、更严的 k（“逢低”后跑赢指数的比例反而下降）。
  **“逢低”占比 35% → 34%，没达到计划的 < 25%**：70 条逢低里 60 条是买在一年区间低位，这本来就是逢低；而且逢低后 3 个月跑赢指数 59%（全部买入 51%），有信息量，不该硬压
- 4.2 `add_market` 按 `benchmark_of`：小盘股的 `px["m"]` 用 XSO（`px["mb"]`），理由里写“同期小盘股指数”
- 4.6 计划说的 17 条“没持股数”**全部是 605**（表格本来就不写剩下多少），没东西可读；改成：上一份持股 − 净卖出 = 估计剩余，卖光了加小标签“清仓”（只进理由，不进打分）；估出来 ≥ 5.2% 和公告矛盾时不说
- 4.7 明细对不上（近期 4 条：TTT、RIV、PPE、MYG）：理由第一句就说，均价/时间线/交易形状不用；`last_run.insight.reconcile` = {ok, bad}（现在 40 / 4）
- MYG 变动日读错：`price_ref`（变动日比明细最后一笔早 30 天以上就用最后一笔）
- `PX_VERSION = 6`（股价背景单独的版本号，和 `INSIGHT_VERSION` 分开）；`Backfill.refresh_px` 历史记录不下载 PDF 直接按日线重算
- 修 bug：日线取回来不够用时会把上次好的股价背景清空（ACL 3 条）→ 保留旧的、下次再试；`update_history` 也保留旧的 px / fwd_x
- 和第十六轮比：历史 257 条变 7 条（ACL 那条下次运行恢复）、近期 84 条变 1 条（TAH）（逐条说明在 `HANDOVER.md` 第十七轮）；标准答案 严格 74.4% / 前两名 92.3% / 高把握 84.2%（19 条）不降，Brier 0.249 → 0.252
- 232 个测试、网页检查通过；`dev-r17` 真实运行三次通过（数据、规则、ACL 恢复）
- 下一轮：第十八轮（`ACCURACY_PLAN.md` 4.3 指数调整、4.4 拆“像调仓”、4.5 形式 + 目的）。TTT、RIV 的“像调仓”明细都对不上，4.4 要一起考虑

### 第十六轮（2026-11-14，已完成并合并到 `main`）：准确率的尺子（按 `ACCURACY_PLAN.md` 第 3 节）

计划文件 `ACCURACY_PLAN.md`（第十六到二十轮）。本轮只加“尺子”，不改标签（回放 257 + 84 条全部不变）。
- 已做：标准答案 `tests/fixtures/gold_purposes.json`（131 条）+ `tools/replay.py --gold`；起点 78 条：严格 74.4% / 宽松 83.3% / 前两名 92.3% / 高把握 81.8%
- 已做：`scrape.outcome_checks`（可检验的预言）、`add_forward_excess`（`fwd_x`，小盘用 XSO）、`calibrate` 加 `avg3x/up3x/check*`、网页“回头看”改成和指数比
- 223 个测试、网页检查通过
- `dev-r16` 真实运行通过：XSO 能取到；167 条历史补上 `fwd_x`（XJO 86 / XSO 81）；标签 0 变化。
  预言检验（做到的比例 vs 同方向平均）：逢低 58.5%（41 条）vs 51.1%、顺势 52% vs 51.1%、回调 80%（5）、普通加仓 35.7%（14）、按计划建仓 下一份同向 4/4 vs 72.5%
  → 逢低、回调略有信息量，顺势基本没有；样本还小，第二十轮 6.1 再用
- 预言改成按方向（`CHECK_OF` 的键是“方向:目的”）：卖出不拿“之后跑赢指数”来检验
- 待办：请 Lok 抽查约 15 条标准答案
- 查清了 MYG 股价对不上：原版 `03153866` 的变动日读成了上一份公告的日期 2026-01-14（明细最后一笔是 10/26；更正版 `03153906` 读对了 10/23），
  股价按 1 月算了。82 条里变动日比明细最后一笔早 >3 天的有 3 条，只有 MYG 是错的（L1/WAM 早 4 天是列的交收日）；历史里另有 EQT 2024-01-11（早 131 天）。
  → 第十七轮 4.7 一起修：变动日比明细最后一笔早 30 天以上时，股价背景改用明细最后一笔的日期，并在理由里说明
- 下一轮：第十七轮（`ACCURACY_PLAN.md` 第 4.1、4.2、4.6、4.7 节）

### 第十五轮（2026-11-14，已完成并合并到 `main`）：倒手识别（找到卖出的另一头 / 接盘方）

起因：用户发现 Perennial 卖 AMX（18.58% → 8.58%，11/9 一天卖掉 950 万股）被标“减仓，像获利了结”，但 ASX 上 AMX 最新一份公告是
Landchecker 一方 11/9 成为大股东、持股 18.58%——正好等于 Perennial 卖出前的全部持股。接盘方不在关注名单里，以前完全看不到。

代码是另一边（没有推送权限的聊天环境）做的，以 patch 交回；这边核对 patch 和 zip 一致、217 个测试 + 40 项网页检查通过，
`dev-r15` 真实运行：84 条里只有 Perennial AMX 变了（→ “整块转让给接盘方（倒手）”，`cp.name` = Landchecker Shareholder Entities，链接正常），然后合并。
顺手修了老 bug：周末/假期的当天公告页写 “No company announcements have been published”，`parse_listing` 只认 “no announcements”，
每个周末点抓取都会误报“ASX 可能改版了”（样本 `tests/fixtures/todayAnns_weekend.html`）。

做了什么：
- `scrape.find_counterparty`（`build_notices` 里调，用的是**全部**大股东公告 `by_code`，不只名单里的）：同一只股票、方向相反、变动日相差 ≤ `CP_DAYS`(3) 天，且数量对得上：
  `whole`（买方之后的持股 ≈ 卖方之前的全部持股，卖方之前 ≥ `CP_WHOLE_MIN` 6%）> `shares`（两边持股数变化差 < 3%）>
  `block`（两边变动的百分点差不多，或 603 买方的全部持股 ≈ 卖方卖掉的；**要求这次的量集中在一天或超过全市场成交量**；有两家都差不多就不说）
- 排除：同一家机构换名申报（`match_entry` 同一家，或名字前两个词一样 `_same_org`）、股数和比例方向相反（摊薄）、日期或数字不全
- `scrape.clean_holder_name`：显示用名字（认 “(collectively, the XXX)”，去 ATF / and its affiliates 等尾巴，认不出返回空）
- `insight.add_counterparty`：理由一句 + 小标签“倒手”（放最前）+ `insight.cp`（网页显示对方公告链接）+ 新目的重新打分
- 新目的：卖 `hand`（整块转让给特定的接盘方，标签 `trim_hand` / `exit_hand`，中性），买 `take`（从别的大股东手里整块接过来，`add_take` / `build_take`，看多）；
  先验 −2.0，`CP_SCORE` whole 4.2 / shares 3.8 / block 3.0，一天集中 +0.4；和 获利/止损/收购/分批/反弹（买方：逢低/顺势/回调/套利/建仓）兼容
- `index.html`：解读里多一行“接盘方的公告：<链接>”
- 只改判断规则，版本号没动；217 个测试、40 项网页检查；`tools/replay.py`：近期 84 条只有 AMX 变了，历史 257 条不变；
  全市场 822 条公告互相配对的压力测试，收紧规则后只剩 AMX 一对（收紧前误配的：KAR、ZIP 卖方本来就在 5% 附近；PLS 大盘股百分点碰巧接近；JPMorgan 自己配自己）

### 第十四轮（2026-11-12，已完成并合并到 `main`）：逐条审查正式版 82 条解读后修判断

- 自家产品：`scrape.is_own_product`（`watchlist.json` 的 `report_codes` + 新字段 `own_codes`，或公司名以基金品牌开头，限 active/hedge）→ `judge` 的 `own` → 标签 `own_product`（RG1、RG8、GLS）
- 603 明细比 4 个月长（Perennial 列的是建仓以来全部）：`LONG_603`，照实说跨度，“最近 4 个月净买入”只用 `trades.daily` 里最近 123 天
- 收购方向看标题：`insight.target_by_headline`（`TARGET_WORDS` / `BIDDER_WORDS` / 别家代码前缀）；`judge` 需要 `code`（build_notices 传入）
- 新目的 `deal`（借收购消息退出，标签 `trim_deal` / `exit_deal`）：收购消息后卖出 deal +2.6、获利 +0.5、止损 −1.0；和 获利/止损/分批 兼容
- 上一份公告：近期找不到时 `scrape.prev_from_history`（修了 EOS、ZIP 没认出“5% 线附近”的问题）
- 209 个测试、39 项网页检查；`dev-r14` 真实运行通过，84 条里 8 条标签变化（都逐条看过）。细节在 `HANDOVER.md` 第 5 节“第十四轮”

目前没有进行到一半的工作。下一步候选见 `HANDOVER.md` 第 9 节（定期逐条审查、相对小盘指数、月报原话进打分、做空数据）。

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
