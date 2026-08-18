"""管理台四维详情：industry / major / occupation / skill_level。

字段依据 `docs/管理台详情接口-原型对照.md`（用浏览器抓原型渲染后 DOM 得到，非猜测）。
一个入口按 type 分派，避免前端为四类各记一个 URL。

可见性：管理台口径——published + draft + disabled，archived 不返回
（与 list_nodes 的 scope=manage 一致，见 config.node_not_archived）。
"""
from __future__ import annotations

from typing import Any

from backend.kg.pg_store.client import connect
from backend.kg.pg_store.config import attrs_level_int, edge_published, node_not_archived
from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL

_EP = edge_published("e")
_NODE_VISIBLE = node_not_archived("n")
_LEVEL_N = attrs_level_int("n")


def _base(row: dict[str, Any]) -> dict[str, Any]:
    from backend.kg.pg_store.query import _node_dict

    return _node_dict(row)


def _levels_grid(levels: list[int], required: int | None) -> list[dict[str, Any]]:
    """L1–L5 档位格：原型里每技能一行五格，高亮到要求等级。"""
    have = {int(v) for v in levels if v}
    return [
        {
            "level": i,
            "exists": i in have,
            "required": bool(required and i <= required),
        }
        for i in range(1, 6)
    ]


def node_detail(node_id: str) -> dict[str, Any]:
    """按节点类型返回管理台详情。未找到返回 {'node': None}。"""
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM kg_node n WHERE n.id = %s AND {_NODE_VISIBLE}", (node_id,)
        ).fetchone()
        if not row:
            return {"node": None, "meta": {"matched": 0}}
        node = _base(row)
        ntype = node.get("type")
        out: dict[str, Any] = {"node": node, "meta": {"matched": 1, "type": ntype}}

        if ntype == "industry":
            out.update(_industry_detail(conn, node_id))
        elif ntype == "major":
            out.update(_major_detail(conn, node_id))
        elif ntype == "occupation":
            out.update(_occupation_detail(conn, node_id))
        elif ntype == "skill_level":
            out.update(_skill_detail(conn, node))
        return out


# ── 行业 ──────────────────────────────────────────────────────
def _industry_detail(conn, iid: str) -> dict[str, Any]:
    majors = conn.execute(
        f"""
        SELECT n.id, n.name, n.status, n.attrs,
               count(DISTINCT o.id) AS occupation_count
        FROM kg_edge e
        JOIN kg_node n ON n.id = e.src_id AND n.type='major' AND {_NODE_VISIBLE}
        LEFT JOIN kg_edge pe ON pe.src_id = n.id AND pe.rel_type='prepares_for'
             AND {edge_published('pe')}
        LEFT JOIN kg_node o ON o.id = pe.dst_id AND o.type='occupation'
        WHERE e.dst_id = %s AND e.rel_type='belongs_to' AND {_EP}
        GROUP BY n.id, n.name, n.status, n.attrs
        ORDER BY count(DISTINCT o.id) DESC, n.name
        """,
        (iid,),
    ).fetchall()
    # 直连岗位（occupation -belongs_to-> industry），与经专业两跳的岗位区分开
    occs = conn.execute(
        f"""
        SELECT n.id, n.name, n.status, n.level
        FROM kg_edge e
        JOIN kg_node n ON n.id = e.src_id AND n.type='occupation' AND {_NODE_VISIBLE}
        WHERE e.dst_id = %s AND e.rel_type='belongs_to' AND {_EP}
        ORDER BY n.name
        """,
        (iid,),
    ).fetchall()
    return {
        "majors": [
            {
                "id": m["id"],
                "name": m["name"],
                "status": m["status"],
                "occupation_count": int(m["occupation_count"] or 0),
            }
            for m in majors
        ],
        "occupations": [dict(o) for o in occs],
        "counts": {"major": len(majors), "occupation_direct": len(occs)},
    }


