"""业务表读写（学员/运营）。"""
from __future__ import annotations

import json
from typing import Any

from backend.kg.pg_store.biz_ddl import ACHIEVEMENT_SEEDS, BIZ_SCHEMA_SQL
from backend.kg.pg_store.client import connect, ensure_schema, use_conn
from backend.kg.pg_store.counts import (
    counts_for_industries,
    counts_for_majors,
    counts_for_occupations,
    industries_for_occupations,
)
from backend.kg.pg_store.query import (
    get_node,
    list_nodes,
    major_occupations,
    occupation_skills,
    search_nodes,
)
from backend.kg.pg_store.skill_aggregate import (
    get_skill_bundle,
    list_skill_bundles,
    occupation_skill_bundles,
)


def ensure_biz_schema() -> None:
    ensure_schema()
    with connect() as conn:
        conn.execute(BIZ_SCHEMA_SQL)
        for code, name, desc, points, cat in ACHIEVEMENT_SEEDS:
            conn.execute(
                """
                INSERT INTO biz_achievement_def (code, name, description, points, category)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (code) DO UPDATE SET
                  name = EXCLUDED.name,
                  description = EXCLUDED.description,
                  points = EXCLUDED.points,
                  category = EXCLUDED.category
                """,
                (code, name, desc, points, cat),
            )
        conn.commit()


# ── 产品元数据（BR-01：唯一来源 skill_level_meta 模块）────────


def skill_level_meta() -> list[dict[str, Any]]:
    from backend.kg.pg_store.skill_level_meta import skill_level_meta as _meta

    return _meta()


def skill_categories() -> list[dict[str, Any]]:
    """技能大类字典。

    取自国家职业技能标准的「职业功能」维度（见 skill_taxonomy.CATEGORY_ORDER），
    并与库内 kg_node.category 实际存量对齐——此前这里写死的是「运营策略/数据能力/
    内容创作…」6 类互联网口径，与库里的分类对不上，导致诊断雷达图的轴是空的。
    """
    from backend.kg.pg_store.skill_taxonomy import CATEGORY_ORDER

    with connect() as conn:
        rows = conn.execute(
            "SELECT category, count(*) n FROM kg_node "
            "WHERE type='skill_level' AND category IS NOT NULL "
            "AND COALESCE(status,'published')='published' GROUP BY 1"
        ).fetchall()
    counts = {r["category"]: int(r["n"]) for r in rows}
    out = [
        {"id": f"C{i + 1}", "name": name, "skill_count": counts.get(name, 0)}
        for i, name in enumerate(CATEGORY_ORDER)
    ]
    # 库里出现但不在标准序里的分类，兜底追加，避免字典漏项
    for name, n in sorted(counts.items(), key=lambda x: -x[1]):
        if name not in CATEGORY_ORDER:
            out.append({"id": f"CX{len(out)}", "name": name, "skill_count": n})
    return out


# 技能名退化匹配的闸门（见 position_match._user_level_for）
FUZZY_MIN_LEN = 4         # 参与包含匹配的最短技能名（中文 4 字）
FUZZY_MIN_RATIO = 0.55    # 短串至少要占长串多少比例（「数据分析」占「数据分析与复盘」
                          # 是 4/7≈0.57，应放行；「运维」占「设备运维管理」只有 0.33，拦下）


def position_match(
    user_id: str, occupation_id: str, *, limit: int = 50
) -> dict[str, Any]:
    """岗位匹配度：用户技能画像 × 岗位 requires，按国标权重加权。

    单项达标率 = min(用户等级 / 要求等级, 1)；总分 = Σ(达标率 × 权重) / Σ权重 × 100。
    比旧的「命中数 / 需求数」更准：既考虑等级差距，也让高权重技能影响更大。

    名称对齐：用户技能名与 skill_key 先精确匹配，再退化为包含匹配
    （用户画像里的技能名来自诊断解析，不保证与国标 skill_key 完全一致）。
    """
    occ = get_node(occupation_id, scope="public")
    if not occ or occ.get("type") != "occupation":
        raise ValueError("occupation not found")
    required = occupation_skill_bundles(occupation_id, limit=limit)

    with connect() as conn:
        urows = conn.execute(
            "SELECT skill_name, level, score FROM biz_user_skill WHERE user_id=%s",
            (user_id,),
        ).fetchall()
    user_levels: dict[str, int] = {}
    for r in urows:
        nm = (r["skill_name"] or "").strip()
        if nm:
            user_levels[nm] = max(user_levels.get(nm, 0), int(r["level"] or 0))
    return match_with_profile(occ, required, user_levels)


def match_with_profile(
    occ: dict[str, Any],
    required: list[dict[str, Any]],
    user_levels: dict[str, int],
) -> dict[str, Any]:
    """给定岗位技能要求与一份技能画像，算加权达标率。

    抽出来是为了让「岗位探索列表」能用**五维记忆推断的画像**跑同一套算法——
    匹配度口径必须只有一个，否则列表、目标卡、诊断报告又会给出对不上的数字。
    """

    def _user_level_for(skill_key: str) -> tuple[int, str | None]:
        """用户画像里哪一项对应这个岗位技能。

        先精确匹配；退化的包含匹配**加了两道闸**：
        1. 双方长度都 ≥ FUZZY_MIN_LEN —— 否则「运维」会命中「设备运维管理」、
           「诊断」会命中「汽车故障诊断」，把无关技能按满档计入，匹配度虚高
        2. 短串要占长串 ≥ FUZZY_MIN_RATIO —— 「故障诊断」→「汽车故障诊断」(0.67)、
           「数据分析」→「数据分析与复盘」(0.57) 放行；「运维」→「设备运维管理」(0.33) 拦下
        命中多个时取重合比例最高的，而不是碰上的第一个。
        """
        if skill_key in user_levels:
            return user_levels[skill_key], skill_key
        k = (skill_key or "").strip().lower()
        if len(k) < FUZZY_MIN_LEN:
            return 0, None
        best: tuple[float, int, str] | None = None
        for nm, lv in user_levels.items():
            n = nm.strip().lower()
            if len(n) < FUZZY_MIN_LEN:
                continue
            if n in k or k in n:
                ratio = min(len(n), len(k)) / max(len(n), len(k))
                if ratio >= FUZZY_MIN_RATIO and (best is None or ratio > best[0]):
                    best = (ratio, lv, nm)
        return (best[1], best[2]) if best else (0, None)

    items: list[dict[str, Any]] = []
    total_w = 0.0
    got_w = 0.0
    for b in required:
        w = b.get("weight")
        w = float(w) if isinstance(w, (int, float)) else 0.0
        req = b.get("required_level") or 0
        ulv, via = _user_level_for(b.get("skill_key") or "")
        if req:
            ratio = min(ulv / req, 1.0) if ulv else 0.0
        else:
            ratio = 1.0 if ulv else 0.0
        total_w += w
        got_w += ratio * w
        items.append(
            {
                "skill_key": b.get("skill_key"),
                "category": b.get("category"),
                "required_level": req,
                "user_level": ulv,
                "weight": w,
                "weight_pct": b.get("weight_pct"),
                "is_core": b.get("is_core"),
                "ratio": round(ratio, 3),
                "ok": ratio >= 1.0,
                "matched_by": via,
            }
        )

    score = round(100 * got_w / total_w, 1) if total_w else 0.0
    # 画像命中了几项：一项没命中时 score 必然是 0，但那是「没有证据」而非
    # 「完全不匹配」，调用方应据此显示「未评估」而不是刺眼的 0%
    covered = [i for i in items if (i.get("user_level") or 0) > 0]
    covered_w = sum(i["weight"] for i in covered)
    strengths = [i for i in items if i["ok"]]
    gaps = sorted(
        (i for i in items if not i["ok"]), key=lambda x: -(x["weight"] or 0)
    )
    # 按技能大类聚合达标率 → 诊断雷达图的真实轴
    by_cat: dict[str, list[float]] = {}
    for i in items:
        by_cat.setdefault(i["category"] or "未分类", []).append(i["ratio"])
    radar = {
        "categories": list(by_cat),
        "scores": [round(100 * sum(v) / len(v)) for v in by_cat.values()],
    }
    return {
        "occupation": {
            "id": occ.get("id"),
            "name": occ.get("name"),
            "level": occ.get("level"),
        },
        "match_score": score,
        "skill_total": len(items),
        "matched_count": len(strengths),
        # 证据覆盖：画像命中的技能数与权重占比
        "covered_count": len(covered),
        "coverage": round(100 * covered_w / total_w, 1) if total_w else 0.0,
        "items": items,
        "strengths": strengths,
        "gaps": gaps,
        "radar": radar,
    }


