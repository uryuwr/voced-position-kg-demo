"""backend/agent/assessment/report.py —— 综合能力报告（纯函数，不碰库）。

锁五件事：
- `_build_radar` 的「技能轴 → 大类轴」回落规则
- `match_score` 按全部**可评分**技能权重做分母（口径 =「这个岗位你整体准备好了多少」），
  与 `match_with_profile` 同源；两边同源本身由 tests/unit/test_match_score_parity.py 锁
- **没有基准就不编分数**：要求档缺失/越界的技能 `scorable=false`，分子分母都不计，
  一项可评分技能都没有时 `match_score` 是 **None** 而不是 0.0（`TestNoBaselineScoring`）
- 1–5 之外的脏档位、非数值权重**不能打死整份报告**（`TestDirtyLevelGuard` /
  `TestDirtyWeightInReport`）
- 短板排序按 `urgency = 权重 × 差距档数`
"""
from __future__ import annotations

import pytest

from backend.agent.assessment.report import (
    RADAR_MAX_AXES,
    RADAR_MIN_AXES,
    _build_radar,
    _ratio,
    build_report,
)
from backend.kg.pg_store.config import PARTIAL_BASELINE_PCT
from tests._stable_data import load_fixture


def _row(skill, *, cat, measured, required, weight=0.25):
    return {
        "skill_key": skill,
        "category": cat,
        "measured_level": measured,
        "required_level": required,
        "weight": weight,
    }


# ── _ratio ────────────────────────────────────────────────────


class TestRatio:
    def test_未测到时为零(self):
        assert _ratio(0, 3) == 0.0

    def test_封顶一(self):
        assert _ratio(5, 2) == 1.0

    def test_按比例(self):
        assert _ratio(2, 4) == 0.5

    def test_无要求档时有实测即满分(self):
        """`_ratio` 的既有语义没动：required=0 ⇒ 有实测即 1.0。

        但它已经**不再能把总分抬高**——无要求档的项 `scorable=false`，分子分母都不计
        （见 `TestNoBaselineScoring`）。1.0 只留在 `items[].ratio` 上给前端展示，
        所以这里仍然断言 1.0：改掉它反而会让「按项展示」少一档信息。
        """
        assert _ratio(3, 0) == 1.0
        assert _ratio(0, 0) == 0.0


# ── _build_radar ──────────────────────────────────────────────


class TestBuildRadar:
    def test_技能够三项时轴是技能(self):
        rows = [
            _row("A", cat="生产准备", measured=3, required=3, weight=0.5),
            _row("B", cat="生产准备", measured=2, required=4, weight=0.3),
            _row("C", cat="设备", measured=2, required=2, weight=0.2),
        ]
        r = _build_radar(rows)
        assert r["axis_type"] == "skill"
        assert r["categories"] == ["A", "B", "C"]          # 权重降序
        assert r["series"][0]["key"] == "user"
        assert r["series"][1]["key"] == "required"
        assert r["series"][0]["scores"] == [60, 40, 40]    # base_score(L3/L2/L2)
        assert r["series"][1]["scores"] == [60, 80, 40]
        # 顶层 scores 与 user 系列同值（老前端读的是顶层）
        assert r["scores"] == r["series"][0]["scores"]

    def test_技能不足三项时回落到大类轴(self):
        # 两项技能同属一个大类 —— 这正是「计算机程序设计员」那类岗位的形状
        rows = [
            _row("A", cat="生产准备", measured=3, required=3, weight=0.5),
            _row("B", cat="生产准备", measured=2, required=4, weight=0.3),
        ]
        r = _build_radar(rows)
        assert r["axis_type"] == "category"
        assert r["categories"] == ["生产准备"]
        # 同大类内取均值：user=(60+40)/2=50，required=(60+80)/2=70
        assert r["series"][0]["scores"] == [50]
        assert r["series"][1]["scores"] == [70]

    def test_回落时空大类记为未分类(self):
        rows = [
            _row("A", cat=None, measured=3, required=3),
            _row("B", cat="", measured=1, required=1),
        ]
        r = _build_radar(rows)
        assert r["categories"] == ["未分类"]

    def test_回落保持首次出现顺序(self):
        rows = [
            _row("A", cat="乙", measured=3, required=3),
            _row("B", cat="甲", measured=3, required=3),
        ]
        assert _build_radar(rows)["categories"] == ["乙", "甲"]

    def test_轴数封顶六根(self):
        rows = [
            _row(f"S{i}", cat=f"C{i}", measured=3, required=3, weight=1 - i / 100)
            for i in range(10)
        ]
        r = _build_radar(rows)
        assert len(r["categories"]) == RADAR_MAX_AXES
        assert r["categories"] == ["S0", "S1", "S2", "S3", "S4", "S5"]

    def test_一项没测到时轴为空但结构完整(self):
        r = _build_radar([])
        assert r["axis_type"] == "category"
        assert r["categories"] == []
        assert [s["key"] for s in r["series"]] == ["user", "required"]
        assert r["scores"] == []

    def test_档位缺失记零分而不是报错(self):
        rows = [
            _row("A", cat="c", measured=None, required=None),
            _row("B", cat="c", measured=None, required=3),
            _row("C", cat="c", measured=2, required=None),
        ]
        r = _build_radar(rows)
        assert r["series"][0]["scores"] == [0, 0, 40]
        assert r["series"][1]["scores"] == [0, 60, 0]

    def test_三是回落阈值(self):
        assert RADAR_MIN_AXES == 3
        assert RADAR_MAX_AXES == 6


