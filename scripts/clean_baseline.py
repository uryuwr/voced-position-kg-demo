"""基准库清洗：按 `audit_baseline_quality.py` 的分类逐项处置。

判据只有一处
------------
分类 SQL 全部 import 自审计脚本，这里不重写。审计说「这 20 个是 URL 编码残留」，
清洗就删这 20 个 —— 两边各写一份判据的话，迟早出现「审计报 20、清洗删 18」，
而且没人看得出来。

物理删还是归档
--------------
    fixture / URL 编码 / 解析噪声  → **物理删**
    职业名当技能 / 过泛            → 归档（archived）

分界是「它曾经是有效数据吗」。`archived` 的语义是「曾经有效、现在下线」，
可以恢复、有审计价值。而 `__e2e_skill_102225`、`3D%25E5%259C%25BA…`、`总计 · L2`
从来没有效过 —— 留成 archived 只会让后人以为「这是被下线的真实技能」。

物理删必须连边一起删（外键 2026-08-19 已删，库不再保证两端存在），
否则留下孤儿边，`check_orphan_edges.py` 会报警。

用法::

    python -X utf8 scripts/clean_baseline.py                 # dry-run，默认
    python -X utf8 scripts/clean_baseline.py --apply
    python -X utf8 scripts/clean_baseline.py --apply --include-vague   # 连「疑似过泛」一起清
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import session
from scripts.audit_baseline_quality import CHECKS, _LIVE_N, _has_is_draft

REPORT = ROOT / "reports" / "baseline_clean.json"

# 从来没有效过 → 物理删；曾经有效 → 归档
HARD_DELETE = {"test_fixture", "url_encoded_name", "parse_noise", "empty_name",
               "manual_unpublished"}
ARCHIVE = {"occupation_as_skill"}
# 需要人判断，默认不动（--include-vague 才处理）
OPT_IN = {"too_vague_skill"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写库；缺省只看不改")
    ap.add_argument("--include-vague", action="store_true",
                    help="连「疑似过泛」也归档 —— 这类是判断题，默认不碰")
    args = ap.parse_args()

    plan: dict[str, dict] = {}
    with session() as c, c.cursor() as cur:
        live_n = _LIVE_N if _has_is_draft(cur) else "true"
        for key, desc, sql in CHECKS:
            if key in OPT_IN and not args.include_vague:
                continue
            cur.execute(sql.format(live=live_n, live_o=live_n.replace("n.", "o.")))
            ids = [r["id"] for r in cur.fetchall()]
            if not ids:
                continue
            how = "delete" if key in HARD_DELETE else "archive"
            plan[key] = {"desc": desc, "how": how, "ids": ids}
            print("%-22s %5d 个  → %s" % (key, len(ids), how))

    if not plan:
        print("没有需要清洗的")
        return

    all_del = [i for v in plan.values() if v["how"] == "delete" for i in v["ids"]]
    all_arc = [i for v in plan.values() if v["how"] == "archive" for i in v["ids"]]
    print()
    print("合计：物理删 %d 个节点，归档 %d 个节点" % (len(all_del), len(all_arc)))

    with session() as c, c.cursor() as cur:
        if all_del:
            cur.execute(
                "SELECT count(*)::int n FROM kg_edge WHERE src_id = ANY(%s) OR dst_id = ANY(%s)",
                (all_del, all_del),
            )
            print("  物理删会连带删除 %d 条边" % cur.fetchone()["n"])

        # 边 published 却指向 archived 节点：把边也归档，否则管理台按边计数
        # 与详情按可见节点算出两个数字（3D设计师「列表 1 项、详情 0 项」就是这么来的）
        cur.execute(
            f"""
            SELECT count(*)::int n FROM kg_edge e
            JOIN kg_node n ON n.id = e.dst_id AND {live_n}
            WHERE COALESCE(e.status,'published') = 'published'
              AND COALESCE(n.status,'published') = 'archived'
            """
        )
        dangling = cur.fetchone()["n"]
        print("  另有 %d 条边 published 却指向 archived 节点，一并归档" % dangling)

    if not args.apply:
        print("\n(dry-run。加 --apply 执行)")
        return

    with session() as c, c.cursor() as cur:
        if all_del:
            # 边先删：留下孤儿边的话 check_orphan_edges 会报警，
            # 而且外键已删、库不会替我们拦
            cur.execute(
                "DELETE FROM kg_edge WHERE src_id = ANY(%s) OR dst_id = ANY(%s)",
                (all_del, all_del),
            )
            print("已删除边：%d" % cur.rowcount)
            cur.execute("DELETE FROM kg_node WHERE id = ANY(%s)", (all_del,))
            print("已删除节点：%d" % cur.rowcount)

        if all_arc:
            # 钉 is_draft：不钉就同时打中线上行与草稿行，改 status 会撞
            # ck_kg_node_draft_status（草稿行 status 恒为 'draft'）直接 500
            cur.execute(
                "UPDATE kg_node SET status='archived' "
                "WHERE id = ANY(%s) AND COALESCE(is_draft,false) = false",
                (all_arc,),
            )
            print("已归档节点：%d" % cur.rowcount)
            cur.execute(
                "UPDATE kg_edge SET status='archived' "
                "WHERE (src_id = ANY(%s) OR dst_id = ANY(%s)) "
                "  AND COALESCE(is_draft,false) = false",
                (all_arc, all_arc),
            )
            print("已归档其关联边：%d" % cur.rowcount)

        cur.execute(
            f"""
            UPDATE kg_edge e SET status = 'archived'
            WHERE COALESCE(e.is_draft,false) = false
              AND COALESCE(e.status,'published') = 'published'
              AND EXISTS (
                    SELECT 1 FROM kg_node n WHERE n.id = e.dst_id AND {live_n}
                      AND COALESCE(n.status,'published') = 'archived'
              )
            """
        )
        print("已归档指向 archived 节点的边：%d" % cur.rowcount)

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps({k: {"desc": v["desc"], "how": v["how"], "count": len(v["ids"])}
                    for k, v in plan.items()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("\n报告：", REPORT)


if __name__ == "__main__":
    main()
