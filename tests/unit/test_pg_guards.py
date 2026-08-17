"""backend/kg/pg_store/ 的三类守卫：可见性 SQL 片段 + attrs 写入校验 + 读侧脏值解析。

「一条脏数据不能打死一整页」——这个项目栽过三次，都是同一个形状。读侧靠
`attrs_level_int` 把脏值取成 NULL，写侧靠 `_assert_attrs_sane` 当场拒绝；
采集脚本和直连改库绕得过应用层，**两侧都要站得住**。

SQL 出了库之后还要在 Python 侧再解析一次（`attrs` 是无约束 TEXT，`kg_edge.weight`
是 numeric 但历史脏行什么都有），这层的唯一实现是 `config.as_level` / `as_weight` /
`weighted_score`。它们此前在报告侧、画像侧、出题侧**各写了一份**，同一条脏数据在
两个页面上给出两个数字（详见 test_match_score_parity.py 的同源锁）；所以口径锁在这里，
调用点只锁「用的是这一份」。
"""
from __future__ import annotations

import math
import re
from decimal import Decimal

import pytest

from backend.kg.pg_store.config import (
    ADMIN_STATUSES,
    ARCHIVED_STATUS,
    LEVEL_MAX,
    LEVEL_MIN,
    PARTIAL_BASELINE_PCT,
    PUBLIC_STATUSES,
    SCORE_STATUSES,
    as_level,
    as_weight,
    attrs_level_int,
    degrade_for_baseline_gap,
    edge_not_archived,
    edge_published,
    is_partial_baseline,
    node_not_archived,
    node_published,
    weighted_score,
)
from backend.kg.pg_store.write import _assert_attrs_sane


# ── 状态口径 ──────────────────────────────────────────────────


class TestStatusVocabulary:
    def test_前台只认已发布(self):
        assert PUBLIC_STATUSES == ("published",)

    def test_管理台三态可见归档除外(self):
        assert set(ADMIN_STATUSES) == {"published", "draft", "disabled"}
        assert ARCHIVED_STATUS == "archived"
        assert ARCHIVED_STATUS not in ADMIN_STATUSES


# ── 可见性 SQL 片段 ───────────────────────────────────────────


class TestVisibilityFragments:
    def test_边前台可见性(self):
        assert edge_published("e") == "COALESCE(e.status, 'published') = 'published'"

    def test_边管理台可见性排除归档(self):
        assert edge_not_archived("e") == "COALESCE(e.status, 'published') <> 'archived'"

    def test_默认别名是e(self):
        assert edge_published() == edge_published("e")
        assert edge_not_archived() == edge_not_archived("e")

    def test_别名可换(self):
        assert edge_published("x").startswith("COALESCE(x.status")

    def test_节点无别名时直接用列名(self):
        assert node_published() == "COALESCE(status, 'published') = 'published'"
        assert node_not_archived() == "COALESCE(status, 'published') <> 'archived'"

    def test_节点带别名(self):
        assert node_published("n") == "COALESCE(n.status, 'published') = 'published'"
        assert node_not_archived("n") == "COALESCE(n.status, 'published') <> 'archived'"

    @pytest.mark.parametrize(
        "frag",
        [edge_published("e"), edge_not_archived("e"), node_published("n"), node_not_archived("n")],
    )
    def test_历史空状态一律按已发布处理(self, frag):
        assert "COALESCE(" in frag and "'published'" in frag


# ── attrs.level 读侧兜底 ──────────────────────────────────────


class TestAttrsLevelInt:
    def test_只有纯数字才转整数(self):
        sql = attrs_level_int("n")
        assert sql == (
            "CASE WHEN (n.attrs::json->>'level') ~ '^[0-9]+$' "
            "THEN (n.attrs::json->>'level')::int END"
        )

    def test_默认别名是n(self):
        assert attrs_level_int() == attrs_level_int("n")

    def test_别名可换(self):
        assert "sl.attrs::json->>'level'" in attrs_level_int("sl")

    def test_没有else分支所以脏值取NULL(self):
        """裸写 ::int 时库里有一行 'L3' 就整页 500；这里必须是 CASE ... END 不带 ELSE。"""
        sql = attrs_level_int("n")
        assert sql.rstrip().endswith("END")
        assert not re.search(r"\bELSE\b", sql, re.I)

    def test_正则锚定首尾防止3点5之类混过(self):
        assert "'^[0-9]+$'" in attrs_level_int("n")


