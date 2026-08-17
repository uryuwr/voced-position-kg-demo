"""backend/agent/assessment/pipeline.py::plan_questions —— 整场题目一次排定。

这个文件锁的是**硬上限真的生效**（BUG-3）。

原缺陷：`MAX_QUESTIONS = 10` 只夹在 `estimate_total` 的 `total` 上，而
`plan_all_skills` 用的是**没夹过**的 `est['cover']`（20 项技能的岗位能算到 16），
`plan_questions` 又把 `total` 覆盖成 `len(plan)` —— 于是 20 项技能的岗位实际排 18 题，
学员要连答 18 道，硬上限形同虚设，进度条上的「共 N 题」也是错的。

`plan_questions` 是**真实出题路径**上唯一的规划入口（`service.stream_questions` 调它，
`est['total']` 直接推给前端做进度条、并写进 `target_total` 当收敛阈值），所以硬上限的
权威断言在这里，而不在 `estimate_total` / `plan_all_skills` 各自的中间量上——
`cover` 要不要一起夹属于修复的实现选择，题数不能超才是产品口径。
"""
from __future__ import annotations

import pytest

from backend.agent.assessment.bank import (
    MAX_PER_SKILL,
    MAX_QUESTIONS,
    MIN_COVER_QUESTIONS,
    OPEN_PER_RUN,
    RADAR_MIN_SKILLS,
    estimate_total,
)
from backend.agent.assessment.pipeline import plan_questions


def items_for(n: int, *, level: int = 5, cats: int = 4) -> list[dict]:
    """n 项等权技能（Σweight = 1），要求档统一，按 cats 个大类轮转。"""
    w = (1.0 / n) if n else 0.0
    return [
        {
            "skill_key": f"S{i}",
            "weight": w,
            "required_level": level,
            "category": f"C{i % cats}",
        }
        for i in range(n)
    ]


# 从「1 项技能」到「40 项技能」——20 与 28 是缺陷单里点名的极端情形；
# 12 是「刚好超过上限」的边界；4 以下是技能少的岗位（全库多数岗位在这一档）
SKILL_COUNTS = [1, 2, 3, 4, 5, 6, 8, 10, 11, 12, 16, 20, 28, 40]


class TestHardCap:
    @pytest.mark.parametrize("level", [2, 4, 5])
    @pytest.mark.parametrize("n", SKILL_COUNTS)
    def test_任意技能数下排出的题数都不超过硬上限(self, n, level):
        plan, est = plan_questions(items_for(n, level=level))
        assert len(plan) <= MAX_QUESTIONS, (
            f"{n} 项技能（要求档 L{level}）排出了 {len(plan)} 题，超过硬上限 {MAX_QUESTIONS}"
        )
        assert int(est["total"]) <= MAX_QUESTIONS

    @pytest.mark.parametrize("level", [2, 4, 5])
    @pytest.mark.parametrize("n", SKILL_COUNTS)
    def test_推给前端的题数就是实际排出的题数(self, n, level):
        """`est['total']` 直接进 `plan` 事件的「本次测评共 N 题」和 `target_total`：
        它比 len(plan) 大 → 进度条永远走不到 100%、收敛判定等不到；小 → 学员答完了还在出题。
        """
        plan, est = plan_questions(items_for(n, level=level))
        assert est["total"] == len(plan)

    @pytest.mark.parametrize("cats", [1, 2, 4, 8])
    def test_大类分布不影响硬上限(self, cats):
        """同大类限额 MAX_PER_CATEGORY 有「卡住就放开」的兜底，放开时也不能突破上限。"""
        plan, est = plan_questions(items_for(20, level=5, cats=cats))
        assert len(plan) <= MAX_QUESTIONS
        assert est["total"] == len(plan)

    def test_权重集中的岗位也不超上限(self):
        """一项占 80% 权重、其余长尾 —— 覆盖题只需 1 项，但下限会把题量顶起来。"""
        items = [{"skill_key": "大头", "weight": 0.8, "required_level": 5, "category": "c"}]
        items += [
            {"skill_key": f"S{i}", "weight": 0.2 / 24, "required_level": 5, "category": f"C{i % 3}"}
            for i in range(24)
        ]
        plan, est = plan_questions(items)
        assert len(plan) <= MAX_QUESTIONS
        assert est["total"] == len(plan)

    def test_权重全为零的岗位也不超上限(self):
        items = [
            {"skill_key": f"S{i}", "weight": 0, "required_level": 5, "category": f"C{i % 4}"}
            for i in range(30)
        ]
        plan, est = plan_questions(items)
        assert len(plan) <= MAX_QUESTIONS
        assert est["total"] == len(plan)


