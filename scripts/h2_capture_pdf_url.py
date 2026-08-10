"""Click 点击查看 and capture PDF network URL."""
from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
OUT = Path("reports/h2_probe/pdf_urls.json")


def main() -> None:
    hits = []

    def on_response(resp):
        u = resp.url
        ct = (resp.headers.get("content-type") or "").lower()
        if "pdf" in ct or ".pdf" in u.lower() or "skillStandard" in u or "file" in u:
            try:
                body = resp.body()
            except Exception:
                body = b""
            hits.append(
                {
                    "url": u,
                    "status": resp.status,
                    "ct": ct,
                    "len": len(body),
                    "is_pdf": body[:4] == b"%PDF",
                }
            )

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=EDGE, headless=True)
        page = browser.new_page()
        page.on("response", on_response)
        page.goto(
            "https://www.osta.org.cn/skillStandard",
            wait_until="networkidle",
            timeout=90000,
        )
        page.wait_for_timeout(1500)
        page.locator("a.arco-link", has_text="点击查看").first.click(timeout=5000)
        page.wait_for_timeout(5000)
        # try extract iframe/src
        extras = page.eval_on_selector_all(
            "iframe, embed, canvas, .pdfViewer",
            """els => els.map(e => ({
              tag: e.tagName,
              src: e.getAttribute('src'),
              id: e.id,
              cls: e.className
            }))""",
        )
        page.screenshot(path="reports/h2_probe/detail_modal2.png")
        # close
        try:
            page.locator(".arco-modal-close-btn").first.click(timeout=2000)
        except Exception:
            pass
        browser.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps({"hits": hits, "dom": extras}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"hits": hits, "dom": extras}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