def _node_to_profession(n: dict[str, Any]) -> dict[str, Any]:
    a = n.get("attrs") if isinstance(n.get("attrs"), dict) else {}
    return {
        "id": n["id"],
        "name": n.get("display_name") or n.get("name"),
        "raw_name": n.get("name"),
        "type": "profession",
        "kg_type": n.get("type"),
        "region": n.get("region"),
        "status": 1 if (n.get("status") or "published") == "published" else 0,
        "desc": n.get("description"),
        "industry": a.get("category") or a.get("industry"),
        "code": a.get("code"),
        "level": a.get("level"),
        "level_zh": a.get("level_zh"),
        "source_url": n.get("source_url"),
        "attrs": a,
        "counts": n.get("counts"),
    }


def _node_to_position(n: dict[str, Any]) -> dict[str, Any]:
    a = n.get("attrs") if isinstance(n.get("attrs"), dict) else {}
    industries = n.get("industries") or []
    return {
        "id": n["id"],
        "name": n.get("display_name") or n.get("name"),
        "raw_name": n.get("name"),
        "type": "position",
        "kg_type": n.get("type"),
        "region": n.get("region"),
        "status": 1 if (n.get("status") or "published") != "archived" else 2,
        "desc": n.get("description"),
        "tier": a.get("tier") or a.get("recommend_tier"),
        "demand": a.get("demand"),
        "salary": a.get("salary"),
        "source_url": n.get("source_url"),
        "attrs": a,
        "edge": n.get("edge"),
        "counts": n.get("counts"),
        "industries": industries,
        "industry_id": n.get("industry_id")
        or (industries[0]["id"] if industries else None),
        "industry_name": n.get("industry_name")
        or (industries[0].get("name") if industries else None),
    }


def _node_to_skill(n: dict[str, Any]) -> dict[str, Any]:
    a = n.get("attrs") if isinstance(n.get("attrs"), dict) else {}
    name = n.get("name") or ""
    skill_name = a.get("skill_name") or name.split("·")[0].strip()
    level_label = a.get("level_code") or (
        name.split("·")[-1].strip() if "·" in name else None
    )
    return {
        "id": n["id"],
        "name": n.get("display_name") or name,
        "skill_name": skill_name,
        "skill_key": a.get("skill_key") or skill_name,
        "level_label": level_label,
        "type": "skill",
        "kg_type": n.get("type"),
        "region": n.get("region"),
        "desc": n.get("description"),
        "required_level": a.get("required_level"),
        "weight": (n.get("edge") or {}).get("weight")
        if isinstance(n.get("edge"), dict)
        else a.get("weight"),
        "source_url": n.get("source_url"),
        "attrs": a,
        "edge": n.get("edge"),
        "counts": n.get("counts"),
    }


def _bundle_to_skill_out(b: dict[str, Any]) -> dict[str, Any]:
    """逻辑技能 bundle → 学生端 SkillOut 兼容字段。"""
    req = b.get("required_level")
    avail = b.get("available_levels") or []
    # 无要求档时退到该技能最高档；文案统一取自 skill_level_meta（BR-01）
    lv = req or (avail[-1] if avail else None)
    level_label = None
    if lv:
        from backend.kg.pg_store.skill_level_meta import label_map

        level_label = label_map().get(int(lv))
    return {
        "id": b.get("id"),
        "name": b.get("name") or b.get("skill_key"),
        "skill_name": b.get("skill_name") or b.get("skill_key"),
        "skill_key": b.get("skill_key"),
        "level_label": level_label,
        "type": "skill",
        "kg_type": "skill_level",
        "region": b.get("region"),
        "desc": b.get("desc") or b.get("description"),
        "required_level": req,
        "weight": b.get("weight"),
        # 胜任力图谱的「权重」「类别」两列 + 核心技能标记（原型 2.4 / 2.5）
        "weight_pct": b.get("weight_pct"),
        "is_core": b.get("is_core"),
        "category": b.get("category"),
        "source_url": b.get("source_url"),
        "attrs": {
            "skill_key": b.get("skill_key"),
            "level_descriptions": b.get("level_descriptions") or {},
            "available_levels": b.get("available_levels") or [],
            "missing_levels": b.get("missing_levels") or [],
            "node_ids": b.get("node_ids") or [],
        },
        "levels": b.get("levels") or [],
        "level_descriptions": b.get("level_descriptions") or {},
        "available_levels": b.get("available_levels") or [],
        "missing_levels": b.get("missing_levels") or [],
        "counts": b.get("counts"),
        "edge": None,
    }


def _attach_major_counts(
    items: list[dict[str, Any]], *, conn: Any | None = None
) -> None:
    ids = [x["id"] for x in items if x.get("id")]
    cmap = counts_for_majors(ids, conn=conn)
    for x in items:
        x["counts"] = cmap.get(x["id"]) or {
            "major": 0,
            "occupation": 0,
            "skill": 0,
            "industry": 0,
            "course": 0,
            "level": 0,
        }


def _attach_position_extra(
    items: list[dict[str, Any]], *, conn: Any | None = None
) -> None:
    ids = [x["id"] for x in items if x.get("id")]
    cmap = counts_for_occupations(ids, conn=conn)
    ind = industries_for_occupations(ids, conn=conn)
    for x in items:
        x["counts"] = cmap.get(x["id"]) or {
            "major": 0,
            "occupation": 0,
            "skill": 0,
            "industry": 0,
            "course": 0,
            "level": 0,
        }
        inds = ind.get(x["id"]) or []
        x["industries"] = inds
        x["industry_id"] = inds[0]["id"] if inds else None
        x["industry_name"] = inds[0].get("name") if inds else None


def list_professions(
    *,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
    region: str | None = None,
    conn: Any | None = None,
) -> dict[str, Any]:
    # 一条连接跑完 list_nodes + counts_for_majors（原先各开各的）
    with use_conn(conn) as c:
        data = list_nodes(
            node_type="major",
            region=region,
            q=q,
            page=page,
            page_size=page_size,
            published_only=True,
            conn=c,
        )
        items = [_node_to_profession(n) for n in data["items"]]
        _attach_major_counts(items, conn=c)
    return {
        "items": items,
        "page": data["page"],
        "page_size": data["page_size"],
        "total": data["total"],
        "total_pages": data["total_pages"],
    }


