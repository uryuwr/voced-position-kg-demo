"""学员端 Agent：LangGraph + AI 网关（对齐 bcs-ai-agent）。

LLM 节点优先 `langgraph.prebuilt.create_react_agent`；逻辑过重再拆自定义 StateGraph。
"""
from __future__ import annotations

from backend.agent.diagnose import run_chat_diagnose, run_resume_diagnose

__all__ = ["run_resume_diagnose", "run_chat_diagnose"]
