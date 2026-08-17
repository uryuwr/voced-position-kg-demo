"""backend/kg/pg_store/biz_store.py::match_with_profile —— 岗位匹配度（纯函数）。

四条产品口径：
- `user_level = 0` 表示**无证据**，不是「水平为零」；调用方靠 covered/coverage 显示「未评估」
- 单项达标率 `min(实测/要求, 1)` 封顶 1.0，超额完成不能把总分顶上去
- **没有要求档就没有基准**：该项 `scorable=false`、分子分母都不计；一项可评分技能
  都没有时 `match_score` 是 None（库内 80% 的岗位是这个状态），见 `TestNoBaseline`
- 技能名退化匹配有两道闸（最短长度 + 重合比例），拦住「运维」命中「设备运维管理」

与诊断报告（`report.build_report`）的同源锁在 tests/unit/test_match_score_parity.py。
"""
from __future__ import annotations

import pytest

from backend.kg.pg_store.biz_store import (
    FUZZY_MIN_LEN,
    FUZZY_MIN_RATIO,
    has_baseline,
    match_with_profile,
)
from tests._stable_data import load_fixture

OCC = {"id": "CN:occupation:mohrss:6-31-01-05", "name": "混凝土工", "level": "四级"}

REQUIRED = [
    {"skill_key": "配料准备", "category": "生产准备", "required_level": 3,
     "weight": 0.5, "weight_pct": 50, "is_core": True},
    {"skill_key": "搅拌操作", "category": "生产准备", "required_level": 4,
     "weight": 0.3, "weight_pct": 30, "is_core": False},
    {"skill_key": "安全防护", "category": "安全", "required_level": 2,
     "weight": 0.2, "weight_pct": 20, "is_core": False},
]


def one(skill="故障诊断", *, level=3, weight=1.0, category="c"):
    return [{"skill_key": skill, "category": category, "required_level": level, "weight": weight}]


class TestNoEvidence:
    def test_画像为空时全部无证据(self):
        r = match_with_profile(OCC, REQUIRED, {})
        assert r["match_score"] == 0.0
        assert r["score_status"] == "no_evidence", "有基准有权重，只是没有证据"
        assert r["covered_count"] == 0
        assert r["coverage"] == 0.0
        assert all(i["user_level"] == 0 and i["matched_by"] is None for i in r["items"])

    def test_覆盖率与匹配度分开表达(self):
        """一项没命中时 score 必然是 0，但那是「没有证据」而非「完全不匹配」。"""
        r = match_with_profile(OCC, REQUIRED, {"配料准备": 3})
        assert r["covered_count"] == 1
        assert r["coverage"] == pytest.approx(50.0)      # 命中项占总权重 50%
        assert r["match_score"] == pytest.approx(50.0)
        assert r["score_status"] == "ok"

    def test_没有技能要求时不除零(self):
        r = match_with_profile(OCC, [], {"任意": 5})
        assert r["match_score"] == 0.0
        assert r["coverage"] == 0.0
        assert r["skill_total"] == 0
        # 当前行为存档：`no_skills` 回 0.0，而 `no_baseline` 回 None —— 两种「算不出分」
        # 取值不一致，前端得写两套判断。未定案，这里只钉现状（与
        # test_assessment_report.py::test_岗位没有技能构成时不除零 是同一处）。
        assert r["score_status"] == "no_skills"
        assert r["no_baseline_weight"] == 0.0, "没有技能就没有「缺基准的权重」"


class TestRatioCap:
    def test_超额完成封顶(self):
        r = match_with_profile(OCC, one(level=2), {"故障诊断": 5})
        assert r["items"][0]["ratio"] == 1.0
        assert r["match_score"] == 100.0

    def test_按比例折算并保留三位(self):
        r = match_with_profile(OCC, one(level=3), {"故障诊断": 1})
        assert r["items"][0]["ratio"] == pytest.approx(0.333)

    def test_没有要求档时展示的达成比仍是一(self):
        """`ratio` 的既有语义没动（有证据即 1.0），但这一项**不再参与算分**。

        原意图是「岗位没给要求档就别为此扣分」。定案后改由 `scorable=false` 表达：
        既不扣分也不加分，整项排除出分子分母。1.0 只留给前端按项展示。
        """
        r = match_with_profile(OCC, one(level=0), {"故障诊断": 1})
        assert r["items"][0]["ratio"] == 1.0
        assert r["items"][0]["scorable"] is False
        assert r["items"][0]["ok"] is False, "没有基准 ⇒ 谈不上达标"
        assert r["match_score"] is None, "旧行为在这里给 100%（唯一一项按满分计入）"
        assert r["score_status"] == "no_baseline"

    def test_没有要求档且无证据时展示的达成比是零(self):
        r = match_with_profile(OCC, one(level=0), {})
        assert r["items"][0]["ratio"] == 0.0
        assert r["items"][0]["scorable"] is False
        assert r["match_score"] is None, "无基准优先于无证据：连能不能评分都还不知道"
        assert r["score_status"] == "no_baseline"

    def test_达标判定是实测达到要求档(self):
        r = match_with_profile(OCC, one(level=3), {"故障诊断": 3})
        assert r["items"][0]["ok"] is True
        assert [s["skill_key"] for s in r["strengths"]] == ["故障诊断"]
        assert r["matched_count"] == 1


