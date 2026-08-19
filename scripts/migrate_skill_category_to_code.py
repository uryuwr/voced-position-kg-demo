"""把 `kg_node.category` 从中文名迁成分类 code，并补齐只写在 attrs 里的那批。

迁移前库里的三种状态
--------------------
    MOHRSS_CN  7279 个  category 列有中文名（操作与加工 / 设备维护与检修 …共 11 种）
    MOHRSS_CN  1559 个  两处都没有            → 落兜底 UNSORTED
    LLM_CN     3135 个  **只有 attrs.category**（技术工程 / 数据能力 …共 9 种），
                        `category` 列全空 —— 读路径查的是列，所以页面上全显示「未分类」，
                        数据其实一直在库里

两套口径由 `skill_taxonomy.to_code()` 的别名表归一到同一批 code，
认不出的一律落 `UNSORTED`（待归类），不猜。

幂等
----
`to_code()` 对 code 自身也返回自己，所以重复跑收敛到同一结果，不会二次污染。

两边都要处理
------------
SQLite 是采集库、PG 是运行库。只改 PG 的话，下次 `migrate` 会把 SQLite 里的
中文名原样灌回来（`attrs = EXCLUDED.attrs`），迁移白做 —— 技能档位回填就是这么
被抹掉过一次的。

用法::

    python -X utf8 scripts/migrate_skill_category_to_code.py --dry-run
    python -X utf8 scripts/migrate_skill_category_to_code.py
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import session
from backend.kg.pg_store.skill_taxonomy import FALLBACK_CODE, to_code

SQLITE = ROOT / "data" / "graph" / "kg.sqlite"


def _attrs_category(attrs) -> str | None:
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs or "{}")
        except Exception:
            return None
    return (attrs or {}).get("category") if isinstance(attrs, dict) else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    plan: list[tuple[str, str]] = []          # (node_id, new_code)
    stat: collections.Counter = collections.Counter()

    with session() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id, category, attrs FROM kg_node WHERE type = 'skill_level'"
        )
        for r in cur.fetchall():
            # 列优先，列为空时回落 attrs —— LLM 那批只有 attrs
            raw = (r["category"] or "").strip() or (_attrs_category(r["attrs"]) or "")
            code = to_code(raw)
            stat[(raw or "(空)", code)] += 1
            if (r["category"] or "") != code:
                plan.append((r["id"], code))

        print("映射结果（旧值 → 新 code，按量倒序）：")
        for (old, code), n in sorted(stat.items(), key=lambda x: -x[1]):
            mark = "  ← 兜底" if code == FALLBACK_CODE and old != "(空)" else ""
            print("   %-16s → %-9s %6d%s" % (old[:16], code, n, mark))
        print()
        print("需要改写 category 列的节点数：%d" % len(plan))

        if args.dry_run:
            print("\n(dry-run，未写入)")
            return

        for nid, code in plan:
            # NOT is_draft：同一 id 有线上行与草稿行两行，漏了就把运营未发布的
            # 草稿一起改掉，且 category 不是 status、撞不到 CHECK，静默生效
            cur.execute(
                "UPDATE kg_node SET category = %s WHERE id = %s AND NOT is_draft",
                (code, nid),
            )
        print("PG 已更新：%d" % len(plan))

    # SQLite：采集库的 nodes 表**没有 category 列**，分类只存在 attrs 里
    # （PG 侧的 category 列是灌库时从 attrs 提取出来的）。所以这边只改 attrs，
    # 不改的话下次 migrate 会把中文名原样灌回 PG，迁移白做。
    if SQLITE.exists():
        s = sqlite3.connect(SQLITE)
        s.row_factory = sqlite3.Row
        rows = s.execute("SELECT id, attrs FROM nodes WHERE type='skill_level'").fetchall()
        n = 0
        for r in rows:
            try:
                a = json.loads(r["attrs"] or "{}")
            except Exception:
                a = {}
            code = to_code(a.get("category"))
            if a.get("category") == code:
                continue
            a["category"] = code
            if not args.dry_run:
                s.execute("UPDATE nodes SET attrs=? WHERE id=?",
                          (json.dumps(a, ensure_ascii=False), r["id"]))
            n += 1
        if not args.dry_run:
            s.commit()
        s.close()
        print("SQLite attrs.category 已更新：%d" % n)


if __name__ == "__main__":
    main()
