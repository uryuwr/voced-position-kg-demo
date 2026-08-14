"""
集中读取环境变量。

**配置真源是 SDP 配置中心**（颗粒 voced-position-kg，见 `sdp_config.py`）。
`backend/.env` 只承载 bootstrap 键（`SDP_CLOUD_CONFIG_SECRET` + profile），
业务配置一律从配置中心下发；本地无 secret 时才回落到 `.env` 里的业务键。

加载顺序（后者覆盖前者）：
  1. 进程环境变量（已有）
  2. 可选：仓库根 `.env`（仅 monorepo 本地兼容，不覆盖已有键）
  3. `backend/.env`（override=True）
  4. **SDP 配置中心**（有 secret 才拉，override=True —— 线上以它为准）

拉不到不拦启动：降级用 1~3 的值，`GET /health` 的 `config_source` 会显示 `local`。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

# backend/ 目录（本文件所在包根；Docker 内为 /app/backend）
BACKEND_DIR = Path(__file__).resolve().parent
# monorepo 仓库根（backend 的上一级）；独立只拷 backend 时可能不存在业务文件
REPO_ROOT = BACKEND_DIR.parent


def _load_env_files() -> set[str]:
    """加载 .env，返回 backend/.env 里**显式声明**的键名。

    这批键随后会传给 SDP bootstrap 作保护名单：远端不覆盖它们，本地开发才能
    临时改 `SERVE_DEV_UI`/`DATABASE_URL` 之类做调试（否则远端一注入就被盖掉）。
    线上镜像没有 .env（Dockerfile 已 rm），保护名单为空，配置中心说了算。
    """
    # monorepo 本地：根 .env 作兜底（不抢占已 export 的变量）
    repo_env = REPO_ROOT / ".env"
    if repo_env.is_file() and REPO_ROOT != BACKEND_DIR:
        load_dotenv(repo_env, override=False)
    # 后端包内配置优先
    backend_env = BACKEND_DIR / ".env"
    if not backend_env.is_file():
        return set()
    declared = {k.upper() for k in dotenv_values(backend_env) if k}
    load_dotenv(backend_env, override=True)
    return declared


_LOCAL_ENV_KEYS = _load_env_files()

# —— SDP 配置中心 ——
# 有 SDP_CLOUD_CONFIG_SECRET 就把远端业务配置注入 os.environ；`.env` 里显式写的键
# 不被覆盖（本地调试用，见 _load_env_files）。
# **必须在下面任何 os.getenv 求值之前完成**：本模块的配置是模块级常量，import 即固化；
# 绕过本模块直接 os.getenv 的地方（api/main.py、kg/pg_store/config.py 等）也都靠
# `import backend.settings` 触发这里，所以放在这个位置就能全覆盖。
_SDP_CONFIG = None
try:
    from backend.sdp_config import bootstrap as _sdp_bootstrap

    _SDP_CONFIG = _sdp_bootstrap(protect=_LOCAL_ENV_KEYS)
except Exception:  # noqa: BLE001 —— 配置中心不可达不拦启动，降级本地
    _SDP_CONFIG = None


def config_source() -> str:
    """`remote`=本进程的业务配置来自 SDP 配置中心；`local`=来自 .env / 环境变量。

    线上排查第一问「这台实例的配置到底从哪来」，别只靠翻日志里那行 bootstrap_ok。
    """
    return "remote" if _SDP_CONFIG else "local"


def config_detail() -> dict:
    if not _SDP_CONFIG:
        return {"source": "local", "keys": 0, "profile": None, "local_override": []}
    return {
        "source": "remote",
        "keys": len(_SDP_CONFIG.applied),
        "profile": os.getenv("APP_ENV") or None,
        # 被本地 .env 挡下、没吃到远端值的键 —— 「配置中心改了没生效」多半就是它
        "local_override": _SDP_CONFIG.overridden_locally,
    }


def _bool(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# —— 服务 ——
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8088"))
SERVE_DEV_UI = _bool("SERVE_DEV_UI", "0")
CORS_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()
]

# —— 鉴权 ——
# 生产必须为 0；本地无 UC 时才开 1
AUTH_BYPASS = _bool("AUTH_BYPASS", "0")
AUTH_DEBUG = _bool("AUTH_DEBUG", "0") or _bool("DEBUG", "0")
DEV_USER_ID = os.getenv("DEV_USER_ID", "0")
DEV_USER_NAME = os.getenv("DEV_USER_NAME", "dev")

# —— 审核 ——
# 0=无审核直写（默认，本期内用）：POST /v1/admin/changes 立即生效
# 1=进待审队列，须 approve 后才写主数据
REVIEW_REQUIRED = _bool("REVIEW_REQUIRED", "0")

# —— UC（与 bcs-ai-agent 对齐，全部来自 .env）——
UC_SDK_URL = os.getenv(
    "UC_SDK_URL",
    "https://<uc-cdn-host>/v0.1/static/uc_sdk/v1.9.9/UC-SDK.min.js",
)
UC_ENV = os.getenv("UC_ENV", "preproduction")
UC_COMPONENT_HOST = os.getenv("UC_COMPONENT_HOST", "<uc-component-host>")
UC_API_HOST = os.getenv("UC_API_HOST", "https://<uc-gateway-host>")
# 仅供 BTS 等**服务端出站**调用兜底；UC 校验一律用前端透传的 sdp-app-id，不读这里
SDP_APP_ID = os.getenv("SDP_APP_ID", "") or os.getenv("BCS_SDP_APP_ID", "")

# —— 数据 ——
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://voced:<your-password>@localhost:5432/voced_kg",
)
KG_REGION = os.getenv("KG_REGION", "CN")

# —— BTS 服务间鉴权（对齐 bcs-ai-agent）：本服务调用外部/BCS 内部接口 ——
# 与 UC MAC Token 不同：那是「浏览器→本服务」的用户鉴权，这是「本服务→外部」的应用鉴权。
BTS_ENDPOINT = os.getenv("BTS_ENDPOINT", "")          # 取 token 的服务，如 https://ucbts.101.com
BTS_ACCOUNT = os.getenv("BTS_ACCOUNT", "")            # 服务账号
BTS_PASSWORD = os.getenv("BTS_PASSWORD", "")          # 服务账号密钥
BTS_API_ENDPOINT = os.getenv("BTS_API_ENDPOINT", "")  # 被调用的外部接口基址
BTS_SDP_APP_ID = os.getenv("BTS_SDP_APP_ID", "") or os.getenv("SDP_APP_ID", "")
BTS_REQUEST_TIMEOUT = int(os.getenv("BTS_REQUEST_TIMEOUT", "30"))
# 外部学习计划服务的路径（相对 BTS_API_ENDPOINT）；留空则学习计划走本地 mock
LEARNING_PLAN_PATH = os.getenv("LEARNING_PLAN_PATH", "")
# 用户画像服务（五维记忆），走 BTS 鉴权；留空则匹配度只用测评画像。
# 键名只认下划线：连字符写法（openq-ai-manager）过不了 SDP 配置中心——它的扁平化
# 只做 upper() 不转连字符，会下发成 OPENQ-AI-MANAGER，而 Linux 环境变量名大小写
# 与字符都敏感，这里就读不到，画像功能会静默降级。别再加回连字符别名。
OPENQ_AI_MANAGER = os.getenv("OPENQ_AI_MANAGER", "").rstrip("/")


def bts_configured() -> bool:
    return bool(BTS_ENDPOINT and BTS_ACCOUNT and BTS_PASSWORD)


# —— AI 网关（对齐 bcs-ai-agent：OpenAI 兼容协议）——
# bcs 使用 GITHUB_TOKEN 作网关 Authorization；LLM_BASE_URL 须以 /v1 结尾（SDK 会拼 /chat/completions）
GITHUB_TOKEN = (os.getenv("GITHUB_TOKEN") or os.getenv("LLM_API_KEY") or "").strip()
LLM_BASE_URL = (os.getenv("LLM_BASE_URL") or "").strip().rstrip("/")
if LLM_BASE_URL and not LLM_BASE_URL.endswith("/v1"):
    LLM_BASE_URL = LLM_BASE_URL + "/v1"
LLM_MODEL = (os.getenv("LLM_MODEL") or "").strip()
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))
LLM_MAX_OUTPUT_TOKENS = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "8192"))
LLM_REQUEST_TIMEOUT = int(os.getenv("LLM_REQUEST_TIMEOUT", "120"))
# 未配齐凭证时诊断走规则降级，不拦服务启动
LLM_ENABLED = bool(GITHUB_TOKEN and LLM_BASE_URL and LLM_MODEL)


def llm_configured() -> bool:
    return LLM_ENABLED


def frontend_config() -> dict:
    """给 GET /v1/config 用。"""
    return {
        "uc_sdk_url": UC_SDK_URL,
        "uc_env": UC_ENV,
        "uc_component_host": UC_COMPONENT_HOST,
        "uc_api_host": UC_API_HOST,
        # sdp_app_id 不再下发：它由前端自己持有并透传（见 frontend/js/api-client.js），
        # 服务端持有一份只会和真实调用方悄悄错开
        "auth_bypass": AUTH_BYPASS,
        "review_required": REVIEW_REQUIRED,
        "api_version": os.getenv("API_VERSION", "0.6.1"),
        "llm_enabled": LLM_ENABLED,
        "llm_model": LLM_MODEL if LLM_ENABLED else None,
        "config_source": config_source(),
    }
