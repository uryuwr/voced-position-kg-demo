"""backend/kg/provenance.py —— id 生成与溯源字段。

id 必须**幂等**：同一条源数据反复入库要落到同一个 id，否则 ON CONFLICT 失效、
图里出现重复节点，边也跟着指错。所以 id 不能带时间戳/随机数。
"""
from __future__ import annotations

import re

import pytest

from backend.kg.provenance import base_provenance, make_edge_id, make_node_id, utc_now_iso


class TestMakeNodeId:
    def test_四段式(self):
        assert make_node_id("CN", "occupation", "mohrss", "6-31-01-05") == \
            "CN:occupation:mohrss:6-31-01-05"

    def test_幂等(self):
        a = make_node_id("CN", "skill_level", "mohrss", "S-001")
        b = make_node_id("CN", "skill_level", "mohrss", "S-001")
        assert a == b, "id 不能含时间戳或随机数，否则重复入库会产生重复节点"

    def test_空格转下划线(self):
        assert make_node_id("US", "skill_level", "onet", "data analysis") == \
            "US:skill_level:onet:data_analysis"

    def test_斜杠转下划线(self):
        """source_id 里的 / 会让 id 看起来像路径，也会破坏按冒号分段的解析。"""
        assert make_node_id("CN", "major", "moe", "560/301") == "CN:major:moe:560_301"

    def test_同时含空格与斜杠(self):
        assert make_node_id("CN", "major", "moe", "a b/c d") == "CN:major:moe:a_b_c_d"

    @pytest.mark.parametrize("sid", [123, 0, 4.5, None, True])
    def test_非字符串源id也能生成(self, sid):
        assert make_node_id("CN", "t", "s", sid).endswith(str(sid))

    def test_空源id不炸(self):
        assert make_node_id("CN", "t", "s", "") == "CN:t:s:"

    def test_中文源id原样保留(self):
        assert make_node_id("CN", "skill_level", "manual", "配料准备") == \
            "CN:skill_level:manual:配料准备"


class TestMakeEdgeId:
    def test_三段式带前缀(self):
        eid = make_edge_id("CN:occupation:mohrss:X", "requires", "CN:skill_level:mohrss:Y")
        assert eid == "edge:CN:occupation:mohrss:X|requires|CN:skill_level:mohrss:Y"

    def test_幂等(self):
        args = ("A", "requires", "B")
        assert make_edge_id(*args) == make_edge_id(*args)

    def test_方向敏感(self):
        assert make_edge_id("A", "r", "B") != make_edge_id("B", "r", "A")

    def test_关系类型参与id(self):
        assert make_edge_id("A", "requires", "B") != make_edge_id("A", "covers", "B")

    def test_竖线做分隔符不与节点id冲突(self):
        """节点 id 用冒号分段，边 id 用竖线分段，两者不会互相吃掉。"""
        eid = make_edge_id("CN:a:b:c", "rel", "CN:d:e:f")
        assert eid.count("|") == 2


class TestProvenance:
    def test_六个溯源字段齐全(self):
        p = base_provenance(
            source_system="MOHRSS",
            source_id="6-31-01-05",
            source_url="https://example.invalid/x",
            license="internal",
            confidence="official",
        )
        assert set(p) == {
            "source_system", "source_id", "source_url", "license", "fetched_at", "confidence"
        }

    def test_源id统一成字符串(self):
        assert base_provenance(
            source_system="S", source_id=123, source_url="u", license="l", confidence="c"
        )["source_id"] == "123"

    def test_不传抓取时间时用当前UTC(self):
        p = base_provenance(
            source_system="S", source_id="1", source_url="u", license="l", confidence="c"
        )
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", p["fetched_at"])

    def test_显式抓取时间原样透传(self):
        p = base_provenance(
            source_system="S", source_id="1", source_url="u", license="l",
            confidence="c", fetched_at="2020-01-01T00:00:00+00:00",
        )
        assert p["fetched_at"] == "2020-01-01T00:00:00+00:00"

    def test_时间戳是不带微秒的UTC(self):
        now = utc_now_iso()
        assert now.endswith("+00:00")
        assert "." not in now, "带微秒会让同一条数据两次入库的 fetched_at 不同"
