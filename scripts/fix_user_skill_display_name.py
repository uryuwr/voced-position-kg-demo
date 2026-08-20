"""把 `biz_user_skill.skill_name` 列里装着的 code 刷成真展示名。

默认 dry-run，`--apply` 才写库。幂等，可重复执行。

    python -X utf8 scripts/fix_user_skill_display_name.py                    # 只看影响
    python -X utf8 scripts/fix_user_skill_display_name.py --apply            # 真刷
    DATABASE_URL=... python -X utf8 scripts/fix_user_skill_display_name.py   # 换库跑

**注意 `.env` 会覆盖进程环境变量**（`settings.py` 以 `override=True` 加载），
所以命令行前置 `DATABASE_URL=` 对本项目无效，换库要改 `backend/.env` 或用 `--dsn`。

## 刷的是哪些行、为什么

`biz_user_skill` 的 `skill_name` 列一度身兼两职：2026-08-19 之前 `skill_key` 就是
中文技能名，所以 `save_assessment_report` 往这一列塞 `skill_key` 完全正确；改成
`SK`+md5 之后，每做一次测评就写进 5–8 行 `skill_name='SKabd68031c5'`，
`/v1/student/me/skills` 与「我的画像」直接把哈希显示给学员。源头已修
（写路径改成 code 进 `skill_id`、展示名进 `skill_name`），所以**这批行是封顶的**。

    skill_id   = 'skill_key:SKabd68031c5'   ← 身份，正确，本脚本不动
    skill_name = 'SKabd68031c5'             ← 展示名这一格装着 code ← 刷这个
                 ↓
    skill_name = '.NET Core开发'

## 刷不刷，学员看到的都是中文名

读路径已经兜住了：`skill_display.profile_levels` 从 `skill_id` 剥 code、再批量查名字。
刷的价值是**让这一列的语义变干净** —— 不刷的话，以后任何直接读 `skill_name` 列而
没走那层解析的代码（手写 SQL、报表、下一个人加的接口）都会拿到 code。
正确性依赖「每个读点都记得走解析」，正是这个项目反复栽的形状。

## 为什么不动 skill_id

`PRIMARY KEY (user_id, skill_id)`。只 UPDATE `skill_name` 不会产生重复行；
动 `skill_id` 就是换主键，等于把这条画像记录换成另一条。

## 匹配路径的影响：只增不减

`skill_name` 列是 `match_with_profile._user_level_for` **按名字模糊匹配**那条路径的
输入。换成真名字后，这些行从「只能按 code 精确命中」变成「code 精确 + 名字模糊
都能命中」（code 那条走 `skill_id`，不受影响）。匹配面只增不减，不会让原本匹配上
的技能失配。

## 查不到名字的行不动

技能可能已被删除（`archived`）。这时保留 code 而不是写空 —— 指向已删技能的历史
画像项仍要看得见，显示成 code 比整行空白好排查（同 `skill_display.display_name`）。
"""

from __future__ import annotations

import argparse
import sys
import warnings

warnings.filterwarnings("ignore")

from backend.kg.pg_store.client import connect  # noqa: E402
from backend.kg.pg_store.skill_aggregate import resolve_skill_names  # noqa: E402
from backend.userprofile.skill_display import code_from_skill_id  # noqa: E402
from backend.kg.skill_key import is_generated  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写库；缺省只报告")
    ap.add_argument(
        "--limit", type=int, default=0, help="只处理前 N 行（调试用，0=全部）"
    )
    args = ap.parse_args()

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT user_id, skill_id, skill_name, level, source
            FROM biz_user_skill
            ORDER BY updated_at DESC
            """
        ).fetchall()

        # 判据用 is_generated（`SK` 前缀 + 纯 ASCII），不是 is_valid_key ——
        # 后者对 `Python`/`SQL`/`Excel` 这类正常技能名也为真，会把它们当 code 刷掉
        dirty = [r for r in rows if is_generated(str(r["skill_name"] or "").strip())]
        if args.limit:
            dirty = dirty[: args.limit]

        print(f"全表 {len(rows)} 行，其中 skill_name 装着 code 的 {len(dirty)} 行")
        if not dirty:
            print("没有需要修的行（幂等：刷过就不会再命中）")
            return 0

        codes = {}
        for r in dirty:
            c = code_from_skill_id(r["skill_id"])
            # skill_id 剥不出就退回 skill_name 本身 —— 它现在就是个 code
            codes[(r["user_id"], r["skill_id"])] = c or str(r["skill_name"]).strip()

        names = resolve_skill_names(set(codes.values()), conn, online_only_rows=False)

        todo, skipped = [], []
        for r in dirty:
            code = codes[(r["user_id"], r["skill_id"])]
            nm = names.get(code)
            (todo if nm else skipped).append((r, code, nm))

        by_source: dict[str, int] = {}
        for r, _, _ in todo:
            by_source[str(r["source"])] = by_source.get(str(r["source"]), 0) + 1
        print(f"能查到真名字、将刷：{len(todo)} 行  按来源 {by_source}")
        print(f"查不到名字、保持不动：{len(skipped)} 行"
              + ("（技能可能已删除）" if skipped else ""))
        print()
        for r, code, nm in todo:
            print(f"  user=…{str(r['user_id'])[-6:]} L{r['level']} {code}"
                  f"  {r['skill_name']!r} → {nm!r}")
        for r, code, _ in skipped:
            print(f"  [跳过] user=…{str(r['user_id'])[-6:]} {code} 查不到对应技能")

        if not args.apply:
            print(f"\n[dry-run] 未写库。加 --apply 才真刷（{len(todo)} 行）")
            return 0

        n = 0
        for r, _, nm in todo:
            n += conn.execute(
                # 只动 skill_name。**不要碰 skill_id**（主键的一部分，见模块说明）；
                # 也不更新 updated_at —— 那一列的语义是「画像什么时候变的」，
                # 这次改的是展示文案，不是学员的能力数据
                """
                UPDATE biz_user_skill SET skill_name = %s
                WHERE user_id = %s AND skill_id = %s
                """,
                (nm, r["user_id"], r["skill_id"]),
            ).rowcount
        conn.commit()
        print(f"\n已更新 {n} 行")

        left = conn.execute(
            "SELECT count(*) AS c FROM biz_user_skill WHERE skill_name LIKE 'SK%'"
        ).fetchone()["c"]
        print(f"复查：skill_name 仍以 SK 开头的行 {left} 条"
              + (f"（就是上面跳过的 {len(skipped)} 条）" if left else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
