"""
E2E: admin-cytoscape Graph 交互（Edge）。
验证：
1) 进入 Graph 不是「几百专业圆圈」而是有边的关系样例
2) 搜索软件技术有节点+边
3) 目录 Table 可加载专业
4) 状态栏文案合理

Usage:
  pip install playwright
  playwright install msedge   # or use channel
  python scripts/e2e_admin_graph.py
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
BASE = "http://127.0.0.1:8088"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


def main() -> int:
    from playwright.sync_api import sync_playwright

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    shot_dir = REPORTS / "e2e_shots"
    shot_dir.mkdir(parents=True, exist_ok=True)
    results: dict = {"ok": True, "checks": [], "base": BASE}

    def check(name: str, cond: bool, detail: str = "") -> None:
        results["checks"].append({"name": name, "pass": bool(cond), "detail": detail})
        if not cond:
            results["ok"] = False
        print(("PASS" if cond else "FAIL"), name, detail)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=EDGE,
            headless=True,
        )
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(f"{BASE}/admin-cytoscape", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(800)

        # 默认 Table
        check("default_table_tab", page.locator(".tab.on[data-v=table]").count() > 0)

        # 地区 CN
        page.select_option("#region", "CN")
        page.wait_for_timeout(500)

        # 进 Graph → 应自动关系样例
        page.locator('.tab[data-v=graph]').click()
        page.wait_for_timeout(2500)
        # 等待查询结束
        for _ in range(20):
            info = page.locator("#info").inner_text()
            if "查询中" not in info and ("节点" in info or "边" in info or "样例" in info or "软件" in info):
                break
            page.wait_for_timeout(400)

        info1 = page.locator("#info").inner_text()
        q_val = page.locator("#q").input_value()
        check("graph_search_filled", bool(q_val), f"q={q_val!r}")
        check("graph_info_has_counts", "节点" in info1 and "边" in info1, info1[:200])

        stats2 = page.evaluate(
            """() => {
              try {
                const c = window.__kgCy;
                const t = document.getElementById('info')?.textContent || '';
                const m = t.match(/节点\\s*(\\d+)\\s*·\\s*边\\s*(\\d+)/);
                const fromInfo = m ? {nodes: +m[1], edges: +m[2]} : {};
                if (c) {
                  return {
                    nodes: c.nodes().length,
                    edges: c.edges().length,
                    labeled: c.nodes().filter(n => (n.data('label')||'').length > 0).length,
                    info: t,
                    q: document.getElementById('q')?.value || '',
                  };
                }
                return {...fromInfo, info: t, err: 'no __kgCy'};
              } catch(e) { return {err: String(e)}; }
            }"""
        )
        results["graph_stats"] = stats2
        n, e = stats2.get("nodes"), stats2.get("edges")
        check("graph_has_nodes", isinstance(n, int) and n > 0 and n < 200, f"nodes={n}")
        check("graph_has_edges", isinstance(e, int) and e > 0, f"edges={e}")
        check("graph_not_huge_catalog", isinstance(n, int) and n <= 120, f"nodes={n} (circle dump would be 300-500)")

        page.screenshot(path=str(shot_dir / "01_graph_showcase.png"), full_page=False)

        # 再查护理
        page.fill("#q", "护理")
        page.locator("#btn").click()
        page.wait_for_timeout(2500)
        for _ in range(15):
            info = page.locator("#info").inner_text()
            if "查询中" not in info:
                break
            page.wait_for_timeout(300)
        info2 = page.locator("#info").inner_text()
        m2 = __import__("re").search(r"节点\s*(\d+)\s*·\s*边\s*(\d+)", info2)
        n2 = int(m2.group(1)) if m2 else 0
        e2 = int(m2.group(2)) if m2 else 0
        check("search_nursing_nodes", n2 > 0, info2[:180])
        page.screenshot(path=str(shot_dir / "02_graph_nursing.png"), full_page=False)

        # 目录 Table
        page.locator("#btnAllMajors").click()
        page.wait_for_timeout(2000)
        check("catalog_switches_table", page.locator(".tab.on[data-v=table]").count() > 0)
        table_html = page.locator("#tableBox").inner_text()
        check("table_has_majors", ("专业" in table_html or "项" in page.locator("#info").inner_text() or len(table_html) > 40), table_html[:120])
        page.screenshot(path=str(shot_dir / "03_table_catalog.png"), full_page=False)

        # 点名称进 Graph
        name_el = page.locator("#tableBox .tname").first
        if name_el.count() > 0:
            title = name_el.inner_text()
            name_el.click()
            page.wait_for_timeout(2800)
            info3 = page.locator("#info").inner_text()
            on_graph = page.locator(".tab.on[data-v=graph]").count() > 0
            check("table_jump_graph", on_graph, f"title={title!r} info={info3[:140]}")
            page.screenshot(path=str(shot_dir / "04_table_to_graph.png"), full_page=False)
        else:
            check("table_has_tname_rows", False, page.locator("#tableBox").inner_text()[:200])

        browser.close()

    out = REPORTS / f"{day}-admin-graph-e2e.md"
    lines = [
        f"# admin-cytoscape E2E · {day}",
        "",
        f"**结果：{'通过' if results['ok'] else '失败'}**",
        "",
        "## 检查项",
    ]
    for c in results["checks"]:
        lines.append(f"- [{'x' if c['pass'] else ' '}] {c['name']}: {c.get('detail','')}")
    lines += ["", "## 截图", f"- `reports/e2e_shots/`", "", "```json", json.dumps(results, ensure_ascii=False, indent=2), "```"]
    out.write_text("\n".join(lines), encoding="utf-8")
    (REPORTS / f"{day}-admin-graph-e2e.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("report", out)
    return 0 if results["ok"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("E2E error", e)
        sys.exit(2)
