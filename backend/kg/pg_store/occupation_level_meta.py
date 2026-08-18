"""岗位职级（occupation level）的枚举真源 —— 代码内定义，**不入库**。

为什么单独一份，不复用 skill_level_meta
--------------------------------------
两者都是 1–5，但语义完全不同，混用会让前端显示出「Java 岗位 · 熟练」这种怪话：

    skill_level_meta   技能掌握程度：了解 / 掌握 / 熟练 / 精通 / 专家
    occupation_level   职级序列    ：入门 / 专员 / 资深 / 经理 / 总监

为什么 code 不存数据库
---------------------
`attrs.level`（1–5 的整数）是**唯一真源**；`level_code`（"L2"）与 `level_name`（"专员"）
都由它派生。存进库就是双写：改了名称要迁移全部历史行，且两列可能不一致
（本项目已经因为「id 里的 L 编号与 name 里的 L 编号不一致」吃过一次亏）。

所以：**写库只写 `attrs.level`，读路径派生 code/name**，前端不要自己拼 `'L'+level`。
"""
from __future__ import annotations

from typing import Any, TypedDict


class OccupationLevelMetaItem(TypedDict):
    level: int
    code: str
    name: str
    desc: str


# 档位定义与 crawlers/cn/link_boss_skill_chain.py 里 LLM 判定 job_level 的口径一致
OCCUPATION_LEVEL_META: list[OccupationLevelMetaItem] = [
    {"level": 1, "code": "L1", "name": "入门", "desc": "入门 / 助理，在指导下承担明确任务"},
    {"level": 2, "code": "L2", "name": "专员", "desc": "能独立负责本职工作的常规交付"},
    {"level": 3, "code": "L3", "name": "资深", "desc": "资深 / 主管，能带小组或负责专项"},
    {"level": 4, "code": "L4", "name": "经理", "desc": "经理，负责团队目标与跨职能协作"},
    {"level": 5, "code": "L5", "name": "总监", "desc": "总监及以上，负责业务线或技术方向"},
]

_BY_LEVEL: dict[int, OccupationLevelMetaItem] = {m["level"]: m for m in OCCUPATION_LEVEL_META}

MIN_LEVEL = 1
MAX_LEVEL = len(OCCUPATION_LEVEL_META)


def _coerce(level: Any) -> int | None:
    """脏值一律取 None，不抛错 —— attrs 是无约束 TEXT，读路径必须自己站得住。"""
    if level is None or isinstance(level, bool):
        return None
    try:
        n = int(str(level).strip())
    except (TypeError, ValueError):
        return None
    return n if MIN_LEVEL <= n <= MAX_LEVEL else None


def level_code(level: Any) -> str | None:
    """1 → "L1"；越界或脏值 → None（不要返回 "Lundefined" 之类的占位）。"""
    n = _coerce(level)
    return _BY_LEVEL[n]["code"] if n else None


def level_name(level: Any) -> str | None:
    n = _coerce(level)
    return _BY_LEVEL[n]["name"] if n else None


def level_meta(level: Any) -> OccupationLevelMetaItem | None:
    n = _coerce(level)
    return _BY_LEVEL[n] if n else None


def code_map() -> dict[str, str]:
    return {str(m["level"]): m["code"] for m in OCCUPATION_LEVEL_META}


def label_map() -> dict[str, str]:
    return {str(m["level"]): m["name"] for m in OCCUPATION_LEVEL_META}


def occupation_level_meta() -> list[OccupationLevelMetaItem]:
    """给 GET /v1/student/meta/* 下发，前端据此渲染，不要硬编码档位名。"""
    return [dict(m) for m in OCCUPATION_LEVEL_META]  # type: ignore[misc]
