"""backend/agent/assessment/bank.py —— 题量规划、技能选取、模型输出解析。

`_extract` 是重点：模型被 max_tokens 截断时整包 JSON 不可解析，兜底逐个扫出已闭合
的题目对象。这条路径线上会真的走到，但 e2e 打不到（要网关刚好把包截断）。
"""
from __future__ import annotations

import pytest

from backend.agent.assessment.bank import (
    COVERAGE_TARGET,
    MAX_BATCHES,
    MAX_PER_CATEGORY,
    MAX_PER_SKILL,
    MAX_QUESTIONS,
    MIN_COVER_QUESTIONS,
    MIN_QUESTIONS,
    OPEN_PER_RUN,
    RADAR_MIN_SKILLS,
    VERIFY_LEVEL,
    _extract,
    _fit_budget,
    _is_real_requirement,
    _norm_choice,
    _norm_open,
    _requirement_hint,
    estimate_total,
    fallback_choice,
    fallback_open,
    plan_all_skills,
    plan_batch_skills,
    should_stop,
)


def it(key, *, weight=0.25, level=3, category="生产准备", **extra):
    return {
        "skill_key": key,
        "weight": weight,
        "required_level": level,
        "category": category,
        **extra,
    }


# ── 常量：出题规模的产品口径，改动必须是有意的 ─────────────────


def test_收敛常量():
    assert (MAX_QUESTIONS, MIN_QUESTIONS, COVERAGE_TARGET) == (10, 4, 0.8)
    assert (MIN_COVER_QUESTIONS, RADAR_MIN_SKILLS, OPEN_PER_RUN) == (4, 3, 2)
    assert (VERIFY_LEVEL, MAX_PER_SKILL, MAX_PER_CATEGORY, MAX_BATCHES) == (4, 2, 3, 4)


# ── estimate_total ────────────────────────────────────────────


class TestEstimateTotal:
    def test_没有技能构成时零题(self):
        est = estimate_total([])
        assert est == {"total": 0, "cover": 0, "verify": 0, "reason": "该岗位没有技能构成"}

    def test_权重集中也不会只出三题(self):
        """一个技能占 80% 权重时覆盖题只需 1 项，但下限把题量顶到 MIN_QUESTIONS。"""
        items = [it("A", weight=0.8), it("B", weight=0.1), it("C", weight=0.05), it("D", weight=0.05)]
        est = estimate_total(items)
        assert est["cover"] == 4       # max(1, RADAR_MIN_SKILLS, MIN_COVER_QUESTIONS) 后按技能数封顶
        assert est["total"] == MIN_QUESTIONS

    def test_技能数不足时以技能总数封顶(self):
        est = estimate_total([it("A", weight=1.0)])
        assert est["cover"] == 1, "只有 1 项技能的岗位出不了 4 道覆盖题"
        assert est["total"] == MIN_QUESTIONS

    def test_权重全为零时按下限取覆盖题(self):
        est = estimate_total([it(f"S{i}", weight=0) for i in range(6)])
        assert est["cover"] == MIN_COVER_QUESTIONS
        assert est["verify"] == 0

    def test_权重分散时按累计到八成算覆盖题(self):
        # 用二进制可精确表示的权重：0.125 × 6 = 0.75 < 0.8 ≤ 0.875 = 0.125 × 7
        items = [it(f"S{i}", weight=0.125, level=2) for i in range(8)]
        est = estimate_total(items)
        assert est["cover"] == 7
        assert est["total"] == 7

    def test_权重累计用浮点所以边界上会多取一项(self):
        """0.1 累计十次不是 1.0；第 8 项算出来差一点点到 80%，于是取 9 项。

        当前行为存档：换成 Decimal / round 会让题量变化，属于产品口径改动。
        """
        items = [it(f"S{i}", weight=0.1, level=2) for i in range(10)]
        assert estimate_total(items)["cover"] == 9

    def test_高要求档追加验证题且上限两道(self):
        items = [it(f"S{i}", weight=0.125, level=5) for i in range(8)]
        est = estimate_total(items)
        assert est["verify"] == OPEN_PER_RUN
        assert est["total"] == 7 + OPEN_PER_RUN

    def test_验证题只看覆盖范围内的技能(self):
        # 前 4 项是低要求档、权重高；L5 的那项权重极低，排在覆盖范围外
        items = [it(f"S{i}", weight=0.24, level=2) for i in range(4)] + [it("X", weight=0.04, level=5)]
        est = estimate_total(items)
        assert est["cover"] == 4
        assert est["verify"] == 0, "覆盖范围外的高要求档技能不该产生验证题"

    def test_总题数不超过硬上限(self):
        items = [it(f"S{i}", weight=0.05, level=5) for i in range(20)]
        est = estimate_total(items)
        assert est["total"] == MAX_QUESTIONS
        # `cover`（按权重覆盖 80% 需要几项技能，这里算出来是 16）是中间量：
        # 它要不要一起夹到硬上限，属于 BUG-3 修复的实现选择，不在这里钉死。
        # 「实际排出的题数 ≤ MAX_QUESTIONS」的权威断言在 test_assessment_pipeline.py。
        assert est["cover"] >= MIN_COVER_QUESTIONS
        assert est["verify"] == OPEN_PER_RUN

    def test_理由串说明为什么是这个题数(self):
        est = estimate_total([it("A", weight=1.0, level=5)])
        assert "按权重覆盖 80%" in est["reason"]
        assert "要求档≥L4" in est["reason"]


