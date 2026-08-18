"""生成「已打磨岗位清单」章节，追加/更新到 docs/热门岗位TOP100与方法论.md。

给验收用：明确列出**在页面上搜哪些词能看到完整五段链路**，并逐岗位标出
行业/专业/技能/晋升/课程各段的实际条数，以及课程资源的真实来源构成
（真实 MOOC 课程 vs 检索落地页），避免"有课程"这个字眼掩盖资源质量差异。

用法::

    python -X utf8 scripts/render_pilot_positions_md.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import session

DOC = ROOT / "docs" / "热门岗位TOP100与方法论.md"
MARK_BEGIN = "<!-- PILOT_POSITIONS:BEGIN -->"
MARK_END = "<!-- PILOT_POSITIONS:END -->"

SQL = """
WITH occ AS (
  SELECT id, name,
         (attrs::jsonb->>'boss_l2')     AS l2,
         (attrs::jsonb->>'job_family')  AS family,
         NULLIF(attrs::jsonb->>'level','')::int AS lv
  FROM kg_node
  WHERE type='occupation' AND source_system='BOSS'
    AND (attrs::jsonb->>'boss_l1') = %s
    AND COALESCE(status,'published')='published'
),
ind AS (SELECT src_id oid, count(*)::int n FROM kg_edge
        WHERE rel_type='belongs_to' AND COALESCE(status,'published')='published' GROUP BY 1),
maj AS (SELECT dst_id oid, count(*)::int n FROM kg_edge
        WHERE rel_type='prepares_for' AND COALESCE(status,'published')='published' GROUP BY 1),
adv AS (SELECT src_id oid, count(*)::int n FROM kg_edge
        WHERE rel_type='advances_to' AND COALESCE(status,'published')='published' GROUP BY 1),
sk AS (SELECT src_id oid, dst_id sid FROM kg_edge
       WHERE rel_type='requires' AND COALESCE(status,'published')='published'),
skn AS (SELECT oid, count(*)::int n FROM sk GROUP BY 1),
-- 课程按来源分：ICOURSE163=真实慕课课程，SEARCH_LANDING_CN=检索入口
crs AS (
  SELECT sk.oid,
         count(*) FILTER (WHERE c.source_system='ICOURSE163')::int  AS real_n,
         count(*) FILTER (WHERE c.source_system<>'ICOURSE163')::int AS landing_n
  FROM sk
  JOIN kg_edge te ON te.src_id = sk.sid AND te.rel_type='taught_by'
                 AND COALESCE(te.status,'published')='published'
  JOIN kg_node c  ON c.id = te.dst_id
  GROUP BY 1
)
SELECT o.name, o.l2, o.family, o.lv,
       COALESCE(ind.n,0) ind_n, COALESCE(maj.n,0) maj_n,
       COALESCE(skn.n,0) sk_n, COALESCE(adv.n,0) adv_n,
       COALESCE(crs.real_n,0) crs_real, COALESCE(crs.landing_n,0) crs_landing
FROM occ o
LEFT JOIN ind ON ind.oid=o.id
LEFT JOIN maj ON maj.oid=o.id
LEFT JOIN adv ON adv.oid=o.id
LEFT JOIN skn ON skn.oid=o.id
LEFT JOIN crs ON crs.oid=o.id
ORDER BY o.l2, o.name
"""


def build(l1: str = "技术") -> str:
    with session() as c, c.cursor() as cur:
        cur.execute(SQL, (l1,))
        rows = cur.fetchall()

    tot = len(rows)
    full = sum(1 for r in rows if r["ind_n"] and r["maj_n"] and r["sk_n"] and r["crs_real"] + r["crs_landing"])
    real_course = sum(1 for r in rows if r["crs_real"])
    with_adv = sum(1 for r in rows if r["adv_n"])

    lines = [
        MARK_BEGIN,
        "",
        "## 七、已打磨岗位清单（验收用）",
        "",
        f"下面 {tot} 个岗位（BOSS 一级门类「{l1}」）已完成**五段链路**打磨。",
        "**验收方式**：打开 `/student` 或 `/capability`，直接搜「岗位」列的词。",
        "",
        f"- 五段齐全（行业+专业+技能+课程）：**{full}/{tot}**",
        f"- 有真实慕课课程（非检索入口）：**{real_course}/{tot}**",
        f"- 有晋升链：**{with_adv}/{tot}**（跨族路径，平行技术方向不算晋升）",
        "",
        "列含义：**行业** `belongs_to` ｜ **专业** `prepares_for` ｜ **技能** `requires`（权重 Σ=1）",
        "｜ **晋升** `advances_to` ｜ **课程** `taught_by`，其中「真课」= 中国大学MOOC 实际课程（带选课人数），",
        "「检索」= 慕课检索入口（课程库无覆盖时的兜底，**不等于真实课程**）。",
        "",
        "| 岗位（搜这个） | 方向 | 职级 | 岗位族 | 行业 | 专业 | 技能 | 晋升 | 真课 | 检索 |",
        "|---|---|:--:|---|:--:|:--:|:--:|:--:|:--:|:--:|",
    ]
    for r in rows:
        lv = f"L{r['lv']}" if r["lv"] else "—"
        lines.append(
            "| {name} | {l2} | {lv} | {fam} | {i} | {m} | {s} | {a} | {cr} | {cl} |".format(
                name=r["name"], l2=r["l2"] or "—", lv=lv, fam=r["family"] or "—",
                i=r["ind_n"] or "—", m=r["maj_n"] or "—", s=r["sk_n"] or "—",
                a=r["adv_n"] or "—",
                cr=r["crs_real"] or "—", cl=r["crs_landing"] or "—",
            )
        )
    lines += ["", MARK_END, ""]
    return "\n".join(lines)


def main() -> None:
    section = build("技术")
    text = DOC.read_text(encoding="utf-8")
    if MARK_BEGIN in text and MARK_END in text:
        head = text.split(MARK_BEGIN)[0]
        tail = text.split(MARK_END)[1]
        text = head + section + tail
    else:
        text = text.rstrip() + "\n\n" + section
    DOC.write_text(text, encoding="utf-8")
    n = section.count("\n| ") - 1
    print(f"已写入 {DOC}")
    print(f"  清单行数: {n}")


if __name__ == "__main__":
    main()
