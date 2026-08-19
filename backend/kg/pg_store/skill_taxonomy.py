"""技能分类体系的单一来源：**code 是真源，name 只用于展示**。

为什么改成 code + 字典表
------------------------
原先 `kg_node.category` 直接存中文名，且库里并存着两套互不相干的口径：

    国标（MOHRSS_CN，8838 个）  操作与加工 / 设备维护与检修 / 质量与检验 …
    LLM （LLM_CN， 3135 个）    技术工程 / 数据能力 / 沟通协作 …

更糟的是 LLM 那批**只写了 `attrs.category`，`category` 列全空** —— 读路径查
`n.category`，于是页面上 3135 个技能一律显示「未分类」，数据其实在库里。

改法：本模块定义 code 体系，启动时幂等灌进 `kg_skill_category` 表；
`kg_node.category` 存 **code**，展示时连表取 `name`。改 name 不动数据，
加分类不改代码。

CATEGORY_ORDER 的双重职责（保留）
--------------------------------
顺序既是前端技能图谱的**分区排列顺序**（从左到右 = 学习推进顺序），
也是种子前置关系的**允许方向**（只能由靠前的分类指向靠后的分类）。
两者必须一致，否则「展示顺序」和「前置方向」会互相矛盾。

粒度取舍
--------
11 个实类 + 1 个兜底。刻意不做细：分类是给人看的抓手，不是标签墙 ——
细了以后每新增一批技能就要重排一次。像「作业准备」这种只在国标制造业口径里
成立的维度，并进了「操作与生产」。
"""
from __future__ import annotations

from typing import Any, TypedDict


class SkillCategory(TypedDict):
    code: str
    name: str
    description: str
    aliases: list[str]


# 兜底分类。叫「待归类」而不是「其他」—— 「其他」听着像已经分好了，
# 「待归类」明说这是缺口，管理台看到就知道要处理。
FALLBACK_CODE = "UNSORTED"

# 顺序 = 学习推进顺序，别随手调；aliases 是历史中文值，迁移与新技能匹配都靠它
SKILL_CATEGORIES: list[SkillCategory] = [
    {
        "code": "SAFETY",
        "name": "安全与环保",
        "description": "作业安全、劳动保护、环境与职业健康。职教里的学习起点",
        "aliases": ["安全与环保", "安全生产", "环境保护"],
    },
    {
        "code": "OPERATE",
        "name": "操作与生产",
        "description": "设备操作、加工工艺、生产作业及其准备工作",
        "aliases": ["操作与加工", "作业准备", "生产操作", "加工制造"],
    },
    {
        "code": "MAINTAIN",
        "name": "设备与维修",
        "description": "设备维护、故障诊断、检修与保养",
        "aliases": ["设备维护与检修", "设备维护", "检修维护"],
    },
    {
        "code": "QUALITY",
        "name": "质量与检验",
        "description": "质量控制、检测检验、标准符合性判定",
        "aliases": ["质量与检验", "质量控制", "检验检测"],
    },
    {
        "code": "TECH",
        "name": "技术研发",
        "description": "软硬件开发、算法、架构设计等工程技术能力",
        "aliases": ["技术工程", "技术研发", "研发工程", "工程技术"],
    },
    {
        "code": "DATA",
        "name": "数据与分析",
        "description": "数据处理、建模分析、商业洞察与量化决策",
        "aliases": ["数据与信息", "数据能力", "商业分析", "数据分析"],
    },
    {
        "code": "DESIGN",
        "name": "设计与内容",
        "description": "视觉与交互设计、原型、内容策划与创作",
        "aliases": ["设计创意", "内容创作", "创意设计"],
    },
    {
        "code": "SERVICE",
        "name": "服务与业务",
        "description": "客户服务、业务办理、用户运营与市场策略",
        "aliases": ["服务与业务", "运营策略", "客户服务", "业务办理"],
    },
    {
        "code": "MANAGE",
        "name": "管理与运营",
        "description": "团队与项目管理、流程运营、技术管理与工艺创新",
        "aliases": ["运营与管理", "技术管理与创新", "管理领导", "项目管理"],
    },
    {
        "code": "TEACH",
        "name": "培训与指导",
        "description": "带教、课程开发、技能传授与考核评价",
        "aliases": ["培训与指导", "教学培训", "带徒传艺"],
    },
    {
        "code": "GENERAL",
        "name": "通用素养",
        "description": "沟通协作、文档表达、职业素养等跨岗位通用能力",
        "aliases": ["通用素养", "沟通协作", "职业素养", "通用能力"],
    },
    {
        "code": FALLBACK_CODE,
        "name": "待归类",
        "description": "尚未归类的技能。管理台应逐步消化这一类，它代表数据缺口而非一种能力",
        "aliases": ["未分类", "其他", "其它"],
    },
]

CATEGORY_ORDER: list[str] = [c["code"] for c in SKILL_CATEGORIES]
_BY_CODE: dict[str, SkillCategory] = {c["code"]: c for c in SKILL_CATEGORIES}

# 别名 → code。含 code 自身与 name，这样迁移前后重复跑都收敛到同一结果
_ALIAS_TO_CODE: dict[str, str] = {}
for _c in SKILL_CATEGORIES:
    for _k in [_c["code"], _c["name"], *_c["aliases"]]:
        _ALIAS_TO_CODE[_k.strip().lower()] = _c["code"]


