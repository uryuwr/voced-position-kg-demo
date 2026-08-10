"""技能分类体系的单一来源。

分类维度取自国家职业技能标准正文结构「职业功能 → 工作内容 → 技能要求」中的
**职业功能**层（见《国家职业标准编制技术规程》）。库内 skill_level 全部来自该标准，
爬取时丢失了职业功能列，故由技能名规则反推（种子脚本 scripts/seed_skill_taxonomy.py）。

CATEGORY_ORDER 同时承担两个职责，必须保持一致，否则「展示顺序」和「前置方向」会互相矛盾：
1. 前端技能图谱的**分区排列顺序**（从左到右 = 学习推进顺序）
2. 种子前置关系的**允许方向**（只能由靠前的分类指向靠后的分类）
"""
from __future__ import annotations

# 职业功能推进顺序：先懂安全 → 会准备 → 能操作 → 会检修/检验 → 能做技术管理 → 才能带教
CATEGORY_ORDER: list[str] = [
    "安全与环保",
    "作业准备",
    "操作与加工",
    "设备维护与检修",
    "质量与检验",
    "数据与信息",
    "服务与业务",
    "技术管理与创新",
    "运营与管理",
    "培训与指导",
]

UNCATEGORIZED = "未分类"


def category_rank(name: str | None) -> int:
    """分类的学习顺序序号；未分类排到最后。"""
    if not name:
        return len(CATEGORY_ORDER) + 1
    try:
        return CATEGORY_ORDER.index(name)
    except ValueError:
        return len(CATEGORY_ORDER) + 1


# 种子前置关系允许的 (前置分类, 后继分类)。
# 用显式白名单而非「顺序相邻」：后者会把同一岗位里并列的职责误连成依赖
# （曾连出「警情受理与调度指挥 → 搜救犬饲养与管理」，两者其实无先后）。
PREREQ_ALLOWED_PAIRS: set[tuple[str, str]] = {
    ("安全与环保", "作业准备"),
    ("安全与环保", "操作与加工"),
    ("作业准备", "操作与加工"),
    ("操作与加工", "设备维护与检修"),
    ("操作与加工", "质量与检验"),
    ("操作与加工", "数据与信息"),
    ("操作与加工", "运营与管理"),
    ("质量与检验", "技术管理与创新"),
    ("设备维护与检修", "技术管理与创新"),
    ("技术管理与创新", "培训与指导"),
    ("运营与管理", "培训与指导"),
}


def topo_depth(keys: list[str], edges: list[tuple[str, str]]) -> dict[str, int]:
    """按前置 DAG 算每个技能的层深（0 = 无前置，可直接开始学）。

    edges 为 (前置, 后继)。用于前端在分区内纵向分层，让「越靠上越先学」成立。
    图中若存在环（理论上被 kg_skill_prereq 的无环校验挡住），迭代上限会兜底防死循环。
    """
    depth = {k: 0 for k in keys}
    incoming: dict[str, list[str]] = {k: [] for k in keys}
    for src, dst in edges:
        if src in depth and dst in depth:
            incoming[dst].append(src)
    for _ in range(len(keys) + 1):
        changed = False
        for k in keys:
            if not incoming[k]:
                continue
            d = max(depth[p] for p in incoming[k]) + 1
            if d > depth[k]:
                depth[k] = d
                changed = True
        if not changed:
            break
    return depth
