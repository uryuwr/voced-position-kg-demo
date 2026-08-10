#!/usr/bin/env python3
"""刷新 kg_node.sort_order + child_count。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.node_layout_meta import refresh_layout_meta


def main() -> int:
    region = None
    if len(sys.argv) > 1 and sys.argv[1] not in ("all", "*", "--all"):
        region = sys.argv[1]
    r = refresh_layout_meta(region=region)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
