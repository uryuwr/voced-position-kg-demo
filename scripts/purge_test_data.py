"""清理联调/手工测试产生的脏数据。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import connect, ensure_schema


def main() -> None:
    ensure_schema()
    with connect() as conn:
        # 手工 id 前缀
        ids = [
            r["id"]
            for r in conn.execute(
                """
                SELECT id FROM kg_node
                WHERE id LIKE 'CN:manual:%'
                   OR name LIKE '%联调%'
                   OR name LIKE '%临时测试%'
                   OR name LIKE '%示例岗位%'
                   OR name LIKE '%示例专业%'
                   OR name LIKE '%管理端联调%'
                """
            ).fetchall()
        ]
        e1 = 0
        if ids:
            e1 = conn.execute(
                "DELETE FROM kg_edge WHERE src_id = ANY(%s) OR dst_id = ANY(%s)",
                (ids, ids),
            ).rowcount
            n1 = conn.execute(
                "DELETE FROM kg_node WHERE id = ANY(%s)", (ids,)
            ).rowcount
        else:
            n1 = 0
        # 旧 proposal + 新 change_request 全清（测试队列）
        try:
            p1 = conn.execute("DELETE FROM kg_proposal").rowcount
        except Exception:
            p1 = 0
        try:
            p2 = conn.execute("DELETE FROM kg_change_request").rowcount
        except Exception:
            p2 = 0
        conn.commit()
    print(
        f"purged nodes={n1} edges={e1} old_proposals={p1} change_requests={p2} node_ids={len(ids)}"
    )


if __name__ == "__main__":
    main()
