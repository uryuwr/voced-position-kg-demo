"""清理同一岗位同一技能挂多档的 requires 边。

问题
----
edge id 是 (src, rel, dst)，而 dst 是**具体等级节点** —— 同一技能的 L2 与 L3 是
两个不同节点，id 自然不同，upsert 覆盖不了。重跑 apply（归并规则调整、档位判定
变化）时旧边原样留下，于是出现：

    .NET Core开发  L2 (weight=None)   ← 旧
    .NET Core开发  L3 (weight=0.2)    ← 新

后果有两个，都不明显但都在误导人：
1. 列表页按 skill_key 聚合算 7 项，详情页按边算 10 项，同一岗位两个数字
2. 「高档天生覆盖低档」，同时要求 L2 和 L3 在业务上没有意义

保留规则（顺序很关键，初版排错过）
--------------------------------
1. **有权重的优先**。同岗位技能权重 Σ≈1 是正式产出的标志；weight 为空的那条
   基本都是历次重跑的残留。初版按「档位高优先」，结果 SQL数据库开发 保留了
   L4(w=None) 却丢掉 L3(w=0.18) —— 把脏边当成了正解。
2. 都有权重时取 **fetched_at 更新**的，即最后一次 apply 的判定。
3. 仍分不出再取档位高的（高档覆盖低档）。

被丢弃那条的权重并入保留项，避免 Σ 掉下来。用 archived 而非物理删除，留审计痕迹。

用法::

    python -X utf8 scripts/fix_duplicate_requires.py --dry-run   # 检查，有重复则 exit 1
    python -X utf8 scripts/fix_duplicate_requires.py             # 修复

`--dry-run` 发现重复时返回非零退出码，可直接当**回归闸门**：铺量批跑完、
`migrate --clear` 重灌完各跑一次。写路径（`link_boss_skill_chain.stage_apply`
重建前先删旧边）与读路径（`skill_aggregate._dedupe_requires_by_skill`）已各挡一层，
但直连改库和历史数据绕得过应用层 —— 这道闸门是第三层。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import session

SQLITE = ROOT / "data" / "graph" / "kg.sqlite"

FIND_SQL = """
SELECT e.id AS eid, e.src_id, e.weight, e.fetched_at,
       (n.attrs::jsonb->>'skill_key') AS skey,
       NULLIF(n.attrs::jsonb->>'level','')::int AS lv,
       o.name AS occ_name
FROM kg_edge e
JOIN kg_node n ON n.id = e.dst_id AND n.type = 'skill_level'
JOIN kg_node o ON o.id = e.src_id
WHERE e.rel_type = 'requires'
  AND COALESCE(e.status,'published') = 'published'
  AND (n.attrs::jsonb->>'skill_key') IS NOT NULL
"""


def pick(rows: list[dict]) -> tuple[dict, list[dict]]:
    """返回 (保留, 丢弃[])。排序键的顺序就是保留规则，见模块说明。"""
    ordered = sorted(
        rows,
        key=lambda r: (
            r["weight"] is not None,          # 1. 有权重的优先（正式产出的标志）
            str(r["fetched_at"] or ""),       # 2. 最新一次 apply
            r["lv"] or 0,                     # 3. 高档覆盖低档
        ),
        reverse=True,
    )
    return ordered[0], ordered[1:]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    groups: dict[tuple[str, str], list[dict]] = {}
    with session() as c, c.cursor() as cur:
        cur.execute(FIND_SQL)
        for r in cur.fetchall():
            groups.setdefault((r["src_id"], r["skey"]), []).append(dict(r))

    dup = {k: v for k, v in groups.items() if len(v) > 1}
    print("同岗位同技能多档的组数:", len(dup))

    drop_ids, reweight = [], []
    for (src, skey), rows in dup.items():
        keep, drops = pick(rows)
        extra = sum(float(d["weight"]) for d in drops if d["weight"] is not None)
        print("  %-14s %-18s 保留 L%-2s(w=%s)  丢弃 %s"
              % (rows[0]["occ_name"][:14], skey[:18], keep["lv"], keep["weight"],
                 ", ".join("L%s(w=%s)" % (d["lv"], d["weight"]) for d in drops)))
        drop_ids += [d["eid"] for d in drops]
        if extra:
            reweight.append((float(keep["weight"] or 0) + extra, keep["eid"]))

    print()
    print("待归档边: %d | 需并权重的保留边: %d" % (len(drop_ids), len(reweight)))
    if not drop_ids:
        print("OK：无同岗位同技能多档的 requires 边")
        return
    if args.dry_run:
        print("(dry-run，未写入)")
        sys.exit(1)  # 当闸门用：有重复即失败

    with session() as c, c.cursor() as cur:
        for w, eid in reweight:
            # 两条都要 NOT is_draft：改 weight 漏了是**静默**覆盖运营未发布的草稿，
            # 改 status 漏了则直接撞 ck_kg_edge_draft_status 报错回滚
            cur.execute(
                "UPDATE kg_edge SET weight=%s WHERE id=%s AND NOT is_draft",
                (round(w, 4), eid),
            )
        cur.execute(
            "UPDATE kg_edge SET status='archived' WHERE id = ANY(%s) AND NOT is_draft",
            (drop_ids,),
        )
        print("PG 已归档:", cur.rowcount)

    if SQLITE.exists():
        s = sqlite3.connect(SQLITE)
        n = s.execute(
            "DELETE FROM edges WHERE id IN (%s)" % ",".join("?" * len(drop_ids)), drop_ids
        ).rowcount
        for w, eid in reweight:
            s.execute("UPDATE edges SET weight=? WHERE id=?", (round(w, 4), eid))
        s.commit()
        s.close()
        print("SQLite 已删除:", n)


if __name__ == "__main__":
    main()
