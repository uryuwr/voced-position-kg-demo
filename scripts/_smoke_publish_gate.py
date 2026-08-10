#!/usr/bin/env python3
"""Smoke: draft 隔离 + 门禁（库层；HTTP 需 AUTH_BYPASS）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from backend.kg.pg_store.client import connect
    from backend.kg.pg_store.publish_rules import (
        PublishGateError,
        assert_publish_allowed,
        validate_publish,
    )
    from backend.kg.pg_store.query import explore_graph, list_nodes, search_nodes
    from backend.kg.pg_store.write import patch_node

    with connect() as conn:
        draft = conn.execute(
            """
            SELECT id, name, status FROM kg_node
            WHERE status='draft' AND type='major' AND region='CN' LIMIT 1
            """
        ).fetchone()
        pub_skill = conn.execute(
            """
            SELECT id, name, status FROM kg_node
            WHERE COALESCE(status,'published')='published'
              AND type='skill_level' AND region='CN' LIMIT 1
            """
        ).fetchone()
        draft_occ = conn.execute(
            """
            SELECT id FROM kg_node
            WHERE status='draft' AND type='occupation' AND region='CN' LIMIT 1
            """
        ).fetchone()

    assert draft, "need a draft major"
    print("draft major", dict(draft))
    print("pub skill", dict(pub_skill) if pub_skill else None)

    # BR-07 search
    hits = search_nodes(draft["name"][:6], limit=50, node_type="major")
    hit_ids = [x["id"] for x in hits]
    print("search hit_draft", draft["id"] in hit_ids, "n", len(hits))
    assert draft["id"] not in hit_ids, "draft must not appear in search"

    pub_list = list_nodes(node_type="major", page=1, page_size=5, published_only=True)
    print("list published majors total", pub_list["total"])
    assert pub_list["total"] == 0

    manage = list_nodes(
        node_type="major", page=1, page_size=3, scope="manage", status="draft"
    )
    print("manage draft majors total", manage["total"])
    assert manage["total"] > 0

    exp = explore_graph("*", node_type="major", depth=0, max_nodes=20)
    print("explore major seeds", len(exp.get("nodes") or []))
    assert len(exp.get("nodes") or []) == 0

    # BR-08 validate
    if draft_occ:
        v = validate_publish(
            node_type="occupation", node_id=draft_occ["id"], action="enable"
        )
        print("validate occ ok", v["ok"], "failed", [x["rule"] for x in v["failed"]])
        assert not v["ok"]

    v2 = validate_publish(node_type="major", node_id=draft["id"], action="enable")
    print("validate major ok", v2["ok"])
    assert not v2["ok"]

    try:
        assert_publish_allowed(
            node_type="major", node_id=draft["id"], action="enable"
        )
        print("FAIL: assert should raise")
        return 1
    except PublishGateError as e:
        print("assert_publish_allowed raised", len(e.violations), "violations")

    try:
        patch_node(
            draft["id"],
            {"status": "published"},
            user_id="smoke",
            user_name="smoke",
        )
        print("FAIL: patch publish should raise")
        return 1
    except ValueError as e:
        print("patch publish blocked:", str(e)[:120])

    # 通过 skill 仍可 search
    if pub_skill:
        hits2 = search_nodes(pub_skill["name"][:4], limit=10, node_type="skill_level")
        print("search published skill hits", len(hits2))

    print("SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
