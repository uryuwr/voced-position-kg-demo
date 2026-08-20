"""用 LLM 生成技能先修关系（`kg_skill_prereq`），按岗位的技能集判断。

为什么规则法覆盖不了这批
------------------------
`seed_skill_prereqs.py` 靠 `PREREQ_ALLOWED_PAIRS`（分类间的允许方向）+ 同岗位共现，
产出了 60 条。它表达的是国标那套**跨分类推进**：安全 → 作业准备 → 操作加工 →
检修质检 → 技术管理 → 培训指导。

但互联网技能的先修绝大多数是**同分类内**的：

    Java核心编程 → SpringBoot开发      （都是 TECH）
    SQL数据库开发 → SQL性能优化          （都是 TECH）

「TECH → TECH」这种规则写不出方向，所以 820 个 BOSS 岗位的先修基本是空的
（全库 5875 个逻辑技能里只有 38 个有先修）。这类先后顺序是语义判断，只能问模型。

为什么按岗位分批，而不是按分类
----------------------------
先修是技能之间的关系，看似与岗位无关。但**判断依据是共现**：同一个岗位同时要求
`Java核心编程` 与 `SpringBoot开发`，才说明这两者在真实工作里是一条学习路径上的。
按分类切批会把八竿子打不着的技能凑一起（TECH 里既有 `UE4材质系统` 也有
`模拟电路基础`），模型只能瞎猜。

而且前端**只画两端都在本岗位技能集内的先修边**（`industry_graph`
的设计，避免画出岗位外的孤立箭头）——按岗位生成，产出即可见。

用编号而不是技能名
------------------
提示词里给编号让模型引用，落库时再映射回 `skill_key`。技能名里带 `.` `#` `%`
和空格（`.NET Core开发`、`C#编程开发`），让模型原样抄名字必然出现细微差异，
匹配不上就整批丢弃。

用法::

    python -X utf8 scripts/gen_skill_prereq_llm.py --l1 技术 --limit 20 --dry-run
    python -X utf8 scripts/gen_skill_prereq_llm.py --l1 技术
    python -X utf8 scripts/gen_skill_prereq_llm.py --all
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import session
from backend.kg.pg_store.skill_prereq import add_prereq

REPORT = ROOT / "reports" / "skill_prereq_llm.json"
CACHE = ROOT / "data" / "staging" / "skill_prereq_llm_cache.json"

PROMPT = """下面是「{occ}」这个岗位要求的技能。请判断它们之间有没有**硬性的先后学习顺序**。

技能清单（编号 | 技能名 | 要求档 | 分类）：
{skills}

什么算先修：不先掌握 A，就没法开始学 B。
  会算：Java核心编程 → SpringBoot开发（不懂 Java 语法学不了框架）
        SQL数据库开发 → SQL性能优化（不会写查询谈不上调优）
  不算：并列关系（前端开发 / 后端开发）、互补关系（编码 / 测试）、
        仅仅是"都需要"（沟通协作 与 项目管理 之间没有先后）

规则：
1. 用**编号**表示，from 是前置技能，to 是后继技能
2. 只输出确定的硬依赖，**宁缺勿滥** —— 这个岗位可能一条都没有，那就返回空数组
3. 不要成环（A→B 就不能再 B→A），也不要自指
4. 同一个技能的前置最多 2 个，只留最直接的那个
5. reason 一句话说清"为什么必须先会 A"

只输出 JSON，不要解释：
{{"pairs": [{{"from": 1, "to": 2, "reason": "一句话"}}]}}"""


def load_occupations(l1: str | None, limit: int | None) -> list[dict]:
    """取岗位及其技能集。只要有 ≥2 个技能的岗位 —— 一个技能谈不上先后。"""
    where = ["o.type = 'occupation'", "COALESCE(o.status,'published') = 'published'",
             "NOT COALESCE(o.is_draft, false)"]
    params: list = []
    if l1:
        where.append("(o.attrs::jsonb->>'boss_l1') = %s")
        params.append(l1)
    sql = f"""
        SELECT o.id, o.name,
               json_agg(json_build_object(
                 'key', n.attrs::jsonb->>'skill_key',
                 'name', COALESCE(n.attrs::jsonb->>'skill_name',
                                  regexp_replace(n.name, ' · L[1-5]$', '')),
                 'level', n.attrs::jsonb->>'level',
                 'cat', n.category
               ) ORDER BY e.weight DESC NULLS LAST) AS skills
        FROM kg_node o
        JOIN kg_edge e ON e.src_id = o.id AND e.rel_type = 'requires'
             AND COALESCE(e.status,'published') = 'published' AND NOT COALESCE(e.is_draft,false)
        JOIN kg_node n ON n.id = e.dst_id AND n.type = 'skill_level'
             AND COALESCE(n.status,'published') = 'published' AND NOT COALESCE(n.is_draft,false)
        WHERE {' AND '.join(where)}
        GROUP BY o.id, o.name
        HAVING count(DISTINCT n.attrs::jsonb->>'skill_key') >= 2
        ORDER BY o.name
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    with session() as c, c.cursor() as cur:
        cur.execute(sql, params)
        out = []
        for r in cur.fetchall():
            # 同一技能的 L1–L5 会重复出现，按 key 去重（先修是逻辑技能之间的关系）
            seen: dict[str, dict] = {}
            for s in r["skills"] or []:
                if s.get("key") and s["key"] not in seen:
                    seen[s["key"]] = s
            if len(seen) >= 2:
                out.append({"id": r["id"], "name": r["name"], "skills": list(seen.values())})
        return out


