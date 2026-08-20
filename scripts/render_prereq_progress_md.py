"""生成「行业 → 岗位 → 技能先修」的验收进度表。

给验收用：直接告诉你**在页面上点哪个行业、哪个岗位**能看到先修箭头。

统计口径与前端一致
------------------
前端只画**两端都在本岗位技能集内**的先修边（`industry_graph.occupation_skills_graph`
的设计，避免画出岗位外的孤立箭头）。所以这里也按「同一岗位同时要求 A 和 B，
且 A 是 B 的先修」来数 —— 库里有 100 条先修，不等于页面上能看到 100 条。

同理只统计**挂了行业**的岗位：没有 `belongs_to` 边的岗位从行业图走不进去，
先修数据再多也点不到。

可重复跑，覆盖输出。用法::

    python -X utf8 scripts/render_prereq_progress_md.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import session

DOC = ROOT / "docs" / "行业岗位技能先修处理进度.md"

# 岗位内可见的先修边：两端技能都被这个岗位要求
VISIBLE_SQL = """
WITH occ_sk AS (
  SELECT o.id oid, o.name onm, (o.attrs::jsonb->>'boss_l1') l1,
         (n.attrs::jsonb->>'skill_key') k
  FROM kg_edge e
  JOIN kg_node o ON o.id = e.src_id AND o.type = 'occupation'
       AND COALESCE(o.status,'published') = 'published' AND NOT COALESCE(o.is_draft,false)
  JOIN kg_node n ON n.id = e.dst_id AND n.type = 'skill_level'
       AND COALESCE(n.status,'published') = 'published' AND NOT COALESCE(n.is_draft,false)
  WHERE e.rel_type = 'requires' AND COALESCE(e.status,'published') = 'published'
),
vis AS (
  SELECT a.oid, a.onm, a.l1, count(*)::int prereq_cnt
  FROM kg_skill_prereq p
  JOIN occ_sk a ON a.k = p.skill_key
  JOIN occ_sk b ON b.k = p.prereq_skill_key AND b.oid = a.oid
  GROUP BY 1,2,3
),
sk AS (SELECT oid, count(DISTINCT k)::int n FROM occ_sk GROUP BY 1)
SELECT v.oid, v.onm, v.l1, v.prereq_cnt, sk.n AS skill_cnt,
       (SELECT string_agg(DISTINCT i.name, ' / ' ORDER BY i.name) FROM kg_edge be
          JOIN kg_node i ON i.id = be.dst_id AND i.type = 'industry'
               AND COALESCE(i.status,'published') = 'published'
         WHERE be.src_id = v.oid AND be.rel_type = 'belongs_to'
           AND COALESCE(be.status,'published') = 'published') AS industries
