"""业务表读写（学员/运营）。"""
from __future__ import annotations

import json
from typing import Any

from backend.kg.pg_store.biz_ddl import ACHIEVEMENT_SEEDS, BIZ_SCHEMA_SQL
from backend.kg.pg_store.client import connect, ensure_schema, use_conn
from backend.kg.pg_store.config import (
    as_level,
    as_weight,
    attrs_level_int,
    degrade_for_baseline_gap,
    edge_published,
    node_published,
    weighted_score,
)
from backend.kg.pg_store.occupation_level_meta import level_code as occ_level_code
from backend.kg.pg_store.occupation_level_meta import level_name as occ_level_name
from backend.kg.pg_store.counts import (
    counts_for_industries,
    counts_for_majors,
    counts_for_occupations,
    industries_for_occupations,
    occupation_in_industry_sql,
    progressions_for_occupations,
    top_skills_for_occupations,
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
        # 技能分类字典：真源在 skill_taxonomy.py，这里幂等灌进表供连表取名。
        # 只 upsert 不 delete —— 管理台自建的分类不该被下次启动抹掉。
        from backend.kg.pg_store.skill_taxonomy import all_categories

        for c in all_categories():
            conn.execute(
                """
                INSERT INTO kg_skill_category
                       (code, name, description, sort_order, is_fallback)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (code) DO UPDATE SET
                  name = EXCLUDED.name,
                  description = EXCLUDED.description,
                  sort_order = EXCLUDED.sort_order,
                  is_fallback = EXCLUDED.is_fallback,
                  updated_at = NOW()
                """,
                (c["code"], c["name"], c["description"], c["sort_order"], c["is_fallback"]),
            )
        conn.commit()


# ── 产品元数据（BR-01：唯一来源 skill_level_meta 模块）────────


def skill_level_meta() -> list[dict[str, Any]]:
    from backend.kg.pg_store.skill_level_meta import skill_level_meta as _meta

    return _meta()


def skill_categories(*, q: str | None = None, with_counts: bool = True) -> list[dict[str, Any]]:
    """技能分类字典，**以 `kg_skill_category` 表为准**。

    `kg_node.category` 存 code，这里连表取 name —— 改分类名不用动一行技能数据。
    统计按 **逻辑技能**（`attrs.skill_key` 去重）算，不是按节点：一个技能在库里是
    L1–L5 五个节点，按节点数会把每个分类的规模虚报五倍。

    历史坑：这里曾写死「运营策略/数据能力/内容创作…」6 类互联网口径，与库里的
    国标分类对不上，诊断雷达图的轴全是空的。所以字典与存量必须同源。
    """
    sql = """
        SELECT c.code, c.name, c.description, c.sort_order, c.is_fallback,
               COALESCE(s.n_cnt, 0)::int AS skill_count
        FROM kg_skill_category c
        LEFT JOIN (
            -- 别名必须叫 n：SKILL_KEY_SQL 里写死了 n.attrs / n.name
            SELECT n.category, count(DISTINCT ({skill_key})) AS n_cnt
            FROM kg_node n
            WHERE n.type = 'skill_level' AND COALESCE(n.status,'published') = 'published'
            GROUP BY n.category
        ) s ON s.category = c.code
        WHERE COALESCE(c.status,'published') = 'published'
        {flt}
        ORDER BY c.sort_order, c.code
    """
    from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL

    params: list[Any] = []
    flt = ""
    if q:
        # 管理台检索：名字或 code 命中即可
        flt = "AND (c.name ILIKE %s OR c.code ILIKE %s OR c.description ILIKE %s)"
        params += [f"%{q}%"] * 3
    with connect() as conn:
        rows = conn.execute(sql.format(skill_key=SKILL_KEY_SQL, flt=flt), params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if not with_counts:
            d.pop("skill_count", None)
        # id 保留给旧前端；code 才是主键
        d["id"] = d["code"]
        out.append(d)
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

    脏值解析（`config.as_level` / `as_weight`）与总分口径（`config.weighted_score`）
    都与 `report.build_report` 共用同一份实现：口径统一过一次还不够，**解析规则不统一
    的话，一条脏数据进来两个数字照样分家**。要求档缺失或越界的技能没有基准、
    算不出达标率，分子分母都不计，改由 `score_status` / `no_baseline` 显式表达。

    缺基准的权重占比超过 `config.PARTIAL_BASELINE_PCT`（30%）时，分数照给但
    `score_status` 降级成 `partial_baseline`（「配了一部分，仅供参考」）——
    判断同样收在 `config.degrade_for_baseline_gap`，两条读路径不许各定阈值。
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
    total_w = 0.0        # 分母：**有可信要求档**的技能权重
    got_w = 0.0
    all_w = 0.0          # 岗位全部技能权重，只用来算「多少权重因缺基准无法评分」
    nb_w = 0.0
    for b in required:
        w = as_weight(b.get("weight"))
        req = as_level(b.get("required_level"))
        raw_ulv, via = _user_level_for(b.get("skill_key") or "")
        # 实测档也要过 as_level。上一轮只统一了「要求档」，漏了这一半：
        # `biz_user_skill.level` 是无 CHECK 约束的 int 列，`memory_levels` 那一路
        # 更是 LLM 解析产物。漏夹的后果不是崩而是**错得看不出来**——
        #   9   → ratio 1.0，匹配度 100%（报告侧同一数据算 0）
        #   -3  → ratio -1.0，配上另一项有证据的技能总分能算成 -100%
        #   "3" → ulv / req 直接 TypeError，整页 500
        ulv = as_level(raw_ulv) or 0
        if req:
            ratio = min(ulv / req, 1.0) if ulv else 0.0
        else:
            # 「无要求档 + 有证据 = 达成比 1.0」是既有展示语义，保持不动；
            # 但它不进分子分母了——没有基准就没有达标可言（见 docstring）
            ratio = 1.0 if ulv else 0.0
        all_w += w
        if req:
            total_w += w
            got_w += ratio * w
        else:
            nb_w += w
        items.append(
            {
                "skill_key": b.get("skill_key"),
                "category": b.get("category"),
                "required_level": req or 0,
                "user_level": ulv,
                "weight": w,
                "weight_pct": b.get("weight_pct"),
                "is_core": b.get("is_core"),
                "ratio": round(ratio, 3),
                "ok": bool(req and ulv and ulv >= req),
                "scorable": bool(req),
                "matched_by": via,
            }
        )

    scorable = [i for i in items if i["scorable"]]
    no_baseline = sorted(
        (i for i in items if not i["scorable"]), key=lambda x: -(x["weight"] or 0)
    )
    # 画像命中了几项：一项没命中时 score 必然是 0，但那是「没有证据」而非
    # 「完全不匹配」，调用方应据此显示「未评估」而不是刺眼的 0%
    covered = [i for i in scorable if (i.get("user_level") or 0) > 0]
    covered_w = sum(i["weight"] for i in covered)
    score, score_status = weighted_score(
        skill_total=len(items),
        scorable_total=len(scorable),
        total_w=total_w,
        got_w=got_w,
        has_evidence=bool(covered),
    )
    # 基准缺口：缺要求档、无法评分的权重占岗位全部技能权重的百分之多少。
    # 权重全为 0（脏数据）时退回按项数算，否则这个字段在最需要它的岗位上恒为 0
    no_baseline_weight = (
        round(100 * nb_w / all_w, 1) if all_w
        else (round(100 * len(no_baseline) / len(items), 1) if items else 0.0)
    )
    # 缺口超过阈值就把 ok 降级成 partial_baseline（分数仍照给）：与报告侧同一个
    # 判断函数，否则同一岗位在列表页和报告页一个带「仅供参考」一个不带
    score_status = degrade_for_baseline_gap(score_status, no_baseline_weight)
    # 优势/短板只在有基准的项里分：缺基准既谈不上达标、也谈不上差距
    strengths = [i for i in scorable if i["ok"]]
    gaps = sorted(
        (i for i in scorable if not i["ok"]), key=lambda x: -(x["weight"] or 0)
    )
    # 按技能大类聚合达标率 → 诊断雷达图的真实轴（缺基准的项没有达标率，不上轴）
    by_cat: dict[str, list[float]] = {}
    for i in scorable:
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
        "score_status": score_status,
        "skill_total": len(items),
        "matched_count": len(strengths),
        # 证据覆盖：画像命中的技能数与权重占比（分母是**可评分**权重）
        "covered_count": len(covered),
        "coverage": round(100 * covered_w / total_w, 1) if total_w else 0.0,
        "no_baseline_weight": no_baseline_weight,
        "items": items,
        "strengths": strengths,
        "gaps": gaps,
        "no_baseline": no_baseline,
        "radar": radar,
    }


def has_baseline(required: list[dict[str, Any]]) -> bool:
    """岗位技能构成里是否**至少有一项**配了可信要求档（1–5）。

    库内 608 个有技能构成的岗位中 490 个（80%）一项都没有：老国标 skill_level 节点
    只写了 `attrs.level_code`（"L1"），没有产品档 `attrs.level`，而读侧
    （`config.attrs_level_int`）只认 `attrs.level`。这类岗位算不出匹配度，
    路由要在级联**之前**判掉，否则会被下游误判成「你的画像没覆盖该岗位」，
    把数据缺口说成学员的问题。
    """
    return any(as_level(b.get("required_level")) for b in required)


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
        # attrs 无任何数据库约束：`required_level` 可能是 "L3"、`weight` 可能是 "abc"。
        # 一律过 config 那份唯一口径（脏值取 None / 0.0），不要让脏值直接撞响应模型 ——
        # 「一条脏数据打死一整页」这个形状本项目已经栽过三次
        "required_level": as_level(a.get("required_level")),
        "weight": as_weight(
            (n.get("edge") or {}).get("weight")
            if isinstance(n.get("edge"), dict)
            else a.get("weight")
        )
        or None,
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
        # 状态四件套：technically bundle 是 5 个节点的聚合，规则见
        # skill_aggregate.attach_bundle_draft_state（任一档有草稿 → 整个 bundle 记 draft）。
        # **必须透传**：技能库列表拿不到 status 就会在前端兜底成一个业务状态
        "status": b.get("status"),
        "record_status": b.get("record_status"),
        "has_draft": b.get("has_draft"),
        "draft_change": b.get("draft_change"),
        # 版本/负责人：bundle 按各档聚合（见 attach_bundle_draft_state）。
        # 只在管理台口径下有值 —— 前台没人用，也不该看到运维元数据
        "version": b.get("version"),
        "version_label": b.get("version_label"),
        "owner": b.get("owner"),
        "owner_name": b.get("owner_name"),
        "updated_by_name": b.get("updated_by_name"),
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
    # 卡片要的两块，学员端原先一张卡两个请求（一页 12 张 = 25 个请求）。
    # 这两个函数各自只发一条 SQL，与页大小无关 —— 详见 counts.py 里的说明。
    prog = progressions_for_occupations(ids, conn=conn)
    tops = top_skills_for_occupations(ids, conn=conn)
    for x in items:
        x["progressions"] = prog.get(x["id"]) or []
        x["top_skills"] = tops.get(x["id"]) or []
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
    """按 id 取专业。**类型不对就当不存在**（调用方回 404）。

    不校验类型的后果实测过：把一个 occupation id 传进来，这里照样把它当专业组装，
    而岗位的 `attrs.level` 是 int、专业的是 `voc_associate` 这种字符串，
    响应模型一校验就 500。「传错类型的 id」是客户端错误，该回 404 而不是 5xx。
    """
    with use_conn(conn) as c:
        n = get_node(pid, scope="public", conn=c)
        if not n or (n.get("type") or "").lower() != "major":
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
    industry_id: str | None = None,
    page: int = 1,
    page_size: int = 20,
    region: str | None = None,
    conn: Any | None = None,
) -> dict[str, Any]:
    """岗位列表。`q`（岗位名关键词）与 `industry_id`（具体行业）可单用也可叠加。

    行业只按 **id** 筛，不按名字模糊匹配：行业名重名、含斜杠（「互联网/AI」），
    模糊匹配会把「电子/通信/半导体」和「电子商务」一起筛出来。前端的用法是
    `/v1/student/industries?q=关键词` 出下拉候选，选中后把 id 传进来。
    """
    iid = (industry_id or "").strip() or None
    extra_where = occupation_in_industry_sql("kg_node") if iid else None
    # 片段里两个 EXISTS 各一个占位符，同一个 id 传两次（直连 + 经专业两跳）
    extra_params = [iid, iid] if iid else None
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
            extra_where=extra_where,
            extra_params=extra_params,
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
    """按 id 取岗位；类型不对当不存在（同 `get_profession` 的理由）。"""
    with use_conn(conn) as c:
        n = get_node(pid, scope="public", conn=c)
        if not n or (n.get("type") or "").lower() != "occupation":
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


