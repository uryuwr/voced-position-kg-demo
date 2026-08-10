"""
AI 网关客户端（对齐 bcs-ai-agent）。

- 协议：OpenAI Chat Completions 兼容
- 环境变量：LLM_BASE_URL、GITHUB_TOKEN（或 LLM_API_KEY）、LLM_MODEL
- base_url 须以 /v1 结尾；Authorization 使用 token 原值（与 bcs llm_stream 一致）
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from backend import settings


def llm_ready() -> bool:
    return settings.llm_configured()


@lru_cache(maxsize=1)
def get_chat_model() -> Any:
    """LangChain ChatOpenAI，指向 AI 网关。未配置则抛错。"""
    if not llm_ready():
        raise RuntimeError(
            "AI 网关未配置：请在 .env 设置 LLM_BASE_URL、GITHUB_TOKEN（或 LLM_API_KEY）、LLM_MODEL"
        )
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as e:
        raise RuntimeError(
            "缺少 langchain-openai：pip install langchain-openai langgraph langchain-core"
        ) from e

    # bcs-ai-agent: default_headers Authorization = api_key 原值
    return ChatOpenAI(
        model=settings.LLM_MODEL,
        api_key=settings.GITHUB_TOKEN,
        base_url=settings.LLM_BASE_URL,
        temperature=settings.LLM_TEMPERATURE,
        max_tokens=settings.LLM_MAX_OUTPUT_TOKENS,
        timeout=settings.LLM_REQUEST_TIMEOUT,
        default_headers={"Authorization": settings.GITHUB_TOKEN},
    )


def gateway_info() -> dict[str, Any]:
    return {
        "enabled": llm_ready(),
        "base_url": settings.LLM_BASE_URL or None,
        "model": settings.LLM_MODEL or None,
        "has_token": bool(settings.GITHUB_TOKEN),
    }
