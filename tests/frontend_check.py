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

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
TODAY = dt.datetime.now(dt.timezone(dt.timedelta(hours=11))).date()


def day(n):
    return (TODAY - dt.timedelta(days=n)).isoformat()


SAMPLE = {
    "schema": 2, "updated_at": f"{day(0)}T20:41:00+11:00", "last_success_at": f"{day(0)}T20:41:00+11:00",
    "scanned_days": [day(1), day(0)],
    "last_run": {"ok": True, "list_pages_ok": 2, "substantial_found": 40, "pdf_checked": 30, "skipped_noise": 10, "deferred": 0, "errors": []},
    "watchlist": ["Paradice Investment Management", "Wilson Asset Management", "Regal Funds Management", "L1 Capital", "Perennial",
                  "Celeste Funds Management", "Ellerston Capital", "Thorney / Tiga Trading", "Washington H. Soul Pattinson", "Tattarang (Andrew Forrest)"],
    "notices": [
        {"id": "1", "code": "SLC", "company": "Superloop Limited", "date": day(0), "time": "16:10", "headline": "Becoming a substantial holder",
         "url": "https://www.asx.com.au/a", "pdf_url": "https://www.asx.com.au/a.pdf", "price_sensitive": False, "type": "becoming",
         "change_date": day(2), "voting_power": [5.12], "holder": "Paradice Investment Management", "confidence": "高"},
        {"id": "2", "code": "XYZ", "company": "", "date": day(1), "time": "09:05", "headline": "<img src=x onerror=alert(1)>",
         "url": "javascript:alert(1)", "pdf_url": "javascript:alert(1)", "price_sensitive": True, "type": "change",
         "change_date": "", "voting_power": [6.2, 7.4], "holder": "Regal Funds Management", "confidence": "中"},
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
    def __init__(self, data, conclusion="success", token_ok=True):
        self.data, self.conclusion, self.token_ok = data, conclusion, token_ok
        self.polls = 0
        self.dispatched = False

    def __call__(self, route, req):
        u = req.url
        auth = req.headers.get("authorization")
        if auth and not self.token_ok:
            return route.fulfill(status=401, body="{}")
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
        check(pg.locator("#rows tr").count() == 2, "近 14 天显示 2 条（25 天前的那条被过滤）")
        check("Superloop Limited" in pg.inner_text("#rows"), "代码下面显示公司名")
        check(pg.locator("#rows a[href^='javascript']").count() == 0, "javascript: 链接不会被渲染成可点击链接")
        check("<img src=x" in pg.inner_text("#rows"), "标题里的 HTML 按纯文本显示")
        check(not pg.locator("#coverage").is_hidden(), "数据天数不足时显示覆盖提示")
        check("悉尼时间" in pg.inner_text("#status"), "状态栏显示数据时间")
        pg.click("#daysSeg button[data-days='30']")
        check(pg.locator("#rows tr").count() == 3, "切到近 30 天后显示 3 条")
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
        check(pg.locator("#rows tr").count() == 2, "仍然显示缓存数据")
        check("令牌无效" in pg.inner_text("#status"), "提示令牌无效")
        b.close()
    httpd.shutdown()
    print("ALL FRONTEND CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print("FAILED:", e)
        sys.exit(1)
