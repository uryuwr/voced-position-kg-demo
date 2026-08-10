#!/usr/bin/env python3
"""
按 BR-02~06 扫描 published 节点，不达标降为 draft（BR-07/08）。

用法:
  python scripts/demote_noncompliant_publish.py --dry-run
  python scripts/demote_noncompliant_publish.py --apply
  python scripts/demote_noncompliant_publish.py --apply --limit 20
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    p = argparse.ArgumentParser(description="Demote non-compliant published nodes to draft")
    p.add_argument("--region", default="CN")
    p.add_argument("--dry-run", action="store_true", help="只报告（默认）")
    p.add_argument("--apply", action="store_true", help="真正写 draft")
    p.add_argument("--limit", type=int, default=None, help="每类最多降级数")
    p.add_argument(
        "--out",
        default=None,
        help="报告 JSON 路径，默认 reports/demote_noncompliant.json",
    )
    args = p.parse_args()
    dry_run = not args.apply
    if args.dry_run:
        dry_run = True

    from backend.kg.pg_store.publish_rules import demote_noncompliant

    summary = demote_noncompliant(
        region=args.region, dry_run=dry_run, limit=args.limit
    )
    out = Path(args.out or ROOT / "reports" / "demote_noncompliant.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nreport → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
