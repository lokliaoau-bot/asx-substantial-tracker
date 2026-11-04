# 部署交接文档：大股东公告雷达（ASX Substantial Holder Tracker）

> 本文写给负责部署的 AI 或工程师。请完整读完第 1–6 节再动手。用户是个人投资者，母语中文，跟用户沟通请用中文。

## 1. 要交付什么

一个部署在 GitHub 上的个人工具：

- **抓取**：GitHub Actions 运行 `scraper/scrape.py`，从 ASX 公开页面抓取大股东公告（Form 603/604/605），解析 PDF，筛出关注名单（10 家主动型投资者）的记录，写入 `data/notices.json` 并提交回仓库。
- **网页**：GitHub Pages 托管 `index.html`。点“抓取最新公告”按钮，网页用用户自己的 GitHub 令牌调用 `workflow_dispatch` 触发抓取，轮询运行状态，完成后在下方表格显示结果。网页支持“添加到主屏幕”。
- **触发方式**：只在网页上点按钮时运行（用户 2026-11-02 要求去掉定时任务），每次补当天和上一交易日。

不需要服务器、数据库或任何付费服务。仓库里不存放任何密钥。

## 2. 架构

```
[手机/电脑上的网页 index.html @ GitHub Pages]
   │  ① POST /repos/{o}/{r}/actions/workflows/scrape.yml/dispatches   （用户的 fine-grained PAT）
   │  ② 轮询 /actions/workflows/scrape.yml/runs，按 run id 找到这次运行
   │  ③ GET /contents/data/notices.json（raw）读取结果；失败再读 Pages 上的 data/notices.json
   ▼
[GitHub Actions: scrape.yml]
   python scraper/scrape.py
     ├─ GET https://www.asx.com.au/asx/v2/statistics/todayAnns.do
     ├─ GET https://www.asx.com.au/asx/v2/statistics/prevBusDayAnns.do
     ├─ GET https://asx.api.markitdigital.com/asx-research/1.0/markets/announcements?page=N&itemsPerPage=1000
     │     ASX 网站自己用的全市场公告接口（无公开文档），按时间倒序最多约 1 万条（两三周），用来补回没点按钮的日子；
     │     PDF 从 …/asx-research/1.0/file/{documentKey} 下载。读不到时只记提醒，不影响列表页。
     │     同一份公告可能挂在两个代码下（“for LM1” / “from FRS”），按 (代码或标的代码, 日期, 时间) 去重
     ├─ 新的大股东公告 → 下载 PDF（可能经过“同意条款”中间页）→ pypdf 读前 2 页
     ├─ data/all_notices.json   全部大股东公告及解析结果（内部存储，保留 35 天）
     └─ data/notices.json       按 watchlist.json 匹配后的结果（网页读取）
   git commit + pull --rebase + push（用 GITHUB_TOKEN）
```

浏览器不能直接请求 ASX（跨域），claude.ai 的 Artifact 也不能访问外部网站，所以采用这个架构。不要改成纯前端抓取。

## 3. 文件清单

| 路径 | 作用 |
| --- | --- |
| `index.html` | 网页（单文件，含 CSS/JS）。自动从 `*.github.io/<repo>` 识别仓库。令牌存在浏览器 localStorage，键名前缀 `ssr_`。 |
| `manifest.webmanifest`、`icon-192.png`、`icon-512.png`、`apple-touch-icon.png` | 添加到手机桌面用的清单和图标。 |
| `scraper/scrape.py` | 抓取脚本。依赖 requests、beautifulsoup4、pypdf、cryptography（接口下载的 PDF 是 AES 加密的）、tzdata；扫描件 OCR 另需 pypdfium2、Pillow 和系统里的 tesseract（工作流用 apt 安装，装不上时自动跳过 OCR）。 |
| `scraper/watchlist.json` | 关注名单：`holders[].name/aliases/hints`，以及 `noise_hints`。 |
| `data/notices.json` | 网页读取的数据。初始是空结构，首次运行后由脚本覆盖。 |
| `data/all_notices.json` | 首次运行时自动生成，不用手动创建。 |
| `.github/workflows/scrape.yml` | 抓取工作流：只有手动触发（网页按钮）。需要 `contents: write`。 |
| `.github/workflows/test.yml` | 推送代码时自动跑离线测试。 |
| `tests/test_scrape.py` | 92 个离线测试（模拟 ASX 列表页和 PDF + `tests/fixtures` 里的真实公告 PDF 样本，不联网；OCR 测试需要本机有 tesseract，没有时自动跳过）。 |
| `tests/frontend_check.py` | 可选的网页端到端检查（Playwright + 模拟 GitHub API）。 |
| `requirements.txt` / `requirements-dev.txt` | 运行依赖 / 测试依赖（版本已锁定，已在 Python 3.12 干净环境验证可安装）。 |
| `README.md` | 给用户看的使用说明。 |

