"""岗位晋升链 advances_to 物化 · 派生结果落成 kg_edge 真边。

背景
----
此前晋升链是 `/v1/capability` 的运行时派生（返回 progressions[]），不落库，
导致它在通用图接口（expand / explore / by-industry）里查不到、也无法被管理端编辑审核。
本脚本把派生结果物化成 kg_edge 真边：rel_type='advances_to'、structure_layer='chain'。

派生规则（与原运行时逻辑一致）
------------------------------
同一岗位族内按 level 分层，仅相邻层之间连边；每个下层岗位按名称最长公共前缀
选唯一上层目标（schema 约定 advances_to 为 1:1 有向），避免 N×M 交叉。
岗位族 = 人社部职业分类大典小类编码 minor_code，无编码时回落名称主干。

⚠️ 数据现实
-----------
库内 occupation 全部来自《职业分类大典》，该数据源**不含职级维度**：
1329 个岗位中 1301 个 level 相同，同小类内成职级序列的仅 2 组且实为并列方向。
因此本脚本当前只能物化出个位数条边。机制先行，待企业职级表 / 招聘级别数据
接入并回填 occupation.level 后，重跑本脚本即可自动产出完整晋升链。

用法：
    python scripts/materialize_advances_to.py --dry-run
    python scripts/materialize_advances_to.py            # 写库（先清旧的 derived 边）
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import connect  # noqa: E402
from backend.kg.pg_store.query import _derive_progressions  # noqa: E402

SOURCE_SYSTEM = "DERIVED"
EDGE_ID = "CN:advances_to:{src}->{dst}"

# ── ladder 模式：按岗位名的职级后缀阶梯识别真实晋升通道 ──
# 依据：我国职业体系中「技能人才（工/员）」与「专业技术人才（工程技术人员）」
# 之间存在真实的上升通道，人社部近年新职业（如快递工程技术人员、农业工程技术人员）
# 正是为技能岗打通的技术晋升口。仅在**同词干**（去掉后缀后前缀一致）时才配对，
# 因此不会把「教练员→体育经理人」这类并列转岗误判成晋升。
LADDER_SUFFIX: list[tuple[str, int]] = [
    ("操作工", 1),
    ("工", 1),
    ("员", 1),
    ("技术员", 2),
    ("技师", 3),
    ("工程技术人员", 4),
    ("工程师", 4),
]


def _stem_rank(name: str) -> tuple[str, str, int] | None:
    for suf, rk in sorted(LADDER_SUFFIX, key=lambda x: -len(x[0])):
        if name.endswith(suf):
            return name[: -len(suf)], suf, rk
    return None


def ladder_progressions(occs: list[dict]) -> list[dict]:
    """同词干、职级后缀相邻的岗位对 → 晋升边（1:1 有向）。"""
    buckets: dict[str, list[tuple[str, str, int, str]]] = {}
    for o in occs:
        n = str(o.get("name") or "")
        r = _stem_rank(n)
        if not r or len(r[0]) < 2:
            continue
        buckets.setdefault(r[0], []).append((n, r[1], r[2], o["id"]))
    out: list[dict] = []
    for stem, items in buckets.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda x: x[2])
        for a, b in zip(items, items[1:]):
            if b[2] <= a[2]:
                continue
            out.append(
                {
                    "from": a[3],
                    "to": b[3],
                    "from_name": a[0],
                    "to_name": b[0],
                    "from_level": a[2],
                    "to_level": b[2],
                    "evidence": f"职级后缀阶梯「{a[1]}→{b[1]}」，同词干「{stem}」",
                }
            )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--region", default="CN")
    ap.add_argument(
        "--mode",
        choices=["ladder", "level"],
        default="ladder",
        help="ladder=职级后缀阶梯(manual_seed，可信)；level=按 occupation.level 派生(derived，"
        "当前 level 为 mock，产出含明显错误，慎用)",
    )
    args = ap.parse_args()
    confidence = "manual_seed" if args.mode == "ladder" else "derived"

    with connect() as conn:
        occs = conn.execute(
            """
            SELECT id, name, level, attrs, region
            FROM kg_node
            WHERE type='occupation' AND COALESCE(status,'published')='published'
              AND region = %s
            """,
            (args.region,),
        ).fetchall()
        rows = [dict(o) for o in occs]
        if args.mode == "ladder":
            progs = ladder_progressions(rows)
        else:
            progs = _derive_progressions(rows)
        print(f"岗位 {len(occs)} 个 · mode={args.mode} · 晋升边 {len(progs)} 条")
        for p in progs[:20]:
            print(f"  L{p['from_level']} {p['from_name']}  ──→  L{p['to_level']} {p['to_name']}")

        if args.dry_run:
            print("\n[dry-run] 未写库")
            return 0

        # 只清本脚本此前以同一 confidence 物化的边，不动其他来源/人工维护的
        deleted = conn.execute(
            # 不碰草稿边：那是运营还没发布的改动，不该被物化脚本顺手清掉
            "DELETE FROM kg_edge WHERE rel_type='advances_to' "
            "AND source_system=%s AND region=%s AND confidence=%s "
            "AND NOT is_draft",
            (SOURCE_SYSTEM, args.region, confidence),
        ).rowcount

        now = datetime.now(timezone.utc).isoformat()
        written = 0
        for p in progs:
            conn.execute(
                """
                INSERT INTO kg_edge
                  (id, src_id, dst_id, rel_type, region, evidence,
                   source_system, source_url, license, fetched_at,
                   confidence, status, structure_layer)
                VALUES (%s,%s,%s,'advances_to',%s,%s,%s,%s,%s,%s,%s,'published','chain')
                ON CONFLICT (id) DO UPDATE SET
                  evidence = EXCLUDED.evidence,
                  fetched_at = EXCLUDED.fetched_at,
                  confidence = EXCLUDED.confidence,
                  structure_layer = EXCLUDED.structure_layer
                """,
                (
                    EDGE_ID.format(src=p["from"], dst=p["to"]),
                    p["from"],
                    p["to"],
                    args.region,
                    p.get("evidence")
                    or f"同岗位族内 L{p['from_level']}→L{p['to_level']} 递进派生",
                    SOURCE_SYSTEM,
                    f"internal://{args.mode}/advances_to",
                    "internal",
                    now,
                    confidence,
                ),
            )
            written += 1
        print(f"\n已清理旧边 {deleted} 条 · 写入 {written} 条 advances_to"
              f"（confidence={confidence}, structure_layer=chain）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