# ── _fit_budget：两类题各有下限（定案 2026-08）─────────────────
#
# 「把想考的题数压进 budget」不能一刀切，也不能只保覆盖题。
# 旧实现验证题纯按比例缩（`round(budget * verify / demand)`，兜底留 1 道），
# 方向反了：技能越多 → demand 越大 → 验证题占比越小。实测 4/8/12 项技能排 2 道，
# 20/28/40 项**只剩 1 道** —— 而验证题是唯一要模型判分的深度考核手段，
# 要求档越高的岗位越需要它。
#
# 定案：验证题下限 = min(需求量, OPEN_PER_RUN)，名额从覆盖题让；
# 两个下限装不下时（budget < cover_floor + OPEN_PER_RUN）优先保覆盖题——
# 雷达画不出来整份报告就没有形状，验证题少一道只是深度弱一点。
# 「实排题数」的权威断言在 test_assessment_pipeline.py::TestCapKeepsSemantics。

FLOOR = 4       # 真实路径上的 cover_floor（= max(RADAR_MIN_SKILLS, MIN_COVER_QUESTIONS)）


class TestFitBudget:
    def test_装得下就原样返回(self):
        assert _fit_budget(4, 2, budget=10, cover_floor=FLOOR) == (4, 2)
        assert _fit_budget(8, 2, budget=10, cover_floor=FLOOR) == (8, 2)

    @pytest.mark.parametrize("cover", [9, 10, 13, 16, 23, 32])
    def test_压缩时验证题保住下限(self, cover):
        """真实路径：budget 恒为 MAX_QUESTIONS，验证题需求量恒为 OPEN_PER_RUN。"""
        c, v = _fit_budget(cover, OPEN_PER_RUN, budget=MAX_QUESTIONS, cover_floor=FLOOR)
        assert v == OPEN_PER_RUN, "验证题被按比例缩掉了 —— 技能越多反而越少考深度"
        assert c == MAX_QUESTIONS - OPEN_PER_RUN
        assert c + v == MAX_QUESTIONS

    def test_验证题需求少于下限时不凭空造(self):
        assert _fit_budget(16, 1, budget=10, cover_floor=FLOOR) == (9, 1)
        assert _fit_budget(16, 0, budget=10, cover_floor=FLOOR) == (10, 0)

    def test_覆盖题需求少于下限时不凭空造(self):
        """1 项技能的岗位排不出 4 道覆盖题，下限得按需求量收。"""
        assert _fit_budget(2, 2, budget=3, cover_floor=FLOOR) == (2, 1)

    def test_预算装不下两个下限时优先保覆盖题(self):
        """cover_floor(4) + OPEN_PER_RUN(2) = 6 > budget，此时验证题让位。"""
        c, v = _fit_budget(3, 2, budget=4, cover_floor=FLOOR)
        assert (c, v) == (3, 1)
        c, v = _fit_budget(10, 2, budget=4, cover_floor=FLOOR)
        assert (c, v) == (FLOOR, 0), "覆盖题下限优先，验证题可以为 0"

    def test_总数永不超预算(self):
        for cover in range(0, 40):
            for verify in range(0, 5):
                c, v = _fit_budget(cover, verify, budget=MAX_QUESTIONS, cover_floor=FLOOR)
                assert c + v <= MAX_QUESTIONS, (cover, verify, c, v)
                assert c <= cover and v <= verify, "不能排出比需求还多的题"
                assert c >= 0 and v >= 0

    @pytest.mark.parametrize("budget", [0, -1])
    def test_预算为零或负时原样返回不做压缩(self, budget):
        assert _fit_budget(5, 2, budget=budget, cover_floor=FLOOR) == (5, 2)


