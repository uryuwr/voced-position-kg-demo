"""
BR-01：技能等级 L1–L5 全局定义（名称 + base_score）。

禁止在业务代码/前端散落硬编码「了解/掌握…」与基准分；
一律从本模块或 GET /v1/student/meta/skill-levels 读取。
"""
from __future__ import annotations

from typing import Any, TypedDict


class SkillLevelMetaItem(TypedDict):
    level: int
    code: str
    name: str
    base_score: int


# 唯一权威表（产品 L1 了解 → L5 专家）
SKILL_LEVEL_META: list[SkillLevelMetaItem] = [
    {"level": 1, "code": "L1", "name": "了解", "base_score": 20},
    {"level": 2, "code": "L2", "name": "掌握", "base_score": 40},
    {"level": 3, "code": "L3", "name": "熟练", "base_score": 60},
    {"level": 4, "code": "L4", "name": "精通", "base_score": 80},
    {"level": 5, "code": "L5", "name": "专家", "base_score": 100},
]

REQUIRED_LEVEL_CODES: tuple[str, ...] = tuple(x["code"] for x in SKILL_LEVEL_META)


def skill_level_meta() -> list[dict[str, Any]]:
    """对外列表（含 API 兼容字段 level/name/base_score）。"""
    return [
        {"level": x["level"], "name": x["name"], "base_score": x["base_score"], "code": x["code"]}
        for x in SKILL_LEVEL_META
    ]


def level_name(level: int | str) -> str:
    li = int(str(level).upper().replace("L", ""))
    for x in SKILL_LEVEL_META:
        if x["level"] == li:
            return x["name"]
    raise KeyError(f"unknown skill level: {level}")


def level_code(level: int | str) -> str:
    li = int(str(level).upper().replace("L", ""))
    return f"L{li}"


def base_score(level: int | str) -> int:
    li = int(str(level).upper().replace("L", ""))
    for x in SKILL_LEVEL_META:
        if x["level"] == li:
            return x["base_score"]
    raise KeyError(f"unknown skill level: {level}")


def label_map() -> dict[int, str]:
    return {x["level"]: x["name"] for x in SKILL_LEVEL_META}


def code_to_name() -> dict[str, str]:
    return {x["code"]: x["name"] for x in SKILL_LEVEL_META}