class TestCapKeepsSemantics:
    """夹上限不能靠 `plan[:MAX_QUESTIONS]`。

    plan 的顺序是「覆盖题在前、验证题在后」，直接切片会把验证题**整批**砍掉：
    要求 L4/L5 的技能只凭一道选择题定档，虚高无从发现，而这正是验证题存在的理由。

    定案（2026-08）：验证题也有下限 `min(需求量, OPEN_PER_RUN)`，名额从覆盖题让。
    此前只有覆盖题有下限、验证题纯按比例缩，方向反了——技能越多 demand 越大、
    验证题占比越小，20/28/40 项技能的岗位只剩 1 道验证题，而高标准岗位恰恰最需要它。
    """

    @pytest.mark.parametrize("n", [12, 16, 20, 28, 40])
    def test_夹上限后覆盖题与验证题两类都还在(self, n):
        plan, _ = plan_questions(items_for(n, level=5))
        cover = [p for p in plan if p["_item_type"] == "choice"]
        verify = [p for p in plan if p["_item_type"] == "open"]

        assert len(cover) + len(verify) == len(plan), "题型只有 choice / open 两种"
        assert len(cover) >= MIN_COVER_QUESTIONS, (
            f"覆盖题只剩 {len(cover)} 道，低于下限 {MIN_COVER_QUESTIONS}"
        )
        assert len(cover) >= RADAR_MIN_SKILLS, "覆盖题不足 3 项，报告雷达画不出来"
        assert len(verify) == OPEN_PER_RUN, (
            f"{n} 项技能的岗位只排了 {len(verify)} 道验证题；要求档全是 L5，"
            f"需求量是 {OPEN_PER_RUN} 道，下限该保满"
        )

    @pytest.mark.parametrize("n", [12, 16, 20, 28, 40])
    def test_验证题名额从覆盖题让而不是反过来(self, n):
        """总题数仍不超上限：验证题保住 2 道是**挤掉覆盖题**换来的，不是超发。"""
        plan, est = plan_questions(items_for(n, level=5))
        cover = sum(1 for p in plan if p["_item_type"] == "choice")
        verify = sum(1 for p in plan if p["_item_type"] == "open")
        assert verify == OPEN_PER_RUN
        assert cover == MAX_QUESTIONS - OPEN_PER_RUN
        assert len(plan) == int(est["total"]) == MAX_QUESTIONS

    @pytest.mark.parametrize("level", [2, 5])
    @pytest.mark.parametrize("n", SKILL_COUNTS)
    def test_估算的分项与实际排出的清单逐项对上(self, n, level):
        """`plan_questions` 返回的 est 描述的是**实际排出的清单**。

        `estimate_total` 那份 `cover`/`verify` 是没夹上限的**需求量**（20 项技能算到
        16+2=18），照抄给调用方就会出现「说好 18 题、实际 10 题」。
        """
        plan, est = plan_questions(items_for(n, level=level))
        cover = sum(1 for p in plan if p["_item_type"] == "choice")
        verify = sum(1 for p in plan if p["_item_type"] == "open")
        assert (int(est["cover"]), int(est["verify"])) == (cover, verify)
        assert int(est["total"]) == cover + verify == len(plan)

    @pytest.mark.parametrize("n", [12, 16, 20, 28, 40])
    def test_夹上限时两类题按需求比例缩而不是砍光一类(self, n):
        items = items_for(n, level=5)
        demand = estimate_total(items)      # 没夹上限的需求量
        plan, _ = plan_questions(items)
        cover = sum(1 for p in plan if p["_item_type"] == "choice")
        verify = sum(1 for p in plan if p["_item_type"] == "open")
        assert int(demand["cover"]) + int(demand["verify"]) > MAX_QUESTIONS, (
            "这组数据本该触发压缩，不然这条用例什么也没测到"
        )
        assert verify == int(demand["verify"]), "验证题有下限，需求量 ≤ 下限时不该被缩"
        assert MIN_COVER_QUESTIONS <= cover < int(demand["cover"]), "被压缩的是覆盖题"
        assert cover + verify == len(plan) <= MAX_QUESTIONS

    @pytest.mark.parametrize("n", [12, 20, 28])
    def test_要求档不高时不排验证题(self, n):
        plan, est = plan_questions(items_for(n, level=2))
        assert [p["_item_type"] for p in plan] == ["choice"] * len(plan)
        assert int(est["verify"]) == 0

    @pytest.mark.parametrize("n", SKILL_COUNTS)
    def test_同一技能最多两题(self, n):
        plan, _ = plan_questions(items_for(n, level=5))
        counts: dict[str, int] = {}
        for p in plan:
            counts[p["skill_key"]] = counts.get(p["skill_key"], 0) + 1
        assert max(counts.values()) <= MAX_PER_SKILL
        # 而且第二题必须是验证用的开放题，不能是两道选择题
        dup = {k for k, v in counts.items() if v == 2}
        for k in dup:
            types = sorted(p["_item_type"] for p in plan if p["skill_key"] == k)
            assert types == ["choice", "open"]

    @pytest.mark.parametrize("n", SKILL_COUNTS)
    def test_覆盖题不重复考同一个技能(self, n):
        plan, _ = plan_questions(items_for(n, level=5))
        cover = [p["skill_key"] for p in plan if p["_item_type"] == "choice"]
        assert len(cover) == len(set(cover)), "覆盖题重复技能等于白占一道题的额度"

    def test_夹上限时先砍权重最低的技能(self):
        """题额有限就该考权重高的：被砍掉的必须是长尾，不是随便截。"""
        items = [
            {"skill_key": f"S{i}", "weight": (20 - i) / 210, "required_level": 2,
             "category": f"C{i % 4}"}
            for i in range(20)
        ]
        plan, _ = plan_questions(items)
        picked = [p["skill_key"] for p in plan]
        assert "S0" in picked and "S1" in picked, "最高权重的技能必须被考到"
        assert "S19" not in picked, "最低权重的技能不该挤掉高权重的"


