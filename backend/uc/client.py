"""UC 用户中心 — MAC Token 验证（对齐 bcs-ai-agent）。

前端 UC SDK `getAuthHeaderAsync()` 生成:
    Authorization: MAC id="<access_token>",nonce="<ts>:<random>",mac="<signature>"

后端:
    POST {UC_API_HOST}/v1.1/tokens/{access_token}/actions/valid
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from backend import settings

logger = logging.getLogger(__name__)

_MAC_FIELD_RE = re.compile(r'(\w+)="([^"]*)"')

# —— 共用 AsyncClient ——
# httpx 本来就带连接池，但池的生命周期 = Client 实例的生命周期。
# 原先写的是 `async with httpx.AsyncClient(...) as c:`，一出作用域连池一起销毁，
# 于是**每个带 Authorization 的请求**都要重做 TCP + TLS 握手
# （实测 94.78 ms → 复用后 27.61 ms）。UC 校验在每个接口前都跑一次，这笔最贵。
#
# AsyncClient 只在**单个 event loop 内**安全，所以连 loop 一起记：
# 换了 loop（测试里常见：一个 TestClient 一个 loop）就重建，
# 否则会在旧 loop 已关闭的连接上报 "Event loop is closed"。
_async_client: httpx.AsyncClient | None = None
_async_client_loop: asyncio.AbstractEventLoop | None = None


def _get_async_client() -> httpx.AsyncClient:
    global _async_client, _async_client_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 非 async 上下文（单元测试直接调）——退回一次性客户端
        loop = None
    client = _async_client
    if client is not None and not client.is_closed and _async_client_loop is loop:
        return client
    client = httpx.AsyncClient(timeout=10, verify=settings.tls_verify())
    _async_client = client
    _async_client_loop = loop
    return client


async def close_async_client() -> None:
    """进程/应用退出时释放连接（FastAPI shutdown 调用）。"""
    global _async_client, _async_client_loop
    client, _async_client, _async_client_loop = _async_client, None, None
    if client is not None and not client.is_closed:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 —— 退出路径不因清理失败而报错
            pass


class UCAuthError(Exception):
    """UC 认证失败。"""


def parse_mac_header(authorization: str) -> dict[str, str]:
    if not authorization or not authorization.upper().startswith("MAC "):
        raise UCAuthError("Authorization header 必须以 'MAC ' 开头")
    fields = dict(_MAC_FIELD_RE.findall(authorization))
    missing = [k for k in ("id", "nonce", "mac") if k not in fields]
    if missing:
        raise UCAuthError(f"MAC header 缺少字段: {', '.join(missing)}")
    return fields


async def validate_uc_token(
    access_token: str,
    mac: str,
    nonce: str,
    http_method: str,
    request_uri: str,
    host: str,
    sdp_app_id: str | None = None,
) -> dict[str, Any]:
    """验证 MAC token，返回至少含 user_id；若响应有 nick_name/user_name 一并返回。

    sdp_app_id：**一律用调用方（前端请求头 `sdp-app-id`）透传的值**，服务端不再
    回落 .env 的 SDP_APP_ID。同一后端可能同时服务多个前端应用，写死或兜底会让
    「服务端配置」和「实际调用方」悄悄错开，出问题很难查；缺失时直接报错更清楚。
    """
    url = f"{settings.UC_API_HOST.rstrip('/')}/v1.1/tokens/{access_token}/actions/valid"
    body = {
        "mac": mac,
        "nonce": nonce,
        "http_method": http_method.upper(),
        "request_uri": request_uri,
        "host": host,
    }
    app_id = (sdp_app_id or "").strip()
    if not app_id:
        raise UCAuthError("缺少 sdp-app-id 请求头（应由前端透传）")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "sdp-app-id": app_id,
    }
    try:
        client = _get_async_client()
        resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as e:
        logger.exception("UC token 验证请求失败: %s", e)
        raise UCAuthError("UC 服务不可用，请稍后重试") from e

    if resp.status_code not in (200, 201):
        logger.warning("UC token 验证返回 %s: %s", resp.status_code, resp.text[:200])
        raise UCAuthError("UC 认证失败，请重新登录")

    try:
        data = resp.json()
    except Exception as e:
        raise UCAuthError("UC 验证接口返回格式异常") from e

    user_id = data.get("user_id")
    if user_id is None:
        raise UCAuthError("UC 验证响应缺少 user_id")

    name = (
        data.get("user_name")
        or data.get("real_name")
        or data.get("nick_name")
        or data.get("user_name_zh")
        or ""
    )
    return {
        "user_id": str(user_id),
        "user_name": str(name) if name else str(user_id),
        "raw": data,
    }
