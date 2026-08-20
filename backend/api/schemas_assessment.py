"""能力测评契约：出题 / 答题 / 结算三段的请求与响应模型。

这里的模型只做一件事——**把实际返回的数据形状如实写进 OpenAPI**。
测评的返回体层层嵌套（题目里有选项、报告里有条目和雷达系列），
若图省事写成 `dict[str, Any]`，Swagger 上就是一个 `any`，
调用方只能靠猜或者去翻服务端代码。

SSE 事件（`stream_questions` / `stream_report`）不是 JSON 响应体，
FastAPI 无法从 `response_model` 推导，所以在这里显式定义各事件的 payload，
并通过路由 docstring 里的 `responses` 示例挂上去。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ── 题目 ─────────────────────────────────────────────────────


class ChoiceOption(BaseModel):
    """SJT（情景判断题）选项。每项对应一个能力档位，选谁即判谁。"""

    value: int = Field(..., ge=1, description="选项序号，作答时回传这个值")
    level: int = Field(..., ge=1, le=5, description="该选项体现的能力档位 1–5，同题内互不相同")
    text: str = Field(..., description="选项文案")


class QuestionOut(BaseModel):
    """一道题。choice 与 open 共用此模型，按 `type` 区分有效字段。"""

    index: int = Field(..., ge=0, description="题号，作答时回传；也是本场测评内的稳定序号")
    type: Literal["choice", "open"] = Field(
        ..., description="choice=情景判断选择题（当场判分）；open=开放问答题（后台判分）"
    )
    variant: str | None = Field(
        None,
        description=(
            "题目来源变体：sjt=模型生成的情景判断题；self_report=网关不可用时的自评降级题"
            "（考核力弱，报告里据此标注）；generic=通用开放题"
        ),
    )
    skill_key: str | None = Field(None, description="考查的技能（聚合主键，非某一档节点 id）")
    category: str | None = Field(None, description="技能大类，雷达图按它聚合")
    required_level: int | None = Field(
        None, ge=1, le=5, description="岗位对该技能要求的档位 1–5，用于判定是否达标"
    )
    weight: float | None = Field(None, description="该技能在岗位 requires 中的权重，Σ≈1")
    prompt: str = Field(..., description="题干")
    options: list[ChoiceOption] = Field(
        default_factory=list, description="选择题的选项；开放题为空数组"
    )
    rubric: list[str] = Field(
        default_factory=list, description="开放题评分要点，前端可作答题提示展示；选择题为空"
    )
    min_chars: int | None = Field(None, description="开放题建议最少字数；选择题为 null")
    planned_total: int | None = Field(
        None, description="本场预计总题数；分批出题时是预估值，以 question_end 为准"
    )


# ── 答题 ─────────────────────────────────────────────────────


class AnswerBody(BaseModel):
    """提交一题。"""

    index: int = Field(..., ge=0, description="题号（question.index）")
    answer: int | str = Field(
        ...,
        description="选择题传选项 value（int）；开放题传作答文本（str）",
    )


class ProgressOut(BaseModel):
    """答题进度。三个计数都来自业务表实时查询，不是内存态。"""

    asked: int = Field(..., ge=0, description="已出题数")
    answered: int = Field(..., ge=0, description="已作答题数")
    grading: int = Field(
        ..., ge=0, description="仍在后台判分的开放题数；结算接口会等它归零"
    )
    target_total: int | None = Field(
        None, description="本场目标题数；进程重启后可能为 null，不要用它判断是否出完"
    )


class AnswerAcceptedOut(BaseModel):
    """答题回执。选择题当场出档位，开放题只回执不给分。"""

    accepted: bool = Field(..., description="是否已记录；校验失败走 400 而不是 false")
    graded: bool = Field(
        ..., description="是否已判分。选择题 true（纯查表）；开放题 false，判分在后台线程"
    )
    index: int = Field(..., ge=0, description="题号，与请求一致")
    level: int | None = Field(
        None, ge=1, le=5, description="实测档位 1–5，仅 graded=true 时有值"
    )
    progress: ProgressOut = Field(..., description="提交后的最新进度")


class AnswerRecordOut(BaseModel):
    """已作答记录（状态恢复用）。"""

    index: int = Field(..., ge=0, description="题号")
    raw_answer: str | None = Field(None, description="学员原始作答（选项 value 或文本）")
    level: int | None = Field(None, ge=1, le=5, description="判定档位 1–5")
    score: int | None = Field(None, description="判分得分")
    grade_status: Literal["pending", "graded", "failed"] = Field(
        ..., description="pending=后台判分中；graded=已判；failed=判分失败（按未测处理）"
    )
    source: str | None = Field(None, description="判分来源：choice=查表；llm=模型；rule=规则兜底")
    evidence_score: float | None = Field(None, description="证据充分度 0–1，开放题判分置信度")
    capped: bool | None = Field(
        None, description="是否因证据不足被压档：作答说得很大但举证不足时不给高档"
    )
    reason: str | None = Field(None, description="判分理由（模型给出）")


# ── 阶段状态 ─────────────────────────────────────────────────


class ParsedSkill(BaseModel):
    """简历解析推断出的一项技能档位。"""

    skill_key: str = Field(..., description="技能聚合主键")
    level: int = Field(..., ge=1, le=5, description="推断档位 1–5")


class StageParseOutput(BaseModel):
    """阶段一「简历解析推断」的产出。"""

    engine: str | None = Field(
        None, description="解析引擎：llm=模型抽取；rule=规则兜底；skip=未提供简历"
    )
    skill_count: int = Field(0, ge=0, description="解析出的技能数")
    skills: list[ParsedSkill] = Field(default_factory=list, description="逐项技能与推断档位")
    note: str | None = Field(None, description="降级或失败说明；正常为 null")


class StageAssessOutput(BaseModel):
    """阶段二「对话问答测评」的产出。"""

    asked: int = Field(0, ge=0, description="已出题数")
    answered: int = Field(0, ge=0, description="已作答题数")
    grading: int = Field(0, ge=0, description="后台判分中的题数")


class StageOut(BaseModel):
    """前端步骤条的一个节点。三节点固定，不随后端图结构变化。"""

    key: Literal["parse", "assess", "report"] = Field(..., description="阶段标识")
    name: str = Field(..., description="阶段中文名：简历解析推断 / 对话问答测评 / 综合能力报告")
    status: Literal["pending", "active", "done"] = Field(
        ..., description="pending=灰；active=高亮；done=打勾"
    )
    output: StageParseOutput | StageAssessOutput | "AssessmentReportOut" | dict[str, Any] = Field(
        default_factory=dict,
        description="该阶段产出，按 key 取对应结构；report 阶段未完成时为空对象 {}",
    )


# ── 报告 ─────────────────────────────────────────────────────


class ReportItem(BaseModel):
    """报告里的一项技能：岗位要求 vs 学员实测。"""

    skill_key: str = Field(..., description="技能聚合主键（ASCII code）")
    skill_name: str | None = Field(None, description="展示名 —— **页面上要显示这个**。`skill_key` 从 2026-08-19 起是 ASCII code（形如 SK0123456789），拿它渲染就是一串哈希")
    category: str = Field(..., description="技能大类，缺失时为「未分类」")
    required_level: int | None = Field(None, ge=1, le=5, description="岗位要求档 1–5")
    required_label: str | None = Field(None, description="要求档文案，如「熟练」")
    measured_level: int | None = Field(None, ge=1, le=5, description="实测档 1–5；未测为 null")
    measured_label: str | None = Field(None, description="实测档文案")
    weight: float = Field(..., description="该技能在岗位中的权重，Σ≈1")
    weight_pct: int | None = Field(None, description="权重百分比（四舍五入），便于直接展示")
    ratio: float = Field(
        ...,
        description=(
            "达成比 = 实测/要求，封顶 1.0。`scorable=false`（岗位没给要求档）时这里是"
            "「有实测即 1.0」的展示值，**不参与匹配度计算**，不要拿它当达标结论"
        ),
    )
    ok: bool = Field(..., description="是否达标（实测 ≥ 要求）；无要求档时恒为 false")
    scorable: bool = Field(
        True,
        description=(
            "该项能否评分。false = 岗位对这个技能的要求档缺失或越界（无基准），"
            "**分子分母都不计入 match_score**，也不进 strengths / gaps —— "
            "没有基准既谈不上达标、也谈不上差距。这类项汇总在 `no_baseline` 里。"
            "历史报告（新增此字段之前生成）缺该键，按 true 处理"
        ),
    )
    tested: bool = Field(
        ...,
        description=(
            "本次是否实际考到。false 时 measured_* 为 null、ratio 为 0，"
            "仍按 0 分计入 match_score（只有 tested_match_score 把它排除在分母外）"
        ),
    )
    source: str | None = Field(None, description="档位来源：choice / llm / rule")
    evidence_score: float | None = Field(None, description="证据充分度 0–1")
    capped: bool | None = Field(None, description="是否因证据不足被压档")
    urgency: float = Field(
        0.0, description="补强紧迫度 = 权重 × 差距档数；短板排序用，越大越该先补"
    )


class RadarSeries(BaseModel):
    """雷达图的一条系列。"""

    key: Literal["user", "required"] = Field(..., description="user=学员实测；required=岗位要求")
    name: str = Field(..., description="系列展示名")
    scores: list[int] = Field(..., description="各轴得分 0–100，顺序与 categories 一致")


class RadarOut(BaseModel):
    """双系列雷达图（学员实测 vs 岗位标准）。"""

    axis_type: Literal["skill", "category"] = Field(
        ...,
        description=(
            "轴的口径：skill=按技能名（默认，与原型一致）；"
            "实测技能不足 3 项时回落为 category=按技能大类聚合"
        ),
    )
    categories: list[str] = Field(..., description="各轴名称")
    series: list[RadarSeries] = Field(..., description="两条系列：实测与要求")
    scores: list[int] = Field(
        ..., description="兼容字段，等同 series[key=user].scores；新接入请用 series"
    )


class ReportCounts(BaseModel):
    """报告的分项计数。

    `tested + untested == skill_total` 恒成立；但 `strength + gap` 只数**测到且有基准**
    的项，缺要求档的项两边都不进（数量见 `no_baseline` 列表长度），所以
    `strength + gap + untested` 可能小于 `skill_total`。
    """

    skill_total: int = Field(..., ge=0, description="岗位技能总数")
    tested: int = Field(..., ge=0, description="本次实测到的技能数")
    untested: int = Field(..., ge=0, description="未覆盖的技能数")
    strength: int = Field(..., ge=0, description="达标项数（仅可评分项）")
    gap: int = Field(..., ge=0, description="未达标项数（仅可评分项）")


class AssessmentReportOut(BaseModel):
    """综合能力报告。

    两个匹配度，分母不同，**不要混用也不要相互换算**：

    - `match_score`：分母是岗位**可评分**技能的全部权重，未测到的按 0 分计入。答「这个
      岗位你整体准备好了多少」，与岗位探索列表（`match_with_profile`）同源，可跨岗位横向
      比较、可用于列表排序。覆盖率 `coverage` 是它的置信度说明。
    - `tested_match_score`：分母只有**实测**技能权重。答「你考过的部分掌握得怎样」，
      只适合在报告详情页单点看；不同覆盖率之间不可比。

    「可评分」= 岗位给了 1–5 的要求档。要求档缺失或越界的技能没有基准、算不出达标率，
    **分子分母都不计**（`items[].scorable=false`，汇总在 `no_baseline`）。一项都没有
    可评分技能时 `match_score` 为 **null**、`score_status="no_baseline"` ——
    前端必须显示「该岗位尚未配置能力要求，无法评分」，**不要显示 0%**：
    0% 会被读成「完全不匹配」，而真相是数据缺口。库内 80% 的岗位目前是这个状态。

    前端接入须知（2026-08 破坏性变更）
    ---------------------------------
    **`match_score` 已由必填 number 改为可空**（`float | None`）。以前可以假定一定有值，
    现在不行了，`?? 0` / `|| 0` 这类兜底会把「没有评分依据」渲染成「完全不匹配」。

    - `null` ≠ `0`。`0` 是**算出来的结论**：有基准、有权重、也测过，只是一项都没达标。
      `null` 是**算不出来**：岗位没配能力要求（`score_status="no_baseline"`）。
      学员看到 0% 会理解成「我完全不行」，看到 null 该理解成「这里还没有依据」——
      两种情绪完全不同，混淆一次就是产品事故。
    - 建议展示：`null` 时保留分数的位置与字号，改成虚线框 + 「? %」占位
      （见 `frontend/student.html` 的 `.score.unknown`），旁边给
      `score_status` 对应的说明文案；不要塌成一行普通文字，否则看不出这里本来有分数。
    - `score_status="partial_baseline"` 时**有分数但只能当参考**：数字照显示，
      必须同时展示「仅供参考」与 `no_baseline_weight`，也不要拿它跨岗位排序。
    - 一句话结论直接用 `summary`：它已经按 `score_status` 分支写好了措辞，
      不会出现「匹配度 None%」。
    """

    session_id: int | None = Field(
        None,
        description=(
            "产生这份报告的诊断会话 id。**生成学习计划要用它**"
            "（`POST /v1/student/goal/learning-plan` 的必填入参）。\n\n"
            "数据层一直有这个字段，但响应模型此前没声明，被 Pydantic 丢掉了，"
            "导致前端拿不到 session_id、生成按钮永久禁用（2026-08-18 修）。"
        ),
    )
    channel: str | None = Field(None, description="产生渠道：assessment / resume / chat / profile")
    target_occupation_id: str | None = Field(None, description="目标岗位节点 id")
    target_occupation_name: str | None = Field(None, description="目标岗位名")
    match_score: float | None = Field(
        None,
        description=(
            "综合能力匹配度 0–100。分母是岗位**可评分**技能的全部权重（未测项按 0 分"
            "计入、缺要求档的项完全不计入），与岗位探索列表口径一致，可横向比较与排序。\n\n"
            "**可空字段（2026-08 起）**：为 null 表示算不出来（岗位一项要求档都没配），"
            "原因见 score_status。**为 null 时不要显示 0%，也不要 `?? 0` 兜底** —— "
            "0% 是「测过但一项都没达标」的结论，null 是「没有评分依据」，"
            "学员会把前者读成「我完全不行」。建议改用虚线框 +「? %」占位并给出说明文案。"
        ),
    )
    score_status: Literal[
        "ok", "partial_baseline", "no_skills", "no_baseline", "no_weight", "no_evidence"
    ] = Field(
        "ok",
        description=(
            "match_score 为什么是这个值，决定前端显示数字、显示数字+提示、还是只显示文案。"
            "取值顺序与服务端词表 `config.SCORE_STATUSES` 同源：\n\n"
            "| 取值 | 含义 | match_score | 建议展示 |\n"
            "| --- | --- | --- | --- |\n"
            "| `ok` | 有基准、有权重、也测到了，分数可用 | 数字 | 正常显示分数，可横向比较与排序 |\n"
            "| `partial_baseline` | 配了一部分：缺要求档的权重**超过 30%**，"
            "分数只依据已配置的那部分算出 | 数字 | 分数照显示，**同时**标「仅供参考 · "
            "该岗位 {no_baseline_weight}% 的能力要求待完善」；不要用于跨岗位排序 |\n"
            "| `no_skills` | 该岗位尚未配置技能构成，无从算起 | 0（无意义） | 「该岗位技能构成待完善」，不显示 0% |\n"
            "| `no_baseline` | 有技能构成但**一项要求档都没配** | **null** | 「? %」占位 + "
            "「该岗位能力标准待完善，暂时无法评分」；**别引导学员去做诊断**，做了也算不出来 |\n"
            "| `no_weight` | 可评分技能的权重全为 0（脏数据），算不出加权分 | 0（无意义） | "
            "同 no_skills，按「暂无法评分」处理 |\n"
            "| `no_evidence` | 有基准也有权重，但本次一项都没测到 | 0（无意义） | "
            "「未评估」+ 引导做测评；不要显示 0% |\n\n"
            "口径与 `PositionMatchOut.source` 的 no_overlap / none 一致：不用 0% 冒充结论。"
            "阈值 30% 是服务端常量（`config.PARTIAL_BASELINE_PCT`），前端不要自己再定一份。"
            "历史报告（新增此字段之前生成）缺该键，按 ok 处理"
        ),
    )
    tested_match_score: float | None = Field(
        None,
        description=(
            "仅按**实测**技能权重为分母的匹配度 0–100，即「考过的部分掌握得怎样」。"
            "与 match_score 不同刻度，不可跨岗位比较；本次未测到任何技能时为 0，"
            "一项可评分技能都没有时为 null（理由同 match_score）。"
            "历史报告（新增此字段之前生成）为 null"
        ),
    )
    coverage: float = Field(
        ...,
        description=(
            "本次测评覆盖的**可评分**技能权重百分比 0–100（分母不含缺要求档的项），"
            "是 match_score 的置信度。它只表达「缺证据」，「缺基准」看 no_baseline_weight"
        ),
    )
    no_baseline_weight: float = Field(
        0.0,
        description=(
            "因缺要求档而**无法评分**的技能权重，占岗位**全部**技能权重的百分比 0–100。"
            "100 表示整个岗位没配能力要求（此时 match_score 为 null）。"
            "**超过 30% 时服务端会把 score_status 降级成 partial_baseline**，"
            "前端不必自己比阈值，但展示「仅供参考」时可以把这个数字带出来"
            "（如「该岗位 82.4% 的能力要求待完善」）。"
            "权重全为 0 的脏数据岗位退回按项数占比计算。"
            "与 coverage 是两种不同的缺失：coverage 缺的是证据，这里缺的是基准"
        ),
    )
    radar: RadarOut = Field(..., description="双系列雷达图数据")
    strengths: list[ReportItem] = Field(
        default_factory=list, description="优势项（已测到且已达标），按超出幅度降序"
    )
    gaps: list[ReportItem] = Field(
        default_factory=list, description="短板项（已测到但未达标），按 urgency 降序"
    )
    untested: list[ReportItem] = Field(
        default_factory=list, description="本次未覆盖的技能，按权重降序；不下能力结论"
    )
    no_baseline: list[ReportItem] = Field(
        default_factory=list,
        description=(
            "**无法评分**的技能（岗位要求档缺失或越界），按权重降序。既不在 strengths "
            "也不在 gaps —— 没有基准谈不上达标或差距。与 `untested` 是**两个正交视角**、"
            "会重叠：一项技能可以既没测到（untested）又没有基准（no_baseline）。"
            "该数据缺口需要运营在管理台补要求档，不是学员的问题"
        ),
    )
    items: list[ReportItem] = Field(default_factory=list, description="全部技能明细（含未测）")
    counts: ReportCounts = Field(..., description="分项计数")
    summary: str | None = Field(None, description="一句话结论")
    created_at: str | None = Field(None, description="生成时间 ISO8601")


# ── 会话状态 ─────────────────────────────────────────────────


class StartBody(BaseModel):
    """开始一场测评。"""

    occupation_id: str | None = Field(
        None, description="目标岗位节点 id；不传则取当前锁定的学习目标"
    )
    resume_text: str | None = Field(
        None, description="简历原文；留空则跳过解析阶段，直接按岗位标准出题"
    )


class AssessmentStateOut(BaseModel):
    """测评当前状态，用于刷新恢复。

    全部来自业务表两条普通查询，不依赖工作流存档——进程重启也能恢复。
    """

    session_id: int = Field(..., description="会话 id")
    exists: bool = Field(..., description="是否已出过题；false 表示会话刚建还没开始")
    occupation_id: str | None = Field(None, description="本场测评对标的岗位 id")
    stages: list[StageOut] = Field(..., description="三阶段状态，顺序固定 parse/assess/report")
    current_stage: Literal["parse", "assess", "report"] = Field(..., description="当前所处阶段")
    questions: list[QuestionOut] = Field(default_factory=list, description="本场全部题目")
    answers: list[AnswerRecordOut] = Field(default_factory=list, description="已作答记录")
    question: QuestionOut | None = Field(
        None, description="下一道未答题；全部答完为 null"
    )
    progress: ProgressOut = Field(..., description="答题进度")
    question_end: bool = Field(
        ...,
        description="题目是否已出完。库里有题即为 true——出题是一次性长连接，不会再追加",
    )
    report: AssessmentReportOut | None = Field(
        None, description="综合能力报告；未结算为 null"
    )


# ── SSE 事件（非 JSON 响应体，仅供文档说明） ─────────────────


class SseSessionEvent(BaseModel):
    """`event: session` —— 长连接建立，携带会话 id。"""

    type: Literal["session"] = Field("session", description="类型")
    session_id: int = Field(..., description="本场会话 id，后续答题/结算都用它")
    occupation_id: str = Field(..., description="本场对标的岗位 id")


class SseStageEvent(BaseModel):
    """`event: stage` —— 阶段状态推进，前端据此点亮步骤条。"""

    type: Literal["stage"] = Field("stage", description="类型")
    key: Literal["parse", "assess", "report"] = Field(..., description="阶段标识")
    status: Literal["pending", "active", "done"] = Field(..., description="阶段状态")
    output: dict[str, Any] = Field(
        default_factory=dict, description="阶段产出，结构同 StageOut.output"
    )


class SsePlanEvent(BaseModel):
    """`event: plan` —— 出题计划，让前端先把进度条总数显示出来。"""

    type: Literal["plan"] = Field("plan", description="类型")
    total: int = Field(..., ge=0, description="预计总题数")
    cover: int = Field(..., ge=0, description="覆盖型题数（广度，每项核心技能各一道）")
    verify: int = Field(..., ge=0, description="验证型题数（深度，高要求档技能追加开放题）")
    reason: str = Field(..., description="题数如此规划的原因，可直接展示给学员")


class SseQuestionEvent(BaseModel):
    """`event: question` —— 推送一道题。前端压入本地队列，答题零等待。"""

    type: Literal["question"] = Field("question", description="类型")
    question: QuestionOut = Field(..., description="题目内容")


class SseQuestionEndEvent(BaseModel):
    """`event: question_end` —— 题目出完的确定信号。

    比按题数判断可靠：模型出题失败降级时，实际条数可能少于 plan.total。
    """

    type: Literal["question_end"] = Field("question_end", description="类型")
    total: int = Field(..., ge=0, description="实际出题数")
    stop_reason: str = Field(
        ..., description="收敛原因：coverage_met / max_questions / max_batches / no_more_skills"
    )


class SseReportEvent(BaseModel):
    """`event: report` —— 结算完成，携带完整报告。"""

    type: Literal["report"] = Field("report", description="类型")
    report: AssessmentReportOut = Field(..., description="综合能力报告")


class SseErrorEvent(BaseModel):
    """`event: error` —— 流中异常。不静默断开，前端要能拿到原因。"""

    type: Literal["error"] = Field("error", description="类型")
    code: str | None = Field(
        None, description="错误码，如 no_skill_composition=该岗位尚未配置技能构成"
    )
    message: str = Field(..., description="错误说明")


StageOut.model_rebuild()
