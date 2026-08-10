"""
将可学资源（learnable_resource）按专业大类/课名关键词挂到 major / skill_level。

confidence=ai_inferred|derived
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
from backend.kg.paths import REPORTS, ensure_dirs
from backend.kg.provenance import make_edge_id, utc_now_iso

REGION = "CN"

# 专业大类中文 → major name 关键词
CAT_TO_MAJOR_KW = {
    "电子与信息": ["计算机", "软件", "网络", "大数据", "人工智能", "物联网", "电子", "通信", "信息"],
    "装备制造": ["机械", "数控", "机电", "智能制造", "工业", "模具", "焊接"],
    "交通运输": ["汽车", "轨道", "航空", "物流", "道路", "航海"],
    "财经商贸": ["会计", "金融", "电子商务", "市场营销", "商贸", "财务"],
    "农林牧渔": ["农业", "园艺", "畜牧", "林业", "水产"],
    "医药卫生": ["护理", "药学", "医学", "康复", "口腔"],
    "土木建筑": ["建筑", "工程造价", "土木", "园林", "市政"],
    "食品药品": ["食品", "药品", "生物"],
    "能源动力": ["电气", "能源", "电力", "新能源"],
}


def ingest(dry_run: bool = False) -> dict:
    ensure_dirs()
    fetched_at = utc_now_iso()
    conn = connect()
    edges = []
    report = {"resources": 0, "edges": 0, "sample": []}
    try:
        resources = conn.execute(
            """
            SELECT id, name, attrs, source_url FROM nodes
            WHERE region=? AND type='course' AND attrs LIKE '%learnable_resource%'
            """,
            (REGION,),
        ).fetchall()
        majors = conn.execute(
            "SELECT id, name FROM nodes WHERE region=? AND type='major'",
            (REGION,),
        ).fetchall()
        skills = conn.execute(
            "SELECT id, name FROM nodes WHERE region=? AND type='skill_level' AND region='CN'",
            (REGION,),
        ).fetchall()

        for res in resources:
            report["resources"] += 1
            attrs = {}
            try:
                attrs = json.loads(res["attrs"] or "{}")
            except Exception:
                pass
            cat = attrs.get("major_category") or ""
            kws = []
            for ck, mks in CAT_TO_MAJOR_KW.items():
                if ck in cat:
                    kws = mks
                    break
            # 也用资源名
            rname = res["name"] or ""
            for mk in ["计算机", "网络", "软件", "数控", "汽车", "电气", "护理", "会计"]:
                if mk in rname:
                    kws.append(mk)
            kws = list(dict.fromkeys(kws))
            if not kws:
                continue
            # 挂专业（最多 5）
            linked = 0
            for m in majors:
                if any(k in (m["name"] or "") for k in kws):
                    eid = make_edge_id(m["id"], "related_to", res["id"])
                    edges.append(
                        {
                            "id": eid,
                            "src_id": m["id"],
                            "dst_id": res["id"],
                            "rel_type": "related_to",
                            "region": REGION,
                            "weight": 0.6,
                            "evidence": f"可学资源「{rname}」按大类/关键词挂专业「{m['name']}」",
                            "attrs": {"match_method": "category_keyword", "review_status": "pending"},
                            "source_system": "LINK_CN_AI",
                            "source_id": f"{m['id']}->{res['id']}",
                            "source_url": res["source_url"] or "https://vocational.smartedu.cn/",
                            "license": "AI/规则挂接；需抽检",
                            "fetched_at": fetched_at,
                            "confidence": "ai_inferred",
                        }
                    )
                    linked += 1
                    if linked >= 5:
                        break
            # 挂技能（名称重叠）
            sl = 0
            for s in skills:
                sn = s["name"] or ""
                if any(k in sn for k in kws) or any(k in rname and k in sn for k in ["网络", "软件", "数控", "数据"]):
                    eid = make_edge_id(s["id"], "taught_by", res["id"])
                    edges.append(
                        {
                            "id": eid,
                            "src_id": s["id"],
                            "dst_id": res["id"],
                            "rel_type": "taught_by",
                            "region": REGION,
                            "weight": 0.5,
                            "evidence": f"技能「{sn}」与可学资源「{rname}」关键词相关",
                            "attrs": {"match_method": "keyword", "review_status": "pending"},
                            "source_system": "LINK_CN_AI",
                            "source_id": f"{s['id']}->{res['id']}",
                            "source_url": res["source_url"] or "https://vocational.smartedu.cn/",
                            "license": "AI/规则挂接；需抽检",
                            "fetched_at": fetched_at,
                            "confidence": "ai_inferred",
                        }
                    )
                    sl += 1
                    if sl >= 3:
                        break
            if report["sample"].__len__() < 15:
                report["sample"].append({"resource": rname, "major_links": linked, "skill_links": sl})

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
    (REPORTS / "link_cn_course_resources.json").write_text(
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
