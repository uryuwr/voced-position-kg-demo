"""Simple CLI queries against kg.sqlite."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.graph_store import connect, stats


def cmd_stats(_: argparse.Namespace) -> None:
    conn = connect()
    try:
        print(json.dumps(stats(conn), ensure_ascii=False, indent=2))
    finally:
        conn.close()


def cmd_search(args: argparse.Namespace) -> None:
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT id, region, type, name, source_system, source_url, confidence
            FROM nodes
            WHERE name LIKE ?
            ORDER BY type, name
            LIMIT ?
            """,
            (f"%{args.q}%", args.limit),
        ).fetchall()
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
    finally:
        conn.close()


def cmd_requires(args: argparse.Namespace) -> None:
    """occupation name contains q → skill_level list"""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT o.name AS occupation, o.source_url AS occupation_url,
                   s.name AS skill_level, s.source_url AS skill_url,
                   e.weight, e.confidence, e.evidence
            FROM edges e
            JOIN nodes o ON o.id = e.src_id
            JOIN nodes s ON s.id = e.dst_id
            WHERE e.rel_type = 'requires'
              AND o.type = 'occupation'
              AND o.name LIKE ?
            ORDER BY e.weight DESC
            LIMIT ?
            """,
            (f"%{args.q}%", args.limit),
        ).fetchall()
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Query vocational KG")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("stats", help="graph counts")
    p1.set_defaults(func=cmd_stats)

    p2 = sub.add_parser("search", help="search nodes by name")
    p2.add_argument("q")
    p2.add_argument("--limit", type=int, default=20)
    p2.set_defaults(func=cmd_search)

    p3 = sub.add_parser("requires", help="skills required by occupation name match")
    p3.add_argument("q")
    p3.add_argument("--limit", type=int, default=30)
    p3.set_defaults(func=cmd_requires)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
