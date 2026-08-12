"""同一（岗位/专业, 技能）只保留一条边——最高档那条，其余归档。

为什么
------
国标采集时，一个岗位对同一技能的每个等级都建了一条 requires 边
（如「汽车维修工 → 发动机检修」同时挂 L1/L2/L3，权重 0.3/0.3/0.4）。
后果有两个：

1. **前后台权重口径打架**：管理台构成页按边逐条列出、weight_sum 全量相加
   （单这一个技能就 1.0）；前台按 skill_key 聚合，只取 max。同一岗位两边数字不同。
2. **权重和普遍远超 1**：同技能多档被重复累加，490 个已发布岗位里 481 个过不了
   BR-03 的「Σweight≈1」门禁。

产品口径：一个岗位对一个技能只有**一个**要求档位 —— 高级技能天然包含低级，
要求 L3 就不必再单列 L1。国标权重的含义是「在该等级考核中的占比」，跨档相加没有意义。

策略
----
每个 (src_id, rel_type, skill_key) 组：保留 **level 最高**那条边（level 相同则取
weight 最大，再相同取 edge id 最小，保证可重复执行结果一致）；其余置 status='archived'
（逻辑删除，读路径已全线过滤，可随时回滚）。

保留最高档 = 与前台既有的 required_level=max(档) 一致，合并后前后台自然同源。

用法：python -X utf8 scripts/dedupe_skill_composition_edges.py [--apply]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import connect  # noqa: E402
from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL  # noqa: E402

# 只处理技能构成的两种边；其它关系不受影响
RELS = ("requires", "covers")


def main(apply: bool) -> int:
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT e.id AS edge_id, e.src_id, e.rel_type, e.weight,
                   o.name AS src_name,
                   ({SKILL_KEY_SQL}) AS skill_key,
                   (n.attrs::json->>'level')::int AS level
            FROM kg_edge e
            JOIN kg_node n ON n.id = e.dst_id AND n.type = 'skill_level'
            JOIN kg_node o ON o.id = e.src_id
            WHERE e.rel_type = ANY(%s)
              AND COALESCE(e.status, 'published') = 'published'
            """,
            (list(RELS),),
        ).fetchall()

        groups: dict[tuple[str, str, str], list[dict]] = {}
        for r in rows:
            groups.setdefault((r["src_id"], r["rel_type"], r["skill_key"]), []).append(dict(r))

        keep_ids: list[str] = []
        drop_ids: list[str] = []
        dup_groups = 0
        samples: list[str] = []
        by_rel: dict[str, int] = {}

        for (src, rel, key), items in groups.items():
            if len(items) == 1:
                continue
            dup_groups += 1
            by_rel[rel] = by_rel.get(rel, 0) + 1
            # 最高档优先；同档取权重大的；再同则取 id 最小 —— 保证幂等
            items.sort(
                key=lambda x: (-(x["level"] or 0), -float(x["weight"] or 0), str(x["edge_id"]))
            )
            keep, drops = items[0], items[1:]
            keep_ids.append(keep["edge_id"])
            drop_ids.extend(d["edge_id"] for d in drops)
            if len(samples) < 6:
                detail = " ".join(
                    f"L{i['level']}(w={i['weight']})" for i in items
                )
                samples.append(
                    f"  {str(keep['src_name'])[:12]:14s} {str(key)[:16]:18s} "
                    f"{detail}  → 保留 L{keep['level']}(w={keep['weight']})"
                )

        print(f"技能构成边（{'/'.join(RELS)}，published）共 {len(rows)} 条")
        print(f"(实体,关系,技能) 组合 {len(groups)} 个，其中重复 {dup_groups} 个 → {by_rel}")
        print(f"将归档 {len(drop_ids)} 条，保留 {len(keep_ids)} 条\n")
        if samples:
            print("=== 样例 ===")
            print("\n".join(samples))

        if not drop_ids:
            print("\n无需处理。")
            return 0
        if not apply:
            print("\n[dry-run] 未写库。加 --apply 执行。")
            return 0

        # 记录本次归档的 edge_id，便于精确回滚（库内原本也存在 archived 边，不能靠状态区分）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kg_edge_dedupe_log (
              edge_id text PRIMARY KEY,
              batch   text NOT NULL,
              logged_at timestamptz DEFAULT now()
            )
            """
        )
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO kg_edge_dedupe_log(edge_id, batch) VALUES (%s, 'skill_composition') "
                "ON CONFLICT (edge_id) DO NOTHING",
                [(i,) for i in drop_ids],
            )
            cur.executemany(
                "UPDATE kg_edge SET status='archived' WHERE id=%s",
                [(i,) for i in drop_ids],
            )
        conn.commit()
        print(f"\n[已提交] 归档 {len(drop_ids)} 条边（status='archived'）。")
        print("  回滚：UPDATE kg_edge SET status='published' WHERE id IN "
              "(SELECT edge_id FROM kg_edge_dedupe_log WHERE batch='skill_composition');")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--apply" in sys.argv))
