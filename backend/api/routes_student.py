"""
学生端业务 API —— 对齐 Open-Q frontend.html 四大模块：
  探索 · AI 诊断 · 学习中心 · 我的
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from backend.api.auth_temp import TempUser, require_temp_user
from backend.api.schemas_biz import (
    BadgeDefOut,
    ChatMessageBody,
    ChatSessionBody,
    DiagnosisReportOut,
    GoalOut,
    GoalPutBody,
    IndustryListOut,
    LearningPathOut,
    MeOut,
    PathGenerateBody,
    PositionDetailOut,
    PositionListOut,
    ProfessionDetailOut,
    ProfessionListOut,
    ProfessionOut,
    ResourceListOut,
    ResumeDiagBody,
    SkillCategory,
    SkillLevelMeta,
    SkillListOut,
    SkillOut,
)
from backend.kg.pg_store import biz_store as biz

router = APIRouter(prefix="/v1/student", tags=[])


# ── 元数据 ───────────────────────────────────────────────────


@router.get(
    "/meta/skill-levels",
    tags=["前台 · 岗位探索与详情"],
    response_model=list[SkillLevelMeta],
    summary="技能等级字典 L1–L5",
    description="对齐原型 SKILL_LEVEL_META：了解/掌握/熟练/精通/专家。",
)
def meta_levels(user: TempUser = Depends(require_temp_user)) -> list[SkillLevelMeta]:
    _ = user
    return [SkillLevelMeta.model_validate(x) for x in biz.skill_level_meta()]


@router.get(
    "/meta/skill-categories",
    tags=["前台 · 岗位探索与详情"],
    response_model=list[SkillCategory],
    summary="技能类目字典",
)
def meta_cats(user: TempUser = Depends(require_temp_user)) -> list[SkillCategory]:
    _ = user
    return [SkillCategory.model_validate(x) for x in biz.skill_categories()]


# ── 探索 ─────────────────────────────────────────────────────


@router.get(
    "/industries",
    tags=["前台 · 岗位探索与详情"],
    response_model=IndustryListOut,
    summary="行业列表（分页）",
    description="探索筛选项；树形仍可用图检索 `GET /v1/industries/tree`。",
)
def student_industries(
    q: str | None = Query(None, description="名称关键字"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    region: str | None = Query(None, description="默认 CN"),
    user: TempUser = Depends(require_temp_user),
) -> IndustryListOut:
    _ = user
    return IndustryListOut.model_validate(
        biz.list_industries_page(q=q, page=page, page_size=page_size, region=region)
    )


@router.get(
    "/professions",
    tags=["前台 · 岗位探索与详情"],
    response_model=ProfessionListOut,
    summary="专业列表（探索首页）",
    description="对应原型「搜索专业 / 专业卡片列表」。底层 type=major。",
)
def student_professions(
    q: str | None = Query(None, description="搜索专业 / 关键词"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    region: str | None = Query(None),
    user: TempUser = Depends(require_temp_user),
) -> ProfessionListOut:
    _ = user
    return ProfessionListOut.model_validate(
        biz.list_professions(q=q, page=page, page_size=page_size, region=region)
    )


@router.get(
    "/professions/{profession_id:path}",
    tags=["前台 · 岗位探索与详情"],
    response_model=ProfessionDetailOut,
    summary="专业详情 + 岗位 + 成长阶梯",
    description="对齐 vProfession：专业信息、对口岗位、ladder。",
)
def student_profession_detail(
    profession_id: str,
    user: TempUser = Depends(require_temp_user),
) -> ProfessionDetailOut:
    _ = user
    p = biz.get_profession(profession_id)
    if not p:
        raise HTTPException(404, "profession not found")
    positions = biz.profession_positions(profession_id)
    ladder_raw = biz.profession_ladder(profession_id)
    return ProfessionDetailOut.model_validate(
        {"profession": p, "positions": positions, "ladder": ladder_raw}
    )


@router.get(
    "/positions",
    tags=["前台 · 岗位探索与详情"],
    response_model=PositionListOut,
    summary="岗位列表",
)
def student_positions(
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    region: str | None = Query(None),
    user: TempUser = Depends(require_temp_user),
) -> PositionListOut:
    _ = user
    return PositionListOut.model_validate(
        biz.list_positions(q=q, page=page, page_size=page_size, region=region)
    )


@router.get(
    "/positions/skill-composition",
    tags=["前台 · 岗位探索与详情"],
    summary="岗位技能构成（query id，推荐）",
    description="逻辑技能 + weight_sum；权重只认 requires 边。id 含冒号时用本接口。",
)
def student_position_skill_composition_q(
    id: str = Query(..., description="岗位节点 id"),
    user: TempUser = Depends(require_temp_user),
) -> dict[str, Any]:
    _ = user
    try:
        from backend.kg.pg_store.skill_aggregate import occupation_skill_composition

        return occupation_skill_composition(id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@router.get(
    "/positions/match",
    tags=["前台 · 岗位探索与详情"],
    summary="岗位匹配度（原型：88% 匹配得分）",
    description=(
        "用户技能画像 × 岗位 requires 的**加权**匹配度，供原型 4 处使用："
        "岗位卡片匹配得分、顶部锁定目标、诊断页基准匹配度、学习路径活跃目标卡。\n\n"
        "算法：单项达标率 = `min(用户等级 / 要求等级, 1)`；"
        "总分 = `Σ(达标率 × 权重) / Σ权重 × 100`。权重取自国家职业技能标准的权重表。\n\n"
        "返回 `strengths`（已达标）与 `gaps`（未达标，按权重倒序）可直接渲染"
        "「优势精通 / 关键能力缺口」两栏；`radar` 按技能大类聚合达标率。\n\n"
        "> 用户画像为空时匹配度为 0，属正常——需先做一次 AI 诊断。"
    ),
    response_description="{ occupation, match_score, items[], strengths[], gaps[], radar }",
)
def student_position_match(
    position_id: str = Query(..., description="岗位节点 id（含冒号，用 query 传）"),
    limit: int = Query(50, ge=1, le=200, description="参与比对的技能条数上限"),
    user: TempUser = Depends(require_temp_user),
) -> dict[str, Any]:
    try:
        return biz.position_match(user.user_id, position_id, limit=limit)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@router.get(
    "/positions/{position_id:path}",
    tags=["前台 · 岗位探索与详情"],
    response_model=PositionDetailOut,
    summary="岗位详情 + 技能要求",
    description=(
        "对齐 vPosition：岗位含 industries / counts；"
        "skills 默认按 skill_key 聚合为逻辑技能（含 levels / level_descriptions），前端无需再 group。"
    ),
)
def student_position_detail(
    position_id: str,
    aggregate: bool = Query(
        True, description="True=逻辑技能聚合；False=skill_level 扁平行"
    ),
    user: TempUser = Depends(require_temp_user),
) -> PositionDetailOut:
    _ = user
    # path 参数若误吞 /skill-composition 后缀则纠正
    if position_id.endswith("/skill-composition"):
        position_id = position_id[: -len("/skill-composition")]
        try:
            from backend.kg.pg_store.skill_aggregate import occupation_skill_composition

            return occupation_skill_composition(position_id)  # type: ignore[return-value]
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
    p = biz.get_position(position_id)
    if not p:
        raise HTTPException(404, "position not found")
    skills = biz.position_skills(position_id, aggregate=aggregate)
    return PositionDetailOut.model_validate({"position": p, "skills": skills})


@router.get(
    "/skills",
    tags=["前台 · 岗位探索与详情"],
    response_model=SkillListOut,
    summary="技能库列表（分页，默认逻辑技能聚合）",
    description=(
        "view=bundle（默认）：按 skill_key 聚合，一页一行逻辑技能；"
        "view=level：原始 skill_level 扁平行。"
    ),
)
def student_skills(
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    region: str | None = Query(None),
    view: str = Query("bundle", description="bundle | level"),
    occupation_id: str | None = Query(None, description="仅该岗位 requires 覆盖的技能"),
    has_level: str | None = Query(None, description="至少含该档，如 L3"),
    user: TempUser = Depends(require_temp_user),
) -> SkillListOut:
    _ = user
    return SkillListOut.model_validate(
        biz.list_skills_page(
            q=q,
            page=page,
            page_size=page_size,
            region=region,
            view=view,
            occupation_id=occupation_id,
            has_level=has_level,
        )
    )


@router.get(
    "/skills/bundles/{skill_key:path}",
    tags=["前台 · 岗位探索与详情"],
    response_model=SkillOut,
    summary="逻辑技能详情（L1–L5 聚合）",
    description="skill_key 或 bundle:{region}:{key}；返回 levels / level_descriptions / counts。",
)
def student_skill_bundle(
    skill_key: str,
    region: str | None = Query(None),
    user: TempUser = Depends(require_temp_user),
) -> SkillOut:
    _ = user
    s = biz.get_skill_detail(skill_key, region=region)
    if not s:
        raise HTTPException(404, "skill bundle not found")
    return SkillOut.model_validate(s)


@router.get(
    "/goal",
    tags=["前台 · 岗位探索与详情"],
    response_model=GoalOut | None,
    summary="当前学习目标岗位",
    description="对齐 state.goal；未设置返回 null。",
)
def student_get_goal(user: TempUser = Depends(require_temp_user)) -> GoalOut | None:
    g = biz.get_goal(user.user_id)
    return GoalOut.model_validate(g) if g else None


@router.put(
    "/goal",
    tags=["前台 · 岗位探索与详情"],
    response_model=GoalOut,
    summary="设定学习目标岗位",
    description="对齐 setGoal：锁定目标并记成就 first_goal。",
)
def student_set_goal(
    body: GoalPutBody,
    user: TempUser = Depends(require_temp_user),
) -> GoalOut:
    try:
        g = biz.set_goal(
            user.user_id,
            user.user_name,
            occupation_id=body.occupation_id,
            major_id=body.major_id,
        )
        return GoalOut.model_validate(g)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete(
    "/goal",
    tags=["前台 · 岗位探索与详情"],
    summary="清除学习目标",
    description="对齐 clearGoal。传 occupation_id 只删该目标，否则清空全部。",
)
def student_clear_goal(
    occupation_id: str | None = Query(None, description="只清除该岗位目标"),
    user: TempUser = Depends(require_temp_user),
) -> dict[str, str]:
    biz.clear_goal(user.user_id, occupation_id)
    return {"status": "cleared"}


@router.get(
    "/goals",
    tags=["前台 · 岗位探索与详情"],
    summary="我的全部目标（活跃 + 历史）",
    description=(
        "一人可锁定多个岗位目标，其中至多一个 `status=active`。"
        "换目标时旧目标转为 `archived` 而非删除，其测评结果与进度仍可回看。"
    ),
)
def student_list_goals(user: TempUser = Depends(require_temp_user)) -> list[dict[str, Any]]:
    return biz.list_goals(user.user_id)


@router.get(
    "/goal/overview",
    tags=["前台 · 岗位探索与详情"],
    summary="目标概览（当前目标 + 晋升路径 + 测评结果）",
    description=(
        "「岗位学习与自适应路径」页顶部卡片的数据源，一次取齐三块：\n\n"
        "- **当前活跃目标**：岗位名/职级/职责/归属专业、技能项数\n"
        "- **下一级成长目标**：沿 `advances_to` 的下一级岗位，及进阶需补的关键技能\n"
        "- **测评结果**：该用户针对**这个岗位**最近一次报告（匹配度/雷达/优势/短板）\n\n"
        "三者都按岗位绑定，换目标即换整套数据。\n\n"
        "注意：`advances_to` 边目前只覆盖约 1% 的岗位，多数情况下 `next_level` 为 null，"
        "前端应隐藏该区块而非报错。"
    ),
)
def student_goal_overview(
    occupation_id: str | None = Query(None, description="留空取当前活跃目标"),
    user: TempUser = Depends(require_temp_user),
) -> dict[str, Any]:
    from backend.kg.pg_store.goal_overview import goal_overview

    return goal_overview(user.user_id, occupation_id)


# ── AI 诊断 ──────────────────────────────────────────────────


@router.post(
    "/diagnosis/resume",
    tags=["前台 · AI 诊断"],
    summary="简历智能诊断",
    description="对齐 vDiagResume：粘贴简历 → 规则解析技能 → 可选对标岗位出报告。",
)
def diag_resume(
    body: ResumeDiagBody,
    user: TempUser = Depends(require_temp_user),
) -> dict[str, Any]:
    occ = body.target_occupation_id
    if not occ:
        g = biz.get_goal(user.user_id)
        occ = (g or {}).get("occupation_id")
    return biz.create_resume_diagnosis(
        user.user_id,
        user.user_name,
        content_text=body.content_text,
        target_occupation_id=occ,
    )


@router.post(
    "/diagnosis/resume/upload",
    tags=["前台 · AI 诊断"],
    summary="上传简历文件诊断（PDF / DOCX / TXT）",
    description=(
        "对应原型「拖拽简历文件到此处」：`multipart/form-data` 上传，"
        "服务端抽取文本后走与 `POST /diagnosis/resume` 相同的诊断流程。\n\n"
        "- 支持 **PDF / DOCX / TXT**，单文件 ≤ 20MB\n"
        "- PDF 优先 pypdf 抽取，失败回退 PyMuPDF；**扫描件/图片型 PDF 无法提取文字**，"
        "此时返回 400 并提示改用文本粘贴\n"
        "- 未显式传 `target_occupation_id` 时自动取当前锁定目标岗位\n\n"
        "返回结构与 `POST /diagnosis/resume` 一致，额外带 `source_file`。"
    ),
)
async def diag_resume_upload(
    file: UploadFile = File(..., description="简历文件（PDF/DOCX/TXT，≤20MB）"),
    target_occupation_id: str | None = Query(
        None, description="对标岗位 id；缺省取当前锁定目标"
    ),
    user: TempUser = Depends(require_temp_user),
) -> dict[str, Any]:
    from backend.api.resume_parse import ResumeParseError, parse_resume_bytes

    data = await file.read()
    try:
        text = parse_resume_bytes(file.filename or "", data)
    except ResumeParseError as e:
        raise HTTPException(400, str(e)) from e

    occ = target_occupation_id
    if not occ:
        g = biz.get_goal(user.user_id)
        occ = (g or {}).get("occupation_id")
    out = biz.create_resume_diagnosis(
        user.user_id,
        user.user_name,
        content_text=text,
        target_occupation_id=occ,
    )
    out["source_file"] = {
        "filename": file.filename,
        "size": len(data),
        "chars": len(text),
    }
    return out


@router.get(
    "/diagnosis/resume/sample",
    tags=["前台 · AI 诊断"],
    summary="范例简历（一键体验用）",
    description=(
        "对应原型「使用标准范例简历一键体验解析」。"
        "范例文本刻意使用库内真实存在的技能名（配料准备 / 搅拌操作 / 泵送操作 …），"
        "因此解析后能命中技能库并算出有意义的匹配度。"
    ),
    response_description="{ content_text, note }",
)
def diag_resume_sample(user: TempUser = Depends(require_temp_user)) -> dict[str, Any]:
    _ = user
    from backend.api.resume_parse import SAMPLE_RESUME

    return {
        "content_text": SAMPLE_RESUME,
        "note": "把 content_text 提交给 POST /v1/student/diagnosis/resume 即可体验",
    }


@router.post(
    "/diagnosis/chat/sessions",
    tags=["前台 · AI 诊断"],
    summary="开启对话测评会话",
    description="对齐 vDiagChat：创建会话并返回首问。",
)
def diag_chat_start(
    body: ChatSessionBody,
    user: TempUser = Depends(require_temp_user),
) -> dict[str, Any]:
    occ = body.target_occupation_id
    if not occ:
        g = biz.get_goal(user.user_id)
        occ = (g or {}).get("occupation_id")
    return biz.create_chat_session(
        user.user_id, user.user_name, target_occupation_id=occ
    )


@router.post(
    "/diagnosis/chat/sessions/{session_id}/messages",
    tags=["前台 · AI 诊断"],
    summary="提交对话回答",
    description="学员回复后规则打分并结束会话，返回报告。",
)
def diag_chat_msg(
    session_id: int,
    body: ChatMessageBody,
    user: TempUser = Depends(require_temp_user),
) -> dict[str, Any]:
    try:
        return biz.post_chat_message(session_id, user.user_id, body.content)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get(
    "/diagnosis/report",
    tags=["前台 · AI 诊断"],
    response_model=DiagnosisReportOut | None,
    summary="能力诊断报告",
    description="对齐 vDiagReport：匹配度、雷达、缺口。可按 session_id 或最近一次。",
)
def diag_report(
    session_id: int | None = Query(None),
    occupation_id: str | None = Query(None, description="无报告时按画像+岗位现算"),
    user: TempUser = Depends(require_temp_user),
) -> DiagnosisReportOut | None:
    if not occupation_id:
        g = biz.get_goal(user.user_id)
        occupation_id = (g or {}).get("occupation_id")
    rep = biz.get_diagnosis_report(
        user.user_id, session_id=session_id, occupation_id=occupation_id
    )
    return DiagnosisReportOut.model_validate(rep) if rep else None


# ── 学习中心 ─────────────────────────────────────────────────


@router.get(
    "/learn/path",
    tags=["前台 · 学习路径"],
    response_model=LearningPathOut | None,
    summary="当前学习路径",
    description="对齐 vLearnPath / vLearnCenter 路径进度。",
)
def learn_path_get(user: TempUser = Depends(require_temp_user)) -> LearningPathOut | None:
    p = biz.get_active_path(user.user_id)
    return LearningPathOut.model_validate(p) if p else None


@router.post(
    "/learn/path/generate",
    tags=["前台 · 学习路径"],
    response_model=LearningPathOut,
    summary="按目标/诊断生成学习路径",
    description="缺口技能优先；无 goal 时 body.occupation_id 必填。",
)
def learn_path_gen(
    body: PathGenerateBody,
    user: TempUser = Depends(require_temp_user),
) -> LearningPathOut:
    try:
        p = biz.generate_path(
            user.user_id, user.user_name, occupation_id=body.occupation_id
        )
        # attach progress
        full = biz.get_active_path(user.user_id)
        return LearningPathOut.model_validate(full or p)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post(
    "/learn/steps/{step_id}/complete",
    tags=["前台 · 学习路径"],
    response_model=LearningPathOut,
    summary="完成学习步骤",
    description="对齐「已标记学完」。",
)
def learn_step_done(
    step_id: int,
    user: TempUser = Depends(require_temp_user),
) -> LearningPathOut:
    try:
        p = biz.complete_step(user.user_id, user.user_name, step_id)
        return LearningPathOut.model_validate(p)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get(
    "/learn/resources",
    tags=["前台 · 学习路径"],
    response_model=ResourceListOut,
    summary="学习资源列表",
    description="对齐资源卡片；当前映射 KG course 节点。",
)
def learn_resources(
    skill_id: str | None = Query(None, description="关联技能 id（提示用）"),
    q: str | None = Query(None, description="资源标题关键字"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: TempUser = Depends(require_temp_user),
) -> ResourceListOut:
    _ = user
    return ResourceListOut.model_validate(
        biz.list_resources(skill_id=skill_id, q=q, page=page, page_size=page_size)
    )


# ── 我的 ─────────────────────────────────────────────────────


@router.get(
    "/me",
    tags=["前台 · 我的"],
    response_model=MeOut,
    summary="我的主页摘要",
    description="对齐 vMe：目标、成长值、徽章、技能画像、当前路径。",
)
def me(user: TempUser = Depends(require_temp_user)) -> MeOut:
    return MeOut.model_validate(biz.me_summary(user.user_id, user.user_name))


@router.get(
    "/me/badges",
    tags=["前台 · 我的"],
    response_model=list[BadgeDefOut],
    summary="成就定义列表",
    description="全部徽章配置；已解锁见 GET /me.badges。",
)
def me_badge_defs(user: TempUser = Depends(require_temp_user)) -> list[BadgeDefOut]:
    _ = user
    return [BadgeDefOut.model_validate(x) for x in biz.list_badge_defs()]


@router.get(
    "/me/skills",
    tags=["前台 · 我的"],
    summary="我的技能画像",
)
def me_skills(user: TempUser = Depends(require_temp_user)) -> list[dict[str, Any]]:
    return biz.me_summary(user.user_id, user.user_name).get("skills") or []
