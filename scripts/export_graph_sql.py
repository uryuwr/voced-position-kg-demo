r"""把图数据导成**可直接在 SDP DB 管理台粘贴执行**的 .sql。

## 为什么不用 pg_dump

`pg_dump --data-only` 出的是 `COPY … FROM stdin` + `\.` 结束符，那是 **psql 客户端指令**，
Web 管理台的 SQL 执行器多半不认；而 `--inserts` 出的是裸 `INSERT`，
**撞上唯一约束就整份中断**。这个脚本出的是 `INSERT … ON CONFLICT DO NOTHING`：

- **不带冲突目标**（不是 `ON CONFLICT (id, is_draft)`）。`kg_node` 除主键外还有一个
  **部分唯一索引** `uq_kg_node_region_type_code`（按 `attrs->>'code'`，仅对非归档非草稿行生效），
  指定目标的写法挡不住它，一旦撞上整条语句报错。不带目标 = 任何唯一冲突都跳过。
- 因此**可重复执行**：导一半超时了，重跑同一份文件即可，已插入的行不会报错。

## 与启动 upsert 的关系（用户明确要求不能冲突）

服务启动时 `ensure_biz_schema()` 会 upsert 两批种子，**它们不在本导出里**：

- `kg_skill_category`（12 个技能分类，真源在 `kg/pg_store/skill_taxonomy.py`）
- `biz_achievement_def`（3 条成就定义）

导出只含 `kg_node` / `kg_edge` / `kg_skill_prereq` 三张纯数据表。**表结构也不导** ——
启动时的幂等 DDL 会建好 21 张表和 31 个索引，导入前先让服务起一次。

## 过滤口径

- **只导线上行**（`NOT is_draft`）。草稿行是本地未发布的编辑痕迹，不该带进预生产。
- **排除 `archived`**。它是逻辑删除、任何接口都不返回；基准库里 17648 个节点 +
  38518 条边是归档的，占总量一半以上，带过去只是让库变大。
  保留 `published` / `draft` / `disabled`：前两者是真内容，`disabled` 只有 6 个。
- **排除两端指向被过滤掉节点的边**（基准库里 11 条）。`kg_edge` 指向 `kg_node` 的外键
  已被删除（PostgreSQL 的外键引用不了部分唯一索引），两端存在性不由库保证 ——
  不排掉就等于往预生产手工种下孤儿边，`scripts/check_orphan_edges.py` 会报。

用法：

    python -X utf8 scripts/export_graph_sql.py --db voced_kg --out out/
    python -X utf8 scripts/export_graph_sql.py --db voced_kg_dev --out out/   # 先修数据多得多
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

# 显式列名，**不依赖列顺序**：目标库将来加列（ALTER TABLE ADD COLUMN）后，
# 按位置插入会错位，而按列名插入不受影响
COLS = {
    "kg_node": [
        "id", "region", "type", "name", "name_en", "name_zh", "aliases", "description",
        "attrs", "source_system", "source_id", "source_url", "license", "fetched_at",
        "confidence", "status", "updated_by", "updated_by_name", "sort_order",
        "child_count", "level", "category", "created_at", "version", "owner",
        "owner_name", "is_draft", "target_status", "base_version",
    ],
    # **不含 `migrated_from`**：老库里有这一列（`related_to` 重构时期的历史标记，
    # 28433 行非空但只有两个取值），而当前的 `ensure_schema()` DDL **不建它** ——
    # 往全新库导入时 `column "migrated_from" does not exist`，整份文件报错中断。
    # 生产代码无人读它（`_check_refactor_progress.py` 读的是 `attrs` 里的同名键，
    # 不是这一列）。凡是「老库有、DDL 不建」的列都要这样排掉，见下方 _check_cols。
    "kg_edge": [
        "id", "src_id", "dst_id", "rel_type", "region", "weight", "evidence", "attrs",
        "source_system", "source_id", "source_url", "license", "fetched_at",
        "confidence", "status", "updated_by", "updated_by_name", "structure_layer",
        "created_at", "is_draft", "target_status", "unit_id",
    ],
    "kg_skill_prereq": [
        "skill_key", "prereq_skill_key", "region", "evidence", "confidence",
        "created_by", "created_at",
    ],
}

KEEP_NODE = "NOT n.is_draft AND COALESCE(n.status,'published') <> 'archived'"
KEEP_EDGE = (
    "NOT e.is_draft AND COALESCE(e.status,'published') <> 'archived'"
    " AND EXISTS (SELECT 1 FROM kg_node kn WHERE kn.id = e.src_id"
    f"            AND NOT kn.is_draft AND COALESCE(kn.status,'published') <> 'archived')"
    " AND EXISTS (SELECT 1 FROM kg_node kn WHERE kn.id = e.dst_id"
    f"            AND NOT kn.is_draft AND COALESCE(kn.status,'published') <> 'archived')"
)

WHERE = {"kg_node": KEEP_NODE, "kg_edge": KEEP_EDGE, "kg_skill_prereq": "TRUE"}
ALIAS = {"kg_node": "n", "kg_edge": "e", "kg_skill_prereq": "p"}
# 稳定排序：同一份数据每次导出逐字节一致，便于 diff 与复核
ORDER = {"kg_node": "n.id", "kg_edge": "e.id", "kg_skill_prereq": "p.skill_key, p.prereq_skill_key"}


def psql(db: str, sql: str, container: str) -> str:
    """在容器里跑 psql，取无表头无对齐的原始输出。"""
    r = subprocess.run(
        ["docker", "exec", "-i", container, "psql", "-U", "voced", "-d", db,
         "-A", "-t", "--no-psqlrc", "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True, encoding="utf-8",
    )
    if r.returncode != 0:
        raise SystemExit(f"psql 失败：{r.stderr[:600]}")
    return r.stdout


def _check_cols(db: str, table: str, container: str) -> None:
    """把「源库有、但没写进 COLS」的列**显式报出来**。

    静默漏列是这个脚本最危险的失败方式：导出照样成功、导入照样成功，
    只是预生产少了一列数据，谁也不会发现。而漏列的成因是真实存在的 ——
    `kg_edge.migrated_from` 在老库有、当前 DDL 不建，必须排掉（见 COLS 注释）；
    反过来，将来有人给 DDL 加了列而忘了加进 COLS，也会在这里被抓住。
    """
    got = {c.strip() for c in psql(
        db,
        f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table}'",
        container,
    ).split("\n") if c.strip()}
    missing = got - set(COLS[table])
    extra = set(COLS[table]) - got
    if extra:
        raise SystemExit(f"★ {table}: COLS 里有源库不存在的列 {sorted(extra)}")
    if missing:
        print(f"  ! {table} 未导出的列：{sorted(missing)}"
              f"（确认是「当前 DDL 不建」的遗留列才可以放过）")


def gen_table(db: str, table: str, batch: int, container: str) -> tuple[list[str], int]:
    cols = COLS[table]
    a = ALIAS[table]
    # 字面量由 **PostgreSQL 自己** 用 %L 生成：引号、反斜杠、NULL、中文全交给它，
    # 不在 Python 里手写转义（那是最容易出错的地方）
    tuple_expr = "format('(' || " + " || ',' || ".join([f"'%L'" for _ in cols]) + " || ')', " \
                 + ", ".join(f"{a}.{c}" for c in cols) + ")"
    sql = f"""
        WITH rows AS (
          SELECT {tuple_expr} AS v,
                 (row_number() OVER (ORDER BY {ORDER[table]}) - 1) / {batch} AS grp
          FROM {table} {a}
          WHERE {WHERE[table]}
        )
        SELECT 'INSERT INTO {table} ({", ".join(cols)}) VALUES '
               || string_agg(v, ',' ORDER BY v)
               || ' ON CONFLICT DO NOTHING;'
        FROM rows GROUP BY grp ORDER BY grp
    """
    out = psql(db, sql, container)
    stmts = [s for s in out.split("\n") if s.strip().startswith("INSERT INTO")]
    n = int(psql(db, f"SELECT count(*) FROM {table} {a} WHERE {WHERE[table]}", container).strip())
    return stmts, n


HEADER = """-- 职业教育知识图谱 · 图数据导入（{db}，{when}）
--
-- 用法：在 SDP DB 管理台按文件名顺序执行。**可重复执行**，导一半中断就重跑同一份。
--
-- 前置：先让服务启动一次，把表建好（启动时跑幂等 DDL，21 张表 + 31 个索引）。
-- 本文件**不含建表语句**，也**不含** kg_skill_category / biz_achievement_def ——
-- 那两批种子由服务启动时 upsert，在这里重复插入会和它冲突。
--
-- 每条语句都是 INSERT ... ON CONFLICT DO NOTHING（**不带冲突目标**）：
-- kg_node 除主键 (id, is_draft) 外还有部分唯一索引 uq_kg_node_region_type_code，
-- 指定目标的写法挡不住它，撞上就整条报错。
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
-- 不开显式事务：管理台对超长事务常有超时，且每条语句本身幂等，逐条提交更稳。
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="voced_kg", help="源库名（基准库 voced_kg / 开发库 voced_kg_dev）")
    ap.add_argument("--container", default="voced-pg")
    ap.add_argument("--out", default="out")
    ap.add_argument("--batch", type=int, default=200, help="每条 INSERT 多少行")
    ap.add_argument("--max-mb", type=float, default=8.0, help="单个 .sql 文件上限（MB）")
    args = ap.parse_args()

    when = psql(args.db, "SELECT now()::timestamp(0)", args.container).strip()
    outdir = pathlib.Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    seq, written = 0, []
    for table in ("kg_node", "kg_edge", "kg_skill_prereq"):
        _check_cols(args.db, table, args.container)
        stmts, n = gen_table(args.db, table, args.batch, args.container)
        print(f"{table}: {n} 行 → {len(stmts)} 条 INSERT")
        buf, size = [], 0
        def flush() -> None:
            nonlocal buf, size, seq
            if not buf:
                return
            seq += 1
            p = outdir / f"{seq:02d}_{table}.sql"
            p.write_text(
                HEADER.format(db=args.db, when=when) + "\n".join(buf) + "\n",
                encoding="utf-8", newline="\n",
            )
            written.append((p, len(buf), size))
            buf, size = [], 0
        for s in stmts:
            b = len(s.encode("utf-8"))
            if size + b > args.max_mb * 1024 * 1024:
                flush()
            buf.append(s)
            size += b
        flush()

    print("\n生成的文件（按文件名顺序执行）：")
    total = 0
    for p, cnt, size in written:
        total += size
        print(f"  {p.name:<28} {cnt:>4} 条语句  {size/1024/1024:.1f} MB")
    print(f"  合计 {len(written)} 个文件 {total/1024/1024:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