FROM vis v JOIN sk ON sk.oid = v.oid
ORDER BY v.prereq_cnt DESC, v.onm
"""


def main() -> None:
    with session() as c, c.cursor() as cur:
        cur.execute(VISIBLE_SQL)
        rows = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT count(*)::int n FROM kg_skill_prereq")
        total_prereq = cur.fetchone()["n"]
        cur.execute(
            "SELECT confidence, count(*)::int n FROM kg_skill_prereq GROUP BY 1 ORDER BY 2 DESC"
        )
        by_conf = {r["confidence"]: r["n"] for r in cur.fetchall()}
        # 各门类的岗位总数，用来算覆盖进度
        cur.execute(
            """SELECT (attrs::jsonb->>'boss_l1') l1, count(*)::int n FROM kg_node
               WHERE type='occupation' AND source_system='BOSS'
                 AND COALESCE(status,'published')='published' AND NOT COALESCE(is_draft,false)
               GROUP BY 1 HAVING (attrs::jsonb->>'boss_l1') IS NOT NULL ORDER BY 2 DESC"""
        )
        l1_total = {r["l1"]: r["n"] for r in cur.fetchall()}

    with_ind = [r for r in rows if r["industries"]]
    without_ind = [r for r in rows if not r["industries"]]
    done_by_l1: dict[str, int] = {}
    for r in rows:
        if r["l1"]:
            done_by_l1[r["l1"]] = done_by_l1.get(r["l1"], 0) + 1

    ver = subprocess.run(
        ["git", "log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.strip()

    L: list[str] = []
    L.append("# 行业 → 岗位 → 技能先修：处理进度")
    L.append("")
    L.append(f"数据库：**开发库 voced_kg_dev**（效果确认后再迁到基准库）。最后更新：{ver}")
    L.append("")
    L.append("## 怎么验收")
    L.append("")
    L.append("打开 `http://127.0.0.1:8088/kg` → 点「行业」列的行业 → 点「岗位」列的岗位，")
    L.append("技能图谱里会画出先修箭头（分区内从上到下）。")
    L.append("")
    L.append("> 页面上看到的条数**可能少于**下表：前端只画两端技能都属于该岗位的边。")
    L.append("> 下表已经按这个口径统计，所以两者应当一致；不一致就是 bug，请告诉我。")
    L.append("")
    L.append("## 总量")
    L.append("")
    L.append("| 指标 | 数值 |")
    L.append("|---|--:|")
    L.append(f"| `kg_skill_prereq` 总条数 | {total_prereq} |")
    for k, v in by_conf.items():
        src = {"ai_inferred": "LLM 生成", "derived": "规则派生（国标分类推进顺序）",
               "manual_seed": "人工录入"}.get(k, k)
        L.append(f"| 其中 {src} | {v} |")
    L.append(f"| 页面上能看到先修的岗位 | {len(rows)} |")
    L.append(f"| 其中挂了行业（能从行业图走到） | **{len(with_ind)}** |")
    L.append(f"| 没挂行业（只能靠搜索进） | {len(without_ind)} |")
    L.append("")

    L.append("## 各门类进度")
    L.append("")
    L.append("| BOSS 门类 | 岗位总数 | 已有先修 | 进度 |")
    L.append("|---|--:|--:|---|")
    for l1, tot in l1_total.items():
        done = done_by_l1.get(l1, 0)
        pct = done * 100 // tot if tot else 0
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        L.append(f"| {l1} | {tot} | {done} | {bar} {pct}% |")
    L.append("")

    L.append("## 能测的：行业 → 岗位")
    L.append("")
    if with_ind:
        L.append("| 行业（在行业图里点这个） | 岗位 | 技能数 | 先修条数 |")
        L.append("|---|---|--:|--:|")
        for r in with_ind:
            L.append("| %s | **%s** | %d | %d |"
                     % (r["industries"], r["onm"], r["skill_cnt"], r["prereq_cnt"]))
    else:
        L.append("（暂无）")
    L.append("")

    if without_ind:
        L.append("## 有先修但没挂行业的岗位")
        L.append("")
        L.append("这些岗位的先修数据是好的，但**从行业图走不到**——它们没有 `belongs_to` 边。")
        L.append("国标 1331 个岗位里 1025 个没挂行业，且已挂的那 306 个也是关键词机械对齐的结果")
        L.append("（每个行业整整 12 个岗位、每个岗位平均挂 6.3 个行业，出现「广告设计师 → 体育」）。")
        L.append("这批要重做，见另一个任务。")
        L.append("")
        L.append("| 岗位 | 技能数 | 先修条数 |")
        L.append("|---|--:|--:|")
        for r in without_ind[:40]:
            L.append("| %s | %d | %d |" % (r["onm"], r["skill_cnt"], r["prereq_cnt"]))
        if len(without_ind) > 40:
            L.append(f"| …共 {len(without_ind)} 个 | | |")
        L.append("")

    DOC.parent.mkdir(parents=True, exist_ok=True)
    DOC.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("已写入", DOC)
    print("  能从行业图走到的岗位：%d，另有 %d 个没挂行业" % (len(with_ind), len(without_ind)))


if __name__ == "__main__":
    main()
