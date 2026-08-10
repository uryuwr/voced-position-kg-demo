"""技能前置关系（kg_skill_prereq）种子 · 测试数据。

构造依据（双重约束，避免凭空 mock）
------------------------------------
1. **职业逻辑**：按国家职业技能标准的职业功能推进顺序——
   安全环保 → 作业准备 → 操作加工 → 检修/质检/数据/服务 → 技术管理/运营 → 培训指导。
   现实里就是「先懂安全 → 会准备 → 能操作 → 会检修检验 → 能做技术管理 → 才能带教」。
   仅在 `PREREQ_ALLOWED_PAIRS` 显式白名单的方向上连边（与前端分区顺序同源）。

2. **真实共现**：两个技能必须被 **同一个岗位同时要求**（kg_edge.requires）才可能成对，
   共现岗位数即边的权重。只取共现最多的若干对，低频尾巴不要。

因此每条边都能追溯到「哪些岗位同时要求了这两个技能」，evidence 里记了岗位数。
置信度标 derived（规则派生），可被人工在管理端覆盖。

用法：
    python scripts/seed_skill_prereqs.py --dry-run
    python scripts/seed_skill_prereqs.py --limit 60
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import connect  # noqa: E402
from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL  # noqa: E402
from backend.kg.pg_store.skill_prereq import add_prereq  # noqa: E402

# 白名单与前端分区展示顺序共用同一份定义（backend/kg/pg_store/skill_taxonomy.py），
# 避免「箭头方向」与「分区排列」互相矛盾。
from backend.kg.pg_store.skill_taxonomy import PREREQ_ALLOWED_PAIRS as ALLOWED_PAIRS  # noqa: E402

MAX_PREREQ_PER_SKILL = 3  # 每个技能最多几个前置，防止某个高频技能被挂满


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=60, help="最多写入多少条前置关系")
    ap.add_argument("--min-cooccur", type=int, default=2, help="最少共现岗位数")
    args = ap.parse_args()

    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT e.src_id AS occ, {SKILL_KEY_SQL} AS k, n.category AS cat
            FROM kg_edge e JOIN kg_node n ON n.id = e.dst_id
            WHERE e.rel_type = 'requires' AND n.type = 'skill_level'
              AND n.category IS NOT NULL
              AND COALESCE(n.status,'published') = 'published'
            """
        ).fetchall()

    # 岗位 -> {(技能, 分类)}
    by_occ: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for r in rows:
        by_occ[r["occ"]].add((r["k"], r["cat"]))

    # (前置, 后继) -> 共现岗位数
    pair_count: dict[tuple[str, str], int] = defaultdict(int)
    for skills in by_occ.values():
        items = list(skills)
        for ka, ca in items:
            for kb, cb in items:
                if ka == kb:
                    continue
                if (ca, cb) in ALLOWED_PAIRS:     # ka 是 kb 的前置
                    pair_count[(ka, kb)] += 1

    ranked = sorted(
        ((p, n) for p, n in pair_count.items() if n >= args.min_cooccur),
        key=lambda x: (-x[1], x[0][1], x[0][0]),
    )

    chosen: list[tuple[str, str, int]] = []
    per_target: dict[str, int] = defaultdict(int)
    seen_pair: set[tuple[str, str]] = set()
    for (pre, tgt), n in ranked:
        if len(chosen) >= args.limit:
            break
        if per_target[tgt] >= MAX_PREREQ_PER_SKILL:
            continue
        if (tgt, pre) in seen_pair:               # 反向已选，跳过避免互为前置
            continue
        chosen.append((pre, tgt, n))
        per_target[tgt] += 1
        seen_pair.add((pre, tgt))

    print(f"候选对 {len(pair_count)} · 达阈值 {len(ranked)} · 选中 {len(chosen)}\n")
    for pre, tgt, n in chosen:
        print(f"  {n:3d} 岗位共现   {pre}  ──→  {tgt}")

    if args.dry_run:
        print("\n[dry-run] 未写库")
        return 0

    ok = fail = 0
    for pre, tgt, n in chosen:
        try:
            add_prereq(
                tgt,
                pre,
                evidence=f"{n} 个岗位同时要求；职业功能顺序派生",
                confidence="derived",
                created_by="seed_skill_prereqs",
            )
            ok += 1
        except ValueError as e:
            print(f"  跳过 {pre} → {tgt}: {e}")
            fail += 1
    print(f"\n写入完成：成功 {ok} · 跳过 {fail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