# ── 脏档位守卫（BUG-1）────────────────────────────────────────
#
# 「一条脏数据不能打死一整页」——这个形状项目栽过四次，这里覆盖要密。
#
# `base_score()` 对 1–5 之外的档位 raise KeyError，而报告里有 5 处直接调它。
# 越界档位有三条**真实**来路，全都没夹到 1–5：
#   1. required_level ← kg_edge 指向的 skill_level 节点 `attrs.level`。读侧
#      `config.attrs_level_int` 的正则 `^[0-9]+$` 只挡非数字，`9` 原样穿过。
#   2. measured_level ← `grade_choice` 取选项自带的 level（`int(picked['level'] or 0) or None`，
#      没夹）；降级自评题 `fallback_choice` 的选项直接来自 `item['available_levels']`，
#      也就是库里的 `attrs.level`。
#   3. measured_level ← `merge_measured` 里简历画像的 `int(lv)`，模型给几就是几。
#
# 口径：越界值按「**无效档位**」处理（雷达记 0 分），与 `attrs_level_int`
# 「脏值取 NULL 而不是抛错」一致；不是夹到 1/5——夹取会让脏数据把分数抬高或
# 凭空造出「岗位要求 L5」。

# 0/None 走的是既有的 falsy 分支；6/7/9/99 是数值越界；负数是 falsy 检查漏掉的那类
DIRTY_LEVELS = [0, None, 6, 7, 9, 99, -1, -3]
OUT_OF_RANGE_LEVELS = [6, 7, 9, 99, -1, -3]


class TestDirtyRadarLevel:
    """`_build_radar` 两条分支（技能轴 / 大类轴）都要挡住越界档位。"""

    @pytest.mark.parametrize("bad", OUT_OF_RANGE_LEVELS)
    def test_技能轴上越界档记零分而不是抛错(self, bad):
        rows = [
            _row("脏", cat="c", measured=bad, required=bad),
            _row("B", cat="c", measured=3, required=3),
            _row("C", cat="d", measured=2, required=2),
        ]
        r = _build_radar(rows)
        assert r["axis_type"] == "skill"
        assert r["series"][0]["scores"][0] == 0, "越界实测档按无效处理"
        assert r["series"][1]["scores"][0] == 0, "越界要求档按无效处理"
        # 干净的那两根轴不受影响
        assert r["series"][0]["scores"][1:] == [60, 40]
        assert r["series"][1]["scores"][1:] == [60, 40]

    @pytest.mark.parametrize("bad", OUT_OF_RANGE_LEVELS)
    def test_大类轴上越界档记零分而不是抛错(self, bad):
        """技能不足 3 项时走大类聚合分支——那里也有 4 处 base_score。"""
        rows = [
            _row("脏", cat="甲", measured=bad, required=bad),
            _row("B", cat="乙", measured=3, required=3),
        ]
        r = _build_radar(rows)
        assert r["axis_type"] == "category"
        assert r["series"][0]["scores"] == [0, 60]
        assert r["series"][1]["scores"] == [0, 60]

    @pytest.mark.parametrize("bad", DIRTY_LEVELS)
    def test_任何脏档位下雷达分值都在零到一百之间(self, bad):
        rows = [
            _row("A", cat="c", measured=bad, required=3),
            _row("B", cat="c", measured=3, required=bad),
            _row("C", cat="d", measured=bad, required=bad),
        ]
        r = _build_radar(rows)
        for ser in r["series"]:
            assert all(isinstance(s, int) and 0 <= s <= 100 for s in ser["scores"]), ser
        assert r["scores"] == r["series"][0]["scores"]

    def test_守卫不能改掉正常数据的分值(self):
        """加了守卫之后这条必须还绿——1–5 的档位分值一分不许动。"""
        rows = [_row(f"S{lv}", cat="c", measured=lv, required=lv) for lv in (1, 2, 3, 4, 5)]
        r = _build_radar(rows)
        assert r["series"][0]["scores"] == [20, 40, 60, 80, 100]
        assert r["series"][1]["scores"] == [20, 40, 60, 80, 100]


