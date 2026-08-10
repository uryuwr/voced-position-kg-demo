"""PostgreSQL graph queries for API (CN-first, same response shape as former Neo4j)."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import connect
from backend.kg.pg_store.config import DEFAULT_REGION, edge_published
from backend.kg.pg_store.migrate import stats as pg_stats
from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL

_LEVEL_SHORT = {
    "ug_bachelor": "本",
    "voc_bachelor": "职本",
    "voc_associate": "高专",
    "voc_secondary": "中职",
}

_TYPE_RANK = {
    "industry": 0,
    "major": 1,
    "occupation": 2,
    "skill_level": 3,
    "course": 4,
    "credential": 5,
}


def _maybe_json(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v


def _major_display_name(name: str | None, attrs: Any, source_id: str | None = None) -> str:
    name = (name or "").strip()
    a = attrs if isinstance(attrs, dict) else {}
    level = a.get("level") or ""
    level_zh = a.get("level_zh") or ""
    code = a.get("code") or ""
    if not code and source_id and ":" in str(source_id):
        code = str(source_id).split(":")[-1]
    short = _LEVEL_SHORT.get(level, "")
    if not short and level_zh:
        if "普通本科" in level_zh:
            short = "本"
        elif "职业本科" in level_zh or "高职本科" in level_zh:
            short = "职本"
        elif "专科" in level_zh:
            short = "高专"
        elif "中等" in level_zh:
            short = "中职"
    parts = [p for p in (short, name, code) if p]
    return " · ".join(parts) if parts else name


def _node_dict(row: dict[str, Any]) -> dict[str, Any]:
    attrs = _maybe_json(row.get("attrs"))
    ntype = row.get("type")
    name = row.get("name")
    source_id = row.get("source_id")
    if ntype == "major":
        display_name = _major_display_name(name, attrs, source_id)
    else:
        display_name = name
    out = {
        "id": row.get("id"),
        "labels": [ntype] if ntype else [],
        "region": row.get("region"),
        "type": ntype,
        "name": name,
        "display_name": display_name,
        "name_en": row.get("name_en"),
        "name_zh": row.get("name_zh"),
        "description": row.get("description"),
        "source_system": row.get("source_system"),
        "source_id": source_id,
        "source_url": row.get("source_url"),
        "confidence": row.get("confidence"),
        "attrs": attrs,
    }
    if row.get("status") is not None:
        out["status"] = row.get("status")
    if row.get("updated_by"):
        out["updated_by"] = row.get("updated_by")
        out["updated_by_name"] = row.get("updated_by_name")
    # 布局元数据（与前端 GraphNode.order / child_count 对齐）
    so = row.get("sort_order")
    if so is not None:
        try:
            out["order"] = int(so)
            out["sort_order"] = int(so)
        except (TypeError, ValueError):
            pass
    if row.get("child_count") is not None:
        try:
            out["child_count"] = int(row.get("child_count") or 0)
        except (TypeError, ValueError):
            out["child_count"] = 0
    else:
        out["child_count"] = 0
    return out


def _rel_dict(row: dict[str, Any]) -> dict[str, Any]:
    rel = row.get("rel_type") or ""
    return {
        "id": row.get("id"),
        "rel_type": rel,
        "neo4j_type": rel.upper() if rel else "",
        "src_id": row.get("src_id"),
        "dst_id": row.get("dst_id"),
        "weight": row.get("weight"),
        "confidence": row.get("confidence"),
        "source_url": row.get("source_url"),
        "evidence": row.get("evidence"),
    }


def _default_region(region: str | None) -> str | None:
    """Empty → DEFAULT_REGION (CN); 'all'/'*' → no filter."""
    if region is None or str(region).strip() == "":
        return DEFAULT_REGION
    r = str(region).strip()
    if r.lower() in ("all", "*", "any"):
        return None
    return r


_PUBLISHED_SQL = "COALESCE(status, 'published') = 'published'"
# 边可见性：归档/草稿边不对外返回。节点过滤挡不住两端都正常的边（如 parent_of）。
_EDGE_PUB = edge_published("e")
_EDGE_PUB_BARE = "COALESCE(status, 'published') = 'published'"


def search_nodes(
    q: str,
    limit: int = 20,
    region: str | None = None,
    node_type: str | None = None,
) -> list[dict]:
    """图检索 / 联想：仅已发布节点（不扩邻域）。"""
    reg = _default_region(region)
    ntype = (node_type or "").strip().lower() or None
    if ntype in ("", "all", "*"):
        ntype = None
    sql = f"""
    SELECT *
    FROM kg_node n
    WHERE lower(n.name) LIKE lower(%s)
      AND (%s::text IS NULL OR n.region = %s)
      AND (%s::text IS NULL OR n.type = %s)
      AND {_PUBLISHED_SQL}
    ORDER BY
      CASE n.type
        WHEN 'industry' THEN 0
        WHEN 'major' THEN 1
        WHEN 'occupation' THEN 2
        WHEN 'skill_level' THEN 3
        WHEN 'course' THEN 4
        WHEN 'credential' THEN 5
        ELSE 6
      END,
      CASE WHEN lower(n.name) LIKE lower(%s) THEN 0 ELSE 1 END,
      n.name
    LIMIT %s
    """
    like = f"%{q}%"
    prefix = f"{q}%"
    with connect() as conn:
        rows = conn.execute(
            sql, (like, reg, reg, ntype, ntype, prefix, limit)
        ).fetchall()
        return [_node_dict(r) for r in rows]


def expand_neighbors(
    node_id: str,
    *,
    limit: int = 25,
    rel_types: list[str] | None = None,
    direction: str = "both",
    region: str | None = None,
) -> dict[str, Any]:
    """
    1 跳邻居展开（业界常规 expand）。
    direction: both | out | in
    """
    limit = max(1, min(int(limit), 200))
    direction = (direction or "both").lower()
    if direction not in ("both", "out", "in"):
        direction = "both"
    reg = _default_region(region)

    with connect() as conn:
        seed = conn.execute(
            f"SELECT * FROM kg_node WHERE id = %s AND {_PUBLISHED_SQL}",
            (node_id,),
        ).fetchone()
        if not seed:
            return {
                "roots": [],
                "nodes": [],
                "edges": [],
                "paths": [],
                "meta": {
                    "matched": 0,
                    "message": "node not found or not published",
                    "depth": 1,
                    "engine": "postgresql",
                },
            }
        if reg is not None and seed.get("region") and seed["region"] != reg:
            # 仍允许展开，仅记 meta
            pass

        if direction == "out":
            edge_sql = """
            SELECT e.* FROM kg_edge e
            WHERE e.src_id = %s
              AND COALESCE(e.status, 'published') = 'published'
              AND (%s::text[] IS NULL OR e.rel_type = ANY(%s))
            ORDER BY e.weight DESC NULLS LAST, e.id
            LIMIT %s
            """
            erows = conn.execute(
                edge_sql, (node_id, rel_types, rel_types, limit)
            ).fetchall()
        elif direction == "in":
            edge_sql = """
            SELECT e.* FROM kg_edge e
            WHERE e.dst_id = %s
              AND COALESCE(e.status, 'published') = 'published'
              AND (%s::text[] IS NULL OR e.rel_type = ANY(%s))
            ORDER BY e.weight DESC NULLS LAST, e.id
            LIMIT %s
            """
            erows = conn.execute(
                edge_sql, (node_id, rel_types, rel_types, limit)
            ).fetchall()
        else:
            edge_sql = """
            SELECT e.* FROM kg_edge e
            WHERE (e.src_id = %s OR e.dst_id = %s)
              AND COALESCE(e.status, 'published') = 'published'
              AND (%s::text[] IS NULL OR e.rel_type = ANY(%s))
            ORDER BY e.weight DESC NULLS LAST, e.id
            LIMIT %s
            """
            erows = conn.execute(
                edge_sql, (node_id, node_id, rel_types, rel_types, limit)
            ).fetchall()

        neighbor_ids: list[str] = []
        edges: list[dict] = []
        for er in erows:
            rd = _rel_dict(er)
            edges.append(rd)
            other = er["dst_id"] if er["src_id"] == node_id else er["src_id"]
            if other != node_id and other not in neighbor_ids:
                neighbor_ids.append(other)

        nodes_map: dict[str, dict] = {seed["id"]: _node_dict(seed)}
        if neighbor_ids:
            nrows = conn.execute(
                f"""
                SELECT * FROM kg_node
                WHERE id = ANY(%s) AND {_PUBLISHED_SQL}
                """,
                (neighbor_ids,),
            ).fetchall()
            for nr in nrows:
                nodes_map[nr["id"]] = _node_dict(nr)

        # 边两端都在 nodes 才保留
        ok_ids = set(nodes_map)
        edges = [
            e
            for e in edges
            if e.get("src_id") in ok_ids and e.get("dst_id") in ok_ids
        ]
        root = nodes_map[seed["id"]]
        nodes = list(nodes_map.values())
        truncated = len(erows) >= limit

    return {
        "roots": [root],
        "nodes": nodes,
        "edges": edges,
        "paths": [],
        "meta": {
            "matched": 1,
            "depth": 1,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "neighbor_count": max(0, len(nodes) - 1),
            "limit": limit,
            "direction": direction,
            "rel_types": rel_types,
            "truncated": truncated,
            "engine": "postgresql",
            "mode": "expand_1hop",
        },
    }


def graph_by_industry(
    industry_id: str | None = None,
    *,
    q: str | None = None,
    region: str | None = None,
    max_nodes: int = 500,
    include_skills: bool = True,
    include_direct_occupations: bool = True,
) -> dict[str, Any]:
    """
    行业闭包子图（非无向 BFS）：
      industry
        ←belongs_to— major
            —prepares_for→ occupation
                —requires→ skill_level
        ←belongs_to— occupation（可选直连岗）

    只保留闭包内节点之间的上述边，不串到其它行业。
    """
    max_nodes = max(10, min(int(max_nodes), 2000))
    reg = _default_region(region)
    iid = (industry_id or "").strip() or None
    q_raw = (q or "").strip()

    with connect() as conn:
        ind = None
        if iid:
            ind = conn.execute(
                f"SELECT * FROM kg_node WHERE id=%s AND type='industry' AND {_PUBLISHED_SQL}",
                (iid,),
            ).fetchone()
        if not ind and q_raw:
            ind = conn.execute(
                f"""
                SELECT * FROM kg_node
                WHERE type='industry' AND {_PUBLISHED_SQL}
                  AND lower(name) LIKE lower(%s)
                  AND (%s::text IS NULL OR region = %s)
                ORDER BY
                  CASE WHEN lower(name) = lower(%s) THEN 0
                       WHEN lower(name) LIKE lower(%s) THEN 1
                       ELSE 2 END,
                  name
                LIMIT 1
                """,
                (f"%{q_raw}%", reg, reg, q_raw, f"{q_raw}%"),
            ).fetchone()
        if not ind:
            return {
                "root": None,
                "roots": [],
                "nodes": [],
                "edges": [],
                "paths": [],
                "meta": {
                    "matched": 0,
                    "message": "industry not found",
                    "mode": "industry_closure",
                    "engine": "postgresql",
                },
            }

        iid = ind["id"]
        root = _node_dict(ind)

        # majors → industry
        major_rows = conn.execute(
            f"""
            SELECT m.*
            FROM kg_edge e
            JOIN kg_node m ON m.id = e.src_id AND m.type = 'major'
              AND COALESCE(m.status, 'published') = 'published'
            WHERE e.dst_id = %s AND e.rel_type = 'belongs_to' AND {_EDGE_PUB}
              AND COALESCE(e.status, 'published') = 'published'
            ORDER BY m.sort_order NULLS LAST, m.name, m.id
            """,
            (iid,),
        ).fetchall()
        major_ids = [r["id"] for r in major_rows]

        # occupations via prepares_for
        occ_from_major: list = []
        if major_ids:
            occ_from_major = conn.execute(
                f"""
                SELECT DISTINCT o.*
                FROM kg_edge e
                JOIN kg_node o ON o.id = e.dst_id AND o.type = 'occupation'
                  AND COALESCE(o.status, 'published') = 'published'
                WHERE e.rel_type = 'prepares_for' AND {_EDGE_PUB}
                  AND e.src_id = ANY(%s)
                  AND COALESCE(e.status, 'published') = 'published'
                ORDER BY o.sort_order NULLS LAST, o.name, o.id
                """,
                (major_ids,),
            ).fetchall()

        occ_direct: list = []
        if include_direct_occupations:
            occ_direct = conn.execute(
                f"""
                SELECT o.*
                FROM kg_edge e
                JOIN kg_node o ON o.id = e.src_id AND o.type = 'occupation'
                  AND COALESCE(o.status, 'published') = 'published'
                WHERE e.dst_id = %s AND e.rel_type = 'belongs_to' AND {_EDGE_PUB}
                  AND COALESCE(e.status, 'published') = 'published'
                ORDER BY o.sort_order NULLS LAST, o.name, o.id
                """,
                (iid,),
            ).fetchall()

        occ_by_id: dict[str, dict] = {}
        for r in list(occ_from_major) + list(occ_direct):
            occ_by_id[r["id"]] = r
        occ_rows = list(occ_by_id.values())
        occ_ids = [r["id"] for r in occ_rows]

        skill_rows: list = []
        if include_skills and occ_ids:
            skill_rows = conn.execute(
                f"""
                SELECT DISTINCT s.*
                FROM kg_edge e
                JOIN kg_node s ON s.id = e.dst_id AND s.type = 'skill_level'
                  AND COALESCE(s.status, 'published') = 'published'
                WHERE e.rel_type = 'requires'
                  AND e.src_id = ANY(%s)
                  AND COALESCE(e.status, 'published') = 'published'
                ORDER BY s.sort_order NULLS LAST, s.name, s.id
                """,
                (occ_ids,),
            ).fetchall()

        full_counts = {
            "industry": 1,
            "major": len(major_rows),
            "occupation": len(occ_rows),
            "skill_level": len(skill_rows),
        }
        full_total = (
            full_counts["industry"]
            + full_counts["major"]
            + full_counts["occupation"]
            + full_counts["skill_level"]
        )

        # 配额填充：行业必留 → 专业 → 岗位 → 技能
        selected: dict[str, dict] = {iid: root}
        budget = max_nodes - 1

        def _take(rows: list, n: int) -> list:
            out = []
            for r in rows:
                if len(out) >= n:
                    break
                d = _node_dict(r)
                if d["id"] not in selected:
                    selected[d["id"]] = d
                    out.append(d)
            return out

        maj_take = min(len(major_rows), max(0, budget))
        _take(major_rows, maj_take)
        budget = max_nodes - len(selected)

        occ_take = min(len(occ_rows), max(0, budget))
        _take(occ_rows, occ_take)
        budget = max_nodes - len(selected)

        if include_skills:
            sk_take = min(len(skill_rows), max(0, budget))
            _take(skill_rows, sk_take)

        sel_ids = list(selected.keys())

        # 闭包内边
        edges: list[dict] = []
        erows = conn.execute(
            """
            SELECT e.* FROM kg_edge e
            WHERE COALESCE(e.status, 'published') = 'published'
              AND e.src_id = ANY(%s) AND e.dst_id = ANY(%s)
              AND e.rel_type = ANY(%s)
            """,
            (
                sel_ids,
                sel_ids,
                [
                    "belongs_to",
                    "prepares_for",
                    "requires",
                ],
            ),
        ).fetchall()
        for er in erows:
            # 再滤：belongs_to 必须连到本行业或闭包内合法
            rt = er["rel_type"]
            if rt == "belongs_to":
                if er["dst_id"] != iid:
                    continue
                if er["src_id"] not in selected:
                    continue
            elif rt == "prepares_for":
                if er["src_id"] not in selected or er["dst_id"] not in selected:
                    continue
                if selected[er["src_id"]].get("type") != "major":
                    continue
                if selected[er["dst_id"]].get("type") != "occupation":
                    continue
            elif rt == "requires":
                if er["src_id"] not in selected or er["dst_id"] not in selected:
                    continue
            edges.append(_rel_dict(er))

        nodes = list(selected.values())
        # 稳定序：类型层 + order
        type_rank = {
            "industry": 0,
            "major": 1,
            "occupation": 2,
            "skill_level": 3,
        }
        nodes.sort(
            key=lambda n: (
                type_rank.get(n.get("type") or "", 9),
                n.get("order") if n.get("order") is not None else 10**9,
                n.get("name") or "",
                n.get("id") or "",
            )
        )
        truncated = len(nodes) < full_total or full_total > max_nodes

        return {
            "root": root,
            "roots": [root],
            "nodes": nodes,
            "edges": edges,
            "paths": [],
            "meta": {
                "matched": 1,
                "max_nodes": max_nodes,
                "node_count": len(nodes),
                "edge_count": len(edges),
                "path_count": 0,
                "industry_id": iid,
                "industry_name": root.get("name"),
                "region": root.get("region") or reg,
                "mode": "industry_closure",
                "truncated": truncated,
                "full_counts": full_counts,
                "full_total": full_total,
                "include_skills": include_skills,
                "include_direct_occupations": include_direct_occupations,
                "engine": "postgresql",
                "layout_note": "industry_closure_layered",
            },
        }


def graph_by_major(
    name: str,
    *,
    region: str | None = None,
    depth: int = 3,
    max_nodes: int = 300,
    rel_types: list[str] | None = None,
    confidence: list[str] | None = None,
) -> dict[str, Any]:
    depth = max(1, min(int(depth), 5))
    max_nodes = max(10, min(int(max_nodes), 2000))
    reg = _default_region(region)
    with connect() as conn:
        roots = conn.execute(
            f"""
            SELECT * FROM kg_node
            WHERE type = 'major'
              AND lower(name) LIKE lower(%s)
              AND (%s::text IS NULL OR region = %s)
              AND {_PUBLISHED_SQL}
            ORDER BY name
            LIMIT 5
            """,
            (f"%{name}%", reg, reg),
        ).fetchall()
        if not roots:
            return {
                "root": None,
                "roots": [],
                "nodes": [],
                "edges": [],
                "meta": {"matched": 0, "message": "major not found"},
            }
        root_nodes = [_node_dict(r) for r in roots]
        data = explore_graph(
            name,
            node_type="major",
            region=region,
            depth=depth,
            max_nodes=max_nodes,
            seed_ids=[n["id"] for n in root_nodes],
            confidence=confidence,
            rel_types=rel_types,
        )
        data["root"] = root_nodes[0]
        data["roots"] = root_nodes
        return data


def occupation_requires(
    name: str, limit: int = 30, region: str | None = None
) -> list[dict]:
    reg = _default_region(region if region is not None else "CN")
    sql = f"""
    SELECT
      o.name AS occupation,
      o.source_url AS occupation_url,
      s.name AS skill_level,
      s.source_url AS skill_url,
      e.weight AS weight,
      e.confidence AS confidence,
      e.evidence AS evidence
    FROM kg_edge e
    JOIN kg_node o ON o.id = e.src_id AND o.type = 'occupation'
    JOIN kg_node s ON s.id = e.dst_id AND s.type = 'skill_level'
    WHERE e.rel_type = 'requires' AND {_EDGE_PUB}
      AND lower(o.name) LIKE lower(%s)
      AND (%s::text IS NULL OR o.region = %s)
    ORDER BY e.weight DESC NULLS LAST
    LIMIT %s
    """
    with connect() as conn:
        rows = conn.execute(sql, (f"%{name}%", reg, reg, limit)).fetchall()
        return [dict(r) for r in rows]


def explore_graph(
    q: str,
    *,
    node_type: str | None = None,
    region: str | None = None,
    depth: int = 2,
    max_nodes: int = 120,
    path_limit: int = 200,
    seed_ids: list[str] | None = None,
    confidence: list[str] | None = None,
    rel_types: list[str] | None = None,
) -> dict[str, Any]:
    """
    Keyword/list seeds + neighborhood expand via recursive CTE.
    depth=0: seeds only.
    """
    # 合理上限：防一次 BFS 拖垮 DB / 前端
    depth = max(0, min(int(depth), 5))
    max_nodes = max(10, min(int(max_nodes), 2000))
    path_limit = max(20, min(int(path_limit or min(max_nodes * 2, 800)), 800))
    ntype = (node_type or "").strip().lower() or None
    if ntype in ("", "all", "*"):
        ntype = None

    q_raw = (q or "").strip()
    list_all = q_raw in ("", "*", "__all__", "全部", "all")
    if list_all and ntype is None and not seed_ids:
        ntype = "major"

    reg = _default_region(region)

    with connect() as conn:
        if seed_ids:
            seeds = conn.execute(
                f"SELECT * FROM kg_node WHERE id = ANY(%s) AND {_PUBLISHED_SQL}",
                (seed_ids,),
            ).fetchall()
            # preserve order of seed_ids
            by_id = {r["id"]: r for r in seeds}
            seeds = [by_id[i] for i in seed_ids if i in by_id]
        elif list_all:
            seed_limit = max_nodes if depth == 0 else min(20, max_nodes)
            seeds = conn.execute(
                f"""
                SELECT * FROM kg_node
                WHERE (%s::text IS NULL OR region = %s)
                  AND (%s::text IS NULL OR type = %s)
                  AND {_PUBLISHED_SQL}
                ORDER BY name
                LIMIT %s
                """,
                (reg, reg, ntype, ntype, seed_limit),
            ).fetchall()
        else:
            seed_limit = 15
            seeds = conn.execute(
                f"""
                SELECT * FROM kg_node
                WHERE lower(name) LIKE lower(%s)
                  AND (%s::text IS NULL OR region = %s)
                  AND (%s::text IS NULL OR type = %s)
                  AND {_PUBLISHED_SQL}
                ORDER BY
                  CASE type
                    WHEN 'industry' THEN 0
                    WHEN 'major' THEN 1
                    WHEN 'occupation' THEN 2
                    WHEN 'skill_level' THEN 3
                    WHEN 'course' THEN 4
                    WHEN 'credential' THEN 5
                    ELSE 6
                  END,
                  CASE WHEN lower(name) LIKE lower(%s) THEN 0 ELSE 1 END,
                  name
                LIMIT %s
                """,
                (f"%{q_raw}%", reg, reg, ntype, ntype, f"{q_raw}%", seed_limit),
            ).fetchall()

        if not seeds:
            return {
                "roots": [],
                "nodes": [],
                "edges": [],
                "paths": [],
                "meta": {"matched": 0, "message": "no node matched", "depth": depth},
            }

        root_nodes = [_node_dict(r) for r in seeds]
        gids = [n["id"] for n in root_nodes]

        if depth == 0:
            paths = [
                {
                    "steps": [
                        {
                            "kind": "node",
                            "id": n["id"],
                            "name": n["name"],
                            "type": n["type"],
                        }
                    ],
                    "length": 0,
                }
                for n in root_nodes
            ]
            return {
                "roots": root_nodes,
                "nodes": root_nodes,
                "edges": [],
                "paths": paths,
                "meta": {
                    "matched": len(root_nodes),
                    "depth": 0,
                    "max_nodes": max_nodes,
                    "node_count": len(root_nodes),
                    "edge_count": 0,
                    "path_count": len(paths),
                    "q": q_raw or "__all__",
                    "type": ntype,
                    "region": reg,
                    "list_all": list_all,
                    "layout_note": "layered_by_type_not_semantic",
                    "engine": "postgresql",
                },
            }

        # BFS expand undirected neighborhood
        nodes_map: dict[str, dict] = {n["id"]: n for n in root_nodes}
        edges_map: dict[str, dict] = {}
        paths: list[dict[str, Any]] = []

        # per-seed limited expand for path table + graph
        per_seed_edge_cap = 80 if list_all else 200
        for seed in root_nodes:
            frontier = {seed["id"]}
            visited_nodes = {seed["id"]}
            parent: dict[str, tuple[str, dict]] = {}  # child -> (parent_id, edge_row)
            seed_edges: list[dict] = []

            for hop in range(depth):
                if not frontier or len(nodes_map) >= max_nodes:
                    break
                rows = conn.execute(
                    """
                    SELECT e.*, 
                           CASE WHEN e.src_id = ANY(%s) THEN e.dst_id ELSE e.src_id END AS other_id
                    FROM kg_edge e
                    WHERE (e.src_id = ANY(%s) OR e.dst_id = ANY(%s))
                      AND (%s::text[] IS NULL OR e.confidence = ANY(%s))
                      AND (%s::text[] IS NULL OR e.rel_type = ANY(%s))
                    LIMIT %s
                    """,
                    (
                        list(frontier),
                        list(frontier),
                        list(frontier),
                        confidence,
                        confidence,
                        rel_types,
                        rel_types,
                        per_seed_edge_cap * 3,
                    ),
                ).fetchall()

                next_frontier: set[str] = set()
                for er in rows:
                    if len(seed_edges) >= per_seed_edge_cap:
                        break
                    src, dst = er["src_id"], er["dst_id"]
                    # determine direction from frontier
                    if src in frontier:
                        other = dst
                    elif dst in frontier:
                        other = src
                    else:
                        continue
                    if other in visited_nodes and er["id"] in edges_map:
                        continue
                    # load other node if needed
                    if other not in nodes_map:
                        if len(nodes_map) >= max_nodes:
                            continue
                        nrow = conn.execute(
                            f"SELECT * FROM kg_node WHERE id = %s AND {_PUBLISHED_SQL}",
                            (other,),
                        ).fetchone()
                        if not nrow:
                            # 停用/未发布邻居不进入图谱
                            continue
                        nodes_map[other] = _node_dict(nrow)
                    visited_nodes.add(other)
                    next_frontier.add(other)
                    rd = _rel_dict(er)
                    edges_map[rd["id"] or f"{src}|{rd['rel_type']}|{dst}"] = rd
                    seed_edges.append(rd)
                    if other not in parent and other != seed["id"]:
                        parent[other] = (src if src != other else dst, rd)

                frontier = next_frontier - {seed["id"]}

            # build simple paths seed → 1-hop neighbors (and short chains)
            # path table: seed alone + each direct edge path
            paths.append(
                {
                    "steps": [
                        {
                            "kind": "node",
                            "id": seed["id"],
                            "name": seed["name"],
                            "type": seed["type"],
                        }
                    ],
                    "length": 0,
                }
            )
            # reconstruct paths up to depth for first N neighbors
            path_count_seed = 0
            for nid, (pid, erd) in list(parent.items())[:60]:
                if path_count_seed >= 40:
                    break
                # walk back to seed
                chain_nodes = [nid]
                chain_rels = [erd]
                cur = pid
                guard = 0
                while cur != seed["id"] and cur in parent and guard < depth + 2:
                    p2, r2 = parent[cur]
                    chain_nodes.append(cur)
                    chain_rels.append(r2)
                    cur = p2
                    guard += 1
                if cur != seed["id"]:
                    # only direct/known
                    if pid == seed["id"]:
                        chain_nodes = [nid]
                        chain_rels = [erd]
                        cur = seed["id"]
                    else:
                        continue
                chain_nodes.append(seed["id"])
                chain_nodes.reverse()
                chain_rels.reverse()
                steps: list[dict[str, Any]] = []
                for i, cid in enumerate(chain_nodes):
                    nd = nodes_map.get(cid)
                    if not nd:
                        break
                    steps.append(
                        {
                            "kind": "node",
                            "id": nd["id"],
                            "name": nd["name"],
                            "type": nd["type"],
                        }
                    )
                    if i < len(chain_rels):
                        rd = chain_rels[i]
                        steps.append(
                            {
                                "kind": "rel",
                                "rel_type": rd.get("neo4j_type") or rd.get("rel_type"),
                                "rel_type_raw": rd.get("rel_type"),
                            }
                        )
                if steps:
                    paths.append({"steps": steps, "length": max(0, len(chain_rels))})
                    path_count_seed += 1

        nodes = list(nodes_map.values())[:max_nodes]
        edges = list(edges_map.values())
        return {
            "roots": root_nodes,
            "nodes": nodes,
            "edges": edges,
            "paths": paths[:path_limit],
            "meta": {
                "matched": len(root_nodes),
                "depth": depth,
                "max_nodes": max_nodes,
                "node_count": len(nodes),
                "edge_count": len(edges),
                "path_count": min(len(paths), path_limit),
                "q": q_raw or q,
                "type": ntype,
                "region": reg,
                "list_all": list_all,
                "expand_seeds": len(gids),
                "truncated": list_all and depth > 0,
                "layout_note": "layered_by_type_not_semantic",
                "engine": "postgresql",
            },
        }


def stats() -> dict[str, Any]:
    return pg_stats()


def list_edges(
    *,
    rel_type: str | None = None,
    node_id: str | None = None,
    src_id: str | None = None,
    dst_id: str | None = None,
    q: str | None = None,
    status: str | None = None,
    scope: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """
    边列表（管理端核对删节点是否连带删边）。
    node_id：匹配 src 或 dst；可与 rel_type / q（端点名称）组合。

    默认只返回 published——归档/草稿边只留在库里，接口不吐。
    status 指定某一状态（如 archived）可精确查询；scope=manage 时不限状态，
    供管理端核对与恢复。
    """
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 200))
    rel = (rel_type or "").strip() or None
    if rel in ("", "all", "*"):
        rel = None
    nid = (node_id or "").strip() or None
    sid = (src_id or "").strip() or None
    did = (dst_id or "").strip() or None
    q_raw = (q or "").strip()
    q_like = f"%{q_raw}%" if q_raw else None
    st = (status or "").strip() or None
    if st in ("", "all", "*"):
        st = None
    manage = (scope or "").strip().lower() in ("manage", "admin", "all")

    where = ["1=1"]
    params: list[Any] = []
    if st:
        where.append("COALESCE(e.status, 'published') = %s")
        params.append(st)
    elif not manage:
        where.append(_EDGE_PUB)
    if rel:
        where.append("e.rel_type = %s")
        params.append(rel)
    if nid:
        where.append("(e.src_id = %s OR e.dst_id = %s)")
        params.extend([nid, nid])
    if sid:
        where.append("e.src_id = %s")
        params.append(sid)
    if did:
        where.append("e.dst_id = %s")
        params.append(did)
    if q_like:
        where.append(
            "(lower(sn.name) LIKE lower(%s) OR lower(dn.name) LIKE lower(%s) "
            "OR lower(e.id) LIKE lower(%s))"
        )
        params.extend([q_like, q_like, q_like])

    where_sql = " AND ".join(where)
    offset = (page - 1) * page_size
    # LEFT JOIN：删节点若残留边，仍可查出（src_name/dst_name 为空）
    with connect() as conn:
        total = conn.execute(
            f"""
            SELECT COUNT(*) AS c
            FROM kg_edge e
            LEFT JOIN kg_node sn ON sn.id = e.src_id
            LEFT JOIN kg_node dn ON dn.id = e.dst_id
            WHERE {where_sql}
            """,
            params,
        ).fetchone()["c"]
        rows = conn.execute(
            f"""
            SELECT
              e.id, e.src_id, e.dst_id, e.rel_type, e.region, e.weight,
              e.confidence, e.evidence, e.source_url, e.status,
              sn.name AS src_name, sn.type AS src_type,
              dn.name AS dst_name, dn.type AS dst_type
            FROM kg_edge e
            LEFT JOIN kg_node sn ON sn.id = e.src_id
            LEFT JOIN kg_node dn ON dn.id = e.dst_id
            WHERE {where_sql}
            ORDER BY e.rel_type, COALESCE(sn.name, e.src_id), COALESCE(dn.name, e.dst_id)
            LIMIT %s OFFSET %s
            """,
            params + [page_size, offset],
        ).fetchall()

    total = int(total or 0)
    total_pages = (total + page_size - 1) // page_size if total else 0
    items = []
    for r in rows:
        items.append(
            {
                "id": r["id"],
                "src_id": r["src_id"],
                "dst_id": r["dst_id"],
                "rel_type": r["rel_type"],
                "neo4j_type": (r["rel_type"] or "").upper(),
                "region": r.get("region"),
                "weight": r.get("weight"),
                "confidence": r.get("confidence"),
                "evidence": r.get("evidence"),
                "source_url": r.get("source_url"),
                "status": r.get("status") or "published",
                "src_name": r.get("src_name"),
                "src_type": r.get("src_type"),
                "dst_name": r.get("dst_name"),
                "dst_type": r.get("dst_type"),
            }
        )
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "rel_type": rel,
        "node_id": nid,
        "q": q_raw or None,
    }


def list_nodes(
    *,
    node_type: str | None = None,
    region: str | None = None,
    q: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
    published_only: bool = True,
    scope: str | None = None,
) -> dict[str, Any]:
    """
    默认仅 published（图 Table / 探索 / 学员端）。
    scope=manage 时：管理控制台可看全状态（含 disabled），便于停用后再发布。
    status 可指定某一状态过滤。
    """
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 200))
    reg = _default_region(region)
    ntype = (node_type or "").strip().lower() or None
    if ntype in ("", "all", "*"):
        ntype = None
    q_raw = (q or "").strip()
    q_like = f"%{q_raw}%" if q_raw and q_raw not in ("*", "全部", "all") else None
    st = (status or "").strip() or None
    if st in ("", "all", "*"):
        st = None
    manage = (scope or "").strip().lower() in ("manage", "admin", "all")
    if manage:
        published_only = False

    where = ["1=1"]
    params: list[Any] = []
    if ntype:
        where.append("type = %s")
        params.append(ntype)
    if reg is not None:
        where.append("region = %s")
        params.append(reg)
    if q_like:
        where.append("lower(name) LIKE lower(%s)")
        params.append(q_like)
    if st:
        where.append("COALESCE(status, 'published') = %s")
        params.append(st)
    elif published_only:
        where.append(_PUBLISHED_SQL)
    # scope=manage 且无 status：不限状态

    where_sql = " AND ".join(where)
    offset = (page - 1) * page_size

    with connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM kg_node WHERE {where_sql}",
            params,
        ).fetchone()["c"]
        rows = conn.execute(
            f"""
            SELECT * FROM kg_node
            WHERE {where_sql}
            ORDER BY sort_order NULLS LAST, name, id
            LIMIT %s OFFSET %s
            """,
            params + [page_size, offset],
        ).fetchall()

        # 管理列表：挂上待审变更，前端显示「待审核」
        pending_map: dict[str, dict[str, Any]] = {}
        if manage and rows:
            ids = [r["id"] for r in rows]
            try:
                pres = conn.execute(
                    """
                    SELECT DISTINCT ON (target_id)
                      id, target_id, action, title, created_at
                    FROM kg_change_request
                    WHERE entity_kind = 'node'
                      AND target_id = ANY(%s)
                    ORDER BY target_id, created_at DESC
                    """,
                    (ids,),
                ).fetchall()
                for p in pres:
                    pending_map[p["target_id"]] = {
                        "pending_change_id": p["id"],
                        "pending_action": p["action"],
                        "pending_title": p.get("title"),
                    }
            except Exception:
                pending_map = {}

    total = int(total or 0)
    total_pages = (total + page_size - 1) // page_size if total else 0
    items = []
    for r in rows:
        nd = _node_dict(r)
        pend = pending_map.get(r["id"])
        if pend:
            # 库内 status 不变（删除/编辑待审期间仍 published，前台照常可见）
            nd["pending_change_id"] = pend["pending_change_id"]
            nd["pending_action"] = pend["pending_action"]
            nd["pending_title"] = pend.get("pending_title")
        items.append(nd)
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "type": ntype,
        "region": reg,
        "q": q_raw or None,
        "status": st,
    }


def get_node(node_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM kg_node WHERE id = %s", (node_id,)
        ).fetchone()
        if not row:
            return None
        return _node_dict(row)


def attach_link_ids(node: dict[str, Any] | None) -> dict[str, Any] | None:
    """
    为编辑表单附带当前关联 id 列表：
    major → industry_ids；occupation → major_ids；skill_level → occupation_ids。
    """
    if not node or not node.get("id"):
        return node
    nid = node["id"]
    ntype = (node.get("type") or "").lower()
    link_ids: dict[str, list[str]] = {
        "industry_ids": [],
        "major_ids": [],
        "occupation_ids": [],
    }
    with connect() as conn:
        if ntype == "major":
            rows = conn.execute(
                """
                SELECT e.dst_id AS id FROM kg_edge e
                JOIN kg_node i ON i.id = e.dst_id AND i.type = 'industry'
                WHERE e.src_id = %s AND e.rel_type = 'belongs_to'
                  AND COALESCE(e.status, 'published') NOT IN ('archived')
                  AND COALESCE(i.status, 'published') NOT IN ('archived')
                ORDER BY i.name
                """,
                (nid,),
            ).fetchall()
            link_ids["industry_ids"] = [r["id"] for r in rows]
            node["industry_ids"] = link_ids["industry_ids"]
        elif ntype == "occupation":
            rows = conn.execute(
                """
                SELECT e.src_id AS id FROM kg_edge e
                JOIN kg_node m ON m.id = e.src_id AND m.type = 'major'
                WHERE e.dst_id = %s AND e.rel_type = 'prepares_for'
                  AND COALESCE(e.status, 'published') NOT IN ('archived')
                  AND COALESCE(m.status, 'published') NOT IN ('archived')
                ORDER BY m.name
                """,
                (nid,),
            ).fetchall()
            link_ids["major_ids"] = [r["id"] for r in rows]
            node["major_ids"] = link_ids["major_ids"]
        elif ntype == "skill_level":
            rows = conn.execute(
                """
                SELECT e.src_id AS id FROM kg_edge e
                JOIN kg_node o ON o.id = e.src_id AND o.type = 'occupation'
                WHERE e.dst_id = %s AND e.rel_type = 'requires'
                  AND COALESCE(e.status, 'published') NOT IN ('archived')
                  AND COALESCE(o.status, 'published') NOT IN ('archived')
                ORDER BY o.name
                """,
                (nid,),
            ).fetchall()
            link_ids["occupation_ids"] = [r["id"] for r in rows]
            node["occupation_ids"] = link_ids["occupation_ids"]
    node["link_ids"] = link_ids
    return node


def list_neighbors(
    node_id: str,
    *,
    rel_type: str | None = None,
    direction: str = "out",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    direction: out = src→dst, in = dst→src, both
    Returns list of {edge, node}
    """
    direction = (direction or "out").lower()
    if direction == "both":
        out_rows = list_neighbors(
            node_id, rel_type=rel_type, direction="out", limit=limit
        )
        in_rows = list_neighbors(
            node_id, rel_type=rel_type, direction="in", limit=limit
        )
        return (out_rows + in_rows)[:limit]

    other_col = "e.dst_id" if direction == "out" else "e.src_id"
    where_col = "e.src_id" if direction == "out" else "e.dst_id"
    sql = f"""
    SELECT
      e.id AS edge_id, e.src_id, e.dst_id, e.rel_type, e.weight,
      e.confidence, e.evidence, e.source_url,
      n.id AS node_id
    FROM kg_edge e
    JOIN kg_node n ON n.id = {other_col}
    WHERE {where_col} = %s
      AND (%s::text IS NULL OR e.rel_type = %s)
      AND COALESCE(e.status, 'published') <> 'archived'
      AND COALESCE(n.status, 'published') <> 'archived'
    ORDER BY e.weight DESC NULLS LAST, n.name
    LIMIT %s
    """
    with connect() as conn:
        rows = conn.execute(sql, (node_id, rel_type, rel_type, limit)).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            edge = {
                "id": r["edge_id"],
                "src_id": r["src_id"],
                "dst_id": r["dst_id"],
                "rel_type": r["rel_type"],
                "neo4j_type": (r["rel_type"] or "").upper(),
                "weight": r.get("weight"),
                "confidence": r.get("confidence"),
                "evidence": r.get("evidence"),
                "source_url": r.get("source_url"),
            }
            nrow = conn.execute(
                "SELECT * FROM kg_node WHERE id = %s", (r["node_id"],)
            ).fetchone()
            out.append({"edge": edge, "node": _node_dict(nrow) if nrow else None})
        return out