# ── attrs.level 写侧校验 ──────────────────────────────────────

SKILL_TYPES = ["skill_level", "skill", "skill_bundle"]


class TestAssertAttrsSane:
    @pytest.mark.parametrize("bad", ["L3", "3.5", 0, 9, True, False, "三级", -1, 3.0, [3], {"a": 1}, "L", "3a"])
    def test_必须拒绝的档位(self, bad):
        with pytest.raises(ValueError, match="attrs.level 必须是 1–5 的整数"):
            _assert_attrs_sane({"level": bad}, "skill_level")

    def test_布尔要单独挡掉(self):
        """bool 是 int 的子类，不特判的话 True 会被当成 1 混进库里。"""
        with pytest.raises(ValueError):
            _assert_attrs_sane({"level": True}, "skill_level")

    @pytest.mark.parametrize("lv", [1, 2, 3, 4, 5])
    def test_一到五的整数放行(self, lv):
        attrs = {"level": lv}
        _assert_attrs_sane(attrs, "skill_level")
        assert attrs["level"] == lv

    def test_数字字符串顺手归一成整数(self):
        attrs = {"level": "3"}
        _assert_attrs_sane(attrs, "skill_level")
        assert attrs["level"] == 3, "库里只存一种形态"
        assert isinstance(attrs["level"], int)

    def test_带空格的数字字符串也归一(self):
        attrs = {"level": " 4 "}
        _assert_attrs_sane(attrs, "skill_level")
        assert attrs["level"] == 4

    @pytest.mark.parametrize("blank", [None, "", "   "])
    def test_空值当作未填写而不是错误(self, blank):
        attrs = {"level": blank}
        _assert_attrs_sane(attrs, "skill_level")   # 不抛
        assert attrs["level"] == blank             # 也不改写

    def test_没有level键时跳过(self):
        _assert_attrs_sane({"code": "X"}, "skill_level")

    @pytest.mark.parametrize("attrs", [None, "nope", 123, [], ("level", 3)])
    def test_attrs不是字典时跳过(self, attrs):
        _assert_attrs_sane(attrs, "skill_level")

    @pytest.mark.parametrize("ntype", SKILL_TYPES)
    def test_三种技能类型都校验(self, ntype):
        with pytest.raises(ValueError):
            _assert_attrs_sane({"level": "L3"}, ntype)

    @pytest.mark.parametrize("ntype", ["SKILL_LEVEL", "Skill", "  skill_bundle  ".strip()])
    def test_类型名大小写不敏感(self, ntype):
        with pytest.raises(ValueError):
            _assert_attrs_sane({"level": "L3"}, ntype)

    @pytest.mark.parametrize("ntype", ["occupation", "major", "industry", "", None, "course"])
    def test_非技能节点的level另有语义不校验(self, ntype):
        """occupation.attrs.level 是「四级/中级工」这类国标职业等级，与产品档无关。"""
        attrs = {"level": "四级/中级工"}
        _assert_attrs_sane(attrs, ntype)
        assert attrs["level"] == "四级/中级工"

    def test_错误信息说明产品档语义便于运营自查(self):
        with pytest.raises(ValueError) as e:
            _assert_attrs_sane({"level": "L3"}, "skill_level")
        msg = str(e.value)
        assert "1 了解" in msg and "5 专家" in msg
        assert "'L3'" in msg, "要把收到的原值回显出来"


# ── 读侧脏值解析：config.as_level ─────────────────────────────
#
# 出了 SQL 还得再解析一次：`attrs_level_int` 的正则 `^[0-9]+$` 只挡非数字，
# `9` 会原样穿过来变成「岗位要求 L9」。三条来路都不干净——required_level 来自
# skill_level 节点的 `attrs.level`，measured_level 来自选项 level 与简历画像
# （模型给几就是几）。