class TestDirtyLevelGuard:
    """整份报告在脏档位下必须仍然生成——测评做完了不能因为一条脏边看不到结果。

    「仍能生成」原本用 `isinstance(rep["match_score"], float)` 当代理指标（能算出数
    就说明没在半路抛异常）。**要求档**全脏的那几条现在是 `match_score=None`
    （定案：没有基准就不编分数），代理指标失效，但用例的意图没变，所以改成直接断言
    意图本身：不抛异常、报告结构完整、并且**明确说出是哪一种算不出来**。
    只把断言放宽成 `(float, type(None))` 会让用例失去区分力 —— 那样连
    「报告里根本没有 match_score 这个键」都能混过去。
    """

    THREE = [
        {"skill_key": "A", "category": "生产准备", "required_level": 3, "weight": 0.5},
        {"skill_key": "B", "category": "生产准备", "required_level": 3, "weight": 0.3},
        {"skill_key": "C", "category": "设备", "required_level": 3, "weight": 0.2},
    ]

    @staticmethod
    def _assert_report_intact(rep, *, n_items: int) -> None:
        """「报告仍然生成」= 结构完整 + 摘要是给人看的话 + 分数字段口径自洽。"""
        assert len(rep["items"]) == n_items
        assert isinstance(rep["summary"], str) and rep["summary"]
        assert "None" not in rep["summary"], "算不出分时不能把 None 拼进文案"
        assert rep["score_status"] in {
            "ok", "no_skills", "no_baseline", "no_weight", "no_evidence"
        }
        score = rep["match_score"]
        if rep["score_status"] == "no_baseline":
            assert score is None, "一项可评分技能都没有 ⇒ 分数必须是 None，不是 0%"
        else:
            assert isinstance(score, float) and 0.0 <= score <= 100.0
        assert all(0.0 <= r["ratio"] <= 1.0 for r in rep["items"]), rep["items"]

    @pytest.mark.parametrize("bad", DIRTY_LEVELS)
    def test_实测档越界时报告仍能生成(self, bad):
        """要求档是干净的 ⇒ 仍然算得出分，只是脏的那项拿 0 分。"""
        rep = build_report(
            occupation=None,
            required_items=self.THREE,
            measured={"A": {"level": bad}, "B": {"level": 2}, "C": {"level": 2}},
        )
        self._assert_report_intact(rep, n_items=3)
        assert isinstance(rep["match_score"], float), "要求档没坏，分数不该消失"
        assert rep["score_status"] == "ok"
        assert all(r["scorable"] for r in rep["items"])

    @pytest.mark.parametrize("bad", DIRTY_LEVELS)
    def test_要求档越界时报告仍能生成(self, bad):
        """3 项技能的要求档全脏 ⇒ 一项都评不了分 ⇒ 报告照出，但分数是 None。"""
        required = [{**it, "required_level": bad} for it in self.THREE]
        rep = build_report(
            occupation=None,
            required_items=required,
            measured={"A": {"level": 3}, "B": {"level": 2}, "C": {"level": 2}},
        )
        self._assert_report_intact(rep, n_items=3)
        assert rep["score_status"] == "no_baseline"
        assert [r["scorable"] for r in rep["items"]] == [False] * 3
        assert len(rep["no_baseline"]) == 3
        # 实测档是干净的，明细里照样展示；只是没有标尺可比
        assert [r["measured_level"] for r in rep["items"]] == [3, 2, 2]

    @pytest.mark.parametrize("bad", DIRTY_LEVELS)
    def test_两侧同时越界时报告仍能生成(self, bad):
        required = [{**it, "required_level": bad} for it in self.THREE]
        rep = build_report(
            occupation=None,
            required_items=required,
            measured={k: {"level": bad} for k in ("A", "B", "C")},
        )
        self._assert_report_intact(rep, n_items=3)
        assert rep["score_status"] == "no_baseline"
        assert rep["no_baseline_weight"] == 100.0, "整个岗位都没配要求档"
        assert rep["strengths"] == [] and rep["gaps"] == []

    @pytest.mark.parametrize("bad", DIRTY_LEVELS)
    def test_只有一项技能且档位越界时也不炸(self, bad):
        """1 项技能 → 雷达走大类回落分支，是另一条代码路径。"""
        rep = build_report(
            occupation=None,
            required_items=[{"skill_key": "A", "category": "c", "required_level": bad, "weight": 1.0}],
            measured={"A": {"level": bad}},
        )
        self._assert_report_intact(rep, n_items=1)
        assert rep["score_status"] == "no_baseline"
        assert rep["radar"]["axis_type"] == "category"

    @pytest.mark.parametrize("bad", DIRTY_LEVELS)
    def test_越界档不会让匹配度跑出零到一百(self, bad):
        """负数档位不夹的话 `_ratio` 会算出负比例，页面上会出现「匹配度 -33.3%」。"""
        rep = build_report(
            occupation=None,
            required_items=self.THREE,
            measured={"A": {"level": bad}, "B": {"level": bad}, "C": {"level": 3}},
        )
        assert 0.0 <= rep["match_score"] <= 100.0
        assert all(0.0 <= r["ratio"] <= 1.0 for r in rep["items"]), rep["items"]

    @pytest.mark.parametrize("bad", OUT_OF_RANGE_LEVELS)
    def test_越界档位没有档位名(self, bad):
        """label_map 里查不到就该是 None，不能编一个名字出来。"""
        rep = build_report(
            occupation=None,
            required_items=[{"skill_key": "A", "category": "c", "required_level": bad, "weight": 1.0}],
            measured={"A": {"level": bad}},
        )
        assert rep["items"][0]["required_label"] is None
        assert rep["items"][0]["measured_label"] is None

    @pytest.mark.parametrize("bad", OUT_OF_RANGE_LEVELS)
    def test_短板紧迫度不会因为越界档变成负数(self, bad):
        rep = build_report(
            occupation=None,
            required_items=[{"skill_key": "A", "category": "c", "required_level": bad, "weight": 0.5}],
            measured={"A": {"level": 3}},
        )
        assert rep["items"][0]["urgency"] >= 0.0

    def test_档位在一到五之内时结论一分不变(self):
        """守卫的回归基线：正常数据的雷达/匹配度不许被守卫改掉。"""
        rep = build_report(
            occupation=None,
            required_items=self.THREE,
            measured={"A": {"level": 5}, "B": {"level": 2}, "C": {"level": 2}},
        )
        assert rep["radar"]["series"][0]["scores"] == [100, 40, 40]
        assert rep["radar"]["series"][1]["scores"] == [60, 60, 60]
        # 0.5*1.0 + 0.3*(2/3) + 0.2*(2/3) = 0.8333
        assert rep["match_score"] == pytest.approx(83.3, abs=0.05)


# ── build_report ──────────────────────────────────────────────

REQUIRED = [
    {"skill_key": "配料准备", "category": "生产准备", "required_level": 3, "weight": 0.4},
    {"skill_key": "搅拌操作", "category": "生产准备", "required_level": 4, "weight": 0.3},
    {"skill_key": "设备维保", "category": "设备", "required_level": 2, "weight": 0.2},
    {"skill_key": "安全防护", "category": "安全", "required_level": 5, "weight": 0.1},
]


