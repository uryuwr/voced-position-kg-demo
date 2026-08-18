"""国标刻度归一（`backend/kg/level_scale.py`）——入库期唯一的等级转换。

这个模块是 2026-08-18 那次「回填凭空消失」的补丁。事故形状：8-14 手工把 8919 个
节点补上产品档（可评分岗位 117 → 608），8-18 有人重灌了一次库，
`pg_store/migrate.py` 的 `attrs = EXCLUDED.attrs` 无条件覆盖，数字**逐位退回**，
全程零报错。修法是把归一挂到入库的必经之路上，而不是靠谁记得跑一次性脚本。

所以这里锁三件事：
1. **方向**：国标一级最高、产品档五级最高，反的。搞反了不崩、不报错，
   只会把高级技师判成入门，分数还挺像样。
2. **幂等**：归一后的节点再过一次必须原样不动——它每次灌库都会被执行。
3. **非国标刻度不许碰**：ONET 的 IM1–IM5 是重要性刻度，套国标表是错的。
"""
from __future__ import annotations

import json

import pytest

from backend.kg.level_scale import (
    CODE_TO_LEVEL,
    ZH_TO_LEVEL,
    normalize_nodes,
    normalize_skill_level_node,
    product_level,
    skill_key_of,
)


def old_shape_node(
    *, level_zh: str = "三级/高级工", level_code: str = "L3", skill: str = "工件加工"
) -> dict:
    """2026-08-11 之前采集端产出的形态：只有国标原码/原名，没有产品档。

    库里那 8919 个节点就长这样，`migrate --clear` 会把它们原样灌回来。
    """
    name = f"{skill} · {level_zh}"
    return {
        "id": f"CN:skill_level:MOHRSS_CN:6-18-01-05|{skill}|{level_code}",
        "region": "CN",
        "type": "skill_level",
        "name": name,
        "name_zh": name,
        "description": f"镗工（6-18-01-05）· {skill} · {level_zh} · 权重20%",
        "attrs": {
            "skill_name": skill,
            "level_zh": level_zh,
            "level_code": level_code,
            "scale": "cn_skill_grade",
            "occupation_code": "6-18-01-05",
        },
    }


class Test方向:
    """国标刻度是反的——这是全模块唯一真正危险的地方。"""

    @pytest.mark.parametrize(
        "zh, code, expect",
        [
            ("一级/高级技师", "L1", 5),   # 最高档 → 专家
            ("二级/技师", "L2", 4),
            ("三级/高级工", "L3", 3),
            ("四级/中级工", "L4", 2),
            ("五级/初级工", "L5", 1),     # 最低档 → 了解
            ("初级", "T1", 1),            # 专业技术三级制铺到 1/3/5
            ("中级", "T2", 3),
            ("高级", "T3", 5),
        ],
    )
    def test_八个国标档位逐一对到产品档(self, zh, code, expect):
        assert product_level(zh, code) == expect

    def test_国标一级不是产品一档(self):
        """写死这条：`L1 → 1` 是最容易犯、后果最重的错——高级技师被判成入门。"""
        assert product_level("一级/高级技师", "L1") == 5
        assert product_level("五级/初级工", "L5") == 1

    def test_等级名优先于原码(self):
        """等级名是人写的规范值，比原码可信；两者矛盾时以等级名为准。"""
        assert product_level("一级/高级技师", "L5") == 5

    def test_两张表方向一致(self):
        """ZH 表与 CODE 表是同一套刻度的两种写法，漂移了就会按走哪条路给不同答案。"""
        pairs = [("一级/高级技师", "L1"), ("二级/技师", "L2"), ("三级/高级工", "L3"),
                 ("四级/中级工", "L4"), ("五级/初级工", "L5"),
                 ("初级", "T1"), ("中级", "T2"), ("高级", "T3")]
        for zh, code in pairs:
            assert ZH_TO_LEVEL[zh] == CODE_TO_LEVEL[code], f"{zh} 与 {code} 不一致"

    def test_高级不会被高级工误伤(self):
        """子串匹配会让「高级」吃掉「高级工」「高级技师」，一个字差两到三档。"""
        assert product_level("高级", None) == 5          # 专业技术高级
        assert product_level("三级/高级工", None) == 3    # 技能三级
        assert product_level("一级/高级技师", None) == 5


