"""匹配度**同源**锁：两个入口必须给出同一个数字。

背景
----
`report.py` 的模块 docstring 从一开始就承诺「岗位探索列表的匹配度与诊断报告的匹配度
同源，不会出现两个数字」，但实现上分母不一样：

- `match_with_profile`（岗位探索列表 / 目标卡）：分母是**全部**技能权重
- `build_report`（诊断报告）：分母只有**实测到的**技能权重

同一份数据能差出 100% vs 30%，学员在两个页面上看到两个对不上的数。

产品决策（2026-08）
------------------
统一到「**这个岗位你整体准备好了多少**」= **全权重分母**：
`build_report.match_score` 改用全部技能权重做分母，`match_with_profile` 不动。
「本次测评覆盖了多少」由两边各自的 `coverage` 表达。

这个文件只做一件事：把**两边同源**钉死。分母口径本身在
`test_assessment_report.py::TestBuildReportMatchScore` 与
`test_match_profile.py` 里各自有正向断言；这里保证以后谁改一边都会立刻红。
所有断言都用「同一份输入喂两个函数、比结果」的形式，不写死期望值——
写死期望值只能锁住某一边，锁不住「同源」。
"""
from __future__ import annotations

import pytest

from backend.agent.assessment.report import build_report
from backend.kg.pg_store.biz_store import match_with_profile

OCC = {"id": "CN:occupation:mohrss:6-31-01-05", "name": "混凝土工", "level": "四级"}

# 四项技能、权重和为 1、要求档覆盖 L2–L5：一份「正常」的岗位技能构成
REQUIRED = [
    {"skill_key": "配料准备", "category": "生产准备", "required_level": 3, "weight": 0.4},
    {"skill_key": "搅拌操作", "category": "生产准备", "required_level": 4, "weight": 0.3},
    {"skill_key": "设备维保", "category": "设备", "required_level": 2, "weight": 0.2},
    {"skill_key": "安全防护", "category": "安全", "required_level": 5, "weight": 0.1},
]
KEYS = [r["skill_key"] for r in REQUIRED]


def both(levels: dict[str, int], required: list[dict] | None = None) -> tuple[dict, dict]:
    """同一份数据分别喂给报告侧与画像侧，返回两个结果。

    `measured` 是 `{skill: {"level": n}}`（grading.merge_measured 的形状），
    `user_levels` 是 `{skill: n}`（五维记忆推断的画像形状）——两者只是容器不同，
    承载的证据完全一样，所以匹配度必须相等。
    """
    req = required if required is not None else REQUIRED
    rep = build_report(
        occupation=OCC,
        required_items=req,
        measured={k: {"level": v} for k, v in levels.items()},
    )
    prof = match_with_profile(OCC, req, dict(levels))
    return rep, prof


# ── 核心：两边相等 ────────────────────────────────────────────

# 覆盖：一个都没测到 / 只测到一项 / 测到一半 / 全测到 / 达标 / 未达标 / 超额
PARITY_CASES = [
    pytest.param({}, id="一个都没测到"),
    pytest.param({"配料准备": 3}, id="只测到最高权重项且达标"),
    pytest.param({"安全防护": 5}, id="只测到最低权重项且达标"),
    pytest.param({"配料准备": 1}, id="只测到一项且远未达标"),
    pytest.param({"配料准备": 3, "搅拌操作": 2}, id="测到一半"),
    pytest.param({"配料准备": 5, "搅拌操作": 5}, id="测到一半且超额完成"),
    pytest.param({k: 3 for k in KEYS}, id="全测到且部分达标"),
    pytest.param({k: 5 for k in KEYS}, id="全测到且全部达标"),
    pytest.param({k: 1 for k in KEYS}, id="全测到且几乎都不达标"),
]


class TestMatchScoreParity:
    @pytest.mark.parametrize("levels", PARITY_CASES)
    def test_同一份数据两边匹配度相等(self, levels):
        rep, prof = both(levels)
        assert rep["match_score"] == pytest.approx(prof["match_score"], abs=0.05), (
            f"匹配度分家了：报告 {rep['match_score']} vs 画像 {prof['match_score']}。"
            "口径必须只有一个（全权重分母），否则学员在诊断报告和岗位列表上看到两个数。"
        )

    @pytest.mark.parametrize("levels", PARITY_CASES)
    def test_同一份数据两边覆盖率也相等(self, levels):
        """coverage 是匹配度的置信度说明，两边分家同样会让学员对不上账。"""
        rep, prof = both(levels)
        assert rep["coverage"] == pytest.approx(prof["coverage"], abs=0.05)

    @pytest.mark.parametrize("levels", PARITY_CASES)
    def test_同一份数据两边逐项达标率也相等(self, levels):
        """匹配度相等还不够——单项 ratio 也得同源，否则是两套算法凑巧总分相同。"""
        rep, prof = both(levels)
        r_by_key = {i["skill_key"]: i["ratio"] for i in rep["items"]}
        p_by_key = {i["skill_key"]: i["ratio"] for i in prof["items"]}
        assert r_by_key == p_by_key

    @pytest.mark.parametrize("levels", PARITY_CASES)
    def test_同一份数据两边达标项集合相同(self, levels):
        rep, prof = both(levels)
        assert {s["skill_key"] for s in prof["strengths"]} == {
            s["skill_key"] for s in rep["strengths"]
        }, "画像侧 strengths 是全部技能里达标的，报告侧只在实测项里选——但实测项才可能达标"