def expand_progression(conn, goal_row: dict[str, Any]) -> dict[str, Any] | None:
    """把 progression_json 里的 id 序列展开成可展示的链路 + 下一目标。

    库里只存 id（见 biz_ddl 注释），岗位名与职级读时查最新的 —— 采集会改名、
    重定职级，存快照就会展示出过时信息。反过来 **id 序列不随图变化**，用户当初
    选的那条路径不会因为重跑 LLM 推断而漂移。

    链路上的岗位可能已被归档（运营下线了某个岗位），这时它在 chain 里标
    `missing=true` 而不是被静默跳过 —— 悄悄少一跳会让「下一目标」凭空前移。
    """
    raw = goal_row.get("progression_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except json.JSONDecodeError:
            raw = {}
    if not isinstance(raw, dict):
        return None
    ids = [str(x) for x in (raw.get("path") or []) if x]
    if len(ids) < 2:
        return None

    rows = conn.execute(
        f"""
        SELECT n.id, n.name, COALESCE(n.level, {attrs_level_int('n')}) AS level,
               n.description
        FROM kg_node n WHERE n.id = ANY(%s) AND n.type='occupation' AND {node_published('n')}
        """,
        (ids,),
    ).fetchall()
    by_id = {r["id"]: dict(r) for r in rows}

    chain = []
    for i, oid in enumerate(ids):
        node = by_id.get(oid)
        lv = (node or {}).get("level")
        chain.append(
            {
                "id": oid,
                "name": (node or {}).get("name"),
                "level": lv,
                "level_code": occ_level_code(lv),
                "level_name": occ_level_name(lv),
                "is_current": i == 0,
                "missing": node is None,
            }
        )
    # 下一目标 = 链路里当前目标之后那一跳。绑定链路的意义就在这：
    # 不必每次按置信度重新猜一个方向，学员看到的「下一级」始终是他选定的那条。
    nxt = chain[1] if len(chain) > 1 else None
    return {
        "direction": raw.get("direction"),
        "chain": chain,
        "hops": len(chain) - 1,
        "next_target": nxt,
        "target": chain[-1] if chain else None,
    }


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
        d = _row_jsonable(row)
        # 在同一条连接里展开，别为了拿几个岗位名再开一条
        d["progression"] = expand_progression(conn, d)
    return d


MAX_PROGRESSION_HOPS = 6


def _validate_progression(conn, occupation_id: str, path: list[str]) -> list[str]:
    """校验绑定的晋升链路，返回规范化后的 id 序列。

    三条都必须挡住，因为 path 是前端传来的：

    1. **首个必须是目标岗位本身** —— 链路是「从我的目标往上走」，起点错了后面
       算出的「下一级」就跟目标无关。
    2. **相邻两跳必须真有 published 的 advances_to 边** —— 否则前端可以拼出
       任意两个岗位的假链路，展示成晋升关系。
    3. **不能有环、长度有上限** —— advances_to 由 LLM 推断，不保证无环
       （A→B→A 出现过），环会让「下一级」在两个岗位间来回跳。
    """
    ids = [str(x).strip() for x in (path or []) if str(x or "").strip()]
    if not ids:
        return []
    if ids[0] != occupation_id:
        raise ValueError("晋升链路的第一个岗位必须是目标岗位本身")
    if len(ids) > MAX_PROGRESSION_HOPS:
        raise ValueError(f"晋升链路过长（最多 {MAX_PROGRESSION_HOPS} 跳）")
    if len(set(ids)) != len(ids):
        raise ValueError("晋升链路存在重复岗位（成环）")
    for a, b in zip(ids, ids[1:]):
        hit = conn.execute(
            f"""
            SELECT 1 FROM kg_edge e
            JOIN kg_node n ON n.id = e.dst_id AND n.type='occupation'
                          AND {node_published('n')}
            WHERE e.src_id=%s AND e.dst_id=%s AND e.rel_type='advances_to'
              AND {edge_published('e')}
            LIMIT 1
            """,
            (a, b),
        ).fetchone()
        if not hit:
            raise ValueError(f"晋升链路不存在这一跳：{a} → {b}")
    return ids


def default_progression(conn, occupation_id: str) -> list[str]:
    """取该岗位的**第一条**晋升链路（沿置信度最高、职级最近的方向一路走到头）。

    给存量目标回填用，也给「锁定目标时没选链路」兜底。排序与
    `goal_overview._next_levels` 同口径：置信度不能裸排（那是文本列，升序恰好把
    最不可信的 ai_inferred 排在前），要用显式 CASE 给序。
    """
    ids = [occupation_id]
    cur = occupation_id
    for _ in range(MAX_PROGRESSION_HOPS - 1):
        row = conn.execute(
            f"""
            SELECT n.id
            FROM kg_edge e
            JOIN kg_node n ON n.id = e.dst_id AND n.type='occupation'
                          AND {node_published('n')}
            WHERE e.src_id=%s AND e.rel_type='advances_to' AND {edge_published('e')}
            ORDER BY CASE e.confidence
                       WHEN 'official' THEN 3 WHEN 'derived' THEN 2 ELSE 1 END DESC,
                     COALESCE(n.level, 99), n.name
            LIMIT 1
            """,
            (cur,),
        ).fetchone()
        if not row or row["id"] in ids:      # 无出边或成环，到此为止
            break
        ids.append(row["id"])
        cur = row["id"]
    return ids if len(ids) > 1 else []


def set_goal(
    user_id: str,
    user_name: str,
    *,
    occupation_id: str,
    major_id: str | None = None,
    progression_path: list[str] | None = None,
) -> dict[str, Any]:
    occ = get_node(occupation_id, scope="public")
    if not occ:
        raise ValueError("occupation not found")
    major = get_node(major_id, scope="public") if major_id else None
    with connect() as conn:
        # 没显式选链路就绑第一条，让「下一级目标」始终有据可依；
        # 该岗位没有任何 advances_to 出边时为空，前端按「暂无晋升方向」展示
        if progression_path:
            hops = _validate_progression(conn, occupation_id, progression_path)
            direction = "user_selected"
        else:
            hops = default_progression(conn, occupation_id)
            direction = "default_first"
        # bound_at 不在应用侧取时间：updated_at 已由 DB 的 NOW() 写，两处各取
        # 一次时钟会对不上（尤其容器与库不同时区时）。要绑定时间读 updated_at。
        prog = (
            json.dumps({"path": hops, "direction": direction}, ensure_ascii=False)
            if hops
            else None
        )
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
              major_id, major_name, status, updated_at, progression_json
            -- progression_json 必须显式 ::jsonb：psycopg 把 str 当 text 传，
            -- 往 jsonb 列写会 DatatypeMismatch 直接 500
            ) VALUES (%s,%s,%s,%s,%s,%s,'active',NOW(),%s::jsonb)
            ON CONFLICT (user_id, occupation_id) DO UPDATE SET
              user_name = EXCLUDED.user_name,
              occupation_name = EXCLUDED.occupation_name,
              major_id = EXCLUDED.major_id,
              major_name = EXCLUDED.major_name,
              status = 'active',
              updated_at = NOW(),
              -- 重锁同一岗位但没选链路时，保留原来绑的那条：默认链路是「猜」出来的，
              -- 不该把用户显式选过的覆盖掉
              progression_json = COALESCE(
                EXCLUDED.progression_json, biz_user_goal.progression_json
              )
            """,
            (
                user_id,
                user_name,
                occupation_id,
                occ.get("name"),
                major_id,
                (major or {}).get("name") if major else None,
                prog,
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


def unlock_achievement(user_id: str, user_name: str, code: str) -> bool:
    """解锁成就的**公开入口**。已解锁或 code 不存在时返回 False，不报错。

    路由层用这个，不要直接叫 `_unlock` —— 私有名却被模块外调用，是审查文档
    第 21 条点名的形状（改私有实现时看不出有外部调用方）。
    """
    return _unlock(user_id, user_name, code)


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
    return {
        "user_id": user_id,
        "user_name": user_name,
        "goal": goal,
        "points": int(pts["total"]) if pts else 0,
        "badges": [_row_jsonable(b) for b in badges],
        "skills": [_row_jsonable(s) for s in skills],
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
    external_path_id: str,
    payload_sha256: str,
    push_status: str = "ok",
    superseded_plan_id: str | None = None,
    last_error: str | None = None,
) -> dict[str, Any]:
    """记录一次学习计划推送。

    计划内容由 e-ai-spaces 持有，这里只存关联与推送状态：便于列表回显、
    从报告追到它衍生出的计划、以及失败后重推。

    幂等键是 `(user_id, external_path_id)` 而不是 plan_id —— 推失败时根本没有
    plan_id（存空串），只有 external_path_id 是我们自己算得出来的确定值，
    重推要能覆盖到同一行。
    """
    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO biz_user_learning_plan
              (user_id, occupation_id, plan_id, session_id, external_path_id,
               payload_sha256, push_status, superseded_plan_id, last_error, pushed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (user_id, external_path_id)
              WHERE external_path_id IS NOT NULL
            DO UPDATE SET
              occupation_id      = EXCLUDED.occupation_id,
              plan_id            = EXCLUDED.plan_id,
              session_id         = EXCLUDED.session_id,
              payload_sha256     = EXCLUDED.payload_sha256,
              push_status        = EXCLUDED.push_status,
              superseded_plan_id = EXCLUDED.superseded_plan_id,
              last_error         = EXCLUDED.last_error,
              pushed_at          = NOW()
            RETURNING *
            """,
            (
                user_id, occupation_id, plan_id, session_id, external_path_id,
                payload_sha256, push_status, superseded_plan_id, last_error,
            ),
        ).fetchone()
        # 报告里也留一份，方便「这份报告生成过哪个计划」独立可查。
        # 只在拿到真 plan_id 时写：推失败时写空串会把上一次成功的记录覆盖掉。
        if session_id and plan_id:
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