# ── 专业 ──────────────────────────────────────────────────────
def _major_detail(conn, mid: str) -> dict[str, Any]:
    industries = conn.execute(
        f"""SELECT n.id, n.name FROM kg_edge e
            JOIN kg_node n ON n.id=e.dst_id AND n.type='industry' AND {_NODE_VISIBLE}
            WHERE e.src_id=%s AND e.rel_type='belongs_to' AND {_EP}""",
        (mid,),
    ).fetchall()

    # 关联岗位：原型显示「职级 · 薪资 · N 项技能 · 权重和」
    occs = conn.execute(
        f"""
        SELECT n.id, n.name, n.status, n.level, n.attrs,
               count(DISTINCT re.dst_id) AS skill_count,
               COALESCE(sum(re.weight), 0) AS weight_sum
        FROM kg_edge e
        JOIN kg_node n ON n.id = e.dst_id AND n.type='occupation' AND {_NODE_VISIBLE}
        LEFT JOIN kg_edge re ON re.src_id = n.id AND re.rel_type='requires'
             AND {edge_published('re')}
        WHERE e.src_id = %s AND e.rel_type='prepares_for' AND {_EP}
        GROUP BY n.id, n.name, n.status, n.level, n.attrs
        ORDER BY n.level NULLS LAST, n.name
        """,
        (mid,),
    ).fetchall()

    # 专业直连技能（covers, E4）：需求变更后专业以此管理技能，
    # 不再展示「经岗位聚合」的技能——后者是间接推导，且无法被运营直接维护。
    direct = conn.execute(
        f"""
        SELECT ({SKILL_KEY_SQL}) AS skill_key, n.category,
               {_LEVEL_N} AS level
        FROM kg_edge e
        JOIN kg_node n ON n.id = e.dst_id AND n.type='skill_level' AND {_NODE_VISIBLE}
        WHERE e.src_id = %s AND e.rel_type='covers' AND {_EP}
        ORDER BY 1
        """,
        (mid,),
    ).fetchall()

    direct_skills = [
        {
            "skill_key": r["skill_key"],
            "category": r["category"],
            "selected_level": r["level"],
        }
        for r in direct
    ]

    occ_ids = [o["id"] for o in occs]
    # 仍保留经岗位聚合的结果供图谱/统计用（前端详情不再展示）
    agg: dict[str, dict[str, Any]] = {}
    if occ_ids:
        rows = conn.execute(
            f"""
            SELECT e.src_id AS occ_id, o.name AS occ_name,
                   ({SKILL_KEY_SQL}) AS skill_key, n.category,
                   {_LEVEL_N} AS level
            FROM kg_edge e
            JOIN kg_node n ON n.id = e.dst_id AND n.type='skill_level' AND {_NODE_VISIBLE}
            JOIN kg_node o ON o.id = e.src_id
            WHERE e.src_id = ANY(%s) AND e.rel_type='requires' AND {_EP}
            """,
            (occ_ids,),
        ).fetchall()

        for r in rows:
            k = r["skill_key"]
            g = agg.setdefault(
                k,
                {
                    "skill_key": k,
                    "category": r["category"],
                    "_by_occ": {},
                    "required_level": None,
                },
            )
            lv = r["level"]
            # 同一技能在同一岗位下有多个等级节点（L1..L5 各一行），
            # 原型每岗位只显示一次并取该岗位的要求档 → 按岗位聚合取最高，避免
            #「砌筑工 L1、砌筑工 L5、砌筑工 L4」这种同名重复。
            cur = g["_by_occ"].get(r["occ_id"])
            if cur is None or (lv or 0) > (cur["level"] or 0):
                g["_by_occ"][r["occ_id"]] = {
                    "occupation_id": r["occ_id"],
                    "occupation_name": r["occ_name"],
                    "level": lv,
                }
            if lv and (g["required_level"] is None or lv > g["required_level"]):
                g["required_level"] = lv          # 专业内取最高要求档

    aggregated = []
    for g in agg.values():
        used = sorted(
            g.pop("_by_occ").values(), key=lambda x: (-(x["level"] or 0), x["occupation_name"])
        )
        g["used_by"] = used
        g["used_count"] = len(used)      # 按**岗位数**计，不是记录行数
        aggregated.append(g)
    aggregated.sort(key=lambda x: (-x["used_count"], x["skill_key"]))

    return {
        "industries": [dict(i) for i in industries],
        "occupations": [
            {
                "id": o["id"],
                "name": o["name"],
                "status": o["status"],
                "level": o["level"],
                "skill_count": int(o["skill_count"] or 0),
                "weight_sum": round(float(o["weight_sum"] or 0), 2),
            }
            for o in occs
        ],
        # skills = 专业直连技能（covers），详情页展示这个
        "skills": direct_skills,
        # aggregated_skills = 经岗位间接汇总，保留供统计/图谱，详情页不再展示
        "aggregated_skills": aggregated,
        "counts": {
            "occupation": len(occs),
            "skill": len(direct_skills),
            "skill_aggregated": len(aggregated),
        },
    }


