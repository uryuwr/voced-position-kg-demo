"""国标档位回填的前后对比与方向校验。

回填要做的事：8919 个老节点爬到了国标等级（`attrs.level_code` / `level_zh`），
但缺产品档 `attrs.level`，而代码只认后者，于是 608 个有技能构成的岗位里
490 个（80%）算不出匹配度。

**这个脚本存在的唯一理由是校验转换方向。** 国标刻度是反的：

    国标 L1 = 一级/高级技师 = 最高   →   产品档 5（专家）
    国标 L5 = 五级/初级工   = 最低   →   产品档 1（了解）

搞反了不会报错、不会崩，只会让**高级技师被判成入门水平**，而且分数看着挺正常，
没人会发现。所以这里逐档核对映射，而不是只看「有多少行被填上了」。

用法：
    python -X utf8 scripts/verify_backfill.py before   # 回填前存快照
    python -X utf8 scripts/verify_backfill.py after    # 回填后对比 + 方向校验

快照存 `.backfill_snapshot.json`（不入库）。
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SNAP = ROOT / ".backfill_snapshot.json"

# 国标 → 产品档 的**正确**映射。回填后逐档核对，错一档就报。
EXPECT = {
    "L1": 5, "L2": 4, "L3": 3, "L4": 2, "L5": 1,   # 五级工制，反向
    "T1": 1, "T2": 3, "T3": 5,                      # 三级制铺开
}


def metrics() -> dict[str, Any]:
    from backend.kg.pg_store.client import connect

    with connect() as c:
        # 岗位可评分性：requires 边指向的节点里有多少带产品档
        occ = c.execute(
            """
            SELECT SUM(CASE WHEN is_full THEN 1 ELSE 0 END) AS full_cnt,
                   SUM(CASE WHEN is_part THEN 1 ELSE 0 END) AS part_cnt,
                   SUM(CASE WHEN is_none THEN 1 ELSE 0 END) AS none_cnt,
                   COUNT(*) AS total
            FROM (
              SELECT o.id,
                     COUNT(*) = SUM(CASE WHEN (n.attrs::json->>'level') ~ '^[1-5]$'
                                         THEN 1 ELSE 0 END) AS is_full,
                     SUM(CASE WHEN (n.attrs::json->>'level') ~ '^[1-5]$' THEN 1 ELSE 0 END)
                         BETWEEN 1 AND COUNT(*) - 1 AS is_part,
                     SUM(CASE WHEN (n.attrs::json->>'level') ~ '^[1-5]$' THEN 1 ELSE 0 END) = 0
                         AS is_none
              FROM kg_edge e
              JOIN kg_node o ON o.id = e.src_id AND o.type='occupation'
                   AND COALESCE(o.status,'published')='published'
              JOIN kg_node n ON n.id = e.dst_id AND n.type='skill_level'
              WHERE e.rel_type='requires' AND COALESCE(e.status,'published')='published'
              GROUP BY o.id
            ) t
            """
        ).fetchone()

        # 逐档映射实况：源码 → 产品档 的分布
        pairs = c.execute(
            """
            SELECT COALESCE(attrs::json->>'level_code',
                            attrs::json->>'source_level_code') AS code,
                   (attrs::json->>'level') AS lv,
                   COUNT(*) AS n
            FROM kg_node
            WHERE type='skill_level'
              AND COALESCE(attrs::json->>'level_code',
                           attrs::json->>'source_level_code') IS NOT NULL
            GROUP BY 1, 2 ORDER BY 1, 2
            """
        ).fetchall()

        node = c.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN attrs::json->>'level' IS NOT NULL THEN 1 ELSE 0 END) AS with_lv
            FROM kg_node WHERE type='skill_level'
            """
        ).fetchone()

    return {
        "occupations": {k: int(occ[k] or 0) for k in ("full_cnt", "part_cnt", "none_cnt", "total")},
        "nodes": {"total": int(node["total"]), "with_level": int(node["with_lv"] or 0)},
        "code_to_level": [
            {"code": r["code"], "level": r["lv"], "n": int(r["n"])} for r in pairs
        ],
    }


def show(m: dict[str, Any], title: str) -> None:
    o, n = m["occupations"], m["nodes"]
    print(f"  {title}")
    print(f"    节点带产品档   {n['with_level']}/{n['total']}")
    print(f"    岗位可评分     齐全 {o['full_cnt']} · 部分 {o['part_cnt']} · "
          f"全缺 {o['none_cnt']}  （共 {o['total']}）")


def check_direction(m: dict[str, Any]) -> list[str]:
    """逐档核对映射方向。返回错误列表，空表示方向正确。"""
    bad: list[str] = []
    for row in m["code_to_level"]:
        code, lv = (row["code"] or "").strip().upper(), row["level"]
        if code not in EXPECT or lv is None:
            continue
        if int(lv) != EXPECT[code]:
            bad.append(f"{code} → {lv}（应为 {EXPECT[code]}），{row['n']} 个节点")
    return bad


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "after"
    now = metrics()

    if mode == "before":
        SNAP.write_text(json.dumps(now, ensure_ascii=False, indent=2), encoding="utf-8")
        show(now, "回填前")
        print(f"\n  快照已存 {SNAP}；回填后跑 `verify_backfill.py after`")
        return 0

    if not SNAP.exists():
        print(f"缺少回填前快照 {SNAP}；先跑 `verify_backfill.py before`")
        return 2
    before = json.loads(SNAP.read_text(encoding="utf-8"))

    show(before, "回填前")
    show(now, "回填后")

    b, a = before["occupations"], now["occupations"]
    gain = a["full_cnt"] - b["full_cnt"]
    print(f"\n  可评分岗位增加 {gain} 个"
          f"（{b['full_cnt']} → {a['full_cnt']}，占比 "
          f"{b['full_cnt'] / max(1, b['total']) * 100:.0f}% → "
          f"{a['full_cnt'] / max(1, a['total']) * 100:.0f}%）")

    print("\n  === 转换方向逐档核对（搞反了不会报错，只会把高手判成入门）===")
    for row in now["code_to_level"]:
        code = (row["code"] or "").strip().upper()
        exp = EXPECT.get(code)
        mark = "" if exp is None or (row["level"] and int(row["level"]) == exp) else "  ← 错！"
        print(f"    {code:<4} → 产品档 {str(row['level']):<5} {row['n']:>5} 个"
              f"{'' if exp is None else f'（应为 {exp}）'}{mark}")

    bad = check_direction(now)
    print(f"\n{'=' * 56}")
    if bad:
        print("方向校验不通过：")
        for x in bad:
            print(f"  {x}")
        print("  → 不要保留这次回填，先查映射表")
        return 1
    print("方向校验通过：L1→5（高级技师=专家）、L5→1（初级工=了解），刻度反转正确")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