class TestFuzzyMatch:
    def test_闸门常量(self):
        assert (FUZZY_MIN_LEN, FUZZY_MIN_RATIO) == (4, 0.55)

    def test_精确匹配优先(self):
        r = match_with_profile(OCC, one("数据分析与复盘"), {"数据分析与复盘": 4, "数据分析": 1})
        assert r["items"][0]["matched_by"] == "数据分析与复盘"
        assert r["items"][0]["user_level"] == 4

    def test_重合比例够高时放行(self):
        # 「数据分析」占「数据分析与复盘」= 4/7 ≈ 0.57 ≥ 0.55
        r = match_with_profile(OCC, one("数据分析与复盘"), {"数据分析": 4})
        assert r["items"][0]["matched_by"] == "数据分析"
        assert r["items"][0]["user_level"] == 4

    def test_重合比例过低时拦下(self):
        # 「运维」占「设备运维管理」只有 2/6 ≈ 0.33，放行会让无关技能按满档计入
        r = match_with_profile(OCC, one("设备运维管理"), {"运维": 5})
        assert r["items"][0]["user_level"] == 0
        assert r["items"][0]["matched_by"] is None

    def test_过短的名字不参与包含匹配(self):
        r = match_with_profile(OCC, one("诊断"), {"汽车故障诊断": 5})
        assert r["items"][0]["user_level"] == 0, "技能名短于 4 字不做退化匹配"

    def test_画像里过短的名字也不参与(self):
        r = match_with_profile(OCC, one("汽车故障诊断"), {"诊断": 5})
        assert r["items"][0]["user_level"] == 0

    def test_命中多个时取重合比例最高的(self):
        r = match_with_profile(
            OCC, one("数据分析与复盘"), {"数据分析": 2, "数据分析与复盘方法": 5}
        )
        assert r["items"][0]["matched_by"] == "数据分析与复盘方法"
        assert r["items"][0]["user_level"] == 5

    def test_大小写与空白不敏感(self):
        r = match_with_profile(OCC, one("Python 后端开发"), {"  python 后端开发  ": 4})
        assert r["items"][0]["user_level"] == 4


class TestWeightRobustness:
    """原意图：weight 来自 kg_edge，脏值不能打死整页岗位列表。这条意图不变。

    变的是「什么算脏值」：`"0.5"` 是**数字字符串**，库里的 JSON/TEXT 里很常见，
    定案（2026-08）要求解析成 0.5 —— 判成 0.0 等于悄悄把这项技能踢出分母，
    比抛错更难查，而且报告侧认 0.5、这边认 0.0 时同一份数据出两个匹配度。
    真正的脏值（`"abc"` 之类）仍然取 0.0，见下面第二条。
    """

    @pytest.mark.parametrize("v,want", [("0.5", 0.5), (" 0.25 ", 0.25), ("1", 1.0)])
    def test_数字字符串权重按数值解析(self, v, want):
        r = match_with_profile(OCC, one(weight=v), {"故障诊断": 3})
        assert r["items"][0]["weight"] == pytest.approx(want)
        assert r["match_score"] == 100.0    # 达标且是唯一一项
        assert r["score_status"] == "ok"

    def test_真正的脏权重按零处理而不是抛错(self):
        r = match_with_profile(OCC, one(weight="abc"), {"故障诊断": 3})
        assert r["items"][0]["weight"] == 0.0
        assert r["match_score"] == 0.0
        assert r["score_status"] == "no_weight", "有基准有证据，只是权重算不出加权分"

    @pytest.mark.parametrize(
        "bad",
        [None, "", "   ", "abc", "0.5kg", [], {}, True, False,
         float("nan"), float("inf"), -1, "-0.5"],
    )
    def test_各种脏权重都不炸且取零(self, bad):
        r = match_with_profile(OCC, one(weight=bad), {"故障诊断": 3})
        assert r["items"][0]["weight"] == 0.0

    def test_一条脏权重不影响其余项(self):
        req = [
            {"skill_key": "脏", "category": "c", "required_level": 3, "weight": "abc"},
            {"skill_key": "净", "category": "c", "required_level": 4, "weight": 0.5},
        ]
        r = match_with_profile(OCC, req, {"脏": 3, "净": 2})
        assert [i["weight"] for i in r["items"]] == [0.0, 0.5]
        assert r["match_score"] == pytest.approx(50.0, abs=0.05)

    def test_技能键缺失时按空串处理(self):
        r = match_with_profile(
            OCC, [{"category": "c", "required_level": 3, "weight": 1.0}], {"任意技能": 3}
        )
        assert r["items"][0]["skill_key"] is None
        assert r["items"][0]["user_level"] == 0


