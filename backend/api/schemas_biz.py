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

    major: int = 0
    occupation: int = 0
    skill: int = Field(0, description="逻辑技能数（DISTINCT skill_key），非 L 扁平行数")
    industry: int = 0
    course: int = 0
    level: int = Field(0, description="技能 bundle 下已有 L 档数")


class IndustryRef(BaseModel):
    id: str
    name: str | None = None


class ProfessionOut(BaseModel):
    """专业（产品口径 profession，图侧 type=major）。"""

    id: str = Field(..., description="节点 id")
    name: str | None = Field(None, description="展示名")
    raw_name: str | None = Field(None, description="原始 name")
    type: str = Field("profession", description="固定 profession")
    kg_type: str | None = Field(None, description="图节点类型 major")
    region: str | None = None
    status: int | None = Field(None, description="1=已发布 0=草稿 2=停用（映射）")
    desc: str | None = Field(None, description="简介")
    industry: str | None = Field(None, description="行业/门类提示")
    code: str | None = Field(None, description="专业代码")
    level: str | None = None
    level_zh: str | None = None
    source_url: str | None = None
    attrs: dict[str, Any] | None = None
    counts: RelationCounts | None = Field(
        None, description="occupation=对口岗数；skill=关联逻辑技能数"
    )


class PositionOut(BaseModel):
    """岗位（产品 position，图侧 occupation）。"""

    id: str
    name: str | None = None
    raw_name: str | None = None
    type: str = "position"
    kg_type: str | None = None
    region: str | None = None
    status: int | None = None
    desc: str | None = None
    tier: str | int | None = Field(None, description="职级/推荐档")
    demand: str | None = None
    salary: str | None = None
    source_url: str | None = None
    attrs: dict[str, Any] | None = None
    edge: dict[str, Any] | None = Field(None, description="与专业/行业连边摘要")
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
    node_id: str | None = None
    description: str | None = None
    status: str | None = None
    weight: float | None = None


class SkillOut(BaseModel):
    """技能：默认逻辑技能 bundle（多 L 已聚合）；view=level 时为单档扁平行。"""

    id: str = Field(..., description="bundle:{region}:{skill_key} 或 skill_level 节点 id")
    name: str | None = None
    skill_name: str | None = Field(None, description="技能名（去等级后缀）")
    skill_key: str | None = Field(None, description="聚合主键")
    level_label: str | None = Field(None, description="等级文案或要求档")
    type: str = "skill"
    kg_type: str | None = None
    region: str | None = None
    desc: str | None = None
    required_level: int | None = Field(None, description="岗位要求档 L1–L5（int）")
    weight: float | None = Field(None, description="岗位要求权重")
    source_url: str | None = None
    attrs: dict[str, Any] | None = None
    edge: dict[str, Any] | None = None
    levels: list[SkillLevelItem] = Field(
        default_factory=list, description="已有等级节点（聚合视图）"
    )
    level_descriptions: dict[str, str] = Field(
        default_factory=dict, description="L1–L5 能力描述文案"
    )
    available_levels: list[int] = Field(default_factory=list, description="已配齐的档位 1–5")
    missing_levels: list[int] = Field(default_factory=list, description="尚缺的档位 1–5")
    counts: RelationCounts | None = None


class ProfessionListOut(PageMeta):
    items: list[ProfessionOut]


class PositionListOut(PageMeta):
    items: list[PositionOut]


class SkillListOut(PageMeta):
    items: list[SkillOut]
    view: str | None = Field(None, description="bundle | level")


class IndustryItem(BaseModel):
    id: str
    name: str | None = None
    code: str | None = None
    level: int | str | None = None
    parent_code: str | None = None
    desc: str | None = None
    counts: RelationCounts | None = Field(
        None, description="major / occupation 直连计数"
    )


class IndustryListOut(PageMeta):
    items: list[IndustryItem]


class LadderStep(BaseModel):
    tier: int = Field(..., description="阶梯层级 1..n")
    position_id: str
    position_name: str | None = None
    position: PositionOut | None = None


