"""学员端契约：岗位匹配、学习目标、学习计划、画像、诊断。

与 `schemas_biz.py` 的分工：那边是四维图谱的通用出参（ProfessionOut / PositionOut /
SkillOut 等，管理台也在用），这里是**学员端独有的业务出参**。

写在这里的每个字段都对应服务端实际返回的键，类型和注释以生产代码为准，
不是「大概应该是这样」。有几处刻意不收紧：`attrs` 是无约束 JSON 列，
`raw` 是外部画像服务的原样透传——它们的自由是事实，注释里写清楚为什么。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.api.schemas_assessment import AssessmentReportOut
from backend.api.schemas_biz import (
    GoalProgressionOut,
    PageMeta,
    ProgressionNodeOut,
    SkillOut,
)

# ── 岗位匹配度 ───────────────────────────────────────────────


class OccupationBrief(BaseModel):
    """岗位摘要（匹配度、总览等处的公共引用）。"""

    id: str | None = Field(None, description="岗位节点 id")
    name: str | None = Field(None, description="岗位名")
    level: int | None = Field(None, description="岗位职级/层级")


class MatchItem(BaseModel):
    """匹配度明细的一项技能：岗位要求 vs 学员已有。"""

    skill_key: str = Field(..., description="技能聚合主键")
    skill_name: str | None = Field(None, description="技能展示名")
    category: str | None = Field(None, description="技能大类 code，见 /v1/kg/skill-categories")
    category_name: str | None = Field(None, description="技能大类展示名（由 code 派生，不入库）")
    required_level: int | None = Field(
        None,
        ge=0,
        le=5,
        description=(
            "岗位要求档 1–5；**0 表示该技能没有指定要求档**（边没指向带产品档的等级节点，"
            "或档位是越界脏值），此时 `scorable=false`、该项不计入匹配度"
        ),
    )
    user_level: int | None = Field(
        None,
        ge=0,
        le=5,
        description="学员已有档 1–5；**0 表示该技能无任何证据**（不是「水平为零」），null 同义",
    )
    weight: float = Field(..., description="该技能在岗位中的权重，Σ≈1")
    is_core: bool | None = Field(None, description="是否核心技能")
    ratio: float = Field(
        ...,
        description=(
            "达成比 = 已有/要求，封顶 1.0。`scorable=false` 时这里是「有证据即 1.0」的"
            "展示值，**不参与匹配度计算**"
        ),
    )
    ok: bool = Field(..., description="是否达标（已有 ≥ 要求）；无要求档时恒为 false")
    scorable: bool = Field(
        True,
        description=(
            "该项能否评分。false = 岗位要求档缺失或越界（无基准），分子分母都不计入 "
            "match_score，也不进 strengths / gaps；汇总在 `no_baseline` 里"
        ),
    )
    matched_by: str | None = Field(
        None,
        description="命中方式：exact=技能名精确匹配；fuzzy=模糊匹配；none=无证据",
    )


class MatchRadarOut(BaseModel):
    """匹配度的雷达图。

    与测评报告的 `RadarOut` 不同：那边是「学员实测 vs 岗位要求」双系列，
    这里只有一条达成率曲线——匹配度算的是差距比例，没有独立的「要求」系列。
    """

    categories: list[str] = Field(default_factory=list, description="各轴名称（技能大类）")
    scores: list[int] = Field(
        default_factory=list, description="各轴达成率 0–100，顺序与 categories 一致"
    )


class PositionMatchOut(BaseModel):
    """岗位匹配度。

    `source` 回答的是「展示这个分数时最该先提醒学员什么」，前端必须据此区分展示。
    前三档是证据强度（级联命中即返回），后四档是「这个数不能当结论」的原因。
    每档的建议展示文案与 `frontend/student.html` 的 `MATCH_SOURCE_LABEL` 保持一致：

    - `diagnosis` —— 该岗位做过完整测评，分数直接取自报告，最准。有分数；
      配文「实测 · 你诊断过这个岗位」
    - `assessment` —— 用**其他场**测评实测到的档位现算（技能有重叠），`estimated=true`。
      有分数；配文「推算 · 用你在其他岗位测出的技能比对」
    - `memory` —— 用五维记忆画像推断，证据最弱，`estimated=true`。有分数；
      配文「预估 · 由你的能力画像推断」
    - `partial_baseline` —— 该岗位**缺要求档的技能权重超过 30%**，分数只依据已配置的
      那部分算出。**分数照给**（那部分是真实依据，丢掉更亏）；配文「参考 · 该岗位
      {no_baseline_weight}% 的能力要求待完善，分数仅供参考」，且不要拿它跨岗位排序
    - `no_overlap` —— 岗位技能与已有证据零交集。`match_score` 为 **null**；
      配文「该岗位要求的技能你还没测过」+ 引导做一次 AI 诊断
    - `no_baseline` —— **该岗位一项技能都没配要求档**，没有基准就算不出达标率。
      `match_score` 为 **null**；配文「该岗位能力标准待完善，暂时无法评分」。
      这是数据缺口（库内 80% 的岗位目前如此）、不是学员的问题：文案别说
      「你的画像没覆盖」，**也别引导去做诊断**——学员做了照样算不出来，
      缺的是运营配置，等岗位标准补齐即可
    - `none` —— 该岗位尚未配置技能构成，或该学员无任何证据。`match_score` 为 **null**；
      配文「尚无评估依据」+ 引导做一次 AI 诊断

    前端接入须知
    ------------
    - **`match_score` 可空**。`null` ≠ `0`：0 是「测过但一项都没达标」的结论，
      null 是「没有评分依据」。`?? 0` 会把后者渲染成前者，学员读成「完全不匹配」。
      建议 null 时保留分数的位置与字号，用虚线框 +「? %」占位（`.score.unknown`），
      旁边给上面列出的对应说明文案。
    - `partial_baseline` / `no_baseline` 的阈值与判定**全在服务端**
      （`config.PARTIAL_BASELINE_PCT` = 30%）：前端不要自己拿 `no_baseline_weight`
      比阈值，每个页面各定一次迟早不一致。
    - `partial_baseline` 时证据强度并没有丢：`estimated`（实测 or 推断）、
      `diagnosis`（测过没测过）、`profile` 都还在，需要时可以叠加显示。
    """

    occupation: OccupationBrief = Field(..., description="岗位摘要")
    match_score: float | None = Field(
        None,
        description=(
            "匹配度 0–100。**可空**：无证据（no_overlap / none）或无基准（no_baseline）"
            "时为 null。**为 null 时不要显示 0%、也不要 `?? 0` 兜底** —— 0% 是「测过但"
            "一项都没达标」的结论，null 是「没有评分依据」，学员会把前者读成「我完全不行」。"
            "建议用虚线框 +「? %」占位，并展示 source 对应的说明文案"
        ),
    )
    source: Literal[
        "diagnosis", "assessment", "memory",
        "partial_baseline", "no_overlap", "no_baseline", "none",
    ] = Field(..., description="展示这个分数时最该先提醒什么；每个取值的含义与建议文案见模型说明")
    score_status: str | None = Field(
        None,
        description=(
            "算分结果的机器可读原因，取值与 `AssessmentReportOut.score_status` 同一套"
            "（含含义与建议文案的表格见那边）："
            "ok / partial_baseline / no_skills / no_baseline / no_weight / no_evidence。"
            "`source` 描述的是「这个数最该配什么提示」，这里描述的是「为什么算得出/算不出」；"
            "两者共用同一个 30% 阈值，不会一个说 partial 一个说 ok。"
            "**刻意不声明成枚举**：历史数据与降级分支可能带来词表外的值，"
            "响应模型不该为此整页 500"
        ),
    )
    estimated: bool = Field(
        False,
        description=(
            "是否为推断值：`assessment` / `memory` 来源为 true（拿已有画像现算），"
            "`diagnosis` 为 false（该岗位实测）。UI 上应与实测分区分；"
            "`source=partial_baseline` 盖住了证据强度时，它是「实测还是推断」的唯一依据"
        ),
    )
    reason: str | None = Field(
        None,
        description=(
            "无法计算、或分数只能当参考时的原因说明（可直接展示给学员）。"
            "`source=partial_baseline` 时这里会写明缺了多少权重的要求档"
        ),
    )
    skill_total: int | None = Field(None, ge=0, description="岗位技能总数")
    matched_count: int | None = Field(None, ge=0, description="已达标技能数")
    covered_count: int | None = Field(
        None, ge=0, description="有证据覆盖的技能数；为 0 时 match_score 无意义"
    )
    coverage: float | None = Field(
        None,
        description="证据覆盖的权重百分比 0–100；分母是**可评分**技能权重（不含缺要求档的项）",
    )
    no_baseline_weight: float | None = Field(
        None,
        description=(
            "因缺要求档而无法评分的技能权重占岗位**全部**技能权重的百分比 0–100；"
            "100 表示整个岗位没配能力要求。**超过 30% 时服务端已把 source 与 "
            "score_status 降级成 partial_baseline**，前端不必自己比阈值，"
            "但展示「仅供参考」时可以把这个数字带出来。与 coverage 是两种不同的缺失"
        ),
    )
    items: list[MatchItem] = Field(default_factory=list, description="全部技能明细")
    strengths: list[MatchItem] = Field(default_factory=list, description="已达标项（仅可评分项）")
    gaps: list[MatchItem] = Field(
        default_factory=list, description="未达标项（仅可评分项），按权重降序"
    )
    no_baseline: list[MatchItem] = Field(
        default_factory=list,
        description=(
            "无法评分的技能（岗位要求档缺失或越界），按权重降序；"
            "既不在 strengths 也不在 gaps。需要运营补要求档"
        ),
    )
    radar: MatchRadarOut | None = Field(
        None, description="单系列雷达图（按技能大类聚合的达成率）；无数据时为空对象"
    )
    diagnosis: "DiagnosedBrief | None" = Field(
        None, description="该岗位的历史诊断摘要；没测过为 null"
    )


class DiagnosedBrief(BaseModel):
    """某岗位的历史诊断摘要。"""

    match_score: float = Field(..., description="诊断得出的匹配度 0–100")
    session_id: int | None = Field(None, description="诊断会话 id")
    channel: str | None = Field(None, description="诊断渠道：assessment / resume / chat")
    diagnosed_at: str | None = Field(None, description="诊断时间 ISO8601")


# ── 技能构成 ─────────────────────────────────────────────────


class SkillCompositionOut(BaseModel):
    """岗位技能构成（逻辑技能 + 边权重）。

    权重只认 `requires` 边上的 weight，节点 `attrs.weight_pct` 仅历史兼容。
    """

    occupation: OccupationBrief = Field(..., description="岗位摘要")
    skills: list[SkillOut] = Field(..., description="逻辑技能列表（多档已聚合成 bundle）")
    skill_count: int = Field(..., ge=0, description="技能数")
    weight_sum: float = Field(
        ..., description="权重之和，**小数**；归一化后应≈1.0，不要声明成 int"
    )
    weighted_skill_count: int = Field(..., ge=0, description="带权重的技能数")
    weight_sum_ok: bool = Field(
        ..., description="权重和是否在容差内（0.85–1.15）；false 表示该岗位权重待归一化"
    )
    note: str | None = Field(None, description="口径说明")


# ── 岗位晋升链路 ──────────────────────────────────────────────


class SkillGapOut(BaseModel):
    """进阶要补的一项技能：目标岗位要求里，当前岗位没有的 / 要求更高的。"""

    skill_key: str = Field(..., description="技能聚合主键（ASCII code）")
    skill_name: str | None = Field(None, description="展示名 —— **页面上要显示这个**。`skill_key` 从 2026-08-19 起是 ASCII code（形如 SK0123456789），拿它渲染就是一串哈希")
    category: str | None = Field(None, description="技能大类 code，见 /v1/kg/skill-categories")
    category_name: str | None = Field(None, description="技能大类展示名（由 code 派生，不入库）")
    required_level: int | None = Field(
        None, ge=1, le=5, description="目标岗位要求的档位 1–5"
    )
    required_label: str | None = Field(
        None, description="档位名称，从 skill_level_meta 读，前端不要硬编码"
    )
    current_required_level: int | None = Field(
        None, ge=1, le=5, description="当前岗位对该技能的要求档；null 表示当前岗位根本不要求"
    )
    weight: float | None = Field(
        None, description="该技能在目标岗位能力结构中的权重，**小数**"
    )


class ProgressionOccBrief(BaseModel):
    """晋升链路上的一个岗位节点。"""

    id: str = Field(..., description="岗位节点 id")
    name: str | None = Field(None, description="岗位名")
    level: int | None = Field(None, description="职级，来自 `attrs.level`（唯一真源）")
    level_code: str | None = Field(
        None, description="职级代号，由 level **派生**不入库（双写会不一致）"
    )
    level_name: str | None = Field(None, description="职级中文名，同样派生")
    description: str | None = Field(None, description="岗位描述")


class ProgressionHop(BaseModel):
    """路径上的一跳。"""

    from_: ProgressionOccBrief = Field(..., alias="from", description="起点岗位")
    to: ProgressionOccBrief = Field(..., description="终点岗位")
    direction: str = Field(
        ..., description="方向：本方向纵深 / 技术纵深 / 管理路线 / 跨方向转型 / 向上发展"
    )
    confidence: str | None = Field(None, description="置信度，晋升边目前均为 ai_inferred")
    evidence: str | None = Field(None, description="判定依据（LLM 给出）")
    unlock_skills: list[SkillGapOut] = Field(
        default_factory=list, description="进阶要补的技能（最多 6 项，按权重倒序）"
    )

    model_config = {"populate_by_name": True}


class ProgressionPath(BaseModel):
    """一条完整晋升路径，可能多跳。"""

    target: ProgressionOccBrief = Field(..., description="路径终点岗位")
    direction: str = Field(..., description="首跳的方向，用作 tab 标题的分类")
    depth: int = Field(..., ge=1, description="跳数")
    hops: list[ProgressionHop] = Field(..., description="逐跳明细")


class PositionProgressionsOut(BaseModel):
    """岗位晋升链路（GET /v1/student/positions/progressions）。

    独立于岗位详情与技能构成接口，加卡片不动既有契约。

    `advances_to` 是 **1:N**：一个岗位可以有多条向上路径（本方向纵深 / 管理路线 /
    跨方向转型）。早期本体误定为 1:1，读路径 `LIMIT 1`，于是 Java 有三条向上路径
    却只显示「全栈工程师」一条。
    """

    occupation: ProgressionOccBrief = Field(..., description="查询的岗位")
    paths: list[ProgressionPath] = Field(..., description="全部晋升路径，长链在前")
    path_count: int = Field(..., ge=0, description="路径条数")
    truncated: bool = Field(
        False, description="是否因超出上限被截断（枢纽岗位可展开出上百条）"
    )
    note: str | None = Field(None, description="口径说明")


# ── 岗位相关课程 ──────────────────────────────────────────────


class CourseResourceOut(BaseModel):
    """一门课程资源。`kind` 决定前端怎么展示，别把两类混在一起计数。"""

    id: str = Field(..., description="课程节点 id")
    name: str = Field(..., description="课程名")
    url: str | None = Field(None, description="课程/检索页地址，可直接点开")
    search_url: str | None = Field(
        None,
        description=(
            "仅 `kind=catalog` 有值：按课程名生成的慕课检索地址。"
            "课标条目自身的 `url` 是教育部专业教学标准的**大类目录页**（与技能无关），"
            "真要展示时请用这个而不是 `url`"
        ),
    )
    platform: str | None = Field(None, description="来源系统，如 ICOURSE163 / XUETANGX")
    platform_label: str | None = Field(None, description="平台中文名，直接展示用")
    kind: Literal["real", "enroll", "catalog", "landing"] = Field(
        ...,
        description=(
            "资源性质，决定前端怎么展示，**只有 real 是点开当场能学的**：\n\n"
            "- `real`：免登录、免报名、无开课周期，点开就能学\n"
            "- `enroll`：是真课程，但**要登录报名**或按学期开课，往期只剩介绍页"
            "（中国大学MOOC / 学堂在线）\n"
            "- `catalog`：教育部课标里的课目条目（`role=curriculum_catalog`），"
            "点开是专业培养方案目录，**没有课程内容**\n"
            "- `landing`：检索入口，点开是搜索结果页\n\n"
            "判定以课程节点的 `attrs.role` 为准，不是按 source_system —— "
            "曾把 MOE_CN 当成真课，学员点开是课标目录页；也曾把 MOOC 归进 real，"
            "学员点开是报名墙。"
        ),
    )
    learner_count: int | None = Field(
        None, description="学习/选课人数，质量信号；landing 类无此值"
    )
    school: str | None = Field(None, description="开课院校/机构")
    img_url: str | None = Field(None, description="封面图")


class PositionCourseSkillGroup(BaseModel):
    """按技能分组的课程。技能顺序按岗位 requires 权重倒序。"""

    skill_key: str = Field(
        ..., description="技能聚合主键（ASCII code，形如 SK0123456789）"
    )
    skill_name: str | None = Field(
        None,
        description=(
            "技能展示名 —— **页面上要显示这个**。原来这里只有 `skill_key`，"
            "且描述写的是「逻辑技能名」，那是 2026-08-19 之前 key 就是中文名时的遗留"
        ),
    )
    required_level: int | None = Field(
        None, ge=1, le=5, description="该岗位要求的档位 1–5，来自 attrs.level"
    )
    weight: float | None = Field(
        None, description="该技能在岗位能力结构中的权重，**小数**（同岗位 Σ≈1）"
    )
    category: str | None = Field(None, description="技能大类 code，见 /v1/kg/skill-categories")
    category_name: str | None = Field(None, description="技能大类展示名（由 code 派生，不入库）")
    courses: list[CourseResourceOut] = Field(default_factory=list, description="课程列表")


class PositionCoursesOut(BaseModel):
    """岗位相关课程（GET /v1/student/positions/courses）。

    独立于岗位详情与技能构成接口：详情接口不含课程，避免为了加卡片改动既有契约。
    """

    occupation: OccupationBrief = Field(..., description="岗位摘要")
    by_skill: list[PositionCourseSkillGroup] = Field(..., description="按技能分组的课程")
    skill_count: int = Field(..., ge=0, description="有课程的技能数")
    course_count: int = Field(
        ..., ge=0, description="课程总数（real + enroll + catalog + landing）"
    )
    real_course_count: int = Field(
        ..., ge=0, description="点开当场能学的资源数，**看这个判断资源是否可用**"
    )
    enroll_count: int = Field(
        0, ge=0, description="需报名/有开课周期的课程数（MOOC 类），不计入 real"
    )
    catalog_count: int = Field(
        0, ge=0, description="课标目录条目数（点开是培养方案，不是课程）"
    )
    catalog_hidden: bool = Field(
        False,
        description="课标条目是否被隐藏（默认 true 且 catalog_count>0 时）。用于提示「这里还有数据缺口」",
    )
    landing_count: int = Field(..., ge=0, description="检索入口数")
    note: str | None = Field(None, description="口径说明")


# ── 学习目标 ─────────────────────────────────────────────────


class GoalItem(BaseModel):
    """一个学习目标（对应 biz_user_goal 一行）。一人可有多个，其一为活跃。"""

    user_id: str = Field(..., description="UC 用户 id")
    user_name: str | None = Field(None, description="用户名（冗余字段，用户中心不在本服务）")
    occupation_id: str | None = Field(None, description="目标岗位节点 id")
    occupation_name: str | None = Field(None, description="目标岗位名")
    major_id: str | None = Field(None, description="关联专业 id")
    major_name: str | None = Field(None, description="关联专业名")
    industry_id: str | None = Field(None, description="所属行业 id")
    industry_name: str | None = Field(None, description="所属行业名")
    status: str | None = Field(None, description="active=当前活跃目标；archived=历史目标")
    created_at: str | None = Field(None, description="设定时间 ISO8601")
    updated_at: str | None = Field(None, description="更新时间 ISO8601；也是链路的绑定时间")
    progression: GoalProgressionOut | None = Field(
        None,
        description=(
            "绑定的晋升链路。锁定目标时可传 `progression_path` 指定，不传则自动绑"
            "第一条（置信度最高、职级最近的方向一路走到头）；该岗位没有 "
            "`advances_to` 出边时为 null"
        ),
    )


class ClearGoalOut(BaseModel):
    """清除目标的回执。"""

    status: Literal["cleared"] = Field(..., description="固定 cleared")


class NextLevelOut(BaseModel):
    """晋升路径上的下一档岗位。

    ⚠ `unlock_skills` 与 `description` 曾漏在这里没声明，后端明明算好了，
    Pydantic 按模型序列化时**静默丢掉**，前端拿不到就一直显示
    「下一级岗位暂未配置技能构成」—— 数据在库里、逻辑也对，只是没进契约。
    """

    id: str = Field(..., description="岗位节点 id")
    name: str | None = Field(None, description="岗位名")
    level: int | None = Field(None, description="职级")
    level_label: str | None = Field(None, description="职级文案，如 L3")
    description: str | None = Field(None, description="岗位职责描述")
    confidence: str | None = Field(
        None, description="晋升边置信度：official / derived / ai_inferred"
    )
    unlock_skills: list[SkillGapOut] = Field(
        default_factory=list,
        description="进阶要补的关键技能（最多 6 项，按目标岗位权重倒序）",
    )


class GoalOverviewOut(BaseModel):
    """学习目标总览：当前目标 + 岗位详情 + 测评结果 + 晋升路径 + 学习计划 id。

    原型上那张卡片一次就要这些数据，拆成多个接口会让首屏串行等待。
    """

    has_goal: bool = Field(..., description="是否已锁定目标；false 时其余字段多为 null")
    goal: GoalItem | None = Field(None, description="当前活跃目标")
    goals: list[GoalItem] = Field(default_factory=list, description="该用户全部目标（含历史）")
    progression: GoalProgressionOut | None = Field(
        None,
        description=(
            "绑定的晋升链路，与 `goal.progression` 同一份，提到顶层方便前端直接画"
            "「当前 → 下一级 → …」。`next_target` 即默认的下一目标，"
            "`next_levels` 已把它排到首位"
        ),
    )
    progression_stale: bool = Field(
        False,
        description=(
            "绑定的下一跳在当前图里已无对应 `advances_to` 边（边被归档或重跑采集改了"
            "方向）。链路仍按原样返回——用户选过的不该被悄悄改掉——但前端应提示"
            "「该晋升方向已变更，请重新选择」"
        ),
    )
    occupation: "GoalOccupationOut | None" = Field(None, description="目标岗位详情")
    major: "RefOut | None" = Field(None, description="关联专业")
    industry: "RefOut | None" = Field(None, description="所属行业")
    match_score: float | None = Field(
        None,
        description=(
            "该岗位最近一次诊断的匹配度 0–100，取自 `assessment.match_score`。"
            "**null 有两种成因，引导文案不同**：没测过 → 引导「去测评」；"
            "测过但岗位没配能力要求档（`assessment.score_status=\"no_baseline\"`）→ "
            "只能提示「该岗位标准待完善」，再引导测评也算不出分。"
            "两种都不要显示 0%"
        ),
    )
    assessment: AssessmentReportOut | None = Field(
        None, description="最近一次的完整测评报告；没测过为 null"
    )
    next_level: NextLevelOut | None = Field(
        None,
        description=(
            "**兼容字段**：`next_levels` 的第一条（置信度最高、职级最近）。"
            "无 advances_to 边时为 null。新前端请读 `next_levels`"
        ),
    )
    next_levels: list[NextLevelOut] = Field(
        default_factory=list,
        description=(
            "全部向上方向。`advances_to` 是 **1:N** —— 一个岗位可以有多条晋升路径"
            "（本方向纵深 / 管理路线 / 跨方向转型）。要看多跳完整链路用 "
            "`GET /v1/student/positions/progressions`"
        ),
    )
    learning_plan_id: str = Field(
        "", description="学习计划 id（学习空间服务的主键）；尚未生成时为空串"
    )
    learning_plan_created_at: str | None = Field(None, description="学习计划生成时间 ISO8601")


class RefOut(BaseModel):
    """id + name 的轻引用。"""

    id: str | None = Field(None, description="节点 id")
    name: str | None = Field(None, description="展示名")


class GoalOccupationOut(BaseModel):
    """总览里的目标岗位详情。"""

    id: str | None = Field(None, description="岗位节点 id")
    name: str | None = Field(None, description="岗位名")
    level: int | None = Field(None, description="职级")
    level_label: str | None = Field(None, description="职级文案，如 L3")
    description: str | None = Field(None, description="岗位职责描述")
    salary: str | None = Field(None, description="薪资区间（来自 attrs）")
    skill_count: int = Field(0, ge=0, description="该岗位技能数")


# ── 已诊断岗位 ───────────────────────────────────────────────


class DiagnosedOccupationItem(BaseModel):
    """已诊断过的岗位一行。"""

    occupation_id: str | None = Field(None, description="岗位节点 id")
    occupation_name: str | None = Field(None, description="岗位名")
    match_score: float | None = Field(None, description="最近一次匹配度 0–100")
    channel: str | None = Field(None, description="诊断渠道：assessment / resume / chat")
    last_session_id: int | None = Field(None, description="最近一次诊断会话 id")
    session_count: int = Field(0, ge=0, description="累计诊断次数")
    diagnosed_at: str | None = Field(None, description="最近诊断时间 ISO8601")
    goal_status: str | None = Field(
        None, description="该岗位的目标状态：active / archived；从未设为目标则为 null"
    )
    is_active_goal: bool = Field(False, description="是否为当前活跃目标")
    major_name: str | None = Field(None, description="关联专业名")
    goal_created_at: str | None = Field(None, description="设为目标的时间 ISO8601")
    plan_id: str = Field("", description="学习计划 id；未生成为空串")
    plan_created_at: str | None = Field(None, description="学习计划生成时间 ISO8601")


class DiagnosedOccupationListOut(BaseModel):
    """已诊断岗位分页列表。分页下沉到 SQL，不是取回内存再切。"""

    items: list[DiagnosedOccupationItem] = Field(..., description="当前页数据")
    total: int = Field(..., ge=0, description="总条数")
    page: int = Field(..., ge=1, description="页码，从 1 起")
    page_size: int = Field(..., ge=1, description="每页条数")
    pages: int = Field(..., ge=0, description="总页数")


# ── 学习计划 ─────────────────────────────────────────────────


class LearningPlanBody(BaseModel):
    """生成学习计划。

    只需要一个 `session_id`：岗位从该会话取，短板由服务端从诊断报告读——
    不信客户端传的短板列表，那会让「学员看到的计划」和「诊断结论」对不上。
    """

    session_id: int = Field(
        ...,
        ge=1,
        description=(
            "据以生成的诊断会话 id（必填）。学习计划必须基于一次真实诊断："
            "没有诊断结果就没有短板数据，生成出来是一条空路径。\n\n"
            "取值来自测评/简历/对话诊断完成后返回的 `session_id`。"
        ),
    )
    recommend_resources: bool = Field(
        True, description="是否为每个技能挂载可学课程（图谱里有 taught_by/related_to 边时）"
    )


class LearningPlanItem(BaseModel):
    """一条学习计划推送记录（对应 `biz_user_learning_plan` 一行）。

    计划内容不在本服务——本地只留关联与推送状态，进度真源在学习空间服务。
    """

    id: int = Field(..., description="本地记录 id")
    user_id: str = Field(..., description="UC 用户 id")
    occupation_id: str = Field(..., description="目标岗位 id")
    plan_id: str = Field(
        ..., description="学习空间返回的计划 id；推送失败时为空串"
    )
    session_id: int | None = Field(None, description="据以生成的诊断会话 id")
    push_status: Literal["ok", "failed"] | None = Field(
        None,
        description="ok=已成功推送；failed=推送失败，`last_error` 里是原因，可重推",
    )
    last_error: str | None = Field(
        None, description="最近一次推送失败的原因；成功时为 null"
    )
    pushed_at: str | None = Field(None, description="最近一次推送时间 ISO8601")
    created_at: str | None = Field(None, description="创建时间 ISO8601")


class LearningPlanCreatedOut(BaseModel):
    """生成学习计划的回执。

    **没有本地兜底**：推不上去就返回错误码，不再像旧版那样发个假 plan_id。
    学习计划是业务主数据，给假 id 会让学员点进去看到空白页。
    """

    plan_id: str = Field(..., description="学习空间返回的计划 id")
    created: bool = Field(
        ...,
        description=(
            "true=本次新建；false=幂等命中（同一次诊断重复调用，"
            "对方返回已存在的计划，无副作用）"
        ),
    )
    superseded_plan_id: str | None = Field(
        None, description="本次换代时被归档的旧计划 id；首次生成为 null"
    )
    occupation_id: str = Field(..., description="目标岗位 id（取自该诊断会话）")
    session_id: int = Field(..., description="据以生成的诊断会话 id")
    phases_count: int = Field(..., ge=1, description="阶段数")
    tasks_count: int = Field(..., ge=1, description="任务总数")
    pushed_at: str | None = Field(None, description="推送时间 ISO8601")


# ── 五维画像 ─────────────────────────────────────────────────


class MemoryItemOut(BaseModel):
    """一条记忆。"""

    memory_id: str | None = Field(None, description="记忆 id，平台侧主键")
    title: str | None = Field(None, description="标题（平台自动生成）")
    summary: str | None = Field(None, description="摘要")
    details: str | None = Field(None, description="详情")
    subtype: str | None = Field(None, description="维度下的子类型标签")
    tags: list[str] = Field(default_factory=list, description="记忆标签 + 维度标签")
    captured_at: str | None = Field(None, description="记忆产生时间 ISO8601")
    facet_details: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "各维专属字段，结构随 facet 而异（experience 有 situation/action/"
            "keyLearning，context 有 currentStatus 等），由画像平台定义，故不收紧"
        ),
    )


class MemoryFacetOut(BaseModel):
    """五维记忆中的一维。"""

    facet: Literal["identity", "context", "preference", "experience", "activity"] = Field(
        ..., description="维度标识"
    )
    label: str = Field(..., description="维度中文名：身份/情境/偏好/经验/活动")
    count: int = Field(..., ge=0, description="该维记忆条数")
    next_cursor: str | None = Field(None, description="分页游标；无更多为 null")
    items: list[MemoryItemOut] = Field(default_factory=list, description="该维记忆列表")
    digest: str = Field(..., description="一行摘要，卡片直接展示；无数据时为「暂无数据」")


class MemoryFacetRequest(BaseModel):
    """请求某一维记忆时的参数。"""

    facet: str = Field(..., description="维度：identity / context / preference / experience / activity")
    limit: int = Field(..., ge=1, le=20, description="该维取多少条，平台上限 20")


class MemorySearchBody(BaseModel):
    """发给画像服务 /memories/search 的请求体。"""

    facets: list[MemoryFacetRequest] = Field(..., description="要查的维度与条数")
    query: str | None = Field(None, description="语义检索词；不传则按维度取最近的")


class MemoryRequestEcho(BaseModel):
    """回显发给画像服务的请求，便于前端核对参数。Authorization 已脱敏。"""

    method: str = Field(..., description="HTTP 方法")
    url: str = Field(..., description="完整请求地址")
    headers: dict[str, str] = Field(..., description="请求头；Authorization 已脱敏")
    body: MemorySearchBody = Field(..., description="请求体")


class MemoryBlockOut(BaseModel):
    """画像服务返回的五维记忆块。服务不可用时 available=false，其余为空但不报错。"""

    available: bool = Field(..., description="画像服务是否可用（BTS + 地址均已配置）")
    endpoint: str | None = Field(None, description="画像服务地址")
    path: str = Field(..., description="记忆查询接口路径")
    facets: list[MemoryFacetOut] = Field(default_factory=list, description="五维记忆")
    total: int = Field(0, ge=0, description="记忆总条数")
    request: MemoryRequestEcho | None = Field(None, description="请求回显")
    raw: dict[str, Any] | None = Field(
        None, description="画像服务的原始响应，原样透传不做加工，供调试核对"
    )
    error: str | None = Field(None, description="调用失败原因；成功为 null")


class MergedSkillEntry(BaseModel):
    """合并后的一项技能档位，标明证据来自哪一侧。"""

    skill_key: str = Field(..., description="技能聚合主键")
    # 出参里有 code 就必须配同层展示名（闸门 scripts/verify_skill_name_exposed.py）。
    # 简历/对话来的自由文本技能没有 code，那时 skill_key 与 skill_name 是同一个值
    skill_name: str | None = Field(None, description="技能展示名")
    level: int = Field(..., ge=1, le=5, description="档位 1–5")
    from_: Literal["assessment", "memory"] = Field(
        ...,
        alias="from",
        description="证据来源：assessment=本系统实测；memory=五维记忆推断",
    )

    model_config = {"populate_by_name": True}


class SkillCounts(BaseModel):
    """技能画像的分项计数。"""

    assessment: int = Field(0, ge=0, description="实测得到的技能数")
    memory: int = Field(0, ge=0, description="记忆推断出的技能数")
    merged: int = Field(0, ge=0, description="合并去重后的技能数")


class SkillProfileMeta(BaseModel):
    """技能画像的解析元信息。"""

    engine: str | None = Field(
        None,
        description="解析引擎：llm=模型抽取；rule=规则兜底；not_parsed=本次未解析（只读缓存）",
    )
    elapsed_ms: int | None = Field(None, description="解析耗时毫秒")
    error: str | None = Field(None, description="解析失败原因")


class SkillProfileBlock(BaseModel):
    """技能画像块：实测与记忆两路证据合并后的档位表。"""

    source: Literal["mixed", "assessment", "memory", "none"] = Field(
        ...,
        description="证据来源：mixed=实测与记忆都有；assessment=仅实测；memory=仅记忆；none=都没有",
    )
    parsed: bool = Field(
        ..., description="本次是否真的解析了记忆。false 表示读的是缓存或仅用实测数据"
    )
    counts: SkillCounts = Field(..., description="分项计数")
    meta: SkillProfileMeta = Field(..., description="解析元信息")
    merged: list[MergedSkillEntry] = Field(
        default_factory=list, description="合并后的技能档位，按档位降序"
    )


class AssessedSkillEntry(BaseModel):
    """一项实测技能（来自 biz_user_skill）。"""

    skill_name: str | None = Field(None, description="技能名")
    level: int = Field(..., ge=1, le=5, description="实测档位 1–5")
    score: int = Field(0, description="得分")
    source: str | None = Field(None, description="来源：assessment / resume / self")
    updated_at: str | None = Field(None, description="更新时间 ISO8601")


class DiagnosisHistoryItem(BaseModel):
    """一次历史诊断。"""

    occupation_id: str | None = Field(None, description="岗位节点 id")
    occupation_name: str | None = Field(None, description="岗位名")
    channel: str | None = Field(None, description="诊断渠道：assessment / resume / chat")
    match_score: float | None = Field(None, description="匹配度 0–100")
    created_at: str | None = Field(None, description="诊断时间 ISO8601")


class StudentProfileOut(BaseModel):
    """学员画像：五维记忆 + 技能档位 + 历史诊断。

    三块互补：`memory` 是用户中心沉淀的跨系统画像，`assessment` 是本系统测出来的
    硬证据，`skills` 是两路合并后的档位表。匹配度级联时实测优先于记忆。
    """

    user: RefOut = Field(..., description="当前用户（id 为 UC user_id，name 为冗余展示名）")
    memory: MemoryBlockOut = Field(..., description="五维记忆")
    skills: SkillProfileBlock = Field(..., description="合并后的技能画像")
    assessment: list[AssessedSkillEntry] = Field(
        default_factory=list, description="实测技能明细，按档位降序"
    )
    diagnoses: list[DiagnosisHistoryItem] = Field(
        default_factory=list, description="历史诊断记录，最近 20 条"
    )


# ── 诊断（简历 / 对话） ──────────────────────────────────────


class ResumeSampleOut(BaseModel):
    """示例简历，供前端一键填充。"""

    content_text: str = Field(..., description="示例简历正文")
    note: str = Field(..., description="用法说明")


class ResumeExtractOut(BaseModel):
    """简历文件 → 文本。"""

    content_text: str = Field(..., description="抽取出的简历正文")
    filename: str | None = Field(None, description="原始文件名")
    chars: int = Field(0, ge=0, description="正文字数")
    engine: str | None = Field(None, description="抽取引擎：docx / pdf / plain")


class ChatSessionOut(BaseModel):
    """对话诊断会话。"""

    session_id: int = Field(..., description="会话 id")
    channel: Literal["chat"] = Field(..., description="固定 chat")
    status: str = Field(..., description="会话状态：active / done")
    target_occupation_id: str | None = Field(None, description="目标岗位 id")
    first_question: str | None = Field(None, description="开场提问")


class ChatMessageOut(BaseModel):
    """一轮对话的回复。轮次够了会同时给出报告。"""

    session_id: int = Field(..., description="会话 id")
    reply: str | None = Field(None, description="AI 追问或结语")
    done: bool = Field(False, description="对话是否结束；true 时 report 有值")
    turn: int | None = Field(None, ge=0, description="当前轮次")
    report: AssessmentReportOut | None = Field(None, description="结束时产出的诊断报告")


class UserSkillItem(BaseModel):
    """学员技能画像的一项（对应 biz_user_skill 一行）。"""

    user_id: str | None = Field(None, description="UC 用户 id")
    skill_id: str = Field(..., description="技能 id 或 skill_key")
    skill_name: str | None = Field(None, description="技能名")
    level: int = Field(1, ge=1, le=5, description="档位 1–5")
    score: int = Field(0, description="得分")
    source: str = Field("self", description="来源：self=自评；assessment=测评；resume=简历解析")
    updated_at: str | None = Field(None, description="更新时间 ISO8601")


PositionMatchOut.model_rebuild()
GoalOverviewOut.model_rebuild()
