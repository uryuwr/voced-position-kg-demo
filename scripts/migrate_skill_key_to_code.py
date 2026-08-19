"""把 `skill_key` 从中文技能名刷成 ASCII code（`SK` + md5 前 10 位）。

背景与形态见 `backend/kg/skill_key.py` 的模块 docstring。这里只讲**迁移**。

默认 dry-run，`--apply` 才写库。可重复执行（幂等）：已经是合法 code 的行不动。

    python -X utf8 scripts/migrate_skill_key_to_code.py            # 只看影响
    python -X utf8 scripts/migrate_skill_key_to_code.py --apply    # 真刷

## 顺序不能换

1. **先补 `attrs.skill_name`**。库里有 18540 个 skill_level 节点的 attrs 里根本
   没有 skill_name，展示名一直靠 `SKILL_KEY_SQL` 从 `name` 里剥 " · L3" 兜着。
   而这一步之后 `skill_key` 就不再是名字了 —— 不先把名字落到 skill_name 上，
   刷完 key 页面上就没有技能名可显示了，且**不报错**，只是全变成 `SKxxxx`。
2. 再写 `attrs.skill_key`。
3. 最后把引用 key 的三张表一起重映射：`kg_skill_prereq`（两列！）、
   `biz_assessment_item`、`biz_assessment_question`。漏了就是断链：先修关系还在、
   但指向一个已经不存在的 key，页面上先修凭空消失；测评题库同理会取不到题。

## 草稿行

`kg_node` 主键是 `(id, is_draft)`，同一 id 两行。两行都要刷 —— 只刷线上行的话，
运营发布草稿时会把中文 key 又写回来。这里不按 is_draft 过滤，是**故意**的
（与 CLAUDE.md「写路径每条 UPDATE 都要钉 is_draft」的规则相反，因为这次要的
就是两行都改）。
"""

from __future__ import annotations

import argparse
import sys
import warnings
from collections import defaultdict

warnings.filterwarnings("ignore")

from backend.kg.pg_store.client import connect  # noqa: E402
from backend.kg.skill_key import (  # noqa: E402
    derive_key,
    is_valid_key,
    normalize_name,
)