# ── 没有基准就不编分数（定案 2026-08，与报告侧同源）──────────
#
# 库内 608 个有技能构成的岗位中 490 个（80%）一项要求档都没配（老国标 skill_level
# 节点只有 `attrs.level_code`，没有产品档 `attrs.level`）。旧行为把这些项按
# 「无要求即满足」计入分子，一份只有 L1 证据的画像能在这类岗位上拿到高分。

HALF = [
    {"skill_key": "有基准", "category": "c", "required_level": 5, "weight": 0.4},
    {"skill_key": "无基准", "category": "c", "required_level": None, "weight": 0.6},
]
DIRTY_REQ = [0, None, 6, 9, 99, -1, "", "L3", "abc", 3.5]


class TestNoBaseline:
    def test_全无基准时给None而不是零(self):
        req = [
            {"skill_key": f"S{i}", "category": "c", "required_level": None, "weight": 0.5}
            for i in range(2)
        ]
        r = match_with_profile(OCC, req, {"S0": 3, "S1": 5})
        assert r["match_score"] is None, "0% 会被读成「完全不匹配」，真相是岗位没配要求"
        assert r["score_status"] == "no_baseline"
        assert r["no_baseline_weight"] == 100.0
        assert len(r["no_baseline"]) == 2
        assert r["strengths"] == [] and r["gaps"] == []
        assert r["radar"] == {"categories": [], "scores": []}, "没有达标率就没有轴"

    @pytest.mark.parametrize("bad", DIRTY_REQ)
    def test_越界或不可解析的要求档一律视作没有基准(self, bad):
        r = match_with_profile(OCC, one(level=bad), {"故障诊断": 3})
        assert r["items"][0]["scorable"] is False
        assert r["items"][0]["required_level"] == 0, "契约里 0 表示「没有指定要求档」"
        assert r["match_score"] is None and r["score_status"] == "no_baseline"

    def test_无基准项不进优势也不进短板(self):
        r = match_with_profile(OCC, HALF, {"有基准": 5, "无基准": 5})
        assert [s["skill_key"] for s in r["strengths"]] == ["有基准"]
        assert r["gaps"] == []
        assert [n["skill_key"] for n in r["no_baseline"]] == ["无基准"]

    def test_无基准项不计入分子(self):
        """旧行为：(1/5×0.4 + 1.0×0.6)/1.0 = 68%。新行为只看有基准的那 0.4。"""
        r = match_with_profile(OCC, HALF, {"有基准": 1, "无基准": 5})
        assert r["items"][1]["ratio"] == 1.0 and r["items"][1]["scorable"] is False
        assert r["match_score"] == pytest.approx(20.0, abs=0.05)

    def test_无基准项不计入分母(self):
        """反方向：旧行为 1.0×0.4/1.0 = 40%（无基准且无证据 ⇒ ratio 0 计入分母）。"""
        r = match_with_profile(OCC, HALF, {"有基准": 5})
        assert r["match_score"] == 100.0
        assert r["coverage"] == 100.0, "分母是可评分权重，该评的那一项有证据"
        assert r["covered_count"] == 1, "覆盖数也只数可评分项"

    def test_部分缺基准时报出基准缺口并降级状态(self):
        """60% 的权重评不了分却给 100% —— 分数照给，但必须标成「仅供参考」。

        阈值口径见 test_pg_guards.py::TestPartialBaselineThreshold，
        与报告侧同一个函数（`config.degrade_for_baseline_gap`）。
        """
        r = match_with_profile(OCC, HALF, {"有基准": 5})
        assert r["no_baseline_weight"] == 60.0
        assert r["match_score"] == 100.0
        assert r["score_status"] == "partial_baseline"

    def test_缺口在阈值内时不降级(self):
        req = [
            {"skill_key": "有基准", "category": "c", "required_level": 5, "weight": 0.8},
            {"skill_key": "无基准", "category": "c", "required_level": None, "weight": 0.2},
        ]
        r = match_with_profile(OCC, req, {"有基准": 5})
        assert (r["no_baseline_weight"], r["score_status"]) == (20.0, "ok")

    def test_权重全为零时基准缺口退回按项数算(self):
        req = [{**it, "weight": 0} for it in HALF]
        r = match_with_profile(OCC, req, {"有基准": 5})
        assert r["no_baseline_weight"] == 50.0      # 1/2 项
        assert r["score_status"] == "no_weight"

    def test_无基准项按权重降序排(self):
        req = [
            {"skill_key": "小", "category": "c", "required_level": None, "weight": 0.1},
            {"skill_key": "大", "category": "c", "required_level": None, "weight": 0.9},
        ]
        r = match_with_profile(OCC, req, {})
        assert [n["skill_key"] for n in r["no_baseline"]] == ["大", "小"]


