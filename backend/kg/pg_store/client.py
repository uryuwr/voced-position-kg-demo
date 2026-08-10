"""psycopg connection helpers."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from backend.kg.pg_store.config import DATABASE_URL

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kg_node (
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

CREATE TABLE IF NOT EXISTS kg_edge (
  id TEXT PRIMARY KEY,
  src_id TEXT NOT NULL REFERENCES kg_node(id),
  dst_id TEXT NOT NULL REFERENCES kg_node(id),
  rel_type TEXT NOT NULL,
  region TEXT NOT NULL,
  weight DOUBLE PRECISION,
  evidence TEXT,
  attrs TEXT,
  source_system TEXT NOT NULL,
  source_id TEXT,
  source_url TEXT NOT NULL,
  license TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  confidence TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kg_node_region_type ON kg_node(region, type);
CREATE INDEX IF NOT EXISTS idx_kg_node_name_lower ON kg_node(lower(name));
CREATE INDEX IF NOT EXISTS idx_kg_node_source ON kg_node(source_system, source_id);
CREATE INDEX IF NOT EXISTS idx_kg_edge_src ON kg_edge(src_id);
CREATE INDEX IF NOT EXISTS idx_kg_edge_dst ON kg_edge(dst_id);
CREATE INDEX IF NOT EXISTS idx_kg_edge_rel ON kg_edge(rel_type);
CREATE INDEX IF NOT EXISTS idx_kg_edge_region ON kg_edge(region);

-- 发布状态：published | draft | archived（迁移数据默认 published）
ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'published';
ALTER TABLE kg_edge ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'published';
ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS updated_by TEXT;
ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS updated_by_name TEXT;
ALTER TABLE kg_edge ADD COLUMN IF NOT EXISTS updated_by TEXT;
ALTER TABLE kg_edge ADD COLUMN IF NOT EXISTS updated_by_name TEXT;

-- 图布局/懒加载：同层稳定序 + 下级数量（能力图「向下」语义）
ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS sort_order INT;
ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS child_count INT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_kg_node_type_sort
  ON kg_node(region, type, sort_order NULLS LAST, name);

-- 能力全景重构：岗位层级(晋升递进) / 技能分类 / 边结构层
ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS level INT;         -- occupation: 岗位层级 1..N；skill_level 可复用为 L 序
ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS category TEXT;     -- skill: 技能大类（运营/数据/内容/商业/技术/通用…）
ALTER TABLE kg_edge ADD COLUMN IF NOT EXISTS structure_layer TEXT; -- tree(归属) | net(关联) | chain(进阶)
CREATE INDEX IF NOT EXISTS idx_kg_edge_layer ON kg_edge(structure_layer);
CREATE INDEX IF NOT EXISTS idx_kg_node_category ON kg_node(type, category);

CREATE TABLE IF NOT EXISTS kg_proposal (
  id BIGSERIAL PRIMARY KEY,
  kind TEXT NOT NULL,
  payload JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  reason TEXT,
  created_by TEXT NOT NULL,
  created_by_name TEXT NOT NULL,
  reviewed_by TEXT,
  reviewed_by_name TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  reviewed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_kg_proposal_status ON kg_proposal(status);
"""


def connect() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


@contextmanager
def session() -> Iterator[psycopg.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema(conn: psycopg.Connection | None = None) -> None:
    own = conn is None
    c = conn or connect()
    try:
        c.execute(SCHEMA_SQL)
        if own:
            c.commit()
    finally:
        if own:
            c.close()


def verify_connectivity() -> dict[str, Any]:
    try:
        with connect() as conn:
            ver = conn.execute("SELECT version()").fetchone()
            try:
                n = conn.execute("SELECT COUNT(*) AS c FROM kg_node").fetchone()
                e = conn.execute("SELECT COUNT(*) AS c FROM kg_edge").fetchone()
                nodes = int((n or {}).get("c") or 0)
                edges = int((e or {}).get("c") or 0)
            except Exception:
                nodes, edges = -1, -1
            return {
                "ok": True,
                "engine": "postgresql",
                "version": (ver or {}).get("version", "")[:80],
                "nodes": nodes,
                "edges": edges,
            }
    except Exception as ex:
        return {"ok": False, "engine": "postgresql", "error": str(ex)}
