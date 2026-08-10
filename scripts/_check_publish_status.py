#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import connect
from backend.kg.pg_store.publish_rules import demote_noncompliant, validate_publish


def main() -> None:
    s = demote_noncompliant(region="CN", dry_run=False)
    print(
        "pass",
        {k: s[k].get("demoted") for k in ("major", "occupation", "skill")},
    )
    with connect() as conn:
        for t in ("major", "occupation", "skill_level", "industry"):
            rows = conn.execute(
                """
                SELECT COALESCE(status, 'published') AS st, count(*) AS c
                FROM kg_node
                WHERE type=%s AND region=%s
                GROUP BY 1 ORDER BY 1
                """,
                (t, "CN"),
            ).fetchall()
            print(t, {r["st"]: r["c"] for r in rows})
        pubs = conn.execute(
            """
            SELECT id, name FROM kg_node
            WHERE type='major' AND COALESCE(status,'published')='published'
              AND region='CN' LIMIT 3
            """
        ).fetchall()
        print("published majors", [dict(r) for r in pubs])
        drafts = conn.execute(
            """
            SELECT id, name, status FROM kg_node
            WHERE type='major' AND status='draft' AND region='CN' LIMIT 1
            """
        ).fetchall()
        print("draft major", [dict(r) for r in drafts])
        if pubs:
            print("validate pub major", validate_publish(node_type="major", node_id=pubs[0]["id"]))
        if drafts:
            print(
                "validate draft major",
                validate_publish(node_type="major", node_id=drafts[0]["id"]),
            )


if __name__ == "__main__":
    main()
