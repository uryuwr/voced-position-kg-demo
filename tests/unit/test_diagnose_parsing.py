"""backend/agent/diagnose.py 的纯函数：模型输出解析 + 技能归一化。

这两个函数是「模型说什么都不能把接口打挂」的最后一道：模型会返回带解释文字的
JSON、代码围栏、越界档位、字符串分数。解析失败要能降级，不能冒泡成 500。
"""
from __future__ import annotations

import pytest

from backend.agent.diagnose import _extract_json_array, _normalize_skills
from backend.agent.skill_keywords import FALLBACK_SKILL_NAME


class TestExtractJsonArray:
    def test_裸数组(self):
        assert _extract_json_array('[{"skill_name":"A","level":3}]') == [{"skill_name": "A", "level": 3}]

    def test_代码围栏(self):
        raw = '```json\n[{"skill_name":"A"}]\n```'
        assert _extract_json_array(raw) == [{"skill_name": "A"}]

    def test_无语言标记的围栏(self):
        assert _extract_json_array('```\n[{"skill_name":"A"}]\n```') == [{"skill_name": "A"}]

    def test_前后有解释文字(self):
        raw = '好的，分析如下：\n[{"skill_name":"A"}]\n以上是我的评估。'
        assert _extract_json_array(raw) == [{"skill_name": "A"}]

    def test_取最外层的中括号(self):
        raw = '[{"skill_name":"A","tags":["x","y"]}]'
        assert _extract_json_array(raw)[0]["tags"] == ["x", "y"]

    def test_对象里包着skills数组(self):
        assert _extract_json_array('{"skills":[{"skill_name":"A"}]}') == [{"skill_name": "A"}]

    def test_数组里的非字典元素被丢掉(self):
        assert _extract_json_array('[{"skill_name":"A"}, "噪声", 1, null]') == [{"skill_name": "A"}]

    @pytest.mark.parametrize("raw", ["", None, "完全不是 JSON", "{}", '{"foo":1}'])
    def test_解析不出时返回None交给调用方降级(self, raw):
        assert _extract_json_array(raw) is None

    def test_空数组是合法结果(self):
        assert _extract_json_array("[]") == []

    def test_坏JSON返回None而不是抛错(self):
        assert _extract_json_array('[{"skill_name":') is None


class TestNormalizeSkills:
    def test_标准形状(self):
        got = _normalize_skills([{"skill_name": "配料准备", "level": 3, "score": 60, "evidence": "简历第三段"}])
        assert got == [{"skill_name": "配料准备", "level": 3, "score": 60, "evidence": "简历第三段"}]

    @pytest.mark.parametrize("key", ["skill_name", "skill_key", "name"])
    def test_三种技能名字段都认(self, key):
        assert _normalize_skills([{key: "A", "level": 2}])[0]["skill_name"] == "A"

    def test_技能名去空白(self):
        assert _normalize_skills([{"skill_name": "  A  ", "level": 2}])[0]["skill_name"] == "A"

    def test_没有技能名的条目被丢掉(self):
        got = _normalize_skills([{"level": 3}, {"skill_name": ""}, {"skill_name": "A", "level": 2}])
        assert [s["skill_name"] for s in got] == ["A"]

    @pytest.mark.parametrize("raw,expect", [(-3, 1), (6, 5), (99, 5), (1, 1), (5, 5)])
    def test_档位夹到一到五(self, raw, expect):
        """模型给 -3 或 99 都不能穿到下游 —— base_score() 遇到越界档位会抛 KeyError。"""
        assert _normalize_skills([{"skill_name": "A", "level": raw}])[0]["level"] == expect

    def test_档位为零按未给出处理(self):
        """`level or required_level or 2`：0 是假值，走默认 2（掌握），不是夹成 1。"""
        assert _normalize_skills([{"skill_name": "A", "level": 0}])[0]["level"] == 2

    @pytest.mark.parametrize("bad", ["三级", None, "", [], {}, "abc"])
    def test_档位坏值回落到掌握(self, bad):
        assert _normalize_skills([{"skill_name": "A", "level": bad}])[0]["level"] == 2

    def test_数字字符串档位可用(self):
        assert _normalize_skills([{"skill_name": "A", "level": "4"}])[0]["level"] == 4

    def test_没给档位时按required_level(self):
        assert _normalize_skills([{"skill_name": "A", "required_level": 4}])[0]["level"] == 4

    @pytest.mark.parametrize("raw,expect", [(-10, 0), (0, 40), (150, 100), (70, 70)])
    def test_分数夹到零到一百(self, raw, expect):
        # score=0 是假值，会回落到 level*20；这里 level 默认 2 → 40
        assert _normalize_skills([{"skill_name": "A", "score": raw}])[0]["score"] == expect

    def test_没给分数时按档位推(self):
        assert _normalize_skills([{"skill_name": "A", "level": 3}])[0]["score"] == 60

    @pytest.mark.parametrize("bad", ["高", None, [], {}])
    def test_分数坏值回落到档位推算(self, bad):
        assert _normalize_skills([{"skill_name": "A", "level": 3, "score": bad}])[0]["score"] == 60

    def test_依据字段截断到五百字(self):
        assert len(_normalize_skills([{"skill_name": "A", "evidence": "长" * 900}])[0]["evidence"]) == 500

    def test_依据可来自reason字段(self):
        assert _normalize_skills([{"skill_name": "A", "reason": "因为"}])[0]["evidence"] == "因为"

    def test_没有依据时标注来源(self):
        assert _normalize_skills([{"skill_name": "A"}])[0]["evidence"] == "agent"

    def test_全被过滤掉时给保底条目而不是空列表(self, no_kg_recall):
        """空技能列表会让下游报告变成一片空白；规则兜底至少给一条。"""
        got = _normalize_skills([{"level": 3}, {"skill_name": "   "}])
        assert [s["skill_name"] for s in got] == [FALLBACK_SKILL_NAME]

    def test_输入为空时同样给保底条目(self, no_kg_recall):
        assert [s["skill_name"] for s in _normalize_skills([])] == [FALLBACK_SKILL_NAME]
