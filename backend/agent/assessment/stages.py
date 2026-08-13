"""把工作流内部状态投影成前端固定的三个阶段。

前端步骤条是写死的三节点（原型）：
    1. 简历解析推断   2. 对话问答测评   3. 综合能力报告

而图内部有 7 个节点（load_target / parse_profile / plan_batch / ask / grade /
aggregate / build_report）。前端不该知道这些，也不该在图改结构时跟着改，
所以这里做一层投影：图状态 → 三阶段的 status + output。

status 取值：pending（灰）| active（高亮）| done（打勾）

题目「出完没有」用 `question_end` 表达：前端据此决定是继续等新题追加到队列，
还是收卷去看报告。这与懒加载出题相配——题是分批产出的，前端拿到的从来不是全量。
"""
from __future__ import annotations

from typing import Any

STAGE_PARSE = "parse"
STAGE_ASSESS = "assess"
STAGE_REPORT = "report"

STAGE_NAMES = {
    STAGE_PARSE: "简历解析推断",
    STAGE_ASSESS: "对话问答测评",
    STAGE_REPORT: "综合能力报告",
}


def _stage(key: str, status: str, output: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"key": key, "name": STAGE_NAMES[key], "status": status, "output": output or {}}


def project(state: dict[str, Any], *, interrupted: bool = False) -> dict[str, Any]:
    """图状态 → 前端可直接渲染的阶段视图。"""
    paper = state.get("paper") or []
    graded = state.get("graded") or []
    cursor = int(state.get("cursor") or 0)
    report = state.get("report") or {}
    profile = state.get("profile_levels") or {}
    profile_meta = state.get("profile_meta") or {}
    occ = state.get("occupation") or {}

    has_profile = bool(profile_meta) and profile_meta.get("engine") != "skip"
    started_assess = bool(paper)
    finished = bool(report)

    # 阶段 1：简历解析
    if not has_profile and not started_assess:
        s1 = _stage(STAGE_PARSE, "active")
    else:
        s1 = _stage(
            STAGE_PARSE,
            "done",
            {
                "engine": profile_meta.get("engine"),
                "skill_count": len(profile),
                # 前端展示「解析出这些技能与推断档位」
                "skills": [
                    {"skill_key": k, "level": v}
                    for k, v in sorted(profile.items(), key=lambda kv: -kv[1])
                ],
                "note": profile_meta.get("error"),
            },
        )

    # 阶段 2：对话问答测评
    answered = len([g for g in graded if not g.get("invalid")])
    if not started_assess:
        s2 = _stage(STAGE_ASSESS, "pending")
    elif finished:
        s2 = _stage(
            STAGE_ASSESS,
            "done",
            {"asked": len(paper), "answered": answered, "batches": state.get("batches")},
        )
    else:
        s2 = _stage(
            STAGE_ASSESS,
            "active",
            {
                "asked": len(paper),
                "answered": answered,
                "cursor": cursor,
                "batches": state.get("batches"),
            },
        )

    # 阶段 3：综合能力报告
    s3 = _stage(STAGE_REPORT, "done" if finished else "pending", report if finished else {})

    current = STAGE_REPORT if finished else (STAGE_ASSESS if started_assess else STAGE_PARSE)
    return {
        "occupation": occ,
        "stages": [s1, s2, s3],
        "current_stage": current,
        # 题目是否已出完：还在测评中且未收敛就代表后面还会有新题
        "question_end": bool(finished or state.get("exhausted")),
        "progress": {
            "asked": len(paper),
            "answered": answered,
            "cursor": cursor,
            # 懒加载下真实总题数事先未知，给个预估值，避免前端显示「1 / ?」
            "planned_total": int(state.get("planned_total") or 0) or None,
            "stop_reason": state.get("stop_reason") or "",
        },
        "awaiting_answer": bool(interrupted),
    }


def pending_question(state: dict[str, Any], interrupt_value: Any = None) -> dict[str, Any] | None:
    """当前待答题目。优先用 interrupt 携带的值，其次按 cursor 从卷子里取。"""
    if isinstance(interrupt_value, dict) and interrupt_value.get("question"):
        return interrupt_value["question"]
    paper = state.get("paper") or []
    cursor = int(state.get("cursor") or 0)
    return paper[cursor] if 0 <= cursor < len(paper) else None


def with_progress(q: dict[str, Any] | None, state: dict[str, Any]) -> dict[str, Any] | None:
    """给题目补上进度信息（分批出题时 total 只能是预估）。"""
    if not q:
        return q
    out = dict(q)
    out["planned_total"] = int(state.get("planned_total") or 0) or None
    out.pop("total", None)          # 旧字段在懒加载下没有意义，避免前端显示 1/?
    return out


def project_from_store(
    session_id: int, *, occupation_id: str | None = None
) -> dict[str, Any]:
    """从业务表投影出三阶段状态（替代原先从 checkpointer 读）。

    刷新恢复因此变成两条普通查询，不再依赖图的存档。
    """
    from backend.agent.assessment import service, store

    qs = store.list_questions(session_id)
    answers = {a["index"]: a for a in store.list_answers(session_id)}
    p = store.progress(session_id)
    ctx = service._ctx(session_id)
    profile = ctx.get("profile_levels") or {}
    profile_meta = ctx.get("profile_meta") or {}

    # 直接按 session_id 查（它本身唯一），不要求调用方传 user_id
    report = None
    try:
        from backend.kg.pg_store.client import connect

        with connect() as conn:
            r = conn.execute(
                "SELECT report_json FROM biz_diagnosis_result WHERE session_id=%s",
                (session_id,),
            ).fetchone()
        if r:
            rep = r["report_json"]
            if isinstance(rep, str):
                import json as _json

                rep = _json.loads(rep)
            report = rep or None
    except Exception:  # noqa: BLE001
        report = None

    started = bool(qs)
    finished = bool(report)
    s1 = _stage(
        STAGE_PARSE,
        "done" if started or profile_meta else "active",
        {
            "engine": profile_meta.get("engine"),
            "skill_count": len(profile),
            "skills": [{"skill_key": k, "level": v} for k, v in profile.items()],
        },
    )
    s2 = _stage(
        STAGE_ASSESS,
        "done" if finished else ("active" if started else "pending"),
        {"asked": p["asked"], "answered": p["answered"], "grading": p["grading"]},
    )
    s3 = _stage(STAGE_REPORT, "done" if finished else "pending", report or {})

    pending_q = store.next_unanswered(session_id)
    return {
        "session_id": session_id,
        "exists": started,
        "occupation_id": occupation_id or (ctx.get("occupation") or {}).get("id"),
        "stages": [s1, s2, s3],
        "current_stage": STAGE_REPORT if finished else (STAGE_ASSESS if started else STAGE_PARSE),
        "questions": qs,
        "answers": list(answers.values()),
        "question": pending_q,
        "progress": {**p, "target_total": ctx.get("target_total")},
        # 出题是一次性长连接：库里有题就说明那一轮已经推完（进程重启后内存里的
        # target_total 会丢，不能拿它做判断，否则刷新恢复时会一直显示「还在出题」）
        "question_end": bool(started),
        "report": report,
    }
