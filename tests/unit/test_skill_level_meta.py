"""backend/kg/pg_store/skill_level_meta.py —— BR-01：档位定义的唯一真源。

档位名称/基准分/行为锚**只能**从这里读，禁止在业务代码或前端硬编码。所以这张表
本身就是契约：改动它等于改产品口径，必须是有意的。
"""
from __future__ import annotations

import pytest

from backend.kg.pg_store.skill_level_meta import (
    REQUIRED_LEVEL_CODES,
    SKILL_LEVEL_META,
    base_score,
    behavior_map,
    code_to_name,
    label_map,
    level_code,
    level_name,
    skill_level_meta,
)


class TestTable:
    def test_五档(self):
        assert [x["level"] for x in SKILL_LEVEL_META] == [1, 2, 3, 4, 5]

    def test_产品档名称(self):
        assert [x["name"] for x in SKILL_LEVEL_META] == ["了解", "掌握", "熟练", "精通", "专家"]

    def test_基准分等差二十(self):
        assert [x["base_score"] for x in SKILL_LEVEL_META] == [20, 40, 60, 80, 100]

    def test_代码与档位对应(self):
        assert REQUIRED_LEVEL_CODES == ("L1", "L2", "L3", "L4", "L5")

    def test_每档都有行为锚(self):
        """行为锚是 SJT 选项文案的来源，缺一条降级题就出不出来。"""
        assert all(len(x["behavior"]) >= 10 for x in SKILL_LEVEL_META)

    def test_对外列表字段齐全(self):
        rows = skill_level_meta()
        assert len(rows) == 5
        assert set(rows[0]) == {"level", "name", "base_score", "code", "behavior"}


class TestLookups:
    def test_映射表(self):
        assert label_map() == {1: "了解", 2: "掌握", 3: "熟练", 4: "精通", 5: "专家"}
        assert set(behavior_map()) == {1, 2, 3, 4, 5}
        assert code_to_name()["L5"] == "专家"

    def test_基准分查询(self):
        assert base_score(3) == 60
        assert base_score("3") == 60
        assert base_score("L3") == 60
        assert base_score("l3") == 60

    def test_档位名查询(self):
        assert level_name(1) == "了解"
        assert level_name("L5") == "专家"

    def test_档位代码归一(self):
        assert level_code(3) == "L3"
        assert level_code("L3") == "L3"
        assert level_code("3") == "L3"

    @pytest.mark.parametrize("bad", [0, 6, 9, -1])
    def test_越界档位抛KeyError(self, bad):
        """调用方必须先把档位夹到 1–5；见 tests/unit/test_known_bugs.py。"""
        with pytest.raises(KeyError):
            base_score(bad)
        with pytest.raises(KeyError):
            level_name(bad)

    @pytest.mark.parametrize("bad", ["", "三级", None, "L", "abc"])
    def test_非数值档位抛ValueError或TypeError(self, bad):
        with pytest.raises((ValueError, TypeError)):
            base_score(bad)


class TestNoHardcodeElsewhere:
    def test_档位词表在画像同步侧保持同源(self):
        from backend.userprofile.sync import _LEVEL_WORD

        assert _LEVEL_WORD == label_map(), "sync.py 里那份档位词表必须跟真源一致"
