"""
将已解析技能标准的 requires，按岗位名称相似度传播到同名/近名岗位（大典细类）。

例：标准挂在「云计算工程技术人员」上 → 名称含「云计算」的其他岗位也可挂同一批 skill（降权、ai_inferred）。
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


def core_tokens(name: str) -> list[str]:
    s = re.sub(r"[（(].*?[）)]", "", name or "")
    s = re.sub(r"(工程技术人员|技术人员|师|员|工)$", "", s)
    parts = re.findall(r"[\u4e00-\u9fff]{2,6}", s)
    stop = {"技术", "工程", "系统", "应用", "管理", "服务"}
    return [p for p in parts if p not in stop][:4]


def ingest(dry_run: bool = False, max_extra_occ: int = 8) -> dict:
    ensure_dirs()
    fetched_at = utc_now_iso()
    conn = connect()
    report = {"seed_occs": 0, "edges": 0, "sample": []}
    edges = []
    try:
        # 已有 requires 的岗位
        seeds = conn.execute(
            """
            SELECT DISTINCT n.id, n.name FROM nodes n
            JOIN edges e ON e.src_id=n.id
            WHERE n.region=? AND n.type='occupation' AND e.rel_type='requires'
            """,
            (REGION,),
        ).fetchall()
        all_occ = conn.execute(
            """
            SELECT id, name, attrs FROM nodes
            WHERE region=? AND type='occupation' AND source_system='MOHRSS_CN'
            """,
            (REGION,),
        ).fetchall()
        has_req = {s["id"] for s in seeds}

        for seed in seeds:
            report["seed_occs"] += 1
            toks = core_tokens(seed["name"] or "")
            if not toks:
                continue
            # 该岗全部 skill
            skills = conn.execute(
                """
                SELECT dst_id, weight, evidence, source_url, source_id FROM edges
                WHERE src_id=? AND rel_type='requires' AND region=?
                """,
                (seed["id"], REGION),
            ).fetchall()
            # 找近名岗
            cands = []
            for o in all_occ:
                if o["id"] in has_req:
                    continue
                try:
                    a = json.loads(o["attrs"] or "{}")
                except Exception:
                    a = {}
                if a.get("recommend_tier") == "low":
                    continue
                on = o["name"] or ""
                sc = sum(1 for t in toks if t in on)
                if sc >= 1 and (toks[0] in on if toks else False):
                    cands.append((sc, o))
            cands.sort(key=lambda x: -x[0])
            added = 0
            for sc, o in cands[:max_extra_occ]:
                for sk in skills:
                    eid = make_edge_id(o["id"], "requires", sk["dst_id"])
                    edges.append(
                        {
                            "id": eid,
                            "src_id": o["id"],
                            "dst_id": sk["dst_id"],
                            "rel_type": "requires",
                            "region": REGION,
                            "weight": (sk["weight"] or 0.5) * 0.7,
                            "evidence": (
                                f"由「{seed['name']}」技能标准 requires 名称传播至「{o['name']}」"
                            ),
                            "attrs": {
                                "match_method": "requires_name_propagate",
                                "from_occupation": seed["name"],
                                "score": sc,
                                "review_status": "pending",
                            },
                            "source_system": "LINK_CN_AI",
                            "source_id": f"prop:{seed['id']}->{o['id']}->{sk['dst_id'][-20:]}",
                            "source_url": sk["source_url"] or "https://www.mohrss.gov.cn/",
                            "license": "规则传播；需抽检",
                            "fetched_at": fetched_at,
                            "confidence": "ai_inferred",
                        }
                    )
                added += 1
                has_req.add(o["id"])
                if len(report["sample"]) < 20:
                    report["sample"].append(
                        {"from": seed["name"], "to": o["name"], "skills": len(skills)}
                    )
            report["edges"] = len(edges)

        if dry_run:
            return report
        eby = {e["id"]: e for e in edges}
        edges = list(eby.values())
        n = upsert_edges(conn, edges)
        conn.commit()
        report["edges_upserted"] = n
        report["db_stats"] = stats(conn)
    finally:
        conn.close()
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "link_cn_requires_propagate.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    print(json.dumps(ingest(args.dry_run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