# 数值越界（正、负、零）；0 与 None 走的是既有 falsy 分支
OUT_OF_RANGE = [0, 6, 7, 9, 99, -1, -3]
UNPARSEABLE = [None, "", "   ", "L3", "三级", "3.5", "3a", [], {}, (), object()]


class TestAsLevel:
    @pytest.mark.parametrize("lv", [1, 2, 3, 4, 5])
    def test_一到五原样放行(self, lv):
        assert as_level(lv) == lv

    def test_档位区间常量就是产品档(self):
        assert (LEVEL_MIN, LEVEL_MAX) == (1, 5)

    @pytest.mark.parametrize("v,want", [("3", 3), (" 4 ", 4), ("\t5\n", 5), (Decimal("2"), 2)])
    def test_数字字符串解析成整数(self, v, want):
        """`attrs` 是 TEXT，库里 `level` 存成 `"3"` 的行比存成 3 的还多。"""
        assert as_level(v) == want

    @pytest.mark.parametrize("bad", OUT_OF_RANGE)
    def test_越界取None而不是夹到边界(self, bad):
        """夹取会凭空造出结论：把 9 当 L5 = 「已达标」，把 -1 当 L1 = 「入门了」。"""
        assert as_level(bad) is None

    @pytest.mark.parametrize("bad", UNPARSEABLE)
    def test_不可解析取None且不抛错(self, bad):
        assert as_level(bad) is None

    @pytest.mark.parametrize("b", [True, False])
    def test_布尔单独挡掉(self, b):
        """bool 是 int 的子类，不特判的话 True 会被当成 L1 混进算分。"""
        assert as_level(b) is None

    @pytest.mark.known_bug
    @pytest.mark.xfail(
        strict=True,
        reason="BUG-8：整数值浮点档位（3.0 / Decimal('3.0')）被当成「没有要求档」，"
               "整个岗位静默变成 no_baseline、分数变 null。",
    )
    @pytest.mark.parametrize("f", [3.0, Decimal("3.0"), "3.0", 1.0, 5.0])
    def test_整数值的浮点档位该收成整数(self, f):
        """`int(str(3.0))` = `int("3.0")` → ValueError → None，于是 3.0 被判成缺失。

        为什么该修而不是存档：psycopg 把 numeric 列读成 `Decimal` 是常态，
        JSON 里 `"level": 3.0` 也完全合法。一旦命中，这个岗位的所有技能都成了
        「无基准」⇒ `match_score` 变 null、页面显示「该岗位尚未配置能力要求」，
        而库里明明配了 —— **静默**给出错结论，比抛异常难查得多。

        口径：值等于整数的浮点/Decimal/数字串收成该整数（`3.0 → 3`），
        真小数仍取 None（见下一条）。修法一行：先 `Decimal(str(v))` 再判 `== int(...)`。
        """
        assert as_level(f) == int(float(f))

    @pytest.mark.parametrize("f", [3.7, 0.5, Decimal("3.5"), "2.5"])
    def test_真小数档位取None(self, f):
        """档位是枚举，2.5 档不存在 —— 这条与上一条的修复方向不冲突，先钉住。"""
        assert as_level(f) is None

    def test_任何输入都不抛错(self):
        for v in [*OUT_OF_RANGE, *UNPARSEABLE, True, 3.0, float("nan"), float("inf"), 10**30]:
            as_level(v)


# ── 读侧脏值解析：config.as_weight（定案 2026-08）─────────────
#
# 旧实现两边不一致：报告侧 `float(w or 0)` 遇 `"abc"` 抛 ValueError 打死整份报告，
# 画像侧 `isinstance(w, (int, float))` 把 `"0.5"` 判成 0.0。两边都不对，
# 而且同一条脏数据在两个页面上给出两个匹配度。


