#!/usr/bin/env python3
"""将有边关联的节点（及非归档边）置为 published，便于图渲染联调。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import connect


def status_counts(conn, region: str = "CN") -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT type, COALESCE(status, 'published') AS st, count(*) AS c
            FROM kg_node
            WHERE region = %s
            GROUP BY 1, 2
            ORDER BY 1, 2
            """,
            (region,),
        ).fetchall()
    ]


def main() -> int:
    region = "CN"
    with connect() as conn:
        before = status_counts(conn, region)
        print("BEFORE")
        for r in before:
            print(f"  {r['type']}: {r['st']}={r['c']}")

        cur = conn.execute(
            """
            UPDATE kg_node n
            SET status = 'published'
            -- 只动线上行：草稿行 status 恒为 draft（ck_kg_node_draft_status），
            -- 顺手把草稿改成 published 就是把没发布的内容发出去了
            WHERE NOT n.is_draft
              AND COALESCE(n.status, 'published') <> 'published'
              AND COALESCE(n.status, 'published') NOT IN ('archived')
              AND EXISTS (
                SELECT 1 FROM kg_edge e
                WHERE (e.src_id = n.id OR e.dst_id = n.id)
                  AND COALESCE(e.status, 'published') NOT IN ('archived')
              )
            """
        )
        n_nodes = int(cur.rowcount or 0)

        cur2 = conn.execute(
            """
            UPDATE kg_edge e
            SET status = 'published'
            WHERE NOT e.is_draft
              AND COALESCE(e.status, 'published') NOT IN ('published', 'archived')
            """
        )
        n_edges = int(cur2.rowcount or 0)
        conn.commit()

        after = status_counts(conn, region)
        print(f"UPDATED nodes={n_nodes} edges={n_edges}")
        print("AFTER")
        for r in after:
            print(f"  {r['type']}: {r['st']}={r['c']}")

        majors = conn.execute(
            """
            SELECT count(*) AS c FROM kg_node
            WHERE type = 'major' AND region = %s
              AND COALESCE(status, 'published') = 'published'
            """,
            (region,),
        ).fetchone()["c"]
        print(f"published majors={majors}")

        report = {
            "region": region,
            "nodes_published": n_nodes,
            "edges_published": n_edges,
            "before": before,
            "after": after,
            "published_majors": int(majors),
        }
        out = ROOT / "reports" / "publish_nodes_with_edges.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
