"""BTS 客户端：带服务端鉴权地调用外部（BCS 内部）接口。

用法：

    from backend.bts import bts_client

    data = bts_client().post("/v1/learning-plans", json={...})

约定与 bcs-ai-agent 一致：
- `sdp-app-id` 既进签名（作为 SDP-* 参与计算）也进请求头
- 未配置 BTS 时 `available()` 返回 False，调用方应据此降级（本项目的学习计划接口
  就是这么做的：没配就返回 mock plan_id），而不是抛错中断业务
"""
from __future__ import annotations

import threading
from typing import Any

import httpx

from backend import settings
from backend.bts.auth import BtsTokenManager, generate_bts_authorization


class BtsError(RuntimeError):
    """外部接口调用失败（含 HTTP 非 2xx）。"""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class BtsClient:
    def __init__(
        self,
        *,
        endpoint: str | None = None,
        bts_endpoint: str | None = None,
        account: str | None = None,
        secret: str | None = None,
        sdp_app_id: str | None = None,
        timeout: int | None = None,
    ):
        self.endpoint = (endpoint or settings.BTS_API_ENDPOINT or "").rstrip("/")
        self.sdp_app_id = sdp_app_id or settings.BTS_SDP_APP_ID or settings.SDP_APP_ID
        self.timeout = timeout or settings.BTS_REQUEST_TIMEOUT
        self._tm = BtsTokenManager(
            bts_endpoint or settings.BTS_ENDPOINT,
            account or settings.BTS_ACCOUNT,
            secret or settings.BTS_PASSWORD,
        )
        # 长生命周期 httpx.Client（本类已是进程单例，见 bts_client()）：
        # 原先每次 request 都 `with httpx.Client(...)`，等于每次调用都重做 TCP+TLS。
        # 同步 Client 多线程安全，直接复用。
        self._http: httpx.Client | None = None
        self._http_lock = threading.Lock()

    def _client(self) -> httpx.Client:
        c = self._http
        if c is not None and not c.is_closed:
            return c
        with self._http_lock:
            c = self._http
            if c is None or c.is_closed:
                c = httpx.Client(timeout=self.timeout, verify=settings.tls_verify())
                self._http = c
            return c

    def close(self) -> None:
        c, self._http = self._http, None
        if c is not None and not c.is_closed:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        self._tm.close()

    def available(self) -> bool:
        """配置齐全才算可用；不可用时调用方应降级而不是报错。"""
        return bool(
            settings.BTS_ENDPOINT and settings.BTS_ACCOUNT and settings.BTS_PASSWORD
        )

    def _headers(self, method: str, url: str, uc_user_id: str | None = None) -> dict[str, str]:
        sdp = {"sdp-app-id": self.sdp_app_id} if self.sdp_app_id else {}
        token = self._tm.get_token()
        h = {
            "Authorization": generate_bts_authorization(url, method, token, sdp),
            "Content-Type": "application/json",
        }
        h.update(sdp)  # 既参与签名，也要真的发出去
        if uc_user_id:
            # BTS 是应用身份，本身不带用户；代表某个用户调用时要用 Userid 头指明目标
            # UC User ID（即前端 MAC token 经 UC 校验后返回的 user_id）
            h["Userid"] = str(uc_user_id)
        return h

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        uc_user_id: str | None = None,
        retry_on_401: bool = True,
    ) -> Any:
        if not self.available():
            raise BtsError("BTS 未配置（BTS_ENDPOINT / BTS_ACCOUNT / BTS_PASSWORD）")
        url = path_or_url if path_or_url.startswith("http") else f"{self.endpoint}{path_or_url}"
        headers = self._headers(method, url, uc_user_id)
        if extra_headers:
            headers.update({k: v for k, v in extra_headers.items() if v})

        resp = self._client().request(
            method.upper(), url, json=json, params=params, headers=headers
        )

        # token 可能在服务端被提前失效，重签一次再试
        if resp.status_code == 401 and retry_on_401:
            self._tm.invalidate()
            return self.request(
                method, path_or_url, json=json, params=params,
                extra_headers=extra_headers, uc_user_id=uc_user_id, retry_on_401=False,
            )

        body: Any
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        if resp.status_code >= 400:
            raise BtsError(
                f"外部接口 {method.upper()} {url} 失败 [{resp.status_code}]",
                status=resp.status_code,
                body=body,
            )
        return body

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)   # kw 可带 uc_user_id / extra_headers

    def post(self, path: str, **kw: Any) -> Any:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw: Any) -> Any:
        return self.request("PUT", path, **kw)


_CLIENT: BtsClient | None = None
_LOCK = threading.Lock()


def bts_client() -> BtsClient:
    """进程内单例：token 缓存要共享，否则每次调用都去换 token。"""
    global _CLIENT
    if _CLIENT is None:
        with _LOCK:
            if _CLIENT is None:
                _CLIENT = BtsClient()
    return _CLIENT


def close_bts_client() -> None:
    """进程退出时释放 HTTP 连接（FastAPI shutdown 调用）。"""
    global _CLIENT
    with _LOCK:
        c, _CLIENT = _CLIENT, None
    if c is not None:
        c.close()


def bts_info() -> dict[str, Any]:
    """配置自检（不回显密钥）。"""
    c = bts_client()
    return {
        "available": c.available(),
        "bts_endpoint": settings.BTS_ENDPOINT or None,
        "api_endpoint": c.endpoint or None,
        "account": settings.BTS_ACCOUNT or None,
        "has_secret": bool(settings.BTS_PASSWORD),
        "sdp_app_id": c.sdp_app_id or None,
    }
