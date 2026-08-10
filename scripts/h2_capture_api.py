"""Capture network APIs used by osta skillStandard list + detail."""
from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
OUT = Path("reports/h2_probe")
OUT.mkdir(parents=True, exist_ok=True)


def main() -> None:
    captured = []

    def on_response(resp):
        try:
            u = resp.url
            if resp.status != 200:
                return
            ct = (resp.headers.get("content-type") or "").lower()
            if "json" not in ct and "pdf" not in ct and "octet" not in ct:
                if not any(x in u for x in ("skill", "standard", "page", "list", "query")):
                    return
            entry = {"url": u, "status": resp.status, "ct": ct[:100]}
            if "json" in ct:
                try:
                    data = resp.json()
                    entry["type"] = type(data).__name__
                    if isinstance(data, dict):
                        entry["keys"] = list(data.keys())[:30]
                        # shallow sample
                        for k in ("data", "list", "records", "rows", "result", "total"):
                            if k in data:
                                v = data[k]
                                if isinstance(v, list):
                                    entry[f"sample_{k}_len"] = len(v)
                                    if v:
                                        entry[f"sample_{k}_item"] = (
                                            v[0]
                                            if isinstance(v[0], (dict, str, int))
                                            else str(type(v[0]))
                                        )
                                else:
                                    entry[f"val_{k}"] = v if not isinstance(v, (dict, list)) else str(type(v))
                        entry["raw_head"] = json.dumps(data, ensure_ascii=False)[:500]
                    elif isinstance(data, list):
                        entry["list_len"] = len(data)
                except Exception as e:
                    entry["json_err"] = str(e)
            if "pdf" in ct or u.lower().endswith(".pdf"):
                entry["is_pdf"] = True
                entry["len"] = len(resp.body())
            captured.append(entry)
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=EDGE, headless=True)
        page = browser.new_page()
        page.on("response", on_response)
        page.goto("https://www.osta.org.cn/skillStandard", wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(2000)
        # click page 2
        for sel in (".arco-pagination-item >> text=2", "li >> text=2", "text=2"):
            try:
                page.locator(sel).first.click(timeout=2000)
                page.wait_for_timeout(2000)
                break
            except Exception:
                continue
        # click first 点击查看
        try:
            page.locator("a.arco-link", has_text="点击查看").first.click(timeout=3000)
            page.wait_for_timeout(3000)
            page.screenshot(path=str(OUT / "detail_modal.png"))
        except Exception as e:
            print("click detail err", e)
        # close modal
        for sel in (".arco-modal-close-btn", "button:has-text('关闭')", ".arco-icon-close"):
            try:
                page.locator(sel).first.click(timeout=1000)
                break
            except Exception:
                pass
        browser.close()

    (OUT / "network_capture.json").write_text(
        json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("captured", len(captured))
    for c in captured[:40]:
        print(json.dumps(c, ensure_ascii=False)[:300])


if __name__ == "__main__":
    main()