class TestSmallCompositions:
    """技能少的岗位：全库多数岗位是这一档，夹上限不能反过来把它们的题砍少。"""

    def test_没有技能构成时不出题(self):
        plan, est = plan_questions([])
        assert plan == []
        assert est["total"] == 0
        assert est["reason"] == "该岗位没有技能构成"

    def test_四项技能的岗位照样出四题(self):
        items = [
            {"skill_key": f"S{i}", "weight": 0.25, "required_level": 2, "category": f"C{i}"}
            for i in range(4)
        ]
        plan, est = plan_questions(items)
        assert len(plan) == 4 and est["total"] == 4

    def test_技能全在一个大类也不少考(self):
        """「计算机程序设计员」那类岗位：4 项技能几乎同属一类，限额不能让它只出 3 题。"""
        items = [
            {"skill_key": f"S{i}", "weight": 0.25, "required_level": 2, "category": "同一类"}
            for i in range(5)
        ]
        plan, _ = plan_questions(items)
        assert len(plan) >= 4

    def test_单技能岗位不会硬凑到题数下限(self):
        """MIN_QUESTIONS=4 是估算下限，但只有 1 项技能时出不了 4 题，
        `total` 必须跟着实际题数走，否则进度条卡在 1/4。"""
        plan, est = plan_questions(
            [{"skill_key": "A", "weight": 1.0, "required_level": 5, "category": "c"}]
        )
        assert est["total"] == len(plan) <= 2


class TestPlanShape:
    def test_每题都带技能与题型标注(self):
        plan, _ = plan_questions(items_for(6, level=5))
        for p in plan:
            assert p["skill_key"] and p["category"]
            assert p["_item_type"] in {"choice", "open"}
            assert p["_reason"] in {"coverage", "coverage_relaxed", "verify_high_bar"}
            assert "weight" in p and "required_level" in p

    def test_估算带上排题理由(self):
        _, est = plan_questions(items_for(20, level=5))
        assert set(est) >= {"total", "cover", "verify", "reason"}
        assert "按权重覆盖 80%" in est["reason"]

    def test_夹过上限时理由串要说明实排多少(self):
        """`reason` 跟 `total` 一起推给前端（service 的 `plan` 事件），也是排查
        「为什么只考 10 题」的唯一线索。所以两头都得留：压缩前的需求量（16 项 / 2 道）
        用来解释原因，实排的分项用来跟「本次测评共 10 题」对上账。
        """
        plan, est = plan_questions(items_for(20, level=5))
        assert est["total"] == len(plan) == MAX_QUESTIONS
        assert "需 16 项" in est["reason"], "压缩前的需求量不能丢，否则看不出被上限压过"
        assert f"受硬上限 {MAX_QUESTIONS} 题约束" in est["reason"]
        assert f"实排 {est['cover']} 覆盖题 + {est['verify']} 验证题" in est["reason"]

    def test_没夹过上限时理由串不加尾巴(self):
        _, est = plan_questions(items_for(4, level=2))
        assert "受硬上限" not in est["reason"]

    def test_不改原始条目(self):
        items = items_for(6, level=5)
        plan_questions(items)
        assert all("_item_type" not in i for i in items)
