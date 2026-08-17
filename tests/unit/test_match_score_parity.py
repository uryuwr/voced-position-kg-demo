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

第二轮（2026-08，「没有基准就不编分数」）
--------------------------------------
分母进一步缩到**可评分**权重（要求档在 1–5 内的技能），并新增 `score_status` /
`no_baseline` / `no_baseline_weight`。这一轮的教训是：**总分口径统一过一次还不够**——
上一轮统一了公式，却各自解析入参：报告侧 `float(w or 0)` 认 `"0.5"`、画像侧
`isinstance(w, (int, float))` 判成 0.0，于是刚统一好的匹配度在脏数据上又分家了。
所以同源锁必须覆盖脏值，`TestParityOnDirtyInput` 就是为此存在的。

这个文件只做一件事：把**两边同源**钉死。分母口径本身在
`test_assessment_report.py::TestBuildReportMatchScore` / `TestNoBaselineScoring` 与
`test_match_profile.py` 里各自有正向断言；这里保证以后谁改一边都会立刻红。
所有断言都用「同一份输入喂两个函数、比结果」的形式，不写死期望值——
写死期望值只能锁住某一边，锁不住「同源」。
"""
from __future__ import annotations

import inspect

import pytest

from backend.agent.assessment.report import build_report
from backend.kg.pg_store.biz_store import match_with_profile
from tests._stable_data import load_fixture

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


def same_score(rep: dict, prof: dict, *, why: str = "") -> None:
    """两边匹配度必须相等，**包括「都算不出来」这种情况**。

    `pytest.approx` 在 None 上不给有用的报错，而 None 恰恰是这一轮新增的取值：
    一边 None 一边 0.0 才是最坏的分家（页面 A 说「未配置能力要求」、页面 B 说 0%）。
    """
    a, b = rep["match_score"], prof["match_score"]
    msg = (
        f"匹配度分家了：报告 {a!r} vs 画像 {b!r}。{why}"
        "口径必须只有一个，否则学员在诊断报告和岗位列表上看到两个数。"
    )
    assert (a is None) == (b is None), msg
    if a is not None:
        assert a == pytest.approx(b, abs=0.05), msg


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
        same_score(rep, prof, why="（干净数据、要求档齐全）")

    @pytest.mark.parametrize("levels", PARITY_CASES)
    def test_同一份数据两边算分状态也相等(self, levels):
        """`score_status` 决定前端显示数字还是文案，它分家等于两个页面口径不同。"""
        rep, prof = both(levels)
        assert rep["score_status"] == prof["score_status"]

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
        same_score(rep, prof)

    def test_全部权重为零时两边都不除零(self):
        req = [{"skill_key": k, "category": "c", "required_level": 3, "weight": 0} for k in "AB"]
        rep, prof = both({"A": 3}, req)
        assert rep["match_score"] == prof["match_score"] == 0.0
        assert rep["score_status"] == prof["score_status"] == "no_weight"

    def test_没有要求档的技能两边同样处理(self):
        """required_level 为空 = 岗位没给要求档 ⇒ 两边都算不出分（都是 None）。

        定案前两边都是「有证据即达标」并计入分子；现在两边都排除出分子分母，
        一项可评分技能都没有 ⇒ 两边都给 None。**关键是两边一起变**。
        """
        req = [
            {"skill_key": "A", "category": "c", "required_level": None, "weight": 0.5},
            {"skill_key": "B", "category": "c", "required_level": 0, "weight": 0.5},
        ]
        rep, prof = both({"A": 1}, req)
        same_score(rep, prof)
        assert rep["match_score"] is None
        assert rep["score_status"] == prof["score_status"] == "no_baseline"

    def test_岗位没有技能构成时两边都是零(self):
        rep, prof = both({"任意": 5}, [])
        assert rep["match_score"] == prof["match_score"] == 0.0
        assert rep["score_status"] == prof["score_status"] == "no_skills"

    def test_单技能岗位两边相等(self):
        req = [{"skill_key": "A", "category": "c", "required_level": 4, "weight": 1.0}]
        rep, prof = both({"A": 2}, req)
        same_score(rep, prof)

    def test_二十项技能只测到六项时两边相等(self):
        """真实测评的形状：岗位二十多项技能，一场只考 10 题。"""
        req = [
            {"skill_key": f"S{i}", "category": f"C{i % 4}", "required_level": (i % 5) + 1,
             "weight": 0.05}
            for i in range(20)
        ]
        rep, prof = both({f"S{i}": (i % 5) + 1 for i in range(6)}, req)
        same_score(rep, prof)


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


class TestNoBaselineParity:
    """「无要求档的技能」两边曾经连**标签**都不一致，现在必须完全一致。

    定案前：报告侧 `ok = bool(lv and req and lv >= req)`（req 为空 ⇒ 不算达标）
    进 `gaps`；画像侧 `ok = ratio >= 1.0`，而 `_ratio` 对无要求档返回 1.0 ⇒ 进
    `strengths`。同一项技能在诊断报告里是短板、在岗位列表里是优势。

    定案后两边一致：无基准的项 `scorable=false`，**既不进 strengths 也不进 gaps**，
    单独进 `no_baseline`。
    """

    NO_REQ = [
        {"skill_key": "无要求档", "category": "c", "required_level": None, "weight": 0.5},
        {"skill_key": "有要求档", "category": "c", "required_level": 3, "weight": 0.5},
    ]

    def test_无要求档的技能两边都不算优势也不算短板(self):
        rep, prof = both({"无要求档": 1, "有要求档": 3}, self.NO_REQ)
        same_score(rep, prof)
        for side in (rep, prof):
            assert [s["skill_key"] for s in side["strengths"]] == ["有要求档"]
            assert side["gaps"] == []
            assert [n["skill_key"] for n in side["no_baseline"]] == ["无要求档"]

    def test_两边的可评分标记逐项相同(self):
        rep, prof = both({"无要求档": 1, "有要求档": 3}, self.NO_REQ)
        assert {i["skill_key"]: i["scorable"] for i in rep["items"]} == {
            i["skill_key"]: i["scorable"] for i in prof["items"]
        }

    def test_两边的基准缺口权重相同(self):
        rep, prof = both({"有要求档": 3}, self.NO_REQ)
        assert rep["no_baseline_weight"] == prof["no_baseline_weight"] == 50.0

    def test_全无基准时两边都给None(self):
        req = [{**it, "required_level": None} for it in self.NO_REQ]
        rep, prof = both({"无要求档": 5, "有要求档": 5}, req)
        same_score(rep, prof, why="（全无基准，两边都该是 None）")
        assert rep["match_score"] is None
        assert rep["score_status"] == prof["score_status"] == "no_baseline"
        assert rep["no_baseline_weight"] == prof["no_baseline_weight"] == 100.0


# ── 脏值下的同源：上一轮就是在这里再次分家的 ───────────────────
#
# 总分公式统一之后，两边仍各自解析入参：报告侧 `float(w or 0)`（`"abc"` 抛 ValueError
# 打死整份报告，`"0.5"` 认成 0.5），画像侧 `isinstance(w, (int, float))`（`"0.5"` 判成 0.0）。
# 于是同一条脏边在诊断报告与岗位列表上给出两个匹配度。定案：两边共用
# `config.as_level` / `as_weight` / `weighted_score`。

DIRTY_WEIGHTS = [None, "", "   ", "abc", "0.5kg", [], {}, True, False,
                 float("nan"), float("inf"), -1, "-0.5", "0.5", " 0.25 ", "1e-3", 0]
DIRTY_REQ_LEVELS = [0, None, 6, 7, 9, 99, -1, -3, "", "3", " 4 ", "L3", "3.5", 3.0, True]
# 实测档的来路：`biz_user_skill.level`（无 CHECK 约束）、简历解析、LLM 画像
DIRTY_MEASURED_LEVELS = [0, None, 6, 9, 99, -1, -3, "3", " 4 ", "abc", "", [], {}, True, 3.0]


def assert_score_parity(rep: dict, prof: dict, *, why: str = "") -> None:
    """**分数与分类**同源：总分、状态、基准缺口、逐项解析、优势/短板/无基准的归类。

    只比总分挡不住分家：两套算法凑巧总分相同、但逐项权重/达成比不同，
    在别的数据上立刻现形。

    不含 `coverage` —— 它在两侧问的**不是同一个问题**（见 `TestEvidenceAsymmetry`），
    所以由 `assert_full_parity` 在「考过 ⟺ 有可用档位」成立的数据上单独比。
    """
    same_score(rep, prof, why=why)
    assert rep["score_status"] == prof["score_status"], f"score_status 分家 {why}"
    assert rep["no_baseline_weight"] == pytest.approx(
        prof["no_baseline_weight"], abs=0.05
    ), f"no_baseline_weight 分家 {why}"
    for field in ("weight", "ratio", "scorable", "ok"):
        a = {i["skill_key"]: i[field] for i in rep["items"]}
        b = {i["skill_key"]: i[field] for i in prof["items"]}
        assert a == b, f"逐项 {field} 分家 {why}：报告 {a} vs 画像 {b}"
    # required_level 的**空值表示**两个契约故意不同（ReportItem 用 null、MatchItem 用 0，
    # 见 schemas_assessment / schemas_student），所以归一之后再比「解析结果」。
    a = {i["skill_key"]: (i["required_level"] or 0) for i in rep["items"]}
    b = {i["skill_key"]: (i["required_level"] or 0) for i in prof["items"]}
    assert a == b, f"逐项 required_level 解析分家 {why}：报告 {a} vs 画像 {b}"
    for bucket in ("strengths", "no_baseline"):
        a = {i["skill_key"] for i in rep[bucket]}
        b = {i["skill_key"] for i in prof[bucket]}
        assert a == b, f"{bucket} 分家 {why}：报告 {a} vs 画像 {b}"
    # `gaps` 的**范围**两边故意不同：报告侧只收「测过但没达标」，没测到的另进
    # `untested`（没有证据不下结论）；画像侧的列表页没有「测过」这个概念，
    # 未达标即短板。两者的关系是恒等式，锁住它而不是锁相等。
    rep_gap = {i["skill_key"] for i in rep["gaps"]}
    rep_untested_scorable = {
        i["skill_key"] for i in rep["untested"] if i["scorable"]
    }
    prof_gap = {i["skill_key"] for i in prof["gaps"]}
    assert prof_gap == rep_gap | rep_untested_scorable, (
        f"gaps 口径分家 {why}：画像 {prof_gap} 应当等于报告的短板 {rep_gap} "
        f"加上未覆盖的可评分项 {rep_untested_scorable}"
    )


def assert_full_parity(rep: dict, prof: dict, *, why: str = "") -> None:
    """分数与分类之外，再加上覆盖率。

    只在「考过 ⟺ 画像里有可用档位」的数据上适用 —— 干净实测档、以及脏 weight /
    脏要求档（它们不影响「有没有证据」的判定）。脏**实测档**下 coverage 会分家，
    那是两侧对「有证据」的定义不同，见 `TestEvidenceAsymmetry`。
    """
    assert_score_parity(rep, prof, why=why)
    assert rep["coverage"] == pytest.approx(prof["coverage"], abs=0.05), f"coverage 分家 {why}"


class TestParityOnDirtyInput:
    """脏 weight / 脏 required_level 下两边仍同源。"""

    @pytest.mark.parametrize("bad", DIRTY_WEIGHTS)
    def test_脏权重两边解析一致(self, bad):
        req = [
            {"skill_key": "脏权重", "category": "c", "required_level": 3, "weight": bad},
            {"skill_key": "净", "category": "c", "required_level": 4, "weight": 0.5},
        ]
        rep, prof = both({"脏权重": 3, "净": 2}, req)
        assert_full_parity(rep, prof, why=f"weight={bad!r}")

    def test_数字字符串权重两边都解析成数值(self):
        """定案二直接钉死的那一条：`"0.5"` 两边都得是 0.5，不是「一边 0.5 一边 0」。"""
        req = [{"skill_key": "A", "category": "c", "required_level": 3, "weight": "0.5"}]
        rep, prof = both({"A": 3}, req)
        assert rep["items"][0]["weight"] == prof["items"][0]["weight"] == 0.5
        same_score(rep, prof)

    @pytest.mark.parametrize("bad", DIRTY_REQ_LEVELS)
    def test_脏要求档两边解析一致(self, bad):
        req = [
            {"skill_key": "脏档", "category": "c", "required_level": bad, "weight": 0.5},
            {"skill_key": "净", "category": "c", "required_level": 4, "weight": 0.5},
        ]
        rep, prof = both({"脏档": 3, "净": 4}, req)
        assert_full_parity(rep, prof, why=f"required_level={bad!r}")

    @pytest.mark.parametrize("bad", DIRTY_REQ_LEVELS)
    def test_要求档全脏时两边都算不出分(self, bad):
        req = [
            {"skill_key": k, "category": "c", "required_level": bad, "weight": 0.5}
            for k in ("A", "B")
        ]
        rep, prof = both({"A": 3}, req)
        assert_full_parity(rep, prof, why=f"required_level 全为 {bad!r}")
        assert (rep["match_score"] is None) == (prof["score_status"] == "no_baseline")

    # 交叉时只取权重的几个**等价类代表**（取 0 的脏值 / 该解析的数字字符串 / 真 0），
    # 全矩阵铺开只是把同一条分支跑 17 遍
    @pytest.mark.parametrize("w", ["abc", None, float("nan"), -1, "0.5", 0])
    @pytest.mark.parametrize("lv", [None, 9, 3])
    def test_权重与要求档同时脏时两边仍同源(self, w, lv):
        req = [
            {"skill_key": "双脏", "category": "c", "required_level": lv, "weight": w},
            {"skill_key": "净", "category": "d", "required_level": 2, "weight": 0.5},
        ]
        rep, prof = both({"双脏": 4, "净": 1}, req)
        assert_full_parity(rep, prof, why=f"weight={w!r} required_level={lv!r}")

    def test_全部字段都脏时两边都不抛错(self):
        req = [
            {"skill_key": "A", "category": None, "required_level": "L9", "weight": "abc"},
            {"skill_key": None, "category": "", "required_level": [], "weight": {}},
        ]
        rep, prof = both({"A": 3}, req)
        assert_full_parity(rep, prof, why="全字段脏")

    # ── 脏**实测**档（原 BUG-7）：上一轮漏掉的那一半，现已修好 ──
    #
    # 报告侧一直走 `as_level(m.get("level"))`，画像侧却把 `user_levels` 的原值
    # 直接送进 `min(ulv / req, 1.0)`。同一个 `9`：报告页 0%、列表页 100%；
    # `"3"` 更直接，画像侧 TypeError 整页 500。修法是画像侧也过 `as_level`。

    @pytest.mark.parametrize("bad", DIRTY_MEASURED_LEVELS)
    def test_脏实测档两边解析一致(self, bad):
        """带一项**干净**实测档作陪：两侧的「有证据」判定才落在同一个前提上。

        全脏时 coverage / score_status 会分家（见 `TestEvidenceAsymmetry`），
        那是两侧对「有证据」的定义不同，不属于本条要锁的解析口径。
        """
        req = [
            {"skill_key": "脏实测", "category": "c", "required_level": 3, "weight": 0.5},
            {"skill_key": "净", "category": "c", "required_level": 3, "weight": 0.5},
        ]
        rep, prof = both({"脏实测": bad, "净": 3}, req)
        assert_score_parity(rep, prof, why=f"measured={bad!r}")

    @pytest.mark.parametrize("bad", [6, 9, 99])
    def test_越界实测档两边都不再给满分(self, bad):
        """就是「报告页 0% / 列表页 100%」那条，正向钉住。"""
        rep, prof = both({"配料准备": bad}, REQUIRED)
        same_score(rep, prof, why=f"measured={bad!r}")
        assert rep["match_score"] == 0.0
        assert {i["skill_key"]: i["ratio"] for i in prof["items"]}["配料准备"] == 0.0

    @pytest.mark.parametrize("bad", ["3", " 4 ", "5"])
    def test_数字字符串实测档两边都照解析(self, bad):
        rep, prof = both({"配料准备": bad}, REQUIRED)
        assert_score_parity(rep, prof, why=f"measured={bad!r}")
        assert prof["items"][0]["user_level"] == int(bad.strip())

    def test_负数实测档两边都不算出负数(self):
        req = [
            {"skill_key": "负档", "category": "c", "required_level": 3, "weight": 0.5},
            {"skill_key": "正常", "category": "c", "required_level": 3, "weight": 0.5},
        ]
        rep, prof = both({"负档": -9, "正常": 3}, req)
        assert_score_parity(rep, prof, why="measured=-9")
        assert rep["match_score"] == prof["match_score"] == 50.0
        assert all(i["ratio"] >= 0 for i in rep["items"] + prof["items"])

    @pytest.mark.parametrize("bad", ["3", "abc", None, [], {}])
    def test_非数值实测档两边都不抛错(self, bad):
        """画像侧曾在这里抛 TypeError；报告侧一直是好的，所以这条只有一侧会红。

        唯一被考到的技能就是脏的那项，两侧对「有证据」的判定必然岔开
        （见 `TestEvidenceAsymmetry`），所以这里只比分数 —— 分数才是学员看到的数。
        """
        rep, prof = both({"配料准备": bad}, REQUIRED)
        same_score(rep, prof, why=f"measured={bad!r}")
        assert all(0.0 <= i["ratio"] <= 1.0 for i in prof["items"])


class TestEvidenceAsymmetry:
    """同源锁的**边界**：两侧对「有证据」的定义本就不同，别误以为这是分家。

    - 报告侧 `tested` = 这项技能**本次被考到**（key 出现在 `measured` 里），
      即使判分结果不可用（`measured_level` 为 None）也算考过；
      `coverage` 与 `has_evidence` 都建立在它上面。
    - 画像侧没有「考过」这个概念，`covered` = 画像里这项技能**有可用档位**
      （`user_level > 0`）。

    干净数据下两者等价，所以 `coverage` 同源锁一直是绿的。脏实测档下才岔开：
    「考过但判分不可用」在报告里算覆盖、在画像里不算。分数不受影响（两边 ratio 都是 0），
    受影响的只有 `coverage`，以及全脏时的 `score_status`。

    存档而非判对错：要不要让 `tested` 也要求「档位可用」，得先定产品口径
    —— 那会同时改掉 `test_实测档为零视为没测到` 锁住的既有语义。
    """

    REQ = [
        {"skill_key": "脏实测", "category": "c", "required_level": 3, "weight": 0.5},
        {"skill_key": "净", "category": "c", "required_level": 3, "weight": 0.5},
    ]

    def test_当前行为存档_脏实测档下覆盖率分家但分数不分家(self):
        rep, prof = both({"脏实测": 9, "净": 3}, self.REQ)
        same_score(rep, prof, why="脏实测档")
        assert rep["coverage"] == 100.0, "报告侧：两项都考过了"
        assert prof["coverage"] == 50.0, "画像侧：只有一项拿到了可用档位"

    def test_当前行为存档_全脏时算分状态分家(self):
        """报告侧「考过了、只是都没考出档位」⇒ ok + 0%；画像侧 ⇒ no_evidence + 0%。

        分数两边都是 0.0，差的是「0% 该怎么读」。画像侧那个说法更准，
        但改报告侧要连着改 `tested` 的语义。
        """
        rep, prof = both({"脏实测": 9, "净": 9}, self.REQ)
        assert rep["match_score"] == prof["match_score"] == 0.0
        assert rep["score_status"] == "ok"
        assert prof["score_status"] == "no_evidence"

    def test_干净数据下两个定义等价(self):
        """这条保证上面两条真的是「脏值专属」，不是随便挑了个差异出来。"""
        for levels in ({}, {"净": 3}, {"脏实测": 3, "净": 3}):
            rep, prof = both(levels, self.REQ)
            assert rep["coverage"] == prof["coverage"]
            assert rep["score_status"] == prof["score_status"]


class TestParityOnRealGraphShapes:
    """冻结的五种真实岗位形态（tests/fixtures/graph_subjects.json）。

    这些是库里真实存在的形状：要求档全缺（80% 的岗位）、部分缺、权重不归一、
    技能数过多。连库取会飘（并行任务在改数据），所以用冻结样本。
    """

    TAGS = ["full_baseline", "no_baseline", "partial_baseline", "weight_unnormalized", "many_skills"]

    @pytest.mark.parametrize("tag", TAGS)
    def test_测到全部技能时两边同源(self, tag):
        items = load_fixture(tag)["required_items"]
        levels = {i["skill_key"]: (i["required_level"] or 3) for i in items}
        rep, prof = both(levels, items)
        assert_full_parity(rep, prof, why=f"fixture={tag}")

    @pytest.mark.parametrize("tag", TAGS)
    def test_一项都没测到时两边同源(self, tag):
        items = load_fixture(tag)["required_items"]
        rep, prof = both({}, items)
        assert_full_parity(rep, prof, why=f"fixture={tag}（无证据）")

    @pytest.mark.parametrize("tag", TAGS)
    def test_只测到最高权重那一项时两边同源(self, tag):
        items = sorted(load_fixture(tag)["required_items"], key=lambda x: -(x["weight"] or 0))
        rep, prof = both({items[0]["skill_key"]: 3}, items)
        assert_full_parity(rep, prof, why=f"fixture={tag}（只测一项）")

    def test_要求档全缺的真实岗位两边都给None(self):
        items = load_fixture("no_baseline")["required_items"]
        rep, prof = both({i["skill_key"]: 3 for i in items}, items)
        assert rep["match_score"] is prof["match_score"] is None
        assert rep["score_status"] == prof["score_status"] == "no_baseline"


class TestSingleImplementation:
    """同源的根：脏值解析与总分公式在全仓**只有一份实现**。

    行为断言之外再钉一道源码断言 —— 谁又在某一侧手写 `float(w or 0)` /
    `int(lv or 0)` / `round(100 * got / total, 1)`，这里立刻报，而不是等到某条
    脏数据在两个页面上给出两个数字才被发现。
    """

    SIDES = [
        ("report", "backend.agent.assessment.report"),
        ("biz_store", "backend.kg.pg_store.biz_store"),
    ]

    @pytest.mark.parametrize("name,mod", SIDES)
    @pytest.mark.parametrize("fn", ["as_level", "as_weight", "weighted_score"])
    def test_两侧用的是config里那一份(self, name, mod, fn):
        import importlib

        from backend.kg.pg_store import config

        m = importlib.import_module(mod)
        assert getattr(m, fn, None) is getattr(config, fn), (
            f"{name} 没有用 config.{fn}，而是自己拿了一份 —— 口径迟早分家"
        )

    def test_报告侧不再有自己的档位守卫(self):
        from backend.agent.assessment import report

        assert not hasattr(report, "_level"), "report._level 已合并进 config.as_level"
        assert not hasattr(report, "LEVEL_MIN"), "档位区间的真源只有 config"

    @pytest.mark.parametrize("fn", [build_report, match_with_profile])
    def test_两个算分函数里都不再裸转权重与档位(self, fn):
        """裸 `float(...)` / `int(...)` 转 weight / level 就是上一轮分家的写法。

        只扫这两个函数体：同模块里别处的 `float(s.get("weight"))` 是学习路径任务的
        权重（应用自己写的 numeric 列，不是无约束的图数据），不在这条规则的射程内。
        """
        import re

        src = inspect.getsource(fn)
        bad = re.findall(r"\b(?:float|int)\(\s*\w*\.?get\(['\"](?:weight|"
                         r"required_level|level)['\"]\)[^)]*\)", src)
        assert not bad, f"{fn.__name__} 里还有裸转：{bad}"

    def test_总分公式只在config里出现一次(self):
        """`round(100 * got / total, 1)` 复制到第二处，两边就能各自漂 0.1%。"""
        import re

        from backend.kg.pg_store import config

        pat = re.compile(r"round\(\s*100\s*\*\s*got_w\s*/\s*total_w")
        assert pat.search(inspect.getsource(config.weighted_score))
        for mod in (build_report, match_with_profile):
            assert not pat.search(inspect.getsource(mod)), (
                f"{mod.__name__} 又自己算了一遍加权分，应当调 config.weighted_score"
            )
