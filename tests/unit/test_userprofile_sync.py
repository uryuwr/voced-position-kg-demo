"""backend/userprofile/sync.py::build_text —— 提交给画像平台的自然语言。

两条最关键的：

- **只写实测到的技能**。没测过不是证据，写进去等于让平台把「没测」记成「不会」，
  而这条记忆之后会被别的岗位当推断依据用，错一次会一直错下去。
- **技能位只写展示名，一个 code 都不许出现**。写进去的文本是平台长期保存的语义
  证据，`SKa1fa1d005d` 对任何推断都是零信息量；且 Idempotency-Key 按 session
  派生，重推覆盖不了。取不到名字宁可丢这一项。
"""
from __future__ import annotations

import pytest

from backend.kg.skill_key import derive_key
from backend.userprofile.sync import _LEVEL_WORD, build_text

_MISSING = object()


def item(name, *, tested=True, measured=3, required=3, ok=True, skill_name=_MISSING):
    """`name` 是技能名，`skill_key` 按它派生成 code 形态（即库里的真实形态）。

    `skill_name=None` 模拟上游没给展示名；传字符串可模拟它回落成了 code。
    """
    d = {
        "skill_key": derive_key(name),
        "tested": tested,
        "measured_level": measured,
        "required_level": required,
        "ok": ok,
    }
    nm = name if skill_name is _MISSING else skill_name
    if nm is not None:
        d["skill_name"] = nm
    return d


REPORT = {
    "match_score": 78.6,
    "items": [
        item("配料准备", measured=3, required=3, ok=True),
        item("搅拌操作", measured=2, required=4, ok=False),
        item("安全防护", tested=False, measured=None, required=5, ok=False),
        item("设备维保", tested=True, measured=None, required=2, ok=False),
    ],
}


class TestOnlyTested:
    def test_未实测的技能一律不写进去(self):
        text = build_text(REPORT, occupation_name="混凝土工")
        assert "配料准备" in text and "搅拌操作" in text
        assert "安全防护" not in text, "没测过不是证据"

    def test_测了但没定出档位的也不写(self):
        text = build_text(REPORT, occupation_name="混凝土工")
        assert "设备维保" not in text

    def test_一项都没实测到时返回空串(self):
        """空串是调用方的信号：push_diagnosis 据此不提交，避免写一条空记忆。"""
        rep = {"match_score": 0, "items": [item("A", tested=False, measured=None)]}
        assert build_text(rep, occupation_name="X") == ""

    @pytest.mark.parametrize("rep", [{}, {"items": []}, {"items": None}])
    def test_报告为空时返回空串(self, rep):
        assert build_text(rep, occupation_name="X") == ""


class TestContent:
    def test_语义自包含带岗位名与匹配度(self):
        text = build_text(REPORT, occupation_name="混凝土工")
        assert "「混凝土工」" in text
        assert "78.6%" in text

    def test_带测评时间且只取日期(self):
        text = build_text(REPORT, occupation_name="混凝土工", when="2026-08-14T10:20:30Z")
        assert "（测评时间 2026-08-14）" in text

    def test_不传时间时不出现时间片段(self):
        assert "测评时间" not in build_text(REPORT, occupation_name="混凝土工")

    def test_解释档位语义让平台能理解几级是什么(self):
        text = build_text(REPORT, occupation_name="X")
        for n, w in _LEVEL_WORD.items():
            assert f"{n} 级表示{w}" in text

    def test_档位词表与技能等级元数据一致(self):
        from backend.kg.pg_store.skill_level_meta import label_map

        assert _LEVEL_WORD == label_map()

    def test_逐项写出实测档与岗位要求档(self):
        text = build_text(REPORT, occupation_name="X")
        assert "配料准备 达到 3 级（熟练），该岗位要求 3 级" in text
        assert "搅拌操作 达到 2 级（掌握），该岗位要求 4 级" in text

    def test_岗位没定要求档时写未定义(self):
        rep = {"match_score": 50, "items": [item("A", measured=5, required=None, ok=True)]}
        assert "该岗位要求 未定义 级" in build_text(rep, occupation_name="X")

    def test_达标与短板分开陈述(self):
        text = build_text(REPORT, occupation_name="X")
        assert "其中 配料准备 已达到该岗位的能力要求。" in text
        assert "搅拌操作 低于岗位要求，是用户当前的主要能力短板。" in text

    def test_全部达标时不提短板(self):
        rep = {"match_score": 100, "items": [item("A", measured=5, required=3, ok=True)]}
        text = build_text(rep, occupation_name="X")
        assert "已达到该岗位的能力要求" in text
        assert "短板" not in text

    def test_全部未达标时不提达标(self):
        rep = {"match_score": 10, "items": [item("A", measured=1, required=5, ok=False)]}
        text = build_text(rep, occupation_name="X")
        assert "短板" in text
        assert "已达到该岗位的能力要求" not in text

    def test_按实测档从高到低排(self):
        rep = {
            "match_score": 60,
            "items": [item("低", measured=1), item("高", measured=5), item("中", measured=3)],
        }
        text = build_text(rep, occupation_name="X")
        assert text.index("高 达到") < text.index("中 达到") < text.index("低 达到")


