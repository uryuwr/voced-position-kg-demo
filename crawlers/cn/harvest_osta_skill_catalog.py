"""
H2 人机协同 · osta 国家职业标准查询系统目录采集。

公开列表（无需登录）：https://www.osta.org.cn/skillStandard
  - 共约 702 条，分页翻取：标准名称 / 职业编号 / 颁布时间 / 发文号

可选：
  --detail-sample N  点开前 N 条「点击查看」探测详情是否登录/是否有 PDF
  --user-data-dir PATH  使用持久化浏览器配置（你登录后 cookie 复用）
  --headed             有界面（便于你手动登录）
  --wait-login         打开页面后等待你登录（检测「退出/个人中心」或回车文件）

产出：
  data/raw/CN/skill_standards/osta_catalog.json
  reports/h2_osta_catalog_harvest.json

Usage:
  python -m crawlers.cn.harvest_osta_skill_catalog
  python -m crawlers.cn.harvest_osta_skill_catalog --detail-sample 3 --headed --wait-login
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.paths import RAW, REPORTS, ensure_dirs

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
LIST_URL = "https://www.osta.org.cn/skillStandard"
DEST_DIR = RAW / "CN" / "skill_standards"
CATALOG_PATH = DEST_DIR / "osta_catalog.json"
WAIT_FLAG = REPORTS / "h2_login_ready.flag"
WAIT_MSG = REPORTS / "h2_WAIT_FOR_LOGIN.txt"


def parse_rows(page) -> list[dict]:
    rows = page.eval_on_selector_all(
        "table tbody tr, table tr",
        """els => els.map(tr => {
          const cells = Array.from(tr.querySelectorAll('td')).map(td => (td.innerText||'').trim());
          if (cells.length < 5) return null;
          // skip header-like
          if (cells[0] === '序号' || !/^\\d+$/.test(cells[0])) return null;
          return {
            seq: cells[0],
            name: cells[1],
            occupation_code: cells[2],
            published_at: cells[3],
            doc_no: cells[4],
            action: cells[5] || ''
          };
        }).filter(Boolean)""",
    )
    return rows or []


def go_next_page(page, current: int) -> bool:
    """点下一页；返回是否成功翻页。"""
    # 尝试页码按钮
    next_num = current + 1
    for sel in (
        f"text=^{next_num}$",
        f".el-pager li >> text={next_num}",
        f"li.number >> text={next_num}",
        f"a >> text={next_num}",
        "button:has-text('下一页')",
        "a:has-text('>')",
        "li.btn-next",
        ".btn-next",
        "text=>",
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            if not loc.is_visible(timeout=500):
                continue
            before = page.locator("table").inner_text(timeout=2000)[:200]
            loc.click(timeout=2000)
            page.wait_for_timeout(1200)
            after = page.locator("table").inner_text(timeout=2000)[:200]
            if after != before:
                return True
        except Exception:
            continue
    return False


def harvest_list(page, max_pages: int = 80) -> list[dict]:
    page.goto(LIST_URL, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(2000)
    all_rows: list[dict] = []
    seen = set()
    page_i = 1
    while page_i <= max_pages:
        rows = parse_rows(page)
        new = 0
        for r in rows:
            key = (r.get("name"), r.get("occupation_code"), r.get("doc_no"))
            if key in seen:
                continue
            seen.add(key)
            r["page"] = page_i
            all_rows.append(r)
            new += 1
        print(f"page {page_i}: +{new} total={len(all_rows)}", flush=True)
        if new == 0 and page_i > 1:
            break
        if not go_next_page(page, page_i):
            # 再试一次：直接点分页区最后一个数字后的 >
            break
        page_i += 1
        time.sleep(0.35)
    return all_rows


def probe_details(page, sample: int = 3) -> list[dict]:
    """点前 sample 条「点击查看」，记录是否跳登录、是否有 PDF。"""
    page.goto(LIST_URL, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(1500)
    results = []
    links = page.locator("table a, table td >> text=点击查看")
    n = min(sample, links.count())
    for i in range(n):
        item = {"index": i, "ok": False}
        try:
            with page.expect_popup(timeout=5000) as pop:
                page.locator("text=点击查看").nth(i).click(timeout=3000)
            detail = pop.value
            detail.wait_for_load_state("domcontentloaded", timeout=30000)
            detail.wait_for_timeout(1500)
            item["url"] = detail.url
            item["title"] = detail.title()
            body = detail.locator("body").inner_text(timeout=5000)[:1500]
            item["body_head"] = body
            item["needs_login"] = any(
                k in body for k in ("登录", "请先登录", "验证码", "统一身份认证")
            )
            item["pdf_links"] = detail.eval_on_selector_all(
                "a[href*='pdf'], a[href*='PDF'], a[href*='download']",
                "els => els.map(e => e.href).slice(0,10)",
            )
            # 有时 PDF 是 embed
            item["has_embed"] = detail.locator("embed, iframe, object").count() > 0
            item["ok"] = True
            detail.close()
        except Exception as e:
            # 可能同页打开
            try:
                page.locator("text=点击查看").nth(i).click(timeout=2000)
                page.wait_for_timeout(2000)
                item["url"] = page.url
                body = page.locator("body").inner_text(timeout=3000)[:1200]
                item["body_head"] = body
                item["needs_login"] = any(
                    k in body for k in ("登录", "请先登录", "验证码")
                )
                item["err"] = str(e)
                item["ok"] = True
                page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(1000)
            except Exception as e2:
                item["error"] = f"{e} | {e2}"
        results.append(item)
        print("detail", i, item.get("url"), "login=", item.get("needs_login"), flush=True)
    return results


def write_wait_login_instructions(user_data_dir: Path) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    msg = f"""# H2 需要你登录（人机协同）

