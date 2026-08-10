"""Probe public pages for Playwright harvest feasibility."""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
OUT = Path("reports/e2e_shots")
OUT.mkdir(parents=True, exist_ok=True)

URLS = [
    "http://biaozhun.osta.org.cn/",
    "https://www.osta.org.cn/",
    "https://vocational.smartedu.cn/",
]


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=EDGE, headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        for i, u in enumerate(URLS):
            print("===", u)
            try:
                page.goto(u, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                print("title:", page.title())
                print("url_final:", page.url)
                body = page.locator("body").inner_text(timeout=5000)
                print("body_len:", len(body))
                print("body_head:", body[:300].replace("\n", " | "))
                links = page.eval_on_selector_all(
                    "a[href]",
                    """els => els.slice(0, 20).map(e => ({
                      t: (e.innerText||'').trim().slice(0, 50),
                      h: e.href
                    }))""",
                )
                print("links:", links[:10])
                # inputs / search
                inputs = page.eval_on_selector_all(
                    "input, select, button",
                    """els => els.slice(0, 25).map(e => ({
                      tag: e.tagName,
                      type: e.getAttribute('type'),
                      name: e.getAttribute('name'),
                      id: e.id,
                      ph: e.getAttribute('placeholder'),
                      txt: (e.innerText||'').trim().slice(0,30)
                    }))""",
                )
                print("controls:", inputs[:15])
                page.screenshot(path=str(OUT / f"probe_{i}.png"), full_page=False)
            except Exception as e:
                print("ERR", type(e).__name__, e)
        browser.close()


if __name__ == "__main__":
    main()
