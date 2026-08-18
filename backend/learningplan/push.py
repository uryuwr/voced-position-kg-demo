"""把 payload 推给 e-ai-spaces，并把上游状态翻译成本服务的语义。

**没有降级分支。** 推不上去学员就拿不到计划，接口直接透出错误。
CLAUDE.md 的「LLM 一律可降级」针对 LLM 网关，学习计划是业务主数据，不适用——
给个假的 plan_id 蒙混过去，学员点进去是空的，比直接报错难查得多。

409 是陷阱，不要重试
--------------------
契约的 409 = 同 `external_path_id` 但内容变了。现实触发路径：第一次推送超时 →
重试之间图谱数据被改（运营调了岗位技能权重）→ 重算出的 payload 不一致 → 409。

所以碰到 409 **不重试、不换 id 重推**，直接记 `failed` 并透出：它表示契约被违反，
需要人去看是谁改了数据。自动换个 id 重推会在对方那边堆出重复路径。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from backend import settings
from backend.bts import BtsError, bts_client
from backend.learningplan.schema import ImportPayload


class PlanPushError(RuntimeError):
    """推送失败。`kind` 决定路由层回什么状态码。

    kind:
        unconfigured  未配置上游（502）
        conflict      409 契约违规，需人工（409）
        rejected      422 对方拒收——本地校验没拦住，属于 builder 的 bug（422）
        upstream      5xx / 网络 / 其他（502）
    """

    def __init__(self, kind: str, message: str, *, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.body = body


def payload_sha256(payload: ImportPayload) -> str:
    """推送快照指纹。`sort_keys` 保证同一内容永远同一指纹，用于事后比对
    「这次重推和上次推的是不是同一份」——409 排查的第一现场。
    """
    raw = json.dumps(payload.wire_dict(), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _extract(body: Any) -> dict[str, Any]:
    """对方的返回可能裹一层 data，也可能是裸对象。两种都吃。"""
    if not isinstance(body, dict):
        return {}
    inner = body.get("data")
    return inner if isinstance(inner, dict) else body


def push_learning_plan(payload: ImportPayload, *, uc_user_id: str) -> dict[str, Any]:
    """推送并返回 `{plan_id, created, superseded_plan_id, raw}`。

    `uc_user_id` 走 `Userid` 头——BTS 是应用身份，本身不带用户，代表谁调用要显式指明。
    """
    client = bts_client()
    if not client.available() or not settings.LEARNING_PLAN_PATH:
        raise PlanPushError(
            "unconfigured",
            "学习计划服务未配置（需要 BTS_ENDPOINT / BTS_ACCOUNT / BTS_PASSWORD "
            "与 E_AI_SPACE）",
        )

    try:
        body = client.post(
            settings.LEARNING_PLAN_PATH,
            json=payload.wire_dict(),
            uc_user_id=str(uc_user_id),
        )
    except BtsError as e:
        if e.status == 409:
            raise PlanPushError(
                "conflict",
                f"上游拒绝：同一 external_path_id 内容已变更（{payload.external_path_id}）。"
                "这通常意味着两次推送之间图谱数据被改过，需人工确认后再处理。",
                status=409, body=e.body,
            ) from e
        if e.status == 422:
            # 本地 schema 应该先拦住。走到这里说明两边契约有出入，要当 bug 修，
            # 而不是加个 try 吞掉——吞掉就变成"推送成功但学员看不到"。
            raise PlanPushError(
                "rejected",
                f"上游校验未通过（本地 schema 未拦住，属于 builder 缺陷）：{e.body}",
                status=422, body=e.body,
            ) from e
        raise PlanPushError(
            "upstream", f"学习计划服务不可用：{e}", status=e.status, body=e.body
        ) from e
    except Exception as e:  # noqa: BLE001  网络层异常也归上游不可用
        raise PlanPushError("upstream", f"学习计划服务请求失败：{e}") from e

    d = _extract(body)
    plan_id = str(d.get("plan_id") or d.get("id") or "").strip()
    if not plan_id:
        # 200 但没给 id：不能当成功——本地会存个空关联，学员点进去是空的
        raise PlanPushError(
            "upstream", f"上游返回 200 但没有 plan_id：{str(body)[:300]}", body=body
        )
    return {
        "plan_id": plan_id,
        # 契约用 created 区分「新建」与「幂等命中」；缺省按新建
        "created": bool(d.get("created", True)),
        "superseded_plan_id": (str(d.get("superseded_plan_id") or "").strip() or None),
        "raw": body,
    }
