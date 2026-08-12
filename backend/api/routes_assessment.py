"""能力测评工作流 API（前台）。

对应原型的三步：**1 简历解析推断 → 2 对话问答测评 → 3 综合能力报告**。
前端步骤条固定三节点，状态与每节点的输出都由 `stages` 字段驱动，
不需要知道 LangGraph 内部有哪些节点。

会话状态存在 LangGraph 的 checkpointer（Postgres）里，刷新页面用
`GET /sessions/{id}` 就能恢复现场——服务端不额外维护「第几题、答了什么」。

题目是**分批懒加载**的：一次只返回当前该答的那道，答完再给下一道；
`question_end=false` 表示后面还会有新题，`true` 表示已收敛、可以看报告了。
流式接口在出题/判分这类耗时几秒到十几秒的步骤上推 `status` 事件。
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.api.auth_temp import TempUser, require_temp_user
from backend.kg.pg_store import biz_store as biz

router = APIRouter(prefix="/v1/student/assessment", tags=["前台 · AI 诊断"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # 关掉 Nginx 缓冲，否则事件会被攒着一起下发，流式失去意义
    "X-Accel-Buffering": "no",
}


class StartBody(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [{"occupation_id": "CN:occupation:MOHRSS:4-12-01-01", "resume_text": "从事汽车维修5年…"}]
        }
    }

    occupation_id: str | None = Field(
        None, description="目标岗位；不传则取该用户已锁定的学习目标"
    )
    resume_text: str | None = Field(
        None, description="简历/自述原文。留空则跳过阶段 1，直接开始测评"
    )


class AnswerBody(BaseModel):
    model_config = {"json_schema_extra": {"examples": [{"answer": 3}, {"answer": "我曾负责…"}]}}

    answer: Any = Field(..., description="选择题传选项 value（int）；问答题传文本")


def _resolve_occupation(user: TempUser, occupation_id: str | None) -> str:
    occ = occupation_id
    if not occ:
        occ = (biz.get_goal(user.user_id) or {}).get("occupation_id")
    if not occ:
        raise HTTPException(400, "缺少目标岗位：请先锁定学习目标或传 occupation_id")
    return occ


def _new_session(user: TempUser, occupation_id: str) -> int:
    """建一条诊断会话，其 id 同时用作工作流 thread_id，报告可复用既有查询。"""
    return biz.create_assessment_session(
        user.user_id, user.user_name, target_occupation_id=occupation_id
    )


def _sse(events: Iterator[dict[str, Any]]) -> StreamingResponse:
    def gen() -> Iterator[str]:
        try:
            for ev in events:
                yield f"event: {ev.get('type','message')}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001 — 流中异常也要让前端收到，不能静默断开
            yield f"event: error\ndata: {json.dumps({'message': str(e)[:300]}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post(
    "/sessions",
    summary="测评 · 开始（解析简历并出首批题）",
    description=(
        "启动工作流：读岗位标准 → 解析简历推断画像 → 出首批题，返回第一道题。\n\n"
        "返回的 `stages` 直接对应前端三节点步骤条；`question` 是当前该答的题；"
        "`question_end=false` 表示题还没出完。"
    ),
)
def start_session(body: StartBody, user: TempUser = Depends(require_temp_user)) -> dict[str, Any]:
    from backend.agent.assessment import service

    occ = _resolve_occupation(user, body.occupation_id)
    sid = _new_session(user, occ)
    return service.start(
        sid,
        user_id=user.user_id,
        user_name=user.user_name,
        occupation_id=occ,
        resume_text=body.resume_text,
    )


@router.post(
    "/sessions/stream",
    summary="测评 · 开始（SSE 流式）",
    description=(
        "同上，但以 SSE 推进度。事件：`status`（正在解析/出题）、`question`（新题）、"
        "`stages`（阶段状态）、`report`、`done`、`error`。\n\n"
        "解析与出题各需数秒到十几秒，流式让前端能显示当前在做什么。"
    ),
)
def start_session_stream(
    body: StartBody, user: TempUser = Depends(require_temp_user)
) -> StreamingResponse:
    from backend.agent.assessment import service

    occ = _resolve_occupation(user, body.occupation_id)
    sid = _new_session(user, occ)

    def events() -> Iterator[dict[str, Any]]:
        yield {"type": "session", "session_id": sid, "occupation_id": occ}
        yield from service.stream_start(
            sid,
            user_id=user.user_id,
            user_name=user.user_name,
            occupation_id=occ,
            resume_text=body.resume_text,
        )

    return _sse(events())


@router.post(
    "/sessions/{session_id}/answer",
    summary="测评 · 提交一题作答",
    description=(
        "选择题传选项 `value`，问答题传文本。返回下一题（若有）与最新阶段状态；"
        "题目出完时 `question_end=true` 且带上 `report`。"
    ),
)
def submit_answer(
    session_id: int, body: AnswerBody, user: TempUser = Depends(require_temp_user)
) -> dict[str, Any]:
    from backend.agent.assessment import service

    _ = user
    try:
        return service.answer(session_id, body.answer)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post(
    "/sessions/{session_id}/answer/stream",
    summary="测评 · 提交作答（SSE 流式）",
    description="判分与下一批出题会用到模型，用流式推 `status`，避免前端长时间空白。",
)
def submit_answer_stream(
    session_id: int, body: AnswerBody, user: TempUser = Depends(require_temp_user)
) -> StreamingResponse:
    from backend.agent.assessment import service

    _ = user
    return _sse(service.stream_answer(session_id, body.answer))


@router.get(
    "/sessions/{session_id}",
    summary="测评 · 当前状态（刷新恢复）",
    description="返回阶段状态与当前待答题目；会话状态由 LangGraph checkpointer 持久化。",
)
def get_session(session_id: int, user: TempUser = Depends(require_temp_user)) -> dict[str, Any]:
    from backend.agent.assessment import service

    _ = user
    st = service.get_state(session_id)
    if not st.get("exists"):
        raise HTTPException(404, "测评会话不存在或已过期")
    return st