## 4. 部署前提

- 用户的 GitHub 账户。仓库必须是 **公开** 的：免费账户的 GitHub Pages 只支持公开仓库，而且网页在没有令牌时也靠公开仓库的 API 读取最新数据。数据都是 ASX 公开信息，令牌不进仓库，公开没有隐私问题。
- 建议单独建一个仓库（例如 `asx-substantial-tracker`），不要并入用户已有的“澳股雷达”仓库，避免工作流和 Pages 配置冲突。
- **只有用户本人能做的事**（AI 无法代劳，请明确告诉用户）：
  1. 创建 fine-grained personal access token（见第 5.6 步）。
  2. 在网页“GitHub 设置”里填入令牌；手机桌面图标打开的页面需要再填一次。

## 5. 部署步骤

下面给出 `gh` 命令行写法；没有命令行权限时，括号里是网页操作。把 `OWNER` 换成用户的 GitHub 用户名。

### 5.1 建仓库并推送全部文件

```bash
cd asx-substantial-tracker
git init -b main
git add -A            # 一定要包含 .github/ 和 .gitignore
git commit -m "Initial commit: ASX substantial holder tracker"
gh repo create OWNER/asx-substantial-tracker --public --source . --push
```

（网页操作：新建公开仓库 → 上传全部文件。网页拖拽上传可能漏掉 `.github` 隐藏目录，需要用 Add file → Create new file 按原路径逐个创建两个 workflow 文件。）

### 5.2 允许工作流写入仓库

```bash
gh api -X PUT repos/OWNER/asx-substantial-tracker/actions/permissions/workflow \
  -f default_workflow_permissions=write -F can_approve_pull_request_reviews=false
```

（网页：Settings → Actions → General → Workflow permissions → Read and write permissions。）

工作流文件里已经声明了 `permissions: contents: write`，这一步是保险。

### 5.3 开启 GitHub Pages

```bash
gh api -X POST repos/OWNER/asx-substantial-tracker/pages \
  -f "source[branch]=main" -f "source[path]=/"
```

（网页：Settings → Pages → Source: Deploy from a branch → main → / (root)。）

网址是 `https://OWNER.github.io/asx-substantial-tracker/`，第一次构建需要 1–2 分钟。

### 5.4 确认测试通过

推送后 `tests` 工作流会自动运行：

```bash
gh run list --workflow tests --limit 1
gh run watch          # 选择对应的运行
```

本地也可以跑：

```bash
pip install -r requirements-dev.txt
python -m pytest -q   # 期望：92 passed（需要 tesseract：sudo apt-get install tesseract-ocr）
```

### 5.5 第一次真实抓取（关键）

```bash
gh workflow run scrape.yml --ref main
gh run watch          # 或 gh run list --workflow scrape-substantial
git pull && python -c "import json;d=json.load(open('data/notices.json'));print(d['last_run'])"
```

验收要点：

- `last_run.ok == true` 且 `list_pages_ok == 2`
- `substantial_found` > 0（交易日一般有几十条）
- `pdf_checked` > 0 且 `errors` 为空，或只有少量“下载失败，下次重试”
- `data/all_notices.json` 已生成。抽查几条 `status == "parsed"` 的记录，`holder_name` 应该是像样的公司名。
- 关注名单命中 0 条是正常的，那两天可能这 10 家都没有申报。

如果不满足，按第 7 节排错。