class TestParityOnOddCompositions:
    """畸形但库里真实存在的技能构成：权重不归一、缺要求档、单技能岗位。"""

    def test_权重和不为一时两边仍相等(self):
        """`covers` 边不归一、`requires` 边采集期也有 Σ≠1 的脏数据。"""
        req = [
            {"skill_key": "A", "category": "c", "required_level": 3, "weight": 2.0},
            {"skill_key": "B", "category": "c", "required_level": 3, "weight": 0.5},
            {"skill_key": "C", "category": "d", "required_level": 3, "weight": 0.25},
        ]
        rep, prof = both({"A": 3, "B": 1}, req)
        assert rep["match_score"] == pytest.approx(prof["match_score"], abs=0.05)

    def test_全部权重为零时两边都不除零(self):
        req = [{"skill_key": k, "category": "c", "required_level": 3, "weight": 0} for k in "AB"]
        rep, prof = both({"A": 3}, req)
        assert rep["match_score"] == prof["match_score"] == 0.0

    def test_没有要求档的技能两边同样处理(self):
        """required_level 为空 = 岗位没给要求档；两边都是「有证据即达标」。"""
        req = [
            {"skill_key": "A", "category": "c", "required_level": None, "weight": 0.5},
            {"skill_key": "B", "category": "c", "required_level": 0, "weight": 0.5},
        ]
        rep, prof = both({"A": 1}, req)
        assert rep["match_score"] == pytest.approx(prof["match_score"], abs=0.05)

    def test_岗位没有技能构成时两边都是零(self):
        rep, prof = both({"任意": 5}, [])
        assert rep["match_score"] == prof["match_score"] == 0.0

    def test_单技能岗位两边相等(self):
        req = [{"skill_key": "A", "category": "c", "required_level": 4, "weight": 1.0}]
        rep, prof = both({"A": 2}, req)
        assert rep["match_score"] == pytest.approx(prof["match_score"], abs=0.05)

    def test_二十项技能只测到六项时两边相等(self):
        """真实测评的形状：岗位二十多项技能，一场只考 10 题。"""
        req = [
            {"skill_key": f"S{i}", "category": f"C{i % 4}", "required_level": (i % 5) + 1,
             "weight": 0.05}
            for i in range(20)
        ]
        rep, prof = both({f"S{i}": (i % 5) + 1 for i in range(6)}, req)
        assert rep["match_score"] == pytest.approx(prof["match_score"], abs=0.05)


class TestParityRoundingSkew:
    """三分之一这类无限小数：两边都必须用**未舍入**的 ratio 加权。

    报告侧的 `items[].ratio` 是 `round(r, 3)`（给前端显示用）。求和时若图省事复用它，
    与画像侧就会差出最多 0.0005×Σw —— 平时被 `round(x, 1)` 吃掉，偶尔在 .x5 边界上
    露出来变成「两个页面差 0.1%」，是最难查的那种不一致。这条挡住这种回归。
    """

    def test_三分之一比例下两边一分不差(self):
        req = [
            {"skill_key": "A", "category": "c", "required_level": 3, "weight": 1 / 3},
            {"skill_key": "B", "category": "c", "required_level": 3, "weight": 1 / 3},
            {"skill_key": "C", "category": "d", "required_level": 3, "weight": 1 / 3},
        ]
        rep, prof = both({"A": 1, "B": 2, "C": 1}, req)
        assert rep["match_score"] == prof["match_score"]

    def test_七分之一权重下两边一分不差(self):
        req = [
            {"skill_key": k, "category": "c", "required_level": 3, "weight": 1 / 7}
            for k in "ABCDEFG"
        ]
        rep, prof = both({"A": 1, "B": 2, "C": 1, "D": 4}, req)
        assert rep["match_score"] == prof["match_score"]


class TestResidualDivergence:
    """同源了 match_score，但**达标判定**在「岗位没给要求档」时两边还不一样。

    - 报告侧 `ok = bool(lv and req and lv >= req)` —— req 为空 ⇒ **不算达标**
    - 画像侧 `ok = ratio >= 1.0`，而 `_ratio` 对「无要求档」返回 1.0 ⇒ **算达标**

    于是同一项技能在诊断报告里进 `gaps`（紧迫度 0，排在最后）、在岗位列表里进
    `strengths`。数字对上了，标签还没对上。存档而非断言修复：得先定「岗位没给
    要求档的技能算不算达标」，两边再一起改。
    """

    NO_REQ = [
        {"skill_key": "无要求档", "category": "c", "required_level": None, "weight": 0.5},
        {"skill_key": "有要求档", "category": "c", "required_level": 3, "weight": 0.5},
    ]

    def test_当前行为存档_无要求档的技能一边算优势一边算短板(self):
        rep, prof = both({"无要求档": 1, "有要求档": 3}, self.NO_REQ)
        assert rep["match_score"] == prof["match_score"] == 100.0, "分数仍然同源"
        assert [s["skill_key"] for s in rep["strengths"]] == ["有要求档"]
        assert [g["skill_key"] for g in rep["gaps"]] == ["无要求档"]
        assert {s["skill_key"] for s in prof["strengths"]} == {"无要求档", "有要求档"}
        assert prof["gaps"] == []