class ProfessionDetailOut(BaseModel):
    profession: ProfessionOut
    positions: list[PositionOut] = Field(default_factory=list, description="对口岗位")
    ladder: list[LadderStep] = Field(default_factory=list, description="成长阶梯")


class PositionDetailOut(BaseModel):
    position: PositionOut
    skills: list[SkillOut] = Field(default_factory=list, description="岗位技能要求")


class GoalOut(BaseModel):
    user_id: str
    user_name: str | None = None
    occupation_id: str | None = Field(None, description="目标岗位 id")
    occupation_name: str | None = None
    major_id: str | None = Field(None, description="关联专业 id")
    major_name: str | None = None
    industry_id: str | None = None
    industry_name: str | None = None
    updated_at: str | None = None


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
    id: str
    name: str


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


class DiagnosisReportOut(BaseModel):
    user_id: str | None = None
    channel: str | None = Field(None, description="resume|chat|profile")
    target_occupation_id: str | None = None
    target_occupation_name: str | None = None
    match_score: float | None = Field(None, description="匹配度 0–100")
    user_skills: list[dict[str, Any]] = Field(default_factory=list)
    required_skills: list[dict[str, Any]] = Field(default_factory=list)
    gaps: list[dict[str, Any]] = Field(default_factory=list, description="缺口技能")
    radar: dict[str, Any] = Field(default_factory=dict, description="雷达图数据")
    summary: str | None = None


class PathGenerateBody(BaseModel):
    occupation_id: str | None = Field(
        None, description="岗位 id；不传则用当前 goal"
    )


class LearningStepOut(BaseModel):
    id: int
    path_id: int
    seq: int
    kind: str
    skill_id: str | None = None
    skill_name: str | None = None
    resource_id: str | None = None
    resource_title: str | None = None
    title: str
    status: str
    completed_at: str | None = None


class LearningPathOut(BaseModel):
    path: dict[str, Any] = Field(..., description="路径主体：目标岗位、状态、来源")
    steps: list[dict[str, Any]] = Field(
        ...,
        description=(
            "全部任务（扁平，按 seq）。每项含 stage/stage_title/category/weight/"
            "duration_min/required_level/skill_name/status"
        ),
    )
    stages: list[dict[str, Any]] | None = Field(
        None,
        description=(
            "阶段任务树（原型「第一/二/三阶段」）：按技能大类分组，"
            "阶段顺序沿用国标职业功能推进顺序。"
            "每组含 stage/title/steps[]/stage_weight_pct（阶段权重%）/completed/total/duration_min"
        ),
    )
    progress: dict[str, Any] | None = Field(
        None,
        description=(
            "进度。completed/total/ratio 为按任务条数；"
            "**weighted_pct 为按权重计的总进度**（原型顶部「35% 完成权重/总权重」用这个）；"
            "duration_min_total 为建议总耗时"
        ),
    )


class ResourceItem(BaseModel):
    id: str
    title: str | None = None
    type: str | None = Field(None, description="video|practice|article|course…")
    status: int | None = None
    provider: str | None = None
    url: str | None = None
    skill_hint: str | None = None
    desc: str | None = None


class ResourceListOut(PageMeta):
    items: list[ResourceItem]


class MeOut(BaseModel):
    user_id: str
    user_name: str | None = None
    goal: dict[str, Any] | None = Field(None, description="当前学习目标")
    points: int = Field(0, description="成长值/积分")
    badges: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[dict[str, Any]] = Field(default_factory=list, description="技能画像")
    active_path_id: int | None = None


class BadgeDefOut(BaseModel):
    code: str
    name: str
    description: str | None = None
    points: int
    category: str | None = None


class AdminDashboardOut(BaseModel):
    kg_nodes: int | None = None
    kg_edges: int | None = None
    nodes_by_type: dict[str, int] = Field(default_factory=dict)
    users_with_goal: int = 0
    diagnosis_sessions: int = 0
    learning_paths: int = 0
    pending_proposals: int = 0