### 5.6 用户创建令牌并在网页上配置

请用户操作：GitHub 头像 → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token：

- Repository access：Only select repositories → 只选这个仓库
- Permissions：**Actions: Read and write**，**Contents: Read-only**（Metadata 只读会自动带上）
- Expiration：建议 90 天，到期后重新生成

然后打开网页 → 展开底部“GitHub 设置”→ 仓库所有者和仓库名通常已自动填好 → 粘贴令牌 → 保存设置 → 点“抓取最新公告”。看到“数据更新于 …（悉尼时间）”即成功。

### 5.7 添加到手机桌面

iPhone：Safari 打开网址 → 分享 → 添加到主屏幕。Android：Chrome → 菜单 → 添加到主屏幕或安装应用。打开后在设置里再填一次令牌。

## 6. 验收标准

1. `tests` 工作流通过（33 个测试）。
2. 手动触发的 `scrape-substantial` 成功，`data/notices.json` 的 `last_run.ok` 为 true。
3. 网页能打开，按钮能触发运行，状态栏会依次显示“排队中 / 正在抓取”，结束后显示“数据更新于 …”。
4. 手机宽度下表格变成卡片，没有横向滚动。
5. 第二天确认定时任务自动运行过（Actions 页面有 schedule 触发的记录）。

## 7. 排错指南（脚本还没有对真实 ASX 网站跑过）

开发环境无法访问 asx.com.au，所以解析逻辑是按 ASX 页面的已知结构、用模拟数据测试的。第一次真实运行若出问题，最可能在以下三处。修复时请顺手把真实 HTML 或 PDF 片段加进 `tests/test_scrape.py` 作为回归测试。

| 症状（看 Actions 日志或 `last_run.errors`） | 可能原因 | 处理 |
| --- | --- | --- |
| `读取 todayAnns.do 失败：HTTP 403` | ASX 拦截了 GitHub Actions 的 IP 或请求头 | 先在本地用 `curl -A "<HEADERS 里的 UA>" <url>` 对比。可调整 `HEADERS`，在 `fetch()` 里先请求一次 ASX 首页拿 cookie。不要用第三方代理绕过，请遵守 ASX 使用条款。 |
| `页面结构认不出来` 或 `0 条公告` | ASX 改版 | 下载 HTML 查看表格结构，调整 `parse_listing()`：行是 `<tr>`，第 1 列代码，第 2 列日期和时间，公告链接含 `idsId=`。 |
| 大量 `中间页里找不到 PDF 地址` | “同意条款”页结构变了 | 查看 `displayAnnouncement.do?display=pdf&idsId=…` 返回的 HTML，调整 `resolve_pdf_link()`，目前依次找 `input[name=pdfURL]`、`/asxpdf/…pdf`、任意 `.pdf` 链接。 |
| `status=parsed` 但很多 `holder_name` 为空或像标签 | PDF 文本排版和预期不同 | 用 `scrape.pdf_text(open(f,'rb').read())` 打印真实文本，调整 `parse_holder_section()` 和 `LABEL_RE`。 |
| 推送失败（403 / non-fast-forward） | 写权限没开，或并发 | 检查 5.2；工作流已带 `pull --rebase` 和 3 次重试。 |
| 定时任务不再运行 | 公开仓库长期没有活动时，GitHub 会停用定时工作流 | Actions 页面重新 Enable。 |

可以调整的环境变量（在 `scrape.yml` 的 Scrape 步骤下加 `env:`）：

- `KEEP_DAYS`：数据保留天数，默认 35
- `MAX_PDFS`：单次最多下载的 PDF 数，默认 300
- `PAUSE_SECONDS`：两次下载的间隔，默认 0.6

## 8. 数据格式

`data/notices.json`（网页读取）：