class TestBuildReportMatchScore:
    """口径（2026-08 产品决策）：分母是全部**可评分**技能权重。

    `match_score` 回答的是「这个岗位你整体准备好了多少」，没测到的项按 0 计入；
    「本次测评覆盖了多少」由 `coverage` 单独表达。这样它与岗位探索列表的
    `match_with_profile` 才是同一个数字（见 test_match_score_parity.py）。

    本类里 `REQUIRED` 的要求档全都在 1–5 内，所以「可评分权重」= 全部权重，
    下面的期望值与口径改动前一致。缺要求档时分母怎么缩，见 `TestNoBaselineScoring`。
    """

    def test_匹配度按全部技能权重做分母(self):
        rep = build_report(
            occupation={"id": "o1", "name": "混凝土工"},
            required_items=REQUIRED,
            measured={"配料准备": {"level": 3}, "搅拌操作": {"level": 2}},
        )
        # 分子 1.0*0.4 + 0.5*0.3 = 0.55；分母是全部权重 1.0
        assert rep["match_score"] == pytest.approx(55.0, abs=0.05)
        assert rep["coverage"] == pytest.approx(70.0, abs=0.05)

    def test_未测到的技能会稀释匹配度(self):
        """口径改动的方向性断言：同一份数据，全权重分母必然 ≤ 实测分母。"""
        measured = {"配料准备": {"level": 3}, "搅拌操作": {"level": 2}}
        rep = build_report(occupation=None, required_items=REQUIRED, measured=measured)
        tested_only = 100 * 0.55 / 0.7          # 旧口径 78.6
        assert rep["match_score"] < tested_only

    def test_若保留旧口径则单独一个字段而不是改回match_score(self):
        """详情页想看「测过的部分考得怎样」可以另加字段，但 match_score 只有一个口径。"""
        rep = build_report(
            occupation=None,
            required_items=REQUIRED,
            measured={"配料准备": {"level": 3}, "搅拌操作": {"level": 2}},
        )
        if "tested_match_score" not in rep:
            pytest.skip("修复未新增 tested_match_score（该字段是可选的）")
        assert rep["tested_match_score"] == pytest.approx(78.6, abs=0.05)
        assert rep["tested_match_score"] != rep["match_score"]

    def test_覆盖率是实测权重占总权重(self):
        rep = build_report(
            occupation=None,
            required_items=REQUIRED,
            measured={"设备维保": {"level": 2}},
        )
        assert rep["coverage"] == pytest.approx(20.0)
        # 唯一测到的项达标，但它只占岗位权重的 20% → 整体准备度 20%
        assert rep["match_score"] == pytest.approx(20.0)

    def test_全部技能都测到时分母就是实测权重(self):
        """全覆盖是两个口径的交点，此时新旧口径必须给出同一个数。"""
        rep = build_report(
            occupation=None,
            required_items=REQUIRED,
            measured={r["skill_key"]: {"level": 5} for r in REQUIRED},
        )
        assert rep["coverage"] == pytest.approx(100.0)
        assert rep["match_score"] == pytest.approx(100.0)

    def test_全部没测到时匹配度为零且覆盖率为零(self):
        rep = build_report(occupation=None, required_items=REQUIRED, measured={})
        assert rep["match_score"] == 0.0
        assert rep["coverage"] == 0.0
        assert rep["score_status"] == "no_evidence", "有基准有权重、只是这次没考"
        assert rep["counts"] == {
            "skill_total": 4, "tested": 0, "untested": 4, "strength": 0, "gap": 0
        }

    def test_岗位没有技能构成时不除零(self):
        rep = build_report(occupation=None, required_items=[], measured={})
        assert rep["match_score"] == 0.0
        assert rep["coverage"] == 0.0
        assert rep["items"] == []
        # 当前行为存档：`no_skills` 返回 0.0，而 `no_baseline` 返回 None。
        # 两者都是「算不出分」，取值却不一致 —— 前端得写两套判断（`score === null`
        # 之外还要看 status），漏一处就又在页面上显示刺眼的 0%。
        # 未定案：要不要让 no_skills 也回 None。这里只把现状钉住。
        assert rep["score_status"] == "no_skills"

    def test_实测档为零视为没测到(self):
        """merge_measured 不会产出 level=0，但画像/脏数据可能带进来。"""
        rep = build_report(
            occupation=None,
            required_items=REQUIRED,
            measured={"配料准备": {"level": 0}},
        )
        item = rep["items"][0]
        assert item["tested"] is True        # measured 里有这个 key
        assert item["measured_level"] is None
        assert item["ratio"] == 0.0
        assert rep["match_score"] == 0.0

    def test_超额完成不会让匹配度超过一百(self):
        rep = build_report(
            occupation=None,
            required_items=[{"skill_key": "A", "category": "c", "required_level": 1, "weight": 1.0}],
            measured={"A": {"level": 5}},
        )
        assert rep["match_score"] == 100.0
        assert rep["items"][0]["ratio"] == 1.0


# ── 没有基准就不编分数（定案 2026-08）──────────────────────────
#
# 库内 608 个有技能构成的岗位中 490 个（80%）要求档全缺：8919 个老国标 skill_level
# 节点只写了 `attrs.level_code`（"L1"），没有产品档 `attrs.level`，而读侧只认后者。
# 旧行为把「没有要求档」当成「无要求即满足」——`_ratio(lv, 0)` 返回 1.0 并计入分子，
# 于是一个只测出 L1 的学员在「镗工」（3 项技能要求档全缺）上拿到 60%，彻底的错答案。
#
# 定案：无基准的项 `scorable=false`，**分子分母都不计**，不进 strengths / gaps，
# 改进 `no_baseline[]`；一项可评分技能都没有时 `match_score` 是 None（不是 0.0，
# 0% 会被读成「完全不匹配」），并由 `score_status` 说明是哪一种算不出来。

NO_BASELINE_THREE = [
    {"skill_key": "镗孔加工", "category": "操作与加工", "required_level": None, "weight": 0.5},
    {"skill_key": "刀具刃磨", "category": "操作与加工", "required_level": None, "weight": 0.3},
    {"skill_key": "设备点检", "category": "设备", "required_level": None, "weight": 0.2},
]

# 一半权重有基准、一半没有：库里最常见的中间形态（partial_baseline 那类）
HALF_BASELINE = [
    {"skill_key": "有基准", "category": "c", "required_level": 5, "weight": 0.4},
    {"skill_key": "无基准", "category": "c", "required_level": None, "weight": 0.6},
]


