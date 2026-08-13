"""测评界面的浏览器端验证（Playwright）。

之前只验了 HTTP 契约，没验过页面真能跑——这里补上：
步骤条是否按服务端 stages 变色、题目是否逐道渲染、报告与雷达图是否画出来、
学员端「③ AI 诊断」tab 与独立页 /assessment 是否共用同一套行为。

协议已改为三段式（出题长连接 / 答题即答即走 / 结算），用例随之调整：

A  /assessment 独立页：三步骤条初始态、阶段1 表单、范例简历
B  开始测评 → 阶段1 done、阶段2 active、第一题出现（出题走一次长连接）
C  逐题作答：同屏只一道；**提交后下一题应立刻出现**（新架构的关键收益：
   题目已在本地队列里，不必等服务端现出）——断言单次翻页 < 3s
D  进度显示「第 N / 共 M 题」（题数出题前已确定）
E  报告：三节点 done、匹配度、优势/短板、雷达 canvas 非空白、双系列图例
F  /student「③ AI 诊断」tab 内挂载同一组件
G  全程无 JS 报错

出题阶段要调模型（约 20–40s），之后答题很快。
用法：python -X utf8 tests/e2e_assessment_ui.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PORT = 18096
BASE = f"http://127.0.0.1:{PORT}"
MAX_ANSWERS = 12
_results: list[tuple[str, bool, str]] = []


def check(case: str, ok: bool, note: str = "") -> None:
    _results.append((case, bool(ok), note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {case}" + (f" — {note}" if note else ""), flush=True)


def start_server() -> subprocess.Popen:
    log = (ROOT / "tests" / "_e2e_server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(ROOT / "tests" / "_e2e_server.py"), str(PORT)],
        cwd=str(ROOT), env=dict(os.environ, PYTHONUTF8="1"), stdout=log, stderr=log,
    )
    for _ in range(90):
        time.sleep(1)
        try:
            if urllib.request.urlopen(f"{BASE}/health", timeout=2).status == 200:
                return proc
        except Exception:
            continue
    proc.kill()
    raise RuntimeError("测试实例启动超时")


def lock_goal() -> str:
    """给 e2e 用户锁定一个有技能构成的目标岗位。

    页面默认用「我锁定的目标」出题，没有目标后端会 400；浏览器在 AUTH_BYPASS 下
    用的 uid 是 "0"（api-client 取 localStorage 或默认 0），这里要用同一个。
    """
    import json as _json

    from backend.kg.pg_store.client import connect

    with connect() as c:
        occ = c.execute(
            """SELECT o.id, o.name, COUNT(*) n FROM kg_edge e
               JOIN kg_node o ON o.id = e.src_id AND o.type='occupation'
               WHERE e.rel_type='requires' AND COALESCE(e.status,'published')='published'
               GROUP BY 1,2 HAVING COUNT(*) BETWEEN 4 AND 8 ORDER BY n DESC LIMIT 1"""
        ).fetchone()
    req = urllib.request.Request(
        f"{BASE}/v1/student/goal",
        data=_json.dumps({"occupation_id": occ["id"]}).encode(),
        headers={"Content-Type": "application/json", "X-Test-Uid": "0", "X-Test-Uname": "e2e"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        assert r.status == 200
    return occ["name"]


def canvas_has_pixels(page, sel: str) -> bool:
    """雷达图是否真画了东西（避免只验 canvas 存在）。"""
    return bool(page.evaluate(
        """(s) => {
          const c = document.querySelector(s);
          if (!c) return false;
          const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
          for (let i = 3; i < d.length; i += 4) if (d[i] !== 0) return true;
          return false;
        }""", sel))


def main() -> int:
    from playwright.sync_api import sync_playwright

    proc = start_server()
    errs: list[str] = []
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            page = b.new_page()
            page.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
            page.on("console", lambda m: errs.append(f"console.error: {m.text}")
                    if m.type == "error" else None)

            goal_name = lock_goal()
            print(f"已锁定目标岗位：{goal_name}\n", flush=True)

            # ---------- A 独立页初始态 ----------
            print("== A /assessment 初始态 ==", flush=True)
            page.goto(f"{BASE}/assessment?e2e=1", wait_until="domcontentloaded")
            page.wait_for_selector(".aw .steps .step", timeout=30000)
            steps = page.locator(".aw .steps .step")
            check("A1 步骤条固定三节点", steps.count() == 3,
                  " | ".join(steps.nth(i).inner_text().replace("\n", " ") for i in range(steps.count())))
            check("A2 首节点为 active",
                  "active" in (steps.nth(0).get_attribute("class") or ""),
                  steps.nth(0).get_attribute("class"))
            check("A3 阶段1 表单存在（上传区+文本框）",
                  page.locator("[data-drop]").count() == 1 and page.locator("[data-resume]").count() == 1)
            page.locator("[data-sample]").click()
            check("A4 范例简历可一键填入",
                  len(page.locator("[data-resume]").input_value()) > 30)

            # ---------- B 开始测评 ----------
            print("\n== B 开始测评（真实调用模型，约 10–30s）==", flush=True)
            t = time.time()
            page.locator("[data-start]").click()
            try:
                page.wait_for_selector(".aw .qbox", timeout=180000)
            except Exception:
                note = page.inner_text(".aw .awlog") if page.locator(".aw .awlog").count() else ""
                check("B0 出题流正常", False, f"页面提示：{note[:120]}")
                raise
            cls = [steps.nth(i).get_attribute("class") or "" for i in range(3)]
            check(f"B1 阶段1→done、阶段2→active（{time.time()-t:.0f}s）",
                  "done" in cls[0] and "active" in cls[1], f"{cls[0]} / {cls[1]}")
            check("B2 出现第一道题", page.locator(".aw .qbox").count() == 1,
                  page.locator(".aw .card2 h3").inner_text()[:40])

            # ---------- C 逐道作答（重点：翻页要快）----------
            print("\n== C 逐题作答 ==", flush=True)
            single_q = True
            answered = 0
            slow: list[float] = []
            progress_txt = ""
            for _ in range(MAX_ANSWERS):
                if page.locator("[data-radar]").count():
                    break
                try:
                    page.wait_for_selector(".aw .qbox", timeout=180000)
                except Exception:
                    break
                if page.locator(".aw .qbox").count() != 1:
                    single_q = False
                if not progress_txt:
                    m = page.locator(".aw .card2 .m").first
                    progress_txt = m.inner_text() if m.count() else ""

                t0 = time.time()
                opts = page.locator(".aw .opt2")
                if opts.count():
                    opts.nth(opts.count() - 1).click()
                elif page.locator("[data-ans]").count():
                    page.fill("[data-ans]",
                              "负责整车故障诊断，读故障码后用示波器比对波形定位线束虚接，"
                              "建立排查清单与复检流程，季度返修率从8%降到3%，培训2名新人。")
                    page.locator("[data-send]").click()
                else:
                    break
                answered += 1
                # 等下一题或报告出现，测量翻页耗时
                try:
                    page.wait_for_function(
                        """(n) => document.querySelectorAll('.aw .opt2, [data-ans]').length === 0
                                 || document.querySelector('[data-radar]')
                                 || (document.querySelector('.aw .card2 .m')
                                     && !document.querySelector('.aw .card2 .m').textContent.includes('第 ' + n + ' 题'))""",
                        arg=answered, timeout=180000)
                except Exception:
                    pass
                cost = time.time() - t0
                if cost > 3:
                    slow.append(round(cost, 1))
                page.wait_for_timeout(300)

            check("C1 同屏只展示一道题", single_q, f"已答 {answered} 题")
            check("C2 作答推进正常", answered >= 3, f"共答 {answered} 题")
            # 题目已在本地队列，翻页不该再等模型；问答题最后一道会触发结算，故放宽 1 次
            check("C3 翻页无明显等待（<3s）", len(slow) <= 1,
                  f"超时次数 {len(slow)}：{slow}" if slow else "全部秒翻")
            check("D1 进度显示总题数", "共" in progress_txt and "题" in progress_txt,
                  progress_txt.replace("\n", " ")[:60])

            # ---------- D 报告 ----------
            print("\n== D 综合能力报告 ==", flush=True)
            page.wait_for_selector("[data-radar]", timeout=180000)
            cls = [steps.nth(i).get_attribute("class") or "" for i in range(3)]
            check("E1 三节点全部 done", all("done" in c for c in cls), " / ".join(cls))
            body = page.inner_text(".aw")
            check("E2 展示匹配度", "综合能力匹配度" in body and "%" in body,
                  (page.locator(".aw .score2").inner_text() if page.locator(".aw .score2").count() else ""))
            check("E3 优势与短板区块存在",
                  "优势能力领域" in body and "关键能力短板" in body)
            check("E4 雷达图已绘制（canvas 非空白）", canvas_has_pixels(page, "[data-radar]"))
            check("E5 双系列图例", "学员实测能力" in body and "岗位标准要求" in body)

            # ---------- E 学员端 tab ----------
            print("\n== E 学员端「③ AI 诊断」tab ==", flush=True)
            page.goto(f"{BASE}/student?e2e=1", wait_until="domcontentloaded")
            page.wait_for_selector('.tabs button[data-tab="diag"]', timeout=30000)
            page.locator('.tabs button[data-tab="diag"]').click()
            page.wait_for_selector("#awBox .steps .step", timeout=30000)
            s2 = page.locator("#awBox .steps .step")
            check("F1 tab 内挂载同一组件（三节点）", s2.count() == 3)
            check("F2 tab 内阶段1 表单可用",
                  page.locator("#awBox [data-drop]").count() == 1)
            page.locator('.tabs button[data-tab="path"]').click()
            page.wait_for_timeout(2500)
            # 两级视图：进来先是「我的岗位诊断」列表，点卡片才进详情
            path_txt = page.inner_text("#tab-path")
            check("F3 路径 tab 一级列表可加载",
                  ("我的岗位诊断" in path_txt) or ("还没有诊断记录" in path_txt),
                  path_txt[:44].replace("\n", " "))
            card = page.locator("#tab-path [data-occ]").first
            if card.count():
                card.click()
                page.wait_for_timeout(3000)
                detail = page.inner_text("#tab-path")
                check("F4 点卡片进二级详情",
                      "胜任力能力报告" in detail and "自适应学习计划" in detail,
                      detail[:44].replace("\n", " "))
            else:
                check("F4 点卡片进二级详情", False, "列表无卡片")

            b.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    print("\n== F 全局 ==", flush=True)
    real = [e for e in errs if not any(
        s in e for s in ("favicon", "uc_sdk", "SDP", "net::ERR", "Failed to load resource"))]
    check("G1 全程无 JS 报错", not real, "; ".join(real[:3]))

    passed = sum(1 for _, ok, _ in _results if ok)
    print(f"\n{'='*56}\n结果：{passed}/{len(_results)} 通过", flush=True)
    if passed != len(_results):
        print("失败用例：", flush=True)
        for c, ok, n in _results:
            if not ok:
                print(f"  - {c} {n}", flush=True)
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
