"""可选 counts 物化表骨架（P3-3）。默认仍用联读 counts.py。"""
from __future__ import annotations

from backend.kg.pg_store.client import connect

DDL = """
CREATE TABLE IF NOT EXISTS kg_node_stats (
  node_id TEXT PRIMARY KEY REFERENCES kg_node(id) ON DELETE CASCADE,
  major_c INT NOT NULL DEFAULT 0,
  occupation_c INT NOT NULL DEFAULT 0,
  skill_c INT NOT NULL DEFAULT 0,
  industry_c INT NOT NULL DEFAULT 0,
  course_c INT NOT NULL DEFAULT 0,
  level_c INT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def ensure_stats_table() -> None:
    with connect() as conn:
        conn.execute(DDL)
        conn.commit()


def refresh_all_stats() -> dict[str, int]:
    """全量重算（夜间任务用）；实现可后续接 counts 批量写。"""
    ensure_stats_table()
    # 骨架：仅建表，不强制切换读路径
    return {"status": "table_ready", "note": "读路径仍用 counts 联读；需要时再切换"}
