"""AI/低置信边抽检列表（管理端）。"""
from __future__ import annotations

import math
from typing import Any

from backend.kg.pg_store.client import connect
from backend.kg.pg_store.config import prefer_draft, prefer_draft_edge

# 端点 JOIN 没有状态过滤：端点一有草稿行，一条边就 JOIN 出两行，抽检列表里
# 同一条边出现两次、total 虚高（方案 §6.2）
_PD_A = prefer_draft("a")
_PD_B = prefer_draft("b")
_PD_E = prefer_draft_edge("e")


def list_edges_for_review(
    *,
    confidence: str | None = "ai_inferred",
    rel_type: str | None = None,
    page: int = 1,
    page_size: int = 20,
    region: str = "CN",
) -> dict[str, Any]:
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    where = ["1=1"]
    params: list[Any] = []
    if region:
        where.append("e.region = %s")
        params.append(region)
    if confidence:
        where.append("e.confidence = %s")
        params.append(confidence)
    if rel_type:
        where.append("e.rel_type = %s")
        params.append(rel_type)
    wsql = " AND ".join(where)
    with connect() as conn:
        total = conn.execute(
            f"SELECT count(*) AS c FROM kg_edge e WHERE {wsql} AND {_PD_E}", params
        ).fetchone()["c"]
        offset = (page - 1) * page_size
        rows = conn.execute(
            f"""
            SELECT e.id, e.src_id, e.dst_id, e.rel_type, e.weight, e.confidence,
                   e.evidence, e.source_url, e.status,
                   a.name AS src_name, a.type AS src_type,
                   b.name AS dst_name, b.type AS dst_type
            FROM kg_edge e
            JOIN kg_node a ON a.id = e.src_id AND {_PD_A}
            JOIN kg_node b ON b.id = e.dst_id AND {_PD_B}
            WHERE {wsql} AND {_PD_E}
            ORDER BY e.fetched_at DESC NULLS LAST, e.id
            LIMIT %s OFFSET %s
            """,
            (*params, page_size, offset),
        ).fetchall()
    items = [dict(r) for r in rows]
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": int(total),
        "total_pages": max(1, math.ceil(int(total) / page_size)) if total else 0,
        "filter": {"confidence": confidence, "rel_type": rel_type, "region": region},
    }
