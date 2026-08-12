"""能力测评工作流（LangGraph StateGraph）。

    START → load_target → parse_profile → plan_batch → ask ⇄ grade → aggregate → build_report → END
                                              ↑___________|      |
                                          答完一批再出一批    interrupt() 等学员作答

为什么用 StateGraph 而不是 create_react_agent
---------------------------------------------
这个流程的形状是**确定的**（解析→出题→逐题问答→判分→报告），不需要模型自己决定
下一步做什么；需要的是在「问答」这一步停下来等真人输入。LangGraph 的 `interrupt()`
配 checkpointer 正好做这件事：图执行到 ask 节点挂起、状态存盘，HTTP 返回题目；
学员提交后用 `Command(resume=...)` 从断点继续，无需自己维护「第几题、已答什么」。

checkpointer
------------
优先 PostgresSaver（复用项目现有 PG，进程重启/多 worker 都能续上），
建表失败或库不可用时退到 MemorySaver（仅当前进程，内测够用）。
thread_id 用诊断会话 id，与 biz_diagnosis_session 一一对应。

LLM 的位置
----------
三处用模型，各自都有降级：

| 节点 | 用途 | 网关不可用时 |
| --- | --- | --- |
| plan_batch | 按岗位职责/技能/要求档出情景判断题（分批、按作答自适应） | 退回行为锚自评题（考核力弱但流程不断） |
| parse_profile | 给图谱召回的候选技能定档 | 命中即 L2 的保守规则 |
| grade | 按出题时生成的 rubric 判问答题 | 证据要素规则打分 |

聚合与报告是确定性计算，不涉及模型。技能选取、档位映射、匹配度也全由知识图谱
与规则决定——模型只负责「把岗位标准写成题」和「读懂学员的自然语言回答」。
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from backend import settings
from backend.agent.assessment.bank import (
    BATCH_SIZE,
    FIRST_BATCH,
    MAX_QUESTIONS,
    generate_batch,
    plan_batch_skills,
    should_stop,
)
from backend.agent.assessment.grading import grade_choice, grade_open, merge_measured
from backend.agent.assessment.report import build_report


class AssessState(TypedDict, total=False):
    # 输入
    session_id: int
    user_id: str
    user_name: str
    occupation_id: str | None
    resume_text: str | None
    # 过程
    occupation: dict[str, Any] | None
    required_items: list[dict[str, Any]]
    profile_levels: dict[str, int]
    profile_meta: dict[str, Any]
    paper: list[dict[str, Any]]
    paper_meta: dict[str, Any]
    batches: int
    exhausted: bool
    stop_reason: str
    cursor: int
    answers: list[dict[str, Any]]
    graded: list[dict[str, Any]]
    measured: dict[str, dict[str, Any]]
    # 输出
    report: dict[str, Any]


# ── 节点 ─────────────────────────────────────────────────────


def parse_profile(state: AssessState) -> dict[str, Any]:
    """简历/自述 → 初始技能画像（可选步骤，无文本则跳过）。"""
    text = (state.get("resume_text") or "").strip()
    if not text:
        return {"profile_levels": {}, "profile_meta": {"engine": "skip"}}
    from backend.agent.assessment.profile import build_profile

    try:
        levels, meta = build_profile(
            text, occupation_name=(state.get("occupation") or {}).get("name")
        )
        return {"profile_levels": levels, "profile_meta": meta}
    except Exception as e:  # noqa: BLE001
        return {"profile_levels": {}, "profile_meta": {"engine": "error", "error": str(e)[:200]}}


def load_target(state: AssessState) -> dict[str, Any]:
    """读岗位职责与技能构成 —— 测评的标准答案来源。"""
    from backend.kg.pg_store.query import get_node
    from backend.kg.pg_store.skill_composition import get_composition

    occ_id = state.get("occupation_id")
    if not occ_id:
        return {"occupation": None, "required_items": []}
    occ = get_node(occ_id) or {}
    comp = get_composition(occ_id)
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
    return {
        "occupation": {
            "id": occ.get("id"),
            "name": occ.get("name"),
            "description": occ.get("description"),
            "level": occ.get("level"),
        },
        "required_items": items,
    }


def plan_batch(state: AssessState) -> dict[str, Any]:
    """出下一批题（首批 3 道，之后按作答自适应）。

    「懒加载」不只是为了首屏快：后续批次能看到前面的作答，才谈得上按学员水平
    调难度、决定深挖还是换维度。
    """
    items = state.get("required_items") or []
    paper = list(state.get("paper") or [])
    graded = list(state.get("graded") or [])
    asked: dict[str, int] = {}
    for q in paper:
        k = str(q.get("skill_key") or "")
        asked[k] = asked.get(k, 0) + 1
    batches = int(state.get("batches") or 0)
    # 批量出题必须按剩余配额裁剪：收敛判定发生在出题**前**，若不扣减，
    # 9 题时判定「未达上限」再出 3 题就会冲到 12 题，越过 MAX_QUESTIONS。
    size = min(FIRST_BATCH if batches == 0 else BATCH_SIZE, MAX_QUESTIONS - len(paper))

    picked = plan_batch_skills(items, asked=asked, graded=graded, size=max(0, size))
    stop, reason = should_stop(
        items,
        asked_total=len(paper),
        batches=batches,
        graded=graded,
        next_batch=picked,
    )
    if stop:
        return {"batches": batches + 1, "exhausted": True, "stop_reason": reason}

    qs, meta = generate_batch(picked, occupation=state.get("occupation"), graded=graded)
    for q in qs:
        q["index"] = len(paper)
        paper.append(q)
    return {
        "paper": paper,
        "batches": batches + 1,
        "paper_meta": {**(state.get("paper_meta") or {}), f"batch{batches + 1}": meta},
        "exhausted": not qs,
        "stop_reason": "" if qs else "本批未产出题目",
        # 出题已把所需信息内联进题目，后续 checkpoint 不必再带整份技能档位明细
        "required_items": [{k: v for k, v in i.items() if k != "levels"} for i in items],
    }


def ask(state: AssessState) -> dict[str, Any]:
    """HITL：挂起等学员作答。interrupt 的返回值即 Command(resume=...) 传入的内容。"""
    paper = state.get("paper") or []
    i = int(state.get("cursor") or 0)
    q = paper[i]
    answer = interrupt({"question": q, "progress": {"index": i, "total": len(paper)}})
    # 允许直接给裸值（选项号/文本），也允许给 {"answer": ...}
    if isinstance(answer, dict) and "answer" in answer:
        answer = answer["answer"]
    return {"answers": (state.get("answers") or []) + [{"index": i, "answer": answer}]}


def grade(state: AssessState) -> dict[str, Any]:
    paper = state.get("paper") or []
    i = int(state.get("cursor") or 0)
    q = paper[i]
    answer = (state.get("answers") or [])[-1].get("answer")

    if q.get("type") == "choice":
        g = grade_choice(q, answer)
    else:
        # 问答题只做校验：拿同技能选择题的自评档作为上限基准
        self_lv = None
        for prev in state.get("graded") or []:
            if prev.get("skill_key") == q.get("skill_key") and prev.get("type") == "choice":
                self_lv = prev.get("level")
                break
        g = grade_open(q, str(answer or ""), self_level=self_lv)
    return {"graded": (state.get("graded") or []) + [g], "cursor": i + 1}


def _after_grade(state: AssessState) -> str:
    """本批还有题就继续问；答完则回去出下一批（由 plan_batch 内的收敛规则决定是否结束）。"""
    if int(state.get("cursor") or 0) < len(state.get("paper") or []):
        return "ask"
    return "aggregate" if state.get("exhausted") else "plan_batch"


def _after_plan(state: AssessState) -> str:
    """出题后：有新题就去问，收敛了就出报告。"""
    if int(state.get("cursor") or 0) < len(state.get("paper") or []):
        return "ask"
    return "aggregate"


def aggregate(state: AssessState) -> dict[str, Any]:
    return {
        "measured": merge_measured(
            state.get("graded") or [], state.get("profile_levels") or {}
        )
    }


def make_report(state: AssessState) -> dict[str, Any]:
    measured = state.get("measured") or merge_measured(
        state.get("graded") or [], state.get("profile_levels") or {}
    )
    rep = build_report(
        occupation=state.get("occupation"),
        required_items=state.get("required_items") or [],
        measured=measured,
        channel="assessment",
    )
    rep["profile_meta"] = state.get("profile_meta") or {}
    rep["paper_meta"] = state.get("paper_meta") or {}
    return {"report": rep}


# ── 图与 checkpointer ────────────────────────────────────────

_GRAPH: Any = None


_POOL: Any = None       # 模块级持有，避免被 GC 关掉连接


def _checkpointer() -> Any:
    """PostgresSaver + 连接池优先；不可用则 MemorySaver（仅本进程）。

    用连接池而非 from_conn_string()：后者返回的是 context manager，
    离开作用域即关连接（踩过一次「the connection is closed」）；
    且 FastAPI 用线程池跑同步端点，单连接并发下不安全。
    """
    global _POOL
    dsn = settings.DATABASE_URL
    if dsn:
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            _POOL = ConnectionPool(
                conninfo=dsn,
                min_size=1,
                max_size=4,
                # PostgresSaver 要求 autocommit + dict_row
                kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
                open=True,
            )
            saver = PostgresSaver(_POOL)
            saver.setup()
            # 显式登记关闭：否则解释器退出时 ConnectionPool.__del__ 会因无法 join
            # 后台线程而抛 PythonFinalizationError（无害但污染日志）
            import atexit

            atexit.register(lambda: _POOL and _POOL.close())
            return saver
        except Exception:
            if _POOL is not None:
                try:
                    _POOL.close()
                except Exception:
                    pass
                _POOL = None
    from langgraph.checkpoint.memory import MemorySaver

    return MemorySaver()


def get_graph() -> Any:
    global _GRAPH
    if _GRAPH is not None:
        return _GRAPH
    g = StateGraph(AssessState)
    g.add_node("parse_profile", parse_profile)
    g.add_node("load_target", load_target)
    g.add_node("plan_batch", plan_batch)
    g.add_node("ask", ask)
    g.add_node("grade", grade)
    g.add_node("aggregate", aggregate)
    g.add_node("build_report", make_report)

    g.add_edge(START, "load_target")        # 先读岗位，简历解析要用岗位名做提示
    g.add_edge("load_target", "parse_profile")
    g.add_edge("parse_profile", "plan_batch")
    g.add_conditional_edges("plan_batch", _after_plan, {"ask": "ask", "aggregate": "aggregate"})
    g.add_edge("ask", "grade")
    g.add_conditional_edges(
        "grade", _after_grade,
        {"ask": "ask", "plan_batch": "plan_batch", "aggregate": "aggregate"},
    )
    g.add_edge("aggregate", "build_report")
    g.add_edge("build_report", END)

    _GRAPH = g.compile(checkpointer=_checkpointer())
    return _GRAPH


def checkpointer_kind() -> str:
    graph = get_graph()
    return type(getattr(graph, "checkpointer", None)).__name__
