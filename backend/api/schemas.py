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


class IndustryLink(BaseModel):
    """岗位挂到的一个行业。"""

    id: str = Field(..., description="行业节点 id")
    name: str | None = Field(None, description="行业名")


class GraphCounts(BaseModel):
    """图查询的计数口径。"""

    node_count: int = Field(0, ge=0, description="返回的节点数")
    edge_count: int = Field(0, ge=0, description="返回的边数")
    root_count: int = Field(0, ge=0, description="根节点数（无父行业的顶层）")
    region: str | None = Field(None, description="地区，如 CN")


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
    version: int | None = Field(
        None, description="发布版本号，从 1 起，每次成功发布（status→published）+1"
    )
    version_label: str | None = Field(
        None, description="版本展示文案，如 `V3`（原型「版本」列）", examples=["V3"]
    )
    owner: str | None = Field(None, description="业务负责人 user-id")
    owner_name: str | None = Field(
        None, description="业务负责人姓名（原型「负责人」列）；新建时默认取创建人"
    )
    code: str | None = Field(
        None,
        description=(
            "业务编码（**同 region + 同 type 内唯一**，可编辑，写入前校验，冲突返回 409）。"
            "与 `id` 解耦：改 code 不影响 id 与已建的边。"
            "各维度编码体系独立——行业为语义 slug（`internet-ecom`）、"
            "专业为教育部专业码（`580506K`）、岗位为大典职业码（`6-18-01-10`）。"
            "同时保留在 `attrs.code` 以兼容既有前端"
        ),
        examples=["internet-ecom"],
    )
    level: int | None = Field(
        None, description="occupation 岗位层级 1..N；skill_level 可复用为 L 序"
    )
    category: str | None = Field(
        None,
        description=(
            "skill_level 技能大类的 **code**（TECH / OPERATE / SAFETY …），"
            "字典见 `GET /v1/kg/skill-categories`。展示请用 `category_name`"
        ),
    )
    category_name: str | None = Field(
        None,
        description=(
            "技能大类展示名，由 code 连 `kg_skill_category` 表取得，**不入库**。"
            "前端不要按 code 自己映射中文 —— 改名只动那张表"
        ),
    )
    # 草稿态（scope=manage 的读路径会带上；前台读到的永远是线上行，这几个字段恒为
    # is_draft=false / record_status=status / has_draft=false）。
    # 详见 docs/方案-管理台草稿态与发布.md
    is_draft: bool | None = Field(
        None, description="这一行是不是草稿行。读路径「同一 id 只取一行、草稿优先」"
    )
    has_draft: bool | None = Field(
        None,
        description=(
            "这条记录有没有未发布的草稿。等价于 `is_draft` —— 草稿优先取行之后，"
            "「拿到的是草稿行」就等于「有草稿」，不是另一个落库字段"
        ),
    )
    record_status: str | None = Field(
        None,
        description=(
            "**记录状态 = 最新版本的状态**：有草稿就是 `draft`，否则等于线上行的 status。"
            "列表上给运营看的就是这个；`status` 仍是本行自己的库内状态。"
            "判定按**发布单元**——只改技能构成（草稿边、没有草稿节点行）也算 draft"
        ),
    )
    draft_change: str | None = Field(
        None,
        description=(
            "草稿改的是什么：`node`=节点字段 | `edges`=仅关联/技能构成 | `both`=两者。"
            "`edges` 那种没有草稿节点行，但同样要发布才生效"
        ),
    )
    target_status: str | None = Field(
        None,
        description=(
            "仅草稿行有：发布后线上行会变成什么（published|disabled|archived），"
            "null=只更新内容不改状态。**不要把它写进 status**，那样草稿会泄漏到前台"
        ),
    )
    base_version: int | None = Field(
        None,
        description="仅草稿行有：基于哪个已发布版本改的；发布时与线上 version 不等则 409",
    )
    # 管理列表 scope=manage：有待审变更时附带（库内 status 仍为 published/disabled，前台不受影响）
    pending_change_id: int | None = Field(
        None, description="待审变更 id；有值表示有未审完的操作，但生效状态仍以 status 为准"
    )
    pending_action: str | None = Field(
        None, description="待审动作 create|update|delete|disable|enable（通过后才改库）"
    )
    pending_title: str | None = Field(None, description="待审变更的标题")
    counts: dict[str, int | float] | None = Field(
        None,
        description=(
            "关联计数（include_counts=1 时联读填充）。"
            "键：major/occupation/skill/industry/course/level；"
            "skill 为逻辑技能 DISTINCT skill_key；"
            "skill_aggregated 为专业经岗位两跳汇总的技能数。"
            "另有 weight_sum（岗位 requires 权重和）是**小数**，故值类型为 int | float"
        ),
    )
    industries: list[IndustryLink] | None = Field(
        None,
        description=(
            "岗位所属行业列表（occupation + include_counts）："
            "直连 belongs_to + 经专业 prepares_for→belongs_to 两跳，按 name, id 稳定排序"
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
    include_skills: bool | None = Field(None, description="是否下钻到技能层；默认不返回，数据量大")
    include_direct_occupations: bool | None = Field(None, description="是否包含行业直挂的岗位（不经专业）")


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
    weight: float | None = Field(None, description="权重")
    confidence: str | None = Field(None, description="置信度")
    evidence: str | None = Field(None, description="判定依据")


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
    meta: GraphCounts = Field(
        default_factory=GraphCounts,
        description="本次查询的计数与地区口径",
    )


class OccupationRequiresRow(BaseModel):
    """岗位技能扁平行（/v1/occupation/requires）。"""

    occupation: str = Field(..., description="岗位名称")
    occupation_url: str | None = Field(None, description="岗位来源 URL")
    skill_level: str = Field(..., description="技能/等级名称")
    skill_url: str | None = Field(None, description="技能来源 URL")
    weight: float | None = Field(None, description="要求权重")
    confidence: str | None = Field(None, description="置信度")
    evidence: str | None = Field(None, description="判定依据")


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
    neo4j_type: str | None = Field(None, description="Neo4j 侧的关系类型（历史兼容字段）")
    region: str | None = Field(None, description="地区，如 CN")
    weight: float | None = Field(None, description="权重")
    confidence: str | None = Field(None, description="置信度")
    evidence: str | None = Field(None, description="判定依据")
    source_url: str | None = Field(None, description="来源链接")
    status: str | None = Field(None, description="published|disabled|…")
    src_name: str | None = Field(None, description="起点名称")
    src_type: str | None = Field(None, description="起点类型")
    dst_name: str | None = Field(None, description="终点名称")
    dst_type: str | None = Field(None, description="终点类型")


class EdgeListResponse(BaseModel):
    items: list[EdgeListItem] = Field(default_factory=list, description="当前页数据")
    page: int = Field(..., description="页码，从 1 起")
    page_size: int = Field(..., description="每页条数")
    total: int = Field(..., description="总条数")
    total_pages: int = Field(..., description="总页数")
    rel_type: str | None = Field(None, description="关系类型")
    node_id: str | None = Field(None, description="按节点过滤时传入的 node_id")
    q: str | None = Field(None, description="本次查询用的关键词（回显）")


class SkillBundleBrief(BaseModel):
    """聚合后的技能 bundle 摘要（一个 skill_key 下的 L1–L5 汇总）。"""

    model_config = ConfigDict(extra="allow")

    id: str | None = Field(None, description="bundle:{region}:{skill_key}")
    skill_key: str | None = Field(None, description="技能聚合主键")
    skill_name: str | None = Field(None, description="技能名（去等级后缀）")
    region: str | None = Field(None, description="地区，如 CN")
    category: str | None = Field(None, description="技能大类 code，见 /v1/kg/skill-categories")
    category_name: str | None = Field(None, description="技能大类展示名（由 code 派生，不入库）")
    available_levels: list[int] = Field(
        default_factory=list, description="已配齐的档位 1–5"
    )
    missing_levels: list[int] = Field(default_factory=list, description="尚缺的档位 1–5")


class DeleteResult(BaseModel):
    """物理删除实际影响的条数。

    节点删除会连带删掉它两端的边（不级联删其它节点），所以两个计数都要给：
    运营点一次「删除岗位」，需要知道顺带带走了多少条关联边。
    """

    node_id: str | None = Field(None, description="被删的节点 id；删边时为 null")
    edge_id: str | None = Field(None, description="被删的边 id；删节点时为 null")
    nodes_deleted: int = Field(0, ge=0, description="删掉的节点数（0 表示目标本就不存在）")
    edges_deleted: int = Field(0, ge=0, description="连带删掉的关联边数")


class ChangePayload(BaseModel):
    """变更内容。

    真实形状随 `entity_kind` + `action` + `dim_type` 而变，无法收敛成单一结构，
    所以这里把**各分支可能出现的键**都列出来（都是可选），并允许额外字段。
    这样 Swagger 上看到的是真实键名，而不是一个 `any`。

    建节点时传节点字段；关联关系用 `*_ids` 列表表达，服务端自动建边，
    客户端不需要自己构造 edge：

    - 专业 major → `industry_ids`（major -belongs_to→ industry）
    - 岗位 occupation → `major_ids`（major -prepares_for→ occupation）
    - 技能 skill_level → `occupation_ids`（occupation -requires→ skill）
    """

    model_config = ConfigDict(extra="allow")

    type: str | None = Field(None, description="节点类型：industry / major / occupation / skill_level")
    id: str | None = Field(None, description="节点 id（编辑/删除时必填）")
    name: str | None = Field(None, description="名称")
    region: str | None = Field(None, description="地区，默认 CN")
    status: str | None = Field(None, description="状态；新建一律先落 draft，门禁通过才升 published")
    code: str | None = Field(None, description="编码")
    description: str | None = Field(None, description="描述")
    attrs: dict[str, Any] | None = Field(
        None, description="自由属性（无数据库约束的 JSON 列）"
    )
    industry_ids: list[str] | None = Field(None, description="关联行业 id 列表（专业用）")
    major_ids: list[str] | None = Field(None, description="关联专业 id 列表（岗位用）")
    occupation_ids: list[str] | None = Field(None, description="关联岗位 id 列表（技能用）")
    skill_key: str | None = Field(None, description="技能聚合主键（技能 bundle 用）")
    levels: dict[str, Any] | None = Field(
        None, description="L1–L5 各档内容（技能 bundle 用），键为档位号"
    )
    src_id: str | None = Field(None, description="边的源节点 id（entity_kind=edge）")
    dst_id: str | None = Field(None, description="边的目标节点 id")
    rel_type: str | None = Field(None, description="边的关系类型")
    weight: float | None = Field(None, description="边权重")


class DeleteResult(BaseModel):
    """物理删除实际影响的条数。

    节点删除会连带删掉它两端的边（不级联删其它节点），所以两个计数都要给：
    运营点一次「删除岗位」，需要知道顺带带走了多少条关联边。
    """

    node_id: str | None = Field(None, description="被删的节点 id；删边时为 null")
    edge_id: str | None = Field(None, description="被删的边 id；删节点时为 null")
    nodes_deleted: int = Field(0, ge=0, description="删掉的节点数（0 表示目标本就不存在）")
    edges_deleted: int = Field(0, ge=0, description="连带删掉的关联边数")


class AppliedResult(BaseModel):
    """变更实际落库的结果。

    与 `ChangePayload` 同理——建普通节点、建技能 bundle、改边三条路径返回的键
    各不相同，这里列出全部可能键（都是可选）并允许额外字段。

    `status=draft` + `gate` 一起出现时，表示写入成功但**发布门禁没过**，
    节点停在草稿态——这不是失败，前台看不到而已，要看 `gate.failed` 补数据。
    """

    model_config = ConfigDict(extra="allow")

    node: KgNode | None = Field(None, description="落库后的节点对象")
    nodes: list[KgNode] | None = Field(
        None, description="技能 bundle 一次建出的多个档位节点"
    )
    linked_edges: list[KgEdge] | None = Field(None, description="自动建出的边")
    skill_bundle: bool | None = Field(None, description="true=走的是技能 bundle 写入路径")
    skill_key: str | None = Field(None, description="技能聚合主键")
    levels: list[str] | list[dict[str, Any]] | None = Field(
        None,
        description=(
            "本次建出的档位码，如 `[\"L1\",\"L3\"]`。"
            "**曾误声明成 `list[dict]`** —— 写入其实成功了，但响应按模型校验时失败，"
            "接口回 400，管理台显示「新建失败」而库里已经有了，人就会重复提交。"
            "各档明细在 `nodes` / `bundle` 里。"
            "留成联合类型是有意的：这一处窄声明已经踩过一次，多一个分支不会让任何"
            "生产方失败，而窄一格就可能重演「写进去了却回 400」"
        ),
    )
    bundle: SkillBundleBrief | None = Field(None, description="聚合后的技能 bundle")
    status: str | None = Field(
        None, description="落库后的状态；draft 表示门禁未过，停在草稿"
    )
    gate: dict[str, Any] | None = Field(
        None, description="发布门禁结果（同 PublishValidateOut）；仅未通过时出现"
    )
    deleted: bool | None = Field(
        None,
        description=(
            "删除类变更是否已执行。**恒为布尔**——删除的条数看 `delete_result`。\n\n"
            "这里曾经在物理删节点时被塞进一个 dict（`{node_id, nodes_deleted, "
            "edges_deleted}`），而驳回那条路给的是 `true`，同一字段两种形状，"
            "响应模型校验直接把整个删除接口打成 500。"
        ),
    )
    delete_result: "DeleteResult | None" = Field(
        None, description="删除实际影响的条数；仅物理删除时出现"
    )
    note: str | None = Field(
        None,
        description=(
            "人话补充说明。内容类动作（新建/编辑/技能构成）会说明「已存为草稿，"
            "发布请走 POST /v1/admin/publish/node」；停用会说明连带停用了几条关联边"
        ),
    )


class ProposalOut(BaseModel):
    """审核提案。"""

    id: int = Field(..., description="提案 ID")
    kind: str = Field(..., description="node|edge|patch_node")
    payload: ChangePayload = Field(..., description="待写入内容")
    status: str = Field(..., description="pending|approved|rejected")
    reason: str | None = Field(None, description="驳回原因等")
    created_by: str | None = Field(None, description="提交人 user-id")
    created_by_name: str | None = Field(None, description="提交人姓名")
    reviewed_by: str | None = Field(None, description="审核人 id")
    reviewed_by_name: str | None = Field(None, description="审核人姓名")
    created_at: str | None = Field(None, description="创建时间 ISO")
    reviewed_at: str | None = Field(None, description="审核时间 ISO")
    applied: AppliedResult | None = Field(
        None, description="approve 后实际写入结果（node/edge）"
    )


class HealthPostgres(BaseModel):
    ok: bool = Field(..., description="是否通过")
    engine: str | None = None
    version: str | None = Field(None, description="版本号")
    nodes: int | None = None
    edges: int | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    status: str = Field(..., description="ok | degraded")
    service: str = Field(..., description="服务名")
    store: str = Field(..., description="postgresql")
    docs: str | None = Field(None, description="Swagger 路径")
    postgresql: HealthPostgres | None = Field(None, description="数据库连通性探测结果")


class DocsLinks(BaseModel):
    swagger: str = Field(..., description="Swagger 文档地址")
    redoc: str = Field(..., description="ReDoc 文档地址")
    openapi_json: str = Field(..., description="OpenAPI 规格地址")
    guide: str = Field(..., description="对接说明页地址")
    note: str = Field(..., description="备注说明")
    servers: list[dict[str, str]] = Field(default_factory=list, description="服务地址列表")


class AuthTempInfo(BaseModel):
    mode: str = Field(..., description="request_headers")
    required_headers: list[str] = Field(..., description="调用必须携带的请求头")
    note: str = Field(..., description="备注说明")


class ServiceDiscovery(BaseModel):
    """GET / 服务发现。"""

    service: str = Field(..., description="服务名")
    version: str = Field(..., description="版本号")
    store: str = Field(..., description="存储后端")
    default_region: str = Field(..., description="默认地区")
    docs: DocsLinks = Field(..., description="Swagger 文档地址")
    auth_temp: AuthTempInfo
    health: str = Field(..., description="健康检查地址")
    api_prefix: str = Field(..., description="接口路径前缀")
    dev_ui_enabled: bool = Field(..., description="是否挂载了自测页（SERVE_DEV_UI）")
    dev_ui: dict[str, str] | None = Field(None, description="自测页地址；未开启为 null")


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
    aliases: list[str] | dict[str, str] | None = Field(
        None,
        description="别名列表或别名结构（可选），用于扩展检索",
    )
    attrs: dict[str, Any] | None = Field(
        None,
        description=(
            "扩展属性 JSON（可选）。随 type 变化，例如专业："
            "`code` 专业代码、`level`/`level_zh` 办学层次；"
            "技能：`skill_name`、`level`（产品档 1–5，1 了解 → 5 专家）"
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
    aliases: list[str] | dict[str, str] | None = Field(
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
    payload: ChangePayload = Field(
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


# ── 系统与图查询（原先无 response_model，文档上看不到形状） ────


class PgProbe(BaseModel):
    """数据库连通性探测。`ok=false` 时只有 error，没有计数。"""

    model_config = ConfigDict(extra="allow")

    ok: bool = Field(..., description="是否连通")
    engine: str | None = Field(None, description="引擎，固定 postgresql")
    version: str | None = Field(None, description="数据库版本（截断至 80 字符）")
    nodes: int | None = Field(None, ge=0, description="kg_node 行数")
    edges: int | None = Field(None, ge=0, description="kg_edge 行数")
    error: str | None = Field(None, description="连接失败原因；ok=true 时为 null")


class AiGatewayStatus(BaseModel):
    """AI 网关就绪态。"""

    enabled: bool = Field(..., description="网关是否就绪（地址、token、模型三者齐备）")
    base_url: str | None = Field(None, description="网关地址；未配置为 null")
    model: str | None = Field(None, description="模型名；未配置为 null")
    has_token: bool = Field(..., description="是否已配置访问 token（不回显 token 本身）")


class GraphLink(BaseModel):
    """行业图的层间连边（专业 → 岗位）。"""

    from_: str = Field(..., alias="from", description="源节点 id")
    to: str = Field(..., description="目标节点 id")
    rel: str = Field(..., description="关系类型，如 prepares_for")

    model_config = ConfigDict(populate_by_name=True)


class ProgressionLink(BaseModel):
    """晋升链的一段。只保留两端都在当前画布内的边，否则会画出断头箭头。"""

    from_: str = Field(..., alias="from", description="低阶岗位 id")
    to: str = Field(..., description="高阶岗位 id")
    from_name: str | None = Field(None, description="低阶岗位名")
    to_name: str | None = Field(None, description="高阶岗位名")
    # 这四个字段以前漏在模型外：query 层产出了，FastAPI 按响应模型序列化时直接丢掉，
    # 前端拿到 undefined 后拼成 "Lundefined 全栈工程师"。加字段是修复的一半，
    # 另一半是 query 改读 attrs.level（见 occupation_level_meta 的双写说明）。
    from_level: int | None = Field(None, ge=1, le=5, description="低阶岗位职级 1–5，来自 attrs.level")
    to_level: int | None = Field(None, ge=1, le=5, description="高阶岗位职级 1–5，来自 attrs.level")
    from_level_code: str | None = Field(
        None, description="低阶职级码（L1–L5），由 level 派生**不入库**；前端直接用，不要自己拼 'L'+level"
    )
    to_level_code: str | None = Field(None, description="高阶职级码（L1–L5），同上")
    from_level_name: str | None = Field(None, description="低阶职级名（入门/专员/资深/经理/总监）")
    to_level_name: str | None = Field(None, description="高阶职级名，同上")
    rel_type: str = Field("advances_to", description="固定 advances_to")
    confidence: str | None = Field(None, description="置信度")
    evidence: str | None = Field(None, description="建边依据")

    model_config = ConfigDict(populate_by_name=True)


class GraphOccupationNode(BaseModel):
    """行业图里的岗位层节点。"""

    model_config = ConfigDict(extra="allow")

    id: str = Field(..., description="岗位节点 id")
    name: str | None = Field(None, description="岗位名")
    level: int | None = Field(None, description="岗位层级")
    major_ids: list[str] = Field(default_factory=list, description="对口专业 id 列表")


class MatrixCell(BaseModel):
    """矩阵里的一个非零格。用行列下标而非二维数组——矩阵很稀疏，摊平会浪费大量 0。"""

    r: int = Field(..., ge=0, description="行下标，对应 rows[r]")
    c: int = Field(..., ge=0, description="列下标，对应 cols[c]")
    v: int = Field(..., description="格值，含义见 metric")


class MajorOccupationMatrix(BaseModel):
    """专业 × 岗位热力图矩阵（layout=matrix 时才有）。"""

    model_config = ConfigDict(extra="allow")

    rows: list[str] = Field(default_factory=list, description="行：专业节点 id，按顺序")
    cols: list[str] = Field(default_factory=list, description="列：岗位节点 id，按顺序")
    cells: list[MatrixCell] = Field(
        default_factory=list, description="非零格；未出现的格视为 0"
    )
    max: int = Field(0, description="全矩阵最大格值，配色归一化用")
    metric: str | None = Field(
        None, description="格值口径，如 skill_affinity=专业与岗位的共同技能数"
    )


class SkillNodeBrief(BaseModel):
    """技能图里的一个技能节点。"""

    model_config = ConfigDict(extra="allow")

    skill_key: str | None = Field(
        None, description="技能聚合主键（ASCII code，形如 SK0123456789）"
    )
    skill_name: str | None = Field(
        None,
        description=(
            "技能展示名 —— **图上的节点标签用这个**。`skill_key` 从 2026-08-19 起是"
            "code，拿它当标签就是一串哈希。"
            "这里**没有** `name` 字段：它与 skill_name 装同一个值，留着只会让前端"
            "每次判「用哪个」，判错了不报错、只是显示不对"
        ),
    )
    depth: int | None = Field(
        None, ge=0, description="前置层深；0 表示无前置，可直接学"
    )
    required_level: int | None = Field(None, ge=1, le=5, description="岗位要求档 1–5")
    weight: float | None = Field(None, description="权重")


class SkillCategoryGroup(BaseModel):
    """技能大类分区。按职业功能推进顺序排，不按技能数量——否则展示顺序会和箭头方向矛盾。"""

    key: str = Field(
        ..., description="技能大类 **code**（TECH / OPERATE …），字典见 `GET /v1/kg/skill-categories`"
    )
    name: str = Field(
        "", description="技能大类展示名。分区标题用这个，别拿 key 显示给人看"
    )
    rank: int = Field(..., description="推进顺序序号，越小越靠前")
    skills: list[SkillNodeBrief] = Field(default_factory=list, description="该区技能")


class SkillPrereqLink(BaseModel):
    """技能前置关系：学 `to` 之前应先具备 `from`。"""

    from_: str = Field(..., alias="from", description="先修技能的 skill_key")
    to: str = Field(..., description="后继技能的 skill_key")
    # 同 PrereqOut.confidence：是来源等级文本，不是分数。原来写成 `float | str`
    # 「两种都收」看着安全，实际是把类型判断推给了前端 —— 每次读都要先判是数字
    # 还是字符串。库里存的只有那四个字面量，收窄成 str。
    confidence: str | None = Field(
        None, description="来源等级：manual_seed / official / derived / ai_inferred"
    )
    evidence: str | None = Field(None, description="判定依据")

    model_config = ConfigDict(populate_by_name=True)


class NodeRef(BaseModel):
    """节点详情里的关联引用。"""

    model_config = ConfigDict(extra="allow")

    id: str | None = Field(None, description="节点 id")
    name: str | None = Field(None, description="名称")
    skill_key: str | None = Field(None, description="技能聚合主键（技能类引用）")
    level: int | None = Field(None, description="等级 / 要求档")
    weight: float | None = Field(None, description="权重")


class HealthOut(BaseModel):
    """健康检查。

    `status=degraded` 表示服务起来了但依赖不健康（通常是连不上 PG），
    此时接口仍会应答，但业务查询会失败——探活别只看 HTTP 200。
    """

    model_config = ConfigDict(extra="allow")

    status: Literal["ok", "degraded"] = Field(..., description="ok=依赖全通；degraded=有依赖不可用")
    service: str = Field(..., description="服务名")
    store: str = Field(..., description="存储后端")
    docs: str = Field(..., description="Swagger 文档地址")
    postgresql: PgProbe | None = Field(None, description="数据库连通性探测结果")
    ai_gateway: AiGatewayStatus | None = Field(
        None, description="AI 网关就绪态，同 GET /v1/admin/ai-gateway"
    )
    config: dict | None = Field(
        None,
        description=(
            "本进程业务配置的来源：`source=remote` 来自 SDP 配置中心（含 profile 与注入键数），"
            "`local` 来自 .env / 环境变量。线上配置改了没生效时先看这里。"
        ),
        json_schema_extra={"example": {"source": "remote", "keys": 27, "profile": "development"}},
    )


class FrontendConfigOut(BaseModel):
    """前端启动配置。

    只暴露前端确实需要的开关，**不含任何密钥**；`auth_bypass` 在生产必须是 0。
    """

    model_config = ConfigDict(extra="allow")

    api_version: str | None = Field(None, description="接口版本号")
    uc_sdk_url: str | None = Field(None, description="用户中心 SDK 地址")
    uc_env: str | None = Field(None, description="用户中心环境")
    uc_component_host: str | None = Field(None, description="用户中心组件域名")
    uc_api_host: str | None = Field(None, description="用户中心接口域名")
    auth_bypass: bool | int | None = Field(
        None, description="是否跳过审核门禁；**生产必须为 0**"
    )
    review_required: bool | int | None = Field(
        None, description="0=修改直写生效；1=进待审队列"
    )
    llm_enabled: bool | None = Field(None, description="AI 网关是否可用；false 时 AI 功能走规则兜底")
    llm_model: str | None = Field(None, description="模型名；网关不可用时为 null")


class SharedSkill(BaseModel):
    """多个对口岗位共同要求的技能。命中岗位越多，越是这个专业的「专业基本功」。"""

    model_config = ConfigDict(extra="allow")

    skill_key: str | None = Field(None, description="技能聚合主键（ASCII code，形如 SK0123456789）")
    skill_name: str | None = Field(
        None,
        description=(
            "技能展示名 —— **页面上要显示这个**。`skill_key` 从 2026-08-19 起是 code，"
            "拿它渲染就是一串哈希（能力全景页的共享技能 chip 踩过）"
        ),
    )
    name: str | None = Field(None, description="技能名（同 skill_name，历史字段）")
    category: str | None = Field(None, description="技能大类 code，见 /v1/kg/skill-categories")
    category_name: str | None = Field(None, description="技能大类展示名（由 code 派生，不入库）")
    occupation_count: int | None = Field(
        None, ge=0, description="有多少个对口岗位要求它"
    )
    max_required_level: int | None = Field(
        None, ge=1, le=5, description="这些岗位里要求最高的档 1–5"
    )


class CapabilityMeta(BaseModel):
    """能力全景的查询口径。"""

    matched: int = Field(0, description="是否命中专业：0=没找到，其余字段为空")
    occupation_count: int = Field(0, ge=0, description="对口岗位数")
    skill_total: int = Field(0, ge=0, description="技能总数")
    skills_included: bool = Field(
        False, description="是否下钻返回了技能明细；默认 false，数据量大"
    )
    progression_count: int = Field(0, ge=0, description="晋升链条数")
    shared_skill_count: int = Field(0, ge=0, description="共性技能数")
    region: str | None = Field(None, description="地区")


class CapabilityOut(BaseModel):
    """专业能力全景：以一个专业为根，一次给出它的行业、对口岗位、晋升链与共性技能。

    前半段（node_types / rel_types / regions / endpoints）是服务能力自描述，
    与查的是哪个专业无关；后半段才是这次查询的结果。
    """

    model_config = ConfigDict(extra="allow")

    node_types: list[str] = Field(default_factory=list, description="支持的节点类型")
    rel_types: list[str] = Field(default_factory=list, description="支持的关系类型")
    regions: list[str] = Field(default_factory=list, description="已有数据的地区")
    endpoints: list[str] = Field(default_factory=list, description="主要查询入口")
    counts: dict[str, int] = Field(default_factory=dict, description="各类型节点数")
    root: KgNode | None = Field(None, description="作为根的专业节点；没匹配到为 null")
    industries: list[KgNode] = Field(
        default_factory=list, description="该专业所属行业（major -belongs_to→ industry）"
    )
    occupations: list[KgNode] = Field(
        default_factory=list, description="对口岗位（major -prepares_for→ occupation）"
    )
    progressions: list[ProgressionLink] = Field(
        default_factory=list, description="岗位间的晋升链"
    )
    shared_skills: list[SharedSkill] = Field(
        default_factory=list, description="多个对口岗位共同要求的技能，按命中岗位数降序"
    )
    meta: CapabilityMeta = Field(
        default_factory=lambda: CapabilityMeta(), description="本次查询的口径与计数"
    )


class GraphIndustryBrief(BaseModel):
    """图上的行业根节点。"""

    id: str = Field(..., description="行业节点 id")
    name: str | None = Field(None, description="行业名")
    region: str | None = Field(None, description="地区")


class GraphMajorNode(BaseModel):
    """行业图里的专业层节点。"""

    id: str = Field(..., description="专业节点 id")
    name: str | None = Field(None, description="专业名")
    occupation_count: int = Field(0, ge=0, description="对口岗位数；截断按它倒序，先丢弱关联的")


class GraphLayers(BaseModel):
    """行业图的分层节点。"""

    majors: list[GraphMajorNode] = Field(default_factory=list, description="专业层")
    occupations: list[GraphOccupationNode] = Field(
        default_factory=list, description="岗位层"
    )


class IndustryGraphMeta(BaseModel):
    """行业图的口径与截断说明。"""

    matched: int = Field(0, description="是否命中行业：0=没找到，此时各层为空")
    major_total: int = Field(0, ge=0, description="专业总数")
    major_shown: int = Field(0, ge=0, description="本次返回的专业数")
    occupation_total: int = Field(0, ge=0, description="岗位总数")
    occupation_shown: int = Field(0, ge=0, description="本次返回的岗位数")
    truncated: bool = Field(False, description="是否发生截断；true 表示你看到的不是全量")
    layout: str | None = Field(None, description="布局：layered / matrix")
    region: str | None = Field(None, description="地区")


class IndustryGraphOut(BaseModel):
    """行业图谱：行业 → 专业 → 岗位 三层 + 晋升链。"""

    industry: GraphIndustryBrief | None = Field(None, description="行业根节点；没命中为 null")
    layers: GraphLayers = Field(default_factory=GraphLayers, description="分层节点")
    links: list[GraphLink] = Field(default_factory=list, description="层间连边")
    progressions: list[ProgressionLink] = Field(
        default_factory=list, description="同岗位族内按 level 递进的晋升链"
    )
    matrix: MajorOccupationMatrix | None = Field(
        None, description="专业×岗位矩阵；仅 layout=matrix 时返回"
    )
    meta: IndustryGraphMeta = Field(
        default_factory=IndustryGraphMeta, description="口径与截断说明"
    )


class SkillsGraphMeta(BaseModel):
    """岗位技能图的口径。"""

    matched: int = Field(0, description="是否命中岗位")
    skill_total: int = Field(0, ge=0, description="技能总数")
    category_count: int = Field(0, ge=0, description="技能大类数")
    uncategorized: int = Field(0, ge=0, description="未分类的技能数")
    prereq_total: int = Field(0, ge=0, description="前置关系数")
    max_depth: int = Field(0, ge=0, description="前置层最大深度")
    order: str | None = Field(None, description="排序口径说明")
    region: str | None = Field(None, description="地区")


class OccupationSkillsGraphOut(BaseModel):
    """岗位技能图谱：技能按大类分区 + 区内前置关系。"""

    occupation: GraphOccupationNode | None = Field(None, description="岗位摘要")
    categories: list[SkillCategoryGroup] = Field(
        default_factory=list,
        description="技能大类分区，按学习顺序排；每区 skills[].depth 为前置层深（0=可直接学）",
    )
    prereqs: list[SkillPrereqLink] = Field(default_factory=list, description="前置关系边")
    meta: SkillsGraphMeta = Field(default_factory=SkillsGraphMeta, description="口径说明")


class NodeDetailOut(BaseModel):
    """节点详情。

    返回键随节点类型而变（技能有 levels / prereqs，岗位有 skills / majors），
    这里列出全部可能键；`extra=allow` 兜住未列出的。
    """

    model_config = ConfigDict(extra="allow")

    id: str | None = Field(None, description="节点 id")
    type: str | None = Field(None, description="节点类型")
    name: str | None = Field(None, description="名称")
    skill_key: str | None = Field(None, description="技能聚合主键（技能详情）")
    category: str | None = Field(None, description="技能大类 code，见 /v1/kg/skill-categories")
    category_name: str | None = Field(None, description="技能大类展示名（由 code 派生，不入库）")
    levels: list[dict[str, Any]] | None = Field(
        None, description="L1–L5 档位栅格，缺档的位置为空"
    )
    level_completeness: str | None = Field(None, description="档位完整度，形如「3/5」")
    level_descriptions: dict[str, str] | None = Field(
        None, description="各档能力描述，键为档位号"
    )
    occupations: list[NodeRef] | None = Field(None, description="引用该技能的岗位")
    prereqs: list[NodeRef] | None = Field(None, description="前置技能")
    unlocks: list[NodeRef] | None = Field(None, description="学完可解锁的技能")
    counts: dict[str, int] | None = Field(None, description="关联计数")
