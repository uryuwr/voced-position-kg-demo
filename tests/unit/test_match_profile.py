"""backend/kg/pg_store/biz_store.py::match_with_profile —— 岗位匹配度（纯函数）。

三条产品口径：
- `user_level = 0` 表示**无证据**，不是「水平为零」；调用方靠 covered/coverage 显示「未评估」
- 单项达标率 `min(实测/要求, 1)` 封顶 1.0，超额完成不能把总分顶上去
- 技能名退化匹配有两道闸（最短长度 + 重合比例），拦住「运维」命中「设备运维管理」
"""
from __future__ import annotations

import pytest

from backend.kg.pg_store.biz_store import (
    FUZZY_MIN_LEN,
    FUZZY_MIN_RATIO,
    match_with_profile,
)

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
        assert r["covered_count"] == 0
        assert r["coverage"] == 0.0
        assert all(i["user_level"] == 0 and i["matched_by"] is None for i in r["items"])

    def test_覆盖率与匹配度分开表达(self):
        """一项没命中时 score 必然是 0，但那是「没有证据」而非「完全不匹配」。"""
        r = match_with_profile(OCC, REQUIRED, {"配料准备": 3})
        assert r["covered_count"] == 1
        assert r["coverage"] == pytest.approx(50.0)      # 命中项占总权重 50%
        assert r["match_score"] == pytest.approx(50.0)

    def test_没有技能要求时不除零(self):
        r = match_with_profile(OCC, [], {"任意": 5})
        assert r["match_score"] == 0.0
        assert r["coverage"] == 0.0
        assert r["skill_total"] == 0


class TestRatioCap:
    def test_超额完成封顶(self):
        r = match_with_profile(OCC, one(level=2), {"故障诊断": 5})
        assert r["items"][0]["ratio"] == 1.0
        assert r["match_score"] == 100.0

    def test_按比例折算并保留三位(self):
        r = match_with_profile(OCC, one(level=3), {"故障诊断": 1})
        assert r["items"][0]["ratio"] == pytest.approx(0.333)

    def test_没有要求档时有证据即达标(self):
        r = match_with_profile(OCC, one(level=0), {"故障诊断": 1})
        assert r["items"][0]["ratio"] == 1.0 and r["match_score"] == 100.0

    def test_没有要求档且无证据时为零(self):
        r = match_with_profile(OCC, one(level=0), {})
        assert r["items"][0]["ratio"] == 0.0 and r["match_score"] == 0.0

    def test_达标判定是比例达一(self):
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
    def test_权重是字符串时按零处理而不是抛错(self):
        """weight 来自 kg_edge，脏值不能打死整页岗位列表。"""
        r = match_with_profile(OCC, one(weight="0.5"), {"故障诊断": 3})
        assert r["items"][0]["weight"] == 0.0
        assert r["match_score"] == 0.0

    @pytest.mark.parametrize("bad", [None, "", "abc", [], {}])
    def test_各种非数值权重都不炸(self, bad):
        r = match_with_profile(OCC, one(weight=bad), {"故障诊断": 3})
        assert r["items"][0]["weight"] == 0.0

    def test_技能键缺失时按空串处理(self):
        r = match_with_profile(
            OCC, [{"category": "c", "required_level": 3, "weight": 1.0}], {"任意技能": 3}
        )
        assert r["items"][0]["skill_key"] is None
        assert r["items"][0]["user_level"] == 0


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
