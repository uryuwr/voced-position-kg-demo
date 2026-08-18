"""清洗已入库的 taught_by 错配边（技能 ↔ 课程名不相关）。

为什么需要
----------
课程采集初版的相关性规则太宽，只要技能名与课程名共享 ≥2 字词片就收，
于是通用词「开发」把无关课程放了进来：

    SpringBoot开发  ←  Linux开发环境及应用 / Python游戏开发入门 / Web前端开发

规则已在 `harvest_mooc_courses.is_relevant` 修正（加停用词表），但**已落库的边
不会自动消失**，必须回洗。这类脏数据不会报错，只会让学员在岗位详情里看到
莫名其妙的推荐课程 —— 属于「静默错误」，比崩溃更难发现。

只清 LLM/规则匹配来源的边（ICOURSE163 / XUETANGX / SEARCH_LANDING_CN），
不动课标 official 边（MOE_CN 的 offers_course 体系另有出处）。

用法::

    python -X utf8 scripts/purge_irrelevant_course_edges.py --dry-run
    python -X utf8 scripts/purge_irrelevant_course_edges.py
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

from crawlers.cn.harvest_mooc_courses import is_relevant, query_variants

SQLITE = ROOT / "data" / "graph" / "kg.sqlite"
TARGET_SOURCES = ("ICOURSE163", "XUETANGX")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(SQLITE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""
        SELECT e.id AS eid, n.attrs AS skill_attrs, n.name AS skill_name,
               c.name AS course_name, c.source_system AS platform
        FROM edges e
        JOIN nodes n ON n.id = e.src_id AND n.type = 'skill_level'
        JOIN nodes c ON c.id = e.dst_id AND c.type = 'course'
        WHERE e.rel_type = 'taught_by'
          AND c.source_system IN ({','.join('?' * len(TARGET_SOURCES))})
        """,
        TARGET_SOURCES,
    ).fetchall()

    keep, drop = 0, []
    for r in rows:
        try:
            a = json.loads(r["skill_attrs"] or "{}")
        except Exception:
            a = {}
        key = a.get("skill_key") or str(r["skill_name"] or "").split(" · ")[0]
        # 用与采集侧同一套判定：任一查询变体判定相关即保留
        ok = any(is_relevant(key, q, r["course_name"]) for q in query_variants(key))
        if ok:
            keep += 1
        else:
            drop.append({"edge": r["eid"], "skill": key,
                         "course": r["course_name"], "platform": r["platform"]})

    print("检查 taught_by 边: %d" % len(rows))
    print("  保留: %d" % keep)
    print("  待删: %d" % len(drop))
    print()
    for d in drop[:15]:
        print("  ✗ %-18s ← %-34s (%s)" % (d["skill"][:18], d["course"][:34], d["platform"]))

    if not args.dry_run and drop:
        conn.executemany("DELETE FROM edges WHERE id=?", [(d["edge"],) for d in drop])
        conn.commit()
        print()
        print("已删除 %d 条错配边" % len(drop))

        # 删完可能有课程节点变成孤儿，一并清掉，避免图里留下不可达节点
        orphan = conn.execute(
            f"""
            SELECT id FROM nodes
            WHERE type='course' AND source_system IN ({','.join('?' * len(TARGET_SOURCES))})
              AND id NOT IN (SELECT dst_id FROM edges WHERE rel_type='taught_by')
            """,
            TARGET_SOURCES,
        ).fetchall()
        if orphan:
            conn.executemany("DELETE FROM nodes WHERE id=?", [(o["id"],) for o in orphan])
            conn.commit()
            print("顺带清理孤儿课程节点 %d 个" % len(orphan))
    conn.close()


if __name__ == "__main__":
    main()
