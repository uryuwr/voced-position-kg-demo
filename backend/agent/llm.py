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


@lru_cache(maxsize=4)
def get_chat_model(*, fast: bool = False, max_tokens: int | None = None) -> Any:
    """LangChain ChatOpenAI，指向 AI 网关。未配置则抛错。

    fast=True 关闭模型的深度思考（doubao-seed 等推理模型默认开启）。
    实测差别极大：同样出 3 道情景判断题，开思考 30s+ 且常把 max_tokens 耗在
    思考过程上导致返回空内容，关掉后 **8.6s** 且题目质量无差。
    出题、判分这类「按模板产出结构化结果」的任务一律用 fast。

    非 doubao 网关不认 thinking 字段，故调用方遇到 400 应退回 fast=False 重试
    （见 _invoke_fast）。
    """
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

    kwargs: dict[str, Any] = {}
    if fast:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    # bcs-ai-agent: default_headers Authorization = api_key 原值
    return ChatOpenAI(
        model=settings.LLM_MODEL,
        api_key=settings.GITHUB_TOKEN,
        base_url=settings.LLM_BASE_URL,
        temperature=settings.LLM_TEMPERATURE,
        max_tokens=max_tokens or settings.LLM_MAX_OUTPUT_TOKENS,
        timeout=settings.LLM_REQUEST_TIMEOUT,
        default_headers={"Authorization": settings.GITHUB_TOKEN},
        **kwargs,
    )


def invoke_fast(messages: list[tuple[str, str]], *, max_tokens: int | None = None) -> str:
    """关思考调用一次，返回文本。网关不支持该字段时自动退回普通调用。"""
    try:
        resp = get_chat_model(fast=True, max_tokens=max_tokens).invoke(messages)
    except Exception as e:  # noqa: BLE001
        if "thinking" not in str(e).lower():
            raise
        resp = get_chat_model(fast=False, max_tokens=max_tokens).invoke(messages)
    content = getattr(resp, "content", "") or ""
    return content if isinstance(content, str) else str(content)


def gateway_info() -> dict[str, Any]:
    return {
        "enabled": llm_ready(),
        "base_url": settings.LLM_BASE_URL or None,
        "model": settings.LLM_MODEL or None,
        "has_token": bool(settings.GITHUB_TOKEN),
    }
