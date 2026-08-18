"""下架不可学的课程资源（逻辑删除，可恢复）。

判定口径：什么叫「能真实学习」
------------------------------
学员点开链接**当场就能开始学**：免登录、免报名、无开课周期。按这条尺子量：

  能学    OFFICIAL_DOCS      官方文档/教程，静态页，随时可读
  不能学  ICOURSE163         中国大学MOOC，要登录报名；按学期开课，往期只剩介绍页
  不能学  XUETANGX           学堂在线，同上
  不能学  SEARCH_LANDING_CN  检索入口，点开是平台搜索结果页，不是具体课程
  不能学  MOE_CN(role=curriculum_catalog)
                             课标里的「课目名称」，点开是专业培养方案大类目录

踩过的坑是 `_course_kind` 里 `real` 这个名字：它当初只保证「是个课程页」，
却被当成了「点开可学」，于是 129 门 MOOC 顶着「真课」标签挂在 67 个岗位上，
学员点进去全是报名墙。分类名要跟着口径走，见 `skill_aggregate._REAL_COURSE_SOURCES`。

为什么归档而不是删
------------------
按项目的状态可见性约定标 `archived`（逻辑删除，任何接口都不返回），要恢复只需
把 status 改回 published。课标那 15960 条是 717 个专业的课程体系数据，将来做
「该专业开设哪些课」还用得上；MOOC 那批将来若接入报名跳转也还能用。

同时归档它们的边：节点 archived 但边还 published 的话，`edge_published()` 过滤
不掉那些边，图查询会画出指向不可见节点的断头箭头。

两边都要处理
------------
SQLite 是采集库、PG 是运行库，`migrate` 是 upsert 且 **ON CONFLICT 不更新
status** —— 只改 PG 的话，下次 migrate 不会把它改回来（安全），但 SQLite 里
仍是可见状态，重灌新库时会复活。所以两边都标。

用法::

    python -X utf8 scripts/archive_courses.py --role curriculum_catalog --dry-run
    python -X utf8 scripts/archive_courses.py --source ICOURSE163 XUETANGX SEARCH_LANDING_CN
    python -X utf8 scripts/archive_courses.py --source ICOURSE163 --restore   # 恢复
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import session

SQLITE = ROOT / "data" / "graph" / "kg.sqlite"

# attrs 可能是空串或非法 JSON（TEXT 无约束），强转前必须守卫，否则一条脏值报废整条语句
_ATTRS_JSON = "(CASE WHEN attrs IS NULL OR btrim(attrs) = '' THEN NULL ELSE attrs::json END)"


def build_filter(role: str | None, sources: list[str] | None) -> tuple[str, list]:
    """返回 (SQL 片段, 参数)。role 与 source 可叠加，至少给一个。"""
    where, params = [], []
    if role:
        where.append(f"{_ATTRS_JSON}->>'role' = %s")
        params.append(role)
    if sources:
        where.append("source_system = ANY(%s)")
        params.append(sources)
    return " AND ".join(where), params


def target_ids_pg(cur, cond: str, params: list, *, archived: bool) -> list[str]:
    want = "=" if archived else "<>"
    cur.execute(
        f"""SELECT id FROM kg_node
            WHERE type = 'course' AND {cond}
              AND COALESCE(status, 'published') {want} 'archived'""",
        params,
    )
    return [r["id"] for r in cur.fetchall()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", help="按 attrs.role 选，如 curriculum_catalog")
    ap.add_argument("--source", nargs="*", help="按 source_system 选，可多个")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", action="store_true", help="恢复为 published")
    args = ap.parse_args()

    if not args.role and not args.source:
        ap.error("至少给一个选择器：--role 或 --source")

    cond, params = build_filter(args.role, args.source)
    new_status = "published" if args.restore else "archived"
    verb = "恢复" if args.restore else "归档"

    with session() as c, c.cursor() as cur:
        ids = target_ids_pg(cur, cond, params, archived=args.restore)
        print(f"PG 待{verb}课程节点: {len(ids)}")
        if ids:
            cur.execute(
                """SELECT count(*)::int n FROM kg_edge
                   WHERE (src_id = ANY(%s) OR dst_id = ANY(%s))
                     AND COALESCE(status,'published') <> %s""",
                (ids, ids, new_status),
            )
            print(f"PG 待{verb}关联边: {cur.fetchone()['n']}")

        if not args.dry_run and ids:
            # 边先于节点：节点不可见但边仍 published 时，图查询会画断头箭头
            cur.execute(
                "UPDATE kg_edge SET status = %s WHERE src_id = ANY(%s) OR dst_id = ANY(%s)",
                (new_status, ids, ids),
            )
            print(f"  PG 边已{verb}: {cur.rowcount}")
            cur.execute("UPDATE kg_node SET status = %s WHERE id = ANY(%s)", (new_status, ids))
            print(f"  PG 节点已{verb}: {cur.rowcount}")

    # SQLite：采集库没有 status 列，用 attrs 打标记，供后续 migrate/采集脚本识别
    if SQLITE.exists():
        s = sqlite3.connect(SQLITE)
        sql = "SELECT id, attrs, source_system FROM nodes WHERE type='course'"
        sp: list = []
        if args.source:
            sql += " AND source_system IN (%s)" % ",".join("?" * len(args.source))
            sp += args.source
        n = 0
        for nid, attrs, _src in s.execute(sql, sp).fetchall():
            try:
                a = json.loads(attrs or "{}")
            except Exception:
                a = {}
            if args.role and a.get("role") != args.role:
                continue
            if a.get("archived") == (not args.restore):
                continue
            a["archived"] = not args.restore
            if not args.dry_run:
                s.execute("UPDATE nodes SET attrs=? WHERE id=?",
                          (json.dumps(a, ensure_ascii=False), nid))
            n += 1
        if not args.dry_run:
            s.commit()
        s.close()
        print(f"SQLite 打 archived 标记: {n}")

    if args.dry_run:
        print("\n(dry-run，未写入)")


if __name__ == "__main__":
    main()