def get_profession(pid: str, *, conn: Any | None = None) -> dict[str, Any] | None:
    with use_conn(conn) as c:
        n = get_node(pid, scope="public", conn=c)
        if not n:
            return None
        p = _node_to_profession(n)
        _attach_major_counts([p], conn=c)
    return p


def profession_positions(
    pid: str, limit: int = 50, *, conn: Any | None = None
) -> list[dict[str, Any]]:
    with use_conn(conn) as c:
        rows = major_occupations(pid, limit=limit, conn=c)
        items = [_node_to_position(r) for r in rows]
        _attach_position_extra(items, conn=c)
    return items


def profession_ladder(
    pid: str,
    *,
    positions: list[dict[str, Any]] | None = None,
    conn: Any | None = None,
) -> list[dict[str, Any]]:
    """用关联岗位按名称序模拟成长阶梯（无官方 tier 时）。

    `positions`：调用方已经拿过这份列表就传进来。专业详情路由紧挨着
    `profession_positions()` 调本函数，而本函数内部又查了一遍同一份数据
    （两条连接、两个事务、两轮 counts 聚合）。只取前 4 条，顺序一致，
    传进来的列表可以直接用。
    """
    if positions is None:
        positions = profession_positions(pid, limit=20, conn=conn)
    ladder = []
    for i, p in enumerate(positions[:4], start=1):
        ladder.append(
            {
                "tier": i,
                "position_id": p["id"],
                "position_name": p["name"],
                "position": p,
            }
        )
    return ladder


def list_positions(
    *,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
    region: str | None = None,
    conn: Any | None = None,
) -> dict[str, Any]:
    # 一条连接跑完 list_nodes + counts_for_occupations + industries_for_occupations
    # （文档第 1 条点名的「一次学员列表 2–4 次连接」就是这里）
    with use_conn(conn) as c:
        data = list_nodes(
            node_type="occupation",
            region=region,
            q=q,
            page=page,
            page_size=page_size,
            published_only=True,
            conn=c,
        )
        items = [_node_to_position(n) for n in data["items"]]
        _attach_position_extra(items, conn=c)
    return {
        "items": items,
        "page": data["page"],
        "page_size": data["page_size"],
        "total": data["total"],
        "total_pages": data["total_pages"],
    }


def get_position(pid: str, *, conn: Any | None = None) -> dict[str, Any] | None:
    with use_conn(conn) as c:
        n = get_node(pid, scope="public", conn=c)
        if not n:
            return None
        p = _node_to_position(n)
        _attach_position_extra([p], conn=c)
    return p


def position_skills(
    pid: str, limit: int = 50, *, aggregate: bool = True
) -> list[dict[str, Any]]:
    if aggregate:
        return [
            _bundle_to_skill_out(b)
            for b in occupation_skill_bundles(pid, limit=limit)
        ]
    rows = occupation_skills(pid, limit=limit)
    return [_node_to_skill(r) for r in rows]


def list_skills_page(
    *,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
    region: str | None = None,
    view: str = "bundle",
    occupation_id: str | None = None,
    has_level: str | None = None,
) -> dict[str, Any]:
    view = (view or "bundle").strip().lower()
    if view in ("bundle", "agg", "aggregate", "1", "true"):
        data = list_skill_bundles(
            q=q,
            page=page,
            page_size=page_size,
            region=region,
            occupation_id=occupation_id,
            has_level=has_level,
        )
        return {
            "items": [_bundle_to_skill_out(b) for b in data["items"]],
            "page": data["page"],
            "page_size": data["page_size"],
            "total": data["total"],
            "total_pages": data["total_pages"],
            "view": "bundle",
        }
    data = list_nodes(
        node_type="skill_level",
        region=region,
        q=q,
        page=page,
        page_size=page_size,
        published_only=True,
    )
    return {
        "items": [_node_to_skill(n) for n in data["items"]],
        "page": data["page"],
        "page_size": data["page_size"],
        "total": data["total"],
        "total_pages": data["total_pages"],
        "view": "level",
    }


def get_skill_detail(skill_key: str, *, region: str | None = None) -> dict[str, Any] | None:
    b = get_skill_bundle(skill_key, region=region)
    return _bundle_to_skill_out(b) if b else None


def list_industries_page(
    *,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
    region: str | None = None,
    conn: Any | None = None,
) -> dict[str, Any]:
    with use_conn(conn) as c:
        data = list_nodes(
            node_type="industry",
            region=region,
            q=q,
            page=page,
            page_size=page_size,
            published_only=True,
            conn=c,
        )
        ids = [n["id"] for n in data["items"]]
        cmap = counts_for_industries(ids, conn=c)
    items = []
    for n in data["items"]:
        a = n.get("attrs") if isinstance(n.get("attrs"), dict) else {}
        items.append(
            {
                "id": n["id"],
                "name": n.get("name"),
                "code": a.get("code"),
                "level": a.get("level"),
                "parent_code": a.get("parent_code"),
                "desc": n.get("description"),
                "counts": cmap.get(n["id"])
                or {
                    "major": 0,
                    "occupation": 0,
                    "skill": 0,
                    "industry": 0,
                    "course": 0,
                    "level": 0,
                },
            }
        )
    return {
        "items": items,
        "page": data["page"],
        "page_size": data["page_size"],
        "total": data["total"],
        "total_pages": data["total_pages"],
    }


# ── 目标 / 积分 / 成就 ────────────────────────────────────────


def _row_jsonable(row: Any) -> dict[str, Any]:
    d = dict(row)
    for k, v in list(d.items()):
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    return d


