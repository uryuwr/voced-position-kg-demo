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

    skill_key: str = Field(..., description="技能聚合主键")
    category: str = Field(..., description="技能大类，缺失时为「未分类」")
    required_level: int | None = Field(None, ge=1, le=5, description="岗位要求档 1–5")
    required_label: str | None = Field(None, description="要求档文案，如「熟练」")
    measured_level: int | None = Field(None, ge=1, le=5, description="实测档 1–5；未测为 null")
    measured_label: str | None = Field(None, description="实测档文案")
    weight: float = Field(..., description="该技能在岗位中的权重，Σ≈1")
    weight_pct: int | None = Field(None, description="权重百分比（四舍五入），便于直接展示")
    ratio: float = Field(..., description="达成比 = 实测/要求，封顶 1.0")
    ok: bool = Field(..., description="是否达标（实测 ≥ 要求）")
    tested: bool = Field(
        ..., description="本次是否实际考到。false 时 measured_* 为 null，不参与匹配度计算"
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
    """报告的分项计数。"""

    skill_total: int = Field(..., ge=0, description="岗位技能总数")
    tested: int = Field(..., ge=0, description="本次实测到的技能数")
    untested: int = Field(..., ge=0, description="未覆盖的技能数")
    strength: int = Field(..., ge=0, description="达标项数")
    gap: int = Field(..., ge=0, description="未达标项数")


class AssessmentReportOut(BaseModel):
    """综合能力报告。

    匹配度**只按实测到的技能算**：一次测评覆盖 6–10 项核心技能，若把没考到的
    技能按 0 分计入，分数会被稀释成一个既不反映能力、也无法改善的数字。
    覆盖率 `coverage` 就是这个分数的置信度说明，两者必须一起看。
    """

    channel: str | None = Field(None, description="产生渠道：assessment / resume / chat / profile")
    target_occupation_id: str | None = Field(None, description="目标岗位节点 id")
    target_occupation_name: str | None = Field(None, description="目标岗位名")
    match_score: float = Field(..., description="综合能力匹配度 0–100，仅按实测技能计")
    coverage: float = Field(
        ..., description="本次测评覆盖的岗位技能权重百分比 0–100，是 match_score 的置信度"
    )
    radar: RadarOut = Field(..., description="双系列雷达图数据")
    strengths: list[ReportItem] = Field(
        default_factory=list, description="优势项（已达标），按超出幅度降序"
    )
    gaps: list[ReportItem] = Field(
        default_factory=list, description="短板项（未达标），按 urgency 降序"
    )
    untested: list[ReportItem] = Field(
        default_factory=list, description="本次未覆盖的技能，按权重降序；不下能力结论"
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

    type: Literal["session"] = "session"
    session_id: int = Field(..., description="本场会话 id，后续答题/结算都用它")
    occupation_id: str = Field(..., description="本场对标的岗位 id")


class SseStageEvent(BaseModel):
    """`event: stage` —— 阶段状态推进，前端据此点亮步骤条。"""

    type: Literal["stage"] = "stage"
    key: Literal["parse", "assess", "report"] = Field(..., description="阶段标识")
    status: Literal["pending", "active", "done"] = Field(..., description="阶段状态")
    output: dict[str, Any] = Field(
        default_factory=dict, description="阶段产出，结构同 StageOut.output"
    )


class SsePlanEvent(BaseModel):
    """`event: plan` —— 出题计划，让前端先把进度条总数显示出来。"""

    type: Literal["plan"] = "plan"
    total: int = Field(..., ge=0, description="预计总题数")
    cover: int = Field(..., ge=0, description="覆盖型题数（广度，每项核心技能各一道）")
    verify: int = Field(..., ge=0, description="验证型题数（深度，高要求档技能追加开放题）")
    reason: str = Field(..., description="题数如此规划的原因，可直接展示给学员")


class SseQuestionEvent(BaseModel):
    """`event: question` —— 推送一道题。前端压入本地队列，答题零等待。"""

    type: Literal["question"] = "question"
    question: QuestionOut = Field(..., description="题目内容")


class SseQuestionEndEvent(BaseModel):
    """`event: question_end` —— 题目出完的确定信号。

    比按题数判断可靠：模型出题失败降级时，实际条数可能少于 plan.total。
    """

    type: Literal["question_end"] = "question_end"
    total: int = Field(..., ge=0, description="实际出题数")
    stop_reason: str = Field(
        ..., description="收敛原因：coverage_met / max_questions / max_batches / no_more_skills"
    )


class SseReportEvent(BaseModel):
    """`event: report` —— 结算完成，携带完整报告。"""

    type: Literal["report"] = "report"
    report: AssessmentReportOut = Field(..., description="综合能力报告")


class SseErrorEvent(BaseModel):
    """`event: error` —— 流中异常。不静默断开，前端要能拿到原因。"""

    type: Literal["error"] = "error"
    code: str | None = Field(
        None, description="错误码，如 no_skill_composition=该岗位尚未配置技能构成"
    )
    message: str = Field(..., description="错误说明")


StageOut.model_rebuild()
