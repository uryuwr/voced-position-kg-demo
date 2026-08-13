"""BTS 服务间认证 —— 临时 token 获取 + MAC 签名（对齐 bcs-ai-agent）。

调 BCS 内部 API 用的是**服务端**鉴权（BtsToken），与本项目已有的 UC MAC Token
（客户端鉴权，代表某个登录用户）是两回事：

    UC MAC   浏览器 → 本服务    带用户身份，签名基于用户的 access_token/mac_key
    BTS      本服务 → 外部服务  带应用身份，签名基于服务账号换来的临时 token

两步流程：

1. `POST {BTS_ENDPOINT}/v1/tokens` 用 account + secret 换 `{access_token, mac_key, expires_at}`，
   本地缓存，提前 60s 视为过期自动刷新
2. 每次请求用 mac_key 对 `[nonce, METHOD, uri, host, ...按 key 排序的 SDP-* 值, ""]`
   做 HMAC-SHA256 → Base64，拼成 `Authorization: BTS id="...",nonce="...",mac="..."`

签名串末尾要留一个空串（join 后产生尾部换行），SDP-* 头是**按 key 大写排序后取值**
参与签名——这两点和 bcs-ai-agent 的实现必须逐字一致，否则服务端验签不过。

本项目是同步栈（psycopg + 同步端点），故用 httpx 同步客户端与 threading.Lock，
不照搬 bcs 的 async 版本。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import random
import string
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

_RANDOM_CHARS = string.ascii_lowercase + string.digits


def _hmac_sha256_b64(key: str, message: str) -> str:
    """与 CryptoJS.HmacSHA256(...).toString(CryptoJS.enc.Base64) 等价。"""
    sig = hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(sig).decode("ascii")


def _nonce() -> str:
    """13 位毫秒时间戳 + ':' + 8 位随机小写字母数字。"""
    return f"{str(int(time.time() * 1000))[:13]}:{''.join(random.choices(_RANDOM_CHARS, k=8))}"


def build_token_request_body(account: str, secret: str) -> dict[str, Any]:
    ts = int(time.time() * 1000)
    return {
        "app_name": account,
        # 服务端只用它做展示/日志，真正校验的是 sign；照 bcs 的做法只带前 4 位
        "app_secret": secret[:4] + "******",
        "token_type": "e",
        "timestamp": ts,
        "sign": _hmac_sha256_b64(secret, f"{account}:{ts}"),
        "version": "python",
        "environment": "server",
    }


class BtsToken:
    __slots__ = ("access_token", "mac_key", "expires_at")

    def __init__(self, access_token: str, mac_key: str, expires_at: str):
        self.access_token = access_token
        self.mac_key = mac_key
        self.expires_at = expires_at

    @property
    def is_expired(self) -> bool:
        """提前 60 秒判过期，留出请求往返的余量。"""
        try:
            # BTS 返回 "2026-03-24T14:49:44.741+0800"，fromisoformat 要求 +08:00
            exp_str = self.expires_at
            if len(exp_str) >= 5 and exp_str[-5] in "+-" and ":" not in exp_str[-5:]:
                exp_str = exp_str[:-2] + ":" + exp_str[-2:]
            exp = datetime.fromisoformat(exp_str)
            now = datetime.now(tz=exp.tzinfo or timezone.utc)
            return (exp - now).total_seconds() < 60
        except (ValueError, TypeError):
            return True


class BtsTokenManager:
    """临时 token 的获取与缓存（线程安全）。"""

    def __init__(self, endpoint: str, account: str, secret: str, *, timeout: int = 10):
        self._endpoint = (endpoint or "").rstrip("/")
        self._account = account
        self._secret = secret
        self._timeout = timeout
        self._token: BtsToken | None = None
        self._lock = threading.Lock()

    def get_token(self) -> BtsToken:
        if self._token and not self._token.is_expired:
            return self._token
        with self._lock:
            if self._token and not self._token.is_expired:  # 双检，避免并发重复取
                return self._token
            self._token = self._fetch()
            return self._token

    def _fetch(self, max_retries: int = 3) -> BtsToken:
        if not (self._endpoint and self._account and self._secret):
            raise RuntimeError(
                "BTS 未配置：请在 .env 设置 BTS_ENDPOINT / BTS_ACCOUNT / BTS_PASSWORD"
            )
        url = f"{self._endpoint}/v1/tokens"
        body = build_token_request_body(self._account, self._secret)
        last: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                # verify=False 对齐 bcs：内网证书常不被信任
                with httpx.Client(timeout=self._timeout, verify=False) as c:
                    resp = c.post(url, json=body)
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"BTS 取 token 失败 [{resp.status_code}]：{resp.text[:300]}"
                    )
                d = resp.json()
                return BtsToken(d["access_token"], d["mac_key"], d["expires_at"])
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                last = e
                if attempt < max_retries:
                    time.sleep(1)
        raise RuntimeError(f"BTS 服务不可达（重试 {max_retries} 次）：{last}")

    def invalidate(self) -> None:
        with self._lock:
            self._token = None


def generate_bts_authorization(
    url: str, method: str, token: BtsToken, sdp_headers: dict[str, str] | None = None
) -> str:
    """签名串：[nonce, METHOD, uri(含 query), host, ...SDP-* 值(按 key 排序), ""] 以 \\n 连接。"""
    parsed = urlparse(url)
    uri = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    parts = [_nonce(), method.upper(), uri, parsed.netloc]
    if sdp_headers:
        for _k, v in sorted(
            ((k.upper(), v) for k, v in sdp_headers.items() if k.upper().startswith("SDP-")),
            key=lambda x: x[0],
        ):
            parts.append(v)
    parts.append("")  # 末尾空串 → join 后产生尾部换行，服务端按此验签
    mac = _hmac_sha256_b64(token.mac_key, "\n".join(parts))
    return f'BTS id="{token.access_token}",nonce="{parts[0]}",mac="{mac}"'
