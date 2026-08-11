"""
API 请求/响应模型（OpenAPI 契约源）。

前端以 /docs · /openapi.json 为准：改本文件字段与 description 即更新文档。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ── 公共枚举说明 ──────────────────────────────────────────────

NodeType = Literal[
    "industry", "major", "occupation", "skill_level", "course", "credential"
]
PublishStatus = Literal["draft", "published", "archived", "disabled"]
Confidence = Literal[
    "official", "derived", "ai_inferred", "manual_seed", "heuristic"
]


# ── 图节点 / 边 ───────────────────────────────────────────────


class KgNode(BaseModel):
    """知识图谱节点（专业/岗位/技能/课程/认证/行业等）。"""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "CN:major:MOE_CN:voc_associate:510201",
                    "labels": ["major"],
                    "region": "CN",
                    "type": "major",
                    "name": "计算机应用技术",
                    "display_name": "高专 · 计算机应用技术 · 510201",
                    "name_en": None,
                    "name_zh": "计算机应用技术",
                    "description": None,
                    "source_system": "MOE_CN",
                    "source_id": "voc_associate:510201",
                    "source_url": "http://www.moe.gov.cn/",
                    "confidence": "official",
                    "attrs": {
                        "code": "510201",
                        "level": "voc_associate",
                        "level_zh": "高职专科",
                    },
                    "status": "published",
                }
            ]
        }
    )

    id: str = Field(..., description="全局唯一 ID，如 CN:major:MOE_CN:…")
    labels: list[str] = Field(
        default_factory=list,
        description="类型标签列表（兼容旧 Neo 形态，通常等于 [type]）",
    )
    region: str | None = Field(None, description="区域：CN | EU | US")
    type: str = Field(
        ...,
        description="节点类型：industry|major|occupation|skill_level|course|credential",
    )
    name: str = Field(..., description="官方/原始名称（查询按此字段匹配）")
    display_name: str | None = Field(
        None,
        description="展示名；专业会消歧为「层次 · 名称 · 代码」",
    )
    name_en: str | None = Field(None, description="英文名")
    name_zh: str | None = Field(None, description="中文名")
    description: str | None = Field(None, description="简介/摘要")
    source_system: str | None = Field(
        None, description="来源系统，如 MOE_CN、MOHRSS、BOSS_ZHIPIN、MANUAL"
    )
    source_id: str | None = Field(None, description="来源侧主键/编码")
    source_url: str | None = Field(None, description="溯源 URL")
    confidence: str | None = Field(
        None,
        description="置信度：official|derived|ai_inferred|manual_seed|…",
    )
    attrs: dict[str, Any] | list[Any] | str | None = Field(
        None,
        description="扩展属性 JSON（专业 code/level、技能等级等，随 type 变化）",
    )
    status: PublishStatus | str | None = Field(
        None, description="库内状态 published|disabled|…；缺省视为 published"
    )
    order: int | None = Field(
        None,
        description="同层稳定排序（库列 sort_order；按 type 内 name 生成，从 1 起）",
    )
    sort_order: int | None = Field(
        None, description="同 order；兼容字段"
    )
    child_count: int | None = Field(
        None,
        description=(
            "向下可展开子节点数：industry→major、major→occupation、"
            "occupation→skill_level；懒加载「可展开 N 项」用"
        ),
    )
    updated_by: str | None = Field(None, description="最近修改人 user-id")
    updated_by_name: str | None = Field(None, description="最近修改人姓名")
    created_at: str | None = Field(
        None,
        description=(
            "创建时间 ISO8601。管理台列表默认按它倒序（`order_by=created_desc`），"
            "新建的数据排最前；历史数据用采集时间 fetched_at 回填"
        ),
        examples=["2026-08-11T08:04:39+00:00"],
    )
    level: int | None = Field(
        None, description="occupation 岗位层级 1..N；skill_level 可复用为 L 序"
    )
    category: str | None = Field(
        None,
        description="skill_level 技能大类（国标职业功能维度：安全与环保/作业准备/…）",
    )
    # 管理列表 scope=manage：有待审变更时附带（库内 status 仍为 published/disabled，前台不受影响）
    pending_change_id: int | None = Field(
        None, description="待审变更 id；有值表示有未审完的操作，但生效状态仍以 status 为准"
    )
    pending_action: str | None = Field(
        None, description="待审动作 create|update|delete|disable|enable（通过后才改库）"
    )
    pending_title: str | None = None
    counts: dict[str, int] | None = Field(
        None,
        description=(
            "关联计数（include_counts=1 时联读填充）。"
            "键：major/occupation/skill/industry/course/level；"
            "skill 为逻辑技能 DISTINCT skill_key"
        ),
    )
    industries: list[dict[str, Any]] | None = Field(
        None,
        description=(
            "岗位所属行业列表（occupation + include_counts）："
            "直连 belongs_to + 经专业 prepares_for→belongs_to 两跳"
        ),
    )
    industry_id: str | None = Field(None, description="industries[0].id")
    industry_name: str | None = Field(None, description="industries[0].name")
    # 管理端编辑 scope=manage 时附带
    industry_ids: list[str] | None = Field(
        None, description="专业→行业 关联 id 列表（编辑表单预选）"
    )
    major_ids: list[str] | None = Field(
        None, description="岗位←专业 关联 id 列表（编辑表单预选）"
    )
    occupation_ids: list[str] | None = Field(
        None, description="技能←岗位 关联 id 列表（编辑表单预选）"
    )
    link_ids: dict[str, list[str]] | None = Field(
        None, description="结构化关联 id：industry_ids/major_ids/occupation_ids"
    )


class KgEdge(BaseModel):
    """知识图谱关系边。"""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "edge:CN:major:…|prepares_for|CN:occupation:…",
                    "rel_type": "prepares_for",
                    "neo4j_type": "PREPARES_FOR",
                    "src_id": "CN:major:MOE_CN:voc_associate:510201",
                    "dst_id": "CN:occupation:…",
                    "weight": 0.8,
                    "confidence": "official",
                    "source_url": "http://www.moe.gov.cn/",
                    "evidence": "专业教学标准 · 对口岗位",
                }
            ]
        }
    )

    id: str | None = Field(None, description="边 ID")
    rel_type: str = Field(
        ...,
        description=(
            "关系类型小写：prepares_for(专→岗)|requires(岗→技)|belongs_to(岗→行业)|"
            "parent_of(行业父子)|related_to|taught_by|…"
        ),
    )
    neo4j_type: str | None = Field(
        None,
        description="关系类型大写（兼容旧前端字段名，等同 rel_type.upper()）",
    )
    src_id: str = Field(..., description="起点节点 id")
    dst_id: str = Field(..., description="终点节点 id")
    weight: float | None = Field(None, description="权重 0～1，越大越强")
    confidence: str | None = Field(None, description="边置信度")
    source_url: str | None = Field(None, description="溯源 URL")
    evidence: str | None = Field(None, description="证据摘要/原文片段")
    status: PublishStatus | str | None = Field(None, description="published|draft|archived")


class PathStep(BaseModel):
    """探索结果路径中的一步：节点或关系。"""

    kind: Literal["node", "rel"] = Field(..., description="node=节点步；rel=关系步")
    id: str | None = Field(None, description="节点 id（kind=node 时）")
    name: str | None = Field(None, description="节点名称（kind=node 时）")
    type: str | None = Field(None, description="节点类型（kind=node 时）")
    rel_type: str | None = Field(
        None, description="关系类型展示值，多为大写（kind=rel 时）"
    )
    rel_type_raw: str | None = Field(
        None, description="关系类型小写原始值（kind=rel 时）"
    )


class GraphPath(BaseModel):
    steps: list[PathStep] = Field(default_factory=list, description="路径步骤序列")
    length: int = Field(0, description="关系跳数")


class GraphMeta(BaseModel):
    """子图/探索元信息。"""

    matched: int = Field(0, description="命中种子节点数；0 表示未找到")
    depth: int | None = Field(None, description="展开跳数")
    max_nodes: int | None = Field(None, description="节点上限")
    node_count: int | None = Field(None, description="返回节点数")
    edge_count: int | None = Field(None, description="返回边数")
    path_count: int | None = Field(None, description="返回路径条数")
    q: str | None = Field(None, description="查询关键字")
    type: str | None = Field(None, description="类型过滤")
    region: str | None = Field(None, description="实际生效区域")
    list_all: bool | None = Field(None, description="是否全量列表模式")
    expand_seeds: int | None = Field(None, description="参与展开的种子数")
    truncated: bool | None = Field(None, description="是否因上限截断")
    layout_note: str | None = Field(None, description="前端布局提示")
    engine: str | None = Field(None, description="查询引擎，如 postgresql")
    message: str | None = Field(None, description="未命中等原因说明")
    mode: str | None = Field(
        None,
        description="expand_1hop | explore_bfs | search_seeds | industry_closure",
    )
    neighbor_count: int | None = Field(None, description="expand 返回的邻居数")
    limit: int | None = Field(None, description="expand 本批 limit")
    direction: str | None = Field(None, description="expand 方向")
    rel_types: list[str] | None = Field(None, description="expand 关系过滤")
    industry_id: str | None = Field(None, description="行业闭包子图的行业 id")
    industry_name: str | None = Field(None, description="行业名称")
    full_counts: dict[str, int] | None = Field(
        None, description="闭包全量各类型节点数（截断前）"
    )
    full_total: int | None = Field(None, description="闭包全量节点总数（截断前）")
    include_skills: bool | None = None
    include_direct_occupations: bool | None = None


class GraphResponse(BaseModel):
    """探索 / by-major 子图响应。"""

    root: KgNode | None = Field(None, description="主根节点（by-major 时）")
    roots: list[KgNode] = Field(default_factory=list, description="种子/根节点列表")
    nodes: list[KgNode] = Field(default_factory=list, description="子图节点")
    edges: list[KgEdge] = Field(default_factory=list, description="子图边")
    paths: list[GraphPath] = Field(
        default_factory=list, description="路径表（explore 有；by-major 可能空）"
    )
    meta: GraphMeta = Field(default_factory=GraphMeta, description="分页/截断等元数据")


class ExpandRequest(BaseModel):
    """1 跳邻居展开（对齐 Graph Explorer / Neo4j expand）。"""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "node_id": "CN:occupation:MOHRSS_CN:4-12-01-01",
                    "limit": 25,
                    "direction": "both",
                    "rel_types": ["requires", "prepares_for"],
                }
            ]
        }
    )

    node_id: str = Field(..., description="要展开的节点全局 id")
    limit: int = Field(25, ge=1, le=200, description="本批邻居上限，可重复 expand（最大 200）")
    direction: Literal["both", "out", "in"] = Field(
        "both", description="both=无向；out=出边；in=入边"
    )
    rel_types: list[str] | None = Field(
        None, description="关系类型白名单；空=不限"
    )
    region: str | None = Field(None, description="区域提示，默认 CN")


class EdgeBrief(BaseModel):
    """挂在列表项上的边摘要。"""

    id: str | None = Field(None, description="边 ID")
    rel_type: str | None = Field(None, description="关系类型")
    weight: float | None = None
    confidence: str | None = None
    evidence: str | None = None


class KgNodeWithEdge(KgNode):
    """节点 + 关联边摘要（专→岗、岗→技列表用）。"""

    edge: EdgeBrief | None = Field(None, description="与查询主体之间的关系摘要")


class IndustryTreeResponse(BaseModel):
    """行业树。"""

    nodes: list[KgNode] = Field(default_factory=list, description="行业节点")
    edges: list[KgEdge] = Field(
        default_factory=list, description="parent_of 边（父→子）"
    )
    roots: list[KgNode] = Field(default_factory=list, description="无父节点的根行业")
    meta: dict[str, Any] = Field(
        default_factory=dict,
        description="region / node_count / edge_count / root_count",
    )


class OccupationRequiresRow(BaseModel):
    """岗位技能扁平行（/v1/occupation/requires）。"""

    occupation: str = Field(..., description="岗位名称")
    occupation_url: str | None = Field(None, description="岗位来源 URL")
    skill_level: str = Field(..., description="技能/等级名称")
    skill_url: str | None = Field(None, description="技能来源 URL")
    weight: float | None = Field(None, description="要求权重")
    confidence: str | None = None
    evidence: str | None = None


class NodeListResponse(BaseModel):
    """
    四维管理 Table 分页列表响应。

    对齐参考后台 backend.html：行业 / 专业 / 岗位 / 技能 各自可像表格一样列出并翻页。
    请求：`GET /v1/kg/nodes?type=major&page=1&page_size=20`
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "items": [
                        {
                            "id": "CN:major:…",
                            "type": "major",
                            "name": "计算机应用技术",
                            "display_name": "高专 · 计算机应用技术 · 510201",
                            "region": "CN",
                        }
                    ],
                    "page": 1,
                    "page_size": 20,
                    "total": 2281,
                    "total_pages": 115,
                    "type": "major",
                    "region": "CN",
                    "q": None,
                    "status": None,
                }
            ]
        }
    )

    items: list[KgNode] = Field(
        default_factory=list,
        description="当前页节点列表（KgNode）",
    )
    page: int = Field(..., description="当前页码，从 1 开始", ge=1)
    page_size: int = Field(..., description="每页条数", ge=1, le=200)
    total: int = Field(..., description="符合条件的总条数（用于分页控件）")
    total_pages: int = Field(..., description="总页数 = ceil(total / page_size)")
    type: str | None = Field(
        None,
        description="本次过滤的节点类型；四维：industry|major|occupation|skill_level",
    )
    region: str | None = Field(None, description="实际生效区域，如 CN")
    q: str | None = Field(None, description="名称关键字（模糊）；空表示不限")
    status: str | None = Field(
        None, description="状态过滤；空表示排除 archived 的全部"
    )