```json
{
  "schema": 2,
  "updated_at": "2026-11-02T20:41:05+11:00",
  "last_success_at": "2026-11-02T20:40:12+11:00",
  "scanned_days": ["2026-10-30", "2026-11-02"],
  "partial_days": {"2026-11-02": "14:05"},
  "last_run": {"ok": true, "list_pages_ok": 2, "substantial_found": 57, "pdf_checked": 41,
               "skipped_noise": 16, "deferred": 0, "errors": []},
  "watchlist": ["Paradice Investment Management", "..."],
  "notices": [{
    "id": "03142997", "code": "SLC", "lodged_code": "", "company": "Superloop Limited",
    "date": "2026-10-30", "time": "16:10",
    "headline": "Becoming a substantial holder",
    "url": "https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId=03142997",
    "pdf_url": "https://www.asx.com.au/asxpdf/20261030/pdf/xxxx.pdf",
    "price_sensitive": false, "type": "becoming",
    "change_date": "2026-10-28", "voting_power": [5.12],
    "holder": "Paradice Investment Management", "confidence": "高"
  }]
}
```

- `code`：公告涉及的股票。申报方自己是上市公司时，ASX 会把公告挂在申报方的代码下（标题 “... for EOL”），这时 `code` 是标的 EOL，`lodged_code` 是挂靠的代码（例如 WMI）；其他情况 `lodged_code` 为空
- `scanned_days`：抓到过公告列表的日子；`partial_days`：其中只抓到一部分的日子和抓取时间（悉尼时间晚上 9 点前读的“今日公告”）。下一个交易日再抓一次，上一交易日列表会把它补全
- `type` 取值：`becoming` / `change` / `ceasing` / `other`（标题看不出类型时，按 PDF 里的表格编号 603/604/605 判断）
- `voting_power`：变动类为 `[之前, 现在]`，新进类为 `[现在]`，退出类为 `[]`
- `confidence` 取值：
  - `高`：PDF 第一节解析出的申报方名称命中
  - `中`：名称没解析出来，但第一节原文命中
  - `低`：PDF 无文字，仅凭标题 “from XXX” 命中

`data/all_notices.json`（内部存储）：`{"schema":2,"entries":{id: entry},"scanned_days":[…],"partial_days":{…},"last_success_at":…}`。`entry.parser` 记录解析规则版本，`PARSER_VERSION` 升级后旧记录会自动重读一次。

- `entry.status` 取值：
  - `new`：刚发现，还没处理
  - `parsed`：已解析
  - `unreadable`：PDF 读不出文字（基本都是扫描件）
  - `noise`：标题显示是噪音申报方，未下载
  - `skipped`：404 或文件过大
  - `error`：下载或解析失败（包括 PDF 打不开、多半是下载不完整），会重试，`attempts` 最多 3 次

第一版的 `data/notices.json` 和 `data/seen.json` 会自动迁移，迁移后 `seen.json` 会被删除。

## 9. 已知限制

- 补抓依赖 ASX 网站的全市场公告接口（无公开文档，最多约 1 万条，平时约两三周，财报季可能只有一周左右）。接口改版或读不到时，退回只读当天和上一交易日的列表页，网页会提示。
- 扫描件 PDF（约一成，多是小公司和个人申报）用 OCR 识别（400dpi、tesseract --psm 6，最多前 3 页；单次运行最多 60 份、总共 12 分钟，超出的留到下次）。字迹模糊或手写的仍读不出，识别出的数字可能有错，所以扫描件只认带 % 的比例，低于 5% 的整组丢弃。
- 养老金只看得到自己操盘的部分；外包给基金经理的仓位以基金经理名义申报。
- 除了澳洲 603/604/605，还能读新西兰双重上市公司的 FMC Act 表格和 James Hardie 的爱尔兰 TR-1 表格；其他自制格式可能读不出名称或比例。
- 持股比例和变动日来自 PDF 文本解析，表格排版特殊的 PDF 可能识别错，网页上都附有原文链接。
- 只覆盖持股 5% 以上的申报。5% 以上每次小于 1% 的变动不需要申报，所以看不到。
- 只用于个人研究，请求频率很低（只在点按钮时运行，下载之间有间隔）。

## 10. 代码审查记录

本版本做了两轮查错。每个问题都先写复现或测试确认，再修复并保留回归测试。

### 第一轮

