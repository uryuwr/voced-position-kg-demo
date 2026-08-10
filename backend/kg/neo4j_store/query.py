"""Neo4j graph queries for API / CLI."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.neo4j_store.client import close_driver, session
from backend.kg.neo4j_store.migrate import stats as neo4j_stats


_LEVEL_SHORT = {
    "ug_bachelor": "本",
    "voc_bachelor": "职本",
    "voc_associate": "高专",
    "voc_secondary": "中职",
}


def _major_display_name(name: str | None, attrs: Any, source_id: str | None = None) -> str:
    """
    同名专业展示消歧：层次简称 · 名称 · 代码
    例：高专 · 中医康复技术 · 520416
    name 字段保持官方原名不变。
    """
    name = (name or "").strip()
    a = attrs if isinstance(attrs, dict) else {}
    level = a.get("level") or ""
    level_zh = a.get("level_zh") or ""
    code = a.get("code") or ""
    if not code and source_id and ":" in str(source_id):
        # source_id = "voc_associate:520416"
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


def _node_dict(n: Any) -> dict[str, Any]:
    """n is neo4j Node"""
    props = dict(n)
    labels = list(n.labels)
    # drop generic Entity from type labels display
    type_labels = [x for x in labels if x != "Entity"]
    attrs = _maybe_json(props.get("attrs"))
    ntype = props.get("type")
    name = props.get("name")
    source_id = props.get("source_id")
    # 列表/搜索/图 统一用 display_name；name 仍是官方原名
    if ntype == "major":
        display_name = _major_display_name(name, attrs, source_id)
    else:
        display_name = name
    return {
        "id": props.get("gid"),
        "labels": type_labels,
        "region": props.get("region"),
        "type": ntype,
        "name": name,
        "display_name": display_name,
        "name_en": props.get("name_en"),
        "name_zh": props.get("name_zh"),
        "description": props.get("description"),
        "source_system": props.get("source_system"),
        "source_id": source_id,
        "source_url": props.get("source_url"),
        "confidence": props.get("confidence"),
        "attrs": attrs,
    }


def _maybe_json(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v


def _rel_dict(r: Any, src_gid: str, dst_gid: str) -> dict[str, Any]:
    props = dict(r)
    return {
        "id": props.get("eid"),
        "rel_type": props.get("rel_type") or r.type.lower(),
        "neo4j_type": r.type,
        "src_id": src_gid,
        "dst_id": dst_gid,
        "weight": props.get("weight"),
        "confidence": props.get("confidence"),
        "source_url": props.get("source_url"),
        "evidence": props.get("evidence"),
    }


def search_nodes(q: str, limit: int = 20, region: str | None = None) -> list[dict]:
    # 专业/岗位优先，避免课名含「计算」等抢联想（course 字母序曾排在 major 前）
    cypher = """
    MATCH (n:Entity)
    WHERE toLower(n.name) CONTAINS toLower($q)
      AND ($region IS NULL OR n.region = $region)
    RETURN n
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
      CASE WHEN toLower(n.name) STARTS WITH toLower($q) THEN 0 ELSE 1 END,
      n.name
    LIMIT $limit
    """
    with session() as s:
        rows = s.run(cypher, q=q, region=region, limit=limit)
        return [_node_dict(r["n"]) for r in rows]


def graph_by_major(
    name: str,
    *,
    region: str | None = None,
    depth: int = 3,
    max_nodes: int = 300,
    rel_types: list[str] | None = None,
    confidence: list[str] | None = None,
) -> dict[str, Any]:
    """
    Expand subgraph from Major node(s) matching name.

    depth: relationship hops (1=major→job, 2=+skills, 3=+courses...)
    max_nodes: hard cap to protect API clients
    """
    depth = max(1, min(depth, 5))
    # Find root majors
    with session() as s:
        roots = list(
            s.run(
                """
                MATCH (m:Major)
                WHERE toLower(m.name) CONTAINS toLower($name)
                  AND ($region IS NULL OR m.region = $region)
                RETURN m
                LIMIT 5
                """,
                name=name,
                region=region,
            )
        )
        if not roots:
            return {
                "root": None,
                "nodes": [],
                "edges": [],
                "meta": {"matched": 0, "message": "major not found"},
            }

        root_nodes = [_node_dict(r["m"]) for r in roots]
        root_gids = [n["id"] for n in root_nodes]

        # Expand along business-forward relationships from Major
        cypher = f"""
        MATCH (m:Major)
        WHERE m.gid IN $gids
        MATCH p = (m)-[:PREPARES_FOR|REQUIRES|COVERS|TAUGHT_BY|LEADS_TO|RELATED_TO|RECOGNIZED_BY|ARTICULATES_TO*1..{depth}]-(x:Entity)
        WITH p LIMIT $path_limit
        WITH relationships(p) AS rels, nodes(p) AS ns
        UNWIND range(0, size(rels)-1) AS i
        WITH rels[i] AS r, ns[i] AS a, ns[i+1] AS b
        WHERE r.eid IS NOT NULL
          AND ($conf IS NULL OR r.confidence IN $conf)
        RETURN DISTINCT a, r, b
        LIMIT $edge_limit
        """
        conf = confidence
        result = list(
            s.run(
                cypher,
                gids=root_gids,
                path_limit=max_nodes * 5,
                edge_limit=max_nodes * 6,
                conf=conf,
            )
        )

    nodes_map: dict[str, dict] = {n["id"]: n for n in root_nodes}
    edges: list[dict] = []
    allowed_rel = set(rel_types) if rel_types else None

    for row in result:
        a = _node_dict(row["a"])
        b = _node_dict(row["b"])
        r = _rel_dict(row["r"], a["id"], b["id"])
        if allowed_rel and r["rel_type"] not in allowed_rel and r["neo4j_type"].lower() not in {
            x.upper() for x in allowed_rel
        }:
            # also allow matching neo4j type names
            if r["neo4j_type"] not in {x.upper() for x in allowed_rel}:
                if r["rel_type"] not in allowed_rel:
                    continue
        nodes_map[a["id"]] = a
        nodes_map[b["id"]] = b
        edges.append(r)
        if len(nodes_map) >= max_nodes:
            break

    # If expansion empty (major isolated), still return root
    truncated = len(nodes_map) >= max_nodes
    return {
        "root": root_nodes[0],
        "roots": root_nodes,
        "nodes": list(nodes_map.values()),
        "edges": edges,
        "meta": {
            "matched": len(root_nodes),
            "depth": depth,
            "max_nodes": max_nodes,
            "node_count": len(nodes_map),
            "edge_count": len(edges),
            "truncated": truncated,
            "region": region,
        },
    }


def occupation_requires(name: str, limit: int = 30, region: str | None = "US") -> list[dict]:
    with session() as s:
        rows = s.run(
            """
            MATCH (o:Occupation)-[r:REQUIRES]->(s:SkillLevel)
            WHERE toLower(o.name) CONTAINS toLower($name)
              AND ($region IS NULL OR o.region = $region)
            RETURN o.name AS occupation,
                   o.source_url AS occupation_url,
                   s.name AS skill_level,
                   s.source_url AS skill_url,
                   r.weight AS weight,
                   r.confidence AS confidence,
                   r.evidence AS evidence
            ORDER BY r.weight DESC
            LIMIT $limit
            """,
            name=name,
            region=region,
            limit=limit,
        )
        return [dict(r) for r in rows]


def explore_graph(
    q: str,
    *,
    node_type: str | None = None,
    region: str | None = None,
    depth: int = 2,
    max_nodes: int = 120,
    path_limit: int = 200,
) -> dict[str, Any]:
    """
    Admin explorer: keyword search (no Cypher) + expand neighborhood.
    depth=0: only seed nodes, no expansion (e.g. full major catalog).
    q empty / * / __all__: list all nodes of type (default major) without name filter.
    """
    depth = max(0, min(int(depth), 4))
    max_nodes = max(10, min(int(max_nodes), 2000))
    path_limit = max(20, min(int(path_limit), 800))
    ntype = (node_type or "").strip().lower() or None
    if ntype in ("", "all", "*"):
        ntype = None

    q_raw = (q or "").strip()
    list_all = q_raw in ("", "*", "__all__", "全部", "all")

    # 全量浏览默认看专业
    if list_all and ntype is None:
        ntype = "major"

    with session() as s:
        # 全量+展开时少取种子，避免 nodes_map 先被岗位占满导致跳数像失效
        if list_all:
            seed_limit = max_nodes if depth == 0 else min(20, max_nodes)
        else:
            seed_limit = 15

        if list_all:
            seeds = list(
                s.run(
                    """
                    MATCH (n:Entity)
                    WHERE ($region IS NULL OR n.region = $region)
                      AND ($ntype IS NULL OR n.type = $ntype)
                    RETURN n
                    ORDER BY n.name
                    LIMIT $limit
                    """,
                    region=region,
                    ntype=ntype,
                    limit=seed_limit,
                )
            )
        else:
            # 种子排序：专业/岗位优先（避免 course 名含「计算机」抢种子导致展开无关子图）
            # 其次：名称以关键字开头 > 仅包含
            seeds = list(
                s.run(
                    """
                    MATCH (n:Entity)
                    WHERE toLower(n.name) CONTAINS toLower($q)
                      AND ($region IS NULL OR n.region = $region)
                      AND ($ntype IS NULL OR n.type = $ntype)
                    RETURN n
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
                      CASE
                        WHEN toLower(n.name) STARTS WITH toLower($q) THEN 0
                        ELSE 1
                      END,
                      n.name
                    LIMIT $limit
                    """,
                    q=q_raw,
                    region=region,
                    ntype=ntype,
                    limit=seed_limit,
                )
            )
        if not seeds:
            return {
                "roots": [],
                "nodes": [],
                "edges": [],
                "paths": [],
                "meta": {"matched": 0, "message": "no node matched", "depth": depth},
            }

        root_nodes = [_node_dict(r["n"]) for r in seeds]
        gids = [n["id"] for n in root_nodes]

        # 0 跳：只返回节点，不展开边
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
                    "region": region,
                    "list_all": list_all,
                    "layout_note": "layered_by_type_not_semantic",
                },
            }

        # 搜索场景种子少，给足路径配额，保证各维度（尤其课程/认证等远端）都被覆盖；
        # 全量场景种子多，限流防路径爆炸。ORDER BY length(p) 让近端维度优先稳定命中，
        # 避免 depth 增大时长路径（技能链）占满配额、把课程/认证挤掉。
        per_seed_paths = 60 if list_all else 150
        path_rows: list[Any] = []
        for gid in gids:
            batch = list(
                s.run(
                    f"""
                    MATCH (seed:Entity {{gid: $gid}})
                    MATCH p = (seed)-[*1..{depth}]-(x:Entity)
                    WHERE all(r IN relationships(p) WHERE r.eid IS NOT NULL)
                    RETURN p
                    ORDER BY length(p)
                    LIMIT $lim
                    """,
                    gid=gid,
                    lim=per_seed_paths,
                )
            )
            path_rows.extend(batch)

    nodes_map: dict[str, dict] = {n["id"]: n for n in root_nodes}
    edges_map: dict[str, dict] = {}
    paths: list[dict[str, Any]] = []

    for row in path_rows:
        p = row["p"]
        ns = list(p.nodes)
        rs = list(p.relationships)
        # 先收节点
        for node in ns:
            nd = _node_dict(node)
            if nd["id"] in nodes_map or len(nodes_map) < max_nodes:
                nodes_map[nd["id"]] = nd
        # 再收边
        for i, rel in enumerate(rs):
            a = _node_dict(ns[i])
            b = _node_dict(ns[i + 1])
            if a["id"] not in nodes_map and len(nodes_map) < max_nodes:
                nodes_map[a["id"]] = a
            if b["id"] not in nodes_map and len(nodes_map) < max_nodes:
                nodes_map[b["id"]] = b
            if a["id"] in nodes_map and b["id"] in nodes_map:
                rd = _rel_dict(rel, a["id"], b["id"])
                edges_map[rd["id"] or f"{a['id']}|{rd['neo4j_type']}|{b['id']}"] = rd
        # 路径表
        steps: list[dict[str, Any]] = []
        for i, node in enumerate(ns):
            nd = _node_dict(node)
            if nd["id"] not in nodes_map:
                continue
            steps.append(
                {"kind": "node", "id": nd["id"], "name": nd["name"], "type": nd["type"]}
            )
            if i < len(rs):
                a = _node_dict(ns[i])
                b = _node_dict(ns[i + 1])
                if a["id"] in nodes_map and b["id"] in nodes_map:
                    rd = _rel_dict(rs[i], a["id"], b["id"])
                    steps.append(
                        {
                            "kind": "rel",
                            "rel_type": rd["neo4j_type"] or rd["rel_type"],
                            "rel_type_raw": rd["rel_type"],
                        }
                    )
        if steps:
            paths.append({"steps": steps, "length": len(rs)})

    if not paths:
        for n in root_nodes:
            paths.append(
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
            )

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
            "region": region,
            "list_all": list_all,
            "expand_seeds": len(gids) if depth > 0 else 0,
            "truncated": list_all and depth > 0,
            "layout_note": "layered_by_type_not_semantic",
        },
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Query Neo4j KG")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p0 = sub.add_parser("stats")
    p0.set_defaults(fn=lambda a: print(json.dumps(neo4j_stats(), ensure_ascii=False, indent=2)))

    p1 = sub.add_parser("search")
    p1.add_argument("q")
    p1.add_argument("--region", default=None)
    p1.add_argument("--limit", type=int, default=20)
    p1.set_defaults(
        fn=lambda a: print(
            json.dumps(search_nodes(a.q, a.limit, a.region), ensure_ascii=False, indent=2)
        )
    )

    p2 = sub.add_parser("by-major")
    p2.add_argument("name")
    p2.add_argument("--region", default=None)
    p2.add_argument("--depth", type=int, default=3)
    p2.add_argument("--max-nodes", type=int, default=300)
    p2.set_defaults(
        fn=lambda a: print(
            json.dumps(
                graph_by_major(a.name, region=a.region, depth=a.depth, max_nodes=a.max_nodes),
                ensure_ascii=False,
                indent=2,
            )
        )
    )

    p3 = sub.add_parser("requires")
    p3.add_argument("q")
    p3.add_argument("--limit", type=int, default=20)
    p3.add_argument("--region", default="US")
    p3.set_defaults(
        fn=lambda a: print(
            json.dumps(
                occupation_requires(a.q, a.limit, a.region), ensure_ascii=False, indent=2
            )
        )
    )

    args = parser.parse_args()
    args.fn(args)
    close_driver()


if __name__ == "__main__":
    main()