class StatsResponse(BaseModel):
    """图规模统计。"""

    engine: str = Field(..., description="postgresql")
    nodes: int = Field(..., description="节点总数")
    edges: int = Field(..., description="边总数")
    nodes_by_type: dict[str, int] = Field(
        default_factory=dict, description="按 type 计数"
    )
    nodes_by_region: dict[str, int] = Field(
        default_factory=dict, description="按 region 计数"
    )
    edges_by_rel_type: dict[str, int] = Field(
        default_factory=dict, description="按 rel_type 计数"
    )
    edges_by_confidence: dict[str, int] = Field(
        default_factory=dict, description="按 confidence 计数"
    )
    operator: dict[str, str] | None = Field(
        None, description="当前请求操作人 {user_id, user_name}"
    )


class ArchiveEdgeResponse(BaseModel):
    id: str = Field(..., description="边 ID")
    status: Literal["archived"] = Field("archived", description="归档后状态")


class EdgeListItem(BaseModel):
    """边列表项（含端点名称，便于核对删节点后是否连带删边）。"""

    id: str | None = Field(None, description="边 ID")
    src_id: str = Field(..., description="起点节点 id")
    dst_id: str = Field(..., description="终点节点 id")
    rel_type: str = Field(..., description="关系类型")
    neo4j_type: str | None = None
    region: str | None = None
    weight: float | None = None
    confidence: str | None = None
    evidence: str | None = None
    source_url: str | None = None
    status: str | None = Field(None, description="published|disabled|…")
    src_name: str | None = Field(None, description="起点名称")
    src_type: str | None = Field(None, description="起点类型")
    dst_name: str | None = Field(None, description="终点名称")
    dst_type: str | None = Field(None, description="终点类型")


