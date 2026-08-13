"""BTS 服务间鉴权（对齐 bcs-ai-agent）：本服务 → 外部/BCS 内部接口。

与已有的 UC MAC Token 区分：那是「浏览器 → 本服务」的**用户**鉴权，
这里是「本服务 → 外部服务」的**应用**鉴权。
"""
from backend.bts.auth import (
    BtsToken,
    BtsTokenManager,
    generate_bts_authorization,
)
from backend.bts.client import BtsClient, BtsError, bts_client, bts_info

__all__ = [
    "BtsToken",
    "BtsTokenManager",
    "generate_bts_authorization",
    "BtsClient",
    "BtsError",
    "bts_client",
    "bts_info",
]