# 展示名：attrs.skill_name 优先，其次从节点名剥掉 " · L3" 后缀
NAME_EXPR = """
COALESCE(
  NULLIF(btrim((CASE WHEN n.attrs IS NULL OR btrim(n.attrs)='' THEN NULL
                     ELSE n.attrs::json END)->>'skill_name'), ''),
  NULLIF(btrim(split_part(n.name, ' · ', 1)), ''),
  NULLIF(btrim(split_part(n.name, '·', 1)), ''),
  n.name
)
"""
CUR_KEY_EXPR = """
NULLIF(btrim((CASE WHEN n.attrs IS NULL OR btrim(n.attrs)='' THEN NULL
                   ELSE n.attrs::json END)->>'skill_key'), '')
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写库；缺省只报告")
    args = ap.parse_args()
    apply = args.apply

    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT n.id, n.is_draft, n.name,
                   {NAME_EXPR} AS disp_name,
                   {CUR_KEY_EXPR} AS cur_key,
                   (CASE WHEN n.attrs IS NULL OR btrim(n.attrs)='' THEN NULL
                         ELSE n.attrs::json END)->>'skill_name' AS has_sname
            FROM kg_node n
            WHERE n.type = 'skill_level'
            """
        ).fetchall()
        print(f"skill_level 节点：{len(rows)} 行（含草稿行）")

        need_sname = [r for r in rows if not (r["has_sname"] or "").strip()]
        print(f"  ① 需要补 attrs.skill_name：{len(need_sname)}")

        # 老 key → 新 key。老 key 缺失时用展示名兜（读路径本来就是这么兜的）
        old2new: dict[str, str] = {}
        newkey_names: dict[str, set[str]] = defaultdict(set)
        need_key = []
        for r in rows:
            disp = (r["disp_name"] or "").strip()
            if not disp:
                continue
            new = derive_key(disp)
            newkey_names[new].add(normalize_name(disp))
            old = (r["cur_key"] or "").strip() or disp
            if is_valid_key(old) and old.startswith("SK"):
                continue                      # 已经刷过
            old2new[old] = new
            need_key.append((r["id"], r["is_draft"], new))
        print(f"  ② 需要写 attrs.skill_key：{len(need_key)}")
        print(f"  ③ 不同技能名映射出的 key 数：{len(newkey_names)}")

        clash = {k: v for k, v in newkey_names.items() if len(v) > 1}
        if clash:
            print(f"  ★ 哈希碰撞 {len(clash)} 个（不同名字撞同一个 key），必须先处理：")
            for k, v in list(clash.items())[:5]:
                print(f"      {k} ← {sorted(v)}")
            return 2
        print("  ③' 无哈希碰撞")

        # 引用 key 的三张表
        refs = {
            "kg_skill_prereq": ("skill_key", "prereq_skill_key"),
            "biz_assessment_item": ("skill_key",),
            "biz_assessment_question": ("skill_key",),
        }
        print("  ④ 引用 skill_key 的表：")
        orphans: dict[str, set[str]] = defaultdict(set)
        for t, cols in refs.items():
            for col in cols:
                vals = [
                    r[col]
                    for r in conn.execute(
                        f"SELECT DISTINCT {col} FROM {t} WHERE {col} IS NOT NULL"
                    ).fetchall()
                ]
                hit = [v for v in vals if v in old2new]
                already = [v for v in vals if is_valid_key(v) and v.startswith("SK")]
                miss = [v for v in vals if v not in old2new and v not in already]
                for v in miss:
                    orphans[f"{t}.{col}"].add(v)
                print(
                    f"      {t}.{col}: {len(vals)} 个值 → 可映射 {len(hit)}"
                    f" · 已是 code {len(already)} · 找不到对应技能 {len(miss)}"
                )
        if orphans:
            print("      找不到对应技能的值（会按名字现算 key，保持可用）：")
            for k, v in orphans.items():
                print(f"        {k}: {sorted(v)[:5]}{' …' if len(v) > 5 else ''}")

        if not apply:
            print("\n[dry-run] 未写库。确认无误后加 --apply")
            return 0

        # ---------------------------------------------------------- 真写
        print("\n开始写库…")
        n1 = conn.execute(
            f"""
            UPDATE kg_node n SET attrs = (
              COALESCE(NULLIF(btrim(n.attrs), '')::jsonb, '{{}}'::jsonb)
              || jsonb_build_object('skill_name', {NAME_EXPR})
            )::text
            WHERE n.type = 'skill_level'
              AND COALESCE(btrim((CASE WHEN n.attrs IS NULL OR btrim(n.attrs)=''
                                       THEN NULL ELSE n.attrs::json END)->>'skill_name'), '') = ''
            """
        ).rowcount
        print(f"  ① attrs.skill_name 补齐：{n1}")

        # skill_key 用 SQL 现算，和 Python 侧同一套（md5 + normalize NFC）
        from backend.kg.skill_key import SQL_DERIVE_KEY

        n2 = conn.execute(
            f"""
            UPDATE kg_node n SET attrs = (
              COALESCE(NULLIF(btrim(n.attrs), '')::jsonb, '{{}}'::jsonb)
              || jsonb_build_object('skill_key', {SQL_DERIVE_KEY(NAME_EXPR)})
            )::text
            WHERE n.type = 'skill_level'
              AND COALESCE({CUR_KEY_EXPR}, '') !~ '^SK[0-9a-f]{{10}}$'
            """
        ).rowcount
        print(f"  ② attrs.skill_key 写入：{n2}")

        for t, cols in refs.items():
            for col in cols:
                n3 = 0
                for old, new in old2new.items():
                    n3 += conn.execute(
                        f"UPDATE {t} SET {col} = %s WHERE {col} = %s", (new, old)
                    ).rowcount
                # 找不到对应技能的：按它自己的字面量现算，保证仍能自洽
                left = [
                    r[col]
                    for r in conn.execute(
                        f"SELECT DISTINCT {col} FROM {t} "
                        f"WHERE {col} IS NOT NULL AND {col} !~ '^SK[0-9a-f]{{10}}$'"
                    ).fetchall()
                ]
                for v in left:
                    n3 += conn.execute(
                        f"UPDATE {t} SET {col} = %s WHERE {col} = %s",
                        (derive_key(v), v),
                    ).rowcount
                print(f"  ④ {t}.{col} 重映射：{n3}")
        conn.commit()

        bad = conn.execute(
            f"""
            SELECT count(*) c FROM kg_node n WHERE n.type='skill_level'
              AND COALESCE({CUR_KEY_EXPR}, '') !~ '^SK[0-9a-f]{{10}}$'
            """
        ).fetchone()["c"]
        print(f"\n收尾核对：仍非 code 形态的 skill_level 节点 {bad}（应为 0）")
        return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