class EdgeListResponse(BaseModel):
    items: list[EdgeListItem] = Field(default_factory=list)
    page: int
    page_size: int
    total: int
    total_pages: int
    rel_type: str | None = None
    node_id: str | None = Field(None, description="按节点过滤时传入的 node_id")
    q: str | None = None


class ProposalOut(BaseModel):
    """审核提案。"""

    id: int = Field(..., description="提案 ID")
    kind: str = Field(..., description="node|edge|patch_node")
    payload: dict[str, Any] = Field(..., description="待写入内容")
    status: str = Field(..., description="pending|approved|rejected")
    reason: str | None = Field(None, description="驳回原因等")
    created_by: str | None = Field(None, description="提交人 user-id")
    created_by_name: str | None = Field(None, description="提交人姓名")
    reviewed_by: str | None = None
    reviewed_by_name: str | None = None
    created_at: str | None = Field(None, description="创建时间 ISO")
    reviewed_at: str | None = Field(None, description="审核时间 ISO")
    applied: dict[str, Any] | None = Field(
        None, description="approve 后实际写入结果（node/edge）"
    )


class HealthPostgres(BaseModel):
    ok: bool
    engine: str | None = None
    version: str | None = None
    nodes: int | None = None
    edges: int | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    status: str = Field(..., description="ok | degraded")
    service: str
    store: str = Field(..., description="postgresql")
    docs: str | None = Field(None, description="Swagger 路径")
    postgresql: HealthPostgres | None = None


