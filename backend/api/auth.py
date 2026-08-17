"""鉴权：UC MAC Token 为主；开发旁路 AUTH_BYPASS=1（.env 配置）。"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import unquote

from fastapi import Header, HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from backend import settings
from backend.uc.client import UCAuthError, parse_mac_header, validate_uc_token

# 当前请求用户
_current_user: ContextVar["AuthUser | None"] = ContextVar("current_user", default=None)

AUTH_BYPASS = settings.AUTH_BYPASS
AUTH_DEBUG = settings.AUTH_DEBUG

# 无需 MAC 的路径
_SKIP_EXACT = {"/", "/health", "/openapi.json", "/docs", "/redoc", "/api-guide"}
_SKIP_PREFIX = (
    "/docs",
    "/redoc",
    "/openapi",
    "/schemas",
    "/seed",
    "/js",
    "/admin",
    "/dev",
    "/cn-sources",
    "/v1/config",  # 前端拉 UC 配置
)


@dataclass(frozen=True)
class AuthUser:
    user_id: str
    user_name: str


def get_auth_user() -> AuthUser | None:
    return _current_user.get()


def require_user() -> AuthUser:
    u = _current_user.get()
    if not u:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "unauthorized",
                "message": "请先 UC 登录；请求需带 Authorization: MAC ...",
            },
        )
    return u


async def require_auth_user(
    request: Request,
    authorization: str | None = Header(None, alias="Authorization"),
    x_user_name: str | None = Header(
        None,
        alias="X-User-Name",
        description="可选展示名（UC 验证后补充；中文请 encodeURIComponent）",
    ),
    x_test_uid: str | None = Header(None, alias="X-Test-Uid"),
    x_test_uname: str | None = Header(None, alias="X-Test-Uname"),
) -> AuthUser:
    """
    FastAPI Depends 用：优先读中间件写入的 ContextVar；
    若无（测试场景）则按旁路头解析。
    """
    u = _current_user.get()
    if u:
        # 可用 X-User-Name 覆盖展示名（已登录前提下）
        if x_user_name:
            name = unquote(x_user_name.strip())
            if name:
                return AuthUser(user_id=u.user_id, user_name=name)
        return u

    if _bypass_enabled():
        uid = (x_test_uid or settings.DEV_USER_ID or "0").strip()
        uname = unquote((x_test_uname or settings.DEV_USER_NAME or "dev").strip())
        if uid:
            user = AuthUser(user_id=uid, user_name=uname or uid)
            _current_user.set(user)
            return user

    raise HTTPException(
        status_code=401,
        detail="未认证：需要 Authorization MAC Token（或开发旁路 AUTH_BYPASS=1）",
    )


def _bypass_enabled() -> bool:
    """开发旁路是否开启。

    只认这两个显式开关。曾经 `AUTH_DEBUG` 会被 `DEBUG=1` 顺带打开
    （见 settings.AUTH_DEBUG 的注释），那等于把「日志开详细点」做成了「不校验身份」。
    """
    return bool(AUTH_BYPASS or AUTH_DEBUG)


def _bypass_user(request: Request) -> AuthUser | None:
    """按测试头造一个用户；旁路未开启则 None。

    这段逻辑原先在中间件里逐字抄了两份（无 Authorization / UC 校验失败各一份）。
    鉴权旁路最怕的就是「收紧了一处，另一处还留着后门」，所以收口到这里，
    要改条件只改 `_bypass_enabled()`。
    """
    if not _bypass_enabled():
        return None
    uid = request.headers.get("x-test-uid") or settings.DEV_USER_ID or "0"
    uname = unquote(
        request.headers.get("x-test-uname") or settings.DEV_USER_NAME or "dev"
    )
    return AuthUser(user_id=str(uid), user_name=uname or str(uid))


def _should_skip(path: str) -> bool:
    if path in _SKIP_EXACT:
        return True
    for p in _SKIP_PREFIX:
        if path == p or path.startswith(p + "/") or path.startswith(p + "?"):
            return True
    # swagger static
    if path.startswith("/docs") or path.startswith("/redoc"):
        return True
    return False


class UCAuthMiddleware(BaseHTTPMiddleware):
    """解析 Authorization: MAC ...，验证 UC，写入 ContextVar。"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # CORS 预检：浏览器规范规定 OPTIONS 预检不携带 Authorization，
        # 在此拦成 401 会让浏览器只报跨域。放行交给外层 CORSMiddleware 应答。
        if request.method == "OPTIONS":
            return await call_next(request)
        # 每个请求重置
        token = _current_user.set(None)
        try:
            if _should_skip(path) or not path.startswith("/v1/"):
                # 开发旁路：静态/文档也可带测试头
                dev = _bypass_user(request)
                if dev:
                    _current_user.set(dev)
                return await call_next(request)

            authorization = request.headers.get("authorization", "")
            if not authorization:
                dev = _bypass_user(request)
                if dev:
                    _current_user.set(dev)
                    return await call_next(request)
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": "missing_authorization",
                        "message": "缺少 Authorization: MAC ... 请先 UC 登录",
                    },
                )

            try:
                mac_fields = parse_mac_header(authorization)
                # 与 UC SDK 签名对齐：Host + 请求行中的 raw path（保留 %3A 等编码）
                # 若用 request.url.path（已解码），会与前端 encodeURIComponent 后的签名不一致 → INVALID_MAC
                host_header = request.headers.get("host", "")
                raw = request.scope.get("raw_path")
                if raw:
                    request_uri = raw.decode("latin-1")
                else:
                    request_uri = request.url.path
                qs = request.scope.get("query_string") or b""
                if qs:
                    request_uri = f"{request_uri}?{qs.decode('latin-1')}"
                # sdp-app-id：优先取前端透传的请求头，缺失才回落 .env 配置
                info = await validate_uc_token(
                    access_token=mac_fields["id"],
                    mac=mac_fields["mac"],
                    nonce=mac_fields["nonce"],
                    http_method=request.method,
                    request_uri=request_uri,
                    host=host_header,
                    sdp_app_id=request.headers.get("sdp-app-id"),
                )
                uname = info["user_name"]
                # 前端可用 X-User-Name 补展示名（getAccountInfo）
                xname = request.headers.get("x-user-name")
                if xname:
                    uname = unquote(xname.strip()) or uname
                _current_user.set(
                    AuthUser(user_id=info["user_id"], user_name=uname or info["user_id"])
                )
            except UCAuthError as e:
                dev = _bypass_user(request)
                if dev:
                    _current_user.set(dev)
                    return await call_next(request)
                return JSONResponse(
                    status_code=401,
                    content={"error": "uc_auth_failed", "message": str(e)},
                )

            return await call_next(request)
        finally:
            _current_user.reset(token)
