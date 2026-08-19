"""学员端 / 管理端业务模型（对齐 frontend.html 交互原型）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PageMeta(BaseModel):
    page: int = Field(..., description="页码，从 1 起")
    page_size: int = Field(..., description="每页条数")
    total: int = Field(..., description="总条数")
    total_pages: int = Field(..., description="总页数")


class RelationCounts(BaseModel):
    """关联节点数量（联读聚合；按 type 有意义键非 0）。"""

    major: int = Field(0, description="关联专业数")
    occupation: int = Field(0, description="关联岗位数")
    skill: int = Field(0, description="逻辑技能数（DISTINCT skill_key），非 L 扁平行数")
    industry: int = Field(0, description="关联行业数")
    course: int = Field(0, description="关联课程数")
    level: int = Field(0, description="技能 bundle 下已有 L 档数")
    skill_aggregated: int = Field(0, description="专业经岗位两跳汇总的技能数（skill 为直连数）")
    weight_sum: float = Field(0.0, description="岗位 requires 权重和，**小数**；归一化后应为 1.0")


class EdgeBrief(BaseModel):
    """连边摘要：这条记录是**经哪条边**关联进来的。

    同一个节点可能因不同的边被带进列表（岗位既 belongs_to 行业、又被专业
    prepares_for），`rel_type` 与 `weight` 说明的是这一次的关联口径。
    """

    id: str | None = Field(None, description="边 id")
    rel_type: str | None = Field(
        None, description="关系类型：requires / covers / belongs_to / prepares_for"
    )
    weight: float | None = Field(None, description="边权重；无权重的关系为 null")
    confidence: str | None = Field(None, description="置信度标记，如 ai_inferred")


# `attrs` 是无约束的 TEXT/JSON 列：同一个键在不同来源里可能是字符串、数字、布尔。
# 直接把它声明成 `str` 就是在赌数据 —— 一条 `attrs.level=2` 的岗位记录混进专业详情，
# 整个接口 500（本轮真撞到过）。展示类字段一律按**实际可能的取值**声明。
# 需要参与计算的字段（required_level / weight）不走这个别名，而是在组装时过
# `config.as_level` / `as_weight` 洗成确定类型 —— 类型放宽只该给纯展示字段。
AttrScalar = str | int | float | bool | None


class IndustryRef(BaseModel):
    id: str = Field(..., description="行业节点 id")
    name: str | None = Field(None, description="行业名")


class ProfessionOut(BaseModel):
    """专业（产品口径 profession，图侧 type=major）。"""

    id: str = Field(..., description="节点 id")
    name: str | None = Field(None, description="展示名")
    raw_name: str | None = Field(None, description="原始 name")
    type: str = Field("profession", description="固定 profession")
    kg_type: str | None = Field(None, description="图节点类型 major")
    region: str | None = Field(None, description="地区，如 CN")
    status: int | None = Field(None, description="1=已发布 0=草稿 2=停用（映射）")
    desc: str | None = Field(None, description="简介")
    industry: AttrScalar = Field(None, description="行业/门类提示（取自 attrs，类型不受约束）")
    code: AttrScalar = Field(None, description="专业代码（取自 attrs，可能是数字）")
    level: AttrScalar = Field(
        None,
        description=(
            "层次：专业是 `voc_associate` 这类字符串，但同一个键在别的节点类型上是 int，"
            "所以声明成标量联合"
        ),
    )
    level_zh: AttrScalar = Field(None, description="学历层次中文名")
    source_url: str | None = Field(None, description="来源链接")
    attrs: dict[str, Any] | None = Field(None, description="自由属性（无数据库约束的 JSON 列，键随数据来源而异）")
    counts: RelationCounts | None = Field(
        None, description="occupation=对口岗数；skill=关联逻辑技能数"
    )


class PositionOut(BaseModel):
    """岗位（产品 position，图侧 occupation）。"""

    id: str = Field(..., description="节点 id")
    name: str | None = Field(None, description="名称")
    raw_name: str | None = Field(None, description="原始名称（未做展示名替换）")
    type: str = Field("position", description="类型")
    kg_type: str | None = Field(None, description="图侧节点类型")
    region: str | None = Field(None, description="地区，如 CN")
    status: int | None = Field(None, description="状态")
    desc: str | None = Field(None, description="简介")
    tier: str | int | None = Field(None, description="职级/推荐档")
    demand: AttrScalar = Field(None, description="需求热度（取自 attrs，可能是数字）")
    salary: AttrScalar = Field(None, description="薪资区间（取自 attrs，可能是数字）")
    source_url: str | None = Field(None, description="来源链接")
    attrs: dict[str, Any] | None = Field(None, description="自由属性（无数据库约束的 JSON 列，键随数据来源而异）")
    edge: EdgeBrief | None = Field(None, description="与专业/行业的连边摘要")
    counts: RelationCounts | None = Field(
        None, description="skill=逻辑技能数；major=对口专业数；industry=归属行业数"
    )
    industries: list[IndustryRef] = Field(
        default_factory=list, description="归属行业（多选）；原型可取首项"
    )
    industry_id: str | None = Field(None, description="industries[0].id，便于单行业展示")
    industry_name: str | None = Field(None, description="industries[0].name")


class SkillLevelItem(BaseModel):
    level: int | None = Field(None, description="产品等级 1–5（1 了解 → 5 专家），唯一判定依据")
    level_label: str | None = Field(None, description="档位文案，取自 skill_level_meta")
    node_id: str | None = Field(None, description="对应的图节点 id")
    description: str | None = Field(None, description="描述")
    status: str | None = Field(None, description="状态")
    weight: float | None = Field(None, description="权重")


class SkillOut(BaseModel):
    """技能：默认逻辑技能 bundle（多 L 已聚合）；view=level 时为单档扁平行。"""

    id: str = Field(..., description="bundle:{region}:{skill_key} 或 skill_level 节点 id")
    name: str | None = Field(None, description="名称")
    skill_name: str | None = Field(None, description="技能名（去等级后缀）")
    skill_key: str | None = Field(None, description="聚合主键")
    level_label: AttrScalar = Field(None, description="等级文案或要求档（取自 attrs）")
    status: str | None = Field(
        None,
        description=(
            "bundle 的**线上**状态：published | draft | disabled | mixed。"
            "由 L1–L5 五个档位节点的线上行聚合而来（各档一致取之，否则 mixed）。"
            "前端**不要**在拿不到它时兜底成某个业务状态 —— 把「没数据」渲染成"
            "「草稿」会让 2 万多个技能全被标成有人在改"
        ),
    )
    record_status: str | None = Field(
        None,
        description=(
            "**记录状态 = 最新版本的状态**：任一档有草稿 → `draft`，否则等于 `status`。"
            "管理台列表的状态列显示这个（仅管理台口径返回）"
        ),
    )
    has_draft: bool | None = Field(
        None, description="这个技能有没有未发布的改动（任一档有草稿行即为 true）"
    )
    draft_change: str | None = Field(
        None, description="草稿改的是什么：node=档位节点字段 | edges=关联 | both"
    )
    version: int | None = Field(
        None,
        description=(
            "版本号。bundle 没有自己的行，取 L1–L5 线上行的**最大** version。"
            "仅管理台口径返回；拿不到时前端应显示「—」，"
            "**不要兜底成 V1** —— 那是个看起来像真值的假版本号"
        ),
    )
    version_label: str | None = Field(None, description="版本展示文案，如 `V3`")
    owner: str | None = Field(None, description="业务负责人 user-id（各档第一个非空）")
    owner_name: str | None = Field(None, description="业务负责人姓名")
    updated_by_name: str | None = Field(None, description="最近修改人姓名")
    type: str = Field("skill", description="类型")
    kg_type: str | None = Field(None, description="图侧节点类型")
    region: str | None = Field(None, description="地区，如 CN")
    desc: str | None = Field(None, description="简介")
    required_level: int | None = Field(None, description="岗位要求档 L1–L5（int）")
    weight: float | None = Field(None, description="岗位要求权重（0–1 小数）")
    weight_pct: int | None = Field(
        None,
        description=(
            "权重百分比，直接展示用（学员端岗位详情的「权重」列读的就是这个）。"
            "**上游一直在算，只是这里没声明** —— Pydantic 把它丢了，"
            "于是学员端每一行权重都显示「-」，学员判断不出哪个技能更重要"
        ),
    )
    is_core: bool | None = Field(
        None, description="核心技能标记（权重 ≥ 30%）；同样是上游已算、这里补声明"
    )
    source_url: str | None = Field(None, description="来源链接")
    attrs: dict[str, Any] | None = Field(None, description="自由属性（无数据库约束的 JSON 列，键随数据来源而异）")
    edge: EdgeBrief | None = Field(
        None, description="与岗位/专业的连边摘要——岗位要求的权重就在这里"
    )
    levels: list[SkillLevelItem] = Field(
        default_factory=list, description="已有等级节点（聚合视图）"
    )
    level_descriptions: dict[str, str] = Field(
        default_factory=dict, description="L1–L5 能力描述文案"
    )
    available_levels: list[int] = Field(default_factory=list, description="已配齐的档位 1–5")
    missing_levels: list[int] = Field(default_factory=list, description="尚缺的档位 1–5")
    counts: RelationCounts | None = Field(None, description="关联计数")


class ProfessionListOut(PageMeta):
    items: list[ProfessionOut] = Field(..., description="当前页数据")


class PositionListOut(PageMeta):
    items: list[PositionOut] = Field(..., description="当前页数据")


class SkillListOut(PageMeta):
    items: list[SkillOut] = Field(..., description="当前页数据")
    view: str | None = Field(None, description="bundle | level")


class IndustryItem(BaseModel):
    id: str = Field(..., description="节点 id")
    name: str | None = Field(None, description="名称")
    code: str | None = Field(None, description="编码")
    level: int | str | None = Field(None, description="等级")
    parent_code: str | None = Field(None, description="父级编码")
    desc: str | None = Field(None, description="简介")
    counts: RelationCounts | None = Field(
        None, description="major / occupation 直连计数"
    )


class IndustryListOut(PageMeta):
    items: list[IndustryItem] = Field(..., description="当前页数据")


class LadderStep(BaseModel):
    tier: int = Field(..., description="阶梯层级 1..n")
    position_id: str = Field(..., description="岗位节点 id")
    position_name: str | None = Field(None, description="岗位名")
    position: PositionOut | None = Field(None, description="岗位详情")


class ProfessionDetailOut(BaseModel):
    profession: ProfessionOut
    positions: list[PositionOut] = Field(default_factory=list, description="对口岗位")
    ladder: list[LadderStep] = Field(default_factory=list, description="成长阶梯")


class PositionDetailOut(BaseModel):
    position: PositionOut = Field(..., description="岗位详情")
    skills: list[SkillOut] = Field(default_factory=list, description="岗位技能要求")


class GoalOut(BaseModel):
    user_id: str = Field(..., description="UC 用户 id")
    user_name: str | None = Field(None, description="用户名（冗余字段，用户中心不在本服务）")
    occupation_id: str | None = Field(None, description="目标岗位 id")
    occupation_name: str | None = Field(None, description="目标岗位名")
    major_id: str | None = Field(None, description="关联专业 id")
    major_name: str | None = Field(None, description="关联专业名")
    industry_id: str | None = Field(None, description="所属行业 id")
    industry_name: str | None = Field(None, description="所属行业名")
    updated_at: str | None = Field(None, description="更新时间 ISO8601")


class GoalPutBody(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "occupation_id": "CN:occupation:…",
                    "major_id": "CN:major:…",
                }
            ]
        }
    )

    occupation_id: str = Field(..., description="目标岗位节点 id（必填）")
    major_id: str | None = Field(None, description="可选：关联专业 id")


class SkillLevelMeta(BaseModel):
    level: int = Field(..., description="1–5")
    name: str = Field(..., description="了解/掌握/熟练/精通/专家")
    base_score: int = Field(..., description="基准分")


class SkillCategory(BaseModel):
    id: str = Field(..., description="节点 id")
    name: str = Field(..., description="名称")


class ResumeDiagBody(BaseModel):
    content_text: str = Field(
        ...,
        min_length=1,
        description="简历正文（粘贴文本）。文件上传后续可扩展 multipart",
    )
    target_occupation_id: str | None = Field(
        None, description="目标岗位 id；不传则用已设 goal 或不对标岗位"
    )


class ChatSessionBody(BaseModel):
    target_occupation_id: str | None = Field(None, description="对话诊断目标岗位")


class ChatMessageBody(BaseModel):
    content: str = Field(..., min_length=1, description="学员回复内容")


class ParsedUserSkill(BaseModel):
    """从简历/对话文本里解析出的一项学员技能。"""

    skill_name: str = Field(..., description="技能名")
    level: int = Field(2, ge=1, le=5, description="推断档位 1–5；关键词命中时默认 2")
    score: int = Field(0, description="推断得分")
    source: str | None = Field(None, description="来源：resume / chat / llm / rule")


class RequiredSkillRef(BaseModel):
    """岗位要求的一项技能。"""

    id: str | None = Field(None, description="技能节点或 bundle id")
    skill_name: str | None = Field(None, description="技能名")
    skill_key: str | None = Field(None, description="技能聚合主键")
    required_level: int | None = Field(None, ge=1, le=5, description="要求档 1–5")
    weight: float | None = Field(None, description="权重")
    category: str | None = Field(None, description="技能大类")


class SimpleRadar(BaseModel):
    """单系列雷达图：按技能大类聚合的达成率。"""

    categories: list[str] = Field(default_factory=list, description="各轴名称（技能大类）")
    scores: list[int] = Field(
        default_factory=list, description="各轴达成率 0–100，顺序与 categories 一致"
    )


class DiagnosisReportOut(BaseModel):
    """诊断报告（简历 / 对话渠道）。

    与测评报告 `AssessmentReportOut` 的区别在证据强度：那边每项技能都有实测档位
    与要求档位的对照，这边只是从文本里解析出的技能名与岗位要求做匹配。
    """

    user_id: str | None = Field(None, description="UC 用户 id")
    channel: str | None = Field(None, description="诊断渠道：resume / chat / profile")
    target_occupation_id: str | None = Field(None, description="目标岗位节点 id")
    target_occupation_name: str | None = Field(None, description="目标岗位名")
    match_score: float | None = Field(None, description="匹配度 0–100")
    user_skills: list[ParsedUserSkill] = Field(
        default_factory=list, description="从简历/对话解析出的学员技能"
    )
    required_skills: list[RequiredSkillRef] = Field(
        default_factory=list, description="岗位要求的技能"
    )
    gaps: list[RequiredSkillRef] = Field(
        default_factory=list, description="缺口技能（岗位要求但学员未体现）"
    )
    radar: SimpleRadar = Field(default_factory=SimpleRadar, description="雷达图数据")
    summary: str | None = Field(None, description="一句话结论")


class ResourceItem(BaseModel):
    id: str = Field(..., description="节点 id")
    title: str | None = Field(None, description="标题")
    type: str | None = Field(None, description="video|practice|article|course…")
    status: int | None = Field(None, description="状态")
    provider: str | None = Field(None, description="提供方")
    url: str | None = Field(None, description="链接")
    skill_hint: str | None = Field(None, description="技能提示")
    desc: str | None = Field(None, description="简介")


class ResourceListOut(PageMeta):
    items: list[ResourceItem] = Field(..., description="当前页数据")


class UserGoalBrief(BaseModel):
    """当前学习目标摘要。"""

    user_id: str | None = Field(None, description="UC 用户 id")
    user_name: str | None = Field(None, description="用户名")
    occupation_id: str | None = Field(None, description="目标岗位 id")
    occupation_name: str | None = Field(None, description="目标岗位名")
    major_id: str | None = Field(None, description="关联专业 id")
    major_name: str | None = Field(None, description="关联专业名")
    industry_id: str | None = Field(None, description="所属行业 id")
    industry_name: str | None = Field(None, description="所属行业名")
    status: str | None = Field(None, description="active=活跃；archived=历史")
    created_at: str | None = Field(None, description="设定时间 ISO8601")
    updated_at: str | None = Field(None, description="更新时间 ISO8601")


class UserBadgeOut(BaseModel):
    """已解锁的成就（成就定义 + 解锁时间）。"""

    code: str = Field(..., description="成就编码")
    name: str = Field(..., description="成就名")
    description: str | None = Field(None, description="成就说明")
    points: int = Field(0, description="该成就的成长值")
    category: str | None = Field(None, description="成就分类")
    unlocked_at: str | None = Field(None, description="解锁时间 ISO8601")


class UserSkillOut(BaseModel):
    """学员技能画像的一项（对应 biz_user_skill 一行）。"""

    user_id: str | None = Field(None, description="UC 用户 id")
    skill_id: str | None = Field(None, description="技能 id 或 skill_key")
    skill_name: str | None = Field(None, description="技能名")
    level: int = Field(1, ge=1, le=5, description="档位 1–5")
    score: int = Field(0, description="得分")
    source: str = Field("self", description="来源：self=自评；assessment=测评；resume=简历解析")
    updated_at: str | None = Field(None, description="更新时间 ISO8601")


class MeOut(BaseModel):
    """学员个人首页数据。"""

    user_id: str = Field(..., description="UC 用户 id")
    user_name: str | None = Field(None, description="用户名")
    goal: UserGoalBrief | None = Field(None, description="当前学习目标；未锁定为 null")
    points: int = Field(0, description="成长值/积分")
    badges: list[UserBadgeOut] = Field(default_factory=list, description="已解锁成就")
    skills: list[UserSkillOut] = Field(default_factory=list, description="技能画像")


class BadgeDefOut(BaseModel):
    code: str = Field(..., description="编码")
    name: str = Field(..., description="名称")
    description: str | None = Field(None, description="描述")
    points: int = Field(..., description="成长值/积分")
    category: str | None = Field(None, description="分类")


class AdminDashboardOut(BaseModel):
    kg_nodes: int | None = Field(None, description="图节点总数")
    kg_edges: int | None = Field(None, description="图边总数")
    nodes_by_type: dict[str, int] = Field(default_factory=dict, description="各类型节点数，键为节点类型")
    users_with_goal: int = Field(0, description="已锁定学习目标的用户数")
    diagnosis_sessions: int = Field(0, description="诊断会话数")
    learning_plans_pushed: int = Field(
        0, description="已成功推送到学习空间的学习计划数（路径本体不在本服务）"
    )
    pending_proposals: int = Field(0, description="待审提案数")