# ── plan_all_skills ───────────────────────────────────────────


class TestPlanAllSkills:
    """这里只测「给定 cover/verify 时怎么选技能」。

    「整场题数不超过 MAX_QUESTIONS」是**出题路径**的口径，断言在
    tests/unit/test_assessment_pipeline.py（`plan_questions` 才是 service 调的入口，
    夹在这一层还是夹在 estimate_total 属于实现选择）。
    """

    def test_覆盖题按权重降序(self):
        items = [it("低", weight=0.1), it("高", weight=0.6), it("中", weight=0.3)]
        plan = plan_all_skills(items, {"cover": 3, "verify": 0})
        assert [p["skill_key"] for p in plan] == ["高", "中", "低"]
        assert {p["_item_type"] for p in plan} == {"choice"}
        assert {p["_reason"] for p in plan} == {"coverage"}

    def test_同大类限额三项(self):
        items = [it(f"S{i}", weight=1 - i / 100, category="同一类") for i in range(6)]
        plan = plan_all_skills(items, {"cover": 3, "verify": 0})
        assert len(plan) == 3

    def test_限额卡住候选时放开补足(self):
        """技能几乎同属一类的岗位（如计算机程序设计员）不能因为限额少考。"""
        items = [it(f"S{i}", weight=1 - i / 100, category="同一类") for i in range(6)]
        plan = plan_all_skills(items, {"cover": 5, "verify": 0})
        assert len(plan) == 5
        assert [p["skill_key"] for p in plan] == ["S0", "S1", "S2", "S3", "S4"]

    def test_验证题追加在覆盖题之后且只给高要求档(self):
        items = [it("高标准", weight=0.5, level=5), it("普通", weight=0.5, level=2)]
        plan = plan_all_skills(items, {"cover": 2, "verify": 1})
        assert [(p["skill_key"], p["_item_type"]) for p in plan] == [
            ("高标准", "choice"),
            ("普通", "choice"),
            ("高标准", "open"),
        ]
        assert plan[-1]["_reason"] == "verify_high_bar"

    def test_不传估算时自己算一份(self):
        items = [it(f"S{i}", weight=0.25, level=2) for i in range(4)]
        assert len(plan_all_skills(items)) == 4

    def test_不改原始条目(self):
        items = [it("A", weight=1.0)]
        plan_all_skills(items, {"cover": 1, "verify": 0})
        assert "_item_type" not in items[0]


# ── plan_batch_skills ─────────────────────────────────────────


class TestPlanBatchSkills:
    def test_没答过时只出覆盖用的选择题(self):
        items = [it(f"S{i}", weight=0.25, category=f"C{i}") for i in range(4)]
        batch = plan_batch_skills(items, size=3)
        assert len(batch) == 3
        assert {b["_item_type"] for b in batch} == {"choice"}

    def test_已出过题的技能不再排covered(self):
        items = [it("A", weight=0.9, category="甲"), it("B", weight=0.1, category="乙")]
        batch = plan_batch_skills(items, asked={"A": 1}, size=3)
        assert [b["skill_key"] for b in batch] == ["B"]

    def test_声称达标的技能追加问答题验证(self):
        items = [it("A", weight=0.9, level=3, category="甲"), it("B", weight=0.1, category="乙")]
        graded = [{"skill_key": "A", "type": "choice", "level": 4, "required_level": 3}]
        batch = plan_batch_skills(items, asked={"A": 1}, graded=graded, size=2)
        assert ("A", "open") in [(b["skill_key"], b["_item_type"]) for b in batch]
        assert batch[0]["_reason"] == "verify_claim"

    def test_明显不足的技能不再深挖(self):
        items = [it("A", weight=0.9, level=4, category="甲"), it("B", weight=0.1, category="乙")]
        graded = [{"skill_key": "A", "type": "choice", "level": 1, "required_level": 4}]
        batch = plan_batch_skills(items, asked={"A": 1}, graded=graded, size=2)
        assert [b["skill_key"] for b in batch] == ["B"]

    def test_单技能出题数达上限就不再追加(self):
        items = [it("A", weight=1.0, level=3)]
        graded = [{"skill_key": "A", "type": "choice", "level": 5, "required_level": 3}]
        batch = plan_batch_skills(items, asked={"A": MAX_PER_SKILL}, graded=graded, size=3)
        assert batch == []

    def test_验证题不超过本批一半(self):
        items = [it(f"S{i}", weight=0.2, level=3, category=f"C{i}") for i in range(5)]
        graded = [
            {"skill_key": f"S{i}", "type": "choice", "level": 5, "required_level": 3}
            for i in range(5)
        ]
        batch = plan_batch_skills(items, asked={f"S{i}": 1 for i in range(5)}, graded=graded, size=4)
        assert sum(1 for b in batch if b["_item_type"] == "open") == 2

    def test_大类限额卡死时放开并标注(self):
        items = [it(f"S{i}", weight=1 - i / 100, category="同一类") for i in range(6)]
        batch = plan_batch_skills(items, size=5)
        assert len(batch) == 5
        assert "coverage_relaxed" in {b["_reason"] for b in batch}

    def test_返回条数不超过批量(self):
        items = [it(f"S{i}", weight=0.1, category=f"C{i}") for i in range(10)]
        assert len(plan_batch_skills(items, size=3)) == 3


