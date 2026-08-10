"""
节点布局元数据：sort_order（同层稳定序）+ child_count（向下可展开数）。

child_count 语义（能力图向下）：
  industry   → 挂到本行业的 major 数（belongs_to 终点为本行业）
  major      → prepares_for 指向的 occupation 数
  occupation → requires 指向的 skill_level 数
  其它       → 0

API 投影字段名：order ← sort_order，child_count 原样。
"""
from __future__ import annotations

from typing import Any

from backend.kg.pg_store.client import connect

_PUB_E = "COALESCE(e.status, 'published') = 'published'"


def refresh_sort_order(*, region: str | None = None) -> int:
    """按 region+type 内 name,id 排序写入 sort_order（从 1 起）。"""
    with connect() as conn:
        # 保证列存在
        conn.execute(
            "ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS sort_order INT"
        )
        conn.execute(
            "ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS child_count INT NOT NULL DEFAULT 0"
        )
        conn.commit()
        if region:
            cur = conn.execute(
                """
                WITH ranked AS (
                  SELECT id,
                    ROW_NUMBER() OVER (
                      PARTITION BY region, type
                      ORDER BY name NULLS LAST, id
                    )::int AS rn
                  FROM kg_node
                  WHERE region = %s
                )
                UPDATE kg_node n SET sort_order = r.rn
                FROM ranked r WHERE n.id = r.id
                """,
                (region,),
            )
        else:
            cur = conn.execute(
                """
                WITH ranked AS (
                  SELECT id,
                    ROW_NUMBER() OVER (
                      PARTITION BY region, type
                      ORDER BY name NULLS LAST, id
                    )::int AS rn
                  FROM kg_node
                )
                UPDATE kg_node n SET sort_order = r.rn
                FROM ranked r WHERE n.id = r.id
                """
            )
        conn.commit()
        return int(cur.rowcount or 0)


def refresh_child_count(*, region: str | None = None) -> int:
    """批量重算 child_count（仅 published 边与 published 子节点）。"""
    reg_sql = "AND n.region = %s" if region else ""
    params: tuple[Any, ...] = (region,) if region else ()
    with connect() as conn:
        conn.execute(
            "ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS sort_order INT"
        )
        conn.execute(
            "ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS child_count INT NOT NULL DEFAULT 0"
        )
        conn.commit()
        # 先清零目标范围
        if region:
            conn.execute(
                "UPDATE kg_node SET child_count = 0 WHERE region = %s",
                (region,),
            )
        else:
            conn.execute("UPDATE kg_node SET child_count = 0")

        # industry ← major.belongs_to
        conn.execute(
            f"""
            UPDATE kg_node n SET child_count = s.c
            FROM (
              SELECT e.dst_id AS id, count(DISTINCT e.src_id)::int AS c
              FROM kg_edge e
              JOIN kg_node m ON m.id = e.src_id AND m.type = 'major'
                AND COALESCE(m.status, 'published') = 'published'
              WHERE e.rel_type = 'belongs_to' AND {_PUB_E}
              GROUP BY e.dst_id
            ) s
            WHERE n.id = s.id AND n.type = 'industry' {reg_sql}
            """,
            params,
        )
        # major → occupation prepares_for
        conn.execute(
            f"""
            UPDATE kg_node n SET child_count = s.c
            FROM (
              SELECT e.src_id AS id, count(DISTINCT e.dst_id)::int AS c
              FROM kg_edge e
              JOIN kg_node o ON o.id = e.dst_id AND o.type = 'occupation'
                AND COALESCE(o.status, 'published') = 'published'
              WHERE e.rel_type = 'prepares_for' AND {_PUB_E}
              GROUP BY e.src_id
            ) s
            WHERE n.id = s.id AND n.type = 'major' {reg_sql}
            """,
            params,
        )
        # occupation → skill_level requires
        conn.execute(
            f"""
            UPDATE kg_node n SET child_count = s.c
            FROM (
              SELECT e.src_id AS id, count(DISTINCT e.dst_id)::int AS c
              FROM kg_edge e
              JOIN kg_node sk ON sk.id = e.dst_id AND sk.type = 'skill_level'
                AND COALESCE(sk.status, 'published') = 'published'
              WHERE e.rel_type = 'requires' AND {_PUB_E}
              GROUP BY e.src_id
            ) s
            WHERE n.id = s.id AND n.type = 'occupation' {reg_sql}
            """,
            params,
        )
        conn.commit()
        # rowcount 不累计；再查有 child_count>0 的数量作摘要
        if region:
            c = conn.execute(
                """
                SELECT count(*) AS c FROM kg_node
                WHERE region=%s AND child_count > 0
                """,
                (region,),
            ).fetchone()["c"]
        else:
            c = conn.execute(
                "SELECT count(*) AS c FROM kg_node WHERE child_count > 0"
            ).fetchone()["c"]
        return int(c)


def refresh_layout_meta(*, region: str | None = "CN") -> dict[str, Any]:
    """全量刷新 sort_order + child_count。"""
    n_order = refresh_sort_order(region=region)
    n_child = refresh_child_count(region=region)
    return {
        "region": region,
        "sort_order_updated": n_order,
        "nodes_with_children": n_child,
    }


def ensure_layout_meta_once() -> None:
    """若尚有 sort_order 为空则自动刷一次（供服务启动调用，勿在 ensure_schema 内嵌套）。"""
    with connect() as conn:
        conn.execute(
            "ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS sort_order INT"
        )
        conn.execute(
            "ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS child_count INT NOT NULL DEFAULT 0"
        )
        conn.commit()
        row = conn.execute(
            "SELECT 1 AS x FROM kg_node WHERE sort_order IS NULL LIMIT 1"
        ).fetchone()
        need = row is not None
    if need:
        refresh_layout_meta(region=None)
