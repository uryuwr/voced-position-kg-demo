"""生成「已打磨岗位清单」章节，追加/更新到 docs/热门岗位TOP100与方法论.md。

给验收用：明确列出**在页面上搜哪些词能看到完整五段链路**，并逐岗位标出
行业/专业/技能/晋升/课程各段的实际条数。课程只计**点开当场能学**的
（免登录、免报名、无开课周期），避免"有课程"这个字眼掩盖资源质量差异 ——
这里连续掩盖过两次：先是课标目录冒充课程，后是 MOOC 的报名墙冒充可学。

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
# 真实资源的判定**只有一处真源**：后端 _REAL_COURSE_SOURCES。
# 这里曾硬编码成「只有 ICOURSE163 算真课」，于是学堂在线和官方文档
# 全被算进「检索入口」，清单统计与页面显示对不上。别再抄一份。
from backend.kg.pg_store.skill_aggregate import _REAL_COURSE_SOURCES

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
    AND (attrs::jsonb->>'boss_l1') = %(l1)s
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
-- 课程按性质分：点开当场能学 vs 其它（需报名、检索入口、课标目录）。
-- ⚠ 必须带 c.status 过滤：下架的资源若照样计数，清单会报出页面上根本看不到的课
--   （2026-08-18 下架 MOOC 与检索入口时踩到，清单显示 6 门、页面显示 0 门）。
crs AS (
  SELECT sk.oid,
         count(*) FILTER (WHERE c.source_system = ANY(%(real_src)s))::int  AS real_n,
         count(*) FILTER (WHERE c.source_system <> ALL(%(real_src)s))::int AS other_n
  FROM sk
  JOIN kg_edge te ON te.src_id = sk.sid AND te.rel_type='taught_by'
                 AND COALESCE(te.status,'published')='published'
  JOIN kg_node c  ON c.id = te.dst_id AND COALESCE(c.status,'published')='published'
  GROUP BY 1
)
SELECT o.name, o.l2, o.family, o.lv,
       COALESCE(ind.n,0) ind_n, COALESCE(maj.n,0) maj_n,
       COALESCE(skn.n,0) sk_n, COALESCE(adv.n,0) adv_n,
       COALESCE(crs.real_n,0) crs_real, COALESCE(crs.other_n,0) crs_other
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
        cur.execute(SQL, {"l1": l1, "real_src": list(_REAL_COURSE_SOURCES)})
        rows = cur.fetchall()

    tot = len(rows)
    # 「五段齐全」的课程段只认可直接学的资源。曾把检索入口也算进来，
    # 于是 109/117 齐全，实际能学的只有 65 —— 数字好看但验收时对不上。
    full = sum(1 for r in rows if r["ind_n"] and r["maj_n"] and r["sk_n"] and r["crs_real"])
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
        f"- 五段齐全（行业+专业+技能+可学课程）：**{full}/{tot}**",
        f"- 有可直接学的资源：**{real_course}/{tot}**",
        f"- 有晋升链：**{with_adv}/{tot}**（跨族路径，平行技术方向不算晋升）",
        "",
        "列含义：**行业** `belongs_to` ｜ **专业** `prepares_for` ｜ **技能** `requires`（权重 Σ=1）",
        "｜ **晋升** `advances_to` ｜ **可直学** `taught_by` 指向**免登录、免报名、无开课周期**的资源。",
        "",
        "> 2026-08-18 收紧了课程口径：中国大学MOOC / 学堂在线要登录报名、按学期开课，",
        "> 往期课程点进去只剩介绍页；慕课检索入口点开是搜索结果页。两类共 1487 个节点已下架，",
        "> 所以本表「可直学」列的数字比上一版小很多 —— 少的那些本来就学不了。",
        "",
        "| 岗位（搜这个） | 方向 | 职级 | 岗位族 | 行业 | 专业 | 技能 | 晋升 | 可直学 |",
        "|---|---|:--:|---|:--:|:--:|:--:|:--:|:--:|",
    ]
    for r in rows:
        lv = f"L{r['lv']}" if r["lv"] else "—"
        lines.append(
            "| {name} | {l2} | {lv} | {fam} | {i} | {m} | {s} | {a} | {cr} |".format(
                name=r["name"], l2=r["l2"] or "—", lv=lv, fam=r["family"] or "—",
                i=r["ind_n"] or "—", m=r["maj_n"] or "—", s=r["sk_n"] or "—",
                a=r["adv_n"] or "—", cr=r["crs_real"] or "—",
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
