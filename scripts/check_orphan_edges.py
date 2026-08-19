"""孤儿边闸门：扫 `kg_edge` 里两端在 `kg_node` 中不存在的边，有则 exit 1。

    python -X utf8 scripts/check_orphan_edges.py
    python -X utf8 scripts/check_orphan_edges.py --fix   # 归档（不物理删）

为什么需要它：草稿态把主键换成 `(id, is_draft)`，`kg_edge` 引用 `kg_node(id)` 的两个外键
因此无法成立、只能删（方案 §1.2）。「边的两端一定存在」从数据库保证降级成应用层保证，
本脚本就是那道闸门。跑在灌库后与发布后，与 `scripts/fix_duplicate_requires.py` 一个性质。

判定分两层，因为两层的严重程度不同：

- **线上边的端点必须有线上行**。缺了就是前台会读到半截关系（列表里出现 src_name 为空的行），
  这是真故障 → exit 1。
- 草稿边允许指向只有草稿行的节点（新建专业顺手挂新建岗位就是这样），不算孤儿；
  但发布时那一步会拒（方案 §7 第 4 步），所以这里只统计、不判失败。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import connect  # noqa: E402
from backend.kg.pg_store.config import DATABASE_URL  # noqa: E402

# 线上边 → 端点必须有线上行
_ONLINE_ORPHAN = """
SELECT e.id, e.rel_type, e.src_id, e.dst_id, e.status,
       (NOT EXISTS (SELECT 1 FROM kg_node n WHERE n.id = e.src_id AND NOT n.is_draft)) AS src_missing,
       (NOT EXISTS (SELECT 1 FROM kg_node n WHERE n.id = e.dst_id AND NOT n.is_draft)) AS dst_missing
FROM kg_edge e
WHERE NOT e.is_draft
  AND (NOT EXISTS (SELECT 1 FROM kg_node n WHERE n.id = e.src_id AND NOT n.is_draft)
    OR NOT EXISTS (SELECT 1 FROM kg_node n WHERE n.id = e.dst_id AND NOT n.is_draft))
ORDER BY e.rel_type, e.id
"""

# 草稿边 → 端点连草稿行都没有，才算真的悬空
_DRAFT_DANGLING = """
SELECT e.id, e.rel_type, e.src_id, e.dst_id, e.unit_id
FROM kg_edge e
WHERE e.is_draft
  AND (NOT EXISTS (SELECT 1 FROM kg_node n WHERE n.id = e.src_id)
    OR NOT EXISTS (SELECT 1 FROM kg_node n WHERE n.id = e.dst_id))
ORDER BY e.id
"""

# 草稿边指向「只有草稿行」的节点：合法，但那个节点得先发布 —— 只提示
_DRAFT_PENDING = """
SELECT count(*) AS c FROM kg_edge e
WHERE e.is_draft
  AND (NOT EXISTS (SELECT 1 FROM kg_node n WHERE n.id = e.src_id AND NOT n.is_draft)
    OR NOT EXISTS (SELECT 1 FROM kg_node n WHERE n.id = e.dst_id AND NOT n.is_draft))
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="孤儿边闸门（外键删除后的替代）")
    ap.add_argument(
        "--fix",
        action="store_true",
        help="把线上孤儿边 status 置 archived（逻辑删除，仍留在库里可核对）",
    )
    ap.add_argument("--limit", type=int, default=20, help="最多打印几条样本")
    args = ap.parse_args()

    print(f"库：{DATABASE_URL.rsplit('@', 1)[-1]}")
    with connect() as conn:
        online = conn.execute(_ONLINE_ORPHAN).fetchall()
        dangling = conn.execute(_DRAFT_DANGLING).fetchall()
        pending = int((conn.execute(_DRAFT_PENDING).fetchone() or {"c": 0})["c"] or 0)

        print(f"线上孤儿边：{len(online)}")
        for r in online[: args.limit]:
            side = "src" if r["src_missing"] else ""
            side += "+dst" if r["dst_missing"] else ""
            print(f"  [{r['rel_type']}] {r['id']}  缺 {side or '?'}")
        if len(online) > args.limit:
            print(f"  …另有 {len(online) - args.limit} 条")

        print(f"草稿边两端完全不存在：{len(dangling)}")
        for r in dangling[: args.limit]:
            print(f"  [{r['rel_type']}] {r['id']}  unit={r['unit_id']}")

        print(f"草稿边指向「尚未发布」的节点：{pending}（合法，发布时会按依赖顺序拦下）")

        if args.fix and online:
            ids = [r["id"] for r in online]
            conn.execute(
                "UPDATE kg_edge SET status='archived' WHERE id = ANY(%s) AND NOT is_draft",
                (ids,),
            )
            conn.commit()
            print(f"已归档 {len(ids)} 条线上孤儿边（未物理删除）")

    bad = len(online) + len(dangling)
    print("PASS 无孤儿边" if not bad else f"FAIL 共 {bad} 条需要处理")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
