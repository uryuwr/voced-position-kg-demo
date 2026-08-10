"""
入库 BOSS 直聘公开行业分类树 → industry 节点 + parent_of 边。

公开接口（无需登录，探测于 2026-08-06）:
  GET https://www.zhipin.com/wapi/zpCommon/data/industry.json

Usage:
  python -m crawlers.cn.ingest_boss_industry --dry-run
  python -m crawlers.cn.ingest_boss_industry
  python -m backend.kg.neo4j_store.migrate
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.graph_store import connect, stats, upsert_edges, upsert_nodes
from backend.kg.paths import RAW, REPORTS, ensure_dirs
from backend.kg.provenance import make_edge_id, make_node_id, utc_now_iso

REGION = "CN"
SOURCE_SYSTEM = "BOSS_ZHIPIN"
LICENSE = "BOSS直聘公开行业分类数据（页面筛选用）；使用时请注明来源 zhipin.com；禁止用于未授权商业转售"
API = "https://www.zhipin.com/wapi/zpCommon/data/industry.json"
HOME = "https://www.zhipin.com/"
RAW_PATH = RAW / "CN" / "industry" / "boss_industry.json"


def fetch_json() -> dict:
    req = urllib.request.Request(
        API,
        headers={
            "User-Agent": "Mozilla/5.0 EducationalKG/1.0 (research; vocational KG)",
            "Accept": "application/json",
            "Referer": HOME,
        },
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode("utf-8"))


def walk(nodes: list, parent: dict | None, level: int, out_nodes: list, out_edges: list, fetched_at: str) -> None:
    for n in nodes or []:
        code = str(n.get("code") or "")
        name = (n.get("name") or "").strip()
        if not code or not name:
            continue
        sid = f"boss:{code}"
        nid = make_node_id(REGION, "industry", SOURCE_SYSTEM, sid)
        node = {
            "id": nid,
            "region": REGION,
            "type": "industry",
            "name": name,
            "name_en": None,
            "name_zh": name,
            "description": f"BOSS直聘行业分类 · L{level}",
            "attrs": {
                "code": code,
                "level": level,
                "parent_code": str(parent["code"]) if parent else None,
                "parent_name": parent.get("name") if parent else None,
                "platform": "boss_zhipin",
                "pinyin": n.get("pinyin"),
                "first_char": n.get("firstChar"),
            },
            "source_system": SOURCE_SYSTEM,
            "source_id": sid,
            "source_url": API,
            "license": LICENSE,
            "fetched_at": fetched_at,
            "confidence": "official",  # 平台官方筛选树，非国家统计标准
        }
        # 平台分类 ≠ 国标；confidence 用 derived 更准确？用户要招聘站来源，用 official 表示「源站原生」
        # 改为 official 表示来自源站结构化字段
        out_nodes.append(node)
        if parent:
            pid = make_node_id(REGION, "industry", SOURCE_SYSTEM, f"boss:{parent['code']}")
            eid = make_edge_id(pid, "parent_of", nid)
            out_edges.append(
                {
                    "id": eid,
                    "src_id": pid,
                    "dst_id": nid,
                    "rel_type": "parent_of",
                    "region": REGION,
                    "weight": 1.0,
                    "evidence": f"BOSS行业树：{parent.get('name')} → {name}",
                    "attrs": {"link_basis": "boss_industry_tree"},
                    "source_system": SOURCE_SYSTEM,
                    "source_id": f"boss:{parent['code']}->{code}",
                    "source_url": API,
                    "license": LICENSE,
                    "fetched_at": fetched_at,
                    "confidence": "official",
                }
            )
        kids = n.get("subLevelModelList") or n.get("subList") or n.get("children") or []
        if kids:
            walk(kids, n, level + 1, out_nodes, out_edges, fetched_at)


def ingest(dry_run: bool = False, use_cache: bool = False) -> dict:
    ensure_dirs()
    (RAW / "CN" / "industry").mkdir(parents=True, exist_ok=True)
    fetched_at = utc_now_iso()
    if use_cache and RAW_PATH.exists():
        data = json.loads(RAW_PATH.read_text(encoding="utf-8"))
    else:
        data = fetch_json()
        RAW_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    report = {
        "source_system": SOURCE_SYSTEM,
        "api": API,
        "fetched_at": fetched_at,
        "dry_run": dry_run,
        "api_code": data.get("code"),
        "api_message": data.get("message"),
    }
    if data.get("code") != 0:
        report["error"] = "api non-zero code"
        return report

    tree = data.get("zpData") or []
    nodes: list[dict] = []
    edges: list[dict] = []
    walk(tree, None, 1, nodes, edges, fetched_at)
    # dedupe
    nodes = list({n["id"]: n for n in nodes}.values())
    edges = list({e["id"]: e for e in edges}.values())
    report["nodes_parsed"] = len(nodes)
    report["edges_parsed"] = len(edges)
    report["sample"] = [{"name": n["name"], "code": n["attrs"]["code"], "level": n["attrs"]["level"]} for n in nodes[:12]]

    if dry_run:
        return report

    conn = connect()
    try:
        nu = upsert_nodes(conn, nodes)
        eu = upsert_edges(conn, edges)
        conn.commit()
        report["nodes_upserted"] = nu
        report["edges_upserted"] = eu
        report["db_stats"] = stats(conn)
    finally:
        conn.close()

    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "ingest_cn_boss_industry.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--use-cache", action="store_true", help="只用本地 boss_industry.json")
    args = p.parse_args()
    print(json.dumps(ingest(args.dry_run, args.use_cache), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
