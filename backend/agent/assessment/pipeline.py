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

from backend.agent.assessment.bank import estimate_total, plan_all_skills


def plan_questions(
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """出题前定题数与题目清单。

    返回 (plan, estimate)：
    - estimate.total 是这场测评的**确定题数**，收敛以它为准，不再「多条件先到先停」
    - plan 是逐题的技能与题型，顺序即出题顺序
    """
    est = estimate_total(items)
    plan = plan_all_skills(items, est)
    # 清单长度就是真实题数：估算里 MIN_QUESTIONS 之类的下限可能超过可出的题
    est = {**est, "total": len(plan) or int(est.get("total") or 0)}
    return plan, est
