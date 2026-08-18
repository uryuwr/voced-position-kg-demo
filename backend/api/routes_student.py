"""
学生端业务 API —— 对齐 Open-Q frontend.html 四大模块：
  探索 · AI 诊断 · 学习中心 · 我的
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from backend.api.auth_temp import TempUser, require_temp_user
from backend.api.schemas_student import (
    ChatMessageOut,
    ChatSessionOut,
    ClearGoalOut,
    DiagnosedOccupationListOut,
    GoalItem,
    GoalOverviewOut,
    LearningPlanBody,
    LearningPlanCreatedOut,
    LearningPlanItem,
    PositionMatchOut,
    ResumeExtractOut,
    ResumeSampleOut,
    PositionCoursesOut,
    SkillCompositionOut,
    StudentProfileOut,
    UserSkillItem,
)
from backend.api.schemas_biz import (
    BadgeDefOut,
    ChatMessageBody,
    ChatSessionBody,
    DiagnosisReportOut,
    GoalOut,
    GoalPutBody,
    IndustryListOut,
    MeOut,
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
from backend import settings
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
    # 一个请求一条连接：原先这里是 get_profession + profession_positions +
    # profession_ladder 三次独立 connect()，而 ladder 内部又把 positions 查了第二遍。
    from backend.kg.pg_store.client import session

    with session() as conn:
        p = biz.get_profession(profession_id, conn=conn)
        if not p:
            raise HTTPException(404, "profession not found")
        positions = biz.profession_positions(profession_id, conn=conn)
        # ladder 只取前 4 条，顺序与 positions 一致，直接吃现成列表
        ladder_raw = biz.profession_ladder(profession_id, positions=positions)
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
    response_model=SkillCompositionOut,
)
def student_position_skill_composition_q(
    id: str = Query(..., description="岗位节点 id"),
    user: TempUser = Depends(require_temp_user),
) -> SkillCompositionOut:
    _ = user
    try:
        from backend.kg.pg_store.skill_aggregate import occupation_skill_composition

        return occupation_skill_composition(id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@router.get(
    "/positions/courses",
    tags=["前台 · 岗位探索与详情"],
    summary="岗位相关课程（按技能分组）",
    description=(
        "沿 occupation -requires-> skill_level -taught_by-> course 聚合课程资源。\n\n"
        "**与岗位详情接口相互独立**：详情接口不返回课程，本接口专供「岗位相关课程」卡片。\n\n"
        "`kind` 区分资源性质：`real` = 平台真实课程（带 `learner_count` 可判热度）；"
        "`landing` = 检索入口（课程库无覆盖时的兜底，点开是搜索页而非课程）。"
        "前端务必分开展示，否则「有 N 门课」会掩盖资源质量差异。\n\n"
        "**课标目录条目默认不返回**（`include_catalog=false`）：那批 MOE_CN 课程的链接"
        "全指向教育部「专业教学标准」大类列表页，与具体技能无关，对学员没有意义。"
        "`catalog_count` 仍会给出数量、`catalog_hidden` 标记是否被隐藏，"
        "便于观察数据缺口。要看课标体系（管理台/专业维度）传 `include_catalog=true`。"
    ),
    response_model=PositionCoursesOut,
)
def student_position_courses(
    id: str = Query(..., description="岗位节点 id"),
    limit_per_skill: int = Query(5, ge=1, le=20, description="每个技能最多返回几门课"),
    include_catalog: bool = Query(
        False,
        description="是否返回课标目录条目。默认 false —— 它们点开是专业教学标准目录页，不是课程",
    ),
    user: TempUser = Depends(require_temp_user),
) -> PositionCoursesOut:
    _ = user
    try:
        from backend.kg.pg_store.skill_aggregate import occupation_courses

        return PositionCoursesOut.model_validate(
            occupation_courses(
                id, limit_per_skill=limit_per_skill, include_catalog=include_catalog
            )
        )
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@router.get(
    "/positions/match",
    tags=["前台 · 岗位探索与详情"],
    summary="岗位匹配度（岗位详情页用）",
    description=(
        "**级联取数，按证据强度从高到低**，命中即返回，不做无谓计算：\n\n"
        "| 优先级 | source | 来源 | 是否调模型 |\n"
        "| --- | --- | --- | --- |\n"
        "| 1 | `diagnosis` | 该用户对**这个岗位**最近一次诊断报告的 match_score | 否 |\n"
        "| 2 | `assessment` | 测评沉淀的实测技能画像（biz_user_skill）现算 | 否 |\n"
        "| 3 | `memory` | 五维记忆经图谱召回 + 模型定级推断 | **是**（约 5–10s） |\n"
        "| 4 | `none` | 无任何证据 | 否 |\n\n"
        "级联之前还有一道判定：该岗位**一项技能都没配要求档**时直接返回 "
        "`source=no_baseline`、`match_score=null` —— 没有基准就算不出达标率，"
        "此时显示的是数据缺口而不是学员水平。\n\n"
        "级联之后还有一道**服务端降级**：岗位缺要求档的技能权重占比**超过 30%**"
        "（阈值是服务端常量 `PARTIAL_BASELINE_PCT`，不要在前端各定一份）时，"
        "`source` 与 `score_status` 都改成 `partial_baseline`，"
        "`match_score` 仍照给 —— 已配置的那部分是真实依据，但只依据 18% 的权重算出的 "
        "50% 会被学员读成「我匹配一半」，必须由服务端明确标成「仅供参考」。"
        "证据强度这时看 `estimated` / `diagnosis` / `profile` 三个字段。\n\n"
        "做过诊断就直接用报告里的数——它由学员实际作答算出，比任何实时推断都准，"
        "也省掉一次模型调用。因此**列表页不再展示匹配度**（避免整页触发模型），"
        "只在进入岗位详情时按需计算。\n\n"
        "算法与诊断报告同源：单项达标率 `min(用户档/要求档, 1)`，"
        "总分 `Σ(达标率×权重)/Σ权重×100`。`estimated=true` 表示是推断值而非实测。"
    ),
    response_description=(
        "{ occupation, match_score, source, estimated, items[], strengths[], gaps[], radar }"
    ),
    response_model=PositionMatchOut,
)
def student_position_match(
    position_id: str = Query(..., description="岗位节点 id（含冒号，用 query 传）"),
    limit: int = Query(50, ge=1, le=200, description="参与比对的技能条数上限"),
    allow_memory: bool = Query(
        True, description="无实测证据时是否允许用五维记忆推断（会调模型，较慢）"
    ),
    user: TempUser = Depends(require_temp_user),
) -> PositionMatchOut:
    from backend.kg.pg_store.config import (
        PARTIAL_BASELINE_PCT,
        degrade_for_baseline_gap,
        is_partial_baseline,
    )
    from backend.kg.pg_store.query import get_node
    from backend.kg.pg_store.skill_aggregate import occupation_skill_bundles
    from backend.userprofile import assessment_levels, diagnosed_match, get_profile

    occ = get_node(position_id, scope="public")
    if not occ or occ.get("type") != "occupation":
        raise HTTPException(404, "position not found")
    occ_brief = {"id": occ.get("id"), "name": occ.get("name"), "level": occ.get("level")}

    def _mark_partial(detail: dict[str, Any]) -> dict[str, Any]:
        """岗位配置不全时把 `source` 降级成 partial_baseline（分数不动）。

        为什么让 `source` 让位：它回答的是「展示这个数时最该先提醒学员什么」——
        既有的 `no_baseline` 已经是这个用法（缺口 100% 时压过证据强度），
        `partial_baseline` 只是同一逻辑的连续形态，缺口 82% 与 100% 不该一个报数据
        缺口一个报「实测」。证据强度并没有丢：`estimated`（实测/推断）、
        `diagnosis`（测过没测过）、`profile` 三个字段都还在。

        阈值与 `score_status` 同一个函数、同一个常量（PARTIAL_BASELINE_PCT），
        两个字段不会一个说 partial 一个说 ok。
        """
        if is_partial_baseline(detail.get("no_baseline_weight")):
            detail["source"] = "partial_baseline"
            detail["reason"] = detail.get("reason") or (
                f"该岗位 {detail.get('no_baseline_weight')}% 的技能权重尚未配置能力要求档"
                f"（超过 {PARTIAL_BASELINE_PCT:g}% 即降级）；分数只依据已配置的那部分算出，"
                "仅供参考，需运营补齐岗位标准"
            )
        return detail

    # ① 诊断过就直接用报告里的匹配度
    diag = diagnosed_match(user.user_id, position_id)

    required = occupation_skill_bundles(position_id, limit=limit)
    if not required:
        return {
            "occupation": occ_brief, "match_score": diag["match_score"] if diag else None,
            "source": "diagnosis" if diag else "none", "estimated": False,
            "reason": "该岗位尚未配置技能构成",
            "items": [], "strengths": [], "gaps": [], "radar": {},
            "diagnosis": diag,
        }

    # ①.5 该岗位一项技能都没配要求档 → 没有基准就算不出达标率。
    # 必须在级联之前判掉：往下走会被 covered_count==0 误判成 no_overlap，
    # 文案变成「你的画像未覆盖该岗位要求的技能」，把数据缺口说成学员的问题。
    if not biz.has_baseline(required):
        detail = biz.match_with_profile(occ_brief, required, assessment_levels(user.user_id))
        detail.update({
            "match_score": None, "source": "no_baseline", "estimated": False,
            "reason": "该岗位的技能尚未配置能力要求档，无法计算匹配度（需运营补齐岗位标准）",
            "diagnosis": diag,
        })
        return detail

    if diag:
        # 明细仍按实测画像现算（供优势/短板/雷达展示），但总分以报告为准
        detail = biz.match_with_profile(occ_brief, required, assessment_levels(user.user_id))
        detail.update({
            "match_score": diag["match_score"],
            "source": "diagnosis",
            "estimated": False,
            "diagnosis": diag,
            # score_status 要描述**这里返回的那个分数**。返回的是报告分（必有值），
            # 而 detail 里的状态是按实测画像现算的：画像与该岗位零交集时它会是
            # no_evidence，与「明明给了一个数」自相矛盾。按基准缺口重判一次。
            "score_status": degrade_for_baseline_gap("ok", detail.get("no_baseline_weight")),
        })
        return _mark_partial(detail)

    # ② 有实测画像 → 现算（无模型调用）
    no_overlap: dict[str, Any] | None = None   # 有画像但与该岗位零交集时的兜底明细
    a = assessment_levels(user.user_id)
    if a:
        detail = biz.match_with_profile(occ_brief, required, a)
        if detail.get("covered_count"):
            detail.update({"source": "assessment", "estimated": True,
                           "profile": {"assessment_count": len(a)}})
            return _mark_partial(detail)
        # 零交集时 0% 会被读成「完全不匹配」，实际是「这些技能一项都没测过」
        no_overlap = detail

    # ③ 退到五维记忆推断（会调模型）
    if allow_memory:
        prof = get_profile(user.user_id)
        if prof["source"] != "none":
            detail = biz.match_with_profile(occ_brief, required, prof["levels"])
            if not detail.get("covered_count"):
                detail.update({
                    "match_score": None, "source": "no_overlap", "estimated": False,
                    "reason": "你的能力画像未覆盖该岗位要求的技能，"
                              "先针对该岗位做一次 AI 诊断才能得到匹配度",
                })
                return detail
            detail.update({
                "source": "memory", "estimated": True,
                "profile": {
                    "memory_count": prof["memory_count"],
                    "engine": (prof.get("meta") or {}).get("engine"),
                    "note": (prof.get("meta") or {}).get("error"),
                },
            })
            return _mark_partial(detail)

    # ④ 无任何证据：不要用 0% 冒充「完全不匹配」
    if no_overlap is not None:
        no_overlap.update({
            "match_score": None, "source": "no_overlap", "estimated": False,
            "reason": "你的能力画像未覆盖该岗位要求的技能，"
                      "先针对该岗位做一次 AI 诊断才能得到匹配度",
        })
        return no_overlap
    return {
        "occupation": occ_brief, "match_score": None, "source": "none", "estimated": False,
        "reason": "尚无测评记录与画像数据，先做一次 AI 诊断",
        "items": [], "strengths": [], "gaps": [], "radar": {},
    }


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
    response_model=ClearGoalOut,
)
def student_clear_goal(
    occupation_id: str | None = Query(None, description="只清除该岗位目标"),
    user: TempUser = Depends(require_temp_user),
) -> ClearGoalOut:
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
    response_model=list[GoalItem],
)
def student_list_goals(user: TempUser = Depends(require_temp_user)) -> list[GoalItem]:
    return biz.list_goals(user.user_id)


@router.post(
    "/goal/learning-plan",
    tags=["前台 · 岗位探索与详情"],
    summary="基于诊断短板生成学习计划并推送到学习空间",
    description=(
        "对应报告页「基于短板一键生成个人自适应学习计划」。\n\n"
        "本服务按诊断结果编排出**阶段任务树**（技能按短板优先排序、按技能大类分阶段、"
        "有课程边的挂上可学资源），推送给学习空间服务，返回它的 `plan_id`。\n\n"
        "**计划内容与学习进度都不在本服务**：这里只保存一行关联记录，"
        "同时把 `plan_id` 写进该次诊断报告的 `learning_plan_id`，两处都能查到。\n\n"
        "**幂等**：一次诊断对应一条路径。同一个 `session_id` 重复调用不会产生新计划，"
        "返回 `created=false` 与原有 `plan_id`；重新测评会产生新会话，"
        "推送时自动带上换代关系，旧计划被归档并在 `superseded_plan_id` 里返回。\n\n"
        "**没有本地兜底**：学习空间不可用时返回 502，不会发一个假的 `plan_id`——"
        "假 id 会让学员点进去看到空白页。前端需要按错误码给文案，"
        "并允许稍后重试（记录已存为 `push_status=failed`，重试不会产生重复计划）。"
    ),
    response_model=LearningPlanCreatedOut,
    responses={
        400: {"description": "该会话还没有诊断结果，或它没有目标岗位——都无法编排出有效路径"},
        404: {"description": "会话不存在，或不属于当前用户"},
        409: {"description": "同一路径 id 的内容发生了变化（多为两次推送之间图谱数据被改），需人工确认"},
        502: {"description": "学习空间服务未配置或不可用"},
    },
)
def student_create_learning_plan(
    body: LearningPlanBody,
    user: TempUser = Depends(require_temp_user),
) -> LearningPlanCreatedOut:
    from backend.learningplan import build_payload, external_path_id_for, push_learning_plan
    from backend.learningplan.courses import courses_for_skill_keys
    from backend.learningplan.push import PlanPushError, payload_sha256

    sess = biz.session_for_learning_plan(user.user_id, body.session_id)
    # 不存在与越权同样回 404：不泄漏「这个 id 是否存在」（CLAUDE.md 约定）
    if not sess:
        raise HTTPException(404, "诊断会话不存在")
    if not sess.get("has_result"):
        raise HTTPException(400, "该诊断会话还没有出结果，请先完成诊断再生成学习计划")
    occ = sess.get("target_occupation_id")
    if not occ:
        raise HTTPException(400, "该诊断会话没有目标岗位，无法生成学习计划")

    skills = biz.position_skills(occ, limit=12, aggregate=True)
    report = biz.get_diagnosis_report(user.user_id, session_id=body.session_id) or {}
    courses = (
        courses_for_skill_keys([s.get("skill_key") or s.get("name") or "" for s in skills])
        if body.recommend_resources
        else {}
    )

    path_id = external_path_id_for(settings.KG_REGION, body.session_id)
    try:
        payload = build_payload(
            session_id=body.session_id,
            region=settings.KG_REGION,
            occupation_id=occ,
            occupation_name=sess.get("occupation_name") or occ,
            skills=skills,
            report=report,
            courses_by_key=courses,
            revision_of=biz.previous_external_path_id(
                user.user_id, occ, exclude_external_path_id=path_id
            ),
        )
    except ValueError as e:
        # 岗位没有技能构成之类的数据问题，属于「当前状态做不了」，不是服务器错误
        raise HTTPException(400, str(e)) from e

    digest = payload_sha256(payload)
    try:
        res = push_learning_plan(payload, uc_user_id=user.user_id)
    except PlanPushError as e:
        # 失败也落库：支持重推，且能查到是哪一次、错在哪，不静默丢弃
        biz.save_learning_plan(
            user.user_id, occ, "", session_id=body.session_id,
            external_path_id=path_id, payload_sha256=digest,
            push_status="failed", last_error=str(e)[:2000],
        )
        raise HTTPException(
            {"conflict": 409, "rejected": 422}.get(e.kind, 502), str(e)
        ) from e

    row = biz.save_learning_plan(
        user.user_id, occ, res["plan_id"], session_id=body.session_id,
        external_path_id=path_id, payload_sha256=digest, push_status="ok",
        superseded_plan_id=res.get("superseded_plan_id"),
    )
    return {
        "plan_id": res["plan_id"],
        "created": res["created"],
        "superseded_plan_id": res.get("superseded_plan_id"),
        "occupation_id": occ,
        "session_id": body.session_id,
        "phases_count": len(payload.phases),
        "tasks_count": sum(len(p.tasks) for p in payload.phases),
        "pushed_at": row.get("pushed_at"),
    }


@router.get(
    "/goal/learning-plans",
    tags=["前台 · 岗位探索与详情"],
    summary="我的学习计划关联记录",
    description="按岗位查该学员生成过的学习计划 id（内容在外部服务，这里只存关联）。",
    response_model=list[LearningPlanItem],
)
def student_list_learning_plans(
    occupation_id: str | None = Query(None),
    user: TempUser = Depends(require_temp_user),
) -> list[LearningPlanItem]:
    return biz.list_learning_plans(user.user_id, occupation_id)


@router.get(
    "/goal/diagnosed",
    tags=["前台 · 岗位探索与详情"],
    summary="已诊断/锁定过的岗位列表",
    description=(
        "「岗位学习与自适应路径」页的一级视图：列出该用户做过诊断或锁定过的岗位，"
        "每项带岗位基础信息（名称/职级/职责）、**当前匹配度**、**诊断时间**与测评次数。\n\n"
        "点某一项后再用 `GET /goal/overview?occupation_id=...` 取该岗位的详情卡片。\n\n"
        "**分页返回** `{items, total, page, page_size, pages}`：每换一次目标就多一条记录，"
        "合并/排序/切片都在 SQL 层完成。活跃目标置顶，其余按最近诊断时间倒序；"
        "刚锁定尚未测评的岗位 `match_score` 为 null，`plan_id` 未生成时为空串。"
    ),
    response_model=DiagnosedOccupationListOut,
)
def student_diagnosed_occupations(
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=100),
    user: TempUser = Depends(require_temp_user),
) -> DiagnosedOccupationListOut:
    from backend.kg.pg_store.goal_overview import diagnosed_occupations

    return diagnosed_occupations(user.user_id, page=page, page_size=page_size)


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
    response_model=GoalOverviewOut,
)
def student_goal_overview(
    occupation_id: str | None = Query(None, description="留空取当前活跃目标"),
    user: TempUser = Depends(require_temp_user),
) -> GoalOverviewOut:
    from backend.kg.pg_store.goal_overview import goal_overview

    return goal_overview(user.user_id, occupation_id)


@router.get(
    "/profile",
    tags=["前台 · 岗位探索与详情"],
    summary="我的画像（五维记忆 + 技能画像 + 诊断记录）",
    description=(
        "一次摊开匹配度用到的全部数据：\n\n"
        "- `memory.facets` —— **五维记忆全量**（身份/情境/偏好/经验/活动），"
        "每维带条数、逐条明细与一行摘要；`memory.raw` 是画像服务的**原始响应**\n"
        "- `assessment` —— 测评/诊断沉淀的实测技能档位（biz_user_skill，跨岗位累积）\n"
        "- `skills.merged` —— 参与匹配度计算的合并画像（实测覆盖记忆推断）\n"
        "- `diagnoses` —— 各岗位最近一次诊断的匹配度\n\n"
        "**默认只调画像服务（约 1–2s），不调模型**：五维原文直接展示即可。"
        "技能画像那部分只读缓存；要重新解析（会调模型 5–10s）加 `parse=1`。"
    ),
    response_model=StudentProfileOut,
)
def student_profile(
    parse: bool = Query(False, description="重新把记忆解析成技能画像（会调模型，5–10s）"),
    user: TempUser = Depends(require_temp_user),
) -> StudentProfileOut:
    from backend.kg.pg_store.client import connect
    from backend.userprofile import assessment_levels, get_profile
    from backend.userprofile import memories as mem
    from backend.userprofile import skill_profile as sp

    a = assessment_levels(user.user_id)

    # 五维记忆：展示用，拉全量五维；只走 BTS，不调模型
    memory: dict[str, Any] = {
        "available": mem.available(),
        "endpoint": settings.OPENQ_AI_MANAGER or None,
        "path": mem.SEARCH_PATH,
        "facets": [],
        "raw": None,
        "request": None,
        "error": None,
    }
    if mem.available():
        req_body = {"facets": [{"facet": f, "limit": n} for f, n in mem.ALL_FACETS]}
        memory["request"] = {
            "method": "POST",
            "url": (settings.OPENQ_AI_MANAGER or "") + mem.SEARCH_PATH,
            "headers": {
                "Authorization": "BTS id=...,nonce=...,mac=...",
                "Userid": str(user.user_id),
                "sdp-app-id": settings.BTS_SDP_APP_ID or settings.SDP_APP_ID,
                "Content-Type": "application/json",
            },
            "body": req_body,
        }
        try:
            raw = mem.search_memories(user.user_id, facets=mem.ALL_FACETS)
            memory["raw"] = raw
            memory["facets"] = mem.facet_view(raw)
            memory["total"] = sum(f["count"] for f in memory["facets"])
        except Exception as e:  # noqa: BLE001 — 调试页要看见失败原因
            memory["error"] = str(e)[:400]
    else:
        memory["error"] = "用户画像服务未配置（OPENQ_AI_MANAGER / BTS）"

    # 技能画像：默认只读缓存，避免打开页面就触发模型
    cached = sp._cached(str(user.user_id))
    if parse or cached:
        prof = get_profile(user.user_id, use_cache=not parse)
    else:
        prof = {"levels": dict(a), "source": "assessment" if a else "none",
                "assessment_count": len(a), "memory_count": 0,
                "meta": {"engine": "not_parsed"}}

    with connect() as conn:
        diag_rows = conn.execute(
            """
            SELECT s.target_occupation_id AS occupation_id,
                   s.target_occupation_name AS occupation_name,
                   s.channel, r.match_score, r.created_at
            FROM biz_diagnosis_result r
            JOIN biz_diagnosis_session s ON s.id = r.session_id
            WHERE s.user_id = %s AND s.target_occupation_id IS NOT NULL
            ORDER BY r.created_at DESC LIMIT 20
            """,
            (user.user_id,),
        ).fetchall()
        skill_rows = conn.execute(
            "SELECT skill_name, level, score, source, updated_at "
            "FROM biz_user_skill WHERE user_id=%s ORDER BY level DESC, skill_name",
            (user.user_id,),
        ).fetchall()

    return {
        "user": {"id": user.user_id, "name": user.user_name},
        "memory": memory,
        "skills": {
            "source": prof["source"],
            "parsed": (prof.get("meta") or {}).get("engine") != "not_parsed",
            "counts": {
                "assessment": prof["assessment_count"],
                "memory": prof["memory_count"],
                "merged": len(prof["levels"]),
            },
            "meta": prof.get("meta") or {},
            "merged": [
                {"skill_key": k, "level": v, "from": "assessment" if k in a else "memory"}
                for k, v in sorted(prof["levels"].items(), key=lambda kv: -kv[1])
            ],
        },
        "assessment": [
            {
                "skill_name": r["skill_name"], "level": r["level"], "score": r["score"],
                "source": r["source"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in skill_rows
        ],
        "diagnoses": [
            {
                "occupation_id": r["occupation_id"],
                "occupation_name": r["occupation_name"],
                "channel": r["channel"],
                "match_score": r["match_score"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in diag_rows
        ],
    }


# ── AI 诊断 ──────────────────────────────────────────────────


@router.post(
    "/diagnosis/resume",
    tags=["前台 · AI 诊断"],
    summary="简历智能诊断",
    description="对齐 vDiagResume：粘贴简历 → 规则解析技能 → 可选对标岗位出报告。",
    response_model=DiagnosisReportOut,
)
def diag_resume(
    body: ResumeDiagBody,
    user: TempUser = Depends(require_temp_user),
) -> DiagnosisReportOut:
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
    response_model=DiagnosisReportOut,
)
async def diag_resume_upload(
    file: UploadFile = File(..., description="简历文件（PDF/DOCX/TXT，≤20MB）"),
    target_occupation_id: str | None = Query(
        None, description="对标岗位 id；缺省取当前锁定目标"
    ),
    user: TempUser = Depends(require_temp_user),
) -> DiagnosisReportOut:
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


@router.post(
    "/diagnosis/resume/extract",
    tags=["前台 · AI 诊断"],
    summary="上传简历文件 · 仅抽取文本（不触发诊断）",
    description=(
        "测评工作流的第 1 步需要的是**简历原文**，随后由工作流自己解析画像；"
        "而 `POST /diagnosis/resume/upload` 会直接跑完旧的一次性诊断流程，"
        "两者副作用不同，故单独提供一个只抽文本的入口（共用同一个解析器）。\n\n"
        "支持 PDF / DOCX / TXT，≤20MB；扫描件类图片型 PDF 提取不到文字会返回 400。"
    ),
    response_model=ResumeExtractOut,
)
async def diag_resume_extract(
    file: UploadFile = File(..., description="简历文件（PDF/DOCX/TXT，≤20MB）"),
    user: TempUser = Depends(require_temp_user),
) -> ResumeExtractOut:
    from backend.api.resume_parse import ResumeParseError, parse_resume_bytes

    _ = user
    data = await file.read()
    try:
        text = parse_resume_bytes(file.filename or "", data)
    except ResumeParseError as e:
        raise HTTPException(400, str(e)) from e
    return {
        "content_text": text,
        "filename": file.filename,
        "size": len(data),
        "chars": len(text),
    }


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
    response_model=ResumeSampleOut,
)
def diag_resume_sample(user: TempUser = Depends(require_temp_user)) -> ResumeSampleOut:
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
    response_model=ChatSessionOut,
)
def diag_chat_start(
    body: ChatSessionBody,
    user: TempUser = Depends(require_temp_user),
) -> ChatSessionOut:
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
    response_model=ChatMessageOut,
)
def diag_chat_msg(
    session_id: int,
    body: ChatMessageBody,
    user: TempUser = Depends(require_temp_user),
) -> ChatMessageOut:
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
    response_model=list[UserSkillItem],
)
def me_skills(user: TempUser = Depends(require_temp_user)) -> list[UserSkillItem]:
    return biz.me_summary(user.user_id, user.user_name).get("skills") or []