# ── 岗位 ──────────────────────────────────────────────────────
def _occupation_detail(conn, oid: str) -> dict[str, Any]:
    industries = conn.execute(
        f"""SELECT n.id, n.name FROM kg_edge e
            JOIN kg_node n ON n.id=e.dst_id AND n.type='industry' AND {_NODE_VISIBLE}
            WHERE e.src_id=%s AND e.rel_type='belongs_to' AND {_EP}""",
        (oid,),
    ).fetchall()
    majors = conn.execute(
        f"""SELECT n.id, n.name FROM kg_edge e
            JOIN kg_node n ON n.id=e.src_id AND n.type='major' AND {_NODE_VISIBLE}
            WHERE e.dst_id=%s AND e.rel_type='prepares_for' AND {_EP}""",
        (oid,),
    ).fetchall()

    from backend.kg.pg_store.skill_aggregate import occupation_skill_bundles

    from backend.kg.pg_store.skill_prereq import prereq_map

    bundles = occupation_skill_bundles(oid, limit=200)
    pmap = prereq_map(conn, [b.get("skill_key") for b in bundles])

    skills = []
    wsum = 0.0
    for b in bundles:
        w = b.get("weight")
        if isinstance(w, (int, float)):
            wsum += float(w)
        skills.append(
            {
                "skill_key": b.get("skill_key"),
                "category": b.get("category"),
                "required_level": b.get("required_level"),
                "weight": w,
                "weight_pct": b.get("weight_pct"),
                "is_core": b.get("is_core"),
                # 原型：每技能显示先修，无则「无先修」
                "prereqs": pmap.get(b.get("skill_key") or "", []),
                "levels": _levels_grid(b.get("available_levels") or [], b.get("required_level")),
            }
        )
    return {
        "industries": [dict(i) for i in industries],
        "majors": [dict(m) for m in majors],
        "skills": skills,
        "weight_sum": round(wsum, 2),
        "counts": {"skill": len(skills), "major": len(majors)},
    }


# ── 技能 ──────────────────────────────────────────────────────
def _skill_detail(conn, node: dict[str, Any]) -> dict[str, Any]:
    from backend.kg.pg_store.skill_aggregate import get_skill_bundle, skill_key_from_node

    key = skill_key_from_node(node)
    bundle = {}
    try:
        bundle = get_skill_bundle(key) or {}
    except Exception:
        bundle = {}

    occs = conn.execute(
        f"""
        -- o.level 是岗位职级；required_level 是该岗位对本技能要求的产品档
        SELECT DISTINCT o.id, o.name, o.status, o.level,
               {_LEVEL_N} AS required_level
        FROM kg_node n
        JOIN kg_edge e ON e.dst_id = n.id AND e.rel_type='requires' AND {_EP}
        JOIN kg_node o ON o.id = e.src_id AND o.type='occupation'
        WHERE n.type='skill_level' AND ({SKILL_KEY_SQL}) = %s AND {_NODE_VISIBLE}
        ORDER BY o.name
        """,
        (key,),
    ).fetchall()

    prereqs = conn.execute(
        "SELECT prereq_skill_key, confidence, evidence FROM kg_skill_prereq WHERE skill_key=%s",
        (key,),
    ).fetchall()
    unlocks = conn.execute(
        "SELECT skill_key, confidence FROM kg_skill_prereq WHERE prereq_skill_key=%s",
        (key,),
    ).fetchall()

    avail = bundle.get("available_levels") or []
    return {
        "skill_key": key,
        "category": node.get("category") or bundle.get("category"),
        # 原型技能库列表有「等级完整度」列 → 这里给 L1–L5 齐全度
        "levels": _levels_grid(avail, None),
        "level_completeness": f"{len(avail)}/5",
        "level_descriptions": bundle.get("level_descriptions") or {},
        "occupations": [dict(o) for o in occs],
        "prereqs": [dict(p) for p in prereqs],
        "unlocks": [dict(u) for u in unlocks],
        "counts": {"occupation": len(occs), "prereq": len(prereqs), "unlock": len(unlocks)},
    }
