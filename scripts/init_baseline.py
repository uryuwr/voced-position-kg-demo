"""基准库初始化：把它收敛成「只有 published 真实数据」的 v1 基线。

基准库（8090 只读端口看的那个）与开发库（8088）的分工：

    8088  voced_kg_dev   联调、点页面、建草稿，随便脏
    8090  voced_kg       基准：**只有发布状态的真实数据**，version 从 1 起

分级执行，默认只做无争议的部分
------------------------------
`--drop-archived` 与 `--drop-unpublished` 都是**不可逆**的，所以默认关闭，
要显式打开。原因不一样：

- `archived` 里 17613 个 course 是我们**有意**归档的（课标目录 15960 条 +
  报名墙课程 1487 条），删掉就只能从 SQLite 采集库重灌
- `draft` 状态的 1176 个线上行是**采集来的真实数据**，只是没走发布流程
  （认证 470、专业 346、岗位 309）。删了要重新采，不是清垃圾

草稿行（`is_draft = true`）没有这个顾虑：它是某个人编辑到一半的中间态，
基准库里就不该有，默认删。

用法::

    python -X utf8 scripts/init_baseline.py                      # dry-run
    python -X utf8 scripts/init_baseline.py --apply              # 删草稿行 + 重置 version
    python -X utf8 scripts/init_baseline.py --apply --drop-archived --drop-unpublished
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import session


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--drop-archived", action="store_true",
                    help="物理删 archived 节点与边（不可逆；含 17613 个有意归档的 course）")
    ap.add_argument("--drop-unpublished", action="store_true",
                    help="物理删 draft/disabled 线上行（不可逆；那 1176 个是真实采集数据）")
    ap.add_argument("--reset-version", action="store_true", default=True,
                    help="version 归 1、清掉编辑痕迹（默认开）")
    args = ap.parse_args()

    steps: list[tuple[str, str]] = []

    # 1) 草稿行 —— 边先删，否则留下指向不存在草稿节点的孤儿边
    steps.append(("删草稿边", "DELETE FROM kg_edge WHERE is_draft"))
    steps.append(("删草稿节点", "DELETE FROM kg_node WHERE is_draft"))

    if args.drop_archived:
        # 边先于节点：外键 2026-08-19 已删，库不再拦孤儿边
        steps.append((
            "删 archived 边",
            "DELETE FROM kg_edge WHERE COALESCE(status,'published') = 'archived'",
        ))
        steps.append((
            "删 archived 节点的关联边",
            "DELETE FROM kg_edge e USING kg_node n "
            "WHERE (e.src_id = n.id OR e.dst_id = n.id) "
            "  AND COALESCE(n.status,'published') = 'archived'",
        ))
        steps.append((
            "删 archived 节点",
            "DELETE FROM kg_node WHERE COALESCE(status,'published') = 'archived'",
        ))

    if args.drop_unpublished:
        steps.append((
            "删 draft/disabled 节点的关联边",
            "DELETE FROM kg_edge e USING kg_node n "
            "WHERE (e.src_id = n.id OR e.dst_id = n.id) "
            "  AND COALESCE(n.status,'published') IN ('draft','disabled')",
        ))
        steps.append((
            "删 draft/disabled 节点",
            "DELETE FROM kg_node WHERE COALESCE(status,'published') IN ('draft','disabled')",
        ))
        steps.append((
            "删 draft/disabled 边",
            "DELETE FROM kg_edge WHERE COALESCE(status,'published') IN ('draft','disabled')",
        ))

    if args.reset_version:
        # 版本初始化：基线就是 v1。编辑痕迹（谁改的、待审 id）属于开发库，
        # 基准库不该带着别人的操作记录发到生产。
        steps.append((
            "version 归 1 + 清编辑痕迹",
            # pending_change_id 不是 kg_node 的列 —— 它是读路径联读待审队列时附加的
            "UPDATE kg_node SET version = 1, updated_by = NULL, updated_by_name = NULL "
            " WHERE version <> 1 OR updated_by IS NOT NULL",
        ))

    with session() as c, c.cursor() as cur:
        for label, sql in steps:
            # dry-run 用 EXPLAIN 拿不到行数，改成把 DELETE/UPDATE 的 WHERE 反用成 COUNT
            if not args.apply:
                probe = _to_count(sql)
                if probe:
                    cur.execute(probe)
                    print("  %-30s 将影响 %6d 行" % (label, cur.fetchone()["n"]))
                else:
                    print("  %-30s （无法预估）" % label)
                continue
            cur.execute(sql)
            print("  %-30s 已影响 %6d 行" % (label, cur.rowcount))

    if not args.apply:
        print("\n(dry-run。加 --apply 执行)")
        return

    with session() as c, c.cursor() as cur:
        cur.execute(
            """SELECT COALESCE(status,'published') st, count(*)::int n FROM kg_node
               GROUP BY 1 ORDER BY 2 DESC"""
        )
        print("\n初始化后节点状态：", {r["st"]: r["n"] for r in cur.fetchall()})
        cur.execute("SELECT count(*)::int n FROM kg_node WHERE is_draft")
        print("残留草稿行：", cur.fetchone()["n"])


def _to_count(sql: str) -> str | None:
    """把 DELETE/UPDATE 改写成 COUNT，用于 dry-run 预估行数。

    只处理本文件里这几种固定形态 —— 通用改写不可靠，宁可返回 None
    也不要给出一个错的数字。
    """
    s = " ".join(sql.split())
    if s.startswith("DELETE FROM kg_edge WHERE"):
        return "SELECT count(*)::int n FROM kg_edge WHERE " + s.split("WHERE", 1)[1]
    if s.startswith("DELETE FROM kg_node WHERE"):
        return "SELECT count(*)::int n FROM kg_node WHERE " + s.split("WHERE", 1)[1]
    if s.startswith("DELETE FROM kg_edge e USING kg_node n WHERE"):
        return ("SELECT count(*)::int n FROM kg_edge e, kg_node n WHERE "
                + s.split("WHERE", 1)[1])
    if s.startswith("UPDATE kg_node SET"):
        return "SELECT count(*)::int n FROM kg_node WHERE " + s.split("WHERE", 1)[1]
    return None


if __name__ == "__main__":
    main()
