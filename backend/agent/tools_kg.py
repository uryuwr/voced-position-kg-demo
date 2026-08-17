"""KG 只读工具，供 create_react_agent 调用。"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from backend.kg.pg_store.skill_aggregate import occupation_skill_bundles
from backend.kg.pg_store.query import get_node, list_nodes, search_nodes


@tool
def search_kg_nodes(q: str, node_type: str = "", limit: int = 10) -> str:
    """按关键字搜索知识图谱节点。node_type 可选：industry/major/occupation/skill_level。"""
    ntype = (node_type or "").strip() or None
    if ntype:
        data = list_nodes(
            node_type=ntype, q=q, page=1, page_size=min(limit, 20), published_only=True
        )
        items = data.get("items") or []
    else:
        items = search_nodes(q, limit=min(limit, 20))
    slim = [
        {
            "id": n.get("id"),
            "type": n.get("type"),
            "name": n.get("display_name") or n.get("name"),
        }
        for n in items[:limit]
    ]
    return json.dumps(slim, ensure_ascii=False)


@tool
def get_occupation_skills(occupation_id: str, limit: int = 30) -> str:
    """查询岗位 required 技能构成（逻辑技能聚合，含 required_level 与 weight）。"""
    from backend.kg.pg_store.skill_aggregate import occupation_skill_composition

    try:
        data = occupation_skill_composition(occupation_id)
        skills = (data.get("skills") or [])[:limit]
        return json.dumps(
            {
                "occupation_id": occupation_id,
                "occupation_name": (data.get("occupation") or {}).get("name"),
                "skills": [
                    {
                        "skill_key": b.get("skill_key"),
                        "name": b.get("name"),
                        "required_level": b.get("required_level"),
                        "weight": b.get("weight"),
                        "available_levels": b.get("available_levels"),
                    }
                    for b in skills
                ],
                "weight_sum": data.get("weight_sum"),
                "skill_count": data.get("skill_count"),
            },
            ensure_ascii=False,
        )
    except ValueError:
        pass
    node = get_node(occupation_id, scope="public")
    if not node or node.get("type") != "occupation":
        return json.dumps({"error": "occupation not found", "id": occupation_id})
    bundles = occupation_skill_bundles(occupation_id, limit=limit)
    skills = []
    weight_sum = 0.0
    for b in bundles:
        w = b.get("weight")
        if w is not None:
            try:
                weight_sum += float(w)
            except (TypeError, ValueError):
                pass
        skills.append(
            {
                "skill_key": b.get("skill_key"),
                "name": b.get("name"),
                "required_level": b.get("required_level"),
                "weight": w,
                "available_levels": b.get("available_levels"),
            }
        )
    return json.dumps(
        {
            "occupation_id": occupation_id,
            "occupation_name": node.get("name"),
            "skills": skills,
            "weight_sum": round(weight_sum, 4),
            "skill_count": len(skills),
        },
        ensure_ascii=False,
    )


@tool
def get_node_profile(node_id: str) -> str:
    """按 id 取节点档案（名称、类型、简介摘要）。"""
    n = get_node(node_id, scope="public")
    if not n:
        return json.dumps({"error": "not found", "id": node_id})
    return json.dumps(
        {
            "id": n.get("id"),
            "type": n.get("type"),
            "name": n.get("display_name") or n.get("name"),
            "description": (n.get("description") or "")[:400],
            "attrs_keys": list((n.get("attrs") or {}).keys())[:20]
            if isinstance(n.get("attrs"), dict)
            else [],
        },
        ensure_ascii=False,
    )


def kg_tools() -> list[Any]:
    return [search_kg_nodes, get_occupation_skills, get_node_profile]
