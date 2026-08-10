"""
Ingest ESCO harvest (api_harvest preferred, fallback api_sample) into graph SQLite.

Writes:
  - occupation nodes (official)
  - skill_level nodes (skill · L_unspecified until ESCO skill levels attached)
  - requires edges occupation → skill_level (essential/optional in attrs)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.graph_store import connect, stats, upsert_edges, upsert_nodes
from backend.kg.paths import RAW, REPORTS, ensure_dirs
from backend.kg.provenance import make_edge_id, make_node_id, utc_now_iso

SOURCE_SYSTEM = "ESCO"
REGION = "EU"
LICENSE = "ESCO open data — attribution required (esco.ec.europa.eu)"


def _label(item: dict, language: str = "en") -> str:
    pl = item.get("preferredLabel") or item.get("title") or item.get("label") or ""
    if isinstance(pl, dict):
        return (
            pl.get(language)
            or pl.get("en")
            or pl.get("en-us")
            or next(iter(pl.values()), "")
        )
    return str(pl)


def _uri(item: dict) -> str:
    return (
        item.get("uri")
        or item.get("resourceUri")
        or (item.get("_links", {}).get("self", {}) or {}).get("href")
        or ""
    )


def _source_id(uri: str) -> str:
    if not uri:
        return "unknown"
    return uri.rstrip("/").split("/")[-1]


def load_items(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    return data.get("results") or data.get("_embedded", {}).get("results") or []


def resolve_base(language: str) -> Path:
    harvest = RAW / "EU" / "esco" / "api_harvest"
    sample = RAW / "EU" / "esco" / "api_sample"
    if (harvest / f"occupations_{language}.json").exists():
        return harvest
    if (sample / f"occupations_{language}.json").exists():
        return sample
    raise FileNotFoundError(
        f"Missing ESCO occupations JSON. Run: python -m crawlers.eu.download_esco"
    )


def skill_node_id(skill_uri: str) -> tuple[str, str]:
    sid = _source_id(skill_uri)
    sl_id = f"{sid}|L_unspecified"
    return make_node_id(REGION, "skill_level", SOURCE_SYSTEM, sl_id), sl_id


def ingest(language: str = "en", db_path: Path | None = None) -> dict:
    ensure_dirs()
    base = resolve_base(language)
    occ_path = base / f"occupations_{language}.json"
    skill_path = base / f"skills_{language}.json"
    rel_path = base / f"relations_{language}.json"

    fetched_at = utc_now_iso()
    nodes: list[dict] = []
    edges: list[dict] = []
    occ_id_by_uri: dict[str, str] = {}
    skill_id_by_uri: dict[str, str] = {}

    for item in load_items(occ_path):
        uri = _uri(item)
        sid = _source_id(uri)
        name = _label(item, language)
        if not name:
            continue
        nid = make_node_id(REGION, "occupation", SOURCE_SYSTEM, sid)
        occ_id_by_uri[uri] = nid
        desc = item.get("description")
        if isinstance(desc, dict):
            desc = desc.get(language) or desc.get("en") or ""
        nodes.append(
            {
                "id": nid,
                "region": REGION,
                "type": "occupation",
                "name": name,
                "name_en": name if language == "en" else None,
                "name_zh": None,
                "description": desc if isinstance(desc, str) else None,
                "attrs": {"uri": uri, "className": item.get("className"), "code": item.get("code")},
                "source_system": SOURCE_SYSTEM,
                "source_id": sid,
                "source_url": uri or "https://esco.ec.europa.eu/",
                "license": LICENSE,
                "fetched_at": fetched_at,
                "confidence": "official",
            }
        )

    for item in load_items(skill_path):
        uri = _uri(item)
        name = _label(item, language)
        if not uri or not name:
            continue
        nid, sl_id = skill_node_id(uri)
        skill_id_by_uri[uri] = nid
        nodes.append(
            {
                "id": nid,
                "region": REGION,
                "type": "skill_level",
                "name": f"{name} · L_unspecified",
                "name_en": f"{name} · L_unspecified" if language == "en" else None,
                "name_zh": None,
                "description": None,
                "attrs": {
                    "skill_name": name,
                    "scale": "l1_l4",
                    "level_code": "L_unspecified",
                    "uri": uri,
                    "skillType": item.get("skillType"),
                },
                "source_system": SOURCE_SYSTEM,
                "source_id": sl_id,
                "source_url": uri or "https://esco.ec.europa.eu/",
                "license": LICENSE,
                "fetched_at": fetched_at,
                "confidence": "official",
            }
        )

    # relations may introduce skills not in skills file
    for rel in load_items(rel_path):
        o_uri = rel.get("occupation_uri") or ""
        s_uri = rel.get("skill_uri") or ""
        if not o_uri or not s_uri:
            continue
        if s_uri not in skill_id_by_uri:
            title = rel.get("skill_title") or _source_id(s_uri)
            nid, sl_id = skill_node_id(s_uri)
            skill_id_by_uri[s_uri] = nid
            nodes.append(
                {
                    "id": nid,
                    "region": REGION,
                    "type": "skill_level",
                    "name": f"{title} · L_unspecified",
                    "name_en": f"{title} · L_unspecified" if language == "en" else None,
                    "name_zh": None,
                    "description": None,
                    "attrs": {
                        "skill_name": title,
                        "scale": "l1_l4",
                        "level_code": "L_unspecified",
                        "uri": s_uri,
                        "skillType": rel.get("skill_type"),
                        "from_relation": True,
                    },
                    "source_system": SOURCE_SYSTEM,
                    "source_id": sl_id,
                    "source_url": s_uri,
                    "license": LICENSE,
                    "fetched_at": fetched_at,
                    "confidence": "official",
                }
            )
        src = occ_id_by_uri.get(o_uri)
        dst = skill_id_by_uri.get(s_uri)
        if not src or not dst:
            continue
        rel_flag = rel.get("relation") or "essential"
        eid = make_edge_id(src, "requires", dst)
        edges.append(
            {
                "id": eid,
                "src_id": src,
                "dst_id": dst,
                "rel_type": "requires",
                "region": REGION,
                "weight": 1.0 if rel_flag == "essential" else 0.5,
                "evidence": f"ESCO occupation {rel_flag} skill link",
                "attrs": {"esco_relation": rel_flag},
                "source_system": SOURCE_SYSTEM,
                "source_id": f"{_source_id(o_uri)}->{_source_id(s_uri)}",
                "source_url": o_uri or "https://esco.ec.europa.eu/",
                "license": LICENSE,
                "fetched_at": fetched_at,
                "confidence": "official",
            }
        )

    # de-dupe nodes by id (last wins)
    by_id = {n["id"]: n for n in nodes}
    nodes = list(by_id.values())
    by_eid = {e["id"]: e for e in edges}
    edges = list(by_eid.values())

    conn = connect(db_path)
    try:
        n = upsert_nodes(conn, nodes)
        e = upsert_edges(conn, edges)
        conn.commit()
        s = stats(conn)
    finally:
        conn.close()

    summary = {
        "source_system": SOURCE_SYSTEM,
        "base_dir": str(base),
        "nodes_upserted": n,
        "edges_upserted": e,
        "occupations": len(occ_id_by_uri),
        "skills": len(skill_id_by_uri),
        "relations": len(edges),
        "db_stats": s,
        "fetched_at": fetched_at,
        "confidence": "official",
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "ingest_esco.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--language", default="en")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    summary = ingest(args.language, Path(args.db) if args.db else None)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
