"""
集中读取环境变量。

独立部署约定：
  - 配置真源在 **backend 包内** `backend/.env`（与 `settings.py` 同级）
  - 模板：`backend/.env.example`
  - Docker/K8s 可用环境变量注入，不必打包 .env

加载顺序（后者覆盖前者）：
  1. 进程环境变量（已有）
  2. 可选：仓库根 `.env`（仅 monorepo 本地兼容，不覆盖已有键）
  3. `backend/.env`（override=True，独立部署以它为准）
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# backend/ 目录（本文件所在包根；Docker 内为 /app/backend）
BACKEND_DIR = Path(__file__).resolve().parent
# monorepo 仓库根（backend 的上一级）；独立只拷 backend 时可能不存在业务文件
REPO_ROOT = BACKEND_DIR.parent


def _load_env_files() -> None:
    # monorepo 本地：根 .env 作兜底（不抢占已 export 的变量）
    repo_env = REPO_ROOT / ".env"
    if repo_env.is_file() and REPO_ROOT != BACKEND_DIR:
        load_dotenv(repo_env, override=False)
    # 后端包内配置优先
    backend_env = BACKEND_DIR / ".env"
    if backend_env.is_file():
        load_dotenv(backend_env, override=True)


_load_env_files()


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
# 用户画像服务（五维记忆），走 BTS 鉴权；留空则匹配度只用测评画像
OPENQ_AI_MANAGER = (
    os.getenv("OPENQ_AI_MANAGER", "") or os.getenv("openq-ai-manager", "")
).rstrip("/")


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
    }
