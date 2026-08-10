"""节点关联计数：联读批量聚合（非物化表）。"""
from __future__ import annotations

from typing import Any

from backend.kg.pg_store.client import connect
from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL

_PUB_N = "COALESCE(n.status, 'published') = 'published'"
_PUB_E = "COALESCE(e.status, 'published') = 'published'"
_PUB_O = "COALESCE(o.status, 'published') = 'published'"
_PUB_M = "COALESCE(m.status, 'published') = 'published'"
_PUB_S = "COALESCE(s.status, 'published') = 'published'"
_PUB_I = "COALESCE(i.status, 'published') = 'published'"


def _empty_counts() -> dict[str, int]:
    return {
        "major": 0,
        "occupation": 0,
        "skill": 0,
        "industry": 0,
        "course": 0,
        "level": 0,
    }


def counts_for_industries(ids: list[str]) -> dict[str, dict[str, int]]:
    """industry: major（直连 belongs_to）、occupation（直连 belongs_to）。"""
    out = {i: _empty_counts() for i in ids}
    if not ids:
        return out
    with connect() as conn:
        # occupation -belongs_to→ industry
        rows = conn.execute(
            f"""
            SELECT e.dst_id AS id, count(DISTINCT e.src_id) AS c
            FROM kg_edge e
            JOIN kg_node o ON o.id = e.src_id AND o.type = 'occupation' AND {_PUB_O}
            WHERE e.rel_type = 'belongs_to'
              AND e.dst_id = ANY(%s)
              AND {_PUB_E}
            GROUP BY e.dst_id
            """,
            (ids,),
        ).fetchall()
        for r in rows:
            if r["id"] in out:
                out[r["id"]]["occupation"] = int(r["c"])

        # major -belongs_to→ industry（当前数据可能为 0）
        rows = conn.execute(
            f"""
            SELECT e.dst_id AS id, count(DISTINCT e.src_id) AS c
            FROM kg_edge e
            JOIN kg_node m ON m.id = e.src_id AND m.type = 'major' AND {_PUB_M}
            WHERE e.rel_type = 'belongs_to'
              AND e.dst_id = ANY(%s)
              AND {_PUB_E}
            GROUP BY e.dst_id
            """,
            (ids,),
        ).fetchall()
        for r in rows:
            if r["id"] in out:
                out[r["id"]]["major"] = int(r["c"])
    return out


