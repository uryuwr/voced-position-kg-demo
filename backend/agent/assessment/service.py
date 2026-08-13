"""测评服务层：**出题 / 答题 / 结算** 三段流水线。

为什么不再用 interrupt
----------------------
LangGraph 的 `interrupt()` 面向「短暂的人工确认」，而一场测评是持续几分钟、
十几次往返的交互。用它建模的代价是：每答一题都要唤醒整张图、读写一次 checkpoint，
更要命的是**出题被答题节奏绑死**——题目早已不依赖作答，却只能等 resume 才能继续出。

三段的时间特性本就不同，拆开各自最优：

    ① 出题   一次长连接推完（题目不依赖作答，可并行生成）
    ② 答题   无状态提交，即答即走；选择题当场判（纯查表），问答题后台判
    ③ 结算   全部答完后一次算出报告

状态落在 biz_assessment_question / biz_assessment_answer 两张业务表（见 store.py），
不再放 checkpointer——那些数据要长期保存、要能出统计，blob 查不了。
LangGraph 仍用在**无人参与的编排**上（出题流水线，见 pipeline.py）。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from typing import Any

from backend.agent.assessment import store
from backend.agent.assessment.grading import grade_choice, grade_open, merge_measured
from backend.agent.assessment.report import build_report

# 会话级上下文（岗位、技能构成、简历画像）：结算时还要用，避免重复查库。
# 有上限并按插入序淘汰：这是**缓存**不是存储，没有任何一处会删它，
# 不封顶就是一条随在线时长单调增长的内存泄漏（每场测评几十 KB 的技能构成）。
# 被淘汰的会话不会出错——load_context 查不到就回库里重建。
_CTX: OrderedDict[int, dict[str, Any]] = OrderedDict()
_CTX_MAX = 500
_CTX_LOCK = threading.Lock()


def _ctx(session_id: int) -> dict[str, Any]:
    with _CTX_LOCK:
        return dict(_CTX.get(session_id) or {})


def _set_ctx(session_id: int, **kw: Any) -> None:
    with _CTX_LOCK:
        _CTX.setdefault(session_id, {}).update(kw)
        _CTX.move_to_end(session_id)
        while len(_CTX) > _CTX_MAX:
            _CTX.popitem(last=False)


def load_context(session_id: int, occupation_id: str) -> dict[str, Any]:
    """岗位职责 + 技能构成 —— 出题与结算共同的标准来源。"""
    cached = _ctx(session_id)
    if cached.get("required_items") is not None:
        return cached
    from backend.kg.pg_store.query import get_node
    from backend.kg.pg_store.skill_composition import get_composition

    occ = get_node(occupation_id) or {}
    comp = get_composition(occupation_id)
    items = [
        {
            "skill_key": i.get("skill_key"),
            "category": i.get("category"),
            "required_level": i.get("selected_level"),
            "weight": i.get("weight"),
            "available_levels": i.get("available_levels"),
            "levels": i.get("levels"),
        }
        for i in (comp.get("items") or [])
    ]
    occupation = {
        "id": occ.get("id"),
        "name": occ.get("name"),
        "description": occ.get("description"),
        "level": occ.get("level"),
    }
    _set_ctx(session_id, occupation=occupation, required_items=items)
    return {"occupation": occupation, "required_items": items}


# ── ① 出题段：一条长连接推完 ─────────────────────────────────


def stream_questions(
    session_id: int,
    *,
    occupation_id: str,
    resume_text: str | None = None,
) -> Iterator[dict[str, Any]]:
    """生成并推送整场题目，直到 question_end。

    题目边生成边推：首批到了就能开始答，其余在学员答题期间陆续到位。
    """
    from backend.agent.assessment.bank import BATCH_SIZE, generate_batch
    from backend.agent.assessment.pipeline import plan_questions

    t0 = time.time()
    yield {"type": "stage", "stage": "parse", "status": "active",
           "message": "正在读取岗位标准与技能构成…"}
    ctx = load_context(session_id, occupation_id)
    items = ctx["required_items"]
    if not items:
        # 全库仅约 37% 的岗位配了技能构成，这条分支很常见：要把阶段状态推完整，
        # 否则前端步骤条会停在「解析中」，再配上 question_end 就显示成「已答完」
        yield {"type": "stage", "stage": "parse", "status": "done",
               "output": {"engine": "skip", "skill_count": 0, "skills": []}}
        yield {
            "type": "error",
            "code": "no_skill_composition",
            "message": f"「{ctx['occupation'].get('name') or '该岗位'}」尚未配置技能构成，"
                       "无法出题。请先在管理台为该岗位配置技能，或换一个岗位测评。",
        }
        yield {"type": "question_end", "total": 0, "planned": 0}
        return

    # 简历解析（可选）
    if (resume_text or "").strip():
        yield {"type": "stage", "stage": "parse", "status": "active",
               "message": "AI 正在解析简历、推断能力档位…"}
        from backend.agent.assessment.profile import build_profile

        try:
            profile, meta = build_profile(
                resume_text or "", occupation_name=ctx["occupation"].get("name")
            )
        except Exception as e:  # noqa: BLE001 — 解析失败不该挡住测评
            profile, meta = {}, {"engine": "error", "error": str(e)[:200]}
    else:
        profile, meta = {}, {"engine": "skip"}
    _set_ctx(session_id, profile_levels=profile, profile_meta=meta)
    yield {
        "type": "stage", "stage": "parse", "status": "done",
        "output": {
            "engine": meta.get("engine"),
            "skill_count": len(profile),
            "skills": [{"skill_key": k, "level": v} for k, v in profile.items()],
        },
    }

    # 规划：整场题目一次排定（不看作答）
    plan, est = plan_questions(items)
    total = int(est.get("total") or len(plan))
    _set_ctx(session_id, target_total=total, plan_reason=est.get("reason"))
    yield {"type": "plan", "total": total, "reason": est.get("reason"),
           "message": f"本次测评共 {total} 题"}
    yield {"type": "stage", "stage": "assess", "status": "active",
           "message": "AI 正在按岗位标准出题…"}

    made = 0
    for i in range(0, len(plan), BATCH_SIZE):
        chunk = plan[i : i + BATCH_SIZE]
        try:
            qs, _meta = generate_batch(chunk, occupation=ctx["occupation"])
        except Exception as e:  # noqa: BLE001 — 单批失败不影响已出的题
            yield {"type": "warn", "message": f"部分题目生成失败：{str(e)[:120]}"}
            continue
        if not qs:
            continue
        store.save_questions(session_id, qs, made)
        for j, q in enumerate(qs):
            yield {"type": "question", "question": {**q, "index": made + j, "total": total}}
        made += len(qs)

    yield {"type": "question_end", "total": made, "planned": total,
           "elapsed_ms": int((time.time() - t0) * 1000)}


# ── ② 答题段：提交即返回，问答题后台判 ───────────────────────

_GRADING: dict[str, bool] = {}


def submit_answer(session_id: int, idx: int, answer: Any) -> dict[str, Any]:
    """记录作答。选择题当场判分（纯查表 0ms），问答题入后台线程，不阻塞答题。"""
    q = store.get_question(session_id, idx)
    if not q:
        raise ValueError(f"题目不存在：#{idx}")

    if q.get("type") == "choice":
        g = grade_choice(q, answer)
        store.save_answer(session_id, idx, answer, grade=g, status="graded")
        return {"accepted": True, "graded": True, "index": idx,
                "level": g.get("level"), "progress": store.progress(session_id)}

    # 问答题判分要几秒，学员不该为此卡在这一题上
    store.save_answer(session_id, idx, answer, status="pending")
    _grade_open_async(session_id, idx, q, str(answer or ""))
    return {"accepted": True, "graded": False, "index": idx,
            "progress": store.progress(session_id)}


def _grade_open_async(session_id: int, idx: int, q: dict[str, Any], answer: str) -> None:
    key = f"{session_id}:{idx}"
    if _GRADING.get(key):
        return
    _GRADING[key] = True

    def _run() -> None:
        try:
            # 同技能选择题的档位作为上限基准：问答题只收敛不抬升
            self_lv = None
            for a in store.list_answers(session_id):
                aq = store.get_question(session_id, a["index"])
                if aq and aq.get("skill_key") == q.get("skill_key") and aq.get("type") == "choice":
                    self_lv = a.get("level")
                    break
            store.update_grade(session_id, idx, grade_open(q, answer, self_level=self_lv))
        except Exception as e:  # noqa: BLE001
            store.update_grade(session_id, idx, {"level": None, "error": str(e)[:200]}, "failed")
        finally:
            _GRADING.pop(key, None)

    threading.Thread(target=_run, name=f"aw-grade-{key}", daemon=True).start()


# ── ③ 结算段：等判分收尾 → 报告 ──────────────────────────────


def stream_report(
    session_id: int, *, user_id: str, occupation_id: str, timeout_s: int = 60
) -> Iterator[dict[str, Any]]:
    load_context(session_id, occupation_id)
    yield {"type": "stage", "stage": "assess", "status": "done"}

    waited = 0.0
    while waited < timeout_s:
        p = store.progress(session_id)
        if not p["grading"]:
            break
        yield {"type": "stage", "stage": "report", "status": "active",
               "message": f"正在等待 {p['grading']} 道问答题判分…"}
        time.sleep(1.5)
        waited += 1.5

    yield {"type": "stage", "stage": "report", "status": "active",
           "message": "正在生成综合能力报告…"}
    report = build_session_report(session_id, user_id=user_id, occupation_id=occupation_id)
    yield {"type": "report", "report": report}
    yield {"type": "stage", "stage": "report", "status": "done"}
    yield {"type": "done", "session_id": session_id, "match_score": report.get("match_score")}


def build_session_report(session_id: int, *, user_id: str, occupation_id: str) -> dict[str, Any]:
    ctx = load_context(session_id, occupation_id)
    graded: list[dict[str, Any]] = []
    for a in store.list_answers(session_id):
        q = store.get_question(session_id, a["index"])
        if not q or not a.get("level"):
            continue
        graded.append({
            **a,
            "type": q.get("type"),
            "skill_key": q.get("skill_key"),
            "category": q.get("category"),
            "required_level": q.get("required_level"),
            "weight": q.get("weight"),
        })
    measured = merge_measured(graded, _ctx(session_id).get("profile_levels") or {})
    report = build_report(
        occupation=ctx["occupation"],
        required_items=ctx["required_items"],
        measured=measured,
        channel="assessment",
    )
    report["profile_meta"] = _ctx(session_id).get("profile_meta") or {}
    report["plan_reason"] = _ctx(session_id).get("plan_reason")
    try:
        from backend.kg.pg_store import biz_store as biz

        biz.save_assessment_report(session_id, user_id, report)
    except Exception:  # noqa: BLE001 — 落库失败不影响本次返回
        pass

    # 同步到五维记忆：让**其他没测过的岗位**也能用上这次测出的能力证据
    # （同一岗位直接读报告分，用不着记忆）。后台提交，画像服务慢/挂都不影响本次返回。
    try:
        from backend.userprofile import invalidate, push_diagnosis_async

        invalidate(str(user_id))          # 画像变了，缓存作废
        push_diagnosis_async(
            user_id, session_id, report,
            occupation_name=(ctx["occupation"] or {}).get("name") or "目标岗位",
            when=report.get("created_at"),
        )
    except Exception:  # noqa: BLE001
        pass
    return report


# ── 状态查询（刷新恢复） ─────────────────────────────────────


def get_state(session_id: int, *, occupation_id: str | None = None) -> dict[str, Any]:
    from backend.agent.assessment.stages import project_from_store

    return project_from_store(session_id, occupation_id=occupation_id)