class TestNoBaselineScoring:
    """无基准项被排除出分子分母；全无基准时不给数字而给原因。"""

    def test_全无基准时给None并说明原因(self):
        rep = build_report(
            occupation=None,
            required_items=NO_BASELINE_THREE,
            measured={"镗孔加工": {"level": 1}, "刀具刃磨": {"level": 1}, "设备点检": {"level": 1}},
        )
        assert rep["match_score"] is None, "旧行为在这里给 60%——只测出 L1 的学员的错答案"
        assert rep["score_status"] == "no_baseline"
        assert rep["tested_match_score"] is None, "另一个口径同样没有基准可比"
        assert rep["no_baseline_weight"] == 100.0
        assert rep["coverage"] == 0.0, "可评分权重为 0 ⇒ 覆盖率无从算起"
        assert len(rep["no_baseline"]) == 3

    @pytest.mark.parametrize("bad", DIRTY_LEVELS)
    def test_越界要求档与缺要求档同等对待(self, bad):
        """0 / None 是「没配」，6 / 9 / -1 是「配错了」；都没有可信基准，处理必须一样。"""
        rep = build_report(
            occupation=None,
            required_items=[{**it, "required_level": bad} for it in NO_BASELINE_THREE],
            measured={"镗孔加工": {"level": 1}},
        )
        assert rep["match_score"] is None
        assert rep["score_status"] == "no_baseline"

    def test_无基准项不进优势也不进短板(self):
        """没有基准既谈不上达标、也谈不上差距 —— 两边都不该收它。"""
        rep = build_report(
            occupation=None,
            required_items=HALF_BASELINE,
            measured={"有基准": {"level": 5}, "无基准": {"level": 5}},
        )
        assert [s["skill_key"] for s in rep["strengths"]] == ["有基准"]
        assert rep["gaps"] == []
        assert [n["skill_key"] for n in rep["no_baseline"]] == ["无基准"]
        assert rep["items"][1]["ok"] is False, "无要求档时 ok 恒为 false"

    def test_无基准项不计入分子(self):
        """无基准项 ratio 是 1.0（展示语义），计入分子就会把总分抬高。

        旧行为：(0.2×0.4 + 1.0×0.6) / 1.0 = 68%。新行为只看有基准的那 0.4。
        """
        rep = build_report(
            occupation=None,
            required_items=HALF_BASELINE,
            measured={"有基准": {"level": 1}, "无基准": {"level": 5}},
        )
        assert rep["items"][1]["ratio"] == 1.0, "_ratio 的既有语义没动"
        assert rep["items"][1]["scorable"] is False
        assert rep["match_score"] == pytest.approx(20.0, abs=0.05)   # 1/5 × 0.4 / 0.4

    def test_无基准项不计入分母(self):
        """反方向：无基准项也不该把总分压低 —— 它进分母就等于按 0 分算。

        旧行为：1.0×0.4 / 1.0 = 40%（无基准且未测 ⇒ ratio 0 计入分母）。
        """
        rep = build_report(
            occupation=None,
            required_items=HALF_BASELINE,
            measured={"有基准": {"level": 5}},
        )
        assert rep["match_score"] == 100.0
        assert rep["coverage"] == 100.0, "可评分的那一项考到了 ⇒ 该评的部分全覆盖"

    def test_部分缺基准时报出基准缺口占多少权重(self):
        """`coverage` 缺的是证据，`no_baseline_weight` 缺的是基准，两个不同的洞。"""
        rep = build_report(
            occupation=None,
            required_items=HALF_BASELINE,
            measured={"有基准": {"level": 5}},
        )
        assert rep["no_baseline_weight"] == 60.0    # 0.6 / (0.4 + 0.6)
        assert rep["coverage"] == 100.0             # 该评的那一项全考到了

    def test_权重全为零时基准缺口退回按项数算(self):
        """否则这个字段在最需要它的岗位（老数据权重也常是 0）上恒为 0，等于没有。"""
        rep = build_report(
            occupation=None,
            required_items=[
                {"skill_key": "有基准", "category": "c", "required_level": 3, "weight": 0},
                {"skill_key": "无基准", "category": "c", "required_level": None, "weight": 0},
                {"skill_key": "无基准2", "category": "c", "required_level": None, "weight": 0},
            ],
            measured={"有基准": {"level": 3}},
        )
        assert rep["no_baseline_weight"] == pytest.approx(66.7, abs=0.05)   # 2/3 项
        assert rep["score_status"] == "no_weight", "有基准但权重全 0 ⇒ 算不出加权分"

    def test_计数里优势加短板可能小于实测数(self):
        """`tested + untested == skill_total` 仍恒成立，但 strength + gap 只数可评分项。"""
        rep = build_report(
            occupation=None,
            required_items=HALF_BASELINE,
            measured={"有基准": {"level": 5}, "无基准": {"level": 5}},
        )
        c = rep["counts"]
        assert c["tested"] + c["untested"] == c["skill_total"] == 2
        assert c["strength"] + c["gap"] == 1 < c["tested"]

    def test_摘要说清是哪一种算不出来而不是拼出None(self):
        rep = build_report(
            occupation=None, required_items=NO_BASELINE_THREE, measured={"镗孔加工": {"level": 1}}
        )
        s = rep["summary"]
        assert "无法计算匹配度" in s and "未配置能力要求档" in s
        assert "None" not in s, "「匹配度 None%」曾经真的写进过给学员看的文案"
        assert "%" not in s, "算不出分就一个百分号都不该出现"

    def test_部分缺基准时摘要仍给分数并提示缺口(self):
        rep = build_report(
            occupation=None, required_items=HALF_BASELINE, measured={"有基准": {"level": 5}}
        )
        assert "综合能力匹配度 100.0%" in rep["summary"]
        assert "另有 1 项因未配要求档无法评分" in rep["summary"]

    def test_当前行为存档_雷达仍把无基准项画上去(self):
        """`_build_radar` 吃的是 `tested_rows`（没过 scorable），要求轴记 0 分。

        画像侧（`match_with_profile`）的雷达只用可评分项，两边的雷达因此不同源：
        同一个岗位，报告页有 3 根「要求 0 分」的轴，列表页一根都没有。
        雷达形状本来就不是同源契约（一边双系列一边单系列），所以只存档、不判对错。
        """
        rep = build_report(
            occupation=None,
            required_items=NO_BASELINE_THREE,
            measured={k["skill_key"]: {"level": 3} for k in NO_BASELINE_THREE},
        )
        assert rep["radar"]["axis_type"] == "skill"
        assert len(rep["radar"]["categories"]) == 3
        assert rep["radar"]["series"][1]["scores"] == [0, 0, 0], "要求档全缺 ⇒ 要求轴全 0"


