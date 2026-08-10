"""UC 用户中心 — MAC Token 验证（对齐 bcs-ai-agent）。

前端 UC SDK `getAuthHeaderAsync()` 生成:
    Authorization: MAC id="<access_token>",nonce="<ts>:<random>",mac="<signature>"

后端:
    POST {UC_API_HOST}/v1.1/tokens/{access_token}/actions/valid
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from backend import settings

logger = logging.getLogger(__name__)

_MAC_FIELD_RE = re.compile(r'(\w+)="([^"]*)"')


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
) -> dict[str, Any]:
    """验证 MAC token，返回至少含 user_id；若响应有 nick_name/user_name 一并返回。"""
    url = f"{settings.UC_API_HOST.rstrip('/')}/v1.1/tokens/{access_token}/actions/valid"
    body = {
        "mac": mac,
        "nonce": nonce,
        "http_method": http_method.upper(),
        "request_uri": request_uri,
        "host": host,
    }
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            resp = await client.post(
                url,
                json=body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
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
