"""
课目标 course → skill_level 关键词边（covers，课覆盖技能）。

curriculum_catalog 的课名与 skill 功能名/职业名重叠时建边。
confidence=ai_inferred
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

from backend.kg.graph_store import connect, stats, upsert_edges
from backend.kg.paths import REPORTS, ensure_dirs
from backend.kg.provenance import make_edge_id, utc_now_iso

REGION = "CN"


def ingest(dry_run: bool = False, limit_courses: int = 0, per_course: int = 3) -> dict:
    ensure_dirs()
    fetched_at = utc_now_iso()
    conn = connect()
    edges = []
    report = {"courses": 0, "edges": 0, "sample": []}
    try:
        courses = conn.execute(
            """
            SELECT id, name, attrs FROM nodes
            WHERE region=? AND type='course'
              AND json_extract(attrs,'$.role')='curriculum_catalog'
            """,
            (REGION,),
        ).fetchall()
        skills = conn.execute(
            """
            SELECT id, name, attrs FROM nodes
            WHERE region=? AND type='skill_level'
            """,
            (REGION,),
        ).fetchall()
        # skill tokens from name
        skill_idx = []
        for s in skills:
            sn = s["name"] or ""
            # "职业·功能·L5" 或类似
            parts = re.split(r"[·•\|]", sn)
            keys = [re.sub(r"\s+", "", p) for p in parts if len(re.sub(r"\s+", "", p)) >= 2]
            skill_idx.append((s, keys, sn))

        for i, c in enumerate(courses):
            if limit_courses and i >= limit_courses:
                break
            report["courses"] += 1
            cname = re.sub(r"\s+", "", c["name"] or "")
            if len(cname) < 2:
                continue
            scored = []
            for s, keys, sn in skill_idx:
                sc = 0
                for k in keys:
                    if len(k) >= 2 and (k in cname or cname in k):
                        sc += 2 if len(k) >= 3 else 1
                if cname[:2] in sn:
                    sc += 1
                if sc:
                    scored.append((sc, s, sn))
            scored.sort(key=lambda x: -x[0])
            n = 0
            for sc, s, sn in scored[:per_course]:
                # course covers skill
                eid = make_edge_id(c["id"], "related_to", s["id"])
                edges.append(
                    {
                        "id": eid,
                        "src_id": c["id"],
                        "dst_id": s["id"],
                        "rel_type": "related_to",
                        "region": REGION,
                        "weight": min(1.0, 0.35 + 0.1 * sc),
                        "evidence": f"课目「{c['name']}」与技能「{sn}」名称重叠",
                        "attrs": {
                            "match_method": "course_skill_keyword",
                            "link_role": "covers",
                            "score": sc,
                            "review_status": "pending",
                        },
                        "source_system": "LINK_CN_AI",
                        "source_id": f"{c['id']}->{s['id']}",
                        "source_url": "https://www.moe.gov.cn/",
                        "license": "AI/规则挂接；需抽检",
                        "fetched_at": fetched_at,
                        "confidence": "ai_inferred",
                    }
                )
                n += 1
            if n and len(report["sample"]) < 15:
                report["sample"].append({"course": c["name"], "links": n})

        report["edges"] = len(edges)
        if dry_run:
            return report
        up = upsert_edges(conn, edges)
        conn.commit()
        report["edges_upserted"] = up
        report["db_stats"] = stats(conn)
    finally:
        conn.close()
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "link_cn_course_skills.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit-courses", type=int, default=0)
    args = p.parse_args()
    print(json.dumps(ingest(args.dry_run, args.limit_courses), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