# ── 配置不全时服务端主动降级（定案 2026-08）───────────────────
#
# 「全缺」与「全配齐」之间的中间态才是最会误导人的：82% 的权重没配要求档、
# 剩下 18% 全达标 ⇒ 分数 50% + 状态 `ok`，学员读成「我匹配一半」。
# 定案：缺口**超过** `config.PARTIAL_BASELINE_PCT`（30%）时分数照给
# （那 18% 是真实依据，丢掉更亏），但状态降级成 `partial_baseline`，
# 摘要里也写明仅供参考。阈值口径本身锁在 test_pg_guards.py::TestPartialBaselineThreshold。


def _two_skill_report(nb_weight: float):
    """一项有基准（权重 1-nb，要求 L5 且已达标）+ 一项无基准（权重 nb），只测到前者。

    这样分数恒为 100.0，`score_status` 的变化只来自基准缺口，不掺别的因素。
    """
    return build_report(
        occupation=None,
        required_items=[
            {"skill_key": "有基准", "category": "c", "required_level": 5,
             "weight": round(1.0 - nb_weight, 4)},
            {"skill_key": "无基准", "category": "c", "required_level": None, "weight": nb_weight},
        ],
        measured={"有基准": {"level": 5}},
    )


class TestPartialBaselineDegrade:
    @pytest.mark.parametrize("nb", [0.31, 0.5, 0.6, 0.9])
    def test_缺口过阈值时状态降级但分数照给(self, nb):
        rep = _two_skill_report(nb)
        assert rep["score_status"] == "partial_baseline"
        assert rep["match_score"] == 100.0, "已配置的那部分是真实依据，不该丢"
        assert rep["no_baseline_weight"] > PARTIAL_BASELINE_PCT

    @pytest.mark.parametrize("nb", [0.0, 0.1, 0.2, 0.3])
    def test_缺口在阈值内时不降级(self, nb):
        rep = _two_skill_report(nb)
        assert rep["score_status"] == "ok"
        assert rep["no_baseline_weight"] <= PARTIAL_BASELINE_PCT

    def test_正好三成不降级(self):
        """严格大于。响应里写 `no_baseline_weight=30.0` 却降级，前端无法解释。"""
        rep = _two_skill_report(PARTIAL_BASELINE_PCT / 100)
        assert rep["no_baseline_weight"] == PARTIAL_BASELINE_PCT
        assert rep["score_status"] == "ok"

    def test_摘要里写明仅供参考(self):
        """前端可能只展示 summary；光把状态放在字段里，读到的人还是会当结论。"""
        s = _two_skill_report(0.6)["summary"]
        assert "分数仅供参考" in s
        assert "占岗位权重 60.0%" in s

    def test_不降级时摘要不加那句提示(self):
        assert "仅供参考" not in _two_skill_report(0.2)["summary"]

    def test_全无基准仍是无基准而不是配置不全(self):
        """缺口 100% 是本档的极端情形，但那时连分数都没有，说「仅供参考」没意义。"""
        rep = build_report(
            occupation=None, required_items=NO_BASELINE_THREE, measured={"镗孔加工": {"level": 3}}
        )
        assert rep["no_baseline_weight"] == 100.0
        assert rep["score_status"] == "no_baseline"
        assert rep["match_score"] is None

    def test_没有证据时不被降级盖掉(self):
        """`no_evidence` 比基准缺口更该先说：连一项都没考，谈缺基准是次要的。"""
        rep = build_report(
            occupation=None, required_items=HALF_BASELINE, measured={}
        )
        assert rep["score_status"] == "no_evidence"
        assert rep["no_baseline_weight"] == 60.0, "缺口照报，只是不改状态"

    def test_权重全为零时按项数算的缺口也能触发降级(self):
        rep = build_report(
            occupation=None,
            required_items=[
                {"skill_key": "有基准", "category": "c", "required_level": 3, "weight": 0},
                {"skill_key": "无基准", "category": "c", "required_level": None, "weight": 0},
            ],
            measured={"有基准": {"level": 3}},
        )
        assert rep["no_baseline_weight"] == 50.0
        # 但状态是 no_weight（算不出加权分），比「仅供参考」更严重，不被降级盖掉
        assert rep["score_status"] == "no_weight"