class TestAsWeight:
    @pytest.mark.parametrize("w", [0.5, 1.0, 0.05, 2.0, 1e-6])
    def test_正常数值原样返回(self, w):
        assert as_weight(w) == w

    @pytest.mark.parametrize(
        "v,want",
        [("0.5", 0.5), (" 0.25 ", 0.25), ("1", 1.0), ("1e-3", 0.001), (Decimal("0.5"), 0.5)],
    )
    def test_数字字符串要解析成数值(self, v, want):
        """定案二的要点：JSON / TEXT 里权重写成字符串很常见，该解析就解析。

        旧的画像侧把 `"0.5"` 判成 0.0，等于悄悄把这项技能踢出分母 —— 比抛错更难查。
        """
        assert as_weight(v) == pytest.approx(want)

    @pytest.mark.parametrize("bad", ["abc", "", "   ", "0.5kg", [], {}, None, object()])
    def test_真正的脏值取零且不抛错(self, bad):
        assert as_weight(bad) == 0.0

    @pytest.mark.parametrize("b", [True, False])
    def test_布尔取零(self, b):
        assert as_weight(b) == 0.0

    @pytest.mark.parametrize("v", [float("nan"), float("inf"), -float("inf"), "nan", "inf"])
    def test_非有限数取零(self, v):
        """nan 参与加权会把匹配度污染成 nan，前端显示成空白，且 nan != nan 到处炸。"""
        got = as_weight(v)
        assert got == 0.0 and math.isfinite(got)

    @pytest.mark.parametrize("v", [-1, -0.5, "-0.5", Decimal("-2")])
    def test_负权重取零(self, v):
        """权重是占比，负权重能把加权分算成负数（页面上出现「匹配度 -33%」）。"""
        assert as_weight(v) == 0.0

    def test_零就是零(self):
        assert as_weight(0) == 0.0 and as_weight("0") == 0.0

    def test_返回值恒为非负有限float(self):
        for v in [0.5, "0.5", "abc", None, True, [], {}, float("nan"), float("inf"), -3, "1e400"]:
            w = as_weight(v)
            assert isinstance(w, float) and math.isfinite(w) and w >= 0.0, v


# ── 「算不出分」的五种原因：config.weighted_score ─────────────


class TestWeightedScore:
    OK = {"skill_total": 2, "scorable_total": 2, "total_w": 1.0, "got_w": 0.5, "has_evidence": True}

    def test_状态词表(self):
        assert SCORE_STATUSES == (
            "ok", "partial_baseline", "no_skills", "no_baseline", "no_weight", "no_evidence"
        )

    def test_正常算加权达标率并保留一位(self):
        assert weighted_score(**self.OK) == (50.0, "ok")
        assert weighted_score(**{**self.OK, "got_w": 1 / 3}) == (33.3, "ok")

    def test_只判算不算得出来不判够不够可信(self):
        """`partial_baseline` 要看基准缺口占多少权重，那个占比只有调用方算得出来。

        所以 `weighted_score` 永远不会返回它 —— 降级由调用方紧接着调
        `degrade_for_baseline_gap` 完成。分成两步是有意的，不要在这里合并。
        """
        assert weighted_score(**self.OK)[1] == "ok"

    def test_岗位没有技能构成(self):
        assert weighted_score(
            skill_total=0, scorable_total=0, total_w=0, got_w=0, has_evidence=False
        ) == (0.0, "no_skills")

    def test_有技能但一项要求档都没配(self):
        assert weighted_score(
            skill_total=5, scorable_total=0, total_w=0, got_w=0, has_evidence=False
        ) == (None, "no_baseline")

    def test_可评分技能权重全为零(self):
        assert weighted_score(**{**self.OK, "total_w": 0.0, "got_w": 0.0}) == (0.0, "no_weight")

    def test_有基准有权重但一项证据都没有(self):
        assert weighted_score(**{**self.OK, "got_w": 0.0, "has_evidence": False}) == (
            0.0,
            "no_evidence",
        )

    def test_只有无基准这一种返回None(self):
        """0% 会被学员读成「完全不匹配」；「岗位没配能力要求」必须是 null 不是 0。"""
        cases = [
            dict(skill_total=0, scorable_total=0, total_w=0, got_w=0, has_evidence=False),
            dict(skill_total=2, scorable_total=0, total_w=0, got_w=0, has_evidence=False),
            {**self.OK, "total_w": 0.0, "got_w": 0.0},
            {**self.OK, "got_w": 0.0, "has_evidence": False},
            self.OK,
        ]
        nones = {st for score, st in map(lambda kw: weighted_score(**kw), cases) if score is None}
        assert nones == {"no_baseline"}

    @pytest.mark.parametrize(
        "kw",
        [
            dict(skill_total=0, scorable_total=0, total_w=0, got_w=0, has_evidence=False),
            dict(skill_total=3, scorable_total=0, total_w=0, got_w=0, has_evidence=True),
            dict(skill_total=3, scorable_total=3, total_w=1.0, got_w=1.0, has_evidence=True),
            dict(skill_total=3, scorable_total=1, total_w=0.1, got_w=0.1, has_evidence=True),
        ],
    )
    def test_状态一定在词表里且分数不越界(self, kw):
        score, status = weighted_score(**kw)
        assert status in SCORE_STATUSES
        assert score is None or 0.0 <= score <= 100.0

    def test_契约里的取值与这里的词表同源(self):
        """前端按 score_status 决定显示数字还是文案，多一个少一个值它就漏处理。"""
        import typing

        from backend.api.schemas_assessment import AssessmentReportOut

        ann = AssessmentReportOut.model_fields["score_status"].annotation
        assert typing.get_args(ann) == SCORE_STATUSES


