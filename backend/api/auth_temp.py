"""兼容层：原 user-id 头方案已改为 UC MAC Token。

业务代码仍可 `from backend.api.auth_temp import require_temp_user, TempUser`。
"""
from __future__ import annotations

from backend.api.auth import AuthUser as TempUser
from backend.api.auth import require_auth_user as require_temp_user

USER_HEADER_NOTE = (
    "【鉴权】业务接口需 `Authorization: MAC ...`（UC 登录后由 SDK 生成）。"
    "开发可用 AUTH_BYPASS=1 + 头 `X-Test-Uid` / `X-Test-Uname`。"
    "可选 `X-User-Name`（encodeURIComponent）补充展示名。"
)

SECURITY_SCHEMES = {
    "UCMacToken": {
        "type": "apiKey",
        "in": "header",
        "name": "Authorization",
        "description": USER_HEADER_NOTE + " 值示例：MAC id=\"...\",nonce=\"...\",mac=\"...\"",
    },
}
SECURITY_REQUIREMENT = [{"UCMacToken": []}]

__all__ = [
    "TempUser",
    "require_temp_user",
    "USER_HEADER_NOTE",
    "SECURITY_SCHEMES",
    "SECURITY_REQUIREMENT",
]
