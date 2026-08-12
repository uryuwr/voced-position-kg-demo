"""测评会话服务层：把 LangGraph 的 invoke/Command 包装成 HTTP 能用的三个动作。

    start(...)          启动会话 → 跑到第一个 interrupt，返回首题 + 阶段状态
    answer(...)         提交一题 → Command(resume=...) 续跑，返回下一题或报告
    get_state(...)      查当前状态（刷新页面后恢复现场）

thread_id 即会话 id，状态全在 checkpointer 里，服务层不自己存「第几题、答了啥」。

流式（stream_*）用 LangGraph 自带的 graph.stream()，逐节点产出事件：
出题、判分都是秒级到十几秒的过程，前端据此显示「AI 正在出题 / 正在判分」。
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

from langgraph.types import Command

from backend.agent.assessment.graph import get_graph
from backend.agent.assessment.stages import pending_question, project


def _cfg(session_id: str | int) -> dict[str, Any]:
    return {"configurable": {"thread_id": f"assess:{session_id}"}}


def _interrupt_value(result: dict[str, Any]) -> Any:
    itr = result.get("__interrupt__") if isinstance(result, dict) else None
    if not itr:
        return None
    first = itr[0]
    return getattr(first, "value", None) or (first if isinstance(first, dict) else None)


def _snapshot(session_id: str | int) -> dict[str, Any]:
    """从 checkpointer 读当前状态（不推进图）。"""
    snap = get_graph().get_state(_cfg(session_id))
    values = dict(snap.values or {})
    # LangGraph 1.x：挂起中的中断挂在 snapshot.interrupts 上
    itrs = getattr(snap, "interrupts", None) or []
    ival = getattr(itrs[0], "value", None) if itrs else None
    return {"values": values, "interrupt": ival, "next": list(snap.next or ())}


def _view(
    session_id: str | int, values: dict[str, Any], interrupt_value: Any
) -> dict[str, Any]:
    view = project(values, interrupted=bool(interrupt_value))
    view["session_id"] = session_id
    q = pending_question(values, interrupt_value) if interrupt_value else None
    view["question"] = q
    report = values.get("report") or None
    view["report"] = report
    if report:
        _persist_report(session_id, values, report)
    return view


def _persist_report(session_id: str | int, values: dict[str, Any], report: dict[str, Any]) -> None:
    """报告落库（幂等）。工作流状态在 checkpointer 里，但下游（学习计划、
    历史报告、岗位匹配度）读的是 biz_* 业务表，所以收敛后要同步过去。"""
    try:
        sid = int(session_id)
    except (TypeError, ValueError):
        return                                  # 自测用的字符串 thread_id，跳过
    if _PERSISTED.get(sid):
        return
    try:
        from backend.kg.pg_store import biz_store as biz

        biz.save_assessment_report(sid, str(values.get("user_id") or ""), report)
        _PERSISTED[sid] = True
    except Exception:                            # noqa: BLE001 — 落库失败不影响本次结果返回
        pass


_PERSISTED: dict[int, bool] = {}


def start(
    session_id: str | int,
    *,
    user_id: str,
    user_name: str,
    occupation_id: str | None,
    resume_text: str | None = None,
) -> dict[str, Any]:
    t = time.time()
    res = get_graph().invoke(
        {
            "session_id": session_id,
            "user_id": user_id,
            "user_name": user_name,
            "occupation_id": occupation_id,
            "resume_text": resume_text,
        },
        _cfg(session_id),
    )
    view = _view(session_id, res, _interrupt_value(res))
    view["elapsed_ms"] = int((time.time() - t) * 1000)
    return view


def answer(session_id: str | int, value: Any) -> dict[str, Any]:
    t = time.time()
    res = get_graph().invoke(Command(resume=value), _cfg(session_id))
    view = _view(session_id, res, _interrupt_value(res))
    view["elapsed_ms"] = int((time.time() - t) * 1000)
    return view


def get_state(session_id: str | int) -> dict[str, Any]:
    snap = _snapshot(session_id)
    if not snap["values"]:
        return {"session_id": session_id, "exists": False}
    view = _view(session_id, snap["values"], snap["interrupt"])
    view["exists"] = True
    return view


# ── 流式 ─────────────────────────────────────────────────────

_NODE_HINT = {
    "load_target": ("assess", "正在读取岗位标准与技能构成…"),
    "parse_profile": ("parse", "AI 正在解析简历、推断能力档位…"),
    "plan_batch": ("assess", "AI 正在按岗位标准出题…"),
    "grade": ("assess", "正在判分…"),
    "aggregate": ("report", "正在汇总实测结果…"),
    "build_report": ("report", "正在生成综合能力报告…"),
}


def _stream(session_id: str | int, payload: Any) -> Iterator[dict[str, Any]]:
    graph = get_graph()
    cfg = _cfg(session_id)
    last: dict[str, Any] = {}
    for mode, chunk in graph.stream(payload, cfg, stream_mode=["updates", "values"]):
        if mode == "updates":
            for node in chunk or {}:
                if node == "__interrupt__":
                    continue
                stage, msg = _NODE_HINT.get(node, ("assess", ""))
                if msg:
                    yield {"type": "status", "stage": stage, "node": node, "message": msg}
        elif mode == "values":
            last = chunk or {}
    snap = _snapshot(session_id)
    values = snap["values"] or last
    view = _view(session_id, values, snap["interrupt"])
    if view.get("question"):
        yield {"type": "question", "question": view["question"], "progress": view["progress"]}
    yield {"type": "stages", "stages": view["stages"], "current_stage": view["current_stage"]}
    if view.get("report"):
        yield {"type": "report", "report": view["report"]}
    yield {
        "type": "done",
        "question_end": view["question_end"],
        "awaiting_answer": view["awaiting_answer"],
        "session_id": session_id,
    }


def stream_start(
    session_id: str | int,
    *,
    user_id: str,
    user_name: str,
    occupation_id: str | None,
    resume_text: str | None = None,
) -> Iterator[dict[str, Any]]:
    yield from _stream(
        session_id,
        {
            "session_id": session_id,
            "user_id": user_id,
            "user_name": user_name,
            "occupation_id": occupation_id,
            "resume_text": resume_text,
        },
    )


def stream_answer(session_id: str | int, value: Any) -> Iterator[dict[str, Any]]:
    yield from _stream(session_id, Command(resume=value))
