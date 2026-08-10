"""Provenance helpers and ID builders."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def make_node_id(region: str, node_type: str, source_system: str, source_id: str) -> str:
    safe = str(source_id).replace(" ", "_").replace("/", "_")
    return f"{region}:{node_type}:{source_system}:{safe}"


def make_edge_id(src_id: str, rel_type: str, dst_id: str) -> str:
    return f"edge:{src_id}|{rel_type}|{dst_id}"


def base_provenance(
    *,
    source_system: str,
    source_id: str,
    source_url: str,
    license: str,
    confidence: str,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    return {
        "source_system": source_system,
        "source_id": str(source_id),
        "source_url": source_url,
        "license": license,
        "fetched_at": fetched_at or utc_now_iso(),
        "confidence": confidence,
    }