def session_for_learning_plan(user_id: str, session_id: int) -> dict[str, Any] | None:
    """取诊断会话 + 岗位名，供生成学习计划用。不属于该用户则返回 None。

    `biz_diagnosis_session` 有三个创建入口（测评 / 简历 / 对话），但
    `biz_diagnosis_result` **不一定有**——测评要答完题、对话要跑完才写。
    所以这里连 result 一起判，把「有会话」和「有诊断结果」分开返回，
    让路由能给出不同的错误：前者是找不到（404），后者是状态不对（400）。

    放过没有 result 的会话，会构造出一条没有短板数据的空路径，被对方 422 拒——
    错误暴露在上游，排查成本高得多。
    """
    with connect() as conn:
        row = conn.execute(
            """
            SELECT s.id, s.user_id, s.target_occupation_id, s.channel, s.status,
                   n.name AS occupation_name,
                   (d.session_id IS NOT NULL) AS has_result
              FROM biz_diagnosis_session s
              LEFT JOIN biz_diagnosis_result d ON d.session_id = s.id
              LEFT JOIN kg_node n ON n.id = s.target_occupation_id
                   AND COALESCE(n.status,'published') = 'published'
             WHERE s.id = %s AND s.user_id = %s
            """,
            (session_id, str(user_id)),
        ).fetchone()
    return _row_jsonable(row) if row else None


