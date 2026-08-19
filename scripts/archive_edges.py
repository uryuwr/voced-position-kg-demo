"""按边类型归档 kg_edge（status='archived'）。

归档 ≠ 删除：数据行留在库里，只是所有读接口默认不再返回
（读路径统一带 `backend.kg.pg_store.config.edge_published()` 过滤）。
要核对或恢复，走 `GET /v1/kg/edges?status=archived`，或直接 UPDATE 回 published。

本期决策（v3.0 E1–E7 对齐）：
  - 认证维度 articulates_to / recognized_by → 归档（v3.0 无 Credential 实体）
  - 行业层级 parent_of                     → 归档（v3.0 E1/E2 无 Industry→Industry）
  - 课程维度 offers_course / taught_by / related_to → **保留**（本期先留着）

用法：
    python scripts/archive_edges.py --dry-run
    python scripts/archive_edges.py
    python scripts/archive_edges.py --restore          # 恢复本脚本归档过的边
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import connect  # noqa: E402

# 要归档的边类型
TARGETS = ["articulates_to", "recognized_by", "parent_of"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", action="store_true", help="把这些类型的边改回 published")
    ap.add_argument("--rel-types", nargs="*", default=None, help="覆盖默认边类型清单")
    args = ap.parse_args()

    rels = args.rel_types or TARGETS
    to_status = "published" if args.restore else "archived"
    from_desc = "archived" if args.restore else "非 archived"

    with connect() as conn:
        print(f"目标：{', '.join(rels)}  →  status={to_status}\n")
        total = 0
        for rel in rels:
            rows = conn.execute(
                "SELECT COALESCE(status,'published') st, count(*) n FROM kg_edge "
                "WHERE rel_type=%s GROUP BY 1 ORDER BY n DESC",
                (rel,),
            ).fetchall()
            cur = {r["st"]: r["n"] for r in rows}
            movable = (
                cur.get("archived", 0)
                if args.restore
                else sum(v for k, v in cur.items() if k != "archived")
            )
            total += movable
            print(f"  {rel:16s} 现状={cur}  待改({from_desc})={movable}")

        if args.dry_run:
            print(f"\n[dry-run] 未写库；将影响 {total} 条")
            return 0

        changed = 0
        for rel in rels:
            if args.restore:
                cur = conn.execute(
                    # 只动线上行：草稿边的 status 恒为 draft（ck_kg_edge_draft_status）
                    "UPDATE kg_edge SET status='published' "
                    "WHERE rel_type=%s AND status='archived' AND NOT is_draft",
                    (rel,),
                )
            else:
                cur = conn.execute(
                    "UPDATE kg_edge SET status='archived' "
                    "WHERE rel_type=%s AND COALESCE(status,'published') <> 'archived' "
                    "AND NOT is_draft",
                    (rel,),
                )
            changed += cur.rowcount
        print(f"\n已更新 {changed} 条边 → status={to_status}")

        print("\n归档后各类边可见性：")
        for r in conn.execute(
            "SELECT rel_type, COALESCE(status,'published') st, count(*) n "
            "FROM kg_edge GROUP BY 1,2 ORDER BY 1"
        ).fetchall():
            vis = "接口可见" if r["st"] == "published" else "仅库内"
            print(f"  {r['rel_type']:16s} {r['st']:10s} {r['n']:6d}  {vis}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