| 级别 | 问题 | 修复 |
| --- | --- | --- |
| 高 | 目标公司名字和关注名单同名时误判。例如 BlackRock 申报持有 Soul Pattinson（SOL）的股份，被算成 Soul Pattinson 在申报；关于 WAM Capital 的公告被算成 Wilson。原因是全文兜底匹配。 | 只用 PDF 第一节解析出的申报方名称匹配，去掉全文兜底，剔除 “To Company Name/Scheme” 行。 |
| 高 | 排队中的工作流基于旧提交运行，推送时 non-fast-forward 失败。 | checkout 时取分支最新提交；推送前 `pull --rebase`，最多重试 3 次。 |
| 高 | 网页用设备时钟判断哪次运行是自己触发的，手机时钟偏差超过 1 分钟就会白等 20 分钟。 | 触发前记录最新的 run id，之后找 id 更大的那次运行。 |
| 中 | 同一天内按时间排序错误（“4:10PM”排在“10:05AM”前）。 | 统一用 24 小时制 `HH:MM`。 |
| 中 | 变动前后比例相同（6.00% → 6.00%）时被去重，只剩一个值。 | 去掉去重。 |
| 中 | `class="pricesens"` 写在图标上时，价格敏感标记漏判。 | 同时判断单元格和图标。 |
| 中 | 中间页给的是 announcements.asx.com.au 绝对地址时，被拼成 www.asx.com.au。 | 正确处理绝对地址。 |
| 中 | `load_watchlist` 的默认参数在函数定义时就固定了（测试中发现）。 | 改为运行时取值。 |
| 中 | 改了关注名单后，旧公告不会重新匹配，删掉的申报方记录还留着。 | 保存全部大股东公告的解析结果，每次运行按当前名单重建 `notices.json`。 |
| 中 | 下载失败的公告没有重试上限；扫描版 PDF 被静默漏掉。 | 加上 `status` 和 `attempts`（最多 3 次）；读不出文字时，按标题 “from XXX” 做低可信度匹配。 |
| 中 | 列表页全部失败时，仍然更新“数据更新于”，工作流也显示成功。 | 新增 `last_success_at` 和 `last_run.ok`；失败时退出码为 2，工作流标红，GitHub 会发邮件；错误信息仍然提交，供网页显示。 |
| 中 | 网页没有令牌时读 Pages 上的文件，可能比仓库里的旧。 | 先用 GitHub API（公开仓库不需要令牌）读最新文件，失败再读 Pages。 |
| 中 | “数据覆盖”提示把 10 个交易日写死，有公众假期时提示永远不会消失。 | 按工作日计算，允许 1 天误差。 |
| 中 | 手机上表格要横向拖动。 | 小屏幕改为卡片布局。 |
| 中 | 只有 `queued` 显示“排队中”，`pending` 和 `waiting` 被显示成“正在抓取”。 | 补全状态映射。 |
| 低 | 文字日期（例如 28 October 2026）识别不了。 | 支持文字月份，统一输出 ISO 日期。 |
| 低 | 当天没有公告（例如假期）被误报为“页面结构变了”。 | 区分“认出结构但为空”和“认不出结构”。 |
| 低 | 加密 PDF 读不出来；超大 PDF 没有下载上限。 | 先用空密码解密；流式下载，上限 25MB；单次最多 300 份，标题含关注名单缩写的优先。 |
| 低 | 写文件时中途崩溃可能留下半个 JSON。 | 先写临时文件，再原子替换。 |
| 低 | 链接没有限制协议（可能出现 `javascript:`）。 | 前后端都只允许 http/https。 |
| 低 | manifest 图标 purpose 写成 “any maskable” 组合，Chrome 会警告。 | 拆成两条。 |

### 第二轮