# ── 配置不全时服务端主动降级：partial_baseline（定案 2026-08）──
#
# 「全缺」与「全配齐」之间还有一大片中间态：某岗位 82% 的权重没配要求档、只有 18%
# 配了，学员那 18% 全达标 ⇒ 分数 50% + 状态 ok，读起来是「我匹配一半」，
# 真相是「这岗位大部分要求没人填」。所以缺口过阈值时分数照给（那 18% 是真实依据），
# 但状态降级成 partial_baseline。阈值必须留在服务端：放前端就是每页各定一次的魔数。


class TestPartialBaselineThreshold:
    def test_阈值是三成且留在服务端(self):
        assert PARTIAL_BASELINE_PCT == 30.0

    @pytest.mark.parametrize("pct", [30.1, 31.0, 50.0, 82.4, 100.0])
    def test_超过阈值就算配置不全(self, pct):
        assert is_partial_baseline(pct) is True

    @pytest.mark.parametrize("pct", [0.0, 1.0, 15.0, 29.9, 30.0])
    def test_不超过阈值不算(self, pct):
        assert is_partial_baseline(pct) is False

    def test_正好三成不算超过(self):
        """严格大于。边界口径必须与契约描述的「超过 30%」字面一致，否则

        会出现响应里写着 `no_baseline_weight=30.0`、状态却是 partial_baseline
        的自相矛盾，前端没法向学员解释。
        """
        assert is_partial_baseline(PARTIAL_BASELINE_PCT) is False
        assert is_partial_baseline(PARTIAL_BASELINE_PCT + 0.1) is True

    def test_历史数据缺字段时不降级(self):
        """None = 「不知道缺多少」。宁可少提示，不要凭空给一个没有依据的警告。"""
        assert is_partial_baseline(None) is False
        assert degrade_for_baseline_gap("ok", None) == "ok"

    def test_只降级ok(self):
        """另外四种本来就在说「这个数不能当结论」，再套一层反而模糊焦点。"""
        assert degrade_for_baseline_gap("ok", 60.0) == "partial_baseline"
        for st in ("no_skills", "no_baseline", "no_weight", "no_evidence"):
            assert degrade_for_baseline_gap(st, 60.0) == st, f"{st} 不该被降级盖掉"

    def test_无基准是本档的极端情形但保留自己的值(self):
        """缺口 100% 时连分数都没有，说「仅供参考」不如说「算不出来」。"""
        assert degrade_for_baseline_gap("no_baseline", 100.0) == "no_baseline"

    def test_降级结果一定在词表里(self):
        for st in SCORE_STATUSES:
            for pct in (None, 0.0, 30.0, 30.1, 100.0):
                assert degrade_for_baseline_gap(st, pct) in SCORE_STATUSES

    def test_不认识的状态原样返回(self):
        """历史报告可能带着旧值，降级函数不能把它改成别的东西。"""
        assert degrade_for_baseline_gap("未来某个状态", 99.0) == "未来某个状态"
