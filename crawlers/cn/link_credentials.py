"""
1+X / 证书节点 → major / occupation 挂边（无官方全表时用名称关键词）。

  major -related_to→ credential  （培养可考）
  occupation -related_to→ credential （岗位相关证）

confidence=ai_inferred，review_status=pending
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
SOURCE = "LINK_CN_AI"


def tokens(name: str) -> list[str]:
    s = re.sub(r"[（(].*?[）)]", "", name or "")
    s = re.sub(r"(证书|职业技能等级|等级|考核|认证|1\+X|试点)", "", s)
    # 拆 2+ 中文片段
    parts = re.findall(r"[\u4e00-\u9fff]{2,8}", s)
    # 去过宽词
    stop = {"技术", "应用", "管理", "服务", "系统", "工程", "专业", "能力", "综合"}
    out = []
    for p in parts:
        if p in stop:
            continue
        out.append(p)
        if len(p) >= 4:
            out.append(p[:2])
            out.append(p[-2:])
    # 去重
    seen = set()
    r = []
    for x in out:
        if x not in seen and len(x) >= 2:
            seen.add(x)
            r.append(x)
    return r[:12]


def ingest(dry_run: bool = False, per_cred: int = 5) -> dict:
    ensure_dirs()
    fetched_at = utc_now_iso()
    conn = connect()
    report = {"credentials": 0, "edges": 0, "sample": []}
    edges = []
    try:
        creds = conn.execute(
            "SELECT id, name, source_url FROM nodes WHERE region=? AND type='credential'",
            (REGION,),
        ).fetchall()
        majors = conn.execute(
            "SELECT id, name FROM nodes WHERE region=? AND type='major'",
            (REGION,),
        ).fetchall()
        occs = conn.execute(
            """
            SELECT id, name, attrs FROM nodes
            WHERE region=? AND type='occupation' AND source_system='MOHRSS_CN'
            """,
            (REGION,),
        ).fetchall()
        occ_list = []
        for o in occs:
            try:
                a = json.loads(o["attrs"] or "{}")
            except Exception:
                a = {}
            if a.get("recommend_tier") == "low":
                continue
            occ_list.append(o)

        for cr in creds:
            report["credentials"] += 1
            kws = tokens(cr["name"] or "")
            if not kws:
                continue
            scored_m = []
            for m in majors:
                mn = m["name"] or ""
                sc = sum(1 for k in kws if k in mn)
                if sc:
                    scored_m.append((sc, m))
            scored_m.sort(key=lambda x: -x[0])
            scored_o = []
            for o in occ_list:
                on = o["name"] or ""
                sc = sum(1 for k in kws if k in on)
                if sc:
                    scored_o.append((sc, o))
            scored_o.sort(key=lambda x: -x[0])

            n_m = n_o = 0
            for sc, m in scored_m[:per_cred]:
                eid = make_edge_id(m["id"], "related_to", cr["id"])
                edges.append(
                    {
                        "id": eid,
                        "src_id": m["id"],
                        "dst_id": cr["id"],
                        "rel_type": "related_to",
                        "region": REGION,
                        "weight": min(1.0, 0.4 + 0.15 * sc),
                        "evidence": f"证书「{cr['name']}」关键词对齐专业「{m['name']}」",
                        "attrs": {
                            "match_method": "credential_keyword",
                            "keywords": kws,
                            "score": sc,
                            "review_status": "pending",
                        },
                        "source_system": SOURCE,
                        "source_id": f"{m['id']}->{cr['id']}",
                        "source_url": cr["source_url"] or "https://www.moe.gov.cn/",
                        "license": "AI/规则挂接；需抽检",
                        "fetched_at": fetched_at,
                        "confidence": "ai_inferred",
                    }
                )
                n_m += 1
            for sc, o in scored_o[:per_cred]:
                eid = make_edge_id(o["id"], "related_to", cr["id"])
                edges.append(
                    {
                        "id": eid,
                        "src_id": o["id"],
                        "dst_id": cr["id"],
                        "rel_type": "related_to",
                        "region": REGION,
                        "weight": min(1.0, 0.4 + 0.15 * sc),
                        "evidence": f"证书「{cr['name']}」关键词对齐岗位「{o['name']}」",
                        "attrs": {
                            "match_method": "credential_keyword",
                            "keywords": kws,
                            "score": sc,
                            "review_status": "pending",
                        },
                        "source_system": SOURCE,
                        "source_id": f"{o['id']}->{cr['id']}",
                        "source_url": cr["source_url"] or "https://www.moe.gov.cn/",
                        "license": "AI/规则挂接；需抽检",
                        "fetched_at": fetched_at,
                        "confidence": "ai_inferred",
                    }
                )
                n_o += 1
            if len(report["sample"]) < 20:
                report["sample"].append(
                    {"credential": cr["name"], "majors": n_m, "occs": n_o, "kws": kws[:6]}
                )

        report["edges"] = len(edges)
        if dry_run:
            return report
        n = upsert_edges(conn, edges)
        conn.commit()
        report["edges_upserted"] = n
        report["db_stats"] = stats(conn)
    finally:
        conn.close()
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "link_cn_credentials.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--per-cred", type=int, default=5)
    args = p.parse_args()
    print(json.dumps(ingest(args.dry_run, args.per_cred), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