def counts_for_majors(ids: list[str]) -> dict[str, dict[str, int]]:
    """major: occupation(prepares_for), skill(两跳 distinct skill_key)。"""
    out = {i: _empty_counts() for i in ids}
    if not ids:
        return out
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT e.src_id AS id, count(DISTINCT e.dst_id) AS c
            FROM kg_edge e
            JOIN kg_node o ON o.id = e.dst_id AND o.type = 'occupation' AND {_PUB_O}
            WHERE e.rel_type = 'prepares_for'
              AND e.src_id = ANY(%s)
              AND {_PUB_E}
            GROUP BY e.src_id
            """,
            (ids,),
        ).fetchall()
        for r in rows:
            if r["id"] in out:
                out[r["id"]]["occupation"] = int(r["c"])

        # major → occ → skill_level，按 skill_key 去重
        rows = conn.execute(
            f"""
            SELECT pf.src_id AS id, count(DISTINCT ({SKILL_KEY_SQL})) AS c
            FROM kg_edge pf
            JOIN kg_node o ON o.id = pf.dst_id AND o.type = 'occupation' AND {_PUB_O}
            JOIN kg_edge rq ON rq.src_id = o.id AND rq.rel_type = 'requires'
              AND COALESCE(rq.status, 'published') = 'published'
            JOIN kg_node n ON n.id = rq.dst_id AND n.type = 'skill_level' AND {_PUB_N}
            WHERE pf.rel_type = 'prepares_for'
              AND pf.src_id = ANY(%s)
              AND COALESCE(pf.status, 'published') = 'published'
            GROUP BY pf.src_id
            """,
            (ids,),
        ).fetchall()
        for r in rows:
            if r["id"] in out:
                out[r["id"]]["skill"] = int(r["c"])
    return out


def counts_for_occupations(ids: list[str]) -> dict[str, dict[str, int]]:
    """occupation: skill(distinct key), major(逆 prepares_for), industry 数。"""
    out = {i: _empty_counts() for i in ids}
    if not ids:
        return out
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT e.src_id AS id, count(DISTINCT ({SKILL_KEY_SQL})) AS c
            FROM kg_edge e
            JOIN kg_node n ON n.id = e.dst_id AND n.type = 'skill_level' AND {_PUB_N}
            WHERE e.rel_type = 'requires'
              AND e.src_id = ANY(%s)
              AND {_PUB_E}
            GROUP BY e.src_id
            """,
            (ids,),
        ).fetchall()
        for r in rows:
            if r["id"] in out:
                out[r["id"]]["skill"] = int(r["c"])

        rows = conn.execute(
            f"""
            SELECT e.dst_id AS id, count(DISTINCT e.src_id) AS c
            FROM kg_edge e
            JOIN kg_node m ON m.id = e.src_id AND m.type = 'major' AND {_PUB_M}
            WHERE e.rel_type = 'prepares_for'
              AND e.dst_id = ANY(%s)
              AND {_PUB_E}
            GROUP BY e.dst_id
            """,
            (ids,),
        ).fetchall()
        for r in rows:
            if r["id"] in out:
                out[r["id"]]["major"] = int(r["c"])

        # 行业数：直连 + 经专业两跳（去重）
        rows = conn.execute(
            f"""
            SELECT occ_id AS id, count(DISTINCT industry_id) AS c FROM (
              SELECT e.src_id AS occ_id, e.dst_id AS industry_id
              FROM kg_edge e
              JOIN kg_node i ON i.id = e.dst_id AND i.type = 'industry'
                AND COALESCE(i.status, 'published') NOT IN ('archived', 'disabled')
              WHERE e.rel_type = 'belongs_to'
                AND e.src_id = ANY(%s)
                AND COALESCE(e.status, 'published') NOT IN ('archived')
              UNION
              SELECT pf.dst_id AS occ_id, e.dst_id AS industry_id
              FROM kg_edge pf
              JOIN kg_node m ON m.id = pf.src_id AND m.type = 'major'
                AND COALESCE(m.status, 'published') NOT IN ('archived', 'disabled')
              JOIN kg_edge e ON e.src_id = m.id AND e.rel_type = 'belongs_to'
                AND COALESCE(e.status, 'published') NOT IN ('archived')
              JOIN kg_node i ON i.id = e.dst_id AND i.type = 'industry'
                AND COALESCE(i.status, 'published') NOT IN ('archived', 'disabled')
              WHERE pf.rel_type = 'prepares_for'
                AND pf.dst_id = ANY(%s)
                AND COALESCE(pf.status, 'published') NOT IN ('archived')
            ) t
            GROUP BY occ_id
            """,
            (ids, ids),
        ).fetchall()
        for r in rows:
            if r["id"] in out:
                out[r["id"]]["industry"] = int(r["c"])
    return out


