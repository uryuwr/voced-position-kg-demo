"""闭环验证：完成诊断 → 同步五维记忆 → 画像有更新 → 跨岗位匹配度可用。

    ① 记录同步前的五维记忆快照（memoryId 集合）
    ② 走真实 HTTP 跑完一次测评（出题 / 逐题作答 / 结算）
    ③ 等异步 memory-signals/force 任务落地
    ④ 重新查五维记忆，比对是否出现新记忆
    ⑤ 用更新后的画像给**没测过的岗位**算匹配度

第 ⑤ 步才是同步记忆的意义：同一岗位直接读报告分用不着记忆，
跨岗位时记忆是唯一的能力证据来源。

用法：python -X utf8 tests/e2e_memory_loop.py
需要：BTS + OPENQ_AI_MANAGER 已配置，且 AI 网关可用（出题要调模型）。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 有真实五维记忆的 UC 用户（前端 MAC 校验后返回的 user_id）
UC_USER = "260193631898"
_results: list[tuple[str, bool, str]] = []


def check(case: str, ok: bool, note: str = "") -> None:
    _results.append((case, bool(ok), note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {case}" + (f" — {note}" if note else ""), flush=True)


def snapshot_memory() -> dict[str, str]:
    """当前五维记忆快照：memoryId → 内容指纹。

    只比 memoryId 集合是不够的：平台对同一件事会做 `corrected`——
    改写已有记忆而不是新建一条，此时 id 不变但内容变了，那同样是「画像有更新」。
    """
    from backend.userprofile import memories as mem

    payload = mem.search_memories(UC_USER, facets=mem.ALL_FACETS)
    snap: dict[str, str] = {}
    for g in payload.get("groups") or []:
        for it in g.get("items") or []:
            if it.get("memoryId"):
                snap[it["memoryId"]] = "|".join(
                    str(it.get(k) or "") for k in ("title", "summary", "details", "updatedAt")
                )
    return snap


def pick_occupations(seen_text: str = "") -> tuple[dict, dict]:
    """挑一个技能少（测得快）的岗位做诊断，另挑一个没测过但技能有重叠的验证跨岗位。

    诊断岗位要避开画像里已经出现过的：同一岗位再测一遍，提交的文本是同一件事，
    平台会判为无变化，第 ④ 步就看不出「画像被更新」了。
    """
    from backend.kg.pg_store.client import connect

    with connect() as c:
        rows = c.execute(
            """SELECT o.id, o.name, COUNT(*) n FROM kg_edge e
               JOIN kg_node o ON o.id = e.src_id AND o.type='occupation'
               WHERE e.rel_type='requires' AND COALESCE(e.status,'published')='published'
               GROUP BY 1,2 HAVING COUNT(*) BETWEEN 3 AND 5 ORDER BY n LIMIT 40"""
        ).fetchall()
        # 跨岗位验证必须挑**与诊断岗位有共享技能**的岗位：技能零重叠时
        # 返回 no_overlap 是正确行为，验不出「记忆是否被用上」。
        #
        # 所以不能先定 target 再找同伴——技能越少的岗位越可能是冷门岗，
        # 技能全库独有、一个同伴都没有（踩过：水生动物饲养工的
        # 饲养技术/环境/设施管理三项无人共用，找同伴直接返回 None）。
        # 改成逐个候选试，直到找到「不在画像里 且 确实有同伴」的那个。
        from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL

        peer_sql = f"""
            WITH tgt AS (
              SELECT DISTINCT ({SKILL_KEY_SQL}) AS k
              FROM kg_edge e JOIN kg_node n ON n.id = e.dst_id AND n.type='skill_level'
              WHERE e.src_id = %s AND e.rel_type='requires'
                AND COALESCE(e.status,'published')='published'
            )
            SELECT o.id, o.name, COUNT(*) AS shared
            FROM kg_edge e
            JOIN kg_node n ON n.id = e.dst_id AND n.type='skill_level'
            JOIN kg_node o ON o.id = e.src_id AND o.type='occupation'
            WHERE e.rel_type='requires' AND COALESCE(e.status,'published')='published'
              AND o.id <> %s AND ({SKILL_KEY_SQL}) IN (SELECT k FROM tgt)
            GROUP BY 1,2 ORDER BY shared DESC LIMIT 1
        """
        fresh = [r for r in rows if r["name"] not in seen_text] or rows
        for cand in fresh:
            peer = c.execute(peer_sql, (cand["id"], cand["id"])).fetchone()
            if peer:
                return dict(cand), dict(peer)

    raise RuntimeError(
        f"{len(fresh)} 个候选岗位都没有共享技能的同伴，跨岗位验证无从做起；"
        "放宽 HAVING COUNT(*) 的区间再试"
    )


def main() -> int:
    import backend.api.auth as auth

    auth.AUTH_DEBUG = True
    from fastapi.testclient import TestClient

    import backend.api.main as m
    from backend.userprofile import memories as mem
    from backend.userprofile import sync as msync

    c = TestClient(m.app)
    H = {"X-Test-Uid": UC_USER, "X-Test-Uname": "e2e"}
    E = lambda x: urllib.parse.quote(x, safe="")  # noqa: E731

    print("== 前置检查 ==", flush=True)
    check("画像服务可用", mem.available())
    check("记忆同步可用", msync.available())
    if not (mem.available() and msync.available()):
        print("\n未配置 BTS / OPENQ_AI_MANAGER，闭环无法验证", flush=True)
        return 1

    # ① 同步前快照
    print("\n== ① 同步前的五维记忆 ==", flush=True)
    before = snapshot_memory()
    print(f"  现有 {len(before)} 条记忆", flush=True)

    target, other = pick_occupations(seen_text=" ".join(before.values()))
    print(f"  诊断岗位：{target['name']}（{target['n']} 项技能，画像中未出现过）", flush=True)
    print(f"  跨岗位验证：{other['name']}（与诊断岗位共享 {other.get('shared', '?')} 项技能）", flush=True)

    # ② 跑完一次测评
    print("\n== ② 完成一次诊断 ==", flush=True)
    t = time.time()
    sid = None
    questions: list[dict] = []
    with c.stream(
        "POST", "/v1/student/assessment/sessions/questions/stream",
        headers=H, json={"occupation_id": target["id"]},
    ) as r:
        ev, data = None, ""
        for line in r.iter_lines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
            elif line == "":
                if data:
                    d = json.loads(data)
                    if ev == "session":
                        sid = d["session_id"]
                    elif ev == "question":
                        questions.append(d["question"])
                    data = ""
    check(f"出题完成（{time.time()-t:.0f}s）", bool(sid) and len(questions) >= 3,
          f"session={sid} 共 {len(questions)} 题")
    if not sid or not questions:
        return 1

    for q in questions:
        ans = (
            max(q["options"], key=lambda o: o["level"])["value"]
            if q["type"] == "choice"
            else "负责该领域核心工作三年，独立处理过多起复杂问题，"
                 "建立了标准排查流程并培训同事，关键指标提升 30%。"
        )
        c.post(f"/v1/student/assessment/sessions/{sid}/answers",
               headers=H, json={"index": q["index"], "answer": ans})

    report = None
    with c.stream("POST", f"/v1/student/assessment/sessions/{sid}/report/stream",
                  headers=H, json={}) as r:
        ev, data = None, ""
        for line in r.iter_lines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
            elif line == "":
                if data:
                    d = json.loads(data)
                    if ev == "report":
                        report = d["report"]
                    data = ""
    check("生成综合能力报告", bool(report and report.get("match_score") is not None),
          f"匹配度 {report.get('match_score') if report else '-'}%")

    # ③ 等异步同步任务
    print("\n== ③ 等待记忆同步（异步 force 提交）==", flush=True)
    sync_ev = None
    for i in range(24):                        # 最多等 120s
        time.sleep(5)
        from backend.kg.pg_store.client import connect

        with connect() as conn:
            row = conn.execute(
                "SELECT payload FROM biz_event WHERE user_id=%s AND event_type='memory_sync' "
                "ORDER BY created_at DESC LIMIT 1",
                (UC_USER,),
            ).fetchone()
        if row:
            p = row["payload"]
            if isinstance(p, str):
                p = json.loads(p)
            if p.get("session_id") == sid:
                sync_ev = p
                break
        if (i + 1) % 4 == 0:
            print(f"  等待中…（{(i+1)*5}s）", flush=True)

    check("同步任务已执行", sync_ev is not None,
          f"submission={(sync_ev or {}).get('submission_id')}" if sync_ev else "超时未见事件")
    if sync_ev:
        check("force 提交被接受", bool(sync_ev.get("ok")),
              sync_ev.get("error") or f"status={sync_ev.get('status')}")

    # ④ 平台的处理结论
    print("\n== ④ 平台归纳结论 ==", flush=True)
    sub: dict = {}
    if sync_ev and sync_ev.get("submission_id"):
        for _ in range(18):                    # 平台异步归纳，最多等 90s
            try:
                sub = msync.get_submission(UC_USER, sync_ev["submission_id"]) or {}
            except Exception as e:             # noqa: BLE001
                sub = {"error": str(e)[:120]}
            if sub.get("status") == "completed":
                break
            time.sleep(5)
    results = sub.get("results") or []
    # created=新建；corrected=改写已有（同一件事再提交一次会走这条）；merged=并入
    actions = [r.get("action") for r in results]
    check("平台已归纳入库", sub.get("status") == "completed" and bool(results),
          f"status={sub.get('status')} action={actions}")
    for r in results:
        print(f"    [{r.get('facet')}] {r.get('action')} {r.get('memoryId')} "
              f"conf={r.get('confidence')}", flush=True)
        print(f"      {(r.get('factText') or '')[:90]}", flush=True)

    # ④' 画像确实变了（新增 or 内容被改写）
    print("\n== ④' 重新查五维记忆 ==", flush=True)
    touched = {r.get("memoryId") for r in results if r.get("memoryId")}
    after: dict[str, str] = {}
    for i in range(18):
        time.sleep(5)
        after = snapshot_memory()
        if (set(after) - set(before)) or any(after.get(k) != before.get(k) for k in after):
            break
        if (i + 1) % 4 == 0:
            print(f"  仍为 {len(after)} 条且内容未变，继续等…（{(i+1)*5}s）", flush=True)
    added = set(after) - set(before)
    changed = {k for k in after if k in before and after[k] != before[k]}
    check("画像有更新（新增或改写）", bool(added or changed),
          f"{len(before)} → {len(after)} 条，新增 {len(added)}、改写 {len(changed)}")
    check("变化的正是平台命中的那条记忆", bool(touched & (added | changed)) or not touched,
          f"平台命中 {touched or '-'}，实际变化 {(added | changed) or '-'}")
    payload = mem.search_memories(UC_USER, facets=mem.ALL_FACETS)
    for g in payload.get("groups") or []:
        for it in g.get("items") or []:
            if it.get("memoryId") in (added | changed):
                tag = "新增" if it["memoryId"] in added else "改写"
                print(f"    [{g['facet']}] {tag} {it.get('title')} — "
                      f"{(it.get('summary') or '')[:60]}", flush=True)

    # ⑤ 跨岗位匹配度用上新画像
    print("\n== ⑤ 跨岗位匹配度（没测过的岗位）==", flush=True)
    from backend.userprofile import invalidate

    invalidate(UC_USER)
    t = time.time()
    d = c.get(f"/v1/student/positions/match?position_id={E(other['id'])}", headers=H).json()
    check(f"未诊断岗位可算出匹配度（{time.time()-t:.0f}s）",
          d.get("match_score") is not None and d.get("covered_count", 0) > 0,
          f"{other['name']}: {d.get('match_score')}% source={d.get('source')} "
          f"命中{d.get('covered_count')}/{d.get('skill_total')}")

    passed = sum(1 for _, ok, _ in _results if ok)
    print(f"\n{'='*56}\n结果：{passed}/{len(_results)} 通过", flush=True)
    if passed != len(_results):
        print("失败用例：", flush=True)
        for k, ok, n in _results:
            if not ok:
                print(f"  - {k} {n}", flush=True)
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
