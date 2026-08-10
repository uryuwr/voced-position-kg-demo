"""SQLite graph store for MVP."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from backend.kg.paths import GRAPH, ensure_dirs

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nodes (
  id TEXT PRIMARY KEY,
  region TEXT NOT NULL,
  type TEXT NOT NULL,
  name TEXT NOT NULL,
  name_en TEXT,
  name_zh TEXT,
  aliases TEXT,
  description TEXT,
  attrs TEXT,
  source_system TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_url TEXT NOT NULL,
  license TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  confidence TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
  id TEXT PRIMARY KEY,
  src_id TEXT NOT NULL,
  dst_id TEXT NOT NULL,
  rel_type TEXT NOT NULL,
  region TEXT NOT NULL,
  weight REAL,
  evidence TEXT,
  attrs TEXT,
  source_system TEXT NOT NULL,
  source_id TEXT,
  source_url TEXT NOT NULL,
  license TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  confidence TEXT NOT NULL,
  FOREIGN KEY (src_id) REFERENCES nodes(id),
  FOREIGN KEY (dst_id) REFERENCES nodes(id)
);

CREATE INDEX IF NOT EXISTS idx_nodes_region_type ON nodes(region, type);
CREATE INDEX IF NOT EXISTS idx_nodes_source ON nodes(source_system, source_id);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id);
CREATE INDEX IF NOT EXISTS idx_edges_rel ON edges(rel_type);
"""


def default_db_path() -> Path:
    ensure_dirs()
    return GRAPH / "kg.sqlite"


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def upsert_node(conn: sqlite3.Connection, node: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO nodes (
          id, region, type, name, name_en, name_zh, aliases, description, attrs,
          source_system, source_id, source_url, license, fetched_at, confidence
        ) VALUES (
          :id, :region, :type, :name, :name_en, :name_zh, :aliases, :description, :attrs,
          :source_system, :source_id, :source_url, :license, :fetched_at, :confidence
        )
        ON CONFLICT(id) DO UPDATE SET
          name=excluded.name,
          name_en=excluded.name_en,
          name_zh=excluded.name_zh,
          aliases=excluded.aliases,
          description=excluded.description,
          attrs=excluded.attrs,
          source_url=excluded.source_url,
          license=excluded.license,
          fetched_at=excluded.fetched_at,
          confidence=excluded.confidence
        """,
        {
            "id": node["id"],
            "region": node["region"],
            "type": node["type"],
            "name": node["name"],
            "name_en": node.get("name_en"),
            "name_zh": node.get("name_zh"),
            "aliases": _json_or_none(node.get("aliases")),
            "description": node.get("description"),
            "attrs": _json_or_none(node.get("attrs")),
            "source_system": node["source_system"],
            "source_id": node["source_id"],
            "source_url": node["source_url"],
            "license": node["license"],
            "fetched_at": node["fetched_at"],
            "confidence": node["confidence"],
        },
    )


def upsert_edge(conn: sqlite3.Connection, edge: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO edges (
          id, src_id, dst_id, rel_type, region, weight, evidence, attrs,
          source_system, source_id, source_url, license, fetched_at, confidence
        ) VALUES (
          :id, :src_id, :dst_id, :rel_type, :region, :weight, :evidence, :attrs,
          :source_system, :source_id, :source_url, :license, :fetched_at, :confidence
        )
        ON CONFLICT(id) DO UPDATE SET
          weight=excluded.weight,
          evidence=excluded.evidence,
          attrs=excluded.attrs,
          source_url=excluded.source_url,
          license=excluded.license,
          fetched_at=excluded.fetched_at,
          confidence=excluded.confidence
        """,
        {
            "id": edge["id"],
            "src_id": edge["src_id"],
            "dst_id": edge["dst_id"],
            "rel_type": edge["rel_type"],
            "region": edge["region"],
            "weight": edge.get("weight"),
            "evidence": edge.get("evidence"),
            "attrs": _json_or_none(edge.get("attrs")),
            "source_system": edge["source_system"],
            "source_id": edge.get("source_id"),
            "source_url": edge["source_url"],
            "license": edge["license"],
            "fetched_at": edge["fetched_at"],
            "confidence": edge["confidence"],
        },
    )


