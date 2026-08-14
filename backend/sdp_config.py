"""SDP 配置中心客户端（Spring Cloud Config）。

启动时用一个 secret 把配置中心的业务配置拉下来、摊平成大写下划线的环境变量写进
``os.environ``，之后 ``settings.py`` 照常 ``os.getenv`` 即可 —— 业务代码零感知。

对齐 bcs-ai-agent / nd-rest 的双层鉴权：

1. 平台级 Basic Auth（全平台固定常量，非颗粒 secret）
2. Header ``x-sdp-app-name`` + ``x-sdp-cloud-config-token = sha256(app:secret:ts:SALT):ts``

⚠️ **本文件含内网平台级凭据（Basic 账密与 token SALT）。仓库转 private 之前不要提交。**
   它们不是关键密钥（第二层 token 仍需颗粒各自的 secret 才算得出来），但属于内网基础设施
   信息，不该出现在公开仓库里。真正的密钥是 ``SDP_CLOUD_CONFIG_SECRET``，它只从环境变量
   读，永远不进代码、不进 .env.example、不进文档。

唯一必需的 bootstrap 变量：

- ``SDP_CLOUD_CONFIG_SECRET`` —— 颗粒独立账密。**有值才拉，没有就跳过，完全走本地配置。**

profile（拉哪套环境）优先级：``APP_ENV`` > ``SDP_ENV_NAME``（SDP K8s 注入）> ``SDP_ENV`` > ``development``
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import quote, urljoin

logger = logging.getLogger(__name__)

# —— 写死常量（对齐 nd-rest WafEnvironmentPostProcessor 默认）——
SDP_APP_NAME = "voced-position-kg"
SDP_CLOUD_CONFIG_URI = "https://sdp-cloud-config-server.sdp.101.com"
SDP_CLOUD_CONFIG_LABEL = "master"
# Portal 上的「配置文件」名 → Header x-sdp-cluster；缺省 application
SDP_CLOUD_CONFIG_FILE = "application"
SDP_CONFIG_TIMEOUT_SEC = 15.0
# 拉失败不拦启动：内测网关常不可达，降级到本地配置继续跑（与 agent/ 的降级风格一致）
SDP_CONFIG_FAIL_FAST = False

# 平台级 Spring Cloud Config Basic Auth（与 Java 默认一致，不是颗粒 secret）
SDP_PLATFORM_CONFIG_USERNAME = "username-69d907b2-99b0-402b-9d19-d4ba5af436ad"
SDP_PLATFORM_CONFIG_PASSWORD = "password-cd668d9b-0092-443d-a6ad-6dc5d343706d"

# SdpCloudTokenUtil
_SALT = "^@hqdm3#eg*cnhlaofk7tch4$k"
_SEP = ":"

_HDR_APP_NAME = "x-sdp-app-name"
_HDR_CONFIG_TOKEN = "x-sdp-cloud-config-token"
_HDR_CLUSTER = "x-sdp-cluster"

# 远端永不覆盖的键：它们决定「去哪儿拉、拉哪套」，被覆盖会自相矛盾
BOOTSTRAP_ENV_KEYS = frozenset(
    {
        "SDP_CLOUD_CONFIG_SECRET",
        "SDP_CONFIG_SECRET",
        "SDP_CLUSTER",
        "SDP_CLOUD_CONFIG_FILE",
        "APP_ENV",
        "SDP_ENV_NAME",
        "SDP_ENV",
    }
)

_DEFAULT_PROFILE = "development"


@dataclass(frozen=True)
class SdpConfigResult:
    source: str
    flat: dict[str, str]
    applied: list[str]
    overridden_locally: list[str] = field(default_factory=list)


def _env(environ: Mapping[str, str], name: str, default: str = "") -> str:
    return (environ.get(name) or default).strip()


def resolve_profile(environ: Mapping[str, str]) -> str:
    """APP_ENV > SDP_ENV_NAME（平台注入）> SDP_ENV > development。"""
    return (
        _env(environ, "APP_ENV")
        or _env(environ, "SDP_ENV_NAME")
        or _env(environ, "SDP_ENV")
        or _DEFAULT_PROFILE
    )


def generate_config_token(app_name: str, app_secret: str, *, now_ms: int | None = None) -> str:
    """sha256(app:secret:ts:SALT).hex() + ":" + ts —— 对齐 SdpCloudTokenUtil。"""
    ts = int(time.time() * 1000) if now_ms is None else int(now_ms)
    raw = f"{app_name}{_SEP}{app_secret}{_SEP}{ts}{_SEP}{_SALT}"
    return f"{hashlib.sha256(raw.encode('utf-8')).hexdigest()}{_SEP}{ts}"


def _flatten(data: Any, prefix: str = "") -> dict[str, str]:
    """嵌套 dict → A_B_C 大写键。list/dict 值转 JSON 字符串，None 跳过。"""
    out: dict[str, str] = {}
    if not isinstance(data, dict):
        return out
    for k, v in data.items():
        key = str(k)
        path = f"{prefix}_{key}" if prefix else key
        env_key = path.upper()
        if isinstance(v, dict):
            out.update(_flatten(v, path))
        elif isinstance(v, list):
            out[env_key] = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        elif v is None:
            continue
        elif isinstance(v, bool):
            out[env_key] = "true" if v else "false"
        else:
            out[env_key] = str(v)
    return out


def _unflatten_dotted(source: Mapping[str, Any]) -> dict[str, Any]:
    """Spring 的 a.b.c 扁平键还原成嵌套 dict。"""
    root: dict[str, Any] = {}
    for k, v in source.items():
        parts = str(k).split(".")
        cur: dict[str, Any] = root
        for p in parts[:-1]:
            nxt = cur.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[p] = nxt
            cur = nxt
        cur[parts[-1]] = v
    return root


def _parse_payload(text: str) -> dict[str, str]:
    text = text.strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            sources = obj.get("propertySources")
            if isinstance(sources, list):
                merged: dict[str, Any] = {}
                # 靠前的 source 优先级更高 → 倒序合并，后写入的覆盖先写入的
                for src in reversed(sources):
                    if not isinstance(src, dict):
                        continue
                    body = src.get("source")
                    if not isinstance(body, dict):
                        continue
                    for k, v in _unflatten_dotted(body).items():
                        if isinstance(v, dict) and isinstance(merged.get(k), dict):
                            merged[k] = {**merged[k], **v}
                        else:
                            merged[k] = v
                return _flatten(merged) if merged else {}
            if isinstance(obj.get("data"), str):
                return _parse_payload(obj["data"])
            return _flatten(obj)
    # 非 JSON 一律按 YAML 处理（Portal 直传 .yml 时走这里）
    try:
        import yaml  # 已登记进 backend/requirements.txt
    except ImportError:  # pragma: no cover
        logger.warning("sdp_config_yaml_missing: 装了 pyyaml 才能解析 YAML 响应")
        return {}
    try:
        loaded = yaml.safe_load(text)
    except Exception:  # noqa: BLE001
        return {}
    return _flatten(loaded) if isinstance(loaded, dict) else {}


def fetch_config(
    *,
    app_secret: str,
    profile: str,
    application: str = SDP_APP_NAME,
    uri: str = SDP_CLOUD_CONFIG_URI,
    label: str = SDP_CLOUD_CONFIG_LABEL,
    cluster: str = SDP_CLOUD_CONFIG_FILE,
    timeout: float = SDP_CONFIG_TIMEOUT_SEC,
) -> tuple[str, dict[str, str]]:
    """从 Spring Cloud Config Server 拉配置，返回 (命中的 url, 扁平化 kv)。"""
    import httpx  # 已登记进 backend/requirements.txt

    base = uri.rstrip("/") + "/"
    headers = {
        "Accept": "application/json, application/yaml, text/plain, */*",
        _HDR_APP_NAME: application,
        _HDR_CONFIG_TOKEN: generate_config_token(application, app_secret),
    }
    if cluster.strip():
        headers[_HDR_CLUSTER] = cluster.strip()

    app_q = quote(application, safe="")
    prof_q = quote(profile, safe="")
    label_q = quote(label, safe="")
    paths = [
        f"{app_q}/{prof_q}/{label_q}",
        f"{app_q}/{prof_q}",
        f"{label_q}/{app_q}-{prof_q}.yml",
        f"{app_q}-{prof_q}.yml",
    ]

    last_err: Exception | None = None
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for path in paths:
            url = urljoin(base, path)
            try:
                resp = client.get(
                    url,
                    auth=(SDP_PLATFORM_CONFIG_USERNAME, SDP_PLATFORM_CONFIG_PASSWORD),
                    headers=headers,
                )
                if resp.status_code == 404:
                    last_err = RuntimeError(f"404 {url}")
                    continue
                resp.raise_for_status()
                flat = _parse_payload(resp.text)
                if not flat and resp.text.strip():
                    logger.warning("sdp_config_empty url=%s body_len=%d", url, len(resp.text))
                return url, flat
            except Exception as e:  # noqa: BLE001
                last_err = e
                continue
    raise RuntimeError(
        f"sdp config fetch failed application={application!r} profile={profile!r}: {last_err}"
    )


def apply_to_environ(
    flat: Mapping[str, str],
    *,
    environ: dict[str, str] | None = None,
    protect: frozenset[str] | set[str] = frozenset(),
) -> tuple[list[str], list[str]]:
    """把远端 kv 写进环境变量，返回 (写入的键, 因本地已声明而跳过的键)。

    ``protect`` 是本地 `.env` 里显式写死的键 —— 它们不被远端覆盖，这样本地开发
    才能临时改 `SERVE_DEV_UI` / `DATABASE_URL` 之类做调试。线上镜像里没有 `.env`
    （Dockerfile 已 `rm`），protect 为空集，配置中心说了算。
    """
    target = environ if environ is not None else os.environ  # type: ignore[assignment]
    applied: list[str] = []
    skipped: list[str] = []
    for key, value in flat.items():
        k = key.upper()
        if k in BOOTSTRAP_ENV_KEYS:
            continue
        if k in protect:
            skipped.append(k)
            continue
        target[k] = value
        applied.append(k)
    return applied, skipped


def bootstrap(protect: frozenset[str] | set[str] = frozenset()) -> SdpConfigResult | None:
    """进程启动时调用：有 secret 就拉配置并注入环境变量，否则返回 None。

    ``protect``：本地 `.env` 显式声明的键，远端不覆盖（本地调试用）。

    **必须在任何模块级 ``os.getenv`` 求值之前调用**（本项目的配置是模块级常量，
    import 即固化）。见 settings.py 顶部。
    """
    secret = _env(os.environ, "SDP_CLOUD_CONFIG_SECRET") or _env(os.environ, "SDP_CONFIG_SECRET")
    if not secret:
        return None

    profile = resolve_profile(os.environ)
    # 平台只注入 SDP_ENV_NAME 时，把解析结果同步到 APP_ENV，后续逻辑读哪个都一致
    if not _env(os.environ, "APP_ENV"):
        os.environ["APP_ENV"] = profile

    cluster = (
        _env(os.environ, "SDP_CLUSTER")
        or _env(os.environ, "SDP_CLOUD_CONFIG_FILE")
        or SDP_CLOUD_CONFIG_FILE
    )

    try:
        url, flat = fetch_config(app_secret=secret, profile=profile, cluster=cluster)
    except Exception as e:  # noqa: BLE001
        if SDP_CONFIG_FAIL_FAST:
            raise
        logger.warning("sdp_config_bootstrap_failed profile=%s: %s", profile, e)
        return None

    applied, skipped = apply_to_environ(flat, protect=protect)
    logger.info(
        "sdp_config_bootstrap_ok app=%s profile=%s label=%s keys=%d applied=%d local_override=%s source=%s",
        SDP_APP_NAME,
        profile,
        SDP_CLOUD_CONFIG_LABEL,
        len(flat),
        len(applied),
        ",".join(skipped) or "-",
        url,
    )
    return SdpConfigResult(
        source=url, flat=dict(flat), applied=applied, overridden_locally=skipped
    )
