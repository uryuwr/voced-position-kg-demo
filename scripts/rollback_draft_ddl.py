"""回滚草稿态 DDL：主键退回 (id)、加回 kg_edge 的两个外键。

    python -X utf8 scripts/rollback_draft_ddl.py            # 只体检，不动库（默认 dry-run）
    python -X utf8 scripts/rollback_draft_ddl.py --apply

**执行前必须跟运营讲清楚：所有未发布的草稿会被删掉，找不回来。**
草稿只有「当前一份」，没有 revision 留档（见方案 §9），删了就是删了。

顺序不能变，每一步都是下一步的前提：

1. 删草稿行 —— 主键退回单列 `id` 时，同一 id 的两行会撞主键，不先删则 ALTER 直接失败
2. 还原主键 (id)
3. 清孤儿边 —— 外键删除期间可能写进了两端不存在的边，不清则 ADD CONSTRAINT 失败
4. 加回外键
5. 删控制列与草稿索引，业务编码唯一索引去掉 `NOT is_draft` 条件

第 3 步会**删数据**（孤儿边本身已经是坏数据，但仍然是删），所以 dry-run 会先把
条数打出来给人看。真正跑的时候整段在一个事务里，中途失败全回滚。
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


def _scan(conn) -> dict[str, int]:
    def one(sql: str) -> int:
        row = conn.execute(sql).fetchone()
        return int((row or {}).get("c") or 0)

    return {
        "draft_nodes": one("SELECT count(*) AS c FROM kg_node WHERE is_draft"),
        "draft_edges": one("SELECT count(*) AS c FROM kg_edge WHERE is_draft"),
        "orphan_src": one(
            "SELECT count(*) AS c FROM kg_edge e WHERE NOT e.is_draft AND NOT EXISTS "
            "(SELECT 1 FROM kg_node n WHERE n.id = e.src_id AND NOT n.is_draft)"
        ),
        "orphan_dst": one(
            "SELECT count(*) AS c FROM kg_edge e WHERE NOT e.is_draft AND NOT EXISTS "
            "(SELECT 1 FROM kg_node n WHERE n.id = e.dst_id AND NOT n.is_draft)"
        ),
    }


ROLLBACK_SQL = """
DELETE FROM kg_node WHERE is_draft;
DELETE FROM kg_edge WHERE is_draft;

ALTER TABLE kg_node DROP CONSTRAINT IF EXISTS ck_kg_node_draft_status;
ALTER TABLE kg_edge DROP CONSTRAINT IF EXISTS ck_kg_edge_draft_status;

DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_index i
    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
    WHERE i.indrelid = 'kg_node'::regclass AND i.indisprimary AND a.attname = 'is_draft'
  ) THEN
    ALTER TABLE kg_node DROP CONSTRAINT kg_node_pkey;
    ALTER TABLE kg_node ADD PRIMARY KEY (id);
  END IF;
END $$;

DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_index i
    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
    WHERE i.indrelid = 'kg_edge'::regclass AND i.indisprimary AND a.attname = 'is_draft'
  ) THEN
    ALTER TABLE kg_edge DROP CONSTRAINT kg_edge_pkey;
    ALTER TABLE kg_edge ADD PRIMARY KEY (id);
  END IF;
END $$;

DELETE FROM kg_edge e WHERE NOT EXISTS (SELECT 1 FROM kg_node n WHERE n.id = e.src_id);
DELETE FROM kg_edge e WHERE NOT EXISTS (SELECT 1 FROM kg_node n WHERE n.id = e.dst_id);

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'kg_edge_src_id_fkey') THEN
    ALTER TABLE kg_edge ADD CONSTRAINT kg_edge_src_id_fkey
      FOREIGN KEY (src_id) REFERENCES kg_node(id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'kg_edge_dst_id_fkey') THEN
    ALTER TABLE kg_edge ADD CONSTRAINT kg_edge_dst_id_fkey
      FOREIGN KEY (dst_id) REFERENCES kg_node(id);
  END IF;
END $$;

DROP INDEX IF EXISTS idx_kg_node_draft;
DROP INDEX IF EXISTS idx_kg_edge_draft_unit;

DROP INDEX IF EXISTS uq_kg_node_region_type_code;
CREATE UNIQUE INDEX uq_kg_node_region_type_code
  ON kg_node(region, type, (attrs::json->>'code'))
  WHERE attrs::json->>'code' IS NOT NULL
    AND attrs::json->>'code' <> ''
    AND COALESCE(status, 'published') <> 'archived';

ALTER TABLE kg_node DROP COLUMN IF EXISTS is_draft;
ALTER TABLE kg_node DROP COLUMN IF EXISTS target_status;
ALTER TABLE kg_node DROP COLUMN IF EXISTS base_version;
ALTER TABLE kg_edge DROP COLUMN IF EXISTS is_draft;
ALTER TABLE kg_edge DROP COLUMN IF EXISTS target_status;
ALTER TABLE kg_edge DROP COLUMN IF EXISTS unit_id;
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="回滚草稿态 DDL（默认 dry-run）")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="真正执行；不带这个开关只体检",
    )
    args = ap.parse_args()

    print(f"库：{DATABASE_URL.rsplit('@', 1)[-1]}")
    with connect() as conn:
        has_col = conn.execute(
            "SELECT 1 AS x FROM information_schema.columns "
            "WHERE table_name='kg_node' AND column_name='is_draft'"
        ).fetchone()
        if not has_col:
            print("kg_node.is_draft 不存在 —— 这个库还没上过草稿态 DDL，无需回滚。")
            return 0
        s = _scan(conn)
        print(f"  草稿行：节点 {s['draft_nodes']} · 边 {s['draft_edges']}（**会被删掉**）")
        print(f"  孤儿边：src 缺 {s['orphan_src']} · dst 缺 {s['orphan_dst']}（加回外键前必须删）")
        if not args.apply:
            print("\ndry-run，未改动任何数据。确认以上数字后加 --apply 执行。")
            return 0
        conn.execute(ROLLBACK_SQL)
        conn.commit()
        left = conn.execute(
            "SELECT count(*) AS c FROM pg_constraint "
            "WHERE conname IN ('kg_edge_src_id_fkey','kg_edge_dst_id_fkey')"
        ).fetchone()
    print(f"已回滚。外键恢复 {int((left or {}).get('c') or 0)}/2。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
