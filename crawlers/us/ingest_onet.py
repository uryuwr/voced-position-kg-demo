"""
Ingest O*NET text database into graph SQLite.

Reads official tab-delimited tables from unzipped db_*_text.zip:
  - Occupation Data.txt
  - Essential Skills.txt / Transferable Skills.txt
    (O*NET-SOC Code, Element ID, Element Name, Scale ID, Data Value, ...)
  - Software Skills.txt (optional, binary association)

Note: O*NET 30.x renamed classic Skills.txt into Essential + Transferable.

Produces:
  - occupation nodes
  - skill_level nodes (skill × scale × bucketed level)
  - requires edges (occupation → skill_level)

Attribution: National Center for O*NET Development
License: see https://www.onetcenter.org/license_db.html
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.graph_store import connect, stats, upsert_edges, upsert_nodes
from backend.kg.paths import RAW, STAGING, ensure_dirs
from backend.kg.provenance import make_edge_id, make_node_id, utc_now_iso

SOURCE_SYSTEM = "ONET"
REGION = "US"
LICENSE = "O*NET Database License (CC; see onetcenter.org/license_db.html)"
SOURCE_HOME = "https://www.onetcenter.org/database.html"
ONET_ONLINE = "https://www.onetonline.org/link/summary/{code}"


def find_text_zip(version: str) -> Path:
    p = RAW / "US" / "onet" / f"db_{version}" / f"db_{version}_text.zip"
    if not p.exists():
        raise FileNotFoundError(
            f"Missing {p}. Run: python -m crawlers.us.download_onet --version {version}"
        )
    return p


def unzip_text(zip_path: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest)
    # O*NET zips may nest a folder
    candidates = list(dest.rglob("Occupation Data.txt"))
    if not candidates:
        # sometimes .txt without space variants
        candidates = list(dest.rglob("Occupation Data.*"))
    if not candidates:
        raise FileNotFoundError(f"Occupation Data.txt not found under {dest}")
    return candidates[0].parent


def read_table(folder: Path, name: str) -> pd.DataFrame:
    path = folder / name
    if not path.exists():
        # try case-insensitive
        matches = [p for p in folder.iterdir() if p.name.lower() == name.lower()]
        if not matches:
            raise FileNotFoundError(path)
        path = matches[0]
    return pd.read_csv(path, sep="\t", dtype=str).fillna("")


def level_bucket(value: float) -> str:
    """Map O*NET Level 0-7 continuous-ish value to L1-L4 for MVP queries."""
    if value < 2.0:
        return "L1"
    if value < 3.5:
        return "L2"
    if value < 5.0:
        return "L3"
    return "L4"


def ingest(version: str, db_path: Path | None = None) -> dict:
    ensure_dirs()
    fetched_at = utc_now_iso()
    zip_path = find_text_zip(version)
    extract_dir = STAGING / "onet" / f"db_{version}_text"
    folder = unzip_text(zip_path, extract_dir)

    occ = read_table(folder, "Occupation Data.txt")
    # Expected columns: O*NET-SOC Code, Title, Description
    code_col = [c for c in occ.columns if "code" in c.lower()][0]
    title_col = [c for c in occ.columns if "title" in c.lower()][0]
    desc_col = next((c for c in occ.columns if "desc" in c.lower()), None)

    nodes: list[dict] = []
    edges: list[dict] = []

    for _, row in occ.iterrows():
        code = str(row[code_col]).strip()
        title = str(row[title_col]).strip()
        if not code or not title:
            continue
        nid = make_node_id(REGION, "occupation", SOURCE_SYSTEM, code)
        nodes.append(
            {
                "id": nid,
                "region": REGION,
                "type": "occupation",
                "name": title,
                "name_en": title,
                "name_zh": None,
                "description": str(row[desc_col]).strip() if desc_col else None,
                "attrs": {"onet_soc_code": code},
                "source_system": SOURCE_SYSTEM,
                "source_id": code,
                "source_url": ONET_ONLINE.format(code=code),
                "license": LICENSE,
                "fetched_at": fetched_at,
                "confidence": "official",
            }
        )

    skill_nodes_seen: set[str] = set()
    skill_files = [
        "Essential Skills.txt",
        "Transferable Skills.txt",
        "Skills.txt",  # legacy name, if present
    ]
    skill_frames: list[pd.DataFrame] = []
    for fname in skill_files:
        try:
            skill_frames.append(read_table(folder, fname))
            print(f"loaded skill table: {fname}")
        except FileNotFoundError:
            continue
    skills = pd.concat(skill_frames, ignore_index=True) if skill_frames else None

    if skills is not None and len(skills):
        cols = {c.lower(): c for c in skills.columns}

        def col(*keys: str) -> str:
            for k in keys:
                for lk, orig in cols.items():
                    if k in lk:
                        return orig
            raise KeyError(keys)

        c_code = col("o*net-soc code", "soc code")
        c_eid = col("element id")
        c_ename = col("element name")
        c_scale = col("scale id")
        c_val = col("data value")

        # Prefer Level (LV); also keep Importance (IM)
        for _, row in skills.iterrows():
            scale = str(row[c_scale]).strip().upper()
            if scale not in ("LV", "IM"):
                continue
            code = str(row[c_code]).strip()
            eid = str(row[c_eid]).strip()
            ename = str(row[c_ename]).strip()
            try:
                val = float(str(row[c_val]).strip())
            except ValueError:
                continue

            if scale == "LV":
                bucket = level_bucket(val)
                scale_name = "onet_level"
            else:
                bucket = "IM" + str(int(round(val)))
                scale_name = "onet_importance"

            sl_source_id = f"{eid}|{scale}|{bucket}"
            sl_id = make_node_id(REGION, "skill_level", SOURCE_SYSTEM, sl_source_id)
            if sl_id not in skill_nodes_seen:
                skill_nodes_seen.add(sl_id)
                nodes.append(
                    {
                        "id": sl_id,
                        "region": REGION,
                        "type": "skill_level",
                        "name": f"{ename} · {bucket}",
                        "name_en": f"{ename} · {bucket}",
                        "name_zh": None,
                        "description": None,
                        "attrs": {
                            "skill_name": ename,
                            "skill_element_id": eid,
                            "scale": scale_name,
                            "level_code": bucket,
                            "raw_value_example": val,
                        },
                        "source_system": SOURCE_SYSTEM,
                        "source_id": sl_source_id,
                        "source_url": (
                            "https://www.onetcenter.org/dictionary/"
                            f"{version.replace('_', '.')}/excel/essential_skills.html"
                        ),
                        "license": LICENSE,
                        "fetched_at": fetched_at,
                        "confidence": "derived",
                    }
                )

            occ_id = make_node_id(REGION, "occupation", SOURCE_SYSTEM, code)
            edge_id = make_edge_id(occ_id, "requires", sl_id)
            edges.append(
                {
                    "id": edge_id,
                    "src_id": occ_id,
                    "dst_id": sl_id,
                    "rel_type": "requires",
                    "region": REGION,
                    "weight": val,
                    "evidence": f"O*NET skills table scale={scale} value={val}",
                    "attrs": {"scale_id": scale, "data_value": val},
                    "source_system": SOURCE_SYSTEM,
                    "source_id": f"{code}|{eid}|{scale}",
                    "source_url": ONET_ONLINE.format(code=code),
                    "license": LICENSE,
                    "fetched_at": fetched_at,
                    "confidence": "official",
                }
            )

    # Software Skills: occupation requires software category (no numeric level)
    try:
        soft = read_table(folder, "Software Skills.txt")
        print("loaded skill table: Software Skills.txt")
        scols = {c.lower(): c for c in soft.columns}
        sc_code = next(scols[k] for k in scols if "soc" in k)
        sc_eid = next(scols[k] for k in scols if k == "element id" or "element id" in k)
        sc_ename = next(scols[k] for k in scols if "element name" in k)
        for _, row in soft.iterrows():
            code = str(row[sc_code]).strip()
            eid = str(row[sc_eid]).strip()
            ename = str(row[sc_ename]).strip()
            if not code or not eid or not ename:
                continue
            sl_source_id = f"{eid}|SW|required"
            sl_id = make_node_id(REGION, "skill_level", SOURCE_SYSTEM, sl_source_id)
            if sl_id not in skill_nodes_seen:
                skill_nodes_seen.add(sl_id)
                nodes.append(
                    {
                        "id": sl_id,
                        "region": REGION,
                        "type": "skill_level",
                        "name": f"{ename} · required",
                        "name_en": f"{ename} · required",
                        "name_zh": None,
                        "description": None,
                        "attrs": {
                            "skill_name": ename,
                            "skill_element_id": eid,
                            "scale": "software_required",
                            "level_code": "required",
                        },
                        "source_system": SOURCE_SYSTEM,
                        "source_id": sl_source_id,
                        "source_url": (
                            "https://www.onetcenter.org/dictionary/"
                            f"{version.replace('_', '.')}/excel/software_skills.html"
                        ),
                        "license": LICENSE,
                        "fetched_at": fetched_at,
                        "confidence": "derived",
                    }
                )
            occ_id = make_node_id(REGION, "occupation", SOURCE_SYSTEM, code)
            edges.append(
                {
                    "id": make_edge_id(occ_id, "requires", sl_id),
                    "src_id": occ_id,
                    "dst_id": sl_id,
                    "rel_type": "requires",
                    "region": REGION,
                    "weight": 1.0,
                    "evidence": "O*NET Software Skills.txt",
                    "attrs": None,
                    "source_system": SOURCE_SYSTEM,
                    "source_id": f"{code}|{eid}|SW",
                    "source_url": ONET_ONLINE.format(code=code),
                    "license": LICENSE,
                    "fetched_at": fetched_at,
                    "confidence": "official",
                }
            )
    except FileNotFoundError:
        pass

    conn = connect(db_path)
    try:
        n_nodes = upsert_nodes(conn, nodes)
        n_edges = upsert_edges(conn, edges)
        conn.commit()
        s = stats(conn)
    finally:
        conn.close()

    summary = {
        "source_system": SOURCE_SYSTEM,
        "version": version,
        "occupations_file_rows": int(len(occ)),
        "nodes_upserted": n_nodes,
        "edges_upserted": n_edges,
        "skill_level_nodes": len(skill_nodes_seen),
        "db_stats": s,
        "source_home": SOURCE_HOME,
        "fetched_at": fetched_at,
    }
    out = STAGING / "onet" / f"ingest_summary_{version}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest O*NET text DB into kg.sqlite")
    parser.add_argument("--version", default="30_3")
    parser.add_argument("--db", default=None, help="optional sqlite path")
    args = parser.parse_args()
    db_path = Path(args.db) if args.db else None
    summary = ingest(args.version, db_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
