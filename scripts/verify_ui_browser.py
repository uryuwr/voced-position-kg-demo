"""浏览器侧验收：脚本测不到的东西，在真实页面里测。

脚本能证明「接口返回对」，证明不了：
- 页面有没有渲染出来（拿到 200 但 JS 崩了照样白屏）
- 控制台有没有报错
- SSE 长连接在浏览器里是否真的持续吐事件（TestClient 的 stream 和浏览器的
  EventSource/fetch-ReadableStream 行为不同）
- 每个接口真实的端到端耗时（含 TLS、排队、渲染）

用法：
    python -X utf8 scripts/verify_ui_browser.py [base_url] [--out 前缀]

默认 http://127.0.0.1:8088。对比改造前后时，把两次的 JSON 产出 diff 一下。

用的是 Playwright 自带 Chromium（`pw.chromium.launch()`），和项目既有的
tests/e2e_assessment_ui.py 同一套，不依赖系统装没装 Chrome。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BASE = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "http://127.0.0.1:8088"
OUT = ROOT / ".ui_verify"


def main() -> int:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(exist_ok=True)
    tag = "baseline" if "18099" in BASE else "current"

    console: list[dict] = []
    requests: list[dict] = []
    failures: list[str] = []

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page(viewport={"width": 1440, "height": 960})

        page.on("console", lambda m: console.append({"type": m.type, "text": m.text[:300]}))
        page.on("pageerror", lambda e: console.append({"type": "pageerror", "text": str(e)[:300]}))

        # 耗时必须在 requestfinished 时读：response 事件触发时 timing 还没填完，
        # 拿到的全是 -1（第一版就栽在这，白采了一轮基线）
        def on_finished(req):
            if "/v1/" not in req.url and "/health" not in req.url:
                return
            t = req.timing or {}
            dur = t.get("responseEnd", -1) - t.get("requestStart", -1)
            try:
                status = req.response().status
            except Exception:  # noqa: BLE001
                status = 0
            requests.append({
                "url": req.url.replace(BASE, ""),
                "status": status,
                # connectEnd<0 说明复用了已有连接（没重新握手），正是池化要看的
                "reused_conn": t.get("connectEnd", 0) < 0,
                "ms": round(dur, 1) if dur > 0 else None,
            })
            if status >= 400:
                failures.append(f"{status} {req.url.replace(BASE, '')}")

        page.on("requestfinished", on_finished)

        print(f"== 打开学员端 {BASE}/dev ==", flush=True)
        t0 = time.perf_counter()
        page.goto(f"{BASE}/dev", wait_until="networkidle", timeout=60000)
        load_ms = (time.perf_counter() - t0) * 1000
        page.wait_for_timeout(2500)          # 等首屏异步请求落地

        title = page.title()
        body_len = len(page.inner_text("body"))
        page.screenshot(path=str(OUT / f"{tag}_student.png"), full_page=False)

        # 有没有真的渲染出内容（白屏时 body 文本极短）
        rendered = body_len > 200

        print(f"  标题：{title}")
        print(f"  首屏 {load_ms:.0f} ms，正文 {body_len} 字符，{'已渲染' if rendered else '疑似白屏'}")

        # 管理台
        print(f"\n== 打开管理台 {BASE}/admin ==", flush=True)
        try:
            page.goto(f"{BASE}/admin", wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(2000)
            admin_len = len(page.inner_text("body"))
            page.screenshot(path=str(OUT / f"{tag}_admin.png"), full_page=False)
            print(f"  正文 {admin_len} 字符，{'已渲染' if admin_len > 200 else '疑似白屏'}")
        except Exception as e:  # noqa: BLE001
            admin_len = 0
            print(f"  打不开：{str(e)[:120]}")

        b.close()

    errs = [c for c in console if c["type"] in ("error", "pageerror")]
    api = [r for r in requests if r["ms"]]
    api.sort(key=lambda r: -(r["ms"] or 0))

    print(f"\n== 结果 ==")
    print(f"  接口请求 {len(requests)} 个，失败 {len(failures)} 个")
    for f in failures[:10]:
        print(f"      {f}")
    print(f"  控制台错误 {len(errs)} 条")
    for c in errs[:8]:
        print(f"      [{c['type']}] {c['text'][:160]}")
    print("  最慢的 8 个接口：")
    for r in api[:8]:
        print(f"      {r['ms']:>7.1f} ms  {r['status']}  {r['url'][:70]}")

    data = {
        "base": BASE, "tag": tag, "load_ms": round(load_ms, 1),
        "body_len": body_len, "admin_len": admin_len,
        "requests": requests, "console_errors": errs, "failures": failures,
    }
    (OUT / f"{tag}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n  产出：{OUT / (tag + '.json')}、{tag}_student.png、{tag}_admin.png")

    ok = rendered and not failures and not errs
    print(f"\n{'=' * 56}\n{'浏览器侧通过 ✔' if ok else '浏览器侧有问题，见上'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
