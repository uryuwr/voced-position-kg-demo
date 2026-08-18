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
from backend.kg.pg_store.skill_taxonomy import name_of

_EP = edge_published("e")
_NP = node_published("n")
_LEVEL_N = attrs_level_int("n")


# 置信度排序权重。**不能直接 ORDER BY e.confidence** —— 那是文本列，
# 升序是 ai_inferred < derived < official，取到的恰好是最不可信的那条。
# 这里显式给序，官方 > 规则推导 > LLM 推断。
_CONF_RANK = (
    "CASE e.confidence WHEN 'official' THEN 3 WHEN 'derived' THEN 2 "
    "WHEN 'ai_inferred' THEN 1 ELSE 0 END"
)


def _next_levels(conn, occupation_id: str, limit: int = 5) -> list[dict[str, Any]]:
    """沿 advances_to 找下一级岗位，**可能有多条**。

    `advances_to` 是 1:N（2026-08-18 起）：一个岗位有多个向上方向。早期本体定成
    1:1，这里 `LIMIT 1`，于是 Java 的「全栈 / 技术经理 / 架构师」三条只显示一条。

    排序：置信度高的在前，同置信度按职级由低到高 —— 卡片讲的是「下一级」，
    最近的一级比最远的更贴题。要看完整多跳路径用 `progression.py`。
    """
    return [
        dict(r)
        for r in conn.execute(
            f"""
            -- level 的真源是 attrs.level，n.level 列只是历史兼容。
            -- 只读列会让 level_label 变成 null，前端那句 'L'+level 就成了 Lundefined
            SELECT n.id, n.name, COALESCE(n.level, {_LEVEL_N}) AS level,
                   n.description, e.confidence
            FROM kg_edge e
            JOIN kg_node n ON n.id = e.dst_id AND n.type='occupation' AND {_NP}
            WHERE e.src_id = %s AND e.rel_type = 'advances_to' AND {_EP}
            ORDER BY {_CONF_RANK} DESC,
                     COALESCE(n.level, {_LEVEL_N}) NULLS LAST, n.name
            LIMIT %s
            """,
            (occupation_id, limit),
        ).fetchall()
    ]


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
    """卡片数据。

    `occupation_id` 为空时取当前活跃目标；**显式传了就以它为准，不要求锁定过目标**。

    后半句是 2026-08-18 修的一个 bug：原来无论传不传 id，只要 `biz_user_goal` 里没有
    对应行就回一个空壳（`has_goal=False` + 其余全空）。但「锁定目标」和「诊断过」是
    两件独立的事——诊断会话自带 `target_occupation_id`，压根不需要先锁目标。
    结果是 `/goal/diagnosed` 能列出 8 个诊断过的岗位、带着匹配度，点进任何一个
    却什么都没有（前端显示「该岗位没有目标记录」），能力报告与学习计划全看不到。

    `has_goal` 仍然如实反映「这个岗位是不是我的目标」，前端据它决定要不要显示
    目标卡与「清除目标」入口，别再用它当「有没有数据」的开关。
    """
    from backend.kg.pg_store import biz_store as biz

    goal = biz.get_goal(user_id, occupation_id)
    occ_id = (goal or {}).get("occupation_id") or (occupation_id or "").strip()
    if not occ_id:
        # 既没传 id 又没有活跃目标 —— 这才是真的无从下手
        return {"goal": None, "has_goal": False, "goals": biz.list_goals(user_id)}
    has_goal = bool(goal and goal.get("occupation_id"))
    labels = label_map()
    with connect() as conn:
        occ = conn.execute(
            "SELECT id, name, level, description, attrs FROM kg_node WHERE id=%s", (occ_id,)
        ).fetchone()
        cur_skills = _skill_keys(conn, occ_id)
        nxts = _next_levels(conn, occ_id)
        nxt = nxts[0] if nxts else None
        report = _latest_report(conn, user_id, occ_id)
        # 优先取推送成功的：失败记录的 plan_id 是空串，按时间排它可能压在最上面，
        # 于是明明成功推过的计划在卡片上显示成"未生成"
        plan_row = conn.execute(
            "SELECT plan_id, created_at FROM biz_user_learning_plan "
            "WHERE user_id=%s AND occupation_id=%s AND COALESCE(plan_id,'') <> '' "
            "ORDER BY COALESCE(pushed_at, created_at) DESC LIMIT 1",
            (user_id, occ_id),
        ).fetchone()
        # 每个向上方向各算一份技能缺口
        gaps_by_id: dict[str, list[dict[str, Any]]] = {}
        for cand in nxts:
            g: list[dict[str, Any]] = []
            cand_skills = _skill_keys(conn, cand["id"])
            # 进阶需具备的关键核心技能 = 下一级要求里，当前岗位没有的 / 要求更高的
            for key, s in sorted(
                cand_skills.items(), key=lambda kv: -(float(kv[1].get("weight") or 0))
            ):
                cur = cur_skills.get(key)
                if cur is None or (s.get("required_level") or 0) > (cur.get("required_level") or 0):
                    g.append(
                        {
                            "skill_key": key,
                            "category": s.get("category"),
                            "category_name": name_of(s.get("category")),
                            "required_level": s.get("required_level"),
                            "required_label": labels.get(s.get("required_level") or 0),
                            "current_required_level": (cur or {}).get("required_level"),
                            "weight": float(s.get("weight") or 0),
                        }
                    )
                if len(g) >= 6:
                    break
            gaps_by_id[cand["id"]] = g
        gap_skills = gaps_by_id.get((nxt or {}).get("id"), [])

    occ_d = dict(occ) if occ else {}
    attrs = occ_d.get("attrs")
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except json.JSONDecodeError:
            attrs = {}

    level = occ_d.get("level")
    return {
        # 如实反映「这个岗位是不是我的目标」。诊断过但没锁定时为 false，
        # 但下面的岗位信息 / 测评结果 / 学习计划照常返回。
        "has_goal": has_goal,
        "goal": goal,
        # 学习计划 id；未生成时为空串。推送失败的记录 plan_id 是空串，
        # 所以这里取到空串既可能是"没生成过"也可能是"推失败了"——
        # 要区分看 GET /goal/learning-plans 的 push_status。
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
        # 专业/行业归属来自「锁定目标」那一行。没锁过目标就没有这层信息
        # （诊断不记专业与行业），两个 id 为 null，不是错误。
        "major": {"id": (goal or {}).get("major_id"), "name": (goal or {}).get("major_name")},
        "industry": {
            "id": (goal or {}).get("industry_id"),
            "name": (goal or {}).get("industry_name"),
        },
        # 匹配度以最近一次针对该岗位的测评为准；没测过则为 None，前端提示先测评
        "match_score": (report or {}).get("match_score"),
        "assessment": report,
        # 单数字段保留做向后兼容 —— 取 next_levels 的第一条（置信度最高、职级最近）。
        # 新前端应该读 next_levels：advances_to 是 1:N，一个岗位有多个向上方向。
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
        "next_levels": [
            {
                "id": n["id"],
                "name": n["name"],
                "level": n.get("level"),
                "level_label": f"L{n['level']}" if n.get("level") is not None else None,
                "description": n.get("description"),
                "confidence": n.get("confidence"),
                "unlock_skills": gaps_by_id.get(n["id"], []),
            }
            for n in nxts
        ],
    }