class DocsLinks(BaseModel):
    swagger: str
    redoc: str
    openapi_json: str
    guide: str
    note: str
    servers: list[dict[str, str]] = Field(default_factory=list)


class AuthTempInfo(BaseModel):
    mode: str = Field(..., description="request_headers")
    required_headers: list[str]
    note: str


class ServiceDiscovery(BaseModel):
    """GET / 服务发现。"""

    service: str
    version: str
    store: str
    default_region: str
    docs: DocsLinks
    auth_temp: AuthTempInfo
    health: str
    api_prefix: str
    dev_ui_enabled: bool
    dev_ui: dict[str, str] | None = None


# ── 写请求体 ─────────────────────────────────────────────────


class NodeCreate(BaseModel):
    """新建节点请求体。字段含义与返回的 `KgNode` 对齐。"""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "type": "major",
                    "name": "示例专业",
                    "region": "CN",
                    "status": "draft",
                    "description": "手工录入",
                    "attrs": {"code": "999999", "level_zh": "高职专科"},
                }
            ]
        }
    )

    type: str = Field(
        ...,
        description=(
            "节点类型（必填）。取值："
            "industry=行业 | major=专业 | occupation=岗位 | "
            "skill_level=技能等级 | course=课程 | credential=认证"
        ),
        examples=["major"],
    )
    name: str = Field(
        ...,
        min_length=1,
        description="节点官方名称（必填）。搜索/匹配主字段，前端列表优先展示 display_name",
        examples=["计算机应用技术"],
    )
    region: str = Field(
        "CN",
        description="区域代码。本期默认/推荐 CN；可选 EU、US",
        examples=["CN"],
    )
    id: str | None = Field(
        None,
        description=(
            "全局唯一 ID（可选）。不传则服务端生成，形如 "
            "`CN:manual:{type}:{12位hex}`。若传入须保证全局唯一"
        ),
    )
    name_en: str | None = Field(None, description="英文名称（可选）")
    name_zh: str | None = Field(None, description="中文名称（可选；可与 name 相同）")
    description: str | None = Field(None, description="简介/备注（可选）")
    aliases: list[str] | dict[str, Any] | None = Field(
        None,
        description="别名列表或别名结构（可选），用于扩展检索",
    )
    attrs: dict[str, Any] | None = Field(
        None,
        description=(
            "扩展属性 JSON（可选）。随 type 变化，例如专业："
            "`code` 专业代码、`level`/`level_zh` 办学层次；"
            "技能：`skill_name`、`level_code`"
        ),
    )
    source_system: str | None = Field(
        "MANUAL",
        description="来源系统标识。手工录入默认 MANUAL；官方数据如 MOE_CN、MOHRSS",
    )
    source_url: str | None = Field(
        None,
        description="溯源链接（可选）。无则服务端可填 manual://admin",
    )
    confidence: str | None = Field(
        "manual_seed",
        description=(
            "置信度。取值建议：official=官方 | derived=规则派生 | "
            "ai_inferred=模型推断 | manual_seed=人工录入（默认）"
        ),
    )
    status: PublishStatus = Field(
        "draft",
        description=(
            "发布状态。draft=草稿（默认，可进审核）| "
            "published=已发布可见 | archived=归档不可用"
        ),
    )


