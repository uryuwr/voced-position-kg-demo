"""出题流水线：LangGraph 只留在**无人参与的编排**上。

    plan → (并行) generate × N → validate → 出卷

这里没有 interrupt：整段一次跑完，不需要断点续跑，也就不需要 checkpointer。
人机往返（答题）由 service 的三段式承担，与本流水线无关。

目前规划与生成都很轻（规划是纯计算，生成是几次模型调用），所以对外只暴露
`plan_questions`；并行生成留在 service 的分批推送里做，避免多引入一层间接。
把它独立成模块是为了让「编排」与「交互」在代码结构上就分开——
将来要加「题目质量校验」「难度平衡」这类步骤时，它们属于这里，不属于 service。
"""
from __future__ import annotations

from typing import Any

from backend.agent.assessment.bank import MAX_QUESTIONS, estimate_total, plan_all_skills


def plan_questions(
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """出题前定题数与题目清单。

    返回 (plan, estimate)：
    - plan 是逐题的技能与题型，顺序即出题顺序
    - estimate.total 是这场测评的**确定题数**，收敛以它为准，不再「多条件先到先停」

    这里返回的 estimate 描述的是**实际排出的清单**：`total` 会推给前端当「本次测评共
    N 题」、并写进 `target_total` 当收敛阈值，`cover`/`verify` 是排查用的分项。三者与
    plan 对不上就会出事：total 偏大则进度条永远走不到 100%、收敛判定等不到；偏小则
    学员答完了还在出题。所以它们一律按 plan 回填，而不是照抄 estimate_total 的
    **需求量**（那份 cover/verify 没夹上限，20 项技能的岗位会算到 16+2=18）。
    """
    est = estimate_total(items)
    plan = plan_all_skills(items, est)
    # 兜底：排题已按 est['total'] 的预算走（bank._fit_budget 按比例分配两类题），
    # 这里再挡一次硬上限。真走到这一步说明规划环节有回退，截断会先牺牲尾部的验证题，
    # 属于「宁可少考也不让学员连答 18 道」的下策，不是常规路径。
    if len(plan) > MAX_QUESTIONS:
        plan = plan[:MAX_QUESTIONS]
    cover = sum(1 for p in plan if p.get("_item_type") != "open")
    verify = len(plan) - cover
    reason = str(est.get("reason") or "")
    if (int(est.get("cover") or 0), int(est.get("verify") or 0)) != (cover, verify):
        reason += f"；受硬上限 {MAX_QUESTIONS} 题约束，实排 {cover} 覆盖题 + {verify} 验证题"
    return plan, {**est, "total": len(plan), "cover": cover, "verify": verify, "reason": reason}