| 级别 | 问题 | 修复 |
| --- | --- | --- |
| 严重 | 单份公告解析时抛出非网络异常，会让整次运行崩溃；而且这份公告下次还会再触发，导致永久停摆。 | 每份公告单独捕获异常，记为 `error` 并限次重试；日志里打印 traceback。 |
| 高 | PDF 按列排版时，“Name”标签和值分开，会把 “The holder became…” 这类标签当成申报方名称，导致真实申报方漏判。 | 名称候选必须通过标签过滤，最多往后看 4 行；解析不出时退回第一节原文匹配。 |
| 中 | 中等可信度匹配用的原文可能延伸到第二节，而第二节可能提到目标公司。 | 原文截到第二节标题为止。 |
| 中 | 变动日只看事件短语后 40 个字符，按列排版时取不到。 | 扩大到 250 个字符，取最先出现的日期。 |
| 中 | 中间页如果还有条款类 PDF 链接，可能下载错文件。 | 优先用 `pdfURL` 和 `/asxpdf/` 路径。 |
| 中 | 手机卡片里，“可信度”和公司名被挤到右侧一列。 | 合并为一个内容块。 |
| 低 | 标签过滤误伤 “The Vanguard Group” 这类以 The 开头的名称。 | 收窄过滤规则。 |
| 低 | “To: Company Name / Scheme” 这种写法识别不了。 | 放宽正则。 |
| 低 | 大股东公告数量统计包含两个列表页的重复。 | 按 id 去重计数。 |
| 低 | 旧版迁移来的记录，可信度会被标题匹配覆盖。 | 调整匹配顺序。 |
| 低 | 请求缺少 Accept 头，可能被防火墙判为机器人。 | 补上 Accept 头。 |
| 低 | 中途崩溃留下的 `.tmp` 文件可能被提交。 | 加 `.gitignore`。 |
| 低 | 抓取成功但读取结果失败时，状态栏仍显示旧数据的时间。 | 明确提示读取失败。 |
| 低 | 深色模式下手机状态栏颜色不对。 | 分别设置浅色和深色的 theme-color。 |
| 工具 | 没有自动测试。 | 新增 `tests.yml`，推送代码时自动跑 pytest。 |

### 第三轮（2026-11-02，第一次真实运行后）

| 级别 | 问题 | 修复 |
| --- | --- | --- |
| 高 | 真实 603/604 多为模板填写式，默认提取时“Name”和名称分家，约 4 成公告读不出申报方，变动日取成上一次申报的日期。 | 版面模式和默认模式都读，每个字段取先解析出的；支持 “29 /10/2026” 和没有 Name 标签的格式；识别 ASX 水印；加 `PARSER_VERSION`。 |

### 第四轮（2026-11-03，3 个交易日、119 条真实公告）

| 级别 | 问题 | 修复 |
| --- | --- | --- |
| 高 | 持股比例表分多行加合计时取了第一行，例如 CFS 母公司新进 Karoon 显示 0.05%（实际合计 5.00%）。 | 只看第二节；优先合计行，否则新进取最大值、变动取比例最大的一行。 |
| 高 | 表头写 “Voting power (%)”、数字不带 % 时一个都读不出（Perennial 的 AMX 公告）；小数点后超过 4 位（5.298524%）也读不出。 | 支持不带 % 的小数和最多 8 位小数。 |
| 高 | 申报方自己上市时，公告挂在申报方代码下（“... for EOL”），网页把 WAM 减持 Energy One 显示成 WMI。3 天里有 8 条这样挂错代码的公告。 | 从标题取真正的标的代码，网页注明挂在哪个代码下；搜索两个代码都能搜到。 |
| 中 | 噪音缩写不分大小写：“from Ms J Smith” 会被当成 Morgan Stanley 跳过；“Top End Pty Ltd” 会低可信度匹配到 Thorney（TOP）。 | 全大写缩写区分大小写。 |
| 中 | 改了名单后，之前被判为噪音的公告永远不会补下载。 | 每次运行重新判断噪音记录。 |
| 中 | 改成手动抓取后，白天点一次会把当天记为“已覆盖”，之后发的公告漏掉也不提示。 | 记录只抓到一部分的日子，网页提示什么时候再点能补全。 |
| 中 | PDF 下载不完整时被永久记为“读不出文字”。 | 改为下次重试。 |
| 中 | 新西兰 FMC Act 表格（Gentrack 等双重上市公司）和爱尔兰 TR-1 表格（James Hardie）完全读不出。 | 新增两种格式的名称、比例、日期解析。 |
| 中 | 网页 403 报错不区分“令牌缺权限”和“请求次数用完”，用户照着改也不知道改哪里。 | 读取 GitHub 返回的原因，分别给出具体操作步骤。 |
| 低 | 标题看不出类型（“Substantial Holder Notice”）时比例只读一个值。 | 按 PDF 里的表格编号判断类型。 |
| 低 | 公司名带 “(ASX: EOL)”、“NWH.AX” 等尾巴；“To: XXX”、没有 “To” 的公司名读不出。 | 显示前清理，支持更多写法。 |
| 低 | 网页标题写死“10 个主动型投资者”。 | 按名单人数显示。 |
| 中 | 2018 版表格的栏目名 “NFPFRN” 被当成申报方名称（TVL），名称读错后不会再用第一节原文匹配。 | 加入标签过滤。 |

