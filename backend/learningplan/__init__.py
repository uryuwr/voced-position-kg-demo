"""学习计划：把本项目的岗位技能编排推送到 e-ai-spaces。

四个文件各管一段，边界是「碰不碰 IO」：

    schema.py   出站契约模型（纯校验，本地就拦住对方的 422）
    builder.py  纯函数：(会话 + 岗位 + 技能 + 报告) → payload，不碰 DB 不发请求
    courses.py  技能 → 课程资源的批量查询（唯一碰 DB 的地方）
    push.py     发请求 + 状态落库

本服务**不再保存路径副本**：phases/tasks 只在推送时构造，学习进度的真源唯一地
在 e-ai-spaces。本地只留一行关联记录（`biz_user_learning_plan`）用于幂等与重推。
"""
from backend.learningplan.builder import build_payload, external_path_id_for
from backend.learningplan.push import PlanPushError, push_learning_plan
from backend.learningplan.schema import ImportPayload

__all__ = [
    "ImportPayload",
    "PlanPushError",
    "build_payload",
    "external_path_id_for",
    "push_learning_plan",
]
