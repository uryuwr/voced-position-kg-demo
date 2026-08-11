"""
独立可部署的知识图谱 HTTP API。

契约文档（局域网 IP 可访问，类 Java Swagger）:
  /docs          Swagger UI
  /redoc         ReDoc
  /openapi.json  OpenAPI 3
  /api-guide     对接说明 HTML

请求/响应字段说明见 backend.api.schemas（改模型即更新 /docs）。

鉴权：UC MAC Token（Authorization）；开发 AUTH_BYPASS=1 + X-Test-Uid。
审核：四维/边变更进待审，通过即生效，驳回删记录。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 尽早加载 .env
import backend.settings  # noqa: F401

from backend.api.auth import UCAuthMiddleware
from backend.api.auth_temp import USER_HEADER_NOTE, TempUser, require_temp_user
from backend.api.openapi_docs import (
    API_DESCRIPTION,
    API_GUIDE_HTML,
    API_TITLE,
    API_VERSION,
    OPENAPI_TAGS,
    apply_security_to_openapi,
    openapi_servers,
)
from backend.api.schemas import (
    ArchiveEdgeResponse,
    AuthTempInfo,
    DocsLinks,
    EdgeCreate,
    EdgeListResponse,
    ExpandRequest,
    GraphResponse,
    HealthResponse,
    IndustryTreeResponse,
    KgEdge,
    KgNode,
    KgNodeWithEdge,
    NodeCreate,
    NodeListResponse,
    NodePatch,
    OccupationRequiresRow,
    ProposalCreate,
    ProposalOut,
    ProposalReviewBody,
    ServiceDiscovery,
    StatsResponse,
)
from backend.api.routes_admin_biz import router as admin_biz_router
from backend.api.routes_student import router as student_router
from backend.kg.pg_store.biz_store import ensure_biz_schema
from backend.kg.pg_store.client import ensure_schema, verify_connectivity
from backend.kg.pg_store.config import DEFAULT_REGION
from backend.kg.pg_store.counts import attach_counts_by_type
from backend.kg.pg_store.query import (
    attach_link_ids,
    capability_by_major,
    expand_neighbors,
    explore_graph,
    get_node,
    graph_by_industry,
    graph_by_major,
    industry_occupations,
    industry_tree,
    list_edges,
    list_nodes,
    major_occupations,
    occupation_requires,
    occupation_skills,
    search_nodes,
    stats as kg_stats,
)
from backend.kg.pg_store.industry_graph import (
    industry_graph,
    occupation_skills_graph,
    search_industries,
)
from backend.kg.pg_store.skill_aggregate import (
    group_nodes_to_bundles,
    occupation_skill_bundles,
)
from backend.kg.pg_store.review import (
    create_proposal,
    ensure_review_schema,
    get_proposal,
    list_proposals,
    review_proposal,
)
from backend.kg.pg_store.write import (
    archive_edge,
    archive_node,
    create_edge,
    create_node,
    patch_node,
)

FRONTEND_DIR = ROOT / "frontend"
SEED_DIR = ROOT / "data" / "seeds" / "it_ai"
SCHEMAS_DIR = ROOT / "schemas"

_SERVE_DEV_UI = os.getenv("SERVE_DEV_UI", "0").strip().lower() in ("1", "true", "yes", "on")
_CORS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
_PORT = int(os.getenv("API_PORT", "8088"))

app = FastAPI(
    title=API_TITLE,
    description=API_DESCRIPTION
    + "\n\n**字段契约**：请求体/响应体均见 Schemas（`KgNode`、`NodeCreate`、`GraphResponse` 等），"
    "每个属性带 description；以 `/docs` → Schemas 与各接口 Responses 为准。",
    version=API_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    openapi_tags=OPENAPI_TAGS,  # 管理端 / 学生端 模块顺序见 openapi_docs.OPENAPI_TAGS
    swagger_ui_parameters={
        "persistAuthorization": True,
        "displayRequestDuration": True,
        "tryItOutEnabled": True,
        "filter": True,
        "defaultModelsExpandDepth": 2,
        "defaultModelExpandDepth": 2,
    },
)

# ⚠ 中间件顺序：Starlette 中「后 add = 更靠外」。
# CORS 必须在最外层（最后 add），把鉴权包在里面。否则：
#   1) CORS 预检 OPTIONS（按浏览器规范不带 Authorization）会被鉴权直接 401；
#   2) 鉴权返回的 401 不经过 CORS 中间件、响应缺 Access-Control-* 头，
#      浏览器只报「跨域」，看不到真实的鉴权失败原因。
app.add_middleware(UCAuthMiddleware)

_cors_kwargs: dict[str, Any] = {
    "allow_credentials": True,
    "allow_methods": ["*"],
    "allow_headers": ["*"],
    "expose_headers": ["*"],
    "max_age": 600,
}
if _CORS == ["*"]:
    # 用 allow_origin_regex 而非 allow_origins=["*"]：
    # 后者与 allow_credentials=True 组合会回 `ACAO: *`，浏览器按规范拒收带凭据的响应；
    # regex 匹配会回显具体 Origin，跨域携带 Authorization/Cookie 才能生效。
    _cors_kwargs["allow_origin_regex"] = ".*"
else:
    _cors_kwargs["allow_origins"] = _CORS
app.add_middleware(CORSMiddleware, **_cors_kwargs)

if SCHEMAS_DIR.exists():
    app.mount("/schemas", StaticFiles(directory=str(SCHEMAS_DIR)), name="schemas")


def custom_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=OPENAPI_TAGS,
    )
    schema = apply_security_to_openapi(schema)
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi  # type: ignore[method-assign]


@app.on_event("startup")
def _startup() -> None:
    try:
        ensure_schema()
        ensure_biz_schema()
        ensure_review_schema()
    except Exception:
        pass
    try:
        from backend.kg.pg_store.node_layout_meta import ensure_layout_meta_once

        ensure_layout_meta_once()
    except Exception:
        pass


# 学生端业务（frontend.html）+ 管理端看板/审核
app.include_router(student_router)
app.include_router(admin_biz_router)


@app.get(
    "/v1/config",
    tags=["系统"],
    summary="前端配置（UC SDK 等，无需登录）",
)
def frontend_config() -> dict[str, Any]:
    """对齐 bcs-ai-agent /api/v1/config；UC 项来自仓库根 .env。"""
    from backend import settings as app_settings

    cfg = app_settings.frontend_config()
    cfg["api_version"] = API_VERSION
    return cfg


@app.get(
    "/v1/me",
    tags=["系统"],
    summary="当前登录用户",
)
def current_me(user: TempUser = Depends(require_temp_user)) -> dict[str, str]:
    return {"user_id": user.user_id, "user_name": user.user_name}


@app.get("/", tags=["系统"], response_model=ServiceDiscovery, summary="服务发现")
def root() -> ServiceDiscovery:
    servers = openapi_servers(_PORT)
    return ServiceDiscovery(
        service="voced-kg-api",
        version=API_VERSION,
        store="postgresql",
        default_region=DEFAULT_REGION,
        docs=DocsLinks(
            swagger="/docs",
            redoc="/redoc",
            openapi_json="/openapi.json",
            guide="/api-guide",
            note="局域网用本机 IP 替换 host，例如 http://192.168.x.x:8088/docs",
            servers=servers,
        ),
        auth_temp=AuthTempInfo(
            mode="uc_mac_token",
            required_headers=["Authorization"],
            note=USER_HEADER_NOTE,
        ),
        health="/health",
        api_prefix="/v1",
        dev_ui_enabled=_SERVE_DEV_UI,
        dev_ui=(
            {
                "workbench": "/dev",
                "admin": "/admin",
                "admin_console": "/admin-console",
                "admin_graph": "/admin-cytoscape",
            }
            if _SERVE_DEV_UI
            else None
        ),
    )


@app.get("/health", tags=["系统"], summary="健康检查（含 AI 网关就绪态）")
def health() -> dict[str, Any]:
    pg = verify_connectivity()
    status = "ok" if pg.get("ok") else "degraded"
    from backend.agent.llm import gateway_info

    return {
        "status": status,
        "service": "voced-kg-api",
        "store": "postgresql",
        "docs": "/docs",
        "postgresql": pg,
        "ai_gateway": gateway_info(),
    }


@app.get("/api-guide", tags=["系统"], response_class=HTMLResponse, summary="对接说明 HTML")
def api_guide() -> HTMLResponse:
    return HTMLResponse(API_GUIDE_HTML)


def _mount_dev_ui() -> None:
    if not _SERVE_DEV_UI:
        return
    if SEED_DIR.exists():
        app.mount("/seed", StaticFiles(directory=str(SEED_DIR)), name="seed")

    @app.get("/dev", include_in_schema=False)
    def dev_index():
        index = FRONTEND_DIR / "index.html"
        if index.exists():
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="frontend/index.html missing")

    # 静态脚本（api-client 等）
    js_dir = FRONTEND_DIR / "js"
    if js_dir.exists():
        app.mount("/js", StaticFiles(directory=str(js_dir)), name="frontend_js")

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin-console", include_in_schema=False)
    def admin_console_page():
        """管理端完整落地：看板 / 四维列表 / 建数 / 审核。"""
        page = FRONTEND_DIR / "admin-console.html"
        if not page.exists():
            raise HTTPException(status_code=404, detail="admin-console.html missing")
        return FileResponse(page)

    @app.get("/admin-cytoscape", include_in_schema=False)
    def admin_graph_page():
        page = FRONTEND_DIR / "admin-cytoscape.html"
        if not page.exists():
            raise HTTPException(status_code=404, detail="admin-cytoscape.html missing")
        return FileResponse(page)

    @app.get("/student", include_in_schema=False)
    def student_page():
        """学员端 E2E 演示页：岗位探索 / 岗位详情 / AI 诊断 / 自适应学习路径。"""
        page = FRONTEND_DIR / "student.html"
        if not page.exists():
            raise HTTPException(status_code=404, detail="student.html missing")
        return FileResponse(page)

    @app.get("/kg", include_in_schema=False)
    @app.get("/kg-explorer", include_in_schema=False)
    def kg_explorer_page():
        """知识图谱主视图：选行业 → 行业/专业/岗位三层图（分层｜热力）→ 点岗位看技能图谱。"""
        page = FRONTEND_DIR / "kg-explorer.html"
        if not page.exists():
            raise HTTPException(status_code=404, detail="kg-explorer.html missing")
        return FileResponse(page)

    @app.get("/capability", include_in_schema=False)
    def capability_page():
        """能力全景演示页（专业→行业/岗位层级→技能折叠）。"""
        page = FRONTEND_DIR / "capability.html"
        if not page.exists():
            raise HTTPException(status_code=404, detail="capability.html missing")
        return FileResponse(page)

    @app.get("/cn-sources", include_in_schema=False)
    def cn_sources_page():
        page = FRONTEND_DIR / "cn-sources.html"
        if not page.exists():
            raise HTTPException(status_code=404, detail="cn-sources.html missing")
        return FileResponse(page)


_mount_dev_ui()


# ── 图谱·读 ───────────────────────────────────────────────────


@app.get(
    "/v1/stats",
    tags=["前台 · 图谱检索"],
    response_model=StatsResponse,
    summary="图规模统计",
    response_description="节点/边计数及分类型汇总（管理看板）",
)
def stats_api(user: TempUser = Depends(require_temp_user)) -> StatsResponse:
    try:
        s = kg_stats()
        s["operator"] = {"user_id": user.user_id, "user_name": user.user_name}
        return StatsResponse.model_validate(s)
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.get(
    "/v1/search",
    tags=["前台 · 图谱检索"],
    response_model=list[KgNode],
    summary="search · 节点名称搜索（仅种子，不扩邻域）",
    description=(
        "按 **name** 模糊搜索节点，返回 `KgNode[]`，**不展开邻居**。\n\n"
        "常规图探索：`search` 定种子 → `POST /v1/graph/expand` 按需 1 跳展开。\n\n"
        "- 排序：industry → major → occupation → skill → course → credential\n"
        "- 可 `type` 过滤维度"
    ),
    response_description="匹配节点列表（KgNode）",
    operation_id="search_nodes",
)
def search(
    q: str = Query(..., min_length=1, description="名称关键字（对 name 模糊匹配）"),
    region: str | None = Query(
        None, description="区域过滤：CN|EU|US；默认服务配置 CN；传 all 不限"
    ),
    type: str | None = Query(
        None, description="可选类型：industry|major|occupation|skill_level|course|credential"
    ),
    limit: int = Query(20, ge=1, le=100, description="最多返回条数"),
    user: TempUser = Depends(require_temp_user),
) -> list[KgNode]:
    _ = user
    return [
        KgNode.model_validate(n)
        for n in search_nodes(q, limit=limit, region=region, node_type=type)
    ]


def _is_manage_scope(scope: str | None) -> bool:
    return (scope or "").strip().lower() in ("manage", "admin", "all")


def _node_detail_core(
    node_id: str,
    *,
    include_counts: bool = False,
    include_links: bool = False,
    scope: str | None = None,
) -> dict:
    n = get_node(node_id)
    if not n:
        raise HTTPException(status_code=404, detail="node not found")
    # BR-07：前台/图仅 published；管理端 scope=manage 可读 draft/disabled
    if not _is_manage_scope(scope):
        if (n.get("status") or "published") != "published":
            raise HTTPException(status_code=404, detail="node not found")
    if include_counts:
        attach_counts_by_type([n], node_type=n.get("type"))
    if include_links or _is_manage_scope(scope):
        attach_link_ids(n)
    return n


@app.get(
    "/v1/nodes/{node_id:path}",
    tags=["前台 · 图谱检索"],
    response_model=KgNode,
    summary="节点详情",
    description=(
        "按全局 id 取单节点档案。id 含冒号时建议用下方 query 接口。"
        "默认仅 published（BR-07）；`scope=manage` 可读草稿/停用（管理端编辑）。"
    ),
    response_description="完整节点对象，含 attrs / display_name",
)
def node_detail(
    node_id: str,
    include_counts: bool = Query(False, description="附加关联 counts（联读）"),
    include_links: bool = Query(
        False, description="附加 industry_ids/major_ids/occupation_ids（编辑用）"
    ),
    scope: str | None = Query(
        None, description="scope=manage 时允许 draft/disabled，并默认带 link_ids"
    ),
    user: TempUser = Depends(require_temp_user),
) -> KgNode:
    _ = user
    n = _node_detail_core(
        node_id,
        include_counts=include_counts,
        include_links=include_links,
        scope=scope,
    )
    return KgNode.model_validate(n)


@app.get(
    "/v1/node",
    tags=["前台 · 图谱检索"],
    response_model=KgNode,
    summary="节点详情（query id，推荐）",
    description=(
        "`GET /v1/node?id=CN:major:…`，避免 path 中冒号被编码导致 UC MAC 校验失败。"
        "默认仅 published；管理端编辑请加 `scope=manage`。"
    ),
)
def node_detail_query(
    id: str = Query(..., description="节点全局 id"),
    include_counts: bool = Query(False, description="附加关联 counts（联读）"),
    include_links: bool = Query(
        False, description="附加 industry_ids/major_ids/occupation_ids"
    ),
    scope: str | None = Query(
        None, description="scope=manage 时允许 draft/disabled，并默认带 link_ids"
    ),
    user: TempUser = Depends(require_temp_user),
) -> KgNode:
    _ = user
    n = _node_detail_core(
        id,
        include_counts=include_counts,
        include_links=include_links,
        scope=scope,
    )
    return KgNode.model_validate(n)


@app.get(
    "/v1/majors/occupations",
    tags=["前台 · 图谱检索"],
    response_model=list[KgNodeWithEdge],
    summary="专业 → 岗位列表",
    description="关系 prepares_for；可传 major_id 或专业名关键字 q。",
    response_description="岗位节点列表，每项带 edge 摘要",
)
def api_major_occupations(
    major_id: str | None = Query(None, description="专业节点全局 id"),
    q: str | None = Query(None, description="专业名称关键字（无 major_id 时必填其一）"),
    region: str | None = Query(None, description="区域，默认 CN"),
    limit: int = Query(50, ge=1, le=200, description="最多返回条数"),
    user: TempUser = Depends(require_temp_user),
) -> list[KgNodeWithEdge]:
    _ = user
    if not major_id and not q:
        raise HTTPException(status_code=400, detail="major_id or q required")
    rows = major_occupations(major_id, q=q, region=region, limit=limit)
    return [KgNodeWithEdge.model_validate(r) for r in rows]


@app.get(
    "/v1/occupations/skills",
    tags=["前台 · 图谱检索"],
    summary="岗位 → 技能列表",
    description=(
        "关系 requires。默认 `aggregate=true`：按 skill_key 聚合为逻辑技能 "
        "（含 levels / required_level / level_descriptions）；"
        "`aggregate=false` 返回 skill_level 扁平行（KgNodeWithEdge）。"
    ),
    response_description="aggregate 时为 bundle 对象数组；否则 KgNodeWithEdge[]",
)
def api_occupation_skills(
    occupation_id: str | None = Query(None, description="岗位节点全局 id"),
    q: str | None = Query(None, description="岗位名称关键字"),
    region: str | None = Query(None, description="区域，默认 CN"),
    limit: int = Query(50, ge=1, le=200, description="最多返回条数"),
    aggregate: bool = Query(
        True,
        description="True=按 skill_key 聚合逻辑技能；False=skill_level 扁平行",
    ),
    user: TempUser = Depends(require_temp_user),
) -> list[Any]:
    _ = user
    if not occupation_id and not q:
        raise HTTPException(status_code=400, detail="occupation_id or q required")
    if aggregate and occupation_id:
        return occupation_skill_bundles(occupation_id, limit=limit)
    rows = occupation_skills(occupation_id, q=q, region=region, limit=limit)
    if aggregate:
        return group_nodes_to_bundles(rows, region=region)
    return [KgNodeWithEdge.model_validate(r) for r in rows]


@app.get(
    "/v1/industries/tree",
    tags=["前台 · 图谱检索"],
    response_model=IndustryTreeResponse,
    summary="行业树",
    description="industry 节点 + parent_of 边（父→子）。",
)
def api_industry_tree(
    region: str | None = Query(None, description="区域，默认 CN"),
    limit: int = Query(500, ge=1, le=2000, description="行业节点上限"),
    user: TempUser = Depends(require_temp_user),
) -> IndustryTreeResponse:
    _ = user
    return IndustryTreeResponse.model_validate(industry_tree(region=region, limit=limit))


@app.get(
    "/v1/industries/{industry_id:path}/occupations",
    tags=["前台 · 图谱检索"],
    response_model=list[KgNodeWithEdge],
    summary="行业下岗位",
    description="occupation -belongs_to→ industry。",
)
def api_industry_occupations(
    industry_id: str,
    limit: int = Query(100, ge=1, le=500, description="最多返回条数"),
    user: TempUser = Depends(require_temp_user),
) -> list[KgNodeWithEdge]:
    _ = user
    rows = industry_occupations(industry_id, limit=limit)
    return [KgNodeWithEdge.model_validate(r) for r in rows]


@app.get(
    "/v1/graph/by-industry",
    tags=["前台 · 图谱检索"],
    response_model=GraphResponse,
    summary="行业闭包子图",
    description=(
        "以行业为根的**闭包子图**（非无向 BFS）：\n"
        "行业 ←belongs_to— 专业 —prepares_for→ 岗位 —requires→ 技能；"
        "可选岗位直连 belongs_to 行业。\n"
        "只含闭包内节点与上述边，不串其它行业。\n\n"
        "参数：`industry_id` 或 `q`（行业名）二选一；"
        "`max_nodes` 默认 500、最大 2000（优先保留 行业→专业→岗位→技能）。"
    ),
    response_description="root=行业 + nodes/edges + meta.full_counts / truncated",
    operation_id="graph_by_industry",
)
def by_industry(
    industry_id: str | None = Query(
        None, description="行业全局 id，如 CN:industry:BOSS_ZHIPIN:boss:100015"
    ),
    q: str | None = Query(None, description="行业名称关键字（无 id 时用，取最佳一条）"),
    region: str | None = Query(None, description="区域，默认 CN"),
    max_nodes: int = Query(
        500, ge=10, le=2000, description="闭包节点上限（优先填满主路径上层）"
    ),
    include_skills: bool = Query(True, description="是否纳入 requires 技能节点"),
    include_direct_occupations: bool = Query(
        True, description="是否纳入直接 belongs_to 本行业的岗位"
    ),
    user: TempUser = Depends(require_temp_user),
) -> GraphResponse:
    _ = user
    if not (industry_id or "").strip() and not (q or "").strip():
        raise HTTPException(
            status_code=400, detail="industry_id or q required"
        )
    data = graph_by_industry(
        industry_id,
        q=q,
        region=region,
        max_nodes=max_nodes,
        include_skills=include_skills,
        include_direct_occupations=include_direct_occupations,
    )
    if data.get("meta", {}).get("matched", 0) == 0:
        raise HTTPException(status_code=404, detail=data.get("meta", {}))
    return GraphResponse.model_validate(data)


@app.get(
    "/v1/graph/by-major",
    tags=["前台 · 图谱检索"],
    response_model=GraphResponse,
    summary="按专业展开子图",
    response_description="root/roots + nodes + edges + meta（管理 Graph / 旧工作台）",
)
def by_major(
    name: str = Query(..., min_length=1, description="专业名称关键字，如 人工智能"),
    region: str | None = Query(None, description="区域，默认 CN"),
    depth: int = Query(3, ge=1, le=5, description="展开跳数（1–5）"),
    max_nodes: int = Query(300, ge=10, le=2000, description="节点数上限（最大 2000）"),
    confidence: str | None = Query(
        None, description="边置信度过滤，逗号分隔，如 official,derived"
    ),
    user: TempUser = Depends(require_temp_user),
) -> GraphResponse:
    _ = user
    conf_list = [c.strip() for c in confidence.split(",")] if confidence else None
    data = graph_by_major(
        name, region=region, depth=depth, max_nodes=max_nodes, confidence=conf_list
    )
    if data.get("meta", {}).get("matched", 0) == 0:
        raise HTTPException(status_code=404, detail=data.get("meta", {}))
    return GraphResponse.model_validate(data)


@app.get(
    "/v1/occupation/requires",
    tags=["前台 · 图谱检索"],
    response_model=list[OccupationRequiresRow],
    summary="岗位技能（名称匹配，扁平行）",
)
def requires(
    q: str = Query(..., min_length=1, description="岗位名称关键字"),
    region: str | None = Query(None, description="区域，默认 CN"),
    limit: int = Query(30, ge=1, le=200, description="最多返回条数"),
    user: TempUser = Depends(require_temp_user),
) -> list[OccupationRequiresRow]:
    _ = user
    rows = occupation_requires(q, limit=limit, region=region)
    return [OccupationRequiresRow.model_validate(r) for r in rows]


@app.post(
    "/v1/graph/expand",
    tags=["前台 · 图谱检索"],
    response_model=GraphResponse,
    summary="expand · 节点 1 跳邻居（常规图探索）",
    description=(
        "对齐 AWS Graph Explorer / Neo4j Browser 的 expand 语义：\n"
        "给定 `node_id`，返回 **1 跳** 邻居与边（默认 limit=25，可再点再扩）。\n\n"
        "请求体见 Schema `ExpandRequest`。\n"
        "前端应合并到当前画布（去重），而不是每次 depth=2 全邻域重扫。"
    ),
    operation_id="expand_neighbors",
)
def expand(
    body: ExpandRequest,
    user: TempUser = Depends(require_temp_user),
) -> GraphResponse:
    _ = user
    data = expand_neighbors(
        body.node_id,
        limit=body.limit,
        rel_types=body.rel_types,
        direction=body.direction,
        region=body.region,
    )
    if data.get("meta", {}).get("matched", 0) == 0:
        raise HTTPException(status_code=404, detail=data.get("meta", {}))
    return GraphResponse.model_validate(data)


@app.get(
    "/v1/graph/explore",
    tags=["前台 · 图谱检索"],
    response_model=GraphResponse,
    summary="search 种子列表（默认不扩边）/ 可选子图",
    description=(
        "**常规用法（推荐）**：`depth=0`（默认）只返回关键字匹配的种子节点，"
        "等同 search；邻居请用 `POST /v1/graph/expand`。\n\n"
        "**兼容/显式子图**：`depth≥1` 仍支持服务端 BFS（旧行为），"
        "易一次拉入大量叶子课，管理端 Graph 已改为 search+expand。\n\n"
        "参考：aws/graph-explorer、neo4j-labs create-context-graph `POST /expand`。"
    ),
    response_description="roots/nodes/edges/paths/meta",
)
def explore(
    q: str = Query(
        "",
        description="名称关键字；空 / * / 全部 = 按类型全量列表（默认 type=major）",
    ),
    type: str | None = Query(
        None,
        description="节点类型过滤：major|occupation|skill_level|course|credential|industry",
    ),
    region: str | None = Query(None, description="区域，默认 CN；all=不限"),
    depth: int = Query(
        0,
        ge=0,
        le=5,
        description="0=仅种子；≥1 服务端 BFS 扩邻域（最大 5，防一次扫太大）",
    ),
    max_nodes: int = Query(
        120,
        ge=10,
        le=2000,
        description="子图节点上限（最大 2000；联调可手填，建议 ≤500）",
    ),
    user: TempUser = Depends(require_temp_user),
) -> GraphResponse:
    _ = user
    data = explore_graph(
        q if q is not None else "",
        node_type=type,
        region=region,
        depth=depth,
        max_nodes=max_nodes,
    )
    if data.get("meta", {}).get("matched", 0) == 0:
        raise HTTPException(status_code=404, detail=data.get("meta", {}))
    return GraphResponse.model_validate(data)


@app.get(
    "/v1/capability",
    tags=["前台 · 图谱检索"],
    summary="能力全景（专业 → 行业/岗位(层级) → 技能，渐进式）",
    description=(
        "面向「能力体系结构化全景」的聚合读接口，一次返回（仅 published）：\n\n"
        "| 层 | 关系 | 内容 |\n"
        "| --- | --- | --- |\n"
        "| 归属（上） | major -belongs_to-> industry | 该专业所属行业 |\n"
        "| 岗位（中） | major -prepares_for-> occupation | 对口岗位，含 `level` 岗位层级(1..N)、`skill_count` |\n"
        "| 技能（下） | occupation -requires-> skill_level | 每岗位技能明细，**默认不返回**，见 `include_skills` |\n"
        "| 晋升链 | occupation -advances_to-> occupation | `progressions[]`，同岗位族内按 level 递进派生 |\n"
        "| 共享技能 | — | `shared_skills[]`，被多个岗位共同要求，供侧栏展示（不画线） |\n\n"
        "**渐进式展示约定**：默认视图只回答「这个专业有哪些岗位、怎么晋升」，技能是二级信息。\n"
        "因此 `include_skills` 默认为 `false`——每个岗位只给 `skill_count` 角标，"
        "用户 hover 单个岗位时再调 `GET /v1/occupations/skills?occupation_id=...` 按需拉取，\n"
        "避免一次把上百条技能边全画出来导致图不可读。\n\n"
        "参数 `major` 与 `major_id` 二选一（`major_id` 优先）。`region` 枚举：`CN`|`EU`|`US`|`all`。\n\n"
        "> 注意：`progressions` 依赖 `occupation.level`。当前库内岗位主要来自「职业分类大典」，"
        "该源不含职级维度，故多数专业返回空数组；待企业职级/招聘级别数据接入后自动生效。"
    ),
    response_description=(
        "{ root(专业), industries[], occupations[含 level/skill_count/skills[]], "
        "progressions[], shared_skills[], meta }"
    ),
    operation_id="capability_by_major",
    responses={
        200: {
            "description": "能力全景三层聚合",
            "content": {
                "application/json": {
                    "example": {
                        "root": {"id": "CN:major:MOE_CN:510205", "type": "major", "name": "大数据技术", "display_name": "高专 · 大数据技术 · 510205"},
                        "industries": [{"id": "CN:industry:BOSS:100", "type": "industry", "name": "信息技术"}],
                        "occupations": [
                            {
                                "id": "CN:occupation:MOHRSS:4-04-05-02", "type": "occupation",
                                "name": "大数据工程技术人员", "level": 2,
                                "skill_count": 19,
                                "skills": [
                                    {"id": "CN:skill_level:...:Hadoop|L3", "type": "skill_level", "name": "Hadoop · 高级", "attrs": {"level_code": "L3", "level_zh": "高级"}}
                                ]
                            }
                        ],
                        "progressions": [
                            {
                                "from": "CN:occupation:MOHRSS:4-04-05-02", "to": "CN:occupation:MOHRSS:4-04-05-03",
                                "from_name": "大数据工程技术人员", "to_name": "大数据分析师",
                                "from_level": 2, "to_level": 3,
                                "rel_type": "advances_to", "confidence": "derived",
                            }
                        ],
                        "shared_skills": [
                            {"skill_key": "培训指导", "occ_count": 7, "levels": ["L2", "L3"], "occupation_ids": ["CN:occupation:...", "CN:occupation:..."]}
                        ],
                        "meta": {
                            "matched": 1, "occupation_count": 20, "skill_total": 100,
                            "skills_included": False, "progression_count": 1,
                            "shared_skill_count": 10, "region": "CN",
                        },
                    }
                }
            },
        }
    },
)
def capability(
    major: str | None = Query(
        None, description="专业名称关键字（与 major_id 二选一）", examples=["软件技术"]
    ),
    major_id: str | None = Query(
        None, description="专业节点全局 id（优先于 major）", examples=["CN:major:MOE_CN:510201"]
    ),
    region: str | None = Query(
        None, description="区域枚举：CN | EU | US | all(不限)；默认 CN", examples=["CN"]
    ),
    limit_occupations: int = Query(200, ge=1, le=1000, description="岗位数上限"),
    limit_skills_per_occ: int = Query(
        80, ge=1, le=500, description="每个岗位返回技能数上限（仅 include_skills=true 时生效）"
    ),
    include_skills: bool = Query(
        False,
        description=(
            "是否下发每个岗位的技能明细。默认 false（渐进式折叠）："
            "`occupations[].skills` 为空数组，只给 `skill_count`；"
            "true 时恢复全量返回，适合导出/离线分析等一次取全的场景"
        ),
    ),
    include_progression: bool = Query(
        True, description="是否返回岗位晋升链 progressions[]（advances_to，派生）"
    ),
    shared_skill_min_occ: int = Query(
        2,
        ge=0,
        le=50,
        description=(
            "共享技能阈值：被 >= N 个岗位共同要求的技能收进 shared_skills[]（按 occ_count 倒序）；"
            "0 = 不计算（可省一次技能明细扫描）"
        ),
    ),
    user: TempUser = Depends(require_temp_user),
) -> dict[str, Any]:
    _ = user
    if not major and not major_id:
        raise HTTPException(status_code=400, detail="major or major_id required")
    data = capability_by_major(
        major=major,
        major_id=major_id,
        region=region,
        limit_occupations=limit_occupations,
        limit_skills_per_occ=limit_skills_per_occ,
        include_skills=include_skills,
        include_progression=include_progression,
        shared_skill_min_occ=shared_skill_min_occ,
    )
    if data.get("meta", {}).get("matched", 0) == 0:
        raise HTTPException(status_code=404, detail="major not found")
    return data


@app.get(
    "/v1/kg/node-detail",
    tags=["管理台 · 数据列表"],
    summary="四维详情（行业 / 专业 / 岗位 / 技能）",
    description=(
        "管理台详情面板一站式取数，按节点 `type` 返回对应结构。"
        "字段对齐 `backend.html` 管理端原型，逐项依据见 "
        "`docs/管理台详情接口-原型对照.md`。\n\n"
        "| type | 返回段 |\n"
        "| --- | --- |\n"
        "| `industry` | `majors[]`（含各专业的岗位数）、`occupations[]`（直连岗位）、`counts` |\n"
        "| `major` | `industries[]`、`occupations[]`（含 level / skill_count / weight_sum）、"
        "`aggregated_skills[]`（**按被引用岗位数倒序**，含 required_level 与 used_by[]） |\n"
        "| `occupation` | `industries[]`、`majors[]`、`skills[]`（含 required_level / weight_pct / "
        "prereqs 先修 / levels 五档格）、`weight_sum` |\n"
        "| `skill_level` | `levels[]`、`level_completeness`、`occupations[]`（引用它的岗位）、"
        "`prereqs[]` 先修、`unlocks[]` 后继 |\n\n"
        "**可见状态**：published + draft + disabled；archived 不返回。\n\n"
        "> 聚合技能的 `used_by` 按岗位去重——同一技能在一个岗位下有 L1–L5 多个节点，"
        "只保留该岗位的最高要求档，避免同名岗位重复出现。"
    ),
    response_description="{ node, …（按类型的段）, counts, meta }",
    operation_id="kg_node_detail",
)
def api_kg_node_detail(
    id: str = Query(..., description="节点全局 id（含冒号，故用 query 传）"),
    user: TempUser = Depends(require_temp_user),
) -> dict[str, Any]:
    _ = user
    from backend.kg.pg_store.node_detail import node_detail

    data = node_detail(id)
    if not data.get("node"):
        raise HTTPException(status_code=404, detail="node not found")
    return data


@app.get(
    "/v1/industries/search",
    tags=["前台 · 图谱检索"],
    summary="行业模糊搜索（平铺，供选择行业的下拉框）",
    description=(
        "行业**平铺**返回，不区分大类/子行业——交互上用户直接搜一个行业选中即可。\n\n"
        "每项附 `major_count` / `occupation_count`，便于下拉里提示规模、避免用户选到空行业。\n"
        "排序：名称前缀命中优先 → 专业数倒序 → 名称。`q` 为空时按专业数倒序列出。"
    ),
    response_description="[{ id, name, region, major_count, occupation_count }]",
)
def api_industries_search(
    q: str | None = Query(None, description="行业名关键字，模糊匹配；空=按规模列出"),
    region: str | None = Query(None, description="区域：CN | EU | US | all；默认 CN"),
    limit: int = Query(30, ge=1, le=200, description="返回条数上限"),
    user: TempUser = Depends(require_temp_user),
) -> list[dict[str, Any]]:
    _ = user
    return search_industries(q=q, region=region, limit=limit)


@app.get(
    "/v1/industry-graph",
    tags=["前台 · 图谱检索"],
    summary="行业关联图（行业 → 专业 → 岗位，只到岗位层）",
    description=(
        "面向「选定一个行业 → 看三层关联图」的主视图接口。**只画到岗位层**，"
        "技能是二级信息，点岗位后另调 `GET /v1/occupation-skills-graph`。\n\n"
        "与通用 `explore/expand` 的区别：按层组织（`layers.majors` / `layers.occupations`），"
        "前端不必自己判层；且由服务端按层截断并在 `meta.truncated` 告知，"
        "避免超大行业撑爆画布（专业数中位数 12，但最大 292）。\n\n"
        "`layout=matrix` 时额外返回 `matrix`（热力图：行=专业、列=岗位）。\n"
        "强度 `metric=skill_affinity`：该岗位的技能与**同专业其他对口岗位**的重合总量，"
        "即「这个岗位在多大程度上代表该专业的主流技能」。\n"
        "（未采用「专业与岗位的共有技能数」，因为 `covers`（专业→技能）边当前为 0 条。）\n\n"
        "`progressions` 为岗位晋升链，只含两端都在当前画布内的 `advances_to` 真边。"
    ),
    response_description="{ industry, layers{majors,occupations}, links[], progressions[], matrix?, meta }",
    operation_id="industry_graph",
)
def api_industry_graph(
    industry_id: str | None = Query(None, description="行业节点全局 id（优先于 industry）"),
    industry: str | None = Query(None, description="行业名关键字（与 industry_id 二选一）"),
    region: str | None = Query(None, description="区域：CN | EU | US | all；默认 CN"),
    limit_majors: int = Query(20, ge=1, le=300, description="专业层上限，按对口岗位数倒序截断"),
    limit_occupations_per_major: int = Query(
        8, ge=1, le=50, description="每个专业取几个岗位，按技能数倒序"
    ),
    layout: str = Query(
        "layered",
        description="展示形态：layered=垂直分层图（默认）；matrix=热力图（附加 matrix 字段）",
        pattern="^(layered|matrix)$",
    ),
    user: TempUser = Depends(require_temp_user),
) -> dict[str, Any]:
    _ = user
    if not industry and not industry_id:
        raise HTTPException(status_code=400, detail="industry or industry_id required")
    data = industry_graph(
        industry_id=industry_id,
        industry=industry,
        region=region,
        limit_majors=limit_majors,
        limit_occupations_per_major=limit_occupations_per_major,
        layout=layout,
    )
    if data.get("meta", {}).get("matched", 0) == 0:
        raise HTTPException(status_code=404, detail="industry not found")
    return data


@app.get(
    "/v1/occupation-skills-graph",
    tags=["前台 · 图谱检索"],
    summary="岗位技能图谱（按分类分区 + 前置关系）",
    description=(
        "点击岗位后的二级视图：技能按 `category` 分区，区内给出前置关系箭头。\n\n"
        "`categories[].key` 取自 `kg_node.category`，按国家职业技能标准的「职业功能」维度"
        "划分（安全与环保 / 作业准备 / 操作与加工 / 设备维护与检修 / 质量与检验 / "
        "数据与信息 / 服务与业务 / 技术管理与创新 / 运营与管理 / 培训与指导）；"
        "未分类技能归入 `未分类` 分区并排在最后。\n\n"
        "`prereqs` 来自 `kg_skill_prereq`，**只含两端都在本岗位技能集内的边**，"
        "避免画出指向岗位外技能的孤立箭头。方向为 `from`(先修) → `to`(后继)。"
    ),
    response_description="{ occupation, categories[{key,skills[]}], prereqs[{from,to}], meta }",
    operation_id="occupation_skills_graph",
)
def api_occupation_skills_graph(
    occupation_id: str = Query(..., description="岗位节点全局 id"),
    region: str | None = Query(None, description="区域：CN | EU | US；默认 CN"),
    limit: int = Query(200, ge=1, le=500, description="技能条数上限"),
    user: TempUser = Depends(require_temp_user),
) -> dict[str, Any]:
    _ = user
    data = occupation_skills_graph(occupation_id, region=region, limit=limit)
    if data.get("meta", {}).get("matched", 0) == 0:
        raise HTTPException(status_code=404, detail="occupation not found")
    return data


# ── 图谱·写 / 管理列表 ───────────────────────────────────────


@app.get(
    "/v1/kg/nodes",
    tags=["管理台 · 数据列表"],
    response_model=NodeListResponse,
    summary="四维管理列表 · 分页列出节点",
    description=(
        "【对齐参考后台 backend.html / 本仓 Admin Table】\n\n"
        "按**维度 type** 分页列出节点，供管理表格使用（非图探索）。\n\n"
        "| 四维 | type 参数 |\n"
        "| --- | --- |\n"
        "| 行业 | `industry` |\n"
        "| 专业 | `major` |\n"
        "| 岗位 | `occupation` |\n"
        "| 技能 | `skill_level` |\n\n"
        "示例：\n"
        "- 专业第 1 页：`GET /v1/kg/nodes?type=major&page=1&page_size=20`\n"
        "- 岗位搜「软件」：`GET /v1/kg/nodes?type=occupation&q=软件&page=1`\n"
        "- 行业：`type=industry`；技能：`type=skill_level`\n\n"
        "返回 `items` + `total` + `page` + `page_size` + `total_pages`，前端可直接做分页器。\n\n"
        "说明：旧 Admin 曾用 `GET /v1/graph/explore?q=*&type=major&depth=0` 凑列表且**无服务端分页**"
        "（最多 500 条客户端翻页）；**新对接请用本接口**。"
    ),
    response_description="分页列表 NodeListResponse",
    operation_id="list_kg_nodes",
)
def api_list_nodes(
    type: str | None = Query(
        None,
        description=(
            "维度/节点类型（管理 Table 必传其一）。"
            "四维：industry|major|occupation|skill_level；"
            "也可 course|credential。不传则混列所有类型（一般不推荐）"
        ),
        examples=["major"],
    ),
    region: str | None = Query(
        None, description="区域，默认 CN；all=不限", examples=["CN"]
    ),
    q: str | None = Query(
        None,
        description="名称关键字模糊搜索（可选）；不传或空=该维度全量分页",
    ),
    status: str | None = Query(
        None,
        description="按状态过滤：published|disabled|…；默认仅 published",
    ),
    scope: str | None = Query(
        None,
        description=(
            "默认仅已发布（图谱 Table/探索/学员同源）。"
            "scope=manage 时管理控制台可看全状态（含停用）"
        ),
        examples=["manage"],
    ),
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(20, ge=1, le=200, description="每页条数，默认 20"),
    include_counts: bool = Query(
        False,
        description="True 时联读附加 counts（行业/专业/岗位）；岗位另带 industries",
    ),
    order_by: str | None = Query(
        None,
        description=(
            "排序：`created_desc` 按创建时间倒序（**新建的排最前**，scope=manage 时的默认）"
            " | `sort_order` 人工序（前台/图谱默认）| `name` 按名称。"
            "注意：新建节点 sort_order 为空，用人工序会被排到最后一页"
        ),
        examples=["created_desc"],
    ),
    user: TempUser = Depends(require_temp_user),
) -> NodeListResponse:
    _ = user
    data = list_nodes(
        node_type=type,
        region=region,
        q=q,
        status=status,
        order_by=order_by,
        page=page,
        page_size=page_size,
        scope=scope,
    )
    if include_counts and data.get("items"):
        attach_counts_by_type(data["items"], node_type=type)
    return NodeListResponse.model_validate(data)


@app.post(
    "/v1/kg/nodes",
    tags=["管理台 · 数据维护"],
    response_model=KgNode,
    summary="新建节点",
    description="请求体见 Schema `NodeCreate`。默认 status=draft。" + USER_HEADER_NOTE,
    response_description="写入后的完整节点",
)
def api_create_node(
    body: NodeCreate,
    user: TempUser = Depends(require_temp_user),
) -> KgNode:
    from backend.kg.pg_store.write import CodeConflictError

    try:
        n = create_node(
            body.model_dump(exclude_none=True),
            user_id=user.user_id,
            user_name=user.user_name,
        )
        return KgNode.model_validate(n)
    except CodeConflictError as e:
        # 编码冲突用 409 而非 400：前端可据此定位到「编码」字段并展示占用者
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
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.patch(
    "/v1/kg/nodes/{node_id:path}",
    tags=["管理台 · 数据维护"],
    response_model=KgNode,
    summary="编辑节点",
    description="请求体见 Schema `NodePatch`；只传需要改的字段。",
)
def api_patch_node(
    node_id: str,
    body: NodePatch,
    user: TempUser = Depends(require_temp_user),
) -> KgNode:
    from backend.kg.pg_store.write import CodeConflictError

    try:
        n = patch_node(
            node_id,
            body.model_dump(exclude_none=True),
            user_id=user.user_id,
            user_name=user.user_name,
        )
    except CodeConflictError as e:
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
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not n:
        raise HTTPException(status_code=404, detail="node not found")
    return KgNode.model_validate(n)


@app.delete(
    "/v1/kg/nodes/{node_id:path}",
    tags=["管理台 · 数据维护"],
    response_model=KgNode,
    summary="归档节点（软删）",
    description="将 status 置为 archived，不物理删除。",
)
def api_archive_node(
    node_id: str,
    user: TempUser = Depends(require_temp_user),
) -> KgNode:
    n = archive_node(node_id, user_id=user.user_id, user_name=user.user_name)
    if not n:
        raise HTTPException(status_code=404, detail="node not found")
    return KgNode.model_validate(n)


@app.get(
    "/v1/kg/edges",
    tags=["管理台 · 数据列表"],
    response_model=EdgeListResponse,
    summary="边列表（分页）",
    description=(
        "管理端边管理：按关系类型 / 端点节点 id / 端点名称检索。"
        "删节点后可按 `node_id` 查询，确认关联边是否已清空（total=0）。"
    ),
)
def api_list_edges(
    rel_type: str | None = Query(
        None,
        description="关系类型：prepares_for|requires|belongs_to|parent_of|related_to|…",
    ),
    node_id: str | None = Query(
        None, description="匹配 src_id 或 dst_id（核对某节点关联边）"
    ),
    src_id: str | None = Query(None, description="仅起点 id"),
    dst_id: str | None = Query(None, description="仅终点 id"),
    q: str | None = Query(None, description="端点名称或边 id 关键字"),
    status: str | None = Query(
        None,
        description=(
            "状态过滤：published（默认只返回这个）| draft | archived | disabled。"
            "归档边只留在库里、默认不返回；要核对或恢复时显式传 archived"
        ),
    ),
    scope: str | None = Query(
        None, description="manage=不限状态（管理端核对用）；缺省仅 published"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    user: TempUser = Depends(require_temp_user),
) -> EdgeListResponse:
    _ = user
    return EdgeListResponse.model_validate(
        list_edges(
            rel_type=rel_type,
            node_id=node_id,
            src_id=src_id,
            dst_id=dst_id,
            q=q,
            status=status,
            scope=scope,
            page=page,
            page_size=page_size,
        )
    )


@app.post(
    "/v1/kg/edges",
    tags=["管理台 · 数据维护"],
    response_model=KgEdge,
    summary="新建边",
    description="请求体见 Schema `EdgeCreate`。默认 status=draft；src_id/dst_id 须已存在。",
)
def api_create_edge(
    body: EdgeCreate,
    user: TempUser = Depends(require_temp_user),
) -> KgEdge:
    try:
        e = create_edge(
            body.model_dump(exclude_none=True),
            user_id=user.user_id,
            user_name=user.user_name,
        )
        return KgEdge.model_validate(e)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.delete(
    "/v1/kg/edges/{edge_id:path}",
    tags=["管理台 · 数据维护"],
    response_model=ArchiveEdgeResponse,
    summary="归档边（软删）",
)
def api_archive_edge(
    edge_id: str,
    user: TempUser = Depends(require_temp_user),
) -> ArchiveEdgeResponse:
    ok = archive_edge(edge_id, user_id=user.user_id, user_name=user.user_name)
    if not ok:
        raise HTTPException(status_code=404, detail="edge not found")
    return ArchiveEdgeResponse(id=edge_id, status="archived")


# ── 审核 ─────────────────────────────────────────────────────


@app.get(
    "/v1/review/proposals",
    tags=["管理台 · 审核发布"],
    response_model=list[ProposalOut],
    summary="提案列表",
    description="默认 status=pending。",
)
def api_list_proposals(
    status: str | None = Query(
        "pending",
        description="过滤状态：pending|approved|rejected；空/all 表示全部",
    ),
    limit: int = Query(50, ge=1, le=200, description="最多返回条数"),
    user: TempUser = Depends(require_temp_user),
) -> list[ProposalOut]:
    _ = user
    st = status if status not in ("", "all", "*") else None
    return [ProposalOut.model_validate(p) for p in list_proposals(status=st, limit=limit)]


@app.get(
    "/v1/review/proposals/{proposal_id}",
    tags=["管理台 · 审核发布"],
    response_model=ProposalOut,
    summary="提案详情",
)
def api_get_proposal(
    proposal_id: int,
    user: TempUser = Depends(require_temp_user),
) -> ProposalOut:
    _ = user
    p = get_proposal(proposal_id)
    if not p:
        raise HTTPException(status_code=404, detail="proposal not found")
    return ProposalOut.model_validate(p)


@app.post(
    "/v1/review/proposals",
    tags=["管理台 · 审核发布"],
    response_model=ProposalOut,
    summary="提交提案",
    description="请求体见 `ProposalCreate`。建模/人工变更先落 pending，审核通过再写 published。",
)
def api_create_proposal(
    body: ProposalCreate,
    user: TempUser = Depends(require_temp_user),
) -> ProposalOut:
    return ProposalOut.model_validate(
        create_proposal(
            body.kind,
            body.payload,
            user_id=user.user_id,
            user_name=user.user_name,
        )
    )


@app.post(
    "/v1/review/proposals/{proposal_id}/decision",
    tags=["管理台 · 审核发布"],
    response_model=ProposalOut,
    summary="通过 / 驳回提案",
    description="请求体见 `ProposalReviewBody`。approve 时按 kind 写入 KG published。",
)
def api_decide_proposal(
    proposal_id: int,
    body: ProposalReviewBody,
    user: TempUser = Depends(require_temp_user),
) -> ProposalOut:
    try:
        return ProposalOut.model_validate(
            review_proposal(
                proposal_id,
                action=body.action,
                user_id=user.user_id,
                user_name=user.user_name,
                reason=body.reason,
            )
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def main() -> None:
    import uvicorn

    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8088"))
    uvicorn.run("backend.api.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
