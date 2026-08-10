"""只读体检：对照 docs/知识图谱重构总方案.md 看 S1–S4 的数据侧落地情况。

用法：python scripts/_check_refactor_progress.py
"""
from __future__ import annotations

from backend.kg.pg_store.client import pg_conn

TARGET_RELS = [
    "contain",
    "advances_to",
    "offers_course",
    "covers",
    "skill_progress",
    "taught_by",
    "recognized_by",
    "articulates_to",
]


def main() -> None:
    with pg_conn() as conn:
        cur = conn.cursor()

        cur.execute("SELECT rel_type, count(*) FROM kg_edge GROUP BY 1 ORDER BY 2 DESC")
        rels = cur.fetchall()
        print("== rel_type 计数 ==")
        for r in rels:
            print(f"  {r[0]:<18} {r[1]}")

        have = {r[0] for r in rels}
        print("\n== 方案新增/目标边是否存在 ==")
        for t in TARGET_RELS:
            print(f"  {t:<18} {'有' if t in have else '缺'}")

        cur.execute("SELECT structure_layer, count(*) FROM kg_edge GROUP BY 1")
        print("\n== structure_layer ==")
        for r in cur.fetchall():
            print(f"  {str(r[0]):<18} {r[1]}")

        cur.execute(
            "SELECT type, count(*) FILTER (WHERE level IS NOT NULL), "
            "count(*) FILTER (WHERE category IS NOT NULL), count(*) "
            "FROM kg_node GROUP BY 1 ORDER BY 4 DESC"
        )
        print("\n== 节点 type / 有 level / 有 category / 总数 ==")
        for r in cur.fetchall():
            print(f"  {r[0]:<14} {r[1]:>7} {r[2]:>7} {r[3]:>8}")

        cur.execute(
            "SELECT count(*) FROM kg_edge WHERE attrs ? 'migrated_from'"
        )
        print(f"\n== 迁移痕迹 attrs.migrated_from ==\n  {cur.fetchone()[0]}")

        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name LIKE 'kg\\_%\\_bak%' ORDER BY 1"
        )
        baks = [r[0] for r in cur.fetchall()]
        print(f"\n== 备份表 ==\n  {baks or '（无）'}")


if __name__ == "__main__":
    main()
