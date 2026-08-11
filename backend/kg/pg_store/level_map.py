"""国标五级 ↔ 产品 L1–L5 映射。

产品语义（自评 / 能力描述 BR-04）：
  L1 了解 → L2 掌握 → L3 熟练 → L4 精通 → L5 专家

国标职业技能等级（人社）：
  五级/初级工 → 四级/中级工 → 三级/高级工 → 二级/技师 → 一级/高级技师

约定：**产品 level_int 升序 = 能力由浅入深**。
管道历史数据常把「一级/高级技师」标成 L1，读路径用本模块纠正为 product L5。
"""
from __future__ import annotations

import re
from typing import Any

# 国标 level_zh → 产品 L 码（长键优先，避免「技师」误匹配「高级技师」）
MOHRSS_ZH_TO_PRODUCT_ORDERED = [
    ("一级/高级技师", "L5"),
    ("高级技师", "L5"),
    ("二级/技师", "L4"),
    ("五级/初级工", "L1"),
    ("四级/中级工", "L2"),
    ("三级/高级工", "L3"),
    ("初级工", "L1"),
    ("中级工", "L2"),
    ("高级工", "L3"),
    ("五级", "L1"),
    ("四级", "L2"),
    ("三级", "L3"),
    ("二级", "L4"),
    ("一级", "L5"),
    ("技师", "L4"),  # 须在高级技师之后
]

# BR-01：标签来自全局 skill_level_meta（勿在此硬编码名称）
def product_labels() -> dict[int, str]:
    from backend.kg.pg_store.skill_level_meta import label_map

    return label_map()


def product_level_int_from_attrs(attrs: dict[str, Any] | None, name: str | None = None) -> int | None:
    """从节点 attrs/name 推断产品侧 1–5。"""
    a = attrs if isinstance(attrs, dict) else {}
    # 显式产品字段
    if a.get("product_level_int") is not None:
        try:
            return max(1, min(5, int(a["product_level_int"])))
        except (TypeError, ValueError):
            pass
    # 三级制（T1/T2/T3，level_zh 为 初级/中级/高级，共 625 个节点）：
    # 与国标五级工是两套刻度，且「初级」不含「工」字，走不到下面的国标词表匹配，
    # 此前一律返回 None → 这些档在管理端既选不了也显示不出。
    # 均匀铺到产品五级：初级→L1(了解) / 中级→L3(熟练) / 高级→L5(专家)。
    tcode = str(a.get("level_code") or "").upper()
    m_t = re.match(r"^T([1-3])$", tcode)
    if m_t:
        return {1: 1, 2: 3, 3: 5}[int(m_t.group(1))]

    zh = str(a.get("level_zh") or a.get("level_label") or "")
    for k, code in MOHRSS_ZH_TO_PRODUCT_ORDERED:
        if k in zh:
            return int(code[1])
    # 名称中的国标词
    s = f"{zh} {name or ''}"
    for k, code in MOHRSS_ZH_TO_PRODUCT_ORDERED:
        if k in s:
            return int(code[1])
    # 已是产品 L 码且 scale 为 l1_l5
    scale = str(a.get("scale") or "")
    code = str(a.get("level_code") or "").upper()
    m = re.match(r"L([1-5])$", code)
    if m:
        li = int(m.group(1))
        # cn_skill_grade 历史：一级→L1，需按 level_zh 优先；无 zh 时保持原码并标注
        if scale in ("cn_skill_grade", "mohrss_5") and zh:
            pass  # already handled above if zh matched
        elif scale in ("cn_skill_grade", "mohrss_5"):
            # 无 zh 时反转：管道 L1=一级专家 → 产品 5
            return 6 - li
        return li
    return None


def product_level_code(li: int | None) -> str | None:
    if li is None:
        return None
    return f"L{int(li)}"
