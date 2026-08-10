"""
Migrate graph from SQLite (system of record for pipelines) → Neo4j.

Usage:
  python -m backend.kg.neo4j_store.migrate
  python -m backend.kg.neo4j_store.migrate --clear   # wipe Entity graph then reload
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.neo4j_store.client import close_driver, ensure_constraints, session, verify_connectivity
from backend.kg.neo4j_store.config import REL_TO_TYPE, SQLITE_PATH, TYPE_TO_LABEL


def _load_sqlite(path: Path) -> tuple[list[dict], list[dict]]:
    if not path.exists():
        raise FileNotFoundError(f"SQLite not found: {path}")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        nodes = [dict(r) for r in conn.execute("SELECT * FROM nodes")]
        edges = [dict(r) for r in conn.execute("SELECT * FROM edges")]
    finally:
        conn.close()
    return nodes, edges


def _node_props(row: dict[str, Any]) -> dict[str, Any]:
    aliases = row.get("aliases")
    attrs = row.get("attrs")
    # keep JSON as string for simple Neo4j property typing
    if aliases is not None and not isinstance(aliases, str):
        aliases = json.dumps(aliases, ensure_ascii=False)
    if attrs is not None and not isinstance(attrs, str):
        attrs = json.dumps(attrs, ensure_ascii=False)
    return {
        "gid": row["id"],
        "region": row["region"],
        "type": row["type"],
        "name": row["name"],
        "name_en": row.get("name_en"),
        "name_zh": row.get("name_zh"),
        "aliases": aliases,
        "description": row.get("description"),
        "attrs": attrs,
        "source_system": row["source_system"],
        "source_id": row["source_id"],
        "source_url": row["source_url"],
        "license": row["license"],
        "fetched_at": row["fetched_at"],
        "confidence": row["confidence"],
    }


def _edge_props(row: dict[str, Any]) -> dict[str, Any]:
    attrs = row.get("attrs")
    if attrs is not None and not isinstance(attrs, str):
        attrs = json.dumps(attrs, ensure_ascii=False)
    return {
        "eid": row["id"],
        "region": row["region"],
        "rel_type": row["rel_type"],
        "weight": row.get("weight"),
        "evidence": row.get("evidence"),
        "attrs": attrs,
        "source_system": row["source_system"],
        "source_id": row.get("source_id"),
        "source_url": row["source_url"],
        "license": row["license"],
        "fetched_at": row["fetched_at"],
        "confidence": row["confidence"],
    }


def clear_graph() -> None:
    with session() as s:
        s.run("MATCH (n:Entity) DETACH DELETE n")
    print("cleared all :Entity nodes and relationships")


def migrate_nodes(nodes: list[dict], batch_size: int = 500) -> int:
    total = 0
    # Group by type for correct labels
    by_type: dict[str, list[dict]] = {}
    for n in nodes:
        by_type.setdefault(n["type"], []).append(_node_props(n))

    with session() as s:
        for ntype, props_list in by_type.items():
            label = TYPE_TO_LABEL.get(ntype, "Entity")
            for i in range(0, len(props_list), batch_size):
                batch = props_list[i : i + batch_size]
                # Entity + specific label
                q = f"""
                UNWIND $rows AS row
                MERGE (n:Entity:{label} {{gid: row.gid}})
                SET n += row
                """
                s.run(q, rows=batch)
                total += len(batch)
                print(f"  nodes {label}: {min(i + batch_size, len(props_list))}/{len(props_list)}")
    return total


def migrate_edges(edges: list[dict], batch_size: int = 1000) -> int:
    total = 0
    by_rel: dict[str, list[dict]] = {}
    for e in edges:
        rt = REL_TO_TYPE.get(e["rel_type"], e["rel_type"].upper())
        item = _edge_props(e)
        item["src_gid"] = e["src_id"]
        item["dst_gid"] = e["dst_id"]
        by_rel.setdefault(rt, []).append(item)

    with session() as s:
        for rel, rows in by_rel.items():
            for i in range(0, len(rows), batch_size):
                batch = rows[i : i + batch_size]
                q = f"""
                UNWIND $rows AS row
                MATCH (a:Entity {{gid: row.src_gid}})
                MATCH (b:Entity {{gid: row.dst_gid}})
                MERGE (a)-[r:{rel} {{eid: row.eid}}]->(b)
                SET r.region = row.region,
                    r.rel_type = row.rel_type,
                    r.weight = row.weight,
                    r.evidence = row.evidence,
                    r.attrs = row.attrs,
                    r.source_system = row.source_system,
                    r.source_id = row.source_id,
                    r.source_url = row.source_url,
                    r.license = row.license,
                    r.fetched_at = row.fetched_at,
                    r.confidence = row.confidence
                """
                s.run(q, rows=batch)
                total += len(batch)
                print(f"  edges {rel}: {min(i + batch_size, len(rows))}/{len(rows)}")
    return total


def stats() -> dict[str, Any]:
    with session() as s:
        nodes = s.run("MATCH (n:Entity) RETURN count(n) AS c").single()["c"]
        edges = s.run("MATCH ()-[r]->() WHERE r.eid IS NOT NULL RETURN count(r) AS c").single()["c"]
        by_type = {
            r["type"]: r["c"]
            for r in s.run(
                "MATCH (n:Entity) RETURN n.type AS type, count(*) AS c ORDER BY c DESC"
            )
        }
        by_region = {
            r["region"]: r["c"]
            for r in s.run(
                "MATCH (n:Entity) RETURN n.region AS region, count(*) AS c ORDER BY c DESC"
            )
        }
        by_rel = {
            r["t"]: r["c"]
            for r in s.run(
                "MATCH ()-[r]->() WHERE r.eid IS NOT NULL "
                "RETURN type(r) AS t, count(*) AS c ORDER BY c DESC"
            )
        }
    return {
        "nodes": nodes,
        "edges": edges,
        "nodes_by_type": by_type,
        "nodes_by_region": by_region,
        "edges_by_type": by_rel,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate SQLite KG → Neo4j")
    parser.add_argument("--sqlite", type=Path, default=SQLITE_PATH)
    parser.add_argument("--clear", action="store_true", help="DETACH DELETE :Entity first")
    parser.add_argument("--stats-only", action="store_true")
    args = parser.parse_args()

    print("verify Neo4j...", verify_connectivity())
    ensure_constraints()

    if args.stats_only:
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
        close_driver()
        return

    if args.clear:
        clear_graph()

    t0 = time.time()
    print(f"load sqlite: {args.sqlite}")
    nodes, edges = _load_sqlite(args.sqlite)
    print(f"  sqlite nodes={len(nodes)} edges={len(edges)}")

    n = migrate_nodes(nodes)
    e = migrate_edges(edges)
    s = stats()
    elapsed = round(time.time() - t0, 2)
    report = {
        "migrated_nodes": n,
        "migrated_edges": e,
        "neo4j_stats": s,
        "seconds": elapsed,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    close_driver()


if __name__ == "__main__":
    main()