# ── should_stop ───────────────────────────────────────────────


class TestShouldStop:
    def test_出满预估题数即停(self):
        stop, why = should_stop([], asked_total=6, batches=1, graded=[], next_batch=[it("A")], target_total=6)
        assert stop and "已出满预估题数 6" == why

    def test_题量上限兜底(self):
        stop, why = should_stop([], asked_total=MAX_QUESTIONS, batches=1, graded=[], next_batch=[it("A")])
        assert stop and str(MAX_QUESTIONS) in why

    def test_批次上限兜底(self):
        stop, why = should_stop([], asked_total=1, batches=MAX_BATCHES, graded=[], next_batch=[it("A")])
        assert stop and str(MAX_BATCHES) in why

    def test_没有可考技能时停(self):
        stop, why = should_stop([], asked_total=1, batches=1, graded=[], next_batch=[])
        assert stop and why == "没有可考的新技能"

    def test_未达任一条件时继续(self):
        stop, why = should_stop([], asked_total=2, batches=1, graded=[], next_batch=[it("A")], target_total=6)
        assert (stop, why) == (False, "")


# ── _extract：模型输出解析（含截断兜底）─────────────────────────


class TestExtract:
    def test_完整JSON(self):
        raw = '{"items":[{"skill_key":"a","prompt":"p1"},{"skill_key":"b","prompt":"p2"}]}'
        assert [x["skill_key"] for x in _extract(raw)] == ["a", "b"]

    def test_JSON前后有解释文字(self):
        raw = '好的，题目如下：\n{"items":[{"skill_key":"a","prompt":"p"}]}\n以上。'
        assert len(_extract(raw)) == 1

    def test_代码围栏(self):
        raw = '```json\n{"items":[{"skill_key":"x","prompt":"q"}]}\n```'
        assert _extract(raw)[0]["skill_key"] == "x"

    def test_被截断时保留前面已完整的题(self):
        """max_tokens 截断：整包不可解析，但前 1 题完好 —— 比整批降级成自评题划算。"""
        raw = '{"items":[{"skill_key":"a","prompt":"p1"},{"skill_key":"b","prom'
        got = _extract(raw)
        assert [x["skill_key"] for x in got] == ["a"]

    def test_截断发生在嵌套选项数组里(self):
        raw = (
            '{"items":[{"skill_key":"a","options":[{"level":1,"text":"t"}]},'
            '{"skill_key":"b","options":[{"level":2,"text":"u"'
        )
        got = _extract(raw)
        assert len(got) == 1
        assert got[0]["options"] == [{"level": 1, "text": "t"}]

    def test_逐个扫描时跳过没有技能键的对象(self):
        raw = '{"items":[{"noise":1},{"skill_key":"a","prompt":"p"},{"skill_key":"b","p'
        assert [x["skill_key"] for x in _extract(raw)] == ["a"]

    def test_只截出items之后的部分(self):
        """前置元数据里的 `{...}` 不该被当成题目。"""
        raw = '{"meta":{"skill_key":"不是题"},"items":[{"skill_key":"a","prompt":"p"},{"skill'
        assert [x["skill_key"] for x in _extract(raw)] == ["a"]

    @pytest.mark.parametrize("raw", ["", "完全不是 JSON", "{}", '{"items":[]}', None])
    def test_解析不出时抛错交给调用方降级(self, raw):
        with pytest.raises(ValueError):
            _extract(raw)

    def test_报错信息带原文前一百五十字便于排查(self):
        with pytest.raises(ValueError, match="模型未返回可解析的 JSON"):
            _extract("hello")


