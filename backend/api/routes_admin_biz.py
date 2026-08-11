"""管理端业务 API：看板 + 变更审核队列 + 技能多档 bundle。"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.api.auth import AuthUser, require_auth_user
from backend.api.schemas_biz import AdminDashboardOut
from backend.kg.pg_store import biz_store as biz
from backend.kg.pg_store import review as rev
from backend.kg.pg_store.skill_aggregate import get_skill_bundle
from backend.kg.pg_store.skill_write import (
    prepare_submit_payload,
    preview_skill_bundle,
)

router = APIRouter(prefix="/v1/admin", tags=[])


class ChangeSubmitBody(BaseModel):
    """提交待审变更。"""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "entity_kind": "node",
                    "action": "create",
                    "dim_type": "major",
                    "payload": {
                        "type": "major",
                        "name": "人工智能技术应用",
                        "region": "CN",
                        "industry_ids": [
                            "CN:industry:BOSS_ZHIPIN:boss:100000",
                        ],
                    },
                },
                {
                    "entity_kind": "node",
                    "action": "create",
                    "dim_type": "occupation",
                    "payload": {
                        "type": "occupation",
                        "name": "算法工程师",
                        "major_ids": ["CN:major:MOE_CN:…"],
                    },
                },
                {
                    "entity_kind": "node",
                    "action": "create",
                    "dim_type": "skill_level",
                    "payload": {
                        "type": "skill_level",
                        "name": "Python 编程 · 三级",
                        "occupation_ids": ["CN:occupation:…"],
                    },
                },
            ]
        }
    }

    entity_kind: Literal["node", "edge"] = Field(..., description="node|edge")
    action: Literal["create", "update", "delete", "disable", "enable"] = Field(
        ...,
        description="create 新建 / update 编辑 / delete 物理删除 / disable 停用 / enable 发布",
    )
    dim_type: str | None = Field(
        None, description="节点四维 type：industry|major|occupation|skill_level"
    )
    target_id: str | None = Field(None, description="编辑/删除/停用/发布时的目标 id")
    title: str | None = Field(None, description="列表展示标题")
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "节点字段 + 可选关联 id 列表（多选，服务端自动建边，客户端无需构造 edge）：\n"
            "- **专业 major**：`industry_ids: string[]` → 自动 major -belongs_to→ industry\n"
            "- **岗位 occupation**：`major_ids: string[]` → 自动 major -prepares_for→ occupation\n"
            "- **技能 skill_level**：`occupation_ids: string[]` → 自动 occupation -requires→ skill\n"
            "边默认 weight=0.8、confidence=manual_seed、status=published。"
        ),
    )


class ChangeOut(BaseModel):
    id: int
    entity_kind: str
    action: str
    dim_type: str | None = None
    target_id: str | None = None
    title: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"
    created_by: str | None = None
    created_by_name: str | None = None
    created_at: str | None = None
    # REVIEW_REQUIRED=0 直写时
    applied: dict[str, Any] | None = None
    direct: bool | None = None
    review_required: bool | None = None


@router.get(
    "/dashboard/summary",
    tags=["管理台 · 运营看板"],
    response_model=AdminDashboardOut,
    summary="运营看板摘要",
)
def dashboard(user: AuthUser = Depends(require_auth_user)) -> AdminDashboardOut:
    _ = user
    return AdminDashboardOut.model_validate(biz.admin_dashboard())


@router.get(
    "/ai-gateway",
    tags=["管理台 · 运营看板"],
    summary="AI 网关就绪态（非 UC）",
)
def ai_gateway_status(user: AuthUser = Depends(require_auth_user)) -> dict[str, Any]:
    _ = user
    from backend.agent.llm import gateway_info

    return gateway_info()


@router.get(
    "/edges/review",
    tags=["管理台 · 审核发布"],
    summary="低置信/AI 边抽检列表",
    description="默认 confidence=ai_inferred；可筛 prepares_for / requires。",
)
def admin_edges_review(
    confidence: str | None = Query("ai_inferred"),
    rel_type: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    region: str = Query("CN"),
    user: AuthUser = Depends(require_auth_user),
) -> dict[str, Any]:
    _ = user
    from backend.kg.pg_store.edge_review import list_edges_for_review

    return list_edges_for_review(
        confidence=confidence,
        rel_type=rel_type,
        page=page,
        page_size=page_size,
        region=region,
    )


@router.get(
    "/changes",
    tags=["管理台 · 审核发布"],
    response_model=list[ChangeOut],
    summary="待审变更列表",
    description="队列内仅待审；通过/驳回后记录删除。",
)
def list_changes(
    dim_type: str | None = Query(None, description="按四维类型过滤"),
    limit: int = Query(50, ge=1, le=200),
    user: AuthUser = Depends(require_auth_user),
) -> list[ChangeOut]:
    _ = user
    return [
        ChangeOut.model_validate(x)
        for x in rev.list_pending(limit=limit, dim_type=dim_type)
    ]


@router.post(
    "/changes",
    tags=["管理台 · 审核发布"],
    response_model=ChangeOut,
    summary="提交变更（默认直写；REVIEW_REQUIRED=1 时进待审）",
    description=(
        "四维节点与边的 新建/编辑/删除/停用/发布 均走此接口。\n\n"
        "- **REVIEW_REQUIRED=0（默认）**：服务端立即写主库，`status=applied`，"
        "发布仍受 BR 门禁；不入待审队列。\n"
        "- **REVIEW_REQUIRED=1**：入待审，须 approve 后生效。"
    ),
)
def submit_change(
    body: ChangeSubmitBody,
    user: AuthUser = Depends(require_auth_user),
) -> ChangeOut:
    from backend.kg.pg_store.write import CodeConflictError

    try:
        row = rev.submit_change(
            entity_kind=body.entity_kind,
            action=body.action,
            payload=body.payload,
            dim_type=body.dim_type,
            target_id=body.target_id,
            title=body.title,
            user_id=user.user_id,
            user_name=user.user_name,
        )
        return ChangeOut.model_validate(row)
    except CodeConflictError as e:
        # 必须在 ValueError 之前捕获（它是 ValueError 子类），否则会被压成 400，
        # 前端就无法据此把错误定位到「编码」字段。
        raise HTTPException(
            status_code=409,
            detail={
                "error": "code_conflict",
                "message": str(e),
                "field": "attrs.code",
                "code": e.code,
                "existing": e.existing,
            },
        ) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post(
    "/changes/{change_id}/approve",
    tags=["管理台 · 审核发布"],
    summary="审核通过并生效",
)
def approve_change(
    change_id: int,
    user: AuthUser = Depends(require_auth_user),
) -> dict[str, Any]:
    try:
        return rev.approve_change(
            change_id, user_id=user.user_id, user_name=user.user_name
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post(
    "/changes/{change_id}/reject",
    tags=["管理台 · 审核发布"],
    summary="驳回（删除待审记录）",
)
def reject_change(
    change_id: int,
    user: AuthUser = Depends(require_auth_user),
) -> dict[str, Any]:
    _ = user
    try:
        return rev.reject_change(change_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ── 发布门禁 BR-02~08 ────────────────────────────────────────


class PublishValidateBody(BaseModel):
    node_type: str | None = Field(
        None, description="major|occupation|skill_level|skill_bundle|…"
    )
    node_id: str | None = Field(None, description="节点 id（专业/岗位）")
    skill_key: str | None = Field(None, description="逻辑技能 key")
    region: str = "CN"
    action: str = Field(
        "enable", description="enable=发布校验 | delete=删除引用校验(BR-06)"
    )


@router.post(
    "/publish/validate",
    tags=["管理台 · 发布门禁"],
    summary="校验是否可发布（BR-02~06）",
    description=(
        "不写库。major→BR-02；occupation→BR-03；skill→BR-04+BR-05；"
        "action=delete 时 skill→BR-06。"
    ),
)
def admin_publish_validate(
    body: PublishValidateBody,
    user: AuthUser = Depends(require_auth_user),
) -> dict[str, Any]:
    _ = user
    from backend.kg.pg_store.publish_rules import validate_publish

    return validate_publish(
        node_type=body.node_type,
        node_id=body.node_id,
        skill_key=body.skill_key,
        region=body.region,
        action=body.action,
    )


@router.get(
    "/publish/validate",
    tags=["管理台 · 发布门禁"],
    summary="校验是否可发布（query）",
)
def admin_publish_validate_get(
    node_type: str | None = Query(None),
    node_id: str | None = Query(None),
    skill_key: str | None = Query(None),
    region: str = Query("CN"),
    action: str = Query("enable"),
    user: AuthUser = Depends(require_auth_user),
) -> dict[str, Any]:
    _ = user
    from backend.kg.pg_store.publish_rules import validate_publish

    return validate_publish(
        node_type=node_type,
        node_id=node_id,
        skill_key=skill_key,
        region=region,
        action=action,
    )


class PublishDemoteBody(BaseModel):
    region: str = "CN"
    dry_run: bool = Field(True, description="true=只报告不落库；false=降级为 draft")
    limit: int | None = Field(
        None, description="每类最多降级数（调试用）；空=全量"
    )


@router.post(
    "/publish/demote",
    tags=["管理台 · 发布门禁"],
    summary="扫描并降级不达标 published 节点为 draft",
    description=(
        "BR-07/08：不达标 major/occupation/skill 置 draft，"
        "草稿不再出现在前台 search/explore/学员列表。"
        "默认 dry_run=true。"
    ),
)
def admin_publish_demote(
    body: PublishDemoteBody,
    user: AuthUser = Depends(require_auth_user),
) -> dict[str, Any]:
    _ = user
    from backend.kg.pg_store.publish_rules import demote_noncompliant

    return demote_noncompliant(
        region=body.region,
        dry_run=body.dry_run,
        limit=body.limit,
    )


# ── 技能逻辑技能（一次多档）──────────────────────────────────


class OccupationLinkIn(BaseModel):
    occupation_id: str = Field(..., description="岗位节点 id")
    weight: float | None = Field(
        None,
        description="该技能在本岗位技能构成中的权重 0–1（>1 视为百分制会 /100）；写在 requires 边上",
    )
    required_level: int | None = Field(
        None, ge=1, le=5, description="岗位要求档 1–5；权重写在该档对应边上"
    )


class SkillLevelObjIn(BaseModel):
    """单档对象；可扩展 criteria / evidence 等。"""

    model_config = {"extra": "allow"}

    label: str | None = Field(None, description="了解/掌握/…")
    description: str | None = Field(None, description="能力等级描述")
    criteria: list[Any] | None = None
    evidence: str | None = None


class SkillBundleBody(BaseModel):
    """一次录入逻辑技能 + L1–L5 对象 + 岗位构成权重。"""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "skill_key": "流量承接",
                    "name": "流量承接",
                    "levels": {
                        "L1": {
                            "label": "了解",
                            "description": "了解站内外流量入口构成",
                        },
                        "L2": {"label": "掌握", "description": "能按 SOP 承接单渠道"},
                        "L3": {"label": "熟练", "description": "能拆解结构并给方案"},
                        "L4": {"label": "精通", "description": "能设计多渠道矩阵"},
                        "L5": {"label": "专家", "description": "能沉淀方法论并复制"},
                    },
                    "occupation_links": [
                        {
                            "occupation_id": "CN:occupation:MOHRSS_CN:4-12-01-01",
                            "weight": 0.25,
                            "required_level": 3,
                        }
                    ],
                }
            ]
        }
    }

    skill_key: str | None = Field(None, description="聚合主键；缺省用 name")
    name: str | None = Field(None, description="展示名；缺省用 skill_key")
    region: str = "CN"
    scale: str = Field("l1_l5", description="等级尺度")
    levels: dict[str, SkillLevelObjIn | str | dict[str, Any]] = Field(
        default_factory=dict,
        description="键 L1–L5；值为对象（推荐）或字符串。创建时至少一档；更新可省略以保留原档",
    )
    occupation_links: list[OccupationLinkIn] = Field(
        default_factory=list,
        description="岗位技能构成：权重在 requires 边上",
    )
    occupation_ids: list[str] = Field(
        default_factory=list,
        description="兼容：无权重时的岗位 id 列表",
    )
    description: str | None = None
    source_url: str | None = None
    confidence: str | None = "manual_seed"
    attrs: dict[str, Any] | None = None


@router.get(
    "/skills",
    tags=["管理台 · 技能多档"],
    summary="逻辑技能列表（管理端，含草稿）",
    description=(
        "按 skill_key 聚合。默认 scope=manage 可见 draft/disabled；"
        "可按 status=published|draft|disabled 筛选。含聚合 status 字段。"
    ),
)
def admin_list_skills(
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    region: str | None = Query("CN"),
    status: str | None = Query(
        None, description="published|draft|disabled；空=全部（不含 archived）"
    ),
    has_level: str | None = Query(None),
    occupation_id: str | None = Query(None),
    user: AuthUser = Depends(require_auth_user),
) -> dict[str, Any]:
    _ = user
    from backend.kg.pg_store.skill_aggregate import list_skill_bundles

    return list_skill_bundles(
        q=q,
        page=page,
        page_size=page_size,
        region=region,
        occupation_id=occupation_id,
        has_level=has_level,
        status=status,
        scope="manage",
        published_only=False,
    )


@router.post(
    "/skills",
    tags=["管理台 · 技能多档"],
    response_model=ChangeOut,
    summary="新建逻辑技能（一次多档，进待审）",
    description=(
        "提交 skill_key + levels.L1..L5 对象 + occupation_links（含 weight）。"
        "审核通过后拆成 N 条 skill_level，requires 边写入岗位侧权重。"
        "不直接写库。"
    ),
)
def admin_create_skill_bundle(
    body: SkillBundleBody,
    user: AuthUser = Depends(require_auth_user),
) -> ChangeOut:
    try:
        raw = body.model_dump(exclude_none=True)
        # levels 内 Pydantic 对象转 dict
        lv = {}
        for k, v in (raw.get("levels") or {}).items():
            if hasattr(v, "model_dump"):
                lv[k] = v.model_dump(exclude_none=True)
            else:
                lv[k] = v
        raw["levels"] = lv
        payload = prepare_submit_payload(raw)
        row = rev.submit_change(
            entity_kind="node",
            action="create",
            dim_type="skill_level",
            payload=payload,
            title=f"新建技能(多档):{payload['skill_key']}",
            user_id=user.user_id,
            user_name=user.user_name,
        )
        return ChangeOut.model_validate(row)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.patch(
    "/skills/{skill_key:path}",
    tags=["管理台 · 技能多档"],
    response_model=ChangeOut,
    summary="更新逻辑技能（多档/岗链，进待审）",
)
def admin_patch_skill_bundle(
    skill_key: str,
    body: SkillBundleBody,
    user: AuthUser = Depends(require_auth_user),
) -> ChangeOut:
    try:
        raw = body.model_dump(exclude_none=True)
        raw["skill_key"] = skill_key
        lv = {}
        for k, v in (raw.get("levels") or {}).items():
            if hasattr(v, "model_dump"):
                lv[k] = v.model_dump(exclude_none=True)
            else:
                lv[k] = v
        raw["levels"] = lv
        if not raw.get("levels"):
            # 允许只改 occupation_links：payload 仍标 skill_bundle
            raw["levels"] = {}
            raw["kind"] = "skill_bundle"
            raw["expand_levels"] = True
            payload = {
                **raw,
                "kind": "skill_bundle",
                "type": "skill_level",
                "expand_levels": True,
                "skill_key": skill_key,
                "name": raw.get("name") or skill_key,
            }
        else:
            payload = prepare_submit_payload(raw)
        row = rev.submit_change(
            entity_kind="node",
            action="update",
            dim_type="skill_level",
            target_id=skill_key,
            payload=payload,
            title=f"更新技能(多档):{skill_key}",
            user_id=user.user_id,
            user_name=user.user_name,
        )
        return ChangeOut.model_validate(row)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post(
    "/skills/preview",
    tags=["管理台 · 技能多档"],
    summary="预览多档拆分结果（不写库、不进审）",
)
def admin_preview_skill_bundle(
    body: SkillBundleBody,
    user: AuthUser = Depends(require_auth_user),
) -> dict[str, Any]:
    _ = user
    try:
        raw = body.model_dump(exclude_none=True)
        lv = {}
        for k, v in (raw.get("levels") or {}).items():
            if hasattr(v, "model_dump"):
                lv[k] = v.model_dump(exclude_none=True)
            else:
                lv[k] = v
        raw["levels"] = lv
        return preview_skill_bundle(raw)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get(
    "/skills/{skill_key:path}",
    tags=["管理台 · 技能多档"],
    summary="查询逻辑技能详情（聚合读）",
)
def admin_get_skill_bundle(
    skill_key: str,
    region: str | None = Query("CN"),
    user: AuthUser = Depends(require_auth_user),
) -> dict[str, Any]:
    _ = user
    # 管理端可看 draft（published_only=False）
    b = get_skill_bundle(skill_key, region=region, published_only=False)
    if not b:
        raise HTTPException(404, "skill bundle not found")
    return b


class PrereqBody(BaseModel):
    prereq_skill_key: str = Field(..., description="先修逻辑技能 key")
    evidence: str | None = None
    region: str = "CN"


class PrereqSetBody(BaseModel):
    prereq_skill_keys: list[str] = Field(default_factory=list)
    region: str = "CN"


class CompositionSkillBody(BaseModel):
    """技能构成：添加/更新一项技能。"""

    model_config = {
        "json_schema_extra": {
            "examples": [{"skill_key": "搅拌操作", "level": 3, "weight": 0.3}]
        }
    }

    skill_key: str = Field(..., description="逻辑技能名（从 /composition/options 里选）")
    level: int | None = Field(
        None,
        ge=1,
        le=5,
        description="要求等级 1–5（对应 L1–L5）。留空取该技能最高档；"
        "该技能未配齐此档时返回 400 并列出可用档位",
    )
    weight: float | None = Field(
        None,
        ge=0,
        le=1,
        description="权重 0–1，**仅岗位生效**；专业技能不带权重（不参与归一化）",
    )


@router.get(
    "/composition/options",
    tags=["管理台 · 数据列表"],
    summary="技能构成 · 可选技能（支持按名称搜索）",
    description=(
        "技能构成抽屉底部下拉的数据源：按 `skill_key` 聚合的已有技能。\n\n"
        "- `q` 按**技能名模糊搜索**（技能上千条，下拉必须能搜）\n"
        "- 每项附 `available_levels`（该技能已配齐的档位）与 `level_completeness`，"
        "前端据此决定 L1–L5 哪些档可选"
    ),
)
def composition_options(
    q: str | None = Query(None, description="技能名关键字，模糊匹配"),
    region: str = Query("CN", description="区域，默认 CN"),
    limit: int = Query(50, ge=1, le=200),
    user: AuthUser = Depends(require_auth_user),
) -> list[dict[str, Any]]:
    _ = user
    from backend.kg.pg_store.skill_composition import list_skill_options

    return list_skill_options(q=q, region=region, limit=limit)


@router.get(
    "/composition",
    tags=["管理台 · 数据列表"],
    summary="技能构成（专业直连技能 / 岗位技能）",
    description=(
        "同时服务**专业**与**岗位**的技能构成页，字段对齐管理端原型。\n\n"
        "| 节点类型 | 关系 | 权重 |\n| --- | --- | --- |\n"
        "| `occupation` | `requires` | 有，可归一化 |\n"
        "| `major` | `covers`（E4） | **无**，专业技能不做归一化 |\n\n"
        "`node` 段为页面头部：所属行业 / 关联专业 / 职级 / 薪资 / 状态 / 版本 / 编码。\n"
        "每项技能返回 `available_levels`（该技能全部档）与 `selected_level`（当前选中档），"
        "前端即可渲染 L1–L5 档位按钮并高亮选中项。"
    ),
    response_description="{ node（头部）, relation, weighted, items[], weight_sum, normalized, can_normalize }",
)
def get_skill_composition(
    node_id: str = Query(..., description="专业或岗位的节点 id（含冒号，用 query 传）"),
    user: AuthUser = Depends(require_auth_user),
) -> dict[str, Any]:
    _ = user
    from backend.kg.pg_store.skill_composition import CompositionError, get_composition

    try:
        return get_composition(node_id)
    except CompositionError as e:
        raise HTTPException(400, str(e)) from e


@router.put(
    "/composition",
    tags=["管理台 · 数据列表"],
    summary="技能构成 · 添加或改档（幂等）",
    description=(
        "从已有技能中选一项加入构成，或改已有项的等级/权重。\n\n"
        "「选中等级」由**边指向哪个等级节点**表达，因此改档实现为"
        "先删该 skill_key 的旧边再建新边——对同一 skill_key 重复调用是幂等的。"
    ),
)
def put_skill_composition(
    body: CompositionSkillBody,
    node_id: str = Query(..., description="专业或岗位的节点 id"),
    user: AuthUser = Depends(require_auth_user),
) -> dict[str, Any]:
    from backend.kg.pg_store.skill_composition import CompositionError, set_skill

    try:
        return set_skill(
            node_id,
            body.skill_key,
            level=body.level,
            weight=body.weight,
            user_id=user.user_id,
            user_name=user.user_name,
        )
    except CompositionError as e:
        raise HTTPException(400, str(e)) from e


@router.delete(
    "/composition",
    tags=["管理台 · 数据列表"],
    summary="技能构成 · 移除一项技能",
)
def delete_skill_composition(
    node_id: str = Query(..., description="专业或岗位的节点 id"),
    skill_key: str = Query(..., description="要移除的逻辑技能名"),
    user: AuthUser = Depends(require_auth_user),
) -> dict[str, Any]:
    from backend.kg.pg_store.skill_composition import CompositionError, remove_skill

    try:
        return remove_skill(
            node_id, skill_key, user_id=user.user_id, user_name=user.user_name
        )
    except CompositionError as e:
        raise HTTPException(400, str(e)) from e


@router.post(
    "/composition/normalize",
    tags=["管理台 · 数据列表"],
    summary="技能构成 · 权重归一化（仅岗位）",
    description=(
        "把该岗位所有技能权重**等比缩放**到和为 1.00 —— 等比而非均分，"
        "保留运营已设定的相对重要性；末位吸收舍入误差以保证精确为 1.00。\n\n"
        "原权重全为空/0 时退化为均分（此时无从推断相对重要性）。\n"
        "**专业技能不带权重，调用会返回 400。**"
    ),
    response_description="归一化后的构成，附 normalized_from（归一前权重和）",
)
def normalize_skill_composition(
    node_id: str = Query(..., description="岗位节点 id"),
    user: AuthUser = Depends(require_auth_user),
) -> dict[str, Any]:
    from backend.kg.pg_store.skill_composition import CompositionError, normalize_weights

    try:
        return normalize_weights(
            node_id, user_id=user.user_id, user_name=user.user_name
        )
    except CompositionError as e:
        raise HTTPException(400, str(e)) from e


@router.get(
    "/skills/{skill_key:path}/prerequisites",
    tags=["管理台 · 技能多档"],
    summary="列出先修技能",
)
def admin_list_prereqs(
    skill_key: str,
    region: str = Query("CN"),
    user: AuthUser = Depends(require_auth_user),
) -> list[dict[str, Any]]:
    _ = user
    from backend.kg.pg_store.skill_prereq import list_prereqs

    return list_prereqs(skill_key, region=region)


@router.post(
    "/skills/{skill_key:path}/prerequisites",
    tags=["管理台 · 技能多档"],
    summary="添加先修（无环校验）",
)
def admin_add_prereq(
    skill_key: str,
    body: PrereqBody,
    user: AuthUser = Depends(require_auth_user),
) -> dict[str, Any]:
    from backend.kg.pg_store.skill_prereq import add_prereq

    try:
        return add_prereq(
            skill_key,
            body.prereq_skill_key,
            region=body.region,
            evidence=body.evidence,
            created_by=user.user_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.put(
    "/skills/{skill_key:path}/prerequisites",
    tags=["管理台 · 技能多档"],
    summary="整体替换先修列表（无环）",
)
def admin_set_prereqs(
    skill_key: str,
    body: PrereqSetBody,
    user: AuthUser = Depends(require_auth_user),
) -> list[dict[str, Any]]:
    from backend.kg.pg_store.skill_prereq import set_prereqs

    try:
        return set_prereqs(
            skill_key,
            body.prereq_skill_keys,
            region=body.region,
            created_by=user.user_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete(
    "/skills/{skill_key:path}/prerequisites/{prereq_key:path}",
    tags=["管理台 · 技能多档"],
    summary="删除一条先修",
)
def admin_del_prereq(
    skill_key: str,
    prereq_key: str,
    region: str = Query("CN"),
    user: AuthUser = Depends(require_auth_user),
) -> dict[str, Any]:
    _ = user
    from backend.kg.pg_store.skill_prereq import remove_prereq

    ok = remove_prereq(skill_key, prereq_key, region=region)
    if not ok:
        raise HTTPException(404, "prereq not found")
    return {"deleted": True, "skill_key": skill_key, "prereq_skill_key": prereq_key}
