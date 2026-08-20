"""管理台契约：发布门禁、待审边、技能库、技能构成、技能先修。

与学员端的区别在可见性：这些接口能看到 `draft` / `disabled` 状态的数据，
所以出参里的 `status` 是原始图状态字符串，不是学员端那套映射过的整数。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.api.schemas import AppliedResult
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


# ── 草稿态：待发布清单 / 发布 / 丢弃 ─────────────────────────
#
# 状态模型见 docs/方案-管理台草稿态与发布.md：一条记录最多两行（线上行 + 草稿行），
# 草稿行的 status 恒为 'draft'，「发布后要变成什么」在 target_status 里。


class DraftItem(BaseModel):
    """待发布清单的一行 = 一个发布单元（节点草稿 + 挂在它上面的草稿边）。"""

    node_id: str = Field(..., description="单元 id（= 节点 id，草稿边的 unit_id 指向它）")
    name: str | None = Field(None, description="名称：有草稿取草稿里的新名字")
    type: str | None = Field(None, description="industry|major|occupation|skill_level|…")
    region: str | None = Field(None, description="区域")
    is_new: bool = Field(
        ..., description="true=从未发布过（只有草稿行），发布后前台才第一次看到它"
    )
    has_node_draft: bool = Field(
        ...,
        description="false=节点本身没改、只改了关联边（如技能构成）；这种单元同样要发布",
    )
    change_kind: str = Field(
        "node",
        description=(
            "这个单元改了什么：`node`=节点字段 | `edges`=仅关联/技能构成 | `both`=两者都有。"
            "**只改技能构成时不会有草稿节点行**，但一样要发布，所以清单必须列出来并标明"
        ),
    )
    change_label: str = Field("", description="`change_kind` 的中文说明，可直接展示")
    record_status: str = Field("draft", description="记录状态，清单里恒为 draft")
    target_status: str | None = Field(
        None,
        description=(
            "发布后线上行应变成的状态：published | disabled | archived；"
            "null=只更新内容、状态不变。"
            "**停用 / 启用 / 删除已改成立即生效、不进草稿**，所以这里实际只剩两种来源："
            "① 新建记录发布时该落成什么状态；② 边的墓碑（技能构成里移除一项）"
        ),
    )
    base_version: int | None = Field(None, description="草稿基于的已发布版本号")
    published_version: int | None = Field(None, description="线上行当前版本号")
    published_status: str | None = Field(None, description="线上行当前状态")
    stale: bool = Field(
        ...,
        description="true=你编辑期间别人发布过（base_version ≠ 线上 version），直接发布会 409",
    )
    edges_upsert: int = Field(0, ge=0, description="待新增/更新的边数")
    edges_remove: int = Field(0, ge=0, description="待删除的边数（墓碑草稿）")
    updated_by_name: str | None = Field(None, description="最后编辑人")
    created_at: str | None = Field(None, description="草稿创建时间 ISO8601")


class DraftListOut(BaseModel):
    """待发布清单（分页）。"""

    items: list[DraftItem] = Field(..., description="发布单元列表")
    page: int = Field(..., ge=1, description="页码")
    page_size: int = Field(..., ge=1, description="每页条数")
    total: int = Field(..., ge=0, description="总单元数")
    total_pages: int = Field(..., ge=0, description="总页数；total=0 时为 0")


class PublishNodeOut(BaseModel):
    """发布一个单元的结果。"""

    node_id: str = Field(..., description="单元 id")
    status: str = Field(..., description="发布后线上行的状态")
    version: int = Field(..., ge=1, description="发布后的版本号（覆盖既有内容时 +1）")
    published_node: bool = Field(
        ..., description="false=本次只发布了边（节点本身没有草稿）"
    )
    edges_published: int = Field(0, ge=0, description="生效的边数")
    edges_archived: int = Field(0, ge=0, description="归档的边数")
    gate: PublishValidateOut | None = Field(
        None,
        description="BR 门禁结果；仅当发布后状态为 published 时才跑，其余为 null",
    )


class PublishBatchItem(BaseModel):
    """批量发布里的一项。逐个独立事务，一个失败不影响其余。"""

    node_id: str = Field(..., description="单元 id")
    ok: bool = Field(..., description="是否发布成功")
    code: str = Field(
        ...,
        description=(
            "published=成功 | not_found=没有草稿 | conflict=并发(409) | "
            "code_conflict=编码被占 | missing_endpoints=端点未发布 | gate_failed=门禁不过"
        ),
    )
    message: str | None = Field(None, description="失败原因（人话，可直接展示）")
    missing: list[str] | None = Field(None, description="missing_endpoints 时缺哪些节点")
    violations: list[PublishCheck] | None = Field(
        None, description="gate_failed 时未通过的规则"
    )
    detail: PublishNodeOut | None = Field(None, description="成功时的发布结果")


class PublishBatchOut(BaseModel):
    """批量发布结果。"""

    total: int = Field(..., ge=0, description="提交的单元数（已去重）")
    published: int = Field(..., ge=0, description="成功数")
    failed: int = Field(..., ge=0, description="失败数")
    items: list[PublishBatchItem] = Field(..., description="逐项结果")


class DraftDiscardedOut(BaseModel):
    """丢弃草稿的结果。线上行不受影响。"""

    node_id: str = Field(..., description="单元 id")
    nodes_discarded: int = Field(..., ge=0, description="删掉的节点草稿行数（0 或 1）")
    edges_discarded: int = Field(..., ge=0, description="删掉的草稿边数")


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
    applied: AppliedResult | None = Field(
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
    skill_key: str = Field(..., description="技能聚合主键（ASCII code）")
    skill_name: str | None = Field(None, description="展示名 —— **页面上要显示这个**。`skill_key` 从 2026-08-19 起是 ASCII code（形如 SK0123456789），拿它渲染就是一串哈希")
    category: str | None = Field(None, description="技能大类")
    prereqs: list[str] = Field(
        default_factory=list,
        description=(
            "先修技能的 skill_key 列表（来自 `kg_skill_prereq`）；空数组表示无先修。"
            "**不限本节点的技能集**——前置技能可能不被本岗位/专业要求，但学员仍需先具备"
        ),
    )
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
    dangling: bool = Field(
        False,
        description=(
            "**异常边标记**：这条边指向的技能节点不是 published（停用 / 归档 / 仅草稿）。"
            "前台按节点状态过滤看不到这条技能，管理台口径看得到 —— "
            "于是同一个岗位「前台 5 项 Σ0.81 / 管理台 6 项 Σ1.00」。"
            "读侧**不会**偷偷把它滤掉（那样运营永远发现不了），而是在这里标出来。"
            "存量数据用 `scripts/check_dangling_status_edges.py` 扫"
        ),
    )
    endpoint_status: str | None = Field(
        None, description="该边指向的技能节点的实际 status（dangling 时用它解释原因）"
    )


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

    skill_key: str = Field(..., description="技能聚合主键（ASCII code，形如 SK0123456789）")
    skill_name: str | None = Field(
        None,
        description="技能展示名 —— **下拉框里该显示这个**，skill_key 是给接口用的 code",
    )
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
    # 两个 key 都是 ASCII code，各配一个同层展示名 —— 少了这两个声明，
    # 数据层给了也会被 Pydantic 静默丢弃，管理台先修列表就是两串哈希
    skill_name: str | None = Field(None, description="技能展示名")
    prereq_skill_name: str | None = Field(None, description="先修技能展示名")
    region: str = Field(..., description="地区，如 CN")
    evidence: str | None = Field(None, description="判定依据")
    # **是文本不是分数**：这个项目的 confidence 一律是来源等级
    # （`manual_seed` / `official` / `derived` / `ai_inferred`，见 kg/provenance.py），
    # 不是 0–1 的置信度。原来声明成 float，于是先修技能一存就 500 ——
    # 而写库其实已经成功，报错发生在拼响应时，比单纯失败更难查。
    # 与 `weight_sum` 那次（照着「它应该是个计数」写 int，害整页岗位列表挂掉）同形。
    confidence: str | None = Field(
        None, description="来源等级：manual_seed / official / derived / ai_inferred"
    )
    created_at: str | None = Field(None, description="创建时间 ISO8601")


class PrereqDeletedOut(BaseModel):
    """删除先修关系的回执。"""

    deleted: Literal[True] = Field(..., description="固定 true")
    skill_key: str = Field(..., description="技能聚合主键")
    prereq_skill_key: str = Field(..., description="被移除的先修技能")


CompositionNodeHeader.model_rebuild()