# ── 降级题 & 归一化 ───────────────────────────────────────────


class TestFallbackAndNormalize:
    def test_降级选择题是自评量表并标注变体(self):
        q = fallback_choice(it("配料准备", level=3, weight=0.4, available_levels=[1, 2, 3]))
        assert q["type"] == "choice"
        assert q["variant"] == "self_report", "自评题考核力弱于 SJT，报告里要能区分"
        assert [o["level"] for o in q["options"]] == [1, 2, 3]
        assert [o["value"] for o in q["options"]] == [1, 2, 3]
        assert "配料准备" in q["prompt"]

    def test_降级选择题必含岗位要求档(self):
        q = fallback_choice(it("A", level=4, available_levels=[1, 2]))
        assert 4 in [o["level"] for o in q["options"]]

    def test_没有可用档位时给满五档(self):
        q = fallback_choice({"skill_key": "A", "required_level": None})
        assert [o["level"] for o in q["options"]] == [1, 2, 3, 4, 5]

    def test_降级问答题带评分要点与字数下限(self):
        q = fallback_open(it("A"))
        assert q["type"] == "open" and q["variant"] == "generic"
        assert len(q["rubric"]) == 3 and q["min_chars"] == 80

    def test_归一化选择题限定档位区间且去重(self):
        gen = {
            "prompt": "遇到坍落度异常你会怎么做？",
            "options": [
                {"level": 0, "text": "越界低"},
                {"level": 9, "text": "越界高"},
                {"level": 3, "text": "三"},
                {"level": 3, "text": "重复三"},
                {"level": "x", "text": "坏值"},
                {"level": 2, "text": "二"},
            ],
        }
        q = _norm_choice(gen, it("A", level=3, weight=0.4))
        assert [o["level"] for o in q["options"]] == [1, 5, 3, 2], "越界档位被夹到 1–5"
        assert [o["value"] for o in q["options"]] == [1, 2, 3, 4]
        assert q["variant"] == "sjt"

    @pytest.mark.parametrize(
        "gen",
        [
            {"prompt": "短", "options": [{"level": 1, "text": "a"}] * 4},
            {"prompt": "这是一道足够长的题干文本", "options": [{"level": 1, "text": "a"}]},
            {},
        ],
    )
    def test_题干过短或选项不足时判为不可用(self, gen):
        assert _norm_choice(gen, it("A")) is None

    def test_归一化问答题最多三条要点且有兜底(self):
        q = _norm_open({"prompt": "请描述一次实际经历", "rubric": ["a", "b", "c", "d"]}, it("A"))
        assert q["rubric"] == ["a", "b", "c"]
        q2 = _norm_open({"prompt": "请描述一次实际经历"}, it("A"))
        assert len(q2["rubric"]) == 3


# ── 国标描述可用性判定 ────────────────────────────────────────


class TestRequirementHint:
    def test_太短的描述不算真描述(self):
        assert _is_real_requirement("能操作设备") is False
        assert _is_real_requirement(None) is False
        assert _is_real_requirement("") is False

    def test_采集期占位串不算真描述(self):
        assert _is_real_requirement("混凝土工 · 配料准备 · L3 · 权重 40% 的占位描述文本补足长度") is False

    def test_足够长且无占位标记才算真描述(self):
        text = "能够依据配合比通知单核对原材料品种与规格，并按批次记录进场检验结果与偏差处理方式"
        assert _is_real_requirement(text) is True

    def test_只取要求档那一档的描述(self):
        text = "能够依据配合比通知单核对原材料品种与规格，并按批次记录进场检验结果与偏差处理方式"
        item = it("A", level=3, levels=[{"level": 2, "requirement": text}, {"level": 3, "requirement": text}])
        assert _requirement_hint(item) == text[:300]
        assert _requirement_hint(it("A", level=5, levels=[{"level": 3, "requirement": text}])) is None

    def test_没有levels时返回空(self):
        assert _requirement_hint(it("A")) is None