def major_occupations(
    major_id: str | None = None,
    *,
    q: str | None = None,
    region: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    reg = _default_region(region)
    with connect() as conn:
        if major_id:
            rows = conn.execute(
                f"""
                SELECT o.*, e.id AS edge_id, e.rel_type, e.weight, e.confidence, e.evidence
                FROM kg_edge e
                JOIN kg_node o ON o.id = e.dst_id AND o.type = 'occupation'
                WHERE e.src_id = %s AND e.rel_type = 'prepares_for' AND {_EDGE_PUB}
                  AND COALESCE(e.status, 'published') <> 'archived'
                ORDER BY o.name
                LIMIT %s
                """,
                (major_id, limit),
            ).fetchall()
        else:
            q_like = f"%{q}%" if q else None
            rows = conn.execute(
                f"""
                SELECT DISTINCT ON (o.id) o.*, e.id AS edge_id, e.rel_type, e.weight, e.confidence
                FROM kg_edge e
                JOIN kg_node m ON m.id = e.src_id AND m.type = 'major'
                JOIN kg_node o ON o.id = e.dst_id AND o.type = 'occupation'
                WHERE e.rel_type = 'prepares_for' AND {_EDGE_PUB}
                  AND (%s::text IS NULL OR m.region = %s)
                  AND (%s::text IS NULL OR lower(m.name) LIKE lower(%s))
                ORDER BY o.id, o.name
                LIMIT %s
                """,
                (reg, reg, q_like, q_like, limit),
            ).fetchall()
        return [
            {
                **_node_dict(r),
                "edge": {
                    "id": r.get("edge_id"),
                    "rel_type": r.get("rel_type") or "prepares_for",
                    "weight": r.get("weight"),
                    "confidence": r.get("confidence"),
                },
            }
            for r in rows
        ]


def occupation_skills(
    occupation_id: str | None = None,
    *,
    q: str | None = None,
    region: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    reg = _default_region(region)
    with connect() as conn:
        if occupation_id:
            rows = conn.execute(
                f"""
                SELECT s.*, e.id AS edge_id, e.rel_type, e.weight, e.confidence, e.evidence
                FROM kg_edge e
                JOIN kg_node s ON s.id = e.dst_id AND s.type = 'skill_level'
                WHERE e.src_id = %s AND e.rel_type = 'requires' AND {_EDGE_PUB}
                  AND COALESCE(e.status, 'published') <> 'archived'
                ORDER BY e.weight DESC NULLS LAST, s.name
                LIMIT %s
                """,
                (occupation_id, limit),
            ).fetchall()
        else:
            q_like = f"%{q}%" if q else None
            rows = conn.execute(
                """
                SELECT s.*, e.id AS edge_id, e.rel_type, e.weight, e.confidence
                FROM kg_edge e
                JOIN kg_node o ON o.id = e.src_id AND o.type = 'occupation'
                JOIN kg_node s ON s.id = e.dst_id AND s.type = 'skill_level'
                WHERE e.rel_type = 'requires'
                  AND (%s::text IS NULL OR o.region = %s)
                  AND (%s::text IS NULL OR lower(o.name) LIKE lower(%s))
                ORDER BY e.weight DESC NULLS LAST, s.name
                LIMIT %s
                """,
                (reg, reg, q_like, q_like, limit),
            ).fetchall()
        return [
            {
                **_node_dict(r),
                "edge": {
                    "id": r.get("edge_id"),
                    "rel_type": r.get("rel_type") or "requires",
                    "weight": r.get("weight"),
                    "confidence": r.get("confidence"),
                    "evidence": r.get("evidence"),
                },
            }
            for r in rows
        ]


def industry_tree(region: str | None = None, limit: int = 500) -> dict[str, Any]:
    reg = _default_region(region)
    with connect() as conn:
        nodes = conn.execute(
            """
            SELECT * FROM kg_node
            WHERE type = 'industry'
              AND (%s::text IS NULL OR region = %s)
              AND COALESCE(status, 'published') <> 'archived'
            ORDER BY name
            LIMIT %s
            """,
            (reg, reg, limit),
        ).fetchall()
        ids = [n["id"] for n in nodes]
        edges = []
        if ids:
            edges = conn.execute(
                f"""
                SELECT * FROM kg_edge
                WHERE rel_type = 'parent_of' AND {_EDGE_PUB_BARE}
                  AND src_id = ANY(%s) AND dst_id = ANY(%s)
                """,
                (ids, ids),
            ).fetchall()
        node_list = [_node_dict(n) for n in nodes]
        edge_list = [_rel_dict(e) for e in edges]
        # children map
        children: dict[str, list[str]] = {}
        for e in edge_list:
            children.setdefault(e["src_id"], []).append(e["dst_id"])
        roots = [n for n in node_list if n["id"] not in {e["dst_id"] for e in edge_list}]
        return {
            "nodes": node_list,
            "edges": edge_list,
            "roots": roots,
            "meta": {
                "region": reg,
                "node_count": len(node_list),
                "edge_count": len(edge_list),
                "root_count": len(roots),
            },
        }


def industry_occupations(
    industry_id: str, limit: int = 100
) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT o.*, e.id AS edge_id, e.rel_type, e.weight, e.confidence
            FROM kg_edge e
            JOIN kg_node o ON o.id = e.src_id AND o.type = 'occupation'
            WHERE e.dst_id = %s AND e.rel_type = 'belongs_to' AND {_EDGE_PUB}
              AND COALESCE(e.status, 'published') <> 'archived'
            ORDER BY o.name
            LIMIT %s
            """,
            (industry_id, limit),
        ).fetchall()
        return [
            {
                **_node_dict(r),
                "edge": {
                    "id": r.get("edge_id"),
                    "rel_type": r.get("rel_type") or "belongs_to",
                    "weight": r.get("weight"),
                    "confidence": r.get("confidence"),
                },
            }
            for r in rows
        ]


_OCC_LEVEL_WORDS = re.compile(
    r"(初级|中级|高级|资深|见习|助理|首席|正高|副高|一级|二级|三级|四级|五级)"
)


def _occ_family_key(row: dict[str, Any]) -> str:
    """岗位族分组键。

    优先用人社部职业分类大典的小类/中类编码（同小类=同岗位族）；
    无编码时回落到「名称剥离层级词后的主干」。
    仅用于晋升链派生时分组，避免跨族乱连。
    """
    attrs = _maybe_json(row.get("attrs")) or {}
    for k in ("minor_code", "mid_code"):
        v = str(attrs.get(k) or "").strip()
        if v:
            return "code:" + v
    name = str(row.get("name") or "")
    stem = _OCC_LEVEL_WORDS.sub("", name).strip()
    return "name:" + (stem or name)


def _lcp_len(a: str, b: str) -> int:
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    return i


def _derive_progressions(occs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """派生岗位晋升链 advances_to（confidence=derived）。

    规则：同岗位族内按 level 分层，仅相邻层之间连边；每个下层岗位按名称最长
    公共前缀选唯一上层目标（schema 约定 advances_to 为 1:1 有向），避免 N×M 交叉。

    局限：完全依赖 occupation.level 的真实性。当前库内岗位来自「职业分类大典」，
    该数据源不含职级维度（97.9% 的岗位 level 相同），因此产出会非常少；
    待企业职级表 / 招聘级别爬取数据到位后无需改动此逻辑即可生效。
    """
    fams: dict[str, list[dict[str, Any]]] = {}
    for o in occs:
        if o.get("level") is None:
            continue
        fams.setdefault(_occ_family_key(o), []).append(o)
    out: list[dict[str, Any]] = []
    for arr in fams.values():
        if len(arr) < 2:
            continue
        by_lv: dict[int, list[dict[str, Any]]] = {}
        for o in arr:
            by_lv.setdefault(int(o["level"]), []).append(o)
        levels = sorted(by_lv)
        for lo, hi in zip(levels, levels[1:]):
            for src in by_lv[lo]:
                sname = str(src.get("name") or "")
                dst = max(by_lv[hi], key=lambda d: _lcp_len(sname, str(d.get("name") or "")))
                out.append(
                    {
                        "from": src["id"],
                        "to": dst["id"],
                        "from_name": src.get("name"),
                        "to_name": dst.get("name"),
                        "from_level": lo,
                        "to_level": hi,
                        "rel_type": "advances_to",
                        "confidence": "derived",
                    }
                )
    return out


def capability_by_major(
    major: str | None = None,
    major_id: str | None = None,
    region: str | None = None,
    limit_occupations: int = 200,
    limit_skills_per_occ: int = 80,
    include_skills: bool = False,
    include_progression: bool = True,
    shared_skill_min_occ: int = 2,
) -> dict[str, Any]:
    """能力全景（仅 published）· 渐进式默认折叠技能层。

    industries:   major -belongs_to-> industry（归属树上层）
    occupations:  major -prepares_for-> occupation（含 level 岗位层级、skill_count）
    skills:       occupation -requires-> skill_level（仅 include_skills=True 时返回明细）
    progressions: occupation -advances_to-> occupation（派生晋升链）
    shared_skills: 被 >= shared_skill_min_occ 个岗位共同要求的技能（侧栏用，不画线）
    """
    reg = _default_region(region)
    node_pub = _PUBLISHED_SQL.replace("status", "n.status")
    with connect() as conn:
        if major_id:
            root = conn.execute(
                f"SELECT * FROM kg_node WHERE id=%s AND {_PUBLISHED_SQL}", (major_id,)
            ).fetchone()
        elif major:
            # 精确名优先，其次前缀，最后包含；同名多条按 region 过滤后取第一条
            root = conn.execute(
                f"SELECT * FROM kg_node WHERE type='major' AND {_PUBLISHED_SQL} "
                "AND (%s::text IS NULL OR region = %s) AND lower(name) LIKE lower(%s) "
                "ORDER BY (lower(name)=lower(%s)) DESC, length(name), name LIMIT 1",
                (reg, reg, "%" + major + "%", major),
            ).fetchone()
        else:
            root = None
        if not root:
            return {"root": None, "industries": [], "occupations": [], "meta": {"matched": 0}}
        mid = root["id"]
        inds = conn.execute(
            "SELECT n.* FROM kg_edge e JOIN kg_node n ON e.dst_id=n.id "
            "WHERE e.src_id=%s AND e.rel_type='belongs_to' AND n.type='industry' "
            f"AND {node_pub}",
            (mid,),
        ).fetchall()
        occs = conn.execute(
            "SELECT n.* FROM kg_edge e JOIN kg_node n ON e.dst_id=n.id "
            "WHERE e.src_id=%s AND e.rel_type='prepares_for' AND n.type='occupation' "
            f"AND {node_pub} ORDER BY n.level NULLS LAST, n.name LIMIT %s",
            (mid, limit_occupations),
        ).fetchall()
        occ_ids = [o["id"] for o in occs]
        skills_by_occ: dict[str, list] = {}
        count_by_occ: dict[str, int] = {}
        shared_skills: list[dict[str, Any]] = []
        if occ_ids:
            # 技能总数始终返回（节点角标用），与是否下发明细无关
            for r in conn.execute(
                "SELECT e.src_id AS _occ, count(*) AS c "
                "FROM kg_edge e JOIN kg_node n ON e.dst_id=n.id "
                "WHERE e.src_id = ANY(%s) AND e.rel_type='requires' AND n.type='skill_level' "
                f"AND {node_pub} GROUP BY e.src_id",
                (occ_ids,),
            ).fetchall():
                count_by_occ[r["_occ"]] = int(r["c"])

            # 明细仅在需要时才拉：下发技能层，或要算共享技能
            if include_skills or shared_skill_min_occ > 0:
                rows = conn.execute(
                    f"SELECT e.src_id AS _occ, {SKILL_KEY_SQL} AS _skill_key, n.* "
                    "FROM kg_edge e JOIN kg_node n ON e.dst_id=n.id "
                    "WHERE e.src_id = ANY(%s) AND e.rel_type='requires' "
                    f"AND n.type='skill_level' AND {node_pub} ORDER BY n.name",
                    (occ_ids,),
                ).fetchall()
                key_occs: dict[str, set[str]] = {}
                key_levels: dict[str, set[str]] = {}
                for r in rows:
                    if include_skills:
                        lst = skills_by_occ.setdefault(r["_occ"], [])
                        if len(lst) < limit_skills_per_occ:
                            lst.append(_node_dict(r))
                    if shared_skill_min_occ > 0:
                        key = r["_skill_key"] or r.get("name") or ""
                        key_occs.setdefault(key, set()).add(r["_occ"])
                        code = (_maybe_json(r.get("attrs")) or {}).get("level_code")
                        if code:
                            key_levels.setdefault(key, set()).add(str(code).upper())
                shared_skills = sorted(
                    (
                        {
                            "skill_key": k,
                            "occ_count": len(v),
                            "levels": sorted(key_levels.get(k, [])),
                            "occupation_ids": sorted(v),
                        }
                        for k, v in key_occs.items()
                        if len(v) >= shared_skill_min_occ
                    ),
                    key=lambda x: (-x["occ_count"], x["skill_key"]),
                )
        # 晋升链读 kg_edge 真边（advances_to / structure_layer=chain），不再运行时派生。
        # 派生逻辑移到 scripts/materialize_advances_to.py 物化入库，
        # 这样晋升关系在通用图接口与管理端里都可见、可编辑、可审核。
        progressions = []
        if include_progression and occ_ids:
            for r in conn.execute(
                """
                SELECT e.src_id, e.dst_id, e.confidence, e.evidence,
                       s.name AS from_name, s.level AS from_level,
                       d.name AS to_name,   d.level AS to_level
                FROM kg_edge e
                JOIN kg_node s ON s.id = e.src_id
                JOIN kg_node d ON d.id = e.dst_id
                WHERE e.rel_type = 'advances_to'
                  AND COALESCE(e.status,'published') = 'published'
                  AND (e.src_id = ANY(%s) OR e.dst_id = ANY(%s))
                ORDER BY s.level NULLS LAST, s.name
                """,
                (occ_ids, occ_ids),
            ).fetchall():
                progressions.append(
                    {
                        "from": r["src_id"],
                        "to": r["dst_id"],
                        "from_name": r["from_name"],
                        "to_name": r["to_name"],
                        "from_level": r["from_level"],
                        "to_level": r["to_level"],
                        "rel_type": "advances_to",
                        "confidence": r["confidence"],
                        "evidence": r["evidence"],
                    }
                )
    occupations = []
    for o in occs:
        d = _node_dict(o)
        d["level"] = o.get("level")
        d["skill_count"] = count_by_occ.get(o["id"], 0)
        d["skills"] = skills_by_occ.get(o["id"], [])
        occupations.append(d)
    return {
        "root": _node_dict(root),
        "industries": [_node_dict(i) for i in inds],
        "occupations": occupations,
        "progressions": progressions,
        "shared_skills": shared_skills,
        "meta": {
            "matched": 1,
            "occupation_count": len(occupations),
            "skill_total": sum(count_by_occ.values()),
            "skills_included": bool(include_skills),
            "progression_count": len(progressions),
            "shared_skill_count": len(shared_skills),
            "region": reg,
        },
    }

