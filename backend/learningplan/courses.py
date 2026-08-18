"""技能 → 课程资源。从 `biz_store._courses_for_skill_key` 迁来，改成批量。

原实现每个技能开一条新连接跑一次查询（`docs/2026-08-14-服务端性能与架构审查.md`
第 77 条记的性能问题）：8 个技能 = 8 次建连 + 8 次往返。这里一次查完。

只在这里碰 DB —— builder 是纯函数，资源要先查好再喂给它。
"""
from __future__ import annotations

from typing import Any

from backend.kg.pg_store.client import connect
from backend.kg.pg_store.config import edge_published, node_published

# 一次查询里每个技能最多取几门课。多了对学员是噪音，也把 payload 撑大。
DEFAULT_LIMIT_PER_SKILL = 2


def courses_for_skill_keys(
    skill_keys: list[str], *, limit_per_skill: int = DEFAULT_LIMIT_PER_SKILL
) -> dict[str, list[dict[str, Any]]]:
    """批量查技能对应的课程，返回 {skill_key: [{id, name, source_url}, ...]}。

    匹配口径与原实现一致：`attrs.skill_name` / `attrs.skill_key` 精确命中，
    或 `name` 模糊包含。模糊匹配留着是因为老数据的 attrs 不全，去掉会少召回。

    没有课程的技能不会出现在返回值里（调用方按 `.get(key, [])` 取）——
    课程数据本就稀缺，"这个技能没有可学资源"是正常状态，不是错误。
    """
    keys = [k for k in {(k or "").strip() for k in skill_keys} if k]
    if not keys:
        return {}

    # DISTINCT ON 去重：一个技能可能同时有 taught_by 和 related_to 指向同一门课
    sql = f"""
        WITH want(skill_key) AS (SELECT unnest(%s::text[])),
        hit AS (
          SELECT DISTINCT w.skill_key, c.id, c.name, c.source_url,
                 -- 精确命中排在模糊命中前面，截断时先丢模糊的
                 CASE WHEN COALESCE(s.attrs::json->>'skill_key','') = w.skill_key
                        OR COALESCE(s.attrs::json->>'skill_name','') = w.skill_key
                      THEN 0 ELSE 1 END AS match_rank
          FROM want w
          JOIN kg_node s ON s.type = 'skill_level' AND {node_published('s')}
            AND (
              s.name LIKE '%%' || w.skill_key || '%%'
              OR (
                s.attrs IS NOT NULL AND btrim(s.attrs) <> ''
                AND (COALESCE(s.attrs::json->>'skill_name','') = w.skill_key
                     OR COALESCE(s.attrs::json->>'skill_key','') = w.skill_key)
              )
            )
          JOIN kg_edge e ON e.rel_type IN ('taught_by','related_to') AND {edge_published('e')}
            AND (e.src_id = s.id OR e.dst_id = s.id)
          JOIN kg_node c ON c.type = 'course' AND {node_published('c')}
            AND c.id = CASE WHEN e.src_id = s.id THEN e.dst_id ELSE e.src_id END
        )
        SELECT skill_key, id, name, source_url FROM (
          SELECT *, ROW_NUMBER() OVER (
                   PARTITION BY skill_key ORDER BY match_rank, name, id) AS rn
          FROM hit
        ) t WHERE rn <= %s
    """
    with connect() as conn:
        rows = conn.execute(sql, (keys, int(limit_per_skill))).fetchall()

    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["skill_key"], []).append(
            {"id": r["id"], "name": r["name"], "source_url": r["source_url"]}
        )
    return out