def previous_external_path_id(
    user_id: str, occupation_id: str, *, exclude_external_path_id: str
) -> str | None:
    """同一学员同一岗位上一条推送成功的 external_path_id，用作换代的 revision_of。

    只认 `push_status='ok'` 的：拿一条失败记录去当"上一版"，对方那边根本没有
    这条路径，换代关系会挂空。
    """
    with connect() as conn:
        row = conn.execute(
            """
            SELECT external_path_id FROM biz_user_learning_plan
             WHERE user_id=%s AND occupation_id=%s AND push_status='ok'
               AND external_path_id IS NOT NULL AND external_path_id <> %s
             ORDER BY pushed_at DESC NULLS LAST, created_at DESC
             LIMIT 1
            """,
            (str(user_id), occupation_id, exclude_external_path_id),
        ).fetchone()
    return (row or {}).get("external_path_id") or None


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
    score_status = "ok"
    if occupation_id:
        try:
            pm = position_match(user_id, occupation_id)
            # 加权匹配度，替代「命中数/需求数」；岗位没配要求档时它是 None（算不出来），
            # 此时不许退回「命中数/需求数」——那个数不看档位差距，会给出虚高的假结论
            match_score = pm["match_score"]
            score_status = pm.get("score_status") or "ok"
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
        "score_status": score_status,
        "user_skills": user_skills,
        "required_skills": required,
        "gaps": gaps,
        "radar": radar,
        # 部分缺基准时也要把「仅供参考」写进这句话：DiagnosisReportOut 没有
        # score_status 字段，学员在简历/对话诊断页只看得到 summary 和那个百分数
        "summary": (
            f"综合匹配度约 {match_score}%（规则引擎，非大模型终态）"
            + ("；该岗位部分能力要求档待完善，分数仅供参考"
               if score_status == "partial_baseline" else "")
            if match_score is not None
            else "该岗位尚未配置能力要求档，无法计算匹配度（规则引擎，非大模型终态）"
        ),
    }


