"""归档「总计」这类**权重表汇总行**——它们不是技能。

国标的「工作要求」章节末尾有一行合计（各技能权重加起来 100%），采集时被当成一条
技能抓了进来。后果：
- 测评会煞有介事地为「总计」出一道情景判断题
- 报告雷达图多出一根毫无意义的轴
- 岗位技能数虚高、权重和被这一行重复计入

处理方式与既有的边去重一致：**归档而非删除**（status='archived'，读路径已全线过滤），
并把受影响的 id 记进 kg_edge_dedupe_log / 本脚本输出，可随时回滚。

用法：python -X utf8 scripts/archive_summary_skills.py [--apply]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import connect  # noqa: E402
from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL  # noqa: E402

# 汇总行 / 章节噪音：整名完全等于这些词才算，避免误伤「合计工时核算」这类真技能
SUMMARY_NAMES = ("总计", "合计", "小计", "总分", "总和", "总权重")


def main(apply: bool) -> int:
    with connect() as conn:
        nodes = conn.execute(
            f"""
            SELECT n.id, ({SKILL_KEY_SQL}) AS k, n.name, n.status
            FROM kg_node n
            WHERE n.type = 'skill_level'
              AND COALESCE(n.status, 'published') <> 'archived'
              AND ({SKILL_KEY_SQL}) = ANY(%s)
            """,
            (list(SUMMARY_NAMES),),
        ).fetchall()
        node_ids = [r["id"] for r in nodes]
        if not node_ids:
            print("没有需要处理的汇总行技能。")
            return 0

        edges = conn.execute(
            """
            SELECT e.id, e.src_id, e.rel_type, o.name AS src_name
            FROM kg_edge e JOIN kg_node o ON o.id = e.src_id
            WHERE e.dst_id = ANY(%s) AND COALESCE(e.status,'published') <> 'archived'
            """,
            (node_ids,),
        ).fetchall()

        by_name: dict[str, int] = {}
        for r in nodes:
            by_name[r["k"]] = by_name.get(r["k"], 0) + 1
        occs = sorted({e["src_name"] for e in edges})

        print(f"汇总行技能节点 {len(node_ids)} 个：{by_name}")
        print(f"关联边 {len(edges)} 条，涉及 {len(occs)} 个岗位/专业：")
        for name in occs[:10]:
            print(f"   - {name}")
        if len(occs) > 10:
            print(f"   …另有 {len(occs) - 10} 个")

        if not apply:
            print("\n[dry-run] 未写库。加 --apply 执行。")
            return 0

        conn.execute(
            "UPDATE kg_edge SET status='archived' WHERE id = ANY(%s) AND NOT is_draft",
            ([e["id"] for e in edges],),
        )
        conn.execute(
            "UPDATE kg_node SET status='archived' WHERE id = ANY(%s) AND NOT is_draft", (node_ids,)
        )
        conn.commit()
        print(f"\n[已提交] 归档节点 {len(node_ids)} 个、边 {len(edges)} 条。")
        print("  回滚：把这些 id 的 status 改回 'published' 即可（脚本可重复执行）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--apply" in sys.argv))