当前自动化已打开浏览器，**请在弹出的 Edge 窗口中完成登录**（若页面要求）。

完成后任选一种方式通知我继续：

1. 在仓库里创建空文件（推荐）:
   {WAIT_FLAG}

   PowerShell:
   New-Item -ItemType File -Force -Path "{WAIT_FLAG}"

2. 或在聊天里回复：「已登录」

浏览器用户数据目录（登录态会保留）:
  {user_data_dir}

列表页（一般无需登录）:
  {LIST_URL}
"""
    WAIT_MSG.write_text(msg, encoding="utf-8")
    print("=" * 60, flush=True)
    print(msg, flush=True)
    print("=" * 60, flush=True)


def wait_for_user_login(timeout_sec: int = 600) -> bool:
    if WAIT_FLAG.exists():
        try:
            WAIT_FLAG.unlink()
        except Exception:
            pass
    write_wait_login_instructions(Path("data/raw/CN/skill_standards/browser_profile"))
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        if WAIT_FLAG.exists():
            print("detected login flag, continue", flush=True)
            try:
                WAIT_FLAG.unlink()
            except Exception:
                pass
            return True
        time.sleep(2)
    print("wait login timeout", flush=True)
    return False


def main() -> None:
    from playwright.sync_api import sync_playwright

    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--wait-login", action="store_true")
    parser.add_argument("--detail-sample", type=int, default=0)
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument(
        "--user-data-dir",
        default=str(DEST_DIR / "browser_profile"),
    )
    args = parser.parse_args()
    ensure_dirs()
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)

    report: dict = {
        "list_url": LIST_URL,
        "catalog_count": 0,
        "detail_probe": [],
        "login_required_for_list": False,
        "login_required_for_detail": None,
    }

    with sync_playwright() as p:
        profile = Path(args.user_data_dir)
        profile.mkdir(parents=True, exist_ok=True)
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            executable_path=EDGE,
            headless=not args.headed and not args.wait_login,
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
        )
        page = context.pages[0] if context.pages else context.new_page()

        if args.wait_login:
            page.goto(LIST_URL, wait_until="domcontentloaded", timeout=90000)
            wait_for_user_login(600)

        print("harvest list...", flush=True)
        catalog = harvest_list(page, max_pages=args.max_pages)
        report["catalog_count"] = len(catalog)
        report["catalog_sample"] = catalog[:15]

        if args.detail_sample > 0:
            print("probe details...", flush=True)
            details = probe_details(page, args.detail_sample)
            report["detail_probe"] = details
            if any(d.get("needs_login") for d in details):
                report["login_required_for_detail"] = True
            elif details:
                report["login_required_for_detail"] = False

        context.close()

    CATALOG_PATH.write_text(
        json.dumps(
            {
                "source": LIST_URL,
                "fetched_note": "osta 国家职业标准查询系统公开列表",
                "count": len(catalog),
                "items": catalog,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    report["catalog_path"] = str(CATALOG_PATH)
    (REPORTS / "h2_osta_catalog_harvest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: report[k] for k in report if k != "detail_probe"}, ensure_ascii=False, indent=2))
    if report.get("detail_probe"):
        print("detail_probe:", json.dumps(report["detail_probe"], ensure_ascii=False, indent=2)[:2000])


if __name__ == "__main__":
    main()