class TestNoMatchScore:
    """匹配度算不出来时（岗位一项要求档都没配 ⇒ `match_score` 为 None）。

    这段文字会被灌进五维记忆，之后当作**别的岗位**的推断依据用；写进
    「综合能力匹配度 None%」等于让平台记住一条脏事实，错一次会一直错下去。
    """

    NO_SCORE = {"match_score": None, "items": REPORT["items"]}

    def test_不把None拼进文案(self):
        text = build_text(self.NO_SCORE, occupation_name="混凝土工")
        assert "None" not in text and "%" not in text

    def test_改成说明未计算原因(self):
        text = build_text(self.NO_SCORE, occupation_name="混凝土工")
        assert "未配置能力要求档" in text and "未计算匹配度" in text

    def test_逐项实测结论照样写(self):
        """匹配度算不出来不影响「实测到什么档位」——那部分证据是真的。"""
        text = build_text(self.NO_SCORE, occupation_name="混凝土工")
        assert "配料准备 达到 3 级（熟练）" in text
        assert "安全防护" not in text, "未实测的仍然不写"

    def test_一项都没实测到时仍返回空串(self):
        rep = {"match_score": None, "items": [item("A", tested=False, measured=None)]}
        assert build_text(rep, occupation_name="X") == ""

    def test_零分与算不出分是两种文案(self):
        """0.0 是真结论（有基准、只是没证据），None 是「无从算起」，不能混。"""
        zero = build_text({"match_score": 0.0, "items": REPORT["items"]}, occupation_name="X")
        none_ = build_text(self.NO_SCORE, occupation_name="X")
        assert "综合能力匹配度 0.0%" in zero
        assert "综合能力匹配度" not in none_


class TestDisplayNameOnly:
    """技能位写展示名，不写 `skill_key`（2026-08-19 改造后漏改了一天）。"""

    def test_技能位写展示名(self):
        text = build_text(REPORT, occupation_name="混凝土工")
        assert "配料准备 达到 3 级" in text
        assert derive_key("配料准备") not in text

    def test_一个code形态都不出现(self):
        text = build_text(REPORT, occupation_name="混凝土工")
        assert "SK" not in text, "灌进五维记忆的文本里出现了 code"

    def test_没给展示名的项直接丢掉(self):
        rep = {
            "match_score": 50,
            "items": [item("配料准备"), item("搅拌操作", skill_name=None)],
        }
        text = build_text(rep, occupation_name="X")
        assert "配料准备" in text
        assert "搅拌操作" not in text and derive_key("搅拌操作") not in text

    def test_展示名回落成code的项也丢掉(self):
        """上游漏挑字段时 `skill_name` 会等于 `skill_key`（report.py 的 `or key`）。

        只判空是不够的：那一格非空但装着 `SKxxxxxxxxxx`，照写就是脏数据。
        """
        k = derive_key("搅拌操作")
        rep = {"match_score": 50, "items": [item("配料准备"), item("搅拌操作", skill_name=k)]}
        text = build_text(rep, occupation_name="X")
        assert k not in text and "SK" not in text
        assert "配料准备" in text, "只该丢有问题的那一项"

    def test_纯英文技能名不被当成code丢掉(self):
        """判据必须是 `is_generated`，不能是 `is_valid_key`。

        后者是 `^[A-Za-z0-9]{2,64}$`，`Python`/`SQL`/`Excel` 全都为真 ——
        用它当判据会把一整批正常技能静默丢掉。
        """
        rep = {
            "match_score": 80,
            "items": [item("Python"), item("SQL"), item("Excel")],
        }
        text = build_text(rep, occupation_name="数据分析师")
        for nm in ("Python", "SQL", "Excel"):
            assert f"{nm} 达到" in text

    def test_全部取不到展示名时返回空串(self):
        rep = {"match_score": 50, "items": [item("A", skill_name=None)]}
        assert build_text(rep, occupation_name="X") == ""

    def test_达标与短板两句也用展示名(self):
        text = build_text(REPORT, occupation_name="X")
        assert "其中 配料准备 已达到" in text
        assert "搅拌操作 低于岗位要求" in text


class TestPushSkipped:
    """整批被丢时不能静默 —— 那是上游 bug，要在 biz_event 里看得见。"""

    def test_丢弃条数与原因都透出(self):
        from backend.userprofile.sync import push_diagnosis

        rep = {
            "match_score": 50,
            "items": [item("A", skill_name=None), item("B", skill_name=None)],
        }
        out = push_diagnosis("u1", 1, rep, occupation_name="X")
        assert out["ok"] is False
        assert out["dropped_no_name"] == 2
        assert "取不到展示名" in out["error"]

    def test_一项都没实测到时不报成取不到名字(self):
        from backend.userprofile.sync import push_diagnosis

        rep = {"match_score": 0, "items": [item("A", tested=False, measured=None)]}
        out = push_diagnosis("u1", 2, rep, occupation_name="X")
        assert "dropped_no_name" not in out
        assert "没有实测到的技能" in out["error"]


class TestFormat:
    def test_是纯自然语言不嵌JSON(self):
        """按《text 内容规范》：五维归类、拆分事实、生成标签都由平台负责。"""
        text = build_text(REPORT, occupation_name="混凝土工")
        for token in ("{", "}", "[", "]", "skill_key", "match_score"):
            assert token not in text

    def test_截断到四千字(self):
        rep = {
            "match_score": 1,
            "items": [item(f"技能{i}", measured=3, required=3) for i in range(300)],
        }
        assert len(build_text(rep, occupation_name="Z")) == 4000

    def test_不改动传入的报告(self):
        rep = {"match_score": 1, "items": [item("低", measured=1), item("高", measured=5)]}
        before = [i["skill_key"] for i in rep["items"]]
        build_text(rep, occupation_name="X")
        assert [i["skill_key"] for i in rep["items"]] == before, "排序不该就地改调用方的数据"
