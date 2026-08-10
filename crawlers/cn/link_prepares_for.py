"""
CN 专业 → 岗位 prepares_for 连边。

当前：读取 staging 种子 JSON（IT 试点），写入 SQLite edges。
  confidence=derived（种子/规则）；有课标原文后可升 official 并改 evidence。

Usage:
  python -m crawlers.cn.link_prepares_for --dry-run
  python -m crawlers.cn.link_prepares_for
  python -m crawlers.maintenance.tag_cn_scope   # 有边后抬升岗位推荐档
  python -m backend.kg.neo4j_store.migrate
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.graph_store import connect, stats, upsert_edges
from backend.kg.paths import REPORTS, STAGING, ensure_dirs
from backend.kg.provenance import make_edge_id, utc_now_iso

REGION = "CN"
SOURCE_SYSTEM = "LINK_CN"
LICENSE = "试点映射：专业—大典岗位（derived）；非教育部正式全量对照表"
DEFAULT_SEED = STAGING / "CN" / "prepares_for_pilot_it.json"


def load_seed(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_major_id(conn, source_id: str) -> tuple[str | None, str | None]:
    row = conn.execute(
        """
        SELECT id, name FROM nodes
        WHERE region=? AND type='major' AND source_id=?
        """,
        (REGION, source_id),
    ).fetchone()
    if row:
        return row["id"], row["name"]
    return None, None


def resolve_occ_id(conn, code: str) -> tuple[str | None, str | None]:
    row = conn.execute(
        """
        SELECT id, name FROM nodes
        WHERE region=? AND type='occupation' AND source_system='MOHRSS_CN'
          AND source_id=?
        """,
        (REGION, code),
    ).fetchone()
    if row:
        return row["id"], row["name"]
    return None, None


def build_edges(conn, seed: dict, fetched_at: str) -> tuple[list[dict], dict]:
    report: dict = {
        "seed_links": len(seed.get("links") or []),
        "edges": 0,
        "missing_majors": [],
        "missing_occupations": [],
        "pairs": [],
    }
    edges: list[dict] = []
    seen: set[str] = set()

    for item in seed.get("links") or []:
        mid_sid = item["major_source_id"]
        maj_id, maj_name = resolve_major_id(conn, mid_sid)
        if not maj_id:
            report["missing_majors"].append(mid_sid)
            continue
        evidence = item.get("evidence") or "prepares_for pilot seed"
        for code in item.get("occupation_codes") or []:
            occ_id, occ_name = resolve_occ_id(conn, code)
            if not occ_id:
                report["missing_occupations"].append(code)
                continue
            eid = make_edge_id(maj_id, "prepares_for", occ_id)
            if eid in seen:
                continue
            seen.add(eid)
            edges.append(
                {
                    "id": eid,
                    "src_id": maj_id,
                    "dst_id": occ_id,
                    "rel_type": "prepares_for",
                    "region": REGION,
                    "weight": 1.0,
                    "evidence": evidence,
                    "attrs": {
                        "match_method": "pilot_seed",
                        "major_source_id": mid_sid,
                        "occupation_code": code,
                        "review_status": "seed_approved",
                    },
                    "source_system": SOURCE_SYSTEM,
                    "source_id": f"{mid_sid}->{code}",
                    "source_url": "https://www.moe.gov.cn/",
                    "license": LICENSE,
                    "fetched_at": fetched_at,
                    "confidence": "derived",
                }
            )
            report["pairs"].append(
                {
                    "major": maj_name,
                    "major_source_id": mid_sid,
                    "occupation": occ_name,
                    "occupation_code": code,
                }
            )

    report["edges"] = len(edges)
    report["missing_majors"] = sorted(set(report["missing_majors"]))
    report["missing_occupations"] = sorted(set(report["missing_occupations"]))
    return edges, report


def ingest(seed_path: Path, dry_run: bool = False) -> dict:
    ensure_dirs()
    fetched_at = utc_now_iso()
    seed = load_seed(seed_path)
    result: dict = {
        "seed_path": str(seed_path),
        "fetched_at": fetched_at,
        "dry_run": dry_run,
        "rel_type": "prepares_for",
    }

    conn = connect()
    try:
        edges, parse_report = build_edges(conn, seed, fetched_at)
        result["parse"] = parse_report
        if dry_run:
            result["sample"] = parse_report["pairs"][:10]
            return result
        n = upsert_edges(conn, edges)
        conn.commit()
        result["edges_upserted"] = n
        result["db_stats"] = stats(conn)
    finally:
        conn.close()

    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "link_cn_prepares_for.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Link CN major→occupation prepares_for")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    args = parser.parse_args()
    out = ingest(args.seed, dry_run=args.dry_run)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