def upsert_nodes(conn: sqlite3.Connection, nodes: Iterable[dict[str, Any]]) -> int:
    n = 0
    for node in nodes:
        upsert_node(conn, node)
        n += 1
    return n


def upsert_edges(conn: sqlite3.Connection, edges: Iterable[dict[str, Any]]) -> int:
    n = 0
    for edge in edges:
        upsert_edge(conn, edge)
        n += 1
    return n


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    cur = conn.cursor()
    node_total = cur.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    edge_total = cur.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    by_type = {
        r["type"]: r["c"]
        for r in cur.execute("SELECT type, COUNT(*) AS c FROM nodes GROUP BY type")
    }
    by_region = {
        r["region"]: r["c"]
        for r in cur.execute("SELECT region, COUNT(*) AS c FROM nodes GROUP BY region")
    }
    by_conf = {
        r["confidence"]: r["c"]
        for r in cur.execute(
            "SELECT confidence, COUNT(*) AS c FROM edges GROUP BY confidence"
        )
    }
    by_source = {
        r["source_system"]: r["c"]
        for r in cur.execute(
            "SELECT source_system, COUNT(*) AS c FROM nodes GROUP BY source_system"
        )
    }
    return {
        "nodes": node_total,
        "edges": edge_total,
        "nodes_by_type": by_type,
        "nodes_by_region": by_region,
        "edges_by_confidence": by_conf,
        "nodes_by_source": by_source,
    }


def purge_sample_data(
    conn: sqlite3.Connection,
    *,
    confidences: tuple[str, ...] = ("manual_seed",),
    source_systems: tuple[str, ...] = ("MANUAL",),
) -> dict[str, int]:
    """
    Remove demo/seed pollution from the graph store.

    Deletes:
      - edges with matching confidence or source_system
      - edges whose endpoints are sample nodes
      - nodes with matching confidence or source_system
    """
    cur = conn.cursor()
    conf_list = list(confidences)
    src_list = list(source_systems)

    # Sample node ids
    placeholders_c = ",".join("?" * len(conf_list)) if conf_list else ""
    placeholders_s = ",".join("?" * len(src_list)) if src_list else ""
    clauses = []
    params: list[Any] = []
    if conf_list:
        clauses.append(f"confidence IN ({placeholders_c})")
        params.extend(conf_list)
    if src_list:
        clauses.append(f"source_system IN ({placeholders_s})")
        params.extend(src_list)
    if not clauses:
        return {"edges_deleted": 0, "nodes_deleted": 0}

    where = " OR ".join(clauses)
    sample_ids = [
        r[0]
        for r in cur.execute(f"SELECT id FROM nodes WHERE {where}", params).fetchall()
    ]

    # Edges by sample provenance
    e1 = cur.execute(f"DELETE FROM edges WHERE {where}", params).rowcount

    # Edges touching sample nodes
    e2 = 0
    if sample_ids:
        # chunk for SQLite variable limit
        chunk = 400
        for i in range(0, len(sample_ids), chunk):
            part = sample_ids[i : i + chunk]
            ph = ",".join("?" * len(part))
            e2 += cur.execute(
                f"DELETE FROM edges WHERE src_id IN ({ph}) OR dst_id IN ({ph})",
                part + part,
            ).rowcount

    n_del = cur.execute(f"DELETE FROM nodes WHERE {where}", params).rowcount
    conn.commit()
    return {
        "edges_deleted": int(e1 + e2),
        "nodes_deleted": int(n_del),
        "sample_node_ids": len(sample_ids),
    }
