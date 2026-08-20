"""行业三层关联图：行业 → 专业 → 岗位（只到岗位，技能是二级信息，另有接口）。

服务于「先选行业 → 看关联图 → 点岗位看技能图谱」的主交互。
与通用 explore/expand 的区别：按层组织、由服务端截断并告知截断量，
前端不需要自己判层，也不会被超大行业撑爆（专业数中位数 12，但最大 292）。
"""
from __future__ import annotations

from typing import Any

from backend.kg.pg_store.client import connect
from backend.kg.pg_store.config import DEFAULT_REGION, edge_published
from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL, SKILL_NAME_SQL
from backend.kg.pg_store.skill_taxonomy import (
    FALLBACK_CODE,
    category_rank,
    name_of,
    topo_depth,
)

_PUB_N = "COALESCE(n.status,'published')='published'"

# 技能构成的可见性谓词与 skill_aggregate 同源（本文件是前台能力全景 → public）
from backend.kg.pg_store.skill_aggregate import _composition_pred as _cp  # noqa: E402

_COMP_E, _COMP_N = _cp("public", edge="e", node="n")

# 边可见性：归档/草稿边不对外返回（各别名一份，供 f-string SQL 内插）
EP_E = edge_published("e")
EP_BE = edge_published("be")
EP_PE = edge_published("pe")
EP_RE = edge_published("re")


