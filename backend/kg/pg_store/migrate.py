"""
Migrate graph from SQLite → PostgreSQL.

Default scope: region=CN only (nodes + edges).

Usage:
  python -m backend.kg.pg_store.migrate
  python -m backend.kg.pg_store.migrate --clear
  python -m backend.kg.pg_store.migrate --region CN
  python -m backend.kg.pg_store.migrate --region all
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.level_scale import normalize_nodes
from backend.kg.pg_store.client import connect, ensure_schema, verify_connectivity
from backend.kg.pg_store.config import DEFAULT_REGION, SQLITE_PATH

NODE_COLS = (
    "id",
    "region",
    "type",
    "name",
    "name_en",
    "name_zh",
    "aliases",
    "description",
    "attrs",
    "source_system",
    "source_id",
    "source_url",
    "license",
    "fetched_at",
    "confidence",
)
EDGE_COLS = (
    "id",
    "src_id",
    "dst_id",
    "rel_type",
    "region",
    "weight",
    "evidence",
    "attrs",
    "source_system",
    "source_id",
    "source_url",
    "license",
    "fetched_at",
    "confidence",
)


def _load_sqlite(path: Path, region: str | None) -> tuple[list[dict], list[dict]]:
    if not path.exists():
        raise FileNotFoundError(f"SQLite not found: {path}")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        if region and region.lower() not in ("all", "*", ""):
            nodes = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM nodes WHERE region = ?", (region,)
                )
            ]
            node_ids = {n["id"] for n in nodes}
            # edges: region match OR both endpoints in CN node set
            edges_raw = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM edges WHERE region = ?", (region,)
                )
            ]
            # drop edges whose endpoints missing (orphan)
            edges = [
                e
                for e in edges_raw
                if e["src_id"] in node_ids and e["dst_id"] in node_ids
            ]
        else:
            nodes = [dict(r) for r in conn.execute("SELECT * FROM nodes")]
            edges = [dict(r) for r in conn.execute("SELECT * FROM edges")]
    finally:
        conn.close()
    return nodes, edges


def _norm_json_field(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v) if v != "" else None


def _node_tuple(n: dict[str, Any]) -> tuple:
    return (
        n["id"],
        n["region"],
        n["type"],
        n["name"],
        n.get("name_en"),
        n.get("name_zh"),
        _norm_json_field(n.get("aliases")),
        n.get("description"),
        _norm_json_field(n.get("attrs")),
        n["source_system"],
        n["source_id"],
        n["source_url"],
        n["license"],
        n["fetched_at"],
        n["confidence"],
    )


def _edge_tuple(e: dict[str, Any]) -> tuple:
    return (
        e["id"],
        e["src_id"],
        e["dst_id"],
        e["rel_type"],
        e["region"],
        e.get("weight"),
        e.get("evidence"),
        _norm_json_field(e.get("attrs")),
        e["source_system"],
        e.get("source_id"),
        e["source_url"],
        e["license"],
        e["fetched_at"],
        e["confidence"],
    )


def clear_graph(conn) -> None:
    conn.execute("TRUNCATE kg_edge, kg_node")


def insert_nodes(conn, nodes: list[dict], batch_size: int = 2000) -> int:
    # is_draft=false 是显式写的，不靠列默认值：灌库是离线采集，不是运营编辑（方案 §4），
    # 落的必须是**线上行**。冲突目标同时带上 is_draft，才不会去撞同一 id 的草稿行 ——
    # 主键是 (id, is_draft)，只写 `ON CONFLICT (id)` 会直接报
    # "there is no unique or exclusion constraint matching"。
    sql = f"""
    INSERT INTO kg_node ({", ".join(NODE_COLS)}, is_draft)
    VALUES ({", ".join(["%s"] * len(NODE_COLS))}, false)
    ON CONFLICT (id, is_draft) DO UPDATE SET
      name = EXCLUDED.name,
      name_en = EXCLUDED.name_en,
      name_zh = EXCLUDED.name_zh,
      aliases = EXCLUDED.aliases,
      description = EXCLUDED.description,
      attrs = EXCLUDED.attrs,
      source_url = EXCLUDED.source_url,
      license = EXCLUDED.license,
      fetched_at = EXCLUDED.fetched_at,
      confidence = EXCLUDED.confidence
    """
    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(nodes), batch_size):
            batch = [_node_tuple(n) for n in nodes[i : i + batch_size]]
            cur.executemany(sql, batch)
            total += len(batch)
    return total


def insert_edges(conn, edges: list[dict], batch_size: int = 2000) -> int:
    sql = f"""
    INSERT INTO kg_edge ({", ".join(EDGE_COLS)}, is_draft)
    VALUES ({", ".join(["%s"] * len(EDGE_COLS))}, false)
    ON CONFLICT (id, is_draft) DO UPDATE SET
      weight = EXCLUDED.weight,
      evidence = EXCLUDED.evidence,
      attrs = EXCLUDED.attrs,
      source_url = EXCLUDED.source_url,
      license = EXCLUDED.license,
      fetched_at = EXCLUDED.fetched_at,
      confidence = EXCLUDED.confidence
    """
    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(edges), batch_size):
            batch = [_edge_tuple(e) for e in edges[i : i + batch_size]]
            cur.executemany(sql, batch)
            total += len(batch)
    return total


def stats(conn=None) -> dict[str, Any]:
    """图规模统计（`/v1/stats` 与灌库末尾的对账都读它）。

    **只数线上行**：草稿行是「同一条记录的另一个版本」，不是新数据。不排除的话
    ① 运营编辑一下前台的总节点数就跟着涨（方案 §12 的「前台逐字节不变」当场破）；
    ② 本文件末尾 `s["nodes"] == len(nodes)` 的对账会因为草稿行而 MISMATCH，
       灌库脚本 exit 1 —— 明明灌对了却报失败。
    """
    own = conn is None
    c = conn or connect()
    try:
        node_total = c.execute(
            "SELECT COUNT(*) AS c FROM kg_node WHERE NOT is_draft"
        ).fetchone()["c"]
        edge_total = c.execute(
            "SELECT COUNT(*) AS c FROM kg_edge WHERE NOT is_draft"
        ).fetchone()["c"]
        by_type = {
            r["type"]: r["c"]
            for r in c.execute(
                "SELECT type, COUNT(*) AS c FROM kg_node WHERE NOT is_draft "
                "GROUP BY type ORDER BY type"
            )
        }
        by_region = {
            r["region"]: r["c"]
            for r in c.execute(
                # 三个分组统计都要 ORDER BY：不排的话 PG 的 HashAggregate 输出顺序
                # 不保证稳定，同样的数据两次请求能给出不同的 JSON 键序，
                # 「编辑前后前台响应逐字节相同」这类断言会随机翻红
                "SELECT region, COUNT(*) AS c FROM kg_node WHERE NOT is_draft "
                "GROUP BY region ORDER BY region"
            )
        }
        by_rel = {
            r["rel_type"]: r["c"]
            for r in c.execute(
                "SELECT rel_type, COUNT(*) AS c FROM kg_edge WHERE NOT is_draft "
                "GROUP BY rel_type ORDER BY rel_type"
            )
        }
        by_conf = {
            r["confidence"]: r["c"]
            for r in c.execute(
                "SELECT confidence, COUNT(*) AS c FROM kg_edge WHERE NOT is_draft "
                "GROUP BY confidence ORDER BY confidence"
            )
        }
        return {
            "engine": "postgresql",
            "nodes": int(node_total),
            "edges": int(edge_total),
            "nodes_by_type": by_type,
            "nodes_by_region": by_region,
            "edges_by_rel_type": by_rel,
            "edges_by_confidence": by_conf,
        }
    finally:
        if own:
            c.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLite → PostgreSQL KG migrate")
    parser.add_argument("--sqlite", type=Path, default=SQLITE_PATH)
    parser.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help="CN (default) | all",
    )
    parser.add_argument("--clear", action="store_true", help="TRUNCATE before load")
    args = parser.parse_args()

    region = (args.region or "CN").strip()
    print(f"pg: {verify_connectivity()}")
    print(f"load sqlite: {args.sqlite} region={region}")
    t0 = time.time()
    nodes, edges = _load_sqlite(args.sqlite, region if region.lower() != "all" else None)
    print(f"  sqlite subset nodes={len(nodes)} edges={len(edges)}")

    # 技能等级归一。**必须在这里做，不能指望源 SQLite 已经是对的**：
    # 下面 insert_nodes 是 `attrs = EXCLUDED.attrs` 无条件覆盖，源里少一个
    # attrs.level，库里已有的产品档就被抹掉，而且不报错。2026-08-14 手工回填的
    # 8919 个节点就是这么在 08-18 一声不响地退回原样的（可评分岗位 608 → 117）。
    lv = normalize_nodes(nodes)
    print(f"  skill_level 归一: 转换 {lv['ok']} · 已就绪 {lv['skip']} · 判定不了 {lv['unresolved']}")
    if lv["unresolved"]:
        print(f"  [警告] {lv['unresolved']} 个 skill_level 既无等级名也无可识别原码，"
              f"按原样入库；这些节点的岗位算不出匹配度")

    with connect() as conn:
        ensure_schema(conn)
        if args.clear:
            clear_graph(conn)
            print("  cleared kg_node / kg_edge")
        n = insert_nodes(conn, nodes)
        e = insert_edges(conn, edges)
        conn.commit()
        s = stats(conn)

    elapsed = round(time.time() - t0, 2)
    print(f"  inserted nodes={n} edges={e} in {elapsed}s")
    print(json.dumps(s, ensure_ascii=False, indent=2))

    # integrity check vs source subset
    ok = s["nodes"] == len(nodes) and s["edges"] == len(edges)
    print("ACCEPT" if ok else "MISMATCH", f"pg_nodes={s['nodes']} src={len(nodes)} pg_edges={s['edges']} src={len(edges)}")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