class NodePatch(BaseModel):
    """编辑节点请求体。全部可选，只传需要修改的字段。"""

    name: str | None = Field(None, description="新名称（可选）")
    name_en: str | None = Field(None, description="新英文名（可选）")
    name_zh: str | None = Field(None, description="新中文名（可选）")
    description: str | None = Field(None, description="新简介（可选）")
    aliases: list[str] | dict[str, Any] | None = Field(
        None, description="覆盖别名（可选）"
    )
    attrs: dict[str, Any] | None = Field(
        None, description="覆盖扩展属性（可选；整对象替换，非深合并）"
    )
    source_url: str | None = Field(None, description="新溯源 URL（可选）")
    confidence: str | None = Field(
        None, description="新置信度（可选）：official|derived|ai_inferred|manual_seed"
    )
    status: PublishStatus | None = Field(
        None, description="新状态（可选）：draft|published|archived"
    )
    region: str | None = Field(None, description="新区域（可选），如 CN")


class EdgeCreate(BaseModel):
    """新建边请求体。"""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "src_id": "CN:major:MOE_CN:voc_associate:510201",
                    "dst_id": "CN:occupation:example",
                    "rel_type": "prepares_for",
                    "weight": 0.8,
                    "status": "draft",
                    "evidence": "专业教学标准对口岗位",
                }
            ]
        }
    )

    src_id: str = Field(
        ...,
        description="起点节点全局 id（必填，须已存在于库中）",
        examples=["CN:major:MOE_CN:voc_associate:510201"],
    )
    dst_id: str = Field(
        ...,
        description="终点节点全局 id（必填，须已存在于库中）",
    )
    rel_type: str = Field(
        ...,
        description=(
            "关系类型小写（必填）。常用："
            "prepares_for=专业培养对口岗位 | requires=岗位要求技能 | "
            "belongs_to=岗位归属行业 | parent_of=行业父子 | "
            "related_to=相关 | taught_by=技能由课程教授"
        ),
        examples=["prepares_for"],
    )
    region: str = Field("CN", description="边所属区域，默认 CN")
    id: str | None = Field(
        None,
        description="边 ID（可选）。不传则生成 `edge:{src}|{rel}|{dst}`",
    )
    weight: float | None = Field(
        None,
        description="关系强度 0～1（可选），越大表示越相关/越重要",
        ge=0,
        le=1,
    )
    evidence: str | None = Field(
        None, description="证据摘要或原文摘录（可选），便于审核溯源"
    )
    attrs: dict[str, Any] | None = Field(
        None, description="边扩展属性 JSON（可选）"
    )
    confidence: str | None = Field(
        "manual_seed",
        description="置信度（可选），默认 manual_seed；官方边用 official",
    )
    status: PublishStatus = Field(
        "draft",
        description="draft=草稿默认 | published=已发布 | archived=归档",
    )
    source_url: str | None = Field(None, description="溯源 URL（可选）")


class ProposalCreate(BaseModel):
    """提交审核提案请求体。"""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "kind": "edge",
                    "payload": {
                        "src_id": "CN:major:…",
                        "dst_id": "CN:occupation:…",
                        "rel_type": "prepares_for",
                        "weight": 0.7,
                    },
                }
            ]
        }
    )

    kind: str = Field(
        ...,
        description=(
            "提案类型（必填）。"
            "node=新建节点（payload 同 NodeCreate）| "
            "edge=新建边（payload 同 EdgeCreate）| "
            "patch_node=改节点（payload 须含 id，其余同 NodePatch）"
        ),
        examples=["edge"],
    )
    payload: dict[str, Any] = Field(
        ...,
        description=(
            "提案内容（必填）。字段与对应写接口请求体一致；"
            "审核 approve 后服务端将 status 置为 published 再写入图谱"
        ),
    )


class ProposalReviewBody(BaseModel):
    """审核决定请求体。"""

    action: Literal["approve", "reject"] = Field(
        ...,
        description="审核动作（必填）：approve=通过并写入 published；reject=驳回不落库",
        examples=["approve"],
    )
    reason: str | None = Field(
        None,
        description="原因说明（可选）。驳回时建议填写，便于提交人修改",
    )