class Test归一:
    def test_老形态节点被补成产品档(self):
        n = old_shape_node()
        assert normalize_skill_level_node(n) == "ok"
        a = n["attrs"]
        assert a["level"] == 3
        assert a["source_level_code"] == "L3"

    def test_国标文案被剥掉(self):
        """国标等级名与产品档数字方向相反（国标四级 = 产品 L2），同屏必然打架。"""
        n = old_shape_node(level_zh="四级/中级工", level_code="L4")
        normalize_skill_level_node(n)
        assert "level_zh" not in n["attrs"]
        assert "level_code" not in n["attrs"]
        assert n["name"] == "工件加工 · L2"
        assert n["name_zh"] == n["name"]
        assert "四级/中级工" not in n["description"]
        assert "· L2" in n["description"]

    def test_描述里的国标正文不被误伤(self):
        """只替换被「·」包住或位于串尾的等级词，正文里的「初级工应能…」要留着。"""
        n = old_shape_node()
        n["description"] = "本职业要求初级工应能独立完成 · 三级/高级工"
        normalize_skill_level_node(n)
        assert "初级工应能独立完成" in n["description"]

    def test_幂等(self):
        """每次灌库都会跑，第二遍必须原样不动——否则 name 会被反复截断。"""
        n = old_shape_node()
        assert normalize_skill_level_node(n) == "ok"
        snapshot = json.dumps(n, ensure_ascii=False, sort_keys=True)
        assert normalize_skill_level_node(n) == "skip"
        assert json.dumps(n, ensure_ascii=False, sort_keys=True) == snapshot

    def test_attrs_是_JSON_串时按串写回(self):
        """SQLite / PG 读出来的 attrs 是串，采集端在内存里造的是 dict，两种都要吃。"""
        n = old_shape_node()
        n["attrs"] = json.dumps(n["attrs"], ensure_ascii=False)
        assert normalize_skill_level_node(n) == "ok"
        assert isinstance(n["attrs"], str)
        assert json.loads(n["attrs"])["level"] == 3

    def test_判定不了的节点原样不动(self):
        """批量灌库路径上，一条脏数据不能打死整批（CLAUDE.md）。"""
        n = old_shape_node(level_zh="", level_code="")
        before = json.dumps(n, ensure_ascii=False, sort_keys=True)
        assert normalize_skill_level_node(n) == "unresolved"
        assert json.dumps(n, ensure_ascii=False, sort_keys=True) == before

    def test_ONET_的重要性刻度不被套国标表(self):
        """IM1–IM5 是 O*NET 的 importance，不是等级；套国标映射会造出假档位。"""
        n = {
            "type": "skill_level", "region": "US", "name": "Reading · IM4",
            "name_zh": None, "description": "",
            "attrs": {"level_code": "IM4", "scale": "onet_importance"},
        }
        assert normalize_skill_level_node(n) == "unresolved"
        assert "level" not in n["attrs"]

    def test_旧回填残留的_product_level_int_被清掉(self):
        n = old_shape_node()
        n["attrs"]["product_level_int"] = 3
        normalize_skill_level_node(n)
        assert "product_level_int" not in n["attrs"]
        assert n["attrs"]["level"] == 3


class Test批量:
    def test_只处理_skill_level_类型(self):
        nodes = [
            old_shape_node(),
            {"type": "occupation", "name": "镗工 · 三级/高级工", "attrs": {}},
        ]
        stats = normalize_nodes(nodes)
        assert stats == {"ok": 1, "skip": 0, "unresolved": 0}
        assert nodes[1]["name"] == "镗工 · 三级/高级工"   # 岗位名没被动

    def test_混合批次的计数(self):
        done = old_shape_node(skill="已归一")
        normalize_skill_level_node(done)
        nodes = [old_shape_node(), done, old_shape_node(level_zh="", level_code="")]
        assert normalize_nodes(nodes) == {"ok": 1, "skip": 1, "unresolved": 1}


class Test技能标识:
    def test_优先取_skill_key_再取_skill_name(self):
        assert skill_key_of({"skill_key": "A", "skill_name": "B"}, "C · L3") == "A"
        assert skill_key_of({"skill_name": "B"}, "C · L3") == "B"

    def test_兜底取_name_首段(self):
        assert skill_key_of({}, "工件加工 · L3") == "工件加工"

    def test_已归一的_name_再取一次不会被截短(self):
        """`制备 · L3` 二次归一时要还原成 `制备`，不能变成 `制备 · L3 · L3`。"""
        n = old_shape_node(skill="制备")
        normalize_skill_level_node(n)
        assert skill_key_of(n["attrs"], n["name"]) == "制备"
