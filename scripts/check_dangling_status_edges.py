"""闸门：published 的边指向 **非 published** 的节点 —— 有则 exit 1。

为什么需要它
------------
CLAUDE.md 记着「只过滤节点挡不住边」（两端都正常的 `parent_of` 那类）。
这个脚本查的是**它的反面**：边自己是 `published`，可它指向的节点被停用或归档了。

实测后果（共享库 `CN:occupation:BOSS:100107`）：一条 `requires` 边 published，
指向的技能节点 `.NET Core开发` 是 `disabled`。于是

- 前台按节点状态过滤 → 看不到这条技能 → 5 项，Σweight=0.81
- 管理台口径 `<> archived` → 看得到 → 6 项，Σweight=1.00

同一个岗位，运营看到的权重和与学员看到的不是一回事，而且**两边都不报错**。
BR-03 的门禁也会因为口径不同而给出运营看不懂的结论。

写侧已经根治（`write.cascade_edge_status_draft`：停用/归档节点时把它两端的边
一起标成同样意图，走草稿、发布时生效），但**存量数据只能靠这个闸门捞出来**。

用法
----
    python -X utf8 scripts/check_dangling_status_edges.py           # 只报告，有则 exit 1
    python -X utf8 scripts/check_dangling_status_edges.py --fix     # 把这些边降到与端点一致
    python -X utf8 scripts/check_dangling_status_edges.py --rel requires

`--fix` 的口径：边跟随**更严格**的那个端点状态（archived > disabled），
只改线上行；草稿边不动（那是运营还没发布的改动）。
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

_SCAN = """
SELECT e.id, e.rel_type, e.src_id, e.dst_id, e.weight,
       COALESCE(s.status, 'published') AS src_status,
       COALESCE(d.status, 'published') AS dst_status,
       s.name AS src_name, d.name AS dst_name
FROM kg_edge e
JOIN kg_node s ON s.id = e.src_id AND NOT s.is_draft
JOIN kg_node d ON d.id = e.dst_id AND NOT d.is_draft
WHERE NOT e.is_draft
  AND COALESCE(e.status, 'published') = 'published'
  AND (COALESCE(s.status, 'published') <> 'published'
    OR COALESCE(d.status, 'published') <> 'published')
  AND (%s::text IS NULL OR e.rel_type = %s)
ORDER BY e.rel_type, e.id
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="published 边指向非 published 节点的闸门")
    ap.add_argument("--rel", default=None, help="只查某个关系类型，如 requires")
    ap.add_argument(
        "--fix",
        action="store_true",
        help="把这些线上边的 status 降到与端点一致（archived 优先于 disabled）",
    )
    ap.add_argument("--limit", type=int, default=20, help="最多打印几条样本")
    args = ap.parse_args()

    print(f"库：{DATABASE_URL.rsplit('@', 1)[-1]}")
    with connect() as conn:
        rows = conn.execute(_SCAN, (args.rel, args.rel)).fetchall()
        print(f"published 边指向非 published 节点：{len(rows)} 条")
        by_rel: dict[str, int] = {}
        for r in rows:
            by_rel[r["rel_type"]] = by_rel.get(r["rel_type"], 0) + 1
        if by_rel:
            print("  按关系：" + " · ".join(f"{k}={v}" for k, v in sorted(by_rel.items())))
        for r in rows[: args.limit]:
            bad = []
            if r["src_status"] != "published":
                bad.append(f"src={r['src_status']}「{r['src_name']}」")
            if r["dst_status"] != "published":
                bad.append(f"dst={r['dst_status']}「{r['dst_name']}」")
            print(f"  [{r['rel_type']}] w={r['weight']} {r['id'][:90]}  {' '.join(bad)}")
        if len(rows) > args.limit:
            print(f"  …另有 {len(rows) - args.limit} 条")

        if args.fix and rows:
            n = 0
            for r in rows:
                # 更严格的那个赢：一端 archived 就 archived，否则 disabled
                target = (
                    "archived"
                    if "archived" in (r["src_status"], r["dst_status"])
                    else "disabled"
                )
                conn.execute(
                    "UPDATE kg_edge SET status = %s WHERE id = %s AND NOT is_draft",
                    (target, r["id"]),
                )
                n += 1
            conn.commit()
            print(f"已把 {n} 条边降到与端点一致（只改线上行，草稿边未动）")
            return 0

    if rows:
        print(
            "FAIL 这些边会让前台与管理台看到不同的技能数/权重和。"
            "跑 --fix 收敛，或在管理台把端点节点重新发布。"
        )
        return 1
    print("PASS 没有指向非 published 节点的 published 边")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