### 第五轮（2026-11-03，加入养老金和 OCR，合并前审查三次）

| 轮次 | 发现的问题 | 处理 |
| --- | --- | --- |
| 1 正确性 | Pillow 只在测试依赖里（reportlab 顺带装的），正式环境 OCR 每份都会报错，测试也发现不了。 | 加进 `requirements.txt`；`ocr_available()` 检查 Pillow；用只装正式依赖的干净环境验证。 |
| 1 正确性 | 新的第一节标题规则在 “substantial” 处截断，把 “shareholder” 当成名称（花旗格式）。 | 规则吃掉 holder / shareholder，并加入标签过滤；31 份文字版真实样本和正式版逐一比对，结果完全一致。 |
| 1 正确性 | 页面尺寸异常的 PDF 按 400dpi 渲染会撑爆内存；OCR 名额用完时被计入下载数。 | 图片最长边限 5000 像素；改正计数。 |
| 2 异常 | tesseract 某次没装上时，那次的扫描件永久标成“读不出”。 | 记录是否真正做过 OCR，OCR 恢复后自动重读。 |
| 2 异常 | 加密 PDF 渲染失败被当成下载失败，重试 3 次并一直报错。 | 改为直接“读不出”。 |
| 2 异常 | 扫描件识别没有总时长上限，可能超过工作流 30 分钟被强杀、整次数据丢失。 | 单次运行 OCR 总时长 12 分钟，超出的留到下次。 |
| 2 异常 | “打字封面信 + 扫描表格”的 PDF 有文字层，不会触发 OCR，什么都读不出。 | 文字层既没有名称也找不到第一节时，再用 OCR 读一遍。 |
| 2 异常 | OCR 把 “of the” 认成 “ofthe”、2026 认成 2626、比例栏糊成 “te1.215”，导致日期和比例读错。 | 词间允许缺空格；丢弃离谱年份；扫描件只认带 % 的数字，低于 5% 的丢弃；新进类低于 5% 一律丢弃。 |

### 第六轮（2026-11-04，抓取成功率和准确率；合并前审查三次）

用 GitHub Actions 把当时全部 133 份真实公告的文字导出成语料，逐条做合理性检查（类型和表格编号是否一致、比例是否合理、变动日是否缺失或离谱、名称是否像标签），再人工核对被标出的公告。可疑项从 36 份降到 26 份，剩下的大多是原文本身就缺（花旗自制表格没有变动日、晚报的公告、扫描件字迹模糊）。