# ── 脏实测档：画像侧也走 as_level（原 BUG-7，2026-08 定案并修好）────
#
# 上一轮把 `required_level` / `weight` 的解析抽成了 `config.as_level` / `as_weight`
# 两侧共用，却漏了 `user_levels` 这一半：`ulv` 是画像里的原值，直接进
# `min(ulv / req, 1.0)`。症状（都已复现过，现已消除）：
#
#   9  → ratio 1.0、列表页匹配度 100%，而报告页同一份数据是 0%
#   -3 → ratio -1.0，配上另一项有证据的技能能把加权分算成 -100%
#   "3"→ `ulv / req` 抛 TypeError，一条脏画像打死整页岗位列表
#
# 来路是真的：`biz_user_skill.level` 只有主键、**没有 CHECK 约束**，越界整数写得进去；
# `memory_levels` 那一路走 LLM 解析，出什么值不受控。当前库里恰好干净，属于潜伏。
# 修法是 `ulv = as_level(raw_ulv) or 0`，与报告侧同一个函数。

DIRTY_MEASURED = [6, 9, 99, -1, -3, "abc", "", "   ", None, 0, 3.0, True, False, [], {}]


class TestDirtyMeasuredLevel:
    """画像里的实测档也必须过 `as_level`：越界/不可解析 ⇒ 按**无证据**处理。"""

    @pytest.mark.parametrize("bad", [6, 9, 99])
    def test_越界实测档按无证据处理而不是满分(self, bad):
        r = match_with_profile(OCC, one(level=3), {"故障诊断": bad})
        assert r["items"][0]["user_level"] == 0, "越界值不该原样透出去"
        assert r["items"][0]["ratio"] == 0.0
        assert r["items"][0]["ok"] is False
        assert r["match_score"] == 0.0
        assert r["score_status"] == "no_evidence"
        assert r["covered_count"] == 0

    @pytest.mark.parametrize("bad", [-1, -3, -9])
    def test_负数实测档不会算出负达成比(self, bad):
        r = match_with_profile(OCC, one(level=3), {"故障诊断": bad})
        assert r["items"][0]["ratio"] == 0.0, "契约说 ratio 封顶 1.0，下限同样得是 0"

    def test_负数实测档不会把总分拖成负数(self):
        """有另一项技能撑着 has_evidence 时，负比例会真的进加权分。"""
        req = [
            {"skill_key": "负档", "category": "c", "required_level": 3, "weight": 0.5},
            {"skill_key": "正常", "category": "c", "required_level": 3, "weight": 0.5},
        ]
        r = match_with_profile(OCC, req, {"负档": -9, "正常": 3})
        assert r["match_score"] == 50.0, "旧行为算出 -100%（页面上真会显示「匹配度 -100%」）"
        assert all(i["ratio"] >= 0.0 for i in r["items"])

    @pytest.mark.parametrize("bad", ["3", "abc", None, [], {}, True])
    def test_非数值实测档不打死整页岗位列表(self, bad):
        """旧行为在 `"3"` / `"abc"` 上抛 TypeError —— 一条脏画像整页 500。"""
        r = match_with_profile(OCC, one(level=3), {"故障诊断": bad})
        assert isinstance(r["match_score"], float)

    @pytest.mark.parametrize("v,want", [("3", 3), (" 4 ", 4), ("5", 5)])
    def test_数字字符串实测档照解析(self, v, want):
        """与 `as_weight` 对 `"0.5"` 的口径一致：该解析就解析，别判成 0。"""
        r = match_with_profile(OCC, one(level=3), {"故障诊断": v})
        assert r["items"][0]["user_level"] == want
        assert r["items"][0]["ratio"] == 1.0

    @pytest.mark.parametrize("bad", DIRTY_MEASURED)
    def test_任何脏实测档都不抛错且输出仍在契约范围内(self, bad):
        r = match_with_profile(OCC, REQUIRED, {"配料准备": bad, "搅拌操作": 3})
        assert 0.0 <= r["match_score"] <= 100.0
        assert all(0.0 <= i["ratio"] <= 1.0 for i in r["items"])
        assert all(isinstance(i["user_level"], int) for i in r["items"])
        assert all(0 <= i["user_level"] <= 5 for i in r["items"])

    def test_模糊匹配命中的脏档也要过守卫(self):
        """脏值可能从退化匹配那条路进来，守卫不能只挡精确匹配那一支。"""
        r = match_with_profile(OCC, one("数据分析与复盘", level=3), {"数据分析": 9})
        assert r["items"][0]["matched_by"] == "数据分析", "还是命中了这一项"
        assert r["items"][0]["user_level"] == 0, "但档位不可信，按无证据算"


