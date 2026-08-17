"""backend/userprofile/sync.py::build_text —— 提交给画像平台的自然语言。

最关键的一条：**只写实测到的技能**。没测过不是证据，写进去等于让平台把「没测」
记成「不会」，而这条记忆之后会被别的岗位当推断依据用，错一次会一直错下去。
"""
from __future__ import annotations

import pytest

from backend.userprofile.sync import _LEVEL_WORD, build_text


def item(key, *, tested=True, measured=3, required=3, ok=True):
    return {
        "skill_key": key,
        "tested": tested,
        "measured_level": measured,
        "required_level": required,
        "ok": ok,
    }


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