def parse_pairs(raw: str) -> list[dict]:
    t = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.M)
    a, b = t.find("{"), t.rfind("}")
    if a >= 0 and b > a:
        try:
            return (json.loads(t[a : b + 1]) or {}).get("pairs") or []
        except Exception:
            pass
    # 截断抢救：按对象级正则捞回完整的 pair
    return [
        {"from": int(m.group(1)), "to": int(m.group(2)), "reason": m.group(3) or ""}
        for m in re.finditer(
            r'\{\s*"from"\s*:\s*(\d+)\s*,\s*"to"\s*:\s*(\d+)'
            r'(?:\s*,\s*"reason"\s*:\s*"([^"]*)")?\s*\}', t)
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--l1", help="限定 BOSS 一级门类")
    ap.add_argument("--all", action="store_true", help="全部岗位")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.3)
    args = ap.parse_args()

    if not args.l1 and not args.all:
        print("需要 --l1 <门类> 或 --all"); sys.exit(2)

    from backend.agent.llm import invoke_fast, llm_ready

    if not llm_ready():
        print("LLM 网关未配置"); sys.exit(1)

    occs = load_occupations(args.l1, args.limit)
    print("待处理岗位：%d" % len(occs))

    cache: dict[str, list] = {}
    if CACHE.exists():
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    edges: list[dict] = []
    dropped: list[dict] = []
    for i, o in enumerate(occs, 1):
        if o["id"] in cache:
            got = cache[o["id"]]
        else:
            listing = "\n".join(
                "%d | %s | L%s | %s" % (j, s["name"], s.get("level") or "?", s.get("cat") or "-")
                for j, s in enumerate(o["skills"], 1)
            )
            msg = [("system", "你是职业技能培养路径专家，只输出 JSON。"),
                   ("user", PROMPT.format(occ=o["name"], skills=listing))]
            try:
                got = parse_pairs(invoke_fast(msg, max_tokens=2000))
            except Exception as e:  # noqa: BLE001
                print("  [%d/%d] %s 调用失败：%s" % (i, len(occs), o["name"], str(e)[:60]))
                continue
            cache[o["id"]] = got
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            if args.sleep:
                time.sleep(args.sleep)

        n_ok = 0
        for p in got:
            try:
                fi, ti = int(p["from"]) - 1, int(p["to"]) - 1
            except (KeyError, TypeError, ValueError):
                continue
            if not (0 <= fi < len(o["skills"]) and 0 <= ti < len(o["skills"])) or fi == ti:
                dropped.append({"occ": o["name"], "pair": p, "why": "编号越界或自指"})
                continue
            src, dst = o["skills"][fi], o["skills"][ti]
            edges.append({
                "skill_key": dst["key"], "prereq_skill_key": src["key"],
                "evidence": "岗位「%s」：先掌握「%s」才能学「%s」—— %s"
                            % (o["name"], src["name"], dst["name"],
                               str(p.get("reason") or "")[:60]),
            })
            n_ok += 1
        if i % 20 == 0 or i == len(occs):
            print("  [%d/%d] 累计有效对 %d" % (i, len(occs), len(edges)))

    # 同一 (后继, 前置) 可能被多个岗位提出 —— 去重，evidence 取第一个
    uniq: dict[tuple[str, str], dict] = {}
    for e in edges:
        uniq.setdefault((e["skill_key"], e["prereq_skill_key"]), e)
    print()
    print("LLM 提出 %d 对，去重后 %d 对，丢弃 %d" % (len(edges), len(uniq), len(dropped)))

    if args.dry_run:
        for e in list(uniq.values())[:12]:
            print("   %s" % e["evidence"][:96])
        print("\n(dry-run，未写库)")
        return

    ok = cyc = 0
    for e in uniq.values():
        try:
            # add_prereq 自带自指与成环校验（_would_cycle），不在这里重写
            add_prereq(e["skill_key"], e["prereq_skill_key"],
                       evidence=e["evidence"], confidence="ai_inferred",
                       created_by="gen_skill_prereq_llm")
            ok += 1
        except ValueError as ex:
            cyc += 1
            dropped.append({"pair": e, "why": str(ex)[:80]})
    print("写入 %d 对，被环/自指校验挡下 %d 对" % (ok, cyc))

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(
        {"occupations": len(occs), "proposed": len(edges), "unique": len(uniq),
         "written": ok, "rejected": cyc, "dropped_sample": dropped[:20]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print("报告：", REPORT)


if __name__ == "__main__":
    main()
