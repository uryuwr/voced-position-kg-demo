"""目标概览：一次取齐「岗位学习与自适应路径」卡片需要的全部数据。

对应原型的那张卡：

    ┌ 当前活跃目标 ─────────────────┬ 下一级成长目标 ──────────┐
    │ 归属专业 / 岗位名 / 职级 / 匹配度 │ 下一级岗位 + 一键升级     │
    │ 岗位职责描述                    │ 进阶需具备的关键核心技能   │
    ├ 胜任力能力报告与技能缺口分析 ───────────────────────────┤
    │ 各维能力 当前 vs 目标（L1–L5 进度条） │ 优势精通 / 关键能力缺口 │
    └──────────────────────────────────────────────────┘

数据来自三处，都已按岗位绑定：
- 锁定目标  `biz_user_goal`（一人多目标，其一 active）
- 测评结果  `biz_diagnosis_result` ← `biz_diagnosis_session.target_occupation_id`
- 晋升路径  `advances_to` 边

**晋升边目前只覆盖 1331 个岗位里的 13 个（1.0%）**，所以 `next_level` 经常为空；
这是数据缺口不是逻辑缺口，接口按「无下一级」正常返回，前端隐藏该区块即可。
"""
from __future__ import annotations

import json
from typing import Any

from backend.kg.pg_store.client import connect
from backend.kg.pg_store.config import edge_published, node_published
from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL
from backend.kg.pg_store.skill_level_meta import label_map

_EP = edge_published("e")
_NP = node_published("n")


def _next_level(conn, occupation_id: str) -> dict[str, Any] | None:
    """沿 advances_to 找下一级岗位。schema 约定 1:1，取置信度最高的一条。"""
    row = conn.execute(
        f"""
        SELECT n.id, n.name, n.level, n.description, e.confidence
        FROM kg_edge e
        JOIN kg_node n ON n.id = e.dst_id AND n.type='occupation' AND {_NP}
        WHERE e.src_id = %s AND e.rel_type = 'advances_to' AND {_EP}
        ORDER BY e.confidence NULLS LAST
        LIMIT 1
        """,
        (occupation_id,),
    ).fetchone()
    return dict(row) if row else None


def _skill_keys(conn, occupation_id: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT ({SKILL_KEY_SQL}) AS skill_key, n.category,
               (n.attrs::json->>'level')::int AS required_level, e.weight
        FROM kg_edge e
        JOIN kg_node n ON n.id = e.dst_id AND n.type='skill_level' AND {_NP}
        WHERE e.src_id = %s AND e.rel_type = 'requires' AND {_EP}
        """,
        (occupation_id,),
    ).fetchall()
    return {r["skill_key"]: dict(r) for r in rows}


def _latest_report(conn, user_id: str, occupation_id: str) -> dict[str, Any] | None:
    """该用户针对**这个岗位**最近一次测评报告。"""
    row = conn.execute(
        """
        SELECT r.report_json, r.match_score, r.created_at, s.id AS session_id, s.channel
        FROM biz_diagnosis_result r
        JOIN biz_diagnosis_session s ON s.id = r.session_id
        WHERE s.user_id = %s AND s.target_occupation_id = %s
        ORDER BY r.created_at DESC
        LIMIT 1
        """,
        (user_id, occupation_id),
    ).fetchone()
    if not row:
        return None
    rep = row["report_json"]
    if isinstance(rep, str):
        rep = json.loads(rep)
    rep = rep or {}
    rep.setdefault("match_score", row["match_score"])
    rep["session_id"] = row["session_id"]
    rep["channel"] = row["channel"]
    rep["created_at"] = row["created_at"].isoformat() if row["created_at"] else None
    return rep


def goal_overview(user_id: str, occupation_id: str | None = None) -> dict[str, Any]:
    """卡片数据。occupation_id 为空时取当前活跃目标。"""
    from backend.kg.pg_store import biz_store as biz

    goal = biz.get_goal(user_id, occupation_id)
    if not goal or not goal.get("occupation_id"):
        return {"goal": None, "has_goal": False, "goals": biz.list_goals(user_id)}

    occ_id = goal["occupation_id"]
    labels = label_map()
    with connect() as conn:
        occ = conn.execute(
            "SELECT id, name, level, description, attrs FROM kg_node WHERE id=%s", (occ_id,)
        ).fetchone()
        cur_skills = _skill_keys(conn, occ_id)
        nxt = _next_level(conn, occ_id)
        report = _latest_report(conn, user_id, occ_id)
        gap_skills: list[dict[str, Any]] = []
        if nxt:
            nxt_skills = _skill_keys(conn, nxt["id"])
            # 进阶需具备的关键核心技能 = 下一级要求里，当前岗位没有的 / 要求更高的
            for key, s in sorted(
                nxt_skills.items(), key=lambda kv: -(float(kv[1].get("weight") or 0))
            ):
                cur = cur_skills.get(key)
                if cur is None or (s.get("required_level") or 0) > (cur.get("required_level") or 0):
                    gap_skills.append(
                        {
                            "skill_key": key,
                            "category": s.get("category"),
                            "required_level": s.get("required_level"),
                            "required_label": labels.get(s.get("required_level") or 0),
                            "current_required_level": (cur or {}).get("required_level"),
                            "weight": float(s.get("weight") or 0),
                        }
                    )
                if len(gap_skills) >= 6:
                    break

    occ_d = dict(occ) if occ else {}
    attrs = occ_d.get("attrs")
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except json.JSONDecodeError:
            attrs = {}

    level = occ_d.get("level")
    return {
        "has_goal": True,
        "goal": goal,
        "goals": biz.list_goals(user_id),
        "occupation": {
            "id": occ_d.get("id"),
            "name": occ_d.get("name"),
            "level": level,
            "level_label": f"L{level}" if level is not None else None,
            "description": occ_d.get("description"),
            "salary": (attrs or {}).get("salary"),
            "skill_count": len(cur_skills),
        },
        "major": {"id": goal.get("major_id"), "name": goal.get("major_name")},
        "industry": {"id": goal.get("industry_id"), "name": goal.get("industry_name")},
        # 匹配度以最近一次针对该岗位的测评为准；没测过则为 None，前端提示先测评
        "match_score": (report or {}).get("match_score"),
        "assessment": report,
        "next_level": (
            {
                "id": nxt["id"],
                "name": nxt["name"],
                "level": nxt.get("level"),
                "level_label": f"L{nxt['level']}" if nxt.get("level") is not None else None,
                "description": nxt.get("description"),
                "unlock_skills": gap_skills,
            }
            if nxt
            else None
        ),
    }
