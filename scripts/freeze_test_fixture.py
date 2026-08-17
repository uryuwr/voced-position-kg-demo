"""把测试要用的图数据冻结成 fixture，让评分类验证不受库变动影响。

为什么需要
----------
库是共享的，并行的采集/入库工作随时在改数据。这轮真吃到了：同一个岗位（镗工）
上午生成的报告里 `required_level=5`，下午同一段代码读同一个 id 只拿到 `None`。

再往下查发现更根本的问题：8919 个 MOHRSS 老节点只有 `attrs.level_code`、
没有产品档 `attrs.level`，而代码只读后者，于是 608 个有构成的岗位里
**490 个（80%）要求档全缺失**，「齐全且今天未被动过」的是 **0 个**。

结论：算分逻辑的验证**必须脱离活库**，否则今天绿明天红，还分不清是
代码坏了还是数据变了。

做法
----
挑若干形态各异的岗位，把「岗位 + 技能构成 + 档位」抠成 JSON 存盘。
验证脚本从 fixture 读，结果完全确定，库怎么变都不影响。

fixture 里刻意保留**当前真实的脏形态**（要求档全缺、部分缺、权重和不为 1），
这些正是线上会遇到、也最容易打死接口的输入。不要「清洗」它们。

两条选样纪律（2026-08 补，都是踩过的坑）
--------------------------------------
1. **一个岗位只能占一个 tag。** 上一版按 WANTED 顺序各自独立挑，`no_baseline` 与
   `weight_unnormalized` 双双落在同一个岗位（大地测量工程技术人员）上：两个 tag 的
   样本字节级相同，`weight_unnormalized` 那一档等于什么都没测。现在按「候选最少的
   tag 先挑」分配，挑过的 id 不再复用。
2. **排除测试残留数据。** `__e2e_*` 技能节点由 tests/e2e_*.py 造、`临时测试*` /
   `ZZ回归*` 岗位由健壮性脚本造，它们会漂移甚至被清掉。上一版的 `partial_baseline`
   样本（混凝土工）其**全部**有要求档的技能都是 `__e2e_skill_*` 残留，还带重复
   skill_key —— 拿它当「真实的部分缺基准」是自欺欺人。

派生样本
--------
排掉残留后库里**没有**真正部分配置的岗位（详见输出里的 survey 块），而
「只算有基准的那部分」「缺口超阈值降级」这两条规则必须有这个形态才测得到。
所以允许从真实样本**派生**一份，并标 `synthetic: true` + `derived_from`：
读 fixture 的人一眼能看出哪份是库里真有的、哪份是造的。派生只做「把一部分
required_level 抹成 None」，其余字段（skill_key / category / weight）全是真数据。

用法：
    python -X utf8 scripts/freeze_test_fixture.py          # 重新冻结
    python -X utf8 scripts/freeze_test_fixture.py --show   # 只看摘要

输出：tests/fixtures/graph_subjects.json（入库，要可复现）
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

DEST = ROOT / "tests" / "fixtures" / "graph_subjects.json"

# 测试残留：技能节点前缀 / 岗位名前缀。选样与抠数据两处都要滤，
# 否则「排除了残留」只是把它从分类里去掉、样本里还留着。
EXCLUDE_SKILL_PREFIX = "__e2e"
EXCLUDE_OCC_PREFIXES = ("临时测试", "ZZ回归")

# 想覆盖的形态；找不到就如实记 missing，不要拿别的形态凑数
WANTED = (
    ("full_baseline", "要求档齐全：正常算分路径"),
    ("no_baseline", "要求档全缺：80% 的现实，应「无法评分」而非 0%"),
    ("partial_baseline", "要求档部分缺：只算有基准的那部分 + 缺口超阈值要降级"),
    ("weight_unnormalized", "权重和不为 1：weight_sum 是小数，曾害整页 500"),
    ("many_skills", "技能数 >10：题量上限与雷达轴回落都在这档触发"),
)

# 库里没有实例、但规则必须覆盖的形态 → 从哪个 tag 派生
DERIVE_FROM = {"partial_baseline": "full_baseline"}


def survey() -> list[dict[str, Any]]:
    """按岗位汇总技能构成。**已排除测试残留**，所以分类看到的就是真实形态。"""
    from backend.kg.pg_store.client import connect

    with connect() as c:
        return [
            dict(r) for r in c.execute(
                """
                SELECT o.id, o.name, COUNT(*) AS n,
                       SUM(CASE WHEN (n.attrs::json->>'level') ~ '^[1-5]$'
                                THEN 1 ELSE 0 END) AS with_lv,
                       SUM(COALESCE(e.weight, 0)) AS wsum
                FROM kg_edge e
                JOIN kg_node o ON o.id = e.src_id AND o.type='occupation'
                     AND COALESCE(o.status,'published')='published'
                JOIN kg_node n ON n.id = e.dst_id AND n.type='skill_level'
                WHERE e.rel_type='requires' AND COALESCE(e.status,'published')='published'
                  AND NOT starts_with(n.name, %s)
                  AND NOT (o.name LIKE ANY (%s))
                GROUP BY 1, 2
                """,
                (EXCLUDE_SKILL_PREFIX, [f"{p}%" for p in EXCLUDE_OCC_PREFIXES]),
            ).fetchall()
        ]


def classify(r: dict[str, Any]) -> set[str]:
    tags: set[str] = set()
    n, lv = int(r["n"]), int(r["with_lv"] or 0)
    if n and lv == n:
        tags.add("full_baseline")
    elif lv == 0:
        tags.add("no_baseline")
    else:
        tags.add("partial_baseline")
    w = float(r["wsum"] or 0)
    if w and not (0.85 <= w <= 1.15):
        tags.add("weight_unnormalized")
    if n > 10:
        tags.add("many_skills")
    return tags


def rank_key(tag: str, r: dict[str, Any]) -> tuple:
    """同一 tag 内的取样偏好。

    `weight_unnormalized` / `many_skills` 讲的是「权重」和「技能数」，可只有**能算分**
    的岗位才测得到它们对分数的影响 —— 所以优先挑有要求档的。库里当前一个都没有
    （见 survey 块），这个偏好是为数据补齐之后自动生效准备的。
    """
    prefer_scorable = tag in ("weight_unnormalized", "many_skills")
    scorable_first = 0 if (prefer_scorable and int(r["with_lv"] or 0)) else 1
    target = 14 if tag == "many_skills" else 5
    return (scorable_first, abs(int(r["n"]) - target), r["id"])


def capture(occ_id: str) -> dict[str, Any]:
    """抠出算分所需的最小闭包：岗位 + 构成条目（含档位与权重）。"""
    from backend.kg.pg_store.query import get_node
    from backend.kg.pg_store.skill_composition import get_composition

    occ = get_node(occ_id, scope="public") or {}
    comp = get_composition(occ_id)
    items = [
        {
            "skill_key": i.get("skill_key"),
            "category": i.get("category"),
            "required_level": i.get("selected_level"),
            "weight": i.get("weight"),
            "available_levels": i.get("available_levels"),
        }
        # 与 survey 同一条排除规则：残留技能不能留在样本里
        for i in (comp.get("items") or [])
        if not str(i.get("skill_key") or "").startswith(EXCLUDE_SKILL_PREFIX)
    ]
    return {
        "occupation": {
            "id": occ.get("id"),
            "name": occ.get("name"),
            "level": occ.get("level"),
            "description": (occ.get("description") or "")[:200],
        },
        # 映射与 service.load_context 一致，否则 fixture 喂不进 build_report
        "required_items": items,
        # 重算而不是照抄 comp 的汇总值：排掉残留之后原值就不对了
        "weight_sum": round(sum(float(i["weight"] or 0) for i in items), 6),
        "weighted": comp.get("weighted"),
    }


def derive_partial(base: dict[str, Any], base_tag: str) -> dict[str, Any]:
    """把一份「要求档齐全」的真实样本改造成「部分缺基准」。

    只动 `required_level`（隔一项抹成 None），skill_key / category / weight 全部保留
    原样 —— 这正是运营逐步补要求档时库里会出现的中间态。
    """
    items = [dict(i) for i in base["required_items"]]
    for i in items[1::2]:
        i["required_level"] = None
        i["available_levels"] = []
    kept = sum(1 for i in items if i["required_level"])
    return {
        **{k: v for k, v in base.items() if k != "why"},
        "required_items": items,
        "synthetic": True,
        "derived_from": {"tag": base_tag, "occupation_id": base["occupation"]["id"]},
        "derivation": (
            f"把 {len(items) - kept}/{len(items)} 项的 required_level 抹成 None；"
            "其余字段为真实图数据"
        ),
    }


def main() -> int:
    if "--show" in sys.argv:
        if not DEST.exists():
            print(f"还没有 fixture：{DEST}")
            return 1
        d = json.loads(DEST.read_text(encoding="utf-8"))
        print(f"冻结于 {d.get('frozen_at')}")
        for tag, s in (d.get("subjects") or {}).items():
            items = s["required_items"]
            lv = sum(1 for i in items if i.get("required_level"))
            flag = "（派生）" if s.get("synthetic") else ""
            print(f"  {tag:<22} {str(s['occupation']['name'])[:18]:<20} "
                  f"{len(items)} 项技能，{lv} 项有要求档，权重和 {s.get('weight_sum')}{flag}")
        for t in (d.get("missing") or []):
            print(f"  {t:<22} —— 库里当前没有这种形态，未冻结")
        for k, v in (d.get("survey") or {}).items():
            print(f"  survey.{k} = {v}")
        return 0

    rows = survey()
    print(f"扫到 {len(rows)} 个有技能构成的岗位（已排除测试残留）")

    # 候选最少的 tag 先挑，挑过的岗位不再给别的 tag 用 —— 否则稀缺形态会被
    # 「no_baseline 有 490 个候选」这种大类抢走，两个 tag 拿到同一个样本。
    pools = {tag: [r for r in rows if tag in classify(r)] for tag, _ in WANTED}
    order = sorted(WANTED, key=lambda tw: len(pools[tw[0]]))

    subjects: dict[str, Any] = {}
    missing: list[str] = []
    used: set[str] = set()
    for tag, why in order:
        cand = [r for r in sorted(pools[tag], key=lambda r: rank_key(tag, r))
                if r["id"] not in used]
        if not cand:
            missing.append(tag)
            print(f"  [缺] {tag:<22} 候选 {len(pools[tag])} 个（去重后 0）· {why}")
            continue
        pick = cand[0]
        used.add(pick["id"])
        subjects[tag] = capture(pick["id"]) | {"why": why}
        print(f"  [取] {tag:<22} {str(pick['name'])[:18]:<20} {pick['n']} 项"
              f"（该形态共 {len(pools[tag])} 个候选）")

    # 库里没有实例的形态：从真实样本派生，标明是造的
    for tag, base_tag in DERIVE_FROM.items():
        if tag in subjects or base_tag not in subjects:
            continue
        why = dict(WANTED)[tag]
        subjects[tag] = derive_partial(subjects[base_tag], base_tag) | {
            "why": f"{why}（库里无实例，由 {base_tag} 派生）"
        }
        missing.remove(tag)
        print(f"  [派生] {tag:<20} 自 {base_tag}（库里 0 个实例）")

    scorable = [r for r in rows if int(r["with_lv"] or 0)]
    survey_facts = {
        "occupations_with_composition": len(rows),
        "scorable": len(scorable),
        "by_shape": {tag: len(pool) for tag, pool in pools.items()},
        "scorable_and_weight_unnormalized": sum(
            1 for r in scorable if "weight_unnormalized" in classify(r)
        ),
        "scorable_and_many_skills": sum(1 for r in scorable if "many_skills" in classify(r)),
        "note": (
            "能算分的岗位全部来自新采集数据，形态统一（权重≈1、技能数≤10）；"
            "权重不归一 / 技能数多 / 部分缺基准这三种形态目前只出现在**算不出分**的老数据上。"
            "所以 weight_unnormalized 与 many_skills 这两档现在测不到「脏权重/多技能如何影响分数」，"
            "等这类岗位补上要求档后重新冻结即可自动改善（见 rank_key 的 scorable 偏好）。"
        ),
    }

    from datetime import UTC, datetime

    DEST.parent.mkdir(parents=True, exist_ok=True)
    DEST.write_text(
        json.dumps(
            {
                "frozen_at": datetime.now(UTC).isoformat(),
                "note": (
                    "由 scripts/freeze_test_fixture.py 生成。刻意保留真实脏形态，不要清洗。"
                    "一个岗位只占一个 tag；已排除 __e2e_* / 临时测试* 等测试残留。"
                    "带 synthetic=true 的样本是派生的，不是库里真有的形态。"
                ),
                "subjects": subjects,
                "missing": missing,
                "survey": survey_facts,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )
    print(f"\n已写入 {DEST}（{len(subjects)} 个样本，{len(missing)} 个形态缺失）")
    ids = [s["occupation"]["id"] for s in subjects.values() if not s.get("synthetic")]
    assert len(ids) == len(set(ids)), f"同一岗位被多个 tag 复用了：{ids}"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