# ── 学习路径 ─────────────────────────────────────────────────


def list_resources(
    *, skill_id: str | None = None, q: str | None = None, page: int = 1, page_size: int = 20
) -> dict[str, Any]:
    """学习资源：KG course 节点中**真正能学**的那部分。

    必须带 `learnable_course()`：库里 91% 的 course 是教育部课标的课目名称
    （`role=curriculum_catalog`），链接指向专业教学标准的大类目录页，
    学员点开没有任何课程内容。详见 config.learnable_course 的说明。
    """
    from backend.kg.pg_store.config import learnable_course

    data = list_nodes(
        node_type="course", q=q, page=page, page_size=page_size, published_only=True,
        extra_where=learnable_course(""),
        # 真实课程排在检索入口前面：过滤掉课标后列表会被 search_landing 占满，
        # 学员翻好几页才看到一门真课
        order_by="resource_quality",
    )
    items = []
    for n in data["items"]:
        a = n.get("attrs") if isinstance(n.get("attrs"), dict) else {}
        is_landing = a.get("match_method") == "search_landing"
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
                # 让前端能区分「点开是课程」还是「点开是搜索页」
                "kind": "landing" if is_landing else "real",
                "learner_count": a.get("learner_count"),
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
        # 路径本体在学习空间服务，这里能数的只有「成功推送过几条」
        plans = conn.execute(
            "SELECT COUNT(*) AS c FROM biz_user_learning_plan WHERE push_status='ok'"
        ).fetchone()["c"]
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
        "learning_plans_pushed": int(plans),
        "pending_proposals": pend,
    }
