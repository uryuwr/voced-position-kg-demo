"""H2 探测：职业标准系统是否需登录、能采到什么。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
OUT = Path("reports/h2_probe")
OUT.mkdir(parents=True, exist_ok=True)

URLS = [
    "https://www.osta.org.cn/skillStandard",
    "https://www.osta.org.cn/career",
    "http://biaozhun.osta.org.cn/",
    "http://osta.mohrss.gov.cn/skillStandard",
]


def dump_page(page, tag: str) -> dict:
    page.wait_for_timeout(2500)
    info = {
        "url": page.url,
        "title": page.title(),
        "body_len": 0,
        "body_head": "",
        "login_hints": [],
        "tables": 0,
        "rows_sample": [],
        "pdf_links": [],
        "api_like": [],
    }
    try:
        body = page.locator("body").inner_text(timeout=8000)
        info["body_len"] = len(body)
        info["body_head"] = body[:800]
        low = body.lower()
        for kw in ("登录", "注册", "请先登录", "验证码", "短信", "统一身份", "权限", "download", "下载"):
            if kw in body or kw in low:
                info["login_hints"].append(kw)
    except Exception as e:
        info["body_err"] = str(e)

    try:
        info["tables"] = page.locator("table").count()
        rows = page.eval_on_selector_all(
            "table tr",
            """els => els.slice(0, 15).map(tr =>
              Array.from(tr.querySelectorAll('td,th')).map(c => (c.innerText||'').trim().slice(0,40)).join(' | ')
            )""",
        )
        info["rows_sample"] = rows
    except Exception:
        pass

    try:
        info["pdf_links"] = page.eval_on_selector_all(
            "a[href*='pdf'], a[href*='PDF'], a[href*='download']",
            """els => els.slice(0, 30).map(e => ({t:(e.innerText||'').trim().slice(0,60), h:e.href}))""",
        )
    except Exception:
        pass

    # network-ish: any visible list items
    try:
        info["list_items"] = page.eval_on_selector_all(
            "li, .el-table__row, .ant-table-row, tr",
            """els => els.slice(0, 20).map(e => (e.innerText||'').trim().replace(/\\s+/g,' ').slice(0,120))""",
        )
    except Exception:
        pass

    page.screenshot(path=str(OUT / f"{tag}.png"), full_page=True)
    return info


def main() -> None:
    report = {"pages": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=EDGE, headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        # capture XHR JSON
        apis = []

        def on_response(resp):
            try:
                ct = (resp.headers.get("content-type") or "").lower()
                u = resp.url
                if any(x in u for x in ("skill", "standard", "biaozhun", "api", "list", "query")):
                    apis.append({"url": u, "status": resp.status, "ct": ct[:80]})
                if "json" in ct and resp.status == 200 and any(
                    x in u for x in ("skill", "standard", "list", "query", "page")
                ):
                    try:
                        apis[-1]["json_keys"] = list((resp.json() or {}).keys())[:20] if isinstance(resp.json(), dict) else type(resp.json()).__name__
                    except Exception:
                        pass
            except Exception:
                pass

        page.on("response", on_response)

        for i, u in enumerate(URLS):
            print("GOTO", u)
            try:
                page.goto(u, wait_until="domcontentloaded", timeout=60000)
                # accept checkbox / 继续访问 if any
                for sel in ("text=继续访问", "text=同意", "#checkbox_read", "text=我已阅读"):
                    try:
                        loc = page.locator(sel).first
                        if loc.count() and loc.is_visible(timeout=800):
                            loc.click(timeout=1000)
                            page.wait_for_timeout(500)
                    except Exception:
                        pass
                info = dump_page(page, f"p{i}")
                info["start_url"] = u
                report["pages"].append(info)
                print(" ->", info["url"], "title=", info["title"], "login=", info["login_hints"][:5])
                print("    rows", len(info.get("rows_sample") or []), "pdf", len(info.get("pdf_links") or []))
            except Exception as e:
                print(" ERR", e)
                report["pages"].append({"start_url": u, "error": str(e)})

        report["apis_seen"] = apis[:50]
        browser.close()

    (OUT / "probe_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("wrote", OUT / "probe_report.json")


if __name__ == "__main__":
    main()
