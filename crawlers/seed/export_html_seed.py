"""
Export IT/AI seed from AI能力知识图谱可视化.html into schema-aligned JSON + SQLite.

confidence = manual_seed
dimension note: HTML uses salary; task target uses credential — we keep courses
and leave credential placeholders empty for later official catalogs.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.graph_store import connect, stats, upsert_edges, upsert_nodes
from backend.kg.paths import ROOT as PROJECT_ROOT
from backend.kg.paths import SEEDS, ensure_dirs
from backend.kg.provenance import make_edge_id, make_node_id, utc_now_iso

HTML_PATH = PROJECT_ROOT / "AI能力知识图谱可视化.html"
SOURCE_SYSTEM = "MANUAL"
REGION = "CN"
LICENSE = "project manual seed (not official)"
SOURCE_URL = "file://AI能力知识图谱可视化.html"


def extract_js_array(html: str, var_name: str) -> str:
    # const nodes=[...];  or const edges=[...]
    m = re.search(rf"const\s+{var_name}\s*=\s*(\[)", html)
    if not m:
        raise ValueError(f"cannot find const {var_name}=")
    start = m.start(1)
    i = start
    depth = 0
    in_str = False
    str_ch = ""
    escape = False
    while i < len(html):
        ch = html[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == str_ch:
                in_str = False
        else:
            if ch in ("'", '"', "`"):
                in_str = True
                str_ch = ch
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return html[start : i + 1]
        i += 1
    raise ValueError(f"unbalanced array for {var_name}")


def js_literal_to_json(text: str) -> str:
    """Rough JS object array → JSON (keys unquoted, single quotes, // comments)."""
    # strip // line comments (not inside strings — seed file has no // in strings)
    s = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    # quote keys: {id: → {"id":
    s = re.sub(r"([{\[,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', s)
    # single-quoted strings → double-quoted
    def repl_str(m: re.Match) -> str:
        inner = m.group(1).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{inner}"'

    s = re.sub(r"'([^'\\]*(?:\\.[^'\\]*)*)'", repl_str, s)
    # trailing commas
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def map_level(level_str: str) -> str:
    mapping = {
        "基础必备": "L1",
        "基础技能": "L1",
        "辅助能力": "L1",
        "通用能力": "L2",
        "核心框架": "L2",
        "核心知识": "L3",
        "专业方向": "L3",
        "工程技能": "L3",
        "前沿技能": "L4",
        "新兴技能": "L4",
    }
    return mapping.get(level_str, "L2")


def export() -> dict:
    ensure_dirs()
    html = HTML_PATH.read_text(encoding="utf-8")
    nodes_js = extract_js_array(html, "nodes")
    edges_js = extract_js_array(html, "edges")
    nodes_raw = json.loads(js_literal_to_json(nodes_js))
    edges_raw = json.loads(js_literal_to_json(edges_js))

    fetched_at = utc_now_iso()
    id_map: dict[str, str] = {}
    out_nodes: list[dict] = []
    out_edges: list[dict] = []

    for n in nodes_raw:
        old_id = n["id"]
        ntype = n["type"]
        label = n["label"]
        info = n.get("info") or {}

        if ntype == "salary":
            # out of target five dimensions for this task — skip
            continue

        if ntype == "skill":
            level = map_level(str(info.get("level", "L2")))
            gtype = "skill_level"
            name = f"{label} · {level}"
            source_id = f"{old_id}|{level}"
            attrs = {
                "skill_name": label,
                "scale": "l1_l4",
                "level_code": level,
                "level_label": info.get("level"),
                "cat": info.get("cat") or n.get("cat"),
                "trend": info.get("trend"),
                "html_id": old_id,
            }
        elif ntype == "major":
            gtype = "major"
            name = label
            source_id = old_id
            attrs = {**info, "html_id": old_id}
        elif ntype == "job":
            gtype = "occupation"
            name = label
            source_id = old_id
            attrs = {**info, "html_id": old_id}
        elif ntype == "course":
            gtype = "course"
            name = label
            source_id = old_id
            attrs = {**info, "html_id": old_id}
        else:
            continue

        gid = make_node_id(REGION, gtype, SOURCE_SYSTEM, source_id)
        id_map[old_id] = gid
        out_nodes.append(
            {
                "id": gid,
                "region": REGION,
                "type": gtype,
                "name": name,
                "name_zh": name,
                "name_en": None,
                "description": info.get("note") or info.get("desc"),
                "attrs": attrs,
                "source_system": SOURCE_SYSTEM,
                "source_id": source_id,
                "source_url": SOURCE_URL,
                "license": LICENSE,
                "fetched_at": fetched_at,
                "confidence": "manual_seed",
            }
        )

    rel_map = {
        "培养": "prepares_for",
        "岗位需求": "requires",
        "技能关联": "related_to",
        "课程覆盖": "taught_by",
        "薪资对应": "related_to",
    }

    for e in edges_raw:
        src_old = e["source"]
        dst_old = e["target"]
        if src_old not in id_map or dst_old not in id_map:
            continue
        label = e.get("label") or ""
        # Heuristic by endpoint types in new ids
        src_id = id_map[src_old]
        dst_id = id_map[dst_old]
        src_type = src_id.split(":")[1]
        dst_type = dst_id.split(":")[1]

        if src_type == "major" and dst_type == "occupation":
            rel = "prepares_for"
        elif src_type == "occupation" and dst_type == "skill_level":
            rel = "requires"
        elif src_type == "major" and dst_type == "skill_level":
            rel = "covers"
        elif src_type == "skill_level" and dst_type == "course":
            rel = "taught_by"
        elif src_type == "course" and dst_type == "skill_level":
            # HTML may link course→skill; invert to taught_by skill→course
            src_id, dst_id = dst_id, src_id
            rel = "taught_by"
        else:
            rel = rel_map.get(label, "related_to")

        out_edges.append(
            {
                "id": make_edge_id(src_id, rel, dst_id),
                "src_id": src_id,
                "dst_id": dst_id,
                "rel_type": rel,
                "region": REGION,
                "weight": e.get("strength"),
                "evidence": f"HTML seed edge label={label}",
                "attrs": {"html_label": label},
                "source_system": SOURCE_SYSTEM,
                "source_id": f"{src_old}->{dst_old}",
                "source_url": SOURCE_URL,
                "license": LICENSE,
                "fetched_at": fetched_at,
                "confidence": "manual_seed",
            }
        )

    seed_dir = SEEDS / "it_ai"
    seed_dir.mkdir(parents=True, exist_ok=True)
    (seed_dir / "nodes.json").write_text(
        json.dumps(out_nodes, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (seed_dir / "edges.json").write_text(
        json.dumps(out_edges, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 默认只写 JSON 文件，禁止污染正式图库；需要演示库时显式 --write-db
    s = None
    wrote_db = False

    summary = {
        "nodes": len(out_nodes),
        "edges": len(out_edges),
        "seed_dir": str(seed_dir),
        "confidence": "manual_seed",
        "fetched_at": fetched_at,
        "wrote_db": wrote_db,
        "db_stats": s,
    }
    (seed_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def export_to_db() -> dict:
    """Optional: write seed into SQLite (discouraged for production graph)."""
    summary = export()
    nodes = json.loads((SEEDS / "it_ai" / "nodes.json").read_text(encoding="utf-8"))
    edges = json.loads((SEEDS / "it_ai" / "edges.json").read_text(encoding="utf-8"))
    conn = connect()
    try:
        upsert_nodes(conn, nodes)
        upsert_edges(conn, edges)
        conn.commit()
        s = stats(conn)
    finally:
        conn.close()
    summary["wrote_db"] = True
    summary["db_stats"] = s
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export HTML IT/AI seed to JSON (does not pollute kg.sqlite by default)"
    )
    parser.add_argument(
        "--write-db",
        action="store_true",
        help="Also upsert into kg.sqlite (manual_seed). Prefer NOT for production graph.",
    )
    args = parser.parse_args()
    if args.write_db:
        summary = export_to_db()
    else:
        summary = export()
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
