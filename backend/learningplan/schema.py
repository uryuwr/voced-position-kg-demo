"""e-ai-spaces `POST /v1/internal/job-plans/import` 的**出站**契约模型。

这里的每条约束都是 2026-08-18 对预生产实测反推出来的（有效签名 + 故意的非法值 →
读对方 422 的 detail），不是照抄文档——文档与实际有出入，最典型的是 `resources`：
文档写 `name` / `source_url`，**实际契约是 `title` / `url`**，照文档写必吃 422。

为什么要在本地再写一遍对方的校验
--------------------------------
不写的话，任何构造错误都要等一次跨网请求才暴露，而且报错以对方的 `loc` 路径呈现
（`body.phases.0.tasks.3.resources.1.url`），排查要反推是哪个技能哪门课。
在这里拦住，错误就落在 builder 的单测里。

`extra="forbid"`：payload 必须严格白名单构造。dump 内部行对象会带出多余键，
对方也是 forbid，等于把一次 422 换到本地。
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 对方的 id 字符集（实测 422 原文）：首字符必须是字母数字，总长 ≤128
ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_ID_RE = re.compile(ID_PATTERN)

MAX_PHASES = 12
MAX_TASKS_PER_PHASE = 50
MAX_TASKS_TOTAL = 200
MAX_SKILLS_PER_TASK = 10
MAX_MINUTES = 6000
# 契约要求各阶段权重之和落在这个闭区间（留 ±1 给浮点舍入）
WEIGHT_SUM_MIN, WEIGHT_SUM_MAX = 99.0, 101.0


def sanitize_id(raw: str, *, fallback: str) -> str:
    """把任意字符串收成合法 id。技能名带中文和引号（如 `“互联网+”增材制造`），
    直接用作 id 会被 422 拒，所以 id 一律由我们自己按规则生成，不取业务名称。
    """
    s = re.sub(r"[^A-Za-z0-9_.:-]", "-", raw or "").strip("-")
    s = re.sub(r"-{2,}", "-", s).lstrip("_.:-")[:128]
    return s if s and _ID_RE.match(s) else fallback


class Resource(BaseModel):
    """学习资源。字段名是 `title`/`url`——不是 `name`/`source_url`，见模块 docstring。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=256)
    url: str = Field(min_length=1, max_length=1024)

    @field_validator("url")
    @classmethod
    def _absolute_http(cls, v: str) -> str:
        # 对方原文：resource url must be an absolute http(s) URL
        if not v.lower().startswith(("http://", "https://")):
            raise ValueError(f"资源链接必须是绝对 http(s) 地址，收到 {v[:60]!r}")
        return v


class Skill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str = Field(min_length=1, max_length=128)
    skill_name: str = Field(min_length=1, max_length=256)


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_task_id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=1024)
    estimated_minutes: int = Field(ge=0, le=MAX_MINUTES)
    completed: bool = False
    skills: list[Skill] = Field(default_factory=list, max_length=MAX_SKILLS_PER_TASK)
    resources: list[Resource] = Field(default_factory=list)
    # 刻意不含 task_weight：契约要求 task_weight > 0，而课程任务本就不该计权
    # （会让同一技能被算两次）。整条路径都不传，由对方按「阶段权重阶段内均分」处理。


class Phase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_phase_id: str = Field(pattern=ID_PATTERN)
    phase_name: str = Field(min_length=1, max_length=256)
    phase_weight: float = Field(gt=0, le=100)
    tasks: list[Task] = Field(min_length=1, max_length=MAX_TASKS_PER_PHASE)


class ImportPayload(BaseModel):
    """导入请求体。

    `tenant_id` 不传：本项目全仓没有 org/tenant 概念，只能走对方的个人租户派生。
    这一条**必须与 e-ai-spaces 的读取侧对齐**，否则会出现对接文档警告的
    「导入成功但用户端查不到」——导入返回 200，学员那边空白，最难查的一类问题。
    """

    model_config = ConfigDict(extra="forbid")

    external_path_id: str = Field(pattern=ID_PATTERN)
    external_job_id: str = Field(min_length=1, max_length=128)
    job_name: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=256)
    goal: str | None = Field(default=None, max_length=1024)
    total_duration: str | None = Field(default=None, max_length=64)
    revision_of_external_path_id: str | None = Field(default=None, pattern=ID_PATTERN)
    phases: list[Phase] = Field(min_length=1, max_length=MAX_PHASES)

    @model_validator(mode="after")
    def _check_totals(self) -> "ImportPayload":
        total = sum(len(p.tasks) for p in self.phases)
        if total > MAX_TASKS_TOTAL:
            raise ValueError(f"全路径任务数 {total} 超过上限 {MAX_TASKS_TOTAL}")

        s = round(sum(p.phase_weight for p in self.phases), 6)
        if not (WEIGHT_SUM_MIN <= s <= WEIGHT_SUM_MAX):
            raise ValueError(
                f"阶段权重之和 {s} 不在 [{WEIGHT_SUM_MIN}, {WEIGHT_SUM_MAX}]；"
                "归一时最后一阶段应取 100-已分配，而不是各自 round"
            )

        # id 在全路径内必须唯一：重复会让对方的进度锚点串台，而且不一定报错
        tids = [t.external_task_id for p in self.phases for t in p.tasks]
        if len(set(tids)) != len(tids):
            dup = sorted({x for x in tids if tids.count(x) > 1})
            raise ValueError(f"external_task_id 重复: {dup[:5]}")
        pids = [p.external_phase_id for p in self.phases]
        if len(set(pids)) != len(pids):
            raise ValueError("external_phase_id 重复")
        return self

    def wire_dict(self) -> dict[str, Any]:
        """发出去的 JSON。`exclude_none` 让未取值的可选字段整体不出现，
        而不是发 `null`——对方对可选字段的 null 处理未验证过，不冒这个险。
        """
        return self.model_dump(mode="json", exclude_none=True)
