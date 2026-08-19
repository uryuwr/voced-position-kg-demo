"""把存量 skill_level 节点的等级刻度迁到产品语义。PG 与源 SQLite 两边都能修。

这个脚本现在只是**存量补齐**
------------------------------
转换逻辑本身已经收进 `backend/kg/level_scale.py`，并挂在两条写入路径上：

- `backend/kg/pg_store/migrate.py` 每次灌库都过一遍（所以重灌不再抹掉产品档）
- `crawlers/cn/ingest_skill_standards.py` 建节点时过一遍（所以新采的数据一进来就是对的）

于是本脚本**不再是「必须记得跑」的一次性补丁**，只用来修那些在上述改动之前
就已经落库、且短期内不会被重灌的存量数据。

为什么当初会丢
--------------
2026-08-14 跑过一次，8919 个节点补上产品档，可评分岗位 117 → 608。
四天后有人重灌了一次库，`migrate.py` 的 `attrs = EXCLUDED.attrs` 无条件覆盖，
数字**逐位退回**回填前，全程零报错。所以修完存量还要修路径，缺一不可。

用法
----
    python -X utf8 scripts/migrate_skill_level_to_product.py            # dry-run，只看会改什么
    python -X utf8 scripts/migrate_skill_level_to_product.py --apply    # 改 PG
    python -X utf8 scripts/migrate_skill_level_to_product.py --apply --sqlite   # PG + 源 SQLite

`--sqlite` 连 `data/graph/kg.sqlite` 一起修：离线采集链路直接读那个文件，
只修 PG 的话，crawlers 看到的仍是国标形态。注意那个文件可能正被采集任务占用，
锁冲突时脚本会报错退出，换个空窗期再跑即可。

跑完用 `scripts/verify_backfill.py after` 逐档核对转换方向——
那个脚本另存了一份独立的期望表，**故意不 import level_scale**，否则等于自己比自己。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.level_scale import normalize_skill_level_node  # noqa: E402
from backend.kg.pg_store.client import connect  # noqa: E402
from backend.kg.pg_store.config import SQLITE_PATH  # noqa: E402


def _attrs_of(v) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return {}
    return v if isinstance(v, dict) else {}


def _plan(rows: list[dict]) -> tuple[list[tuple], dict, int, int]:
    """对一批节点做归一，返回 (更新元组, 分布统计, 跳过数, 判定不了数)。

    分布统计要在归一**之前**取原码/等级名——归一会把它们剥掉，之后再取就没了。
    """
    updates: list[tuple] = []
    stats: dict[tuple[str, str, int | None], int] = {}
    skipped = unresolved = 0

    for r in rows:
        before = _attrs_of(r.get("attrs"))
        code = str(before.get("level_code") or before.get("source_level_code") or "?").strip().upper()
        zh = str(before.get("level_zh") or "-")

        node = dict(r)
        result = normalize_skill_level_node(node)
        if result == "unresolved":
            unresolved += 1
            if unresolved <= 10:   # 上千行刷屏会把真正要看的分布顶掉
                print(f"  [未能判定] {r['id']} level_zh={zh!r} level_code={code!r}")
            elif unresolved == 11:
                print("  [未能判定] …（后续省略，总数见下方汇总）")
            continue
        if result == "skip":
            skipped += 1
            continue

        lv = _attrs_of(node["attrs"]).get("level")
        stats[(code, zh, lv)] = stats.get((code, zh, lv), 0) + 1
        updates.append(
            (
                json.dumps(_attrs_of(node["attrs"]), ensure_ascii=False),
                node["name"],
                node["description"],
                r["id"],
            )
        )
    return updates, stats, skipped, unresolved


def _report(label: str, updates: list, stats: dict, skipped: int, unresolved: int) -> None:
    print(f"\n=== {label} ===")
    if stats:
        print("  变更分布（国标原码 / 国标等级名 → 产品档）")
        for (code, zh, lv), n in sorted(stats.items(), key=lambda x: (x[0][2] or 0, x[0][0])):
            print(f"    {code:4s} {zh:16s} → level={lv}  {n:6d}")
    print(f"  待更新 {len(updates)} · 已就绪跳过 {skipped} · 无法判定 {unresolved}")


def do_pg(apply: bool) -> int:
    with connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT id, attrs, name, name_zh, description FROM kg_node WHERE type='skill_level'"
            ).fetchall()
        ]
        print(f"PG skill_level 节点总数: {len(rows)}")
        updates, stats, skipped, unresolved = _plan(rows)
        _report("PostgreSQL", updates, stats, skipped, unresolved)

        if not apply or not updates:
            return 1 if unresolved and apply else 0
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE kg_node SET attrs=%s, name=%s, description=%s "
            "WHERE id=%s AND NOT is_draft", updates
            )
        conn.commit()
        print(f"  [已提交] PG 更新 {len(updates)} 个节点")
    return 0


def do_sqlite(path: Path, apply: bool) -> int:
    if not path.exists():
        print(f"  [跳过] 源 SQLite 不存在: {path}")
        return 0
    # 采集任务可能正握着这个文件；给 15 秒等锁，等不到就报错退出而不是硬等
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        # 只动 CN：ONET 用 IM1–IM5（重要性刻度）、ESCO 另有一套，都不是国标等级，
        # 拿国标映射表去套是错的。它们的产品档另有归属，不在本脚本范围内。
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT id, attrs, name, name_zh, description FROM nodes "
                "WHERE type='skill_level' AND region='CN'"
            )
        ]
        print(f"\nSQLite skill_level（region=CN）节点数: {len(rows)}  ({path})")
        updates, stats, skipped, unresolved = _plan(rows)
        _report("源 SQLite", updates, stats, skipped, unresolved)

        if not apply or not updates:
            return 0
        conn.executemany(
            "UPDATE nodes SET attrs=?, name=?, description=? WHERE id=?", updates
        )
        conn.commit()
        print(f"  [已提交] SQLite 更新 {len(updates)} 个节点")
    finally:
        conn.close()
    return 0


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    also_sqlite = "--sqlite" in argv

    rc = do_pg(apply)
    if also_sqlite:
        rc = do_sqlite(SQLITE_PATH, apply) or rc

    if not apply:
        print("\n[dry-run] 未写任何库。加 --apply 执行（再加 --sqlite 连源文件一起修）。")
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
