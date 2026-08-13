"""管理台契约：发布门禁、待审边、技能库、技能构成、技能先修。

与学员端的区别在可见性：这些接口能看到 `draft` / `disabled` 状态的数据，
所以出参里的 `status` 是原始图状态字符串，不是学员端那套映射过的整数。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.api.schemas_biz import SkillOut

# ── AI 网关 ──────────────────────────────────────────────────


class AiGatewayOut(BaseModel):
    """AI 网关连通状态。内测环境网关常为空，`enabled=false` 时所有 AI 功能走规则兜底。"""

    enabled: bool = Field(..., description="网关是否就绪（地址、token、模型三者齐备）")
    base_url: str | None = Field(None, description="网关地址；未配置为 null")
    model: str | None = Field(None, description="模型名；未配置为 null")
    has_token: bool = Field(..., description="是否已配置访问 token（不回显 token 本身）")


# ── 发布门禁 ─────────────────────────────────────────────────


class PublishCheck(BaseModel):
    """一条门禁规则的检查结果。"""

    rule: str = Field(..., description="规则编号，如 BR-02 / BR-04 / BR-06")
    ok: bool = Field(..., description="是否通过")
    message: str = Field(..., description="人话结论，可直接展示给运营")
    detail: dict[str, Any] = Field(
        default_factory=dict,
        description="规则各自的取证细节，结构随 rule 而异（缺哪些档、少哪些边等）",
    )


class PublishValidateOut(BaseModel):
    """发布门禁校验结果。`ok=false` 时 `failed` 就是拦下它的理由清单。"""

    ok: bool = Field(..., description="是否允许发布")
    checks: list[PublishCheck] = Field(..., description="全部规则的检查结果")
    failed: list[PublishCheck] = Field(..., description="未通过的规则；ok=true 时为空数组")
    rules: str = Field(..., description="本次覆盖的规则范围，如「BR-02~BR-06 / BR-08」")


class DemoteTypeSummary(BaseModel):
    """某一类节点的降级统计。"""

    checked: int = Field(0, ge=0, description="扫描数")
    demoted: int = Field(0, ge=0, description="降级数")
    ids: list[str] = Field(default_factory=list, description="被降级的节点 id，最多 50 个")
    keys: list[str] = Field(
        default_factory=list, description="被降级的 skill_key，最多 50 个（仅 skill 有）"
    )


class PublishDemoteOut(BaseModel):
    """不合规节点批量降级为 draft 的结果。"""

    major: DemoteTypeSummary = Field(..., description="专业")
    occupation: DemoteTypeSummary = Field(..., description="岗位")
    skill: DemoteTypeSummary = Field(..., description="技能")
    dry_run: bool = Field(..., description="true=只扫不改")
    region: str = Field(..., description="地区，如 CN")


# ── 待审边 ───────────────────────────────────────────────────


class EdgeReviewItem(BaseModel):
    """一条待审的边。"""

    id: str = Field(..., description="边 id")
    rel_type: str = Field(..., description="关系类型，如 requires / covers / belongs_to")
    src_id: str = Field(..., description="源节点 id")
    src_name: str | None = Field(None, description="源节点名")
    src_type: str | None = Field(None, description="源节点类型")
    dst_id: str = Field(..., description="目标节点 id")
    dst_name: str | None = Field(None, description="目标节点名")
    dst_type: str | None = Field(None, description="目标节点类型")
    confidence: str | None = Field(
        None, description="置信度标记，如 ai_inferred=模型推断（默认筛选项）"
    )
    weight: float | None = Field(None, description="边权重（requires 才有）")
    status: str | None = Field(None, description="边状态：published / draft / disabled")
    evidence: str | None = Field(None, description="建边依据")
    source_url: str | None = Field(None, description="来源链接")


class EdgeReviewFilter(BaseModel):
    """本次查询用到的筛选条件（回显）。"""

    confidence: str | None = Field(None, description="置信度筛选值")
    rel_type: str | None = Field(None, description="关系类型筛选值")
    region: str | None = Field(None, description="地区")


class EdgeReviewListOut(BaseModel):
    """待审边分页列表。"""

    items: list[EdgeReviewItem] = Field(..., description="当前页数据")
    page: int = Field(..., ge=1, description="页码，从 1 起")
    page_size: int = Field(..., ge=1, description="每页条数")
    total: int = Field(..., ge=0, description="总条数")
    total_pages: int = Field(..., ge=0, description="总页数")
    filter: EdgeReviewFilter = Field(..., description="筛选条件回显")


# ── 变更审核 ─────────────────────────────────────────────────


class ChangeApprovedOut(BaseModel):
    """变更通过。"""

    approved: Literal[True] = Field(..., description="固定 true")
    id: int = Field(..., description="变更单 id")
    applied: dict[str, Any] | None = Field(
        None, description="实际落库的内容；结构随变更类型而异（建节点 / 改边 / 改属性）"
    )


class ChangeRejectedOut(BaseModel):
    """变更驳回。"""

    rejected: Literal[True] = Field(..., description="固定 true")
    id: int = Field(..., description="变更单 id")
    deleted: bool = Field(..., description="变更单是否已删除")


# ── 技能库 ───────────────────────────────────────────────────


class SkillBundleListOut(BaseModel):
    """技能库分页列表（逻辑技能 bundle，多档已聚合）。"""

    items: list[SkillOut] = Field(..., description="当前页技能")
    page: int = Field(..., ge=1, description="页码，从 1 起")
    page_size: int = Field(..., ge=1, description="每页条数")
    total: int = Field(..., ge=0, description="总条数")
    total_pages: int = Field(..., ge=0, description="总页数")


class OccupationLink(BaseModel):
    """技能被哪个岗位引用。"""

    occupation_id: str = Field(..., description="岗位节点 id")
    occupation_name: str | None = Field(None, description="岗位名")
    level: int | None = Field(None, ge=1, le=5, description="该岗位要求的档位 1–5")
    weight: float | None = Field(None, description="权重")


class SkillBundlePreviewOut(BaseModel):
    """技能 bundle 写入前的影响面预览：会建几个节点、几条边。"""

    skill_key: str = Field(..., description="技能聚合主键")
    level_codes: list[str] = Field(..., description="将要写入的档位编码")
    level_count: int = Field(..., ge=0, description="档位数")
    occupation_count: int = Field(..., ge=0, description="关联岗位数")
    occupation_links: list[OccupationLink] = Field(..., description="关联岗位明细")
    will_create_nodes: int = Field(..., ge=0, description="预计新建节点数")
    will_create_edges: int = Field(..., ge=0, description="预计新建边数")


# ── 技能构成 ─────────────────────────────────────────────────


class CompositionNodeHeader(BaseModel):
    """构成页头部：原型「岗位模型 · 内容运营专员」上方那几项。"""

    id: str = Field(..., description="节点 id")
    type: str = Field(..., description="节点类型：occupation / major")
    name: str | None = Field(None, description="名称")
    status: str | None = Field(None, description="图状态：published / draft / disabled")
    level: int | None = Field(None, description="职级")
    level_label: str | None = Field(None, description="职级文案，如 L3")
    version_label: str = Field(..., description="版本号文案，如 V1")
    owner_name: str | None = Field(None, description="负责人")
    description: str | None = Field(None, description="描述")
    industries: list["IdName"] = Field(default_factory=list, description="所属行业（可多个）")
    industry_name: str | None = Field(
        None, description="行业名拼接串，便于直接展示；无为 null"
    )
    majors: list["IdName"] = Field(default_factory=list, description="关联专业（岗位才有）")
    major_name: str | None = Field(None, description="专业名拼接串")
    salary: str | None = Field(None, description="薪资区间（来自 attrs）")
    demand: str | None = Field(None, description="需求热度（来自 attrs）")
    code: str | None = Field(None, description="编码（来自 attrs）")


class IdName(BaseModel):
    """id + name 的轻引用。"""

    id: str = Field(..., description="节点 id")
    name: str | None = Field(None, description="名称")


class CompositionLevelDetail(BaseModel):
    """技能某一档的明细。"""

    level: int | None = Field(None, ge=1, le=5, description="产品档 1–5")
    level_label: str | None = Field(None, description="档位文案，取自 skill_level_meta")
    node_id: str | None = Field(None, description="该档的 skill_level 节点 id")
    requirement: str | None = Field(None, description="该档的能力要求描述")
    status: str | None = Field(None, description="该档节点的状态")


class CompositionItem(BaseModel):
    """技能构成里的一项。

    一个技能只出现一次——高级档天然包含低级档，同一技能挂多档是数据错误，
    写入侧已加判重（`mode=add` 时重复会返回 409）。
    """

    edge_id: str = Field(..., description="requires / covers 边的 id")
    skill_key: str = Field(..., description="技能聚合主键")
    category: str | None = Field(None, description="技能大类")
    skill_level_id: str = Field(..., description="边指向的那个 skill_level 节点 id")
    available_levels: list[int] = Field(
        default_factory=list, description="该技能已配齐的档位 1–5"
    )
    levels: list[CompositionLevelDetail] = Field(
        default_factory=list, description="各档明细（含档位文案与要求描述）"
    )
    selected_level: int | None = Field(
        None,
        ge=1,
        le=5,
        description="当前选中的要求档。改档 = 删旧边建新边，边指向哪档就是要求哪档",
    )
    weight: float | None = Field(
        None, description="权重；专业 covers 无权重，此时为 null"
    )
    weight_pct: int | None = Field(None, description="权重百分比，便于直接展示")


class CompositionCounts(BaseModel):
    """构成计数。"""

    skill: int = Field(0, ge=0, description="技能项数")


class SkillCompositionAdminOut(BaseModel):
    """岗位/专业的技能构成（管理台视图）。

    岗位 `requires` 技能带权重且需归一化（Σ≈1）；专业 `covers` 技能无权重、不归一。
    `weighted` 就是这两种情况的区分标志。
    """

    node: CompositionNodeHeader = Field(..., description="节点头部信息")
    relation: Literal["requires", "covers"] = Field(
        ..., description="requires=岗位要求技能（带权重）；covers=专业覆盖技能（无权重）"
    )
    weighted: bool = Field(..., description="是否带权重；covers 为 false")
    items: list[CompositionItem] = Field(..., description="技能构成明细")
    weight_sum: float | None = Field(
        None, description="权重之和，**小数**；weighted=false 时为 null"
    )
    normalized: bool = Field(..., description="权重是否已归一（0.995–1.005）")
    can_normalize: bool = Field(..., description="是否支持一键归一化（等同 weighted）")
    counts: CompositionCounts = Field(..., description="计数")
    normalized_from: float | None = Field(
        None, description="归一化前的权重和；仅归一化接口返回"
    )


class SkillOptionLevel(BaseModel):
    """备选技能的一个档位。"""

    level: int | None = Field(None, ge=1, le=5, description="产品档 1–5")
    level_label: str | None = Field(None, description="档位文案")
    requirement: str | None = Field(None, description="该档能力要求描述")


class SkillOptionOut(BaseModel):
    """备选技能（下拉用），按 skill_key 聚合。"""

    skill_key: str = Field(..., description="技能聚合主键")
    category: str | None = Field(None, description="技能大类")
    available_levels: list[int] = Field(
        default_factory=list, description="已配齐的档位 1–5"
    )
    levels: list[SkillOptionLevel] = Field(
        default_factory=list, description="各档明细"
    )
    level_completeness: str = Field(
        ..., description="档位完整度，形如「3/5」——五档没配齐的技能选了会缺档"
    )


# ── 技能先修 ─────────────────────────────────────────────────


class PrereqOut(BaseModel):
    """一条先修关系：学 `skill_key` 之前应先具备 `prereq_skill_key`。"""

    skill_key: str = Field(..., description="技能聚合主键")
    prereq_skill_key: str = Field(..., description="先修技能的聚合主键")
    region: str = Field(..., description="地区，如 CN")
    evidence: str | None = Field(None, description="判定依据")
    confidence: float | None = Field(None, description="置信度 0–1")
    created_at: str | None = Field(None, description="创建时间 ISO8601")


class PrereqDeletedOut(BaseModel):
    """删除先修关系的回执。"""

    deleted: Literal[True] = Field(..., description="固定 true")
    skill_key: str = Field(..., description="技能聚合主键")
    prereq_skill_key: str = Field(..., description="被移除的先修技能")


CompositionNodeHeader.model_rebuild()