| 级别 | 问题 | 修复 |
| --- | --- | --- |
| 高 | 只能读到当天和上一交易日的列表，没点按钮的日子永久漏掉（3 天只覆盖到 3 个交易日）。 | 接入 ASX 网站的全市场公告接口，每次点按钮往前翻约 1 万条，补回最近两三周；去重后实测从 3 个交易日补到 16 个交易日，新增 475 条大股东公告，已存的 3 天没有一条重复。 |
| 高 | 自制表格没有 “1. Details of substantial holder” 标题时读不出申报方，漏掉了 Perennial 新进 PC Gold（PC2）。 | 支持 “Name: …” 和 “Name of Substantial Holder: …”（只在第二节之前、表格标题之后找，避免读到封面信或签名栏）。 |
| 高 | L1 Capital 和 Platinum 合并后改用 “L1 Group Limited” 申报（GLS 13.16% → 9.16%），没匹配上。 | 名单加入 L1 Group；Tattarang 加入 Wyloo。 |
| 高 | JPMorgan（“28/October/2026”）、Morgan Stanley（美式 “October 28, 2026”）的变动日全部读不出；方格填写的 “28 10 2026”、“28102026” 也读不出。 | 支持这些写法；美式 月/日 自动对调。 |
| 中 | 填写内容是贴在模板上的图片时（TVL），文字层只有空白模板，又因为找得到第一节而不做 OCR。 | 文字层读不出申报方时也做 OCR，只用 OCR 补缺的字段，数字仍以文字层为准。 |
| 中 | 名称写 “See Annexure A” 被当成申报方；扫描件把 “> 2. Details of voting power” 当成名称；扫描件把说明文字里的比例拼进变动比例（99% → 14.45%）。 | 加入标签过滤和杂字符清理；扫描件的变动比例必须出自同一行。 |
| 中 | 美国公司 CDI 的 SEC 13G 表格读不出。 | 新增 13G/13D 解析（申报方、比例、事件日期、公司名）。 |
| 中 | 标题类型和表格不一致（604 表格配“退出”标题）。 | PDF 开头单独一行写着 Form 603/604/605 时以表格为准。 |
| 中 | 周末打开时，“今日公告”页上的周五公告被记成“只抓到一部分”。 | 按每条公告的日期判断，只有今天的才可能不完整。 |
| 中 | PDF 返回 404 时被永久跳过，刚发布还没生成 PDF 的公告会漏掉。 | 404 改为下次运行重试（最多 3 次）。 |
| 低 | 覆盖天数把周末算进去；补抓后网页提示文字不对（“过了今天就补不回来”）。 | 只算工作日；提示文字按补抓是否成功调整。 |

合并前三轮审查发现并修复：

| 轮次 | 发现的问题 | 处理 |
| --- | --- | --- |
| 1 正确性 | 补抓翻到后面某一页时网络出错，前面已经翻到的结果会被整个丢掉。 | 保留已经拿到的部分，记一条提醒，更早的下次再补。语料新旧对比：133 份里 20 份结果变化，逐条人工确认全部是改对或改成留空，其余 113 份不变。 |
| 2 异常 | 安静时期 1 万条可能跨一个多月，超过 35 天保留期的公告会每次被下载、清掉、再下载。 | 只补 16 天窗口以内的；覆盖天数也不早于窗口起点。 |
| 2 异常 | 列表页和接口对同一份公告的时间若差一分钟，会显示两条。 | 再按（申报方、股票、类型、变动日、比例）去重一次，优先保留列表页那条。 |
| 3 真实环境 | 接口下载的 PDF 都是 AES 加密（只限制编辑，空密码能打开），正式环境缺 cryptography，第一次真实运行 169 份全部打不开；本地测试的 PDF 都没加密，发现不了。 | 加入 cryptography；pypdf 打不开时改用 pdfium 读文字层；真要密码的 PDF 按“读不出”处理，不反复重试。在 GitHub 上用同一批公告的网站版和接口版 PDF 逐份对比解析结果。 |

### 测试结果（交付时）

- `python -m pytest -q`：92 passed（其中 29 个用 `tests/fixtures` 里的真实 ASX 公告 PDF，每份只保留前两页；在只装 `requirements-dev.txt` 的 Python 3.12 干净环境里验证）。
- `python tests/frontend_check.py`：33 项检查全部通过。覆盖桌面和手机布局、XSS 链接过滤、成功和失败的抓取流程、令牌失效回退、令牌缺权限和请求次数用完的提示、挂靠代码显示、当天只抓到一部分的提示。
- 尚未验证：真实 ASX 页面和 PDF 的结构（开发环境无法访问 asx.com.au）、GitHub Pages 构建时间、定时任务的长期保活。

### 开发依赖里的一条无害警告

在开发环境跑测试时，可能出现 `RequestsDependencyWarning: ... chardet (7.x) doesn't match`。这是 reportlab 带进来的 chardet 引起的，只在测试环境出现。只装 `requirements.txt` 的生产环境没有这个警告，不影响功能。
