"""
为尚无 learnable 的「有专→课」专业，生成可点击搜索落地页作为可学资源入口。

URL 指向国家智慧教育/大学 MOOC 公开搜索页（非假造课程详情，而是合法检索入口）。
attrs.role=learnable_resource, playable=true, match_method=search_landing
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.graph_store import connect, stats, upsert_edges, upsert_nodes
from backend.kg.paths import REPORTS, ensure_dirs
from backend.kg.provenance import make_edge_id, make_node_id, utc_now_iso

REGION = "CN"
SOURCE = "SEARCH_LANDING_CN"
LICENSE = "公开检索入口（智慧教育/爱课程搜索）；非单课详情页，使用时请跳转后自选资源"


def search_url(name: str, platform: str) -> str:
    q = urllib.parse.quote(name)
    if platform == "icourse163":
        return f"https://www.icourse163.org/search.htm?search={q}"
    # 国家职业教育智慧教育平台检索
    return f"https://vocational.smartedu.cn/#/search?keyword={q}"


def ingest(dry_run: bool = False, per_major: int = 3, max_new: int = 800) -> dict:
    ensure_dirs()
    fetched_at = utc_now_iso()
    conn = connect()
    report = {"candidates": 0, "nodes": 0, "edges": 0, "sample": []}
    nodes: list[dict] = []
    edges: list[dict] = []
    try:
        # 已有 learnable 名称
        existing = {
            r[0]
            for r in conn.execute(
                """
                SELECT name FROM nodes
                WHERE region=? AND type='course'
                  AND json_extract(attrs,'$.role')='learnable_resource'
                """,
                (REGION,),
            )
        }
        # 专业→课目标（core 优先）
        rows = conn.execute(
            """
            SELECT m.id mid, m.name mname, c.id cid, c.name cname,
                   json_extract(c.attrs,'$.course_kind') kind
            FROM edges e
            JOIN nodes m ON m.id=e.src_id AND m.type='major' AND m.region=?
            JOIN nodes c ON c.id=e.dst_id AND c.type='course'
            WHERE e.rel_type='related_to' AND e.region=?
              AND json_extract(c.attrs,'$.role')='curriculum_catalog'
            ORDER BY m.name, CASE json_extract(c.attrs,'$.course_kind')
              WHEN 'core' THEN 0 WHEN 'foundation' THEN 1 ELSE 2 END
            """,
            (REGION, REGION),
        ).fetchall()

        per_m: dict[str, int] = {}
        for mid, mname, cid, cname, kind in rows:
            if report["nodes"] >= max_new:
                break
            if not cname or len(cname) < 2 or cname in existing:
                continue
            # 跳过明显脏名
            if any(x in cname for x in ("标准", "实训基地", "师风", "页", "附录")):
                continue
            if per_m.get(mid, 0) >= per_major:
                continue
            report["candidates"] += 1
            for plat in ("smartedu", "icourse163"):
                if report["nodes"] >= max_new:
                    break
                sid = f"search:{plat}:{cname[:80]}"
                nid = make_node_id(REGION, "course", SOURCE, sid)
                url = search_url(cname, plat)
                node = {
                    "id": nid,
                    "region": REGION,
                    "type": "course",
                    "name": f"{cname}（{('智慧教育' if plat=='smartedu' else '大学MOOC')}检索）",
                    "name_en": None,
                    "name_zh": cname,
                    "description": f"可学入口：在公开平台检索「{cname}」· 关联专业 {mname}",
                    "attrs": {
                        "role": "learnable_resource",
                        "playable": True,
                        "resource_type": "search_landing",
                        "platform": plat,
                        "query": cname,
                        "major_name": mname,
                        "from_curriculum": True,
                        "course_kind": kind,
                    },
                    "source_system": SOURCE,
                    "source_id": sid,
                    "source_url": url,
                    "license": LICENSE,
                    "fetched_at": fetched_at,
                    "confidence": "derived",
                }
                nodes.append(node)
                # major -related_to→ learnable
                edges.append(
                    {
                        "id": make_edge_id(mid, "related_to", nid),
                        "src_id": mid,
                        "dst_id": nid,
                        "rel_type": "related_to",
                        "region": REGION,
                        "weight": 0.55,
                        "evidence": f"课目标「{cname}」→公开检索可学入口（{plat}）",
                        "attrs": {
                            "match_method": "curriculum_search_landing",
                            "review_status": "pending",
                        },
                        "source_system": SOURCE,
                        "source_id": f"{mid}->{sid}",
                        "source_url": url,
                        "license": LICENSE,
                        "fetched_at": fetched_at,
                        "confidence": "derived",
                    }
                )
                # curriculum -related_to→ learnable
                edges.append(
                    {
                        "id": make_edge_id(cid, "related_to", nid),
                        "src_id": cid,
                        "dst_id": nid,
                        "rel_type": "related_to",
                        "region": REGION,
                        "weight": 0.7,
                        "evidence": f"课目标对齐检索资源「{cname}」",
                        "attrs": {
                            "match_method": "curriculum_search_landing",
                            "link_role": "same_title_search",
                            "review_status": "pending",
                        },
                        "source_system": SOURCE,
                        "source_id": f"{cid}->{sid}",
                        "source_url": url,
                        "license": LICENSE,
                        "fetched_at": fetched_at,
                        "confidence": "derived",
                    }
                )
                report["nodes"] += 1
                existing.add(cname)
            per_m[mid] = per_m.get(mid, 0) + 1
            if len(report["sample"]) < 12:
                report["sample"].append({"major": mname, "course": cname})

        report["edges"] = len(edges)
        if dry_run:
            return report
        # dedupe
        by_id = {n["id"]: n for n in nodes}
        nodes = list(by_id.values())
        eby = {e["id"]: e for e in edges}
        edges = list(eby.values())
        nu = upsert_nodes(conn, nodes)
        eu = upsert_edges(conn, edges)
        conn.commit()
        report["nodes_upserted"] = nu
        report["edges_upserted"] = eu
        report["db_stats"] = stats(conn)
    finally:
        conn.close()
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "seed_cn_learnable_search.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--per-major", type=int, default=3)
    p.add_argument("--max-new", type=int, default=800)
    args = p.parse_args()
    print(json.dumps(ingest(args.dry_run, args.per_major, args.max_new), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