class TestHasBaseline:
    """路由在级联**之前**用它判掉无基准岗位。

    往下走会被 `covered_count == 0` 误判成 `no_overlap`，文案变成「你的画像未覆盖
    该岗位要求的技能」—— 把数据缺口说成学员的问题。
    """

    def test_有一项可信要求档就算有基准(self):
        assert has_baseline(HALF) is True

    def test_全缺时没有基准(self):
        assert has_baseline([{"required_level": None}, {"required_level": 0}]) is False

    @pytest.mark.parametrize("bad", DIRTY_REQ)
    def test_脏要求档不算基准(self, bad):
        assert has_baseline([{"required_level": bad}]) is False

    def test_空技能构成没有基准(self):
        assert has_baseline([]) is False

    def test_与算分口径一致(self):
        """`has_baseline` 与 `score_status=="no_baseline"` 必须是同一个判断。

        分成两处实现过一次就会漂：路由说「有基准」而算分说「没有」，
        接口会返回 `source=diagnosis` + `match_score=null` 这种自相矛盾的组合。
        """
        # 不含空技能构成：路由在 has_baseline 之前就用 `if not required` 判掉了
        # （返回 source="none"），而算分那边给的是 no_skills 而非 no_baseline。
        cases = [HALF, one(level=3), one(level=9), [{"required_level": None, "weight": 1}],
                 load_fixture("no_baseline")["required_items"],
                 load_fixture("partial_baseline")["required_items"]]
        for req in cases:
            r = match_with_profile(OCC, req, {})
            assert has_baseline(req) is (r["score_status"] != "no_baseline"), req

    @pytest.mark.parametrize("tag,want", [("full_baseline", True), ("no_baseline", False),
                                          ("partial_baseline", True), ("many_skills", False)])
    def test_真实岗位形态(self, tag, want):
        assert has_baseline(load_fixture(tag)["required_items"]) is want


class TestOutputShape:
    def test_短板按权重降序(self):
        r = match_with_profile(OCC, REQUIRED, {})
        assert [g["skill_key"] for g in r["gaps"]] == ["配料准备", "搅拌操作", "安全防护"]

    def test_雷达按大类聚合达标率(self):
        r = match_with_profile(OCC, REQUIRED, {"配料准备": 3})
        assert r["radar"]["categories"] == ["生产准备", "安全"]
        # 生产准备 = (1.0 + 0.0)/2 → 50；安全 = 0
        assert r["radar"]["scores"] == [50, 0]

    def test_空大类归到未分类(self):
        r = match_with_profile(OCC, one(category=None), {})
        assert r["radar"]["categories"] == ["未分类"]

    def test_回显岗位三要素(self):
        r = match_with_profile(OCC, REQUIRED, {})
        assert r["occupation"] == {"id": OCC["id"], "name": "混凝土工", "level": "四级"}

    def test_条目透传权重百分比与核心标记(self):
        it = match_with_profile(OCC, REQUIRED, {})["items"][0]
        assert it["weight_pct"] == 50 and it["is_core"] is True
