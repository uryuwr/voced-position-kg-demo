"""
人机协同备用：当 industry.json 直拉失败时，用 Playwright 打开 Boss 捕获行业接口。

  python -m crawlers.cn.harvest_boss_industry_playwright
  python -m crawlers.cn.harvest_boss_industry_playwright --headed --wait-login
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.paths import RAW, REPORTS, ensure_dirs

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
OUT = RAW / "CN" / "industry" / "boss_industry.json"
WAIT_FLAG = REPORTS / "h2_login_ready.flag"  # 复用通用登录标记
WAIT_MSG = REPORTS / "industry_WAIT_FOR_LOGIN.txt"


def main() -> None:
    from playwright.sync_api import sync_playwright

    p = argparse.ArgumentParser()
    p.add_argument("--headed", action="store_true")
    p.add_argument("--wait-login", action="store_true")
    args = p.parse_args()
    ensure_dirs()
    (RAW / "CN" / "industry").mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)

    captured = []

    def on_response(resp):
        try:
            if "industry.json" in resp.url and resp.status == 200:
                captured.append(resp.json())
        except Exception:
            pass

    with sync_playwright() as pw:
        profile = RAW / "CN" / "industry" / "browser_profile"
        profile.mkdir(parents=True, exist_ok=True)
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            executable_path=EDGE,
            headless=not args.headed and not args.wait_login,
            viewport={"width": 1400, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", on_response)
        page.goto("https://www.zhipin.com/web/geek/jobs", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(3000)
        if args.wait_login or "安全" in (page.title() or "") or "验证" in (page.title() or ""):
            WAIT_MSG.write_text(
                f"""# 行业采集需要你过验证/登录

1. 在弹出的 Edge 中完成 Boss 安全验证或登录
2. 完成后创建标记:
   New-Item -ItemType File -Force -Path "{WAIT_FLAG}"
3. 或聊天回复「已登录」

页面: https://www.zhipin.com/
""",
                encoding="utf-8",
            )
            print(WAIT_MSG.read_text(encoding="utf-8"), flush=True)
            if WAIT_FLAG.exists():
                try:
                    WAIT_FLAG.unlink()
                except Exception:
                    pass
            t0 = time.time()
            while time.time() - t0 < 600:
                if WAIT_FLAG.exists() or captured:
                    break
                time.sleep(2)
            try:
                WAIT_FLAG.unlink()
            except Exception:
                pass
            page.goto("https://www.zhipin.com/web/geek/jobs", wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(4000)

        # 主动请求一次
        try:
            data = page.evaluate(
                """async () => {
                  const r = await fetch('/wapi/zpCommon/data/industry.json', {credentials:'include'});
                  return await r.json();
                }"""
            )
            if data:
                captured.append(data)
        except Exception as e:
            print("page fetch err", e, flush=True)

        ctx.close()

    if not captured:
        print(json.dumps({"ok": False, "error": "no industry.json captured"}, ensure_ascii=False))
        sys.exit(1)
    best = captured[-1]
    OUT.write_text(json.dumps(best, ensure_ascii=False, indent=2), encoding="utf-8")
    n = len(best.get("zpData") or [])
    print(json.dumps({"ok": True, "path": str(OUT), "top_level": n, "code": best.get("code")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
