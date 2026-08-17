"""给需要连库的测试提供「稳定受试对象」+ 漂移检测。

背景：这个 PG 是共享的，并行的采集/入库工作随时在改图数据。已经真吃到过：
同一个岗位上午 `required_level=5`、下午 `None`，同一段代码两个结果。
测试跟着数据飘，红了也分不清是代码坏了还是数据变了。

两条路，按场景选：

1. **算分/报告类逻辑** → 用 `tests/fixtures/graph_subjects.json`（`load_fixture`）。
   完全脱库，结果确定。由 `scripts/freeze_test_fixture.py` 冻结。
2. **必须连库的 e2e** → 用 `pick_subject()` 按不变量现挑，并用 `fingerprint()`
   在测试首尾各取一次；不一致就说明数据在测试期间被改了，
   这时候应该报「数据漂移」而不是报测试失败。

不变量优于硬编码 id：写死某个岗位 id，等它被并行工作改掉，测试就假红。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "graph_subjects.json"


class DataDrift(RuntimeError):
    """受试数据在测试期间被改动。不是代码缺陷，需要重跑或换受试对象。"""


def load_fixture(tag: str | None = None) -> Any:
    """读冻结的图数据样本。tag 见 freeze_test_fixture.WANTED。

    一个 tag 对应一个岗位、互不复用，且已排除 `__e2e_*` 之类的测试残留。
    个别形态库里没有实例（当前是 `partial_baseline`），由真实样本**派生**而来，
    带 `synthetic: True` + `derived_from`；断言「这是库里真有的形态」时要看这两个键。
    JSON 里的 `survey` 块记着冻结时各形态各有多少候选，解释了为什么要派生。
    """
    if not FIXTURE.exists():
        raise FileNotFoundError(
            f"缺少 {FIXTURE}；先跑 python -X utf8 scripts/freeze_test_fixture.py"
        )
    d = json.loads(FIXTURE.read_text(encoding="utf-8"))
    subs = d.get("subjects") or {}
    if tag is None:
        return subs
    if tag not in subs:
        raise KeyError(f"fixture 里没有 {tag}；现有：{sorted(subs)}")
    return subs[tag]


def fingerprint(occ_id: str) -> str:
    """受试岗位「技能构成」的指纹。变了就说明数据被动过。

    只取影响算分的三项（skill_key / 要求档 / 权重）——节点描述之类改了不影响结论，
    不该把它算进漂移。
    """
    from backend.kg.pg_store.skill_composition import get_composition

    items = sorted(
        (
            str(i.get("skill_key")),
            str(i.get("selected_level")),
            f"{float(i.get('weight') or 0):.4f}",
        )
        for i in (get_composition(occ_id).get("items") or [])
    )
    return hashlib.sha256(json.dumps(items, ensure_ascii=False).encode()).hexdigest()[:16]


def pick_subject(
    *,
    need_baseline: bool = True,
    min_skills: int = 3,
    max_skills: int = 8,
    exclude_names: str = "",
) -> dict[str, Any] | None:
    """按不变量现挑一个受试岗位；挑不到返回 None（调用方应 SKIP 而非 FAIL）。

    `need_baseline=True` 要求所有技能都有产品档 `attrs.level`——注意当前库里
    这类岗位**全部来自今天新建的 LLM_CN 数据**，老 MOHRSS 的 8919 个节点
    只有 `level_code`，产品档为空。所以这个筛选会强烈偏向新数据。
    """
    from backend.kg.pg_store.client import connect

    having = "COUNT(*) = SUM(CASE WHEN (n.attrs::json->>'level') ~ '^[1-5]$' THEN 1 ELSE 0 END)"
    with connect() as c:
        rows = c.execute(
            f"""
            SELECT o.id, o.name, COUNT(*) AS n
            FROM kg_edge e
            JOIN kg_node o ON o.id = e.src_id AND o.type='occupation'
                 AND COALESCE(o.status,'published')='published'
            JOIN kg_node n ON n.id = e.dst_id AND n.type='skill_level'
            WHERE e.rel_type='requires' AND COALESCE(e.status,'published')='published'
            GROUP BY 1, 2
            HAVING COUNT(*) BETWEEN %s AND %s {'AND ' + having if need_baseline else ''}
            ORDER BY COUNT(*), o.id
            LIMIT 40
            """,
            (min_skills, max_skills),
        ).fetchall()
    for r in rows:
        if r["name"] and r["name"] in exclude_names:
            continue
        return {"id": r["id"], "name": r["name"], "n": int(r["n"]),
                "fingerprint": fingerprint(r["id"])}
    return None


def assert_no_drift(occ_id: str, before: str) -> None:
    """测试收尾时调。数据变了就抛 DataDrift，让调用方区别对待。"""
    now = fingerprint(occ_id)
    if now != before:
        raise DataDrift(
            f"受试岗位 {occ_id} 的技能构成在测试期间变了（{before} → {now}）。"
            "并行的采集/入库工作会改图数据；本次结论不可信，请重跑。"
        )
