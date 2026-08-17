"""backend/kg/pg_store/ 的两类守卫：可见性 SQL 片段 + attrs 写入校验。

「一条脏数据不能打死一整页」——这个项目栽过三次，都是同一个形状。读侧靠
`attrs_level_int` 把脏值取成 NULL，写侧靠 `_assert_attrs_sane` 当场拒绝；
采集脚本和直连改库绕得过应用层，**两侧都要站得住**。
"""
from __future__ import annotations

import re

import pytest

from backend.kg.pg_store.config import (
    ADMIN_STATUSES,
    ARCHIVED_STATUS,
    PUBLIC_STATUSES,
    attrs_level_int,
    edge_not_archived,
    edge_published,
    node_not_archived,
    node_published,
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
