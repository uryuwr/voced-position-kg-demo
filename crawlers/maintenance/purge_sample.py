"""
Remove sample / manual_seed data from SQLite graph to avoid polluting official graph.

Usage:
  python -m crawlers.maintenance.purge_sample
  python -m crawlers.maintenance.purge_sample --also-esco-api-sample
  python -m crawlers.maintenance.purge_sample --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.graph_store import connect, purge_sample_data, stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Purge sample/seed rows from kg.sqlite")
    parser.add_argument("--db", default=None, help="SQLite path (default data/graph/kg.sqlite)")
    parser.add_argument(
        "--also-esco-api-sample",
        action="store_true",
        help="Also delete source_system=ESCO rows (old API sample) before full re-ingest",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print current stats / what would be purged counts via SELECT",
    )
    args = parser.parse_args()
    db = Path(args.db) if args.db else None
    conn = connect(db)
    try:
        before = stats(conn)
        confidences = ("manual_seed",)
        systems = ["MANUAL"]
        if args.also_esco_api_sample:
            systems.append("ESCO")

        if args.dry_run:
            cur = conn.cursor()
            n = cur.execute(
                "SELECT COUNT(*) FROM nodes WHERE confidence=? OR source_system IN (%s)"
                % (",".join("?" * len(systems))),
                ("manual_seed", *systems),
            ).fetchone()[0]
            e = cur.execute(
                "SELECT COUNT(*) FROM edges WHERE confidence=? OR source_system IN (%s)"
                % (",".join("?" * len(systems))),
                ("manual_seed", *systems),
            ).fetchone()[0]
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "would_touch_nodes": n,
                        "would_touch_edges_by_provenance": e,
                        "systems": systems,
                        "before": before,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return

        result = purge_sample_data(
            conn,
            confidences=confidences,
            source_systems=tuple(systems),
        )
        after = stats(conn)
        out = {
            "purged": result,
            "systems": systems,
            "before": before,
            "after": after,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
