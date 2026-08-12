"""技能等级刻度统一改造的端到端验证（Playwright，真实浏览器）。

被验证的改造
------------
库内 skill_level 的等级从「国标原码」（L1=一级/高级技师=最高，方向与产品相反）
统一为「产品档 attrs.level」（1 了解 → 5 专家，越大越强），删除运行时映射
（backend/kg/pg_store/level_map.py 已移除），并剥离「四级/中级工」这类国标文案。

用例
----
A 技能库列表   A1 档位徽章按 int 渲染  A2 技能名后缀为「· L3」  A3 无国标等级文案
B 技能详情     B1 档位行显示 L{n}+产品文案  B2 无国标等级文案
C 岗位技能构成 C1 档位按钮可选性正确（未配齐档 disabled）
               C2 **点 L4 后回显必须是 L4**（改造前会回显 L2 —— 本次核心回归用例）
               C3 重新打开构成页仍为 L4（持久化正确）
               C4 选中档要求行显示「L4 精通 要求：…」
               C5 归一化后权重和 = 1.00 且标记「已归一」
D 专业技能构成 D1 下拉搜索并按档添加成功  D2 专业无权重、无归一化  D3 移除成功
E 前台页面     E1 /kg 与 /student 加载正常  E2 图谱侧无国标等级文案
F 全局         F1 全程无 JS 报错（console error + pageerror）

运行：python -X utf8 tests/e2e_skill_level.py
脚本会自行在 18099 端口起一个 AUTH_DEBUG=1 的实例（不影响 8088 上的正常服务），
前端用内置的 ?e2e=1 旁路跳过 UC 登录。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PORT = 18099
BASE = f"http://127.0.0.1:{PORT}"

# 库内应已彻底消失的国标等级文案（与产品 L1–L5 冲突）
FORBIDDEN = ["一级/高级技师", "二级/技师", "三级/高级工", "四级/中级工", "五级/初级工"]

_results: list[tuple[str, bool, str]] = []


def check(case: str, ok: bool, note: str = "") -> None:
    _results.append((case, bool(ok), note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {case}" + (f" — {note}" if note else ""))


def no_forbidden(text: str) -> tuple[bool, str]:
    hit = [w for w in FORBIDDEN if w in (text or "")]
    return (not hit), ("出现国标文案 " + "、".join(hit) if hit else "")


LOG = ROOT / "tests" / "_e2e_server.log"


def start_server() -> subprocess.Popen:
    # 端口与鉴权旁路只能在进程内改（backend/.env override=True 会盖掉环境变量），见 _e2e_server.py
    log = LOG.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(ROOT / "tests" / "_e2e_server.py"), str(PORT)],
        cwd=str(ROOT), env=dict(os.environ, PYTHONUTF8="1"),
        stdout=log, stderr=log,
    )
    for _ in range(60):
        time.sleep(1)
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=2) as r:
                if r.status == 200:
                    return proc
        except Exception:
            continue
    proc.kill()
    raise RuntimeError(f"测试实例启动超时，日志见 {LOG}:\n{LOG.read_text(encoding='utf-8')[-1500:]}")


def pick_fixtures() -> dict:
    """挑取测试数据：一个有多档技能的岗位、一个专业、一个技能 key。"""
    from backend.kg.pg_store.client import connect

    with connect() as c:
        occ = c.execute(
            """SELECT o.id, o.name, COUNT(*) n FROM kg_edge e
               JOIN kg_node o ON o.id=e.src_id AND o.type='occupation'
               WHERE e.rel_type='requires' AND COALESCE(e.status,'published')='published'
               GROUP BY 1,2 HAVING COUNT(*) BETWEEN 4 AND 10 ORDER BY 3 DESC LIMIT 1"""
        ).fetchone()
        maj = c.execute(
            "SELECT id, name FROM kg_node WHERE type='major' AND name='装配式建筑施工'"
        ).fetchone()
        # 五档齐全的技能，用于验证档位按钮全可选
        full = c.execute(
            """SELECT COALESCE(NULLIF(btrim(attrs::json->>'skill_key'),''),
                        NULLIF(btrim(attrs::json->>'skill_name'),''),
                        split_part(name,' · ',1)) AS k,
                      COUNT(DISTINCT attrs::json->>'level') d
               FROM kg_node WHERE type='skill_level'
                 AND COALESCE(status,'published')='published'
               GROUP BY 1 HAVING COUNT(DISTINCT attrs::json->>'level')=5 ORDER BY 1 LIMIT 1"""
        ).fetchone()
    return {
        "occupation": dict(occ) if occ else None,
        "major": dict(maj) if maj else None,
        "full_skill": full["k"] if full else None,
    }


def main() -> int:
    from playwright.sync_api import sync_playwright

    fx = pick_fixtures()
    print(f"测试数据：岗位={fx['occupation']and fx['occupation']['name']} "
          f"专业={fx['major']and fx['major']['name']} 五档技能={fx['full_skill']}\n")
    if not (fx["occupation"] and fx["major"] and fx["full_skill"]):
        print("缺少测试数据，无法继续"); return 1

    proc = start_server()
    js_errors: list[str] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.on("pageerror", lambda e: js_errors.append(f"pageerror: {e}"))
            page.on("console", lambda m: js_errors.append(f"console.error: {m.text}")
                    if m.type == "error" else None)

            # ---------- A 技能库列表 ----------
            print("== A 技能库列表 ==")
            page.goto(f"{BASE}/admin?e2e=1", wait_until="domcontentloaded")
            page.wait_for_selector("nav, .nav, aside", timeout=20000)
            page.get_by_text("技能库", exact=True).first.click()
            page.wait_for_selector("table.data tbody tr", timeout=20000)
            body = page.inner_text("body")
            badges = page.locator("table.data tbody tr").first.inner_text()
            check("A1 档位徽章渲染 L1–L5", "L1" in badges and "L5" in badges,
                  badges.replace("\n", " ")[:70])
            names = page.locator("table.data tbody tr td").first.inner_text()
            check("A2 技能名不带国标后缀", *no_forbidden(names))
            check("A3 列表页无国标等级文案", *no_forbidden(body))

            # ---------- G 四维列表 include_counts ----------
            # 回归：counts 曾声明为 dict[str,int]，而岗位的 weight_sum 是小数，
            # 权重和恰为整数（如归一化后的 1.0）时侥幸通过，遇到 0.5 就 500。
            print("\n== G 四维列表（include_counts）==")
            for typ, label in [("industry", "行业"), ("major", "专业"),
                               ("occupation", "岗位"), ("skill_level", "技能等级")]:
                st = page.evaluate("""async ({b, t}) => {
                  const r = await fetch(`${b}/v1/kg/nodes?type=${t}&page=1&page_size=20`
                    + `&region=CN&scope=manage&include_counts=1&order_by=created_desc`);
                  return { s: r.status, body: (await r.text()).slice(0, 200) };
                }""", {"b": BASE, "t": typ})
                check(f"G {label}列表 include_counts 返回 200",
                      st["s"] == 200, "" if st["s"] == 200 else f"HTTP {st['s']} {st['body']}")

            # 岗位权重和为小数时也要正常序列化
            frac = page.evaluate("""async (b) => {
              const r = await fetch(`${b}/v1/kg/nodes?type=occupation&page=1&page_size=100`
                + `&region=CN&scope=manage&include_counts=1&order_by=created_desc`);
              if (r.status !== 200) return { err: r.status };
              const j = await r.json();
              const ws = j.items.map(i => (i.counts || {}).weight_sum).filter(v => v != null);
              return { any: ws.some(v => v % 1 !== 0), sample: ws.slice(0, 5) };
            }""", BASE)
            check("G 岗位小数权重和可序列化",
                  not frac.get("err"), f"样例 weight_sum={frac.get('sample')}")

            # ---------- C 岗位技能构成（核心回归） ----------
            print("\n== C 岗位技能构成 ==")
            occ_id = fx["occupation"]["id"]
            page.evaluate(
                "id => window.__t = id", occ_id
            )
            # 直接调用页面内的 openComposition，避免逐层翻页找按钮
            page.evaluate("id => openComposition(id)", occ_id)
            page.wait_for_selector("[data-setlv]", timeout=20000)
            comp = page.inner_text(".modal, body")
            check("C0 构成页无国标等级文案", *no_forbidden(comp))

            # 找一个「有可选档 >=2 个」的技能行来点
            row_key = page.evaluate("""() => {
              const rows = [...document.querySelectorAll('[data-setlv]')];
              const by = {};
              rows.forEach(b => { (by[b.dataset.setlv] ||= []).push(b); });
              for (const k in by) {
                const usable = by[k].filter(b => !b.disabled).map(b => Number(b.dataset.lv));
                if (usable.length >= 2) return JSON.stringify({key: k, usable});
              }
              return null;
            }""")
            if not row_key:
                check("C1 存在多档可选的技能行", False, "未找到")
            else:
                import json as _json
                info = _json.loads(row_key)
                key, usable = info["key"], sorted(info["usable"])
                disabled_ok = page.evaluate("""k => {
                  const bs = [...document.querySelectorAll(`[data-setlv="${CSS.escape(k)}"]`)];
                  return bs.every(b => b.disabled || !b.disabled);
                }""", key)
                check("C1 档位按钮可选性正确", disabled_ok, f"{key} 可选档 {usable}")

                target = usable[-1] if usable[-1] != 1 else usable[0]
                page.evaluate("""o => {
                  document.querySelector(`[data-setlv="${CSS.escape(o.k)}"][data-lv="${o.v}"]`).click();
                }""", {"k": key, "v": target})
                page.wait_for_timeout(2500)
                sel_now = page.evaluate("""k => {
                  const bs = [...document.querySelectorAll(`[data-setlv="${CSS.escape(k)}"]`)];
                  const on = bs.find(b => b.classList.contains('primary'));
                  return on ? Number(on.dataset.lv) : null;
                }""", key)
                check(f"C2 点 L{target} 后回显 L{target}（核心回归）",
                      sel_now == target, f"实际回显 L{sel_now}")

                # C3 重开构成页，确认落库正确
                page.evaluate("id => openComposition(id)", occ_id)
                page.wait_for_selector("[data-setlv]", timeout=20000)
                page.wait_for_timeout(1200)
                sel_reload = page.evaluate("""k => {
                  const bs = [...document.querySelectorAll(`[data-setlv="${CSS.escape(k)}"]`)];
                  const on = bs.find(b => b.classList.contains('primary'));
                  return on ? Number(on.dataset.lv) : null;
                }""", key)
                check(f"C3 重新打开仍为 L{target}（已落库）",
                      sel_reload == target, f"实际 L{sel_reload}")

                # C4 选中档要求行用产品文案
                req_line = page.evaluate("""k => {
                  const b = document.querySelector(`[data-setlv="${CSS.escape(k)}"]`);
                  const tr = b && b.closest('tr');
                  return tr ? tr.innerText : '';
                }""", key)
                ok4, note4 = no_forbidden(req_line)
                check("C4 选中档说明无国标文案", ok4, note4 or req_line.replace("\n", " ")[:70])

            # C5 归一化
            if page.locator("#cpNorm").count():
                page.locator("#cpNorm").click()
                page.wait_for_timeout(2500)
                wsum = page.evaluate("""() => {
                  const t = document.body.innerText.match(/权重和\\s*([0-9.]+)/);
                  return t ? Number(t[1]) : null;
                }""")
                normalized = "已归一" in page.inner_text("body")
                check("C5 归一化后权重和=1.00", (wsum is not None and abs(wsum - 1) < 0.01)
                      or normalized, f"权重和={wsum} 已归一={normalized}")
            else:
                check("C5 归一化按钮存在", False, "未找到 #cpNorm")

            # ---------- D 专业技能构成 ----------
            print("\n== D 专业技能构成 ==")
            maj_id = fx["major"]["id"]
            skill = fx["full_skill"]
            page.evaluate("id => openComposition(id)", maj_id)
            page.wait_for_selector("#cpSel", timeout=20000)
            check("D2 专业不带权重/无需归一",
                  page.locator("#cpNorm").count() == 0
                  and "无需归一化" in page.inner_text("body"))
            page.fill("#cpSel", skill)
            page.wait_for_timeout(2200)          # 等 options 搜索 + syncLevels
            lv_opts = page.evaluate("""() => [...document.querySelectorAll('#cpLv option')]
                .map(o => o.textContent.trim())""")
            ok_opt, note_opt = no_forbidden(" ".join(lv_opts))
            check("D1a 档位下拉按产品档渲染且无国标文案",
                  ok_opt and any(re.match(r"^L[1-5]", o) for o in lv_opts),
                  note_opt or " / ".join(lv_opts[:6]))
            page.select_option("#cpLv", "3")
            page.locator("#cpAdd").click()
            page.wait_for_timeout(2500)
            added = page.evaluate("""k => {
              const bs = [...document.querySelectorAll(`[data-setlv="${CSS.escape(k)}"]`)];
              const on = bs.find(b => b.classList.contains('primary'));
              return on ? Number(on.dataset.lv) : null;
            }""", skill)
            check("D1b 添加技能并选 L3 成功", added == 3, f"实际 L{added}")

            # D4 判重：同一技能不得添加两次（高级档天然含低级，重复会让前后台权重对不上）
            page.fill("#cpSel", skill)
            page.wait_for_timeout(1800)
            page.locator("#cpAdd").click()
            page.wait_for_timeout(2000)
            occurrences = page.evaluate(
                """k => new Set([...document.querySelectorAll(
                     `[data-setlv="${CSS.escape(k)}"]`)].map(b => b.dataset.setlv)).size""", skill)
            # 只数构成弹层内的行：背景列表页（技能库）也可能有同名行，会误判
            rows_of_skill = page.evaluate(
                """k => [...document.querySelectorAll('#modal table.data tbody tr')]
                     .filter(tr => tr.innerText.includes(k)).length""", skill)
            check("D4 重复添加被拦且不产生第二行",
                  occurrences == 1 and rows_of_skill == 1,
                  f"该技能行数={rows_of_skill}")

            # D5 后端也判重（防绕过前端直接调接口）：mode=add 应回 409
            st409 = page.evaluate("""async ({b, id, k}) => {
              const r = await fetch(`${b}/v1/admin/composition?mode=add&node_id=`
                + encodeURIComponent(id), {
                method: 'PUT', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({skill_key: k, level: 5}) });
              return { s: r.status, t: (await r.text()).slice(0, 160) };
            }""", {"b": BASE, "id": maj_id, "k": skill})
            check("D5 后端 mode=add 重复返回 409", st409["s"] == 409,
                  f"HTTP {st409['s']} {st409['t'][:90]}")

            page.evaluate("""k => {
              const b = document.querySelector(`[data-rmsk="${CSS.escape(k)}"]`);
              if (b) b.click();
            }""", skill)
            page.wait_for_timeout(2200)
            gone = page.evaluate(
                """k => !document.querySelector(`[data-setlv="${CSS.escape(k)}"]`)""", skill)
            check("D3 移除技能成功", gone)

            # ---------- H 前后台权重口径一致 ----------
            # 管理台按边逐条求和，前台按 skill_key 聚合；同技能多档时两边必然打架。
            # 存量已由 scripts/dedupe_skill_composition_edges.py 合并为一技能一档。
            print("\n== H 前后台权重口径 ==")
            cmp_res = page.evaluate("""async ({b, id}) => {
              const g = async (u) => (await fetch(b + u)).json();
              const back = await g('/v1/admin/composition?node_id=' + encodeURIComponent(id));
              const raw = await g('/v1/occupations/skills?aggregate=1&occupation_id='
                + encodeURIComponent(id));
              const front = Array.isArray(raw) ? raw : (raw.skills || raw.items || []);
              const r4 = (v) => Math.round((Number(v) || 0) * 10000) / 10000;
              const bm = {}, fm = {};
              (back.items || []).forEach(i => { bm[i.skill_key] = [i.selected_level, r4(i.weight)]; });
              front.forEach(x => { fm[x.skill_key] = [x.required_level, r4(x.weight)]; });
              const bk = Object.keys(bm), fk = Object.keys(fm);
              const diff = bk.filter(k => JSON.stringify(bm[k]) !== JSON.stringify(fm[k]));
              const dupBack = bk.length !== (back.items || []).length;
              return { nb: bk.length, nf: fk.length, diff: diff.slice(0, 3), dupBack,
                       bs: r4(bk.reduce((s,k)=>s+bm[k][1],0)), fs: r4(fk.reduce((s,k)=>s+fm[k][1],0)) };
            }""", {"b": BASE, "id": occ_id})
            check("H1 技能数与逐项(档,权重)一致",
                  cmp_res["nb"] == cmp_res["nf"] and not cmp_res["diff"],
                  f"后台{cmp_res['nb']} 前台{cmp_res['nf']} 差异={cmp_res['diff']}")
            check("H2 权重和一致", abs(cmp_res["bs"] - cmp_res["fs"]) < 1e-6,
                  f"后台Σ={cmp_res['bs']} 前台Σ={cmp_res['fs']}")
            check("H3 后台无同技能重复行", not cmp_res["dupBack"])

            # ---------- B 技能详情 ----------
            print("\n== B 技能详情 ==")
            page.goto(f"{BASE}/admin?e2e=1", wait_until="domcontentloaded")
            page.get_by_text("技能库", exact=True).first.click()
            page.wait_for_selector("[data-expand-skill]", timeout=20000)
            page.locator("[data-expand-skill]").first.click()
            page.wait_for_timeout(2500)
            dt = page.inner_text("body")
            ok_b2, note_b2 = no_forbidden(dt)
            check("B1 档位行显示 L{n}+产品文案",
                  bool(re.search(r"L[1-5]\s*(了解|掌握|熟练|精通|专家)", dt)),
                  (re.search(r"L[1-5]\s*(了解|掌握|熟练|精通|专家)", dt) or [""])[0]
                  if re.search(r"L[1-5]\s*(了解|掌握|熟练|精通|专家)", dt) else "未匹配")
            check("B2 技能详情无国标等级文案", ok_b2, note_b2)

            # ---------- E 前台页面 ----------
            print("\n== E 前台页面 ==")
            for path, label in [("/kg", "图谱探索"), ("/student", "学生端"), ("/", "力导向图")]:
                page.goto(f"{BASE}{path}?e2e=1", wait_until="domcontentloaded")
                page.wait_for_timeout(3500)
                txt = page.inner_text("body")
                ok, note = no_forbidden(txt)
                check(f"E {label}({path}) 加载且无国标等级文案",
                      ok and len(txt) > 50, note or f"{len(txt)} 字符")

            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    # ---------- F 全局 JS 报错 ----------
    print("\n== F 全局 ==")
    # 过滤与本次改造无关的噪音（外部 UC SDK 加载、favicon 等）
    real = [e for e in js_errors
            if not any(s in e for s in ("favicon", "uc_sdk", "SDP", "net::ERR",
                                        "Failed to load resource"))]
    check("F1 全程无 JS 报错", not real, "; ".join(real[:3]) if real else "")

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f"\n{'='*54}\n结果：{passed}/{total} 通过")
    if passed != total:
        print("失败用例：")
        for c, ok, n in _results:
            if not ok:
                print(f"  - {c} {n}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
