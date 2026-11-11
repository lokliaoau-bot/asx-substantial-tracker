"""网页的端到端检查（可选）：用无头浏览器打开 index.html，模拟 GitHub API。

需要：pip install playwright && python -m playwright install chromium
运行：python tests/frontend_check.py      （全部通过会打印 ALL FRONTEND CHECKS PASSED）
"""
import datetime as dt
import functools
import http.server
import json
import shutil
import socketserver
import sys
import tempfile
import threading
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
TODAY = dt.datetime.now(ZoneInfo("Australia/Sydney")).date()


def day(n):
    return (TODAY - dt.timedelta(days=n)).isoformat()


SAMPLE = {
    "schema": 2, "updated_at": f"{day(0)}T20:41:00+11:00", "last_success_at": f"{day(0)}T20:41:00+11:00",
    "scanned_days": [day(1), day(0)],
    "partial_days": {day(0): "14:05"},
    "last_run": {"ok": True, "list_pages_ok": 2, "substantial_found": 40, "pdf_checked": 30, "skipped_noise": 10, "deferred": 0,
                 "backfill": {"ok": True, "from": day(20), "added": 300}, "errors": []},
    "watchlist": ["Paradice Investment Management", "Wilson Asset Management", "Regal Funds Management", "L1 Capital", "Perennial",
                  "Celeste Funds Management", "Ellerston Capital", "Thorney / Tiga Trading", "Washington H. Soul Pattinson", "Tattarang (Andrew Forrest)",
                  "AustralianSuper", "UniSuper"],
    "groups": {**{n: "主动基金" for n in ["Paradice Investment Management", "Wilson Asset Management", "Regal Funds Management", "L1 Capital",
                                        "Perennial", "Celeste Funds Management", "Ellerston Capital", "Thorney / Tiga Trading",
                                        "Washington H. Soul Pattinson", "Tattarang (Andrew Forrest)"]},
               "AustralianSuper": "养老金", "UniSuper": "养老金"},
    "notices": [
        {"id": "1", "code": "SLC", "company": "Superloop Limited", "date": day(0), "time": "16:10", "headline": "Becoming a substantial holder",
         "url": "https://www.asx.com.au/a", "pdf_url": "https://www.asx.com.au/a.pdf", "price_sensitive": False, "type": "becoming",
         "change_date": day(2), "voting_power": [5.12], "holder": "Paradice Investment Management", "confidence": "高"},
        {"id": "2", "code": "XYZ", "company": "", "date": day(1), "time": "09:05", "headline": "<img src=x onerror=alert(1)>",
         "url": "javascript:alert(1)", "pdf_url": "javascript:alert(1)", "price_sensitive": True, "type": "change",
         "change_date": "", "voting_power": [6.2, 7.4], "holder": "Regal Funds Management", "confidence": "中"},
        {"id": "4", "code": "EOL", "lodged_code": "WMI", "company": "Energy One Limited", "date": day(1), "time": "17:00",
         "headline": "Change in substantial holding for EOL", "url": "https://www.asx.com.au/d", "pdf_url": "https://www.asx.com.au/d.pdf",
         "price_sensitive": False, "type": "change", "change_date": day(4), "voting_power": [9.76, 7.68],
         "holder": "Wilson Asset Management", "confidence": "高"},
        {"id": "5", "code": "SGH", "lodged_code": "", "company": "SGH Ltd", "date": day(0), "time": "18:00",
         "headline": "Change in substantial holding", "url": "https://www.asx.com.au/e", "pdf_url": "https://www.asx.com.au/e.pdf",
         "price_sensitive": False, "type": "change", "change_date": day(3), "voting_power": [6.06, 7.11],
         "holder": "AustralianSuper", "group": "养老金", "confidence": "高", "ocr": True,
         "insight": {"action": "add_dip", "label": "逢低加仓", "tone": "bull", "confidence": "中", "style": "养老金",
                     "tags": ["多家同向", "<b>换股</b>"],
                     "reasons": ["持股比例 6.06% → 7.11%，持股数增加 420 万股。", "<img src=x onerror=window.__xss=1>买入时股价处在低位。"]}},
        {"id": "3", "code": "OLD", "company": "Old Co", "date": day(25), "time": "10:00", "headline": "Ceasing to be a substantial holder",
         "url": "https://www.asx.com.au/c", "pdf_url": "", "price_sensitive": False, "type": "ceasing",
         "change_date": day(26), "voting_power": [], "holder": "Regal Funds Management", "confidence": "低"},
    ],
}
FAILED = {**SAMPLE, "last_run": {**SAMPLE["last_run"], "ok": False, "list_pages_ok": 0, "errors": ["读取 todayAnns.do 失败：HTTP 503"]}}


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(directory):
    handler = functools.partial(QuietHandler, directory=str(directory))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class GitHubMock:
    def __init__(self, data, conclusion="success", token_ok=True, dispatch_error=None):
        self.data, self.conclusion, self.token_ok = data, conclusion, token_ok
        self.dispatch_error = dispatch_error      # (status, message, headers)
        self.polls = 0
        self.dispatched = False

    def __call__(self, route, req):
        u = req.url
        auth = req.headers.get("authorization")
        if auth and not self.token_ok:
            return route.fulfill(status=401, body="{}")
        if u.endswith("/dispatches") and self.dispatch_error:
            st, msg, hdrs = self.dispatch_error
            return route.fulfill(status=st, content_type="application/json", headers=hdrs, body=json.dumps({"message": msg}))
        if u.endswith("/dispatches"):
            self.dispatched = True
            return route.fulfill(status=204, body="")
        if "/runs?" in u:
            runs = [{"id": 100, "status": "completed", "conclusion": "success", "created_at": "2020-01-01T00:00:00Z", "html_url": "https://x/100"}]
            if self.dispatched:
                self.polls += 1
                # 故意把 created_at 写成很早以前，验证不依赖手机时钟
                runs.insert(0, {"id": 101, "status": "in_progress" if self.polls < 2 else "completed",
                                "conclusion": None if self.polls < 2 else self.conclusion,
                                "created_at": "2020-01-01T00:00:00Z", "html_url": "https://x/101"})
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"workflow_runs": runs}))
        if "/contents/data/notices.json" in u:
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(self.data))
        return route.fulfill(status=404, body="{}")


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok -", msg)


