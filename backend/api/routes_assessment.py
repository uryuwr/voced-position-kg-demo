"""能力测评 API：**出题 / 答题 / 结算** 三段。

对应原型三步：1 简历解析推断 → 2 对话问答测评 → 3 综合能力报告。
前端步骤条固定三节点，状态由 `stage` 事件与 `GET /sessions/{id}` 的 `stages` 驱动。

    ① POST /sessions/questions/stream   一条 SSE 长连接推完**全部题目**
                                        stage → plan(总题数) → question×N → question_end
    ② POST /sessions/{id}/answers       提交一题，即答即走
                                        选择题当场判分；问答题后台判，不阻塞下一题
    ③ POST /sessions/{id}/report/stream 等判分收尾 → 综合能力报告

为什么出题和答题分开：题目不依赖作答（整场一次排定），把它们绑在同一个请求里
会让「出题被答题节奏拖住」——学员每答一题都要等服务端现出下一题。
前端拿到长连接推来的题后放进本地队列，答题零等待。
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import StreamingResponse
from backend.api.auth_temp import TempUser, require_temp_user
from backend.api.schemas_assessment import (
    AnswerAcceptedOut,
    AnswerBody,
    AssessmentStateOut,
    SseErrorEvent,
    SsePlanEvent,
    SseQuestionEndEvent,
    SseQuestionEvent,
    SseReportEvent,
    SseSessionEvent,
    SseStageEvent,
    StartBody,
)
from backend.kg.pg_store import biz_store as biz

router = APIRouter(prefix="/v1/student/assessment", tags=["前台 · AI 诊断"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # 关掉 Nginx 缓冲，否则事件会被攒着一起下发，流式失去意义
    "X-Accel-Buffering": "no",
}


def _resolve_occupation(user: TempUser, occupation_id: str | None) -> str:
    occ = occupation_id or (biz.get_goal(user.user_id) or {}).get("occupation_id")
    if not occ:
        raise HTTPException(400, "缺少目标岗位：请先锁定学习目标或传 occupation_id")
    return occ


def _own_session(session_id: int, user: TempUser) -> dict[str, Any]:
    """校验会话归属并返回会话元信息。

    不存在与不属于都回 404（而不是 403）：403 会把「这个 id 确实存在」这条信息
    透给试探者，自增 id 下等于给出可枚举的会话清单。
    """
    meta = biz.session_meta(session_id)
    if not meta or meta["user_id"] != str(user.user_id):
        raise HTTPException(404, "测评会话不存在")
    return meta


def _sse(events: Iterator[dict[str, Any]]) -> StreamingResponse:
    def gen() -> Iterator[str]:
        try:
            for ev in events:
                yield f"event: {ev.get('type','message')}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001 — 流中异常也要让前端收到，不能静默断开
            yield f"event: error\ndata: {json.dumps({'message': str(e)[:300]}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post(
    "/sessions/questions/stream",
    summary="① 出题 · 一条长连接推完全部题目（SSE）",
    description=(
        "建会话 → 解析简历 → 规划题数 → 生成并**陆续推送全部题目**。\n\n"
        "事件顺序：`session` → `stage`(解析) → `plan`(含确定题数) → "
        "`stage`(出题) → `question` × N → `question_end`。\n\n"
        "前端把 `question` 事件压进本地队列，一次展示一道；收到 `question_end` "
        "即表示题目出完（它是服务端的确定信号，比按题数判断可靠——"
        "模型出题失败降级时实际条数可能少于计划）。"
    ),
    response_class=StreamingResponse,
    responses={
        200: {
            "description": (
                "`text/event-stream`。每个事件的 `data` 是下列之一，按 `type` 区分：\n\n"
                "- `session` —— SseSessionEvent\n"
                "- `stage` —— SseStageEvent\n"
                "- `plan` —— SsePlanEvent\n"
                "- `question` —— SseQuestionEvent\n"
                "- `question_end` —— SseQuestionEndEvent\n"
                "- `error` —— SseErrorEvent"
            ),
            "content": {
                "text/event-stream": {
                    "schema": {
                        "title": "出题流事件",
                        "oneOf": [
                            SseSessionEvent.model_json_schema(ref_template="#/components/schemas/{model}"),
                            SseStageEvent.model_json_schema(ref_template="#/components/schemas/{model}"),
                            SsePlanEvent.model_json_schema(ref_template="#/components/schemas/{model}"),
                            SseQuestionEvent.model_json_schema(ref_template="#/components/schemas/{model}"),
                            SseQuestionEndEvent.model_json_schema(ref_template="#/components/schemas/{model}"),
                            SseErrorEvent.model_json_schema(ref_template="#/components/schemas/{model}"),
                        ],
                    }
                }
            },
        }
    },
)
def stream_questions(
    body: StartBody, user: TempUser = Depends(require_temp_user)
) -> StreamingResponse:
    from backend.agent.assessment import service

    occ = _resolve_occupation(user, body.occupation_id)
    sid = biz.create_assessment_session(
        user.user_id, user.user_name, target_occupation_id=occ
    )

    def events() -> Iterator[dict[str, Any]]:
        yield {"type": "session", "session_id": sid, "occupation_id": occ}
        yield from service.stream_questions(
            sid, occupation_id=occ, resume_text=body.resume_text
        )

    return _sse(events())


@router.post(
    "/sessions/{session_id}/answers",
    summary="② 答题 · 提交一题（即答即走）",
    description=(
        "选择题**当场判分**（选项自带档位，纯查表）；问答题落库后交后台线程判分，"
        "接口立即返回——学员不该为了等模型判分卡在这一题上。\n\n"
        "返回 `progress.grading` 表示还有几道在后台判分；结算接口会等它们收尾。"
    ),
    response_model=AnswerAcceptedOut,
)
def submit_answer(
    session_id: int = Path(..., ge=1, description="测评会话 id"),
    body: AnswerBody = ...,
    user: TempUser = Depends(require_temp_user),
) -> AnswerAcceptedOut:
    from backend.agent.assessment import service

    _own_session(session_id, user)
    try:
        return AnswerAcceptedOut.model_validate(
            service.submit_answer(session_id, body.index, body.answer)
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post(
    "/sessions/{session_id}/report/stream",
    summary="③ 结算 · 生成综合能力报告（SSE）",
    description=(
        "等待后台判分收尾（推 `stage` 进度），再聚合实测结果与岗位标准，"
        "产出匹配度 / 双系列雷达 / 优势 / 短板，并落库到诊断报告。"
    ),
    response_class=StreamingResponse,
    responses={
        200: {
            "description": (
                "`text/event-stream`。事件按 `type` 区分：`stage`（判分进度）→ "
                "`report`（完整报告）；异常走 `error`。"
            ),
            "content": {
                "text/event-stream": {
                    "schema": {
                        "title": "结算流事件",
                        "oneOf": [
                            SseStageEvent.model_json_schema(ref_template="#/components/schemas/{model}"),
                            SseReportEvent.model_json_schema(ref_template="#/components/schemas/{model}"),
                            SseErrorEvent.model_json_schema(ref_template="#/components/schemas/{model}"),
                        ],
                    }
                }
            },
        }
    },
)
def stream_report(
    session_id: int = Path(..., ge=1, description="测评会话 id"),
    occupation_id: str | None = Query(
        None, description="兜底目标岗位；会话已落库岗位时以库为准，此参数忽略"
    ),
    user: TempUser = Depends(require_temp_user),
) -> StreamingResponse:
    from backend.agent.assessment import service

    meta = _own_session(session_id, user)
    # 目标岗位以会话落库的为准：结算必须拿**出题时那个岗位**的标准去打分，
    # 否则传个别的 occupation_id 就能算出一份自洽但错的报告。
    occ = meta["target_occupation_id"] or _resolve_occupation(user, occupation_id)
    return _sse(
        service.stream_report(session_id, user_id=user.user_id, occupation_id=occ)
    )


@router.get(
    "/sessions/{session_id}",
    summary="测评 · 当前状态（刷新恢复）",
    description=(
        "三阶段状态 + 全部题目 + 已作答 + 下一道未答题。"
        "状态来自业务表（biz_assessment_question / biz_assessment_answer），"
        "刷新恢复只是两条普通查询。"
    ),
    response_model=AssessmentStateOut,
)
def get_session(
    session_id: int = Path(..., ge=1, description="测评会话 id"),
    occupation_id: str | None = Query(
        None, description="兜底目标岗位；会话已落库岗位时以库为准"
    ),
    user: TempUser = Depends(require_temp_user),
) -> AssessmentStateOut:
    from backend.agent.assessment import service

    meta = _own_session(session_id, user)
    st = service.get_state(
        session_id, occupation_id=meta["target_occupation_id"] or occupation_id
    )
    if not st.get("exists"):
        raise HTTPException(404, "测评会话不存在或尚未出题")
    return AssessmentStateOut.model_validate(st)
