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
from backend.kg.pg_store.config import attrs_level_int, edge_published, node_published
from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL
from backend.kg.pg_store.skill_level_meta import label_map

_EP = edge_published("e")
_NP = node_published("n")
_LEVEL_N = attrs_level_int("n")


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
               {_LEVEL_N} AS required_level, e.weight
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


def diagnosed_occupations(
    user_id: str, *, page: int = 1, page_size: int = 12
) -> dict[str, Any]:
    """该用户「做过诊断 / 锁定过」的岗位列表 —— 路径页的一级视图，分页返回。

    合并两个来源：诊断记录（一个岗位可能测过多次，取最近一次）与目标记录
    （刚锁定还没测的岗位也要能进详情页发起测评）。

    合并、排序、切片都在 SQL 里做——每换一次目标就多一条记录，全量查出来再在
    内存里排序切片，数据涨起来就是隐患。
    """
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 100))
    offset = (page - 1) * page_size

    merged_sql = """
        WITH latest AS (
          SELECT DISTINCT ON (s.target_occupation_id)
                 s.target_occupation_id AS occupation_id,
                 s.target_occupation_name AS occupation_name,
                 s.channel, s.id AS session_id, r.match_score, r.created_at AS diagnosed_at
          FROM biz_diagnosis_session s
          JOIN biz_diagnosis_result r ON r.session_id = s.id
          WHERE s.user_id = %(uid)s AND s.target_occupation_id IS NOT NULL
          ORDER BY s.target_occupation_id, r.created_at DESC
        ),
        cnt AS (
          SELECT target_occupation_id AS occupation_id, COUNT(*) AS session_count
          FROM biz_diagnosis_session
          WHERE user_id = %(uid)s AND target_occupation_id IS NOT NULL
          GROUP BY 1
        ),
        goals AS (
          SELECT occupation_id, occupation_name, status, major_name, created_at
          FROM biz_user_goal WHERE user_id = %(uid)s AND occupation_id IS NOT NULL
        )
        SELECT COALESCE(l.occupation_id, g.occupation_id) AS occupation_id,
               COALESCE(l.occupation_name, g.occupation_name) AS occupation_name,
               l.match_score, l.channel, l.session_id, l.diagnosed_at,
               COALESCE(c.session_count, 0) AS session_count,
               g.status AS goal_status, g.major_name, g.created_at AS goal_created_at
        FROM latest l
        FULL OUTER JOIN goals g ON g.occupation_id = l.occupation_id
        LEFT JOIN cnt c ON c.occupation_id = COALESCE(l.occupation_id, g.occupation_id)
    """

    with connect() as conn:
        total = int(
            conn.execute(
                f"SELECT COUNT(*) n FROM ({merged_sql}) t", {"uid": user_id}
            ).fetchone()["n"]
        )
        rows = conn.execute(
            f"""
            SELECT * FROM ({merged_sql}) t
            ORDER BY (goal_status = 'active') DESC NULLS LAST,
                     diagnosed_at DESC NULLS LAST,
                     goal_created_at DESC NULLS LAST
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {"uid": user_id, "limit": page_size, "offset": offset},
        ).fetchall()

        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "occupation_id": r["occupation_id"],
                    "occupation_name": r["occupation_name"],
                    "match_score": r["match_score"],
                    "channel": r["channel"],
                    "last_session_id": r["session_id"],
                    "session_count": int(r["session_count"] or 0),
                    "diagnosed_at": r["diagnosed_at"].isoformat() if r["diagnosed_at"] else None,
                    "goal_status": r["goal_status"],
                    "is_active_goal": r["goal_status"] == "active",
                    "major_name": r["major_name"],
                    "goal_created_at": (
                        r["goal_created_at"].isoformat() if r["goal_created_at"] else None
                    ),
                    "plan_id": "",
                    "plan_created_at": None,
                }
            )

        ids = [x["occupation_id"] for x in out if x["occupation_id"]]
        if ids:
            # 学习计划 id（uuid 字符串）；未接通/未生成时保持空串
            for r in conn.execute(
                """
                SELECT DISTINCT ON (occupation_id) occupation_id, plan_id, created_at
                FROM biz_user_learning_plan
                WHERE user_id = %s AND occupation_id = ANY(%s)
                ORDER BY occupation_id, created_at DESC
                """,
                (user_id, ids),
            ).fetchall():
                for it in out:
                    if it["occupation_id"] == r["occupation_id"]:
                        it["plan_id"] = r["plan_id"] or ""
                        it["plan_created_at"] = (
                            r["created_at"].isoformat() if r["created_at"] else None
                        )
            for n in conn.execute(
                f"SELECT n.id, n.name, n.level, n.description FROM kg_node n "
                f"WHERE n.id = ANY(%s) AND {_NP}",
                (ids,),
            ).fetchall():
                for it in out:
                    if it["occupation_id"] == n["id"]:
                        it["occupation_name"] = it.get("occupation_name") or n["name"]
                        it["level"] = n["level"]
                        it["level_label"] = (
                            f"L{n['level']}" if n["level"] is not None else None
                        )
                        it["description"] = n["description"]

    return {
        "items": out,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size if page_size else 1,
    }


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
        plan_row = conn.execute(
            "SELECT plan_id, created_at FROM biz_user_learning_plan "
            "WHERE user_id=%s AND occupation_id=%s ORDER BY created_at DESC LIMIT 1",
            (user_id, occ_id),
        ).fetchone()
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
        # 学习计划 id（uuid 字符串）；未生成/接口未接通时为空串
        "learning_plan_id": (plan_row or {}).get("plan_id") or "",
        "learning_plan_created_at": (
            plan_row["created_at"].isoformat() if plan_row and plan_row["created_at"] else None
        ),
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
