"""backend/agent/assessment/report.py —— 综合能力报告（纯函数，不碰库）。

锁四件事：
- `_build_radar` 的「技能轴 → 大类轴」回落规则
- `match_score` 按**全部**技能权重做分母（口径 =「这个岗位你整体准备好了多少」），
  与 `match_with_profile` 同源；两边同源本身由 tests/unit/test_match_score_parity.py 锁
- 1–5 之外的脏档位**不能打死整份报告**（`TestDirtyLevelGuard`）
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
        # required=0 表示岗位没给要求档；测到了就算达标，没测到才是 0
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
    """整份报告在脏档位下必须仍然生成——测评做完了不能因为一条脏边看不到结果。"""

    THREE = [
        {"skill_key": "A", "category": "生产准备", "required_level": 3, "weight": 0.5},
        {"skill_key": "B", "category": "生产准备", "required_level": 3, "weight": 0.3},
        {"skill_key": "C", "category": "设备", "required_level": 3, "weight": 0.2},
    ]

    @pytest.mark.parametrize("bad", DIRTY_LEVELS)
    def test_实测档越界时报告仍能生成(self, bad):
        rep = build_report(
            occupation=None,
            required_items=self.THREE,
            measured={"A": {"level": bad}, "B": {"level": 2}, "C": {"level": 2}},
        )
        assert isinstance(rep["match_score"], float)
        assert isinstance(rep["summary"], str)
        assert len(rep["items"]) == 3

    @pytest.mark.parametrize("bad", DIRTY_LEVELS)
    def test_要求档越界时报告仍能生成(self, bad):
        required = [{**it, "required_level": bad} for it in self.THREE]
        rep = build_report(
            occupation=None,
            required_items=required,
            measured={"A": {"level": 3}, "B": {"level": 2}, "C": {"level": 2}},
        )
        assert isinstance(rep["match_score"], float)

    @pytest.mark.parametrize("bad", DIRTY_LEVELS)
    def test_两侧同时越界时报告仍能生成(self, bad):
        required = [{**it, "required_level": bad} for it in self.THREE]
        rep = build_report(
            occupation=None,
            required_items=required,
            measured={k: {"level": bad} for k in ("A", "B", "C")},
        )
        assert isinstance(rep["match_score"], float)

    @pytest.mark.parametrize("bad", DIRTY_LEVELS)
    def test_只有一项技能且档位越界时也不炸(self, bad):
        """1 项技能 → 雷达走大类回落分支，是另一条代码路径。"""
        rep = build_report(
            occupation=None,
            required_items=[{"skill_key": "A", "category": "c", "required_level": bad, "weight": 1.0}],
            measured={"A": {"level": bad}},
        )
        assert isinstance(rep["match_score"], float)
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
    """口径（2026-08 产品决策）：分母是**全部**技能权重。

    `match_score` 回答的是「这个岗位你整体准备好了多少」，没测到的项按 0 计入；
    「本次测评覆盖了多少」由 `coverage` 单独表达。这样它与岗位探索列表的
    `match_with_profile` 才是同一个数字（见 test_match_score_parity.py）。
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
        assert rep["counts"] == {
            "skill_total": 4, "tested": 0, "untested": 4, "strength": 0, "gap": 0
        }

    def test_岗位没有技能构成时不除零(self):
        rep = build_report(occupation=None, required_items=[], measured={})
        assert rep["match_score"] == 0.0
        assert rep["coverage"] == 0.0
        assert rep["items"] == []

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