def search_industries(
    q: str | None = None, region: str | None = None, limit: int = 30
) -> list[dict[str, Any]]:
    """行业模糊搜索（平铺，不分大类/子行业），带专业与岗位数供下拉展示。"""
    reg = region or DEFAULT_REGION
    kw = f"%{(q or '').strip()}%"
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT n.id, n.name, n.region,
                   count(DISTINCT m.id) AS major_count,
                   count(DISTINCT o.id) AS occupation_count
            FROM kg_node n
            LEFT JOIN kg_edge be ON be.dst_id = n.id AND be.rel_type = 'belongs_to'
                 AND {EP_BE}
            LEFT JOIN kg_node m ON m.id = be.src_id AND m.type = 'major'
                 AND COALESCE(m.status,'published') = 'published'
            LEFT JOIN kg_edge pe ON pe.src_id = m.id AND pe.rel_type = 'prepares_for'
                 AND {EP_PE}
            LEFT JOIN kg_node o ON o.id = pe.dst_id AND o.type = 'occupation'
                 AND COALESCE(o.status,'published') = 'published'
            WHERE n.type = 'industry' AND {_PUB_N}
              AND (%s::text IS NULL OR n.region = %s)
              AND (%s = '%%%%' OR n.name ILIKE %s)
            GROUP BY n.id, n.name, n.region
            ORDER BY (n.name ILIKE %s) DESC, count(DISTINCT m.id) DESC, n.name
            LIMIT %s
            """,
            (reg, reg, kw, kw, f"{(q or '').strip()}%", limit),
        ).fetchall()
    return [dict(r) for r in rows]


def industry_graph(
    industry_id: str | None = None,
    industry: str | None = None,
    region: str | None = None,
    limit_majors: int = 20,
    limit_occupations_per_major: int = 8,
    layout: str = "layered",
) -> dict[str, Any]:
    """行业 → 专业 → 岗位 三层图。layout=matrix 时附带热力图矩阵。"""
    reg = region or DEFAULT_REGION
    with connect() as conn:
        if industry_id:
            root = conn.execute(
                f"SELECT * FROM kg_node n WHERE n.id=%s AND n.type='industry' AND {_PUB_N}",
                (industry_id,),
            ).fetchone()
        elif industry:
            root = conn.execute(
                f"""SELECT * FROM kg_node n WHERE n.type='industry' AND {_PUB_N}
                    AND (%s::text IS NULL OR n.region=%s) AND n.name ILIKE %s
                    ORDER BY (lower(n.name)=lower(%s)) DESC, length(n.name) LIMIT 1""",
                (reg, reg, f"%{industry}%", industry),
            ).fetchone()
        else:
            root = None
        if not root:
            return {"industry": None, "layers": {"majors": [], "occupations": []},
                    "links": [], "meta": {"matched": 0}}

        iid = root["id"]
        # 专业层：按对口岗位数倒序截断，保证截断掉的是弱关联专业
        majors = conn.execute(
            f"""
            SELECT n.id, n.name, n.attrs, n.region,
                   count(DISTINCT o.id) AS occupation_count
            FROM kg_edge be
            JOIN kg_node n ON n.id = be.src_id AND n.type='major' AND {_PUB_N}
            LEFT JOIN kg_edge pe ON pe.src_id = n.id AND pe.rel_type='prepares_for'
                 AND {EP_PE}
            LEFT JOIN kg_node o ON o.id = pe.dst_id AND o.type='occupation'
                 AND COALESCE(o.status,'published')='published'
            WHERE be.dst_id = %s AND be.rel_type = 'belongs_to' AND {EP_BE}
            GROUP BY n.id, n.name, n.attrs, n.region
            ORDER BY count(DISTINCT o.id) DESC, n.name
            LIMIT %s
            """,
            (iid, limit_majors),
        ).fetchall()
        major_total = conn.execute(
            f"""SELECT count(*) c FROM kg_edge be JOIN kg_node n ON n.id=be.src_id
                AND n.type='major' AND {_PUB_N}
                WHERE be.dst_id=%s AND be.rel_type='belongs_to' AND {EP_BE}""",
            (iid,),
        ).fetchone()["c"]

        major_ids = [m["id"] for m in majors]
        links: list[dict[str, Any]] = [
            {"from": iid, "to": m["id"], "rel": "belongs_to"} for m in majors
        ]
        occs: dict[str, dict[str, Any]] = {}
        occ_total = 0
        if major_ids:
            occ_total = conn.execute(
                f"""SELECT count(DISTINCT o.id) c FROM kg_edge pe
                    JOIN kg_node o ON o.id=pe.dst_id AND o.type='occupation'
                    AND COALESCE(o.status,'published')='published'
                    WHERE pe.src_id = ANY(%s) AND pe.rel_type='prepares_for' AND {EP_PE}""",
                (major_ids,),
            ).fetchone()["c"]
            # 每个专业各取前 N 个岗位（按技能数倒序，技能多的更有代表性）
            rows = conn.execute(
                f"""
                SELECT * FROM (
                  SELECT pe.src_id AS major_id, n.id, n.name, n.level, n.region,
                         count(DISTINCT re.dst_id) AS skill_count,
                         row_number() OVER (
                           PARTITION BY pe.src_id
                           ORDER BY count(DISTINCT re.dst_id) DESC, n.name
                         ) AS rn
                  FROM kg_edge pe
                  JOIN kg_node n ON n.id = pe.dst_id AND n.type='occupation' AND {_PUB_N}
                  LEFT JOIN kg_edge re ON re.src_id = n.id AND re.rel_type='requires'
                 AND {EP_RE}
                  WHERE pe.src_id = ANY(%s) AND pe.rel_type='prepares_for' AND {EP_PE}
                  GROUP BY pe.src_id, n.id, n.name, n.level, n.region
                ) t WHERE t.rn <= %s
                """,
                (major_ids, limit_occupations_per_major),
            ).fetchall()
            for r in rows:
                o = occs.setdefault(
                    r["id"],
                    {"id": r["id"], "name": r["name"], "level": r["level"],
                     "region": r["region"], "skill_count": int(r["skill_count"] or 0),
                     "major_ids": []},
                )
                o["major_ids"].append(r["major_id"])
                links.append({"from": r["major_id"], "to": r["id"], "rel": "prepares_for"})

        occ_ids = list(occs)
        # 晋升链：只保留两端都在当前画布内的边
        progressions = []
        if occ_ids:
            for r in conn.execute(
                """
                SELECT e.src_id, e.dst_id, e.confidence, e.evidence,
                       s.name AS from_name, d.name AS to_name
                FROM kg_edge e
                JOIN kg_node s ON s.id = e.src_id AND NOT s.is_draft
                JOIN kg_node d ON d.id = e.dst_id AND NOT d.is_draft
                WHERE e.rel_type='advances_to'
                  AND COALESCE(e.status,'published')='published'
                  AND e.src_id = ANY(%s) AND e.dst_id = ANY(%s)
                """,
                (occ_ids, occ_ids),
            ).fetchall():
                progressions.append({
                    "from": r["src_id"], "to": r["dst_id"],
                    "from_name": r["from_name"], "to_name": r["to_name"],
                    "rel_type": "advances_to", "confidence": r["confidence"],
                    "evidence": r["evidence"],
                })

        matrix = None
        if layout == "matrix" and major_ids and occ_ids:
            matrix = _build_matrix(conn, major_ids, occ_ids)

    out = {
        "industry": {"id": iid, "name": root["name"], "region": root["region"]},
        "layers": {
            "majors": [
                {"id": m["id"], "name": m["name"],
                 "occupation_count": int(m["occupation_count"] or 0)}
                for m in majors
            ],
            "occupations": list(occs.values()),
        },
        "links": links,
        "progressions": progressions,
        "meta": {
            "matched": 1,
            "major_total": major_total, "major_shown": len(majors),
            "occupation_total": occ_total, "occupation_shown": len(occs),
            "truncated": major_total > len(majors) or occ_total > len(occs),
            "layout": layout,
            "region": reg,
        },
    }
    if matrix is not None:
        out["matrix"] = matrix
    return out


def occupation_skills_graph(
    occupation_id: str, region: str | None = None, limit: int = 200
) -> dict[str, Any]:
    """岗位技能图谱：技能按 category 分区 + 区内前置关系（点击岗位后的二级视图）。"""
    reg = region or DEFAULT_REGION
    with connect() as conn:
        occ = conn.execute(
            f"SELECT * FROM kg_node n WHERE n.id=%s AND n.type='occupation' AND {_PUB_N}",
            (occupation_id,),
        ).fetchone()
        if not occ:
            return {"occupation": None, "categories": [], "prereqs": [], "meta": {"matched": 0}}

        rows = conn.execute(
            f"""
            SELECT ({SKILL_KEY_SQL}) AS skill_key,
                   min({SKILL_NAME_SQL}) AS skill_name, n.category,
                   array_agg(DISTINCT (n.attrs::json->>'level_code')) AS levels
            FROM kg_edge e JOIN kg_node n ON n.id = e.dst_id
            WHERE e.src_id=%s AND e.rel_type='requires' AND {EP_E}
              AND n.type='skill_level' AND {_PUB_N}
            -- **按表达式分组，不用位置序号**：原来写 `GROUP BY 1, 2`，
            -- 在 SELECT 第 2 位插一列（skill_name）之后，2 就指到聚合函数上，
            -- PG 直接报 "aggregate functions are not allowed in GROUP BY"。
            -- 位置序号让 SELECT 列表的顺序变成隐式契约，加列就炸。
            GROUP BY ({SKILL_KEY_SQL}), n.category
            ORDER BY ({SKILL_KEY_SQL})
            LIMIT %s
            """,
            (occupation_id, limit),
        ).fetchall()

        groups: dict[str, list[dict[str, Any]]] = {}
        keys: list[str] = []
        cat_of: dict[str, str] = {}
        for r in rows:
            k = r["skill_key"]
            keys.append(k)
            # 分区 key 一律用 **code**。这里曾写 `or UNCATEGORIZED`，而那个常量
            # 是中文展示名 —— 结果有分类的分区 key 是 TECH、没分类的是「待归类」，
            # 同一个字段混着两种取值，下面按 key 取计数也就永远取不到。
            cat = r["category"] or FALLBACK_CODE
            cat_of[k] = cat
            groups.setdefault(cat, []).append(
                {
                    "skill_key": k,
                    # 展示名：key 是 SKxxxxxxxxxx，图上每个技能节点的标签用这个。
                    # 只给 `skill_name` —— 与 `name` 完全重复，两个字段装同一个值
                    # 只会让前端每次都要判「用哪个」，而判错了不报错、只是显示不对
                    "skill_name": r["skill_name"] or k,
                    "levels": sorted([x for x in (r["levels"] or []) if x]),
                }
            )

        prereqs = []
        if keys:
            # 只保留两端都在本岗位技能集内的前置边，避免画出岗位外的孤立箭头
            for p in conn.execute(
                """
                SELECT skill_key, prereq_skill_key, confidence, evidence
                FROM kg_skill_prereq
                WHERE region=%s AND skill_key = ANY(%s) AND prereq_skill_key = ANY(%s)
                """,
                (reg, keys, keys),
            ).fetchall():
                prereqs.append(
                    {
                        "from": p["prereq_skill_key"],
                        "to": p["skill_key"],
                        "confidence": p["confidence"],
                        "evidence": p["evidence"],
                    }
                )

    # 分区按「职业功能推进顺序」排，不是按技能数量——否则展示顺序会和箭头方向矛盾
    # （抽查 400 个岗位，按数量排时有 312 个的分区顺序与学习顺序不一致）。
    depth = topo_depth(keys, [(p["from"], p["to"]) for p in prereqs])
    for skills in groups.values():
        for s in skills:
            s["depth"] = depth.get(s["skill_key"], 0)
        # 区内按层深排，同层按名称，供前端纵向分层
        skills.sort(key=lambda s: (s["depth"], s["skill_key"]))
    # key 是 code，name 是展示名 —— 前端不要按 code 硬编码中文
    categories = [
        {"key": c, "name": name_of(c), "rank": category_rank(c), "skills": s}
        for c, s in sorted(groups.items(), key=lambda x: (category_rank(x[0]), x[0]))
    ]
    return {
        "occupation": {"id": occ["id"], "name": occ["name"], "level": occ["level"]},
        "categories": categories,
        "prereqs": prereqs,
        "meta": {
            "matched": 1,
            "skill_total": len(keys),
            "category_count": len(categories),
            "uncategorized": len(groups.get(FALLBACK_CODE, [])),
            "prereq_total": len(prereqs),
            "max_depth": max(depth.values(), default=0),
            "order": "categories 按学习顺序排列；skills[].depth 为前置层深(0=可直接学)",
            "region": reg,
        },
    }


def _build_matrix(conn, major_ids: list[str], occ_ids: list[str]) -> dict[str, Any]:
    """热力图矩阵：行=专业，列=岗位。

    强度 = 该岗位的技能与「同专业其他对口岗位」的重合总量（技能贴合度）。
    为什么不用「专业与岗位的共有技能」：kg_edge 里 covers（专业→技能）为 0 条，
    专业没有直接技能边，算不出交集；而贴合度只依赖 requires，数据完备，
    且能区分「该岗位是否代表这个专业的主流技能」，比 0/1 有无关联更有信息量。
    """
    rows = conn.execute(
        f"""
        WITH mo AS (
          SELECT pe.src_id AS m, pe.dst_id AS o
          FROM kg_edge pe
          WHERE pe.rel_type='prepares_for' AND {EP_PE}
            AND pe.src_id = ANY(%s) AND pe.dst_id = ANY(%s)
        ), os AS (
          -- 批量热力矩阵：形状是 major × occupation 的聚合，不是「单实体取明细」，
          -- 所以没并进 entity_skill_composition，但**共用它那份可见性谓词**
          SELECT e.src_id AS o, ({SKILL_KEY_SQL}) AS k
          FROM kg_edge e JOIN kg_node n ON n.id = e.dst_id AND {_COMP_N}
          WHERE e.rel_type='requires' AND {_COMP_E} AND n.type='skill_level'
            AND e.src_id = ANY(%s)
        ), ms AS (
          SELECT mo.m, os.k, count(DISTINCT os.o) AS cnt
          FROM mo JOIN os ON os.o = mo.o
          GROUP BY mo.m, os.k
        )
        SELECT mo.m, mo.o, COALESCE(sum(ms.cnt), 0) AS v
        FROM mo
        LEFT JOIN os ON os.o = mo.o
        LEFT JOIN ms ON ms.m = mo.m AND ms.k = os.k
        GROUP BY mo.m, mo.o
        """,
        (major_ids, occ_ids, occ_ids),
    ).fetchall()
    ri = {m: i for i, m in enumerate(major_ids)}
    ci = {o: i for i, o in enumerate(occ_ids)}
    cells = [
        {"r": ri[r["m"]], "c": ci[r["o"]], "v": int(r["v"] or 0)}
        for r in rows
        if r["m"] in ri and r["o"] in ci and int(r["v"] or 0) > 0
    ]
    return {
        "rows": major_ids,
        "cols": occ_ids,
        "cells": cells,
        "max": max((c["v"] for c in cells), default=0),
        "metric": "skill_affinity",
    }