def main():
    tmp = Path(tempfile.mkdtemp())
    for name in ("index.html", "manifest.webmanifest", "icon-192.png", "icon-512.png", "apple-touch-icon.png"):
        shutil.copy(ROOT / name, tmp / name)
    (tmp / "data").mkdir()
    (tmp / "data" / "notices.json").write_text(json.dumps(SAMPLE), encoding="utf-8")
    httpd = serve(tmp)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/index.html"
    shots = Path(tempfile.gettempdir())

    with sync_playwright() as p:
        b = p.chromium.launch()

        print("1. 打开页面（读 Pages 上的数据）")
        pg = b.new_page(viewport={"width": 1200, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("dialog", lambda d: (errs.append("dialog: " + d.message), d.dismiss()))
        pg.goto(url)
        pg.wait_for_timeout(800)
        check(pg.locator("#rows tr").count() == 4, "近 14 天显示 4 条（25 天前的那条被过滤）")
        check("12 个投资者（10 家主动基金、2 家养老金）" in pg.inner_text("#lede"), "标题里的人数按关注名单和分组计算")
        check(pg.locator("#holders .group-title").count() == 2, "申报方卡片按主动基金 / 养老金分两组")
        check("扫描件·文字识别" in pg.inner_text("#rows"), "扫描件识别出来的记录有标注")
        check(pg.locator("#rows details.ins").count() == 1 and "逢低加仓" in pg.inner_text("#rows details.ins summary"),
              "有操作解读的公告显示标签，没有的显示 —")
        check(pg.locator("#rows details.ins summary .chip").count() == 2 and "多家同向" in pg.inner_text("#rows details.ins summary")
              and pg.locator("#rows details.ins summary b").count() == 0, "解读的小标签显示出来，且不会被当成 HTML")
        pg.click("#rows details.ins summary")
        check("持股数增加 420 万股" in pg.inner_text("#rows details.ins") and "推断" in pg.inner_text("#rows details.ins"),
              "点开标签能看到理由，并说明这是推断")
        check(pg.evaluate("window.__xss") is None and pg.locator("#rows details.ins img").count() == 0, "理由里的 HTML 不会被执行")
        pg.click("#groupSeg button[data-group='养老金']")
        check(pg.locator("#rows tr").count() == 1 and "AustralianSuper" in pg.inner_text("#rows"), "只看养老金")
        check(pg.locator("#holders .holder").count() == 2, "只看养老金时只显示养老金的卡片")
        pg.click("#groupSeg button[data-group='主动基金']")
        check(pg.locator("#rows tr").count() == 3, "只看主动基金")
        pg.click("#groupSeg button[data-group='all']")
        check("挂在 WMI 名下发布" in pg.inner_text("#rows") and "EOL" in pg.inner_text("#rows"),
              "挂在申报方代码下的公告显示真正的标的代码，并注明挂在哪个代码下")
        check(not pg.locator("#partial").is_hidden() and "14:05" in pg.inner_text("#partial") and "自动补全" in pg.inner_text("#partial"),
              "今天只抓到一部分时提示（有自动补抓时提示稍后再点即可）")
        check("自动补回最近两三周" in pg.inner_text("#coverage"), "覆盖提示说明会自动补抓")
        pg.fill("#q", "WMI")
        check(pg.locator("#rows tr").count() == 1, "按挂靠代码也能搜到")
        pg.fill("#q", "")
        check("Superloop Limited" in pg.inner_text("#rows"), "代码下面显示公司名")
        check(pg.locator("#rows a[href^='javascript']").count() == 0, "javascript: 链接不会被渲染成可点击链接")
        check("<img src=x" in pg.inner_text("#rows"), "标题里的 HTML 按纯文本显示")
        check(not pg.locator("#coverage").is_hidden(), "数据天数不足时显示覆盖提示")
        check("悉尼时间" in pg.inner_text("#status"), "状态栏显示数据时间")
        pg.click("#daysSeg button[data-days='30']")
        check(pg.locator("#rows tr").count() == 5, "切到近 30 天后显示 5 条")
        pg.screenshot(path=str(shots / "fe_desktop.png"), full_page=True)
        check(not errs, f"没有脚本错误 {errs}")

        print("2. 手机布局")
        m = b.new_page(viewport={"width": 390, "height": 844})
        m.goto(url)
        m.wait_for_timeout(800)
        check(m.evaluate("getComputedStyle(document.querySelector('thead')).display") == "none", "手机上表头隐藏，改为卡片")
        check(m.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "手机上没有横向滚动")
        check(m.evaluate("[...document.querySelectorAll('#rows td')].every(td => td.children.length <= 1)"),
              "手机卡片里每格只有一个内容块（可信度、公司名不会被挤到右边）")
        m.screenshot(path=str(shots / "fe_mobile.png"), full_page=True)

        print("3. 点按钮抓取（成功）")
        gh = GitHubMock(SAMPLE)
        pg = b.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.route("https://api.github.com/**", gh)
        pg.goto(url)
        pg.wait_for_timeout(500)
        pg.click("#runBtn")
        check("先在下方" in pg.inner_text("#status"), "没填令牌时提示先设置")
        pg.fill("#owner", "me"); pg.fill("#repo", "repo"); pg.fill("#token", "t0ken")
        pg.click("#saveCfg")
        pg.wait_for_timeout(500)
        pg.click("#runBtn")
        pg.wait_for_function("document.querySelector('#runBtn').disabled === false", timeout=30000)
        check(gh.dispatched, "已调用 workflow_dispatch")
        check("数据更新于" in pg.inner_text("#status"), "运行完成后刷新数据（不依赖设备时钟）")
        check(not errs, f"没有脚本错误 {errs}")

        print("4. 点按钮抓取（失败）")
        gh = GitHubMock(FAILED, conclusion="failure")
        pg = b.new_page()
        pg.route("https://api.github.com/**", gh)
        pg.goto(url)
        pg.evaluate("localStorage.setItem('ssr_owner','me');localStorage.setItem('ssr_repo','repo');localStorage.setItem('ssr_token','t')")
        pg.reload()
        pg.wait_for_timeout(500)
        pg.click("#runBtn")
        pg.wait_for_function("document.querySelector('#runBtn').disabled === false", timeout=30000)
        st = pg.inner_text("#status")
        check("没有成功" in st and "HTTP 503" in st, "失败时显示具体原因")
        check(not pg.locator("#issues").is_hidden(), "失败时显示红色提示条")

        print("5. 令牌失效时回退到 Pages 数据并提示")
        gh = GitHubMock(SAMPLE, token_ok=False)
        pg = b.new_page()
        pg.route("https://api.github.com/**", gh)
        pg.goto(url)
        pg.evaluate("localStorage.setItem('ssr_owner','me');localStorage.setItem('ssr_repo','repo');localStorage.setItem('ssr_token','bad')")
        pg.reload()
        pg.wait_for_timeout(800)
        check(pg.locator("#rows tr").count() == 4, "仍然显示缓存数据")
        check("令牌无效" in pg.inner_text("#status"), "提示令牌无效")

        print("6. 令牌缺 Actions 权限 / 请求次数用完时，给出不同的提示")
        for err, want in (((403, "Resource not accessible by personal access token", {}), "Actions 设为 Read and write"),
                          ((403, "API rate limit exceeded", {"x-ratelimit-remaining": "0"}), "请求次数")):
            gh = GitHubMock(SAMPLE, dispatch_error=err)
            pg = b.new_page()
            pg.route("https://api.github.com/**", gh)
            pg.goto(url)
            pg.evaluate("localStorage.setItem('ssr_owner','me');localStorage.setItem('ssr_repo','repo');localStorage.setItem('ssr_token','t')")
            pg.reload()
            pg.wait_for_timeout(500)
            pg.click("#runBtn")
            pg.wait_for_function("document.querySelector('#runBtn').disabled === false", timeout=30000)
            check(want in pg.inner_text("#status"), f"{err[1]} → 提示里有“{want}”")
        b.close()
    httpd.shutdown()
    print("ALL FRONTEND CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print("FAILED:", e)
        sys.exit(1)