def industries_for_occupations(
    ids: list[str],
    *,
    published_only: bool = False,
) -> dict[str, list[dict[str, str | None]]]:
    """
    occupation → industries[]（稳定序：name, id）。

    来源：
    1) 直连 occupation -belongs_to→ industry
    2) 经专业 occupation ←prepares_for- major -belongs_to→ industry

    管理列表默认 published_only=False，避免岗位/专业为 draft 时看不到所属行业名。
    """
    out: dict[str, list[dict[str, str | None]]] = {i: [] for i in ids}
    if not ids:
        return out
    if published_only:
        i_ok = _PUB_I
        m_ok = _PUB_M
        e_ok = _PUB_E
    else:
        i_ok = "COALESCE(i.status, 'published') NOT IN ('archived', 'disabled')"
        m_ok = "COALESCE(m.status, 'published') NOT IN ('archived', 'disabled')"
        e_ok = "COALESCE(e.status, 'published') NOT IN ('archived')"

    with connect() as conn:
        # 直连
        rows = conn.execute(
            f"""
            SELECT e.src_id AS occ_id, i.id, i.name
            FROM kg_edge e
            JOIN kg_node i ON i.id = e.dst_id AND i.type = 'industry' AND {i_ok}
            WHERE e.rel_type = 'belongs_to'
              AND e.src_id = ANY(%s)
              AND {e_ok}
            ORDER BY e.src_id, i.name, i.id
            """,
            (ids,),
        ).fetchall()
        # 经专业两跳
        rows2 = conn.execute(
            f"""
            SELECT pf.dst_id AS occ_id, i.id, i.name
            FROM kg_edge pf
            JOIN kg_node m ON m.id = pf.src_id AND m.type = 'major' AND {m_ok}
            JOIN kg_edge e ON e.src_id = m.id AND e.rel_type = 'belongs_to' AND {e_ok}
            JOIN kg_node i ON i.id = e.dst_id AND i.type = 'industry' AND {i_ok}
            WHERE pf.rel_type = 'prepares_for'
              AND pf.dst_id = ANY(%s)
              AND COALESCE(pf.status, 'published') NOT IN ('archived')
            ORDER BY pf.dst_id, i.name, i.id
            """,
            (ids,),
        ).fetchall()

    seen: dict[str, set[str]] = {i: set() for i in ids}
    for r in list(rows) + list(rows2):
        oid = r["occ_id"]
        if oid not in out:
            continue
        iid = r["id"]
        if iid in seen[oid]:
            continue
        seen[oid].add(iid)
        out[oid].append({"id": iid, "name": r["name"]})
    # 按 name 稳定排序
    for oid in out:
        out[oid].sort(key=lambda x: ((x.get("name") or ""), x.get("id") or ""))
    return out


def attach_counts_by_type(
    nodes: list[dict[str, Any]],
    *,
    node_type: str | None = None,
) -> list[dict[str, Any]]:
    """就地/拷贝附加 counts（及岗位 industries）。"""
    if not nodes:
        return nodes
    ntype = node_type or nodes[0].get("type")
    ids = [n["id"] for n in nodes if n.get("id")]
    cmap: dict[str, dict[str, int]] = {}
    ind_map: dict[str, list[dict[str, str | None]]] = {}
    if ntype == "industry":
        cmap = counts_for_industries(ids)
    elif ntype == "major":
        cmap = counts_for_majors(ids)
    elif ntype == "occupation":
        cmap = counts_for_occupations(ids)
        ind_map = industries_for_occupations(ids)
    else:
        for n in nodes:
            n.setdefault("counts", _empty_counts())
        return nodes

    for n in nodes:
        nid = n.get("id")
        n["counts"] = cmap.get(nid, _empty_counts()) if nid else _empty_counts()
        if ntype == "occupation" and nid:
            inds = ind_map.get(nid) or []
            n["industries"] = inds
            if inds:
                n["industry_id"] = inds[0].get("id")
                n["industry_name"] = inds[0].get("name")
            else:
                n["industry_id"] = None
                n["industry_name"] = None
    return nodes


def counts_for_node(node_id: str, node_type: str | None = None) -> dict[str, Any]:
    """单节点 counts（+ 岗位 industries）。"""
    from backend.kg.pg_store.query import get_node

    n = get_node(node_id)
    if not n:
        return {"counts": _empty_counts()}
    t = node_type or n.get("type")
    attach_counts_by_type([n], node_type=t)
    return {
        "counts": n.get("counts") or _empty_counts(),
        "industries": n.get("industries"),
        "industry_id": n.get("industry_id"),
        "industry_name": n.get("industry_name"),
    }
