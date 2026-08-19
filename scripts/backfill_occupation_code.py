"""补岗位 attrs.code —— BOSS 来源的 821 个岗位从来没有这个字段。

背景
----
`attrs.code` 是岗位的业务主键：PG 上有部分唯一索引
`uq_kg_node_region_type_code (region, type, attrs->>'code')`（见 client.py:107），
`write.py` 也按它查节点。国标岗位 1639 个全有（国家职业分类大典代码），
但 BOSS 岗位只有 `boss_code`（BOSS 职位分类树的三级类目代码），读路径认不出来，
管理台「编码」列一律显示「—」。

实测 821 个 boss_code 全部存在、全部唯一、与国标 code 零冲突，可直接用作 code。

依赖面：全库只有 kg_node.attrs.code 一处存岗位编码。其它表的 code 列
（biz_achievement_def.code / biz_user_achievement.achievement_code /
kg_skill_category.code）都是别的东西，无需同步。

用法::
    python -X utf8 scripts/backfill_occupation_code.py --dry-run
    python -X utf8 scripts/backfill_occupation_code.py --apply
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SQLITE = ROOT / "data" / "graph" / "kg.sqlite"

from backend.kg.pg_store.client import connect as pg_connect  # noqa: E402

APPLY = "--apply" in sys.argv


def pick_code(attrs: dict) -> str | None:
    """BOSS 用 boss_code；其它来源没有可用编码就不硬造（宁缺勿错）。"""
    if attrs.get("code"):
        return None                      # 已有，不动
    bc = attrs.get("boss_code")
    return str(bc).strip() if bc not in (None, "") else None


def main() -> int:
    with pg_connect() as pg:
        rows = pg.execute("""
            SELECT id, name, source_system, attrs
            FROM kg_node
            WHERE type='occupation' AND COALESCE(status,'published')<>'archived'
              AND (attrs::json->>'code' IS NULL OR attrs::json->>'code'='')
        """).fetchall()
        plans, skipped = [], []
        for r in rows:
            a = r["attrs"] if isinstance(r["attrs"], dict) else json.loads(r["attrs"] or "{}")
            code = pick_code(a)
            if code:
                plans.append((r["id"], code, r["name"], a))
            else:
                skipped.append((r["name"], r["source_system"]))

        print(f"无 code 的岗位 {len(rows)} 个 → 可补 {len(plans)}，无可用编码 {len(skipped)}")
        for n, s in skipped[:5]:
            print(f"   跳过：{n}（{s}）")
        # 补完是否仍唯一
        codes = [c for _, c, _, _ in plans]
        if len(set(codes)) != len(codes):
            print("！补入的 code 自身有重复，中止")
            return 1
        hit = pg.execute("""
            SELECT count(*) n FROM kg_node
            WHERE type='occupation' AND COALESCE(status,'published')<>'archived'
              AND attrs::json->>'code' = ANY(%s)
        """, (codes,)).fetchone()["n"]
        print(f"   与既有 code 冲突：{hit} 个")
        if hit:
            return 1
        for _, c, n, _ in plans[:5]:
            print(f"   {n[:24]:26} code ← {c}")

        if not APPLY:
            print("\n（dry-run，加 --apply 写入）")
            return 0

        sq = sqlite3.connect(SQLITE)
        n_pg = n_sq = 0
        for nid, code, _, a in plans:
            a2 = dict(a)
            a2["code"] = code
            blob = json.dumps(a2, ensure_ascii=False)
            # NOT is_draft：不钉住会连运营未发布的草稿行一起覆盖（attrs 不是
            # status，撞不到 CHECK，静默生效）
            pg.execute(
                "UPDATE kg_node SET attrs=%s WHERE id=%s AND NOT is_draft", (blob, nid)
            )
            n_pg += 1
            cur = sq.execute("UPDATE nodes SET attrs=? WHERE id=?", (blob, nid))
            n_sq += cur.rowcount
        pg.commit(); sq.commit()
        print(f"\nPG 更新 {n_pg}，sqlite 更新 {n_sq}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