def get_goal(user_id: str, occupation_id: str | None = None) -> dict[str, Any] | None:
    """默认取当前活跃目标；给 occupation_id 则取该岗位那条（含已归档的历史目标）。"""
    with connect() as conn:
        if occupation_id:
            row = conn.execute(
                "SELECT * FROM biz_user_goal WHERE user_id=%s AND occupation_id=%s",
                (user_id, occupation_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM biz_user_goal WHERE user_id=%s "
                "ORDER BY (status='active') DESC, updated_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
    if not row:
        return None
    return _row_jsonable(row)


def set_goal(
    user_id: str,
    user_name: str,
    *,
    occupation_id: str,
    major_id: str | None = None,
) -> dict[str, Any]:
    occ = get_node(occupation_id, scope="public")
    if not occ:
        raise ValueError("occupation not found")
    major = get_node(major_id, scope="public") if major_id else None
    with connect() as conn:
        # 换目标不删旧目标，只把它降为 archived：旧目标的测评结果与晋升进度仍要可查
        conn.execute(
            "UPDATE biz_user_goal SET status='archived' "
            "WHERE user_id=%s AND occupation_id <> %s AND status='active'",
            (user_id, occupation_id),
        )
        conn.execute(
            """
            INSERT INTO biz_user_goal (
              user_id, user_name, occupation_id, occupation_name,
              major_id, major_name, status, updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,'active',NOW())
            ON CONFLICT (user_id, occupation_id) DO UPDATE SET
              user_name = EXCLUDED.user_name,
              occupation_name = EXCLUDED.occupation_name,
              major_id = EXCLUDED.major_id,
              major_name = EXCLUDED.major_name,
              status = 'active',
              updated_at = NOW()
            """,
            (
                user_id,
                user_name,
                occupation_id,
                occ.get("name"),
                major_id,
                (major or {}).get("name") if major else None,
            ),
        )
        conn.commit()
    _unlock(user_id, user_name, "first_goal")
    return get_goal(user_id)  # type: ignore[return-value]


def list_goals(user_id: str) -> list[dict[str, Any]]:
    """该用户的全部目标（活跃在前）。原型「当前活跃目标」之外还要能回看历史目标。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM biz_user_goal WHERE user_id=%s "
            "ORDER BY (status='active') DESC, updated_at DESC",
            (user_id,),
        ).fetchall()
    return [_row_jsonable(r) for r in rows]


def clear_goal(user_id: str, occupation_id: str | None = None) -> None:
    with connect() as conn:
        if occupation_id:
            conn.execute(
                "DELETE FROM biz_user_goal WHERE user_id=%s AND occupation_id=%s",
                (user_id, occupation_id),
            )
        else:
            conn.execute("DELETE FROM biz_user_goal WHERE user_id = %s", (user_id,))
        conn.commit()


def _add_points(user_id: str, user_name: str, delta: int) -> int:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO biz_user_points (user_id, user_name, total, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
              total = biz_user_points.total + EXCLUDED.total,
              user_name = EXCLUDED.user_name,
              updated_at = NOW()
            """,
            (user_id, user_name, delta),
        )
        row = conn.execute(
            "SELECT total FROM biz_user_points WHERE user_id = %s", (user_id,)
        ).fetchone()
        conn.commit()
    return int(row["total"]) if row else delta


def _unlock(user_id: str, user_name: str, code: str) -> bool:
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM biz_user_achievement WHERE user_id=%s AND achievement_code=%s",
            (user_id, code),
        ).fetchone()
        if exists:
            return False
        defn = conn.execute(
            "SELECT points FROM biz_achievement_def WHERE code=%s", (code,)
        ).fetchone()
        if not defn:
            return False
        conn.execute(
            """
            INSERT INTO biz_user_achievement (user_id, achievement_code)
            VALUES (%s, %s) ON CONFLICT DO NOTHING
            """,
            (user_id, code),
        )
        conn.commit()
    _add_points(user_id, user_name, int(defn["points"]))
    return True