class TestNoBaselineOnRealGraphShapes:
    """用 tests/fixtures/graph_subjects.json 里冻结的岗位形态跑一遍。

    这个 PG 是共享的、并行任务随时在改图数据，连库断言会飘（同一岗位上午
    required_level=5、下午 None）。所以受试数据冻结在 fixture 里，见 tests/_stable_data.py。

    期望值一律**从 fixture 自己算**，不写死百分比：重新冻结（换受试岗位）后这些用例
    仍然有效，否则每次 `scripts/freeze_test_fixture.py` 都要连带改一批断言，
    改的人只会把断言往松的方向改。
    """

    TAGS = ["full_baseline", "no_baseline", "partial_baseline",
            "weight_unnormalized", "many_skills"]

    @staticmethod
    def _report(tag: str, *, level: int = 3):
        sub = load_fixture(tag)
        items = sub["required_items"]
        return sub, build_report(
            occupation=sub["occupation"],
            required_items=items,
            measured={i["skill_key"]: {"level": level} for i in items},
        )

    @staticmethod
    def _nb_pct(items: list[dict]) -> float:
        """独立算一遍「缺基准权重占比」，当 backend 那份的对照。"""
        total = sum(float(i["weight"] or 0) for i in items)
        nb = sum(float(i["weight"] or 0) for i in items if not i["required_level"])
        return round(100 * nb / total, 1) if total else round(
            100 * sum(1 for i in items if not i["required_level"]) / len(items), 1
        )

    def test_要求档齐全的岗位正常算分(self):
        sub, rep = self._report("full_baseline", level=5)
        assert rep["score_status"] == "ok"
        assert rep["match_score"] == 100.0
        assert rep["no_baseline_weight"] == 0.0
        assert rep["no_baseline"] == []
        assert len(rep["items"]) == len(sub["required_items"]) == 6

    def test_要求档全缺的岗位不给分(self):
        """这是库里 80%（491/608）岗位的形状：老 MOHRSS 节点只有 level_code。"""
        sub, rep = self._report("no_baseline")
        items = sub["required_items"]
        assert all(not i["required_level"] for i in items), "fixture 得真的是「全缺」"
        assert rep["match_score"] is None
        assert rep["score_status"] == "no_baseline"
        assert rep["no_baseline_weight"] == 100.0
        assert len(rep["no_baseline"]) == len(items)
        # 权重和不为 1 也不影响结论 —— 分母是实际权重和，不假设归一
        assert sub["weight_sum"] != pytest.approx(1.0)

    def test_要求档全缺与权重不归一是两个不同的样本(self):
        """上一版这两个 tag 落在同一个岗位上，`weight_unnormalized` 那档等于没测。

        选样纪律现在写在 `scripts/freeze_test_fixture.py`（候选最少的 tag 先挑、
        挑过的 id 不复用），这里把结果钉住。
        """
        a, b = load_fixture("no_baseline"), load_fixture("weight_unnormalized")
        assert a["occupation"]["id"] != b["occupation"]["id"]
        assert a["required_items"] != b["required_items"]

    def test_要求档部分缺的岗位只算有基准的那部分(self):
        sub, rep = self._report("partial_baseline", level=2)
        items = sub["required_items"]
        scorable = [i for i in items if i["required_level"]]
        assert 0 < len(scorable) < len(items), "fixture 得真的是「部分缺」才有意义"
        assert isinstance(rep["match_score"], float), "有基准的那部分照样算得出分"
        assert len(rep["no_baseline"]) == len(items) - len(scorable)
        assert rep["no_baseline_weight"] == pytest.approx(self._nb_pct(items), abs=0.05)
        # 缺口远超 30%：正是引入 partial_baseline 要解决的那个场景
        assert rep["no_baseline_weight"] > PARTIAL_BASELINE_PCT
        assert rep["score_status"] == "partial_baseline"
        assert all(not i["scorable"] for i in rep["no_baseline"])
        keys = {r["skill_key"] for r in rep["strengths"] + rep["gaps"]}
        assert keys <= {i["skill_key"] for i in scorable}, "无基准项漏进了优势/短板"

    def test_部分缺基准的样本是派生的且标注清楚(self):
        """排掉 `__e2e_*` 残留后库里 0 个真正部分配置的岗位（survey.by_shape 记着）。

        这个形态是规则必须覆盖的（只算有基准的那部分 + 超阈值降级），所以从
        full_baseline 派生。读 fixture 的人必须能一眼分清哪份是真数据、哪份是造的，
        否则「真实形态」这个承诺就废了。
        """
        sub = load_fixture("partial_baseline")
        assert sub["synthetic"] is True
        assert sub["derived_from"]["tag"] == "full_baseline"
        assert "required_level" in sub["derivation"]
        # 只动了要求档：技能名与权重仍与来源样本逐项一致
        base = load_fixture("full_baseline")
        assert [i["skill_key"] for i in sub["required_items"]] == [
            i["skill_key"] for i in base["required_items"]
        ]
        assert [i["weight"] for i in sub["required_items"]] == [
            i["weight"] for i in base["required_items"]
        ]

    def test_没有测试残留数据混进样本(self):
        """`__e2e_*` 技能由 e2e 脚本造、会漂移会被清；拿它当真实形态是自欺欺人。"""
        for tag in self.TAGS:
            keys = [str(i["skill_key"]) for i in load_fixture(tag)["required_items"]]
            assert not [k for k in keys if k.startswith("__e2e")], f"{tag} 里有 e2e 残留"
            assert len(keys) == len(set(keys)), f"{tag} 里有重复 skill_key：{keys}"

    @pytest.mark.parametrize("tag", TAGS)
    def test_五种真实形态都出得来报告(self, tag):
        _, rep = self._report(tag)
        assert isinstance(rep["summary"], str) and "None" not in rep["summary"]
        assert rep["match_score"] is None or 0.0 <= rep["match_score"] <= 100.0
        assert 0.0 <= rep["no_baseline_weight"] <= 100.0
        assert all(0.0 <= i["ratio"] <= 1.0 for i in rep["items"])


# ── 脏权重不打死报告（原 BUG-5，2026-08 定案）─────────────────
#
# `weight` 来自 `kg_edge.weight`，采集脚本与直连改库都绕得过应用层校验。
# 旧实现报告侧 `float(it.get("weight") or 0)` 遇 `"abc"` 直接 ValueError、整份报告 500，
# 而画像侧 `isinstance(w, (int, float))` 把 `"0.5"` 判成 0.0 —— 两边都不对且互不一致。
# 定案：共用 `config.as_weight`（脏值 0.0、数字字符串照解析）。口径本身锁在
# test_pg_guards.py::TestAsWeight，两边同源锁在 test_match_score_parity.py。