def to_code(value: str | None) -> str:
    """任意历史写法（中文名 / 别名 / code）归一成 code；认不出的落兜底。

    读写两侧都走这里 —— 采集脚本、直连改库、历史数据的写法各不相同，
    只在某一处做映射的话，另一处就会把同一个分类当成两个。
    """
    v = (value or "").strip()
    return _ALIAS_TO_CODE.get(v.lower(), FALLBACK_CODE) if v else FALLBACK_CODE


def name_of(code: str | None) -> str:
    """code → 展示名。未知 code 也给兜底名，不返回 None ——
    前端拿到 null 会显示成 "undefined"。

    优先用库里的 `kg_skill_category`（管理台可能改过名字或新增了分类），
    库不可用时回落到本模块常量。
    """
    key = (code or "").strip()
    if not key:
        return _BY_CODE[FALLBACK_CODE]["name"]
    n = db_name_map().get(key)
    if n:
        return n
    c = _BY_CODE.get(key)
    return c["name"] if c else _BY_CODE[FALLBACK_CODE]["name"]


# 字典表快照。分类是**低频变更的小表**（十几行），每序列化一个技能节点查一次库
# 就太蠢了；但也不能永久缓存，管理台改完名字要能看到。折中：进程内缓存 + TTL。
_NAME_CACHE: dict[str, str] = {}
_NAME_CACHE_AT: float = 0.0
_NAME_TTL = 60.0


def db_name_map(*, force: bool = False) -> dict[str, str]:
    """`code → name`，来自 `kg_skill_category` 表。

    读表失败**不抛异常**：分类只是展示名，拿不到就用代码里的常量兜底，
    不该让一张字典表把整个技能列表拖挂（这个项目已经被「一条脏数据打死一整页」
    坑过三次）。
    """
    global _NAME_CACHE_AT
    import time

    now = time.monotonic()
    if not force and _NAME_CACHE and (now - _NAME_CACHE_AT) < _NAME_TTL:
        return _NAME_CACHE
    try:
        from backend.kg.pg_store.client import connect

        with connect() as conn:
            rows = conn.execute(
                "SELECT code, name FROM kg_skill_category "
                "WHERE COALESCE(status,'published') = 'published'"
            ).fetchall()
        _NAME_CACHE.clear()
        _NAME_CACHE.update({r["code"]: r["name"] for r in rows})
        _NAME_CACHE_AT = now
    except Exception:
        # 表还没建（首次启动早于 ensure_biz_schema）或库不可达
        if not _NAME_CACHE:
            _NAME_CACHE.update({c["code"]: c["name"] for c in SKILL_CATEGORIES})
        # **失败也要盖时间戳**，否则 TTL 判断永远不成立（_NAME_CACHE_AT 停在 0.0），
        # 每次 name_of 都重连一次死库、每次都等满连接超时。
        # `_node_dict` 对每个带 category 的节点都调 name_of —— 库不可达时
        # 单元测试从 4s 变成好几分钟，线上则是「表还没建好那几秒」把请求全拖住。
        # 仍然只缓存 TTL 那么久，库恢复后 60s 内自愈。
        _NAME_CACHE_AT = now
    return _NAME_CACHE


def invalidate_name_cache() -> None:
    """管理台增删改分类后调用，让下一次读立刻拿到新值。"""
    global _NAME_CACHE_AT
    _NAME_CACHE_AT = 0.0


def category_meta(code: str | None) -> SkillCategory | None:
    return _BY_CODE.get((code or "").strip())


def category_rank(code: str | None) -> int:
    """分类的学习顺序序号；未知/兜底排到最后。"""
    if not code:
        return len(CATEGORY_ORDER) + 1
    try:
        return CATEGORY_ORDER.index(code)
    except ValueError:
        return len(CATEGORY_ORDER) + 1


def all_categories() -> list[dict[str, Any]]:
    """字典全量，带顺序。管理台下拉与 /meta 接口都用这个。"""
    return [
        {
            "code": c["code"],
            "name": c["name"],
            "description": c["description"],
            "sort_order": i,
            "is_fallback": c["code"] == FALLBACK_CODE,
        }
        for i, c in enumerate(SKILL_CATEGORIES)
    ]


# 兼容：老代码用中文名做「未分类」判断
UNCATEGORIZED = _BY_CODE[FALLBACK_CODE]["name"]


# 种子前置关系允许的 (前置分类, 后继分类)，用 code。
# 用显式白名单而非「顺序相邻」：后者会把同一岗位里并列的职责误连成依赖
# （曾连出「警情受理与调度指挥 → 搜救犬饲养与管理」，两者其实无先后）。
PREREQ_ALLOWED_PAIRS: set[tuple[str, str]] = {
    ("SAFETY", "OPERATE"),
    ("SAFETY", "MAINTAIN"),
    ("OPERATE", "MAINTAIN"),
    ("OPERATE", "QUALITY"),
    ("OPERATE", "DATA"),
    ("OPERATE", "MANAGE"),
    ("TECH", "DATA"),
    ("TECH", "MANAGE"),
    ("DESIGN", "TECH"),
    ("QUALITY", "MANAGE"),
    ("MAINTAIN", "MANAGE"),
    ("SERVICE", "MANAGE"),
    ("GENERAL", "SERVICE"),
    ("MANAGE", "TEACH"),
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