def me_summary(user_id: str, user_name: str) -> dict[str, Any]:
    goal = get_goal(user_id)
    with connect() as conn:
        pts = conn.execute(
            "SELECT total FROM biz_user_points WHERE user_id=%s", (user_id,)
        ).fetchone()
        badges = conn.execute(
            """
            SELECT a.code, a.name, a.description, a.points, a.category, u.unlocked_at
            FROM biz_user_achievement u
            JOIN biz_achievement_def a ON a.code = u.achievement_code
            WHERE u.user_id = %s
            ORDER BY u.unlocked_at DESC
            """,
            (user_id,),
        ).fetchall()
        skills = conn.execute(
            "SELECT * FROM biz_user_skill WHERE user_id=%s ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
        path = conn.execute(
            """
            SELECT * FROM biz_learning_path
            WHERE user_id=%s AND status='active'
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    return {
        "user_id": user_id,
        "user_name": user_name,
        "goal": goal,
        "points": int(pts["total"]) if pts else 0,
        "badges": [_row_jsonable(b) for b in badges],
        "skills": [_row_jsonable(s) for s in skills],
        "active_path_id": path["id"] if path else None,
    }


def list_badge_defs() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM biz_achievement_def ORDER BY points"
        ).fetchall()
    return [dict(r) for r in rows]


# ── 诊断 ─────────────────────────────────────────────────────

def _parse_resume_skills(text: str) -> list[dict[str, Any]]:
    """简历规则兜底 —— 词表与档位规则见 `backend.agent.skill_keywords`。

    这里原来自带一份 `_SKILL_KW`（8 条），和 `agent/diagnose.py` 那份（10 条）
    早就漂移了：少了 `C#`、汽车维修、航标作业，也不先查技能库。于是写着「汽车维修」
    的简历走对话诊断命中、走简历诊断命不中。现在两条路径共用同一实现。

    import 放函数内：`agent/` 依赖 `kg/pg_store/`，模块级反向 import 会成环。
    """
    from backend.agent.skill_keywords import rule_parse_skills

    return rule_parse_skills(
        text or "",
        evidence_fmt="简历命中关键词规则：{pat}",
        fallback_evidence="未识别到领域关键词，给基础分",
    )


def create_resume_diagnosis(
    user_id: str,
    user_name: str,
    *,
    content_text: str,
    target_occupation_id: str | None = None,
) -> dict[str, Any]:
    occ_name = None
    if target_occupation_id:
        o = get_node(target_occupation_id, scope="public")
        occ_name = (o or {}).get("name")
    # AI 网关 create_react_agent 优先，失败/未配置则规则
    try:
        from backend.agent.diagnose import run_resume_diagnose

        agent_out = run_resume_diagnose(
            content_text or "",
            target_occupation_id=target_occupation_id,
            target_occupation_name=occ_name,
        )
        skills = agent_out.get("skills") or _parse_resume_skills(content_text or "")
        agent_meta = agent_out.get("meta") or {}
        agent_summary = agent_out.get("summary")
    except Exception as e:
        skills = _parse_resume_skills(content_text or "")
        agent_meta = {"engine": "rule", "error": str(e)[:200]}
        agent_summary = None
    return _persist_resume_diagnosis(
        user_id,
        user_name,
        content_text=content_text or "",
        target_occupation_id=target_occupation_id,
        occ_name=occ_name,
        skills=skills,
        agent_meta=agent_meta,
        agent_summary=agent_summary,
    )


def _persist_resume_diagnosis(
    user_id: str,
    user_name: str,
    *,
    content_text: str,
    target_occupation_id: str | None,
    occ_name: str | None,
    skills: list[dict[str, Any]],
    agent_meta: dict[str, Any],
    agent_summary: str | None,
) -> dict[str, Any]:
    """落库：简历资产 + 会话 + 技能画像 + 报告。同步与流式两条路径共用此段。"""
    with connect() as conn:
        res = conn.execute(
            """
            INSERT INTO biz_resume_asset (user_id, content_text, parse_json, status)
            VALUES (%s, %s, %s::jsonb, 'parsed')
            RETURNING id
            """,
            (
                user_id,
                content_text,
                json.dumps(
                    {"skills": skills, "agent_meta": agent_meta}, ensure_ascii=False
                ),
            ),
        ).fetchone()
        sess = conn.execute(
            """
            INSERT INTO biz_diagnosis_session
              (user_id, user_name, channel, target_occupation_id, target_occupation_name, status, finished_at)
            VALUES (%s, %s, 'resume', %s, %s, 'finished', NOW())
            RETURNING *
            """,
            (user_id, user_name, target_occupation_id, occ_name),
        ).fetchone()
        sid = sess["id"]
        # upsert user skills（skill_key 维度）
        for s in skills:
            sid_key = f"skill_key:{(s.get('skill_name') or '').strip()}"
            conn.execute(
                """
                INSERT INTO biz_user_skill (user_id, skill_id, skill_name, level, score, source, updated_at)
                VALUES (%s,%s,%s,%s,%s,'resume',NOW())
                ON CONFLICT (user_id, skill_id) DO UPDATE SET
                  level = EXCLUDED.level, score = EXCLUDED.score,
                  source = EXCLUDED.source, updated_at = NOW()
                """,
                (user_id, sid_key, s["skill_name"], s["level"], s["score"]),
            )
        # 先提交技能画像：_build_report → position_match 另开连接读 biz_user_skill，
        # 未提交则读不到本次解析结果，匹配度会恒为 0。
        conn.commit()
        report = _build_report(user_id, target_occupation_id, skills, channel="resume")
        if agent_summary:
            report["summary"] = agent_summary
        report["agent_meta"] = agent_meta
        conn.execute(
            """
            INSERT INTO biz_diagnosis_result
              (session_id, match_score, gap_json, radar_json, evidence_json, report_json)
            VALUES (%s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb)
            ON CONFLICT (session_id) DO UPDATE SET
              match_score = EXCLUDED.match_score,
              gap_json = EXCLUDED.gap_json,
              report_json = EXCLUDED.report_json
            """,
            (
                sid,
                report["match_score"],
                json.dumps(report["gaps"], ensure_ascii=False),
                json.dumps(report["radar"], ensure_ascii=False),
                json.dumps(
                    {"resume_id": res["id"], "agent_meta": agent_meta},
                    ensure_ascii=False,
                ),
                json.dumps(report, ensure_ascii=False),
            ),
        )
        conn.commit()
    _unlock(user_id, user_name, "first_diag")
    return {
        "session_id": sid,
        "channel": "resume",
        "resume_id": res["id"],
        "parsed_skills": skills,
        "report": report,
        "agent_meta": agent_meta,
    }


def save_learning_plan(
    user_id: str,
    occupation_id: str,
    plan_id: str,
    *,
    session_id: int | None = None,
    gap_skills: list[str] | None = None,
    source: str = "api",
) -> dict[str, Any]:
    """记录「学员 × 岗位 × 学习计划」的关联。

    学习计划内容由外部服务持有，这里只存 plan_id 与生成依据，便于列表回显、
    以及从综合能力报告追到它衍生出的计划。
    """
    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO biz_user_learning_plan
              (user_id, occupation_id, plan_id, session_id, gap_skills, source)
            VALUES (%s,%s,%s,%s,%s::jsonb,%s)
            ON CONFLICT (user_id, occupation_id, plan_id) DO UPDATE SET
              session_id = EXCLUDED.session_id,
              gap_skills = EXCLUDED.gap_skills,
              source = EXCLUDED.source,
              created_at = NOW()
            RETURNING *
            """,
            (
                user_id,
                occupation_id,
                plan_id,
                session_id,
                json.dumps(gap_skills or [], ensure_ascii=False),
                source,
            ),
        ).fetchone()
        # 报告里也留一份，方便「这份报告生成过哪个计划」独立可查
        if session_id:
            conn.execute(
                """
                UPDATE biz_diagnosis_result
                   SET report_json = jsonb_set(
                         COALESCE(report_json, '{}'::jsonb), '{learning_plan_id}', to_jsonb(%s::text), true)
                 WHERE session_id = %s
                """,
                (plan_id, session_id),
            )
        conn.commit()
    return _row_jsonable(row)


def list_learning_plans(user_id: str, occupation_id: str | None = None) -> list[dict[str, Any]]:
    with connect() as conn:
        if occupation_id:
            rows = conn.execute(
                "SELECT * FROM biz_user_learning_plan WHERE user_id=%s AND occupation_id=%s "
                "ORDER BY created_at DESC",
                (user_id, occupation_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM biz_user_learning_plan WHERE user_id=%s ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
    return [_row_jsonable(r) for r in rows]


def create_assessment_session(
    user_id: str, user_name: str, *, target_occupation_id: str | None = None
) -> int:
    """建一条测评会话，返回 id —— 同时用作 LangGraph 的 thread_id。

    复用 biz_diagnosis_session（channel='assessment'）而不是另起一张表：
    报告落库、历史查询、学习计划等下游都已按这张表实现。
    """
    occ_name = None
    if target_occupation_id:
        occ_name = (get_node(target_occupation_id, scope="public") or {}).get("name")
    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO biz_diagnosis_session
              (user_id, user_name, channel, target_occupation_id, target_occupation_name, status)
            VALUES (%s, %s, 'assessment', %s, %s, 'active')
            RETURNING id
            """,
            (user_id, user_name, target_occupation_id, occ_name),
        ).fetchone()
        conn.commit()
    return int(row["id"])


def session_meta(session_id: int) -> dict[str, Any] | None:
    """会话的归属人与目标岗位；会话不存在返回 None。

    两个用途都要求「以库为准，不信调用方传来的值」：

    - **归属人**：session_id 是 BIGSERIAL 自增值，前端拿到的就是「隔壁那个人的 id 减一」。
      题目、作答、能力报告都挂在它下面，不校验归属等于把别人的测评向任何一个登录
      用户敞开——既能读走报告，也能替别人答题污染其画像。
    - **目标岗位**：结算接口的 `occupation_id` 是可传的查询参数。用它去取技能构成，
      就会拿 B 岗位的标准去给 A 岗位的作答打分，算出一份看起来正常的错报告。
    """
    if not isinstance(session_id, int) or session_id <= 0:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT user_id, target_occupation_id FROM biz_diagnosis_session WHERE id = %s",
            (session_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "user_id": str(row["user_id"]),
        "target_occupation_id": row["target_occupation_id"],
    }


def save_assessment_report(session_id: int, user_id: str, report: dict[str, Any]) -> None:
    """测评收敛后落库：写报告 + 结束会话 + 更新技能画像。"""
    measured = [
        {
            "skill_name": i.get("skill_key"),
            "level": i.get("measured_level"),
            "score": int((i.get("ratio") or 0) * 100),
        }
        for i in (report.get("items") or [])
        if i.get("tested") and i.get("measured_level")
    ]
    with connect() as conn:
        conn.execute(
            "UPDATE biz_diagnosis_session SET status='finished', finished_at=NOW() WHERE id=%s",
            (session_id,),
        )
        conn.execute(
            """
            INSERT INTO biz_diagnosis_result
              (session_id, match_score, gap_json, radar_json, evidence_json, report_json)
            VALUES (%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb)
            ON CONFLICT (session_id) DO UPDATE SET
              match_score=EXCLUDED.match_score,
              gap_json=EXCLUDED.gap_json,
              radar_json=EXCLUDED.radar_json,
              report_json=EXCLUDED.report_json
            """,
            (
                session_id,
                report.get("match_score"),
                json.dumps(report.get("gaps") or [], ensure_ascii=False),
                json.dumps(report.get("radar") or {}, ensure_ascii=False),
                json.dumps({"skills": measured}, ensure_ascii=False),
                json.dumps(report, ensure_ascii=False),
            ),
        )
        for s in measured:
            conn.execute(
                """
                INSERT INTO biz_user_skill (user_id, skill_id, skill_name, level, score, source, updated_at)
                VALUES (%s,%s,%s,%s,%s,'assessment',NOW())
                ON CONFLICT (user_id, skill_id) DO UPDATE SET
                  level=EXCLUDED.level, score=EXCLUDED.score,
                  source=EXCLUDED.source, updated_at=NOW()
                """,
                (user_id, f"skill_key:{s['skill_name']}", s["skill_name"], s["level"], s["score"]),
            )
        conn.commit()
    _unlock(user_id, user_id, "first_diag")


def create_chat_session(
    user_id: str,
    user_name: str,
    *,
    target_occupation_id: str | None = None,
) -> dict[str, Any]:
    occ_name = None
    if target_occupation_id:
        o = get_node(target_occupation_id, scope="public")
        occ_name = (o or {}).get("name")
    with connect() as conn:
        sess = conn.execute(
            """
            INSERT INTO biz_diagnosis_session
              (user_id, user_name, channel, target_occupation_id, target_occupation_name, status)
            VALUES (%s, %s, 'chat', %s, %s, 'active')
            RETURNING *
            """,
            (user_id, user_name, target_occupation_id, occ_name),
        ).fetchone()
        q = (
            f"围绕岗位「{occ_name or '目标岗位'}」，请用一段具体经历说明你做过的关键工作与结果。"
            if occ_name
            else "请描述一段你最有代表性的工作/实习经历，包含任务、方法与结果。"
        )
        conn.execute(
            """
            INSERT INTO biz_chat_message (session_id, role, content)
            VALUES (%s, 'assistant', %s)
            """,
            (sess["id"], q),
        )
        conn.commit()
    return {
        "session_id": sess["id"],
        "channel": "chat",
        "status": "active",
        "target_occupation_id": target_occupation_id,
        "first_question": q,
    }


def _chat_prepare(
    session_id: int, user_id: str, content: str
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """存学员本轮回答并取回会话与近期历史。同步与流式两条路径共用。

    单独提交：流式路径要先把这一轮回答落库，再花几秒跑模型，
    中途断连也不至于丢掉学员输入。
    """
    with connect() as conn:
        sess = conn.execute(
            "SELECT * FROM biz_diagnosis_session WHERE id=%s AND user_id=%s",
            (session_id, user_id),
        ).fetchone()
        if not sess:
            raise ValueError("session not found")
        conn.execute(
            "INSERT INTO biz_chat_message (session_id, role, content) VALUES (%s,'user',%s)",
            (session_id, content),
        )
        hist_rows = conn.execute(
            """
            SELECT role, content FROM biz_chat_message
            WHERE session_id=%s ORDER BY id DESC LIMIT 8
            """,
            (session_id,),
        ).fetchall()
        history = [
            {"role": r["role"], "content": r["content"]}
            for r in reversed(list(hist_rows))
        ]
        conn.commit()
    return dict(sess), history


def post_chat_message(
    session_id: int, user_id: str, content: str
) -> dict[str, Any]:
    sess, history = _chat_prepare(session_id, user_id, content)
    try:
        from backend.agent.diagnose import run_chat_diagnose

        chat_out = run_chat_diagnose(
            content,
            target_occupation_id=sess.get("target_occupation_id"),
            target_occupation_name=sess.get("target_occupation_name"),
            history=history,
        )
        skills = chat_out.get("skills") or _parse_resume_skills(content)
        score = int(chat_out.get("score") or min(100, 40 + 10 * len(skills)))
        reply = chat_out.get("reply") or f"已记录 {len(skills)} 项技能线索。"
        agent_meta = chat_out.get("meta") or {}
    except Exception as e:
        skills = _parse_resume_skills(content)
        score = min(100, 40 + 10 * len(skills))
        reply = f"回答已记录（规则）。技能线索 {len(skills)} 项。"
        agent_meta = {"engine": "rule", "error": str(e)[:200]}
    return _persist_chat_message(
        sess, user_id, skills=skills, score=score, reply=reply, agent_meta=agent_meta
    )


def _persist_chat_message(
    sess: dict[str, Any],
    user_id: str,
    *,
    skills: list[dict[str, Any]],
    score: int,
    reply: str,
    agent_meta: dict[str, Any],
) -> dict[str, Any]:
    """落库：助手回复 + 报告 + 技能画像，并结束会话。两条路径共用。"""
    session_id = sess["id"]
    with connect() as conn:
        conn.execute(
            "INSERT INTO biz_chat_message (session_id, role, content, meta_json) VALUES (%s,'assistant',%s,%s::jsonb)",
            (
                session_id,
                reply,
                json.dumps(
                    {"skills": skills, "score": score, "agent_meta": agent_meta},
                    ensure_ascii=False,
                ),
            ),
        )
        # 同 resume 流程：先提交技能画像，否则 position_match 读不到本轮结果
        conn.commit()
        # finish after user message
        report = _build_report(
            user_id, sess.get("target_occupation_id"), skills, channel="chat"
        )
        conn.execute(
            "UPDATE biz_diagnosis_session SET status='finished', finished_at=NOW() WHERE id=%s",
            (session_id,),
        )
        conn.execute(
            """
            INSERT INTO biz_diagnosis_result
              (session_id, match_score, gap_json, radar_json, evidence_json, report_json)
            VALUES (%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb)
            ON CONFLICT (session_id) DO UPDATE SET match_score=EXCLUDED.match_score, report_json=EXCLUDED.report_json
            """,
            (
                session_id,
                report["match_score"],
                json.dumps(report["gaps"], ensure_ascii=False),
                json.dumps(report["radar"], ensure_ascii=False),
                json.dumps({"skills": skills}, ensure_ascii=False),
                json.dumps(report, ensure_ascii=False),
            ),
        )
        for s in skills:
            conn.execute(
                """
                INSERT INTO biz_user_skill (user_id, skill_id, skill_name, level, score, source, updated_at)
                VALUES (%s,%s,%s,%s,%s,'chat',NOW())
                ON CONFLICT (user_id, skill_id) DO UPDATE SET
                  -- 以最近一次诊断为准，直接覆盖。此前用 GREATEST「只升不降」，
                  -- 历史高分会永久留在画像里，用户重测也降不下来，导致
                  -- 顶部按画像算的匹配度长期高于实测报告（曾出现 53% vs 28%）。
                  level=EXCLUDED.level,
                  score=EXCLUDED.score,
                  updated_at=NOW()
                """,
                (
                    user_id,
                    f"parsed:{s['skill_name']}",
                    s["skill_name"],
                    s["level"],
                    s["score"],
                ),
            )
        conn.commit()
    _unlock(user_id, sess["user_name"] or user_id, "first_diag")
    return {"reply": reply, "session_id": session_id, "status": "finished", "report": report}


def get_diagnosis_report(
    user_id: str, *, session_id: int | None = None, occupation_id: str | None = None
) -> dict[str, Any] | None:
    with connect() as conn:
        if session_id:
            row = conn.execute(
                """
                SELECT r.*, s.channel, s.target_occupation_id, s.target_occupation_name
                FROM biz_diagnosis_result r
                JOIN biz_diagnosis_session s ON s.id = r.session_id
                WHERE r.session_id=%s AND s.user_id=%s
                """,
                (session_id, user_id),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT r.*, s.channel, s.target_occupation_id, s.target_occupation_name
                FROM biz_diagnosis_result r
                JOIN biz_diagnosis_session s ON s.id = r.session_id
                WHERE s.user_id=%s
                ORDER BY r.created_at DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
    if not row:
        # on-the-fly from skills + occupation
        with connect() as conn:
            skills = conn.execute(
                "SELECT skill_name, level, score FROM biz_user_skill WHERE user_id=%s",
                (user_id,),
            ).fetchall()
        if not skills and not occupation_id:
            return None
        sk = [dict(s) for s in skills]
        return _build_report(user_id, occupation_id, sk, channel="profile")
    rep = row.get("report_json") or {}
    if isinstance(rep, str):
        rep = json.loads(rep)
    return rep


def _build_report(
    user_id: str,
    occupation_id: str | None,
    user_skills: list[dict[str, Any]],
    *,
    channel: str,
) -> dict[str, Any]:
    required: list[dict[str, Any]] = []
    occ_name = None
    if occupation_id:
        occ = get_node(occupation_id, scope="public")
        occ_name = (occ or {}).get("name")
        required = position_skills(occupation_id, limit=30)
    # simple match: name token overlap
    u_names = {(s.get("skill_name") or s.get("name") or "").lower() for s in user_skills}
    gaps = []
    matched = 0
    for r in required:
        rn = (r.get("skill_name") or r.get("name") or "").lower()
        hit = any(u and (u in rn or rn in u) for u in u_names if u)
        if hit:
            matched += 1
        else:
            gaps.append(
                {
                    "skill_id": r.get("id"),
                    "skill_name": r.get("skill_name") or r.get("name"),
                    "required_weight": r.get("weight"),
                    "suggestion": "建议通过课程/实操补齐该技能",
                }
            )
    total_req = max(len(required), 1)
    match_score = round(100 * matched / total_req, 1) if required else 55.0
    # 雷达图：按技能大类聚合真实达标率。
    # 旧实现是「写死 6 类 + 每轴同一个占位分」，与库内 10 类分类对不上，图形没有信息量。
    radar = {"categories": [], "scores": []}
    if occupation_id:
        try:
            pm = position_match(user_id, occupation_id)
            match_score = pm["match_score"]      # 加权匹配度，替代「命中数/需求数」
            radar = pm["radar"]
            gaps = [
                {
                    "skill_id": g.get("skill_key"),
                    "skill_name": g.get("skill_key"),
                    "category": g.get("category"),
                    "required_level": g.get("required_level"),
                    "user_level": g.get("user_level"),
                    "required_weight": g.get("weight"),
                    "weight_pct": g.get("weight_pct"),
                    "is_core": g.get("is_core"),
                    "suggestion": "建议通过课程/实操补齐该技能",
                }
                for g in pm["gaps"]
            ]
        except Exception:
            pass
    if not radar["categories"]:
        cats = [c["name"] for c in skill_categories()]
        radar = {"categories": cats, "scores": [0] * len(cats)}
    return {
        "user_id": user_id,
        "channel": channel,
        "target_occupation_id": occupation_id,
        "target_occupation_name": occ_name,
        "match_score": match_score,
        "user_skills": user_skills,
        "required_skills": required,
        "gaps": gaps,
        "radar": radar,
        "summary": f"综合匹配度约 {match_score}%（规则引擎，非大模型终态）",
    }


# ── 学习路径 ─────────────────────────────────────────────────


def _courses_for_skill_key(skill_key: str, limit: int = 2) -> list[dict[str, Any]]:
    """技能→课程：taught_by / related_to，按 skill_key 找 skill_level 再扩边。"""
    if not skill_key:
        return []
    with connect() as conn:
        # attrs 为 TEXT，安全转 json
        rows = conn.execute(
            """
            SELECT c.id, c.name, c.source_url, e.rel_type
            FROM kg_node s
            JOIN kg_edge e ON e.rel_type IN ('taught_by', 'related_to')
              AND (e.src_id = s.id OR e.dst_id = s.id)
            JOIN kg_node c ON c.type = 'course'
              AND c.id = CASE WHEN e.src_id = s.id THEN e.dst_id ELSE e.src_id END
              AND COALESCE(c.status,'published') = 'published'
            WHERE s.type = 'skill_level'
              AND COALESCE(s.status,'published') = 'published'
              AND (
                s.name LIKE %s
                OR (
                  s.attrs IS NOT NULL AND btrim(s.attrs) <> ''
                  AND (
                    COALESCE(s.attrs::json->>'skill_name','') = %s
                    OR COALESCE(s.attrs::json->>'skill_key','') = %s
                  )
                )
              )
            LIMIT %s
            """,
            (f"%{skill_key}%", skill_key, skill_key, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def generate_path(
    user_id: str,
    user_name: str,
    *,
    occupation_id: str | None = None,
) -> dict[str, Any]:
    if not occupation_id:
        g = get_goal(user_id)
        occupation_id = (g or {}).get("occupation_id")
    if not occupation_id:
        raise ValueError("occupation_id required (or set goal first)")
    occ = get_node(occupation_id, scope="public")
    if not occ:
        raise ValueError("occupation not found")
    skills = position_skills(occupation_id, limit=12, aggregate=True)
    gaps_report = get_diagnosis_report(user_id, occupation_id=occupation_id) or {}
    gap_names = {
        (g.get("skill_name") or "").lower()
        for g in (gaps_report.get("gaps") or [])
        if g.get("skill_name")
    }
    gap_ids = {g.get("skill_id") for g in gaps_report.get("gaps") or []}

    def _prio(s: dict[str, Any]) -> tuple:
        nm = (s.get("skill_name") or s.get("skill_key") or s.get("name") or "").lower()
        is_gap = 0 if (s.get("id") in gap_ids or any(g and (g in nm or nm in g) for g in gap_names)) else 1
        w = s.get("weight")
        try:
            wf = -float(w) if w is not None else 0.0
        except (TypeError, ValueError):
            wf = 0.0
        return (is_gap, wf)

    ordered = sorted(skills, key=_prio)

    # 阶段划分：按技能大类分组，阶段顺序沿用国标「职业功能」推进顺序
    # （安全环保 → 作业准备 → 操作加工 → 检修/质检 → 技术管理 → 培训指导），
    # 与技能前置关系、技能图谱的分区顺序同源，学员看到的先后是一致的。
    from backend.kg.pg_store.skill_taxonomy import category_rank

    ordered = ordered[:8]
    stage_of: dict[str, int] = {}
    for cat in sorted(
        {(s.get("category") or "未分类") for s in ordered}, key=category_rank
    ):
        stage_of[cat] = len(stage_of) + 1

    # 建议耗时：按目标等级估算（无真实课时数据，标注为估算值）
    _DURATION_BY_LEVEL = {1: 30, 2: 45, 3: 60, 4: 90, 5: 120}

    with connect() as conn:
        conn.execute(
            "UPDATE biz_learning_path SET status='archived' WHERE user_id=%s AND status='active'",
            (user_id,),
        )
        path = conn.execute(
            """
            INSERT INTO biz_learning_path
              (user_id, user_name, occupation_id, occupation_name, status, source)
            VALUES (%s,%s,%s,%s,'active','diagnosis')
            RETURNING *
            """,
            (user_id, user_name, occupation_id, occ.get("name")),
        ).fetchone()
        steps = []
        seq = 0
        for s in ordered:
            sk = s.get("skill_key") or s.get("skill_name") or s.get("name") or "技能"
            seq += 1
            req = s.get("required_level")
            title = f"补齐技能：{sk}"
            if req:
                title += f"（目标 L{req}）"
            cat = s.get("category") or "未分类"
            st = conn.execute(
                """
                INSERT INTO biz_learning_step
                  (path_id, seq, kind, skill_id, skill_name, title, status,
                   stage, stage_title, category, weight, duration_min, required_level)
                VALUES (%s,%s,'skill',%s,%s,%s,'pending',%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (
                    path["id"], seq, s.get("id"), sk, title,
                    stage_of.get(cat), cat, cat,
                    s.get("weight"),
                    _DURATION_BY_LEVEL.get(int(req or 0), 45),
                    req,
                ),
            ).fetchone()
            steps.append(dict(st))
            # 挂可学课程步骤（有边才有；无则跳过——HITL 资源不足）
            for course in _courses_for_skill_key(str(sk), limit=1):
                seq += 1
                ct = f"学习资源：{course.get('name')}"
                st2 = conn.execute(
                    """
                    INSERT INTO biz_learning_step
                      (path_id, seq, kind, skill_id, skill_name, resource_id, resource_title, title, status,
                       stage, stage_title, category, weight, duration_min, required_level)
                    VALUES (%s,%s,'course',%s,%s,%s,%s,%s,'pending',%s,%s,%s,%s,%s,%s)
                    RETURNING *
                    """,
                    (
                        path["id"],
                        seq,
                        s.get("id"),
                        sk,
                        course.get("id"),
                        course.get("name"),
                        ct,
                        stage_of.get(cat),
                        cat,
                        cat,
                        # 课程步骤不重复计权，避免同一技能被算两次
                        0.0,
                        _DURATION_BY_LEVEL.get(int(req or 0), 45),
                        req,
                    ),
                ).fetchone()
                steps.append(dict(st2))
        conn.commit()
    _unlock(user_id, user_name, "first_path")
    return {
        "path": _row_jsonable(path),
        "steps": [_row_jsonable(s) for s in steps],
        "meta": {
            "engine": "rule+graph",
            "note": "技能按缺口/权重排序；课程来自 taught_by/related_to（稀缺时仅技能步）",
            "skill_steps": sum(1 for s in steps if s.get("kind") == "skill"),
            "course_steps": sum(1 for s in steps if s.get("kind") == "course"),
        },
    }


def get_active_path(user_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        path = conn.execute(
            """
            SELECT * FROM biz_learning_path
            WHERE user_id=%s AND status='active'
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        if not path:
            return None
        steps = conn.execute(
            "SELECT * FROM biz_learning_step WHERE path_id=%s ORDER BY seq",
            (path["id"],),
        ).fetchall()
    done = sum(1 for s in steps if s["status"] == "completed")
    rows = [_row_jsonable(s) for s in steps]

    # 阶段任务树（原型 4.8）：按 stage 分组，阶段权重 = 该阶段任务权重和 / 总权重
    total_w = sum(float(s.get("weight") or 0) for s in rows)
    done_w = sum(
        float(s.get("weight") or 0) for s in rows if s.get("status") == "completed"
    )
    stages: dict[int, dict[str, Any]] = {}
    for s in rows:
        stg = s.get("stage") or 0
        g = stages.setdefault(
            stg,
            {
                "stage": stg,
                "title": s.get("stage_title") or s.get("category") or "未分组",
                "steps": [],
                "weight": 0.0,
                "duration_min": 0,
            },
        )
        g["steps"].append(s)
        g["weight"] += float(s.get("weight") or 0)
        g["duration_min"] += int(s.get("duration_min") or 0)
    stage_list = []
    for g in sorted(stages.values(), key=lambda x: x["stage"]):
        g_done = sum(1 for s in g["steps"] if s.get("status") == "completed")
        g["stage_weight_pct"] = round(100 * g["weight"] / total_w) if total_w else 0
        g["completed"] = g_done
        g["total"] = len(g["steps"])
        stage_list.append(g)

    return {
        "path": _row_jsonable(path),
        "steps": rows,
        "stages": stage_list,
        "progress": {
            "completed": done,
            "total": len(steps),
            "ratio": round(done / len(steps), 3) if steps else 0,
            # 原型顶部「35%（完成权重/总权重）」用这个，而非按任务条数
            "weighted_ratio": round(done_w / total_w, 3) if total_w else 0,
            "weighted_pct": round(100 * done_w / total_w) if total_w else 0,
            "duration_min_total": sum(int(s.get("duration_min") or 0) for s in rows),
        },
    }


def complete_step(user_id: str, user_name: str, step_id: int) -> dict[str, Any]:
    with connect() as conn:
        st = conn.execute(
            """
            SELECT s.*, p.user_id FROM biz_learning_step s
            JOIN biz_learning_path p ON p.id = s.path_id
            WHERE s.id=%s AND p.user_id=%s
            """,
            (step_id, user_id),
        ).fetchone()
        if not st:
            raise ValueError("step not found")
        conn.execute(
            "UPDATE biz_learning_step SET status='completed', completed_at=NOW() WHERE id=%s",
            (step_id,),
        )
        conn.commit()
    _unlock(user_id, user_name, "first_step")
    # streak-ish
    with connect() as conn:
        n = conn.execute(
            """
            SELECT COUNT(*) AS c FROM biz_learning_step s
            JOIN biz_learning_path p ON p.id=s.path_id
            WHERE p.user_id=%s AND s.status='completed'
            """,
            (user_id,),
        ).fetchone()
    if n and int(n["c"]) >= 3:
        _unlock(user_id, user_name, "streak_3")
    return get_active_path(user_id)  # type: ignore[return-value]


def list_resources(
    *, skill_id: str | None = None, q: str | None = None, page: int = 1, page_size: int = 20
) -> dict[str, Any]:
    """学习资源：优先 KG course 节点。"""
    data = list_nodes(
        node_type="course", q=q, page=page, page_size=page_size, published_only=True
    )
    items = []
    for n in data["items"]:
        a = n.get("attrs") if isinstance(n.get("attrs"), dict) else {}
        items.append(
            {
                "id": n["id"],
                "title": n.get("name"),
                "type": a.get("role") or "course",
                "status": 1,
                "provider": n.get("source_system"),
                "url": n.get("source_url"),
                "skill_hint": skill_id,
                "desc": n.get("description"),
            }
        )
    return {
        "items": items,
        "page": data["page"],
        "page_size": data["page_size"],
        "total": data["total"],
        "total_pages": data["total_pages"],
    }


# ── 管理看板 ─────────────────────────────────────────────────


def admin_dashboard() -> dict[str, Any]:
    from backend.kg.pg_store.migrate import stats as kg_stats

    kg = kg_stats()
    with connect() as conn:
        users_goal = conn.execute("SELECT COUNT(*) AS c FROM biz_user_goal").fetchone()["c"]
        diag = conn.execute("SELECT COUNT(*) AS c FROM biz_diagnosis_session").fetchone()["c"]
        paths = conn.execute("SELECT COUNT(*) AS c FROM biz_learning_path").fetchone()["c"]
        pend = 0
        try:
            pending = conn.execute(
                "SELECT COUNT(*) AS c FROM kg_change_request"
            ).fetchone()
            pend = int(pending["c"]) if pending else 0
        except Exception:
            try:
                pending = conn.execute(
                    "SELECT COUNT(*) AS c FROM kg_proposal WHERE status='pending'"
                ).fetchone()
                pend = int(pending["c"]) if pending else 0
            except Exception:
                pend = 0
    return {
        "kg_nodes": kg.get("nodes"),
        "kg_edges": kg.get("edges"),
        "nodes_by_type": kg.get("nodes_by_type"),
        "users_with_goal": int(users_goal),
        "diagnosis_sessions": int(diag),
        "learning_paths": int(paths),
        "pending_proposals": pend,
    }