class TestDirtyWeightInReport:
    @pytest.mark.parametrize("bad", ["abc", "", "   ", "0.5kg", [], {}, None, True, False])
    def test_非数值权重不打死报告(self, bad):
        rep = build_report(
            occupation=None,
            required_items=[{"skill_key": "A", "category": "c", "required_level": 3, "weight": bad}],
            measured={"A": {"level": 3}},
        )
        assert rep["items"][0]["weight"] == 0.0
        assert rep["items"][0]["weight_pct"] is None
        assert rep["score_status"] == "no_weight", "权重全脏 ⇒ 算不出加权分，但报告照出"
        assert rep["match_score"] == 0.0

    @pytest.mark.parametrize("v,want", [("0.5", 0.5), (" 0.25 ", 0.25), ("1", 1.0)])
    def test_数字字符串权重按数值解析(self, v, want):
        """库里 `weight` 写成字符串很常见；判成 0 等于悄悄把这项踢出分母。"""
        rep = build_report(
            occupation=None,
            required_items=[{"skill_key": "A", "category": "c", "required_level": 3, "weight": v}],
            measured={"A": {"level": 3}},
        )
        assert rep["items"][0]["weight"] == pytest.approx(want)
        assert rep["match_score"] == 100.0

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.5, "-0.5"])
    def test_非有限与负权重取零(self, bad):
        """nan 会把匹配度污染成 nan（前端一片空白），负权重能算出「匹配度 -33%」。"""
        rep = build_report(
            occupation=None,
            required_items=[
                {"skill_key": "脏", "category": "c", "required_level": 3, "weight": bad},
                {"skill_key": "净", "category": "c", "required_level": 3, "weight": 0.5},
            ],
            measured={"脏": {"level": 3}, "净": {"level": 3}},
        )
        assert rep["items"][0]["weight"] == 0.0
        assert rep["match_score"] == 100.0

    def test_一条脏权重不影响其余项(self):
        rep = build_report(
            occupation=None,
            required_items=[
                {"skill_key": "脏", "category": "c", "required_level": 3, "weight": "abc"},
                {"skill_key": "净", "category": "c", "required_level": 4, "weight": 0.5},
            ],
            measured={"脏": {"level": 3}, "净": {"level": 2}},
        )
        assert [i["weight"] for i in rep["items"]] == [0.0, 0.5]
        assert rep["match_score"] == pytest.approx(50.0, abs=0.05)   # 只剩「净」的 2/4


class TestBuildReportUrgencyAndSort:
    def test_短板按权重乘差距档数排序(self):
        required = [
            {"skill_key": "低权重大差距", "category": "c", "required_level": 5, "weight": 0.2},
            {"skill_key": "高权重小差距", "category": "c", "required_level": 3, "weight": 0.6},
        ]
        rep = build_report(
            occupation=None,
            required_items=required,
            measured={"低权重大差距": {"level": 1}, "高权重小差距": {"level": 2}},
        )
        gaps = {g["skill_key"]: g["urgency"] for g in rep["gaps"]}
        assert gaps["低权重大差距"] == pytest.approx(0.2 * 4)   # 0.8
        assert gaps["高权重小差距"] == pytest.approx(0.6 * 1)   # 0.6
        assert [g["skill_key"] for g in rep["gaps"]] == ["低权重大差距", "高权重小差距"]

    def test_没有要求档时紧迫度为零(self):
        rep = build_report(
            occupation=None,
            required_items=[{"skill_key": "A", "category": "c", "required_level": None, "weight": 0.5}],
            measured={"A": {"level": 1}},
        )
        assert rep["items"][0]["urgency"] == 0.0

    def test_优势按超越幅度再按权重排序(self):
        required = [
            {"skill_key": "刚好达标高权重", "category": "c", "required_level": 1, "weight": 0.9},
            {"skill_key": "大幅超越低权重", "category": "c", "required_level": 3, "weight": 0.1},
        ]
        rep = build_report(
            occupation=None,
            required_items=required,
            measured={"刚好达标高权重": {"level": 1}, "大幅超越低权重": {"level": 5}},
        )
        assert [s["skill_key"] for s in rep["strengths"]] == ["大幅超越低权重", "刚好达标高权重"]

    def test_未覆盖项按权重降序(self):
        rep = build_report(occupation=None, required_items=REQUIRED, measured={"安全防护": {"level": 5}})
        assert [u["skill_key"] for u in rep["untested"]] == ["配料准备", "搅拌操作", "设备维保"]

    def test_优势与短板只在实测项里选(self):
        rep = build_report(occupation=None, required_items=REQUIRED, measured={"配料准备": {"level": 3}})
        keys = {r["skill_key"] for r in rep["strengths"] + rep["gaps"]}
        assert keys == {"配料准备"}, "没考过的技能不该出现在优势/短板里 —— 没有证据"


class TestBuildReportShape:
    def test_岗位为空时不炸(self):
        rep = build_report(occupation=None, required_items=REQUIRED, measured={})
        assert rep["target_occupation_id"] is None
        assert rep["target_occupation_name"] is None

    def test_渠道默认为测评(self):
        assert build_report(occupation=None, required_items=[], measured={})["channel"] == "assessment"
        rep = build_report(occupation=None, required_items=[], measured={}, channel="chat")
        assert rep["channel"] == "chat"

    def test_学习计划入口最多带八个短板(self):
        required = [
            {"skill_key": f"S{i}", "category": "c", "required_level": 5, "weight": 0.1}
            for i in range(12)
        ]
        rep = build_report(
            occupation=None,
            required_items=required,
            measured={f"S{i}": {"level": 1} for i in range(12)},
        )
        assert rep["next_action"]["type"] == "learning_path"
        assert len(rep["next_action"]["gap_skills"]) == 8
        assert rep["counts"]["gap"] == 12

    def test_条目带出证据强度字段(self):
        rep = build_report(
            occupation=None,
            required_items=[{"skill_key": "A", "category": "c", "required_level": 3, "weight": 0.5}],
            measured={"A": {"level": 2, "source": "sjt", "evidence_score": 61, "capped": True}},
        )
        it = rep["items"][0]
        assert it["source"] == "sjt"
        assert it["evidence_score"] == 61
        assert it["capped"] is True
        assert it["weight_pct"] == 50
        assert it["required_label"] == "熟练" and it["measured_label"] == "掌握"

    def test_权重为零时不给百分比(self):
        rep = build_report(
            occupation=None,
            required_items=[{"skill_key": "A", "category": "c", "required_level": 3, "weight": 0}],
            measured={"A": {"level": 3}},
        )
        assert rep["items"][0]["weight_pct"] is None

    def test_摘要含匹配度与三个计数(self):
        rep = build_report(occupation=None, required_items=REQUIRED, measured={"配料准备": {"level": 3}})
        s = rep["summary"]
        # 唯一测到的项达标且占 40% 权重 → 整体准备度 40%（不是「测过的都满分 → 100%」）
        assert "综合能力匹配度 40.0%" in s
        assert f"综合能力匹配度 {rep['match_score']}%" in s, "摘要里的数必须就是 match_score"
        assert "已达标 1 项" in s and "待补强 0 项" in s and "另有 3 项本次未覆盖" in s
