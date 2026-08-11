"""把 skill_level 节点的等级刻度一次性迁到产品语义，消灭运行时映射。

背景
----
库里 8919 个 skill_level 节点原先存的是**国标原码**：
    attrs.level_code = L1..L5，其中 L1=一级/高级技师（最高）、L5=五级/初级工（最低）
另有 625 个走的是专业技术三级制 T1/T2/T3（初级/中级/高级）。
产品侧只有一套 L1–L5（了解→掌握→熟练→精通→专家，**越大越强**），方向与国标相反，
于是读路径长期挂着 level_map 做运行时反转 —— 而只要有一条路径漏传 level_zh，
就会把国标 L4 当成产品 L4，造成「提交 level=4、回显 selected_level=2」这类不一致。

迁移后（两阶段，一次跑完）
--------------------------
阶段 1 · 统一刻度
    attrs.level             = int 1..5   产品语义，唯一真源（越大越强）
    attrs.source_level_code = 'L4'/'T3'  国标原码，只留在 attrs 内做溯源，不对外输出
    移除 attrs.level_code（语义已被 level 取代，留着必然再被误读）与 attrs.product_level_int。

阶段 2 · 剥离国标等级文案
    「四级/中级工」这类国标等级名与产品 L1–L5（了解/掌握/熟练/精通/专家）是两套说法，
    同屏出现必然打架（且数字方向相反：国标四级 = 产品 L2）。因此：
    移除 attrs.level_zh；
    name        「制备 · 三级/高级工」 → 「制备 · L3」
    description 占位串里的「· 四级/中级工 ·」→「· L2 ·」（国标正文描述不受影响）

id / source_id 不动
-------------------
标识符形如 `CN:skill_level:MOHRSS_CN:4-04-05-07|运维|L2`，其中 L2 是**源系统**的
等级码。它是不透明标识，且采集端 source_id 继续用国标原码生成，重跑不会产生重复节点。

幂等：两阶段各自判断，已完成的部分跳过；可反复执行。
用法：python -X utf8 scripts/migrate_skill_level_to_product.py [--apply]
不带 --apply 只做 dry-run，打印将要变更的分布。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import connect  # noqa: E402

# 国标等级原文名 → 产品 level。精确匹配（库里 level_zh 就是这 8 个规范值），
# 不用子串匹配——「高级」会误伤「高级工」「高级技师」。
ZH_TO_LEVEL = {
    "五级/初级工": 1,
    "四级/中级工": 2,
    "三级/高级工": 3,
    "二级/技师": 4,
    "一级/高级技师": 5,
    # 专业技术人才三级制：三档均匀铺到产品五档（L2/L4 因源数据无此粒度而空缺）
    "初级": 1,
    "中级": 3,
    "高级": 5,
}

# level_zh 缺失时的兜底：国标五级码需反转（L1=一级=最高→5），T 码按三级制铺开
CODE_TO_LEVEL = {
    "L1": 5, "L2": 4, "L3": 3, "L4": 2, "L5": 1,
    "T1": 1, "T2": 3, "T3": 5,
}


def product_level(level_zh: str | None, level_code: str | None) -> int | None:
    zh = (level_zh or "").strip()
    if zh in ZH_TO_LEVEL:
        return ZH_TO_LEVEL[zh]
    return CODE_TO_LEVEL.get((level_code or "").strip().upper())


# 只替换被「·」包住或位于串尾的等级词，避免误伤国标正文里的「初级工应能…」
GRADE_IN_TEXT = re.compile(
    r"·\s*(一级/高级技师|二级/技师|三级/高级工|四级/中级工|五级/初级工|初级|中级|高级)"
    r"(?=\s*·|\s*$)"
)


def skill_key_of(attrs: dict, name: str) -> str:
    """与 SKILL_KEY_SQL 一致：attrs.skill_key → skill_name → name 首段。"""
    for k in ("skill_key", "skill_name"):
        v = str(attrs.get(k) or "").strip()
        if v:
            return v
    return (name or "").split(" · ")[0].split("·")[0].strip() or (name or "")


def main(apply: bool) -> int:
    stats: dict[tuple[str, str, int | None], int] = {}
    skipped = unresolved = 0
    phase2 = 0
    updates: list[tuple[str, str, str, str]] = []

    with connect() as conn:
        rows = conn.execute(
            "SELECT id, attrs, name, description FROM kg_node WHERE type='skill_level'"
        ).fetchall()
        print(f"skill_level 节点总数: {len(rows)}")

        for r in rows:
            try:
                a = json.loads(r["attrs"]) if isinstance(r["attrs"], str) else (r["attrs"] or {})
            except Exception:
                a = {}
            if not isinstance(a, dict):
                a = {}

            code = str(a.get("level_code") or "").strip().upper()
            zh = a.get("level_zh")
            name = r["name"] or ""
            desc = r["description"] or ""

            # —— 阶段 1：统一刻度 ——
            need1 = a.get("level") is None or bool(code) or "product_level_int" in a
            if need1:
                lv = product_level(zh, code)
                if lv is None:
                    unresolved += 1
                    print(f"  [未能判定] {r['id']} level_zh={zh!r} level_code={code!r}")
                    continue

                # 旧 backfill 写过 product_level_int，不一致要暴露出来而非默默覆盖
                old_pli = a.get("product_level_int")
                if old_pli is not None and int(old_pli) != lv:
                    print(f"  [冲突] {r['id']} product_level_int={old_pli} 但按等级名应为 {lv}")

                stats[(code or "?", str(zh), lv)] = stats.get((code or "?", str(zh), lv), 0) + 1
                a["level"] = lv
                if code:
                    a["source_level_code"] = code   # 只留 attrs 内溯源，不对外输出
                a.pop("level_code", None)           # 语义已由 level 承担
                a.pop("product_level_int", None)    # 与 level 重复
            else:
                lv = a.get("level")

            # —— 阶段 2：剥离国标等级文案 ——
            need2 = ("level_zh" in a) or GRADE_IN_TEXT.search(name) or GRADE_IN_TEXT.search(desc)
            if need2:
                phase2 += 1
                a.pop("level_zh", None)
                key = skill_key_of(a, name)
                name = f"{key} · L{lv}" if lv else key
                desc = GRADE_IN_TEXT.sub(f"· L{lv}" if lv else "", desc)

            if need1 or need2:
                updates.append(
                    (json.dumps(a, ensure_ascii=False), name, desc, r["id"])
                )
            else:
                skipped += 1

        if stats:
            print("\n=== 阶段1 变更分布（国标原码 / 国标等级名 → 产品 level）===")
            for (code, zh, lv), n in sorted(stats.items(), key=lambda x: (x[0][2] or 0, x[0][0])):
                print(f"  {code:4s} {zh:16s} → level={lv}  {n:6d}")
        print(f"\n阶段1 待迁移 {sum(stats.values())} · 阶段2 待剥离文案 {phase2}")
        print(f"合计更新 {len(updates)} · 已完成跳过 {skipped} · 无法判定 {unresolved}")
        if updates:
            print("\n=== 变更样例 ===")
            for u in updates[:3]:
                print(f"  name → {u[1][:44]}")
                print(f"  desc → {(u[2] or '')[:80]}")

        if not apply:
            print("\n[dry-run] 未写库。加 --apply 执行。")
            return 0
        if unresolved:
            print("\n[中止] 存在无法判定等级的节点，请先修数据再迁移。")
            return 1

        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE kg_node SET attrs=%s, name=%s, description=%s WHERE id=%s", updates
            )
        conn.commit()
        print(f"\n[已提交] 更新 {len(updates)} 个节点。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--apply" in sys.argv))
