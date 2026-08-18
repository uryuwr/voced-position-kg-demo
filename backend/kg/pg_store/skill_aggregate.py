"""技能按 L 分存；读路径按 skill_key 聚合为逻辑技能 bundle。"""
from __future__ import annotations

import json
import math
import re
from typing import Any

from backend.kg.pg_store.client import connect
from backend.kg.pg_store.config import DEFAULT_REGION

# SQL 片段：kg_node 别名 n，attrs 为 TEXT（可能为空）
_ATTRS_JSON = """(
  CASE
    WHEN n.attrs IS NULL OR btrim(n.attrs) = '' THEN NULL
    ELSE n.attrs::json
  END
)"""

# SQL 片段：kg_node 别名 n，attrs 为 TEXT
SKILL_KEY_SQL = f"""
COALESCE(
  NULLIF(trim(both FROM ({_ATTRS_JSON}->>'skill_key')), ''),
  NULLIF(trim(both FROM ({_ATTRS_JSON}->>'skill_name')), ''),
  NULLIF(trim(both FROM split_part(n.name, ' · ', 1)), ''),
  NULLIF(trim(both FROM split_part(n.name, '·', 1)), ''),
  n.name
)
"""

# attrs.level 是产品等级（1 了解 → 5 专家）的唯一真源，由采集端直接写入，
# 库内历史数据已由 scripts/migrate_skill_level_to_product.py 迁移到位。
# 非数字的 level（"L3"/"三级"/"3.5"）取 NULL，而不是让 PG 抛 invalid input syntax
# 把整个列表接口打成 500 —— 详见 config.attrs_level_int 的说明。
LEVEL_SQL = f"""
CASE WHEN trim(both FROM ({_ATTRS_JSON}->>'level')) ~ '^[0-9]+$'
     THEN trim(both FROM ({_ATTRS_JSON}->>'level'))::int END
"""

def _level_labels() -> dict[int, str]:
    from backend.kg.pg_store.skill_level_meta import label_map

    return label_map()


_PUB_N = "COALESCE(n.status, 'published') = 'published'"


def _default_region(region: str | None) -> str | None:
    if region is None or str(region).strip() == "":
        return DEFAULT_REGION
    r = str(region).strip()
    if r.lower() in ("all", "*", "any"):
        return None
    return r


def _maybe_json(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v


def skill_key_from_node(n: dict[str, Any]) -> str:
    a = n.get("attrs") if isinstance(n.get("attrs"), dict) else _maybe_json(n.get("attrs")) or {}
    if not isinstance(a, dict):
        a = {}
    for k in ("skill_key", "skill_name"):
        v = (a.get(k) or "").strip()
        if v:
            return v
    name = (n.get("name") or "").strip()
    if "·" in name:
        return name.split("·")[0].strip()
    return name or (n.get("id") or "")


def level_from_node(n: dict[str, Any]) -> int | None:
    """产品等级 1–5（了解→专家）。直读 attrs.level，不做任何刻度换算。"""
    a = n.get("attrs") if isinstance(n.get("attrs"), dict) else _maybe_json(n.get("attrs")) or {}
    if not isinstance(a, dict):
        a = {}
    try:
        return max(1, min(5, int(a["level"])))
    except (KeyError, TypeError, ValueError):
        return None


def bundle_id(region: str | None, skill_key: str) -> str:
    reg = region or DEFAULT_REGION or "CN"
    return f"bundle:{reg}:{skill_key}"


def _level_entry(n: dict[str, Any], level: int | None) -> dict[str, Any]:
    a = n.get("attrs") if isinstance(n.get("attrs"), dict) else {}
    li = level
    # 档位文案只认产品语义（了解/掌握/…）；国标「四级/中级工」那套已在数据层剥离
    label = (_level_labels().get(li) if li else None) or a.get("level_label")
    desc = None
    # level_descriptions 的 key 是产品档 L 码（"L4"），与 level 同源
    ld = a.get("level_descriptions")
    if isinstance(ld, dict) and li and ld.get(f"L{li}"):
        desc = ld.get(f"L{li}")
    if not desc:
        desc = n.get("description")
    return {
        "level": li,
        "level_label": label,
        "node_id": n.get("id"),
        "description": desc,
        "status": n.get("status") or "published",
        "weight": (n.get("edge") or {}).get("weight")
        if isinstance(n.get("edge"), dict)
        else None,
    }


def merge_level_descriptions(nodes: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for n in nodes:
        a = n.get("attrs") if isinstance(n.get("attrs"), dict) else {}
        ld = a.get("level_descriptions")
        if isinstance(ld, dict):
            for k, v in ld.items():
                if v and str(v).strip():
                    out[str(k)] = str(v)
        li = level_from_node(n)
        code = f"L{li}" if li else ""
        if code and n.get("description") and code not in out:
            # 避免把纯拼接短句当能力描述；过短则跳过
            d = str(n["description"]).strip()
            if len(d) >= 12 and "（" not in d[:4]:
                out[code] = d
    return out


def aggregate_bundle_status(nodes: list[dict[str, Any]]) -> str:
    """逻辑技能展示状态：各档 status 一致则取之，否则 mixed。"""
    if not nodes:
        return "draft"
    sts = {(n.get("status") or "published") for n in nodes}
    if len(sts) == 1:
        return next(iter(sts))
    # 优先级：有 published 则视为已发布（部分档可能仍 draft）
    if "published" in sts and ("draft" in sts or "disabled" in sts):
        return "mixed"
    if "draft" in sts:
        return "draft"
    if "disabled" in sts:
        return "disabled"
    return "mixed"


def assemble_bundle(
    skill_key: str,
    nodes: list[dict[str, Any]],
    *,
    region: str | None = None,
    required_level: int | None = None,
    weight: float | None = None,
    occupation_count: int | None = None,
    major_count: int | None = None,
) -> dict[str, Any]:
    nodes_sorted = sorted(
        nodes,
        key=lambda n: (level_from_node(n) is None, level_from_node(n) or 99),
    )
    levels = [_level_entry(n, level_from_node(n)) for n in nodes_sorted]
    # 档位一律用 int 1–5 对外，前端按 skill_level_meta 渲染文案，不再传 "L3" 这类码
    avail_set = {lv["level"] for lv in levels if lv["level"]}
    available = [i for i in range(1, 6) if i in avail_set]
    missing = [i for i in range(1, 6) if i not in avail_set]
    ld = merge_level_descriptions(nodes_sorted)
    reg = region or (nodes_sorted[0].get("region") if nodes_sorted else DEFAULT_REGION)
    conf = None
    for n in nodes_sorted:
        if n.get("confidence"):
            conf = n["confidence"]
            break
    # 技能大类（国标「职业功能」维度，见 backend/kg/pg_store/skill_taxonomy.py）。
    # 同一 skill_key 的各等级节点分类一致，取第一个非空。
    category = None
    for n in nodes_sorted:
        if n.get("category"):
            category = n["category"]
            break
    # 代表 description：优先 required 档
    desc = None
    if required_level:
        for lv in levels:
            if lv.get("level") == required_level and lv.get("description"):
                desc = lv["description"]
                break
    if not desc and levels:
        desc = levels[-1].get("description")
    counts = {"level": len(nodes_sorted)}
    if occupation_count is not None:
        counts["occupation"] = occupation_count
    if major_count is not None:
        counts["major"] = major_count
    return {
        "id": bundle_id(reg, skill_key),
        "type": "skill",
        "kg_type": "skill_level",
        "skill_key": skill_key,
        "skill_name": skill_key,
        "name": skill_key,
        "region": reg,
        "status": aggregate_bundle_status(nodes_sorted),
        "levels": levels,
        "level_descriptions": ld,
        "available_levels": available,
        "missing_levels": missing,
        # 原型「技能库」列表的「等级完整度」列：L1–L5 配齐几档
        "level_completeness": f"{len(available)}/5",
        "level_complete": len(available) >= 5,
        "required_level": required_level,
        "weight": weight,
        # weight 为 0~1 小数（源自国标权重表 attrs.weight_pct/100）；
        # 前端「权重」列直接用 weight_pct 展示百分比即可，不必自己乘 100。
        "weight_pct": round(weight * 100) if isinstance(weight, (int, float)) else None,
        # is_core：权重 >= 30% 视为该岗位的核心技能（原型「核心」标记）
        "is_core": bool(isinstance(weight, (int, float)) and weight >= 0.3),
        "category": category,
        "desc": desc,
        "description": desc,
        "counts": counts,
        "source_url": nodes_sorted[0].get("source_url") if nodes_sorted else None,
        "confidence": conf,
        "node_ids": [n.get("id") for n in nodes_sorted if n.get("id")],
    }


def group_nodes_to_bundles(
    nodes: list[dict[str, Any]],
    *,
    region: str | None = None,
) -> list[dict[str, Any]]:
    """扁平行 skill_level → bundle 列表（保持首次出现顺序）。"""
    order: list[str] = []
    buckets: dict[str, list[dict[str, Any]]] = {}
    req_level: dict[str, int] = {}
    weights: dict[str, float] = {}
    for n in nodes:
        key = skill_key_from_node(n)
        if key not in buckets:
            order.append(key)
            buckets[key] = []
        buckets[key].append(n)
        li = level_from_node(n)
        edge = n.get("edge") if isinstance(n.get("edge"), dict) else {}
        w = edge.get("weight")
        try:
            fw = float(w) if w is not None else None
        except (TypeError, ValueError):
            fw = None
        # 一个实体对一个技能只有一个要求档（高级天然含低级）。存量重复已由
        # scripts/dedupe_skill_composition_edges.py 合并；这里保留兜底聚合：
        # 取最高档，**权重取该档那条边的**，而非 max(权重) —— 国标权重是「该等级
        # 考核中的占比」，取别档的权重会和 required_level 不同源，导致前后台数字打架。
        cur = req_level.get(key)
        if li is not None and (cur is None or li > cur):
            req_level[key] = li
            if fw is not None:
                weights[key] = fw
        elif cur is None and key not in weights and fw is not None:
            weights[key] = fw          # 该技能无档位信息时的兜底
    return [
        assemble_bundle(
            k,
            buckets[k],
            region=region,
            required_level=req_level.get(k),
            weight=weights.get(k),
        )
        for k in order
    ]


def list_skill_bundles(
    *,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
    region: str | None = None,
    occupation_id: str | None = None,
    has_level: str | None = None,
    published_only: bool = True,
    status: str | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    """
    分页逻辑技能列表（GROUP BY skill_key）。
    scope=manage 时含 draft/disabled；status 可筛 published|draft|disabled|mixed。
    """
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 200))
    reg = _default_region(region)
    q_raw = (q or "").strip()
    q_like = f"%{q_raw}%" if q_raw and q_raw not in ("*", "全部", "all") else None
    has_lv = None
    if has_level:
        m = re.match(r"L?([1-5])$", str(has_level).strip(), re.I)
        if m:
            has_lv = int(m.group(1))
    manage = (scope or "").strip().lower() in ("manage", "admin", "all")
    if manage:
        published_only = False
    st = (status or "").strip().lower() or None
    if st in ("", "all", "*"):
        st = None

    key_expr = SKILL_KEY_SQL
    li_expr = LEVEL_SQL

    # 节点级 status 条件（聚合前）
    if st and st != "mixed":
        status_clause = "AND COALESCE(n.status, 'published') = %s"
        status_params: list[Any] = [st]
    elif published_only:
        status_clause = f"AND {_PUB_N}"
        status_params = []
    else:
        # 管理端：排除归档
        status_clause = "AND COALESCE(n.status, 'published') NOT IN ('archived')"
        status_params = []

    with connect() as conn:
        if occupation_id:
            base_from = f"""
            FROM kg_edge e
            JOIN kg_node n ON n.id = e.dst_id AND n.type = 'skill_level'
            WHERE e.src_id = %s AND e.rel_type = 'requires'
              AND COALESCE(e.status, 'published') = 'published'
              {status_clause}
            """
            params_base: list[Any] = [occupation_id, *status_params]
        else:
            base_from = f"""
            FROM kg_node n
            WHERE n.type = 'skill_level'
              {status_clause}
              AND (%s::text IS NULL OR n.region = %s)
            """
            params_base = [*status_params, reg, reg]

        filters = ""
        params_f: list[Any] = []
        if q_like:
            filters += f" AND (lower({key_expr}) LIKE lower(%s) OR lower(n.name) LIKE lower(%s))"
            params_f.extend([q_like, q_like])
        if has_lv is not None:
            filters += f" AND ({li_expr}) = %s"
            params_f.append(has_lv)

        count_sql = f"""
        SELECT count(*) AS c FROM (
          SELECT {key_expr} AS skill_key
          {base_from}
          {filters}
          GROUP BY 1
        ) t
        """
        total_row = conn.execute(count_sql, (*params_base, *params_f)).fetchone()
        total = int(total_row["c"] if total_row else 0)
        total_pages = max(1, math.ceil(total / page_size)) if total else 0
        offset = (page - 1) * page_size

        page_sql = f"""
        SELECT {key_expr} AS skill_key,
               count(*) AS level_count,
               array_agg(n.id ORDER BY {li_expr} NULLS LAST) AS node_ids
        {base_from}
        {filters}
        GROUP BY 1
        ORDER BY 1
        LIMIT %s OFFSET %s
        """
        page_rows = conn.execute(
            page_sql, (*params_base, *params_f, page_size, offset)
        ).fetchall()

        all_ids: list[str] = []
        key_order: list[str] = []
        key_to_ids: dict[str, list[str]] = {}
        for r in page_rows:
            sk = r["skill_key"]
            key_order.append(sk)
            ids = list(r["node_ids"] or [])
            key_to_ids[sk] = ids
            all_ids.extend(ids)

        nodes_by_id: dict[str, dict[str, Any]] = {}
        if all_ids:
            from backend.kg.pg_store.query import _node_dict  # local import avoid cycle

            nrows = conn.execute(
                "SELECT * FROM kg_node WHERE id = ANY(%s)", (all_ids,)
            ).fetchall()
            for row in nrows:
                nodes_by_id[row["id"]] = _node_dict(row)

        # occupation counts for page keys
        occ_counts: dict[str, int] = {}
        if key_order:
            occ_sql = f"""
            SELECT {key_expr} AS skill_key, count(DISTINCT e.src_id) AS c
            FROM kg_edge e
            JOIN kg_node n ON n.id = e.dst_id AND n.type = 'skill_level'
            JOIN kg_node o ON o.id = e.src_id AND o.type = 'occupation'
              AND COALESCE(o.status, 'published') = 'published'
            WHERE e.rel_type = 'requires'
              AND COALESCE(e.status, 'published') = 'published'
              AND ({key_expr}) = ANY(%s)
            GROUP BY 1
            """
            for r in conn.execute(occ_sql, (key_order,)).fetchall():
                occ_counts[r["skill_key"]] = int(r["c"])

        items = []
        for sk in key_order:
            nodes = [nodes_by_id[i] for i in key_to_ids[sk] if i in nodes_by_id]
            items.append(
                assemble_bundle(
                    sk,
                    nodes,
                    region=reg,
                    occupation_count=occ_counts.get(sk, 0),
                )
            )

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }


def get_skill_bundle(
    skill_key: str,
    *,
    region: str | None = None,
    published_only: bool = True,
) -> dict[str, Any] | None:
    sk = (skill_key or "").strip()
    if not sk:
        return None
    # 允许传入 bundle:CN:xxx
    if sk.startswith("bundle:"):
        parts = sk.split(":", 2)
        if len(parts) == 3:
            region = region or parts[1]
            sk = parts[2]
    reg = _default_region(region)
    key_expr = SKILL_KEY_SQL
    status_sql = (
        "AND COALESCE(n.status, 'published') = 'published'"
        if published_only
        else "AND COALESCE(n.status, 'published') NOT IN ('archived')"
    )
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT n.*
            FROM kg_node n
            WHERE n.type = 'skill_level'
              {status_sql}
              AND (%s::text IS NULL OR n.region = %s)
              AND ({key_expr}) = %s
            ORDER BY n.name
            """,
            (reg, reg, sk),
        ).fetchall()
        if not rows:
            return None
        from backend.kg.pg_store.query import _node_dict

        nodes = [_node_dict(r) for r in rows]
        occ_row = conn.execute(
            f"""
            SELECT count(DISTINCT e.src_id) AS c
            FROM kg_edge e
            JOIN kg_node n ON n.id = e.dst_id AND n.type = 'skill_level'
            JOIN kg_node o ON o.id = e.src_id AND o.type = 'occupation'
              AND COALESCE(o.status, 'published') = 'published'
            WHERE e.rel_type = 'requires'
              AND COALESCE(e.status, 'published') = 'published'
              AND ({key_expr}) = %s
            """,
            (sk,),
        ).fetchone()
        occ_c = int(occ_row["c"] if occ_row else 0)
        bundle = assemble_bundle(sk, nodes, region=reg, occupation_count=occ_c)
        try:
            from backend.kg.pg_store.skill_prereq import list_prereqs

            prereqs = list_prereqs(sk, region=reg)
            bundle["prerequisites"] = [
                {
                    "skill_key": p["prereq_skill_key"],
                    "name": p["prereq_skill_key"],
                    "evidence": p.get("evidence"),
                }
                for p in prereqs
            ]
        except Exception:
            bundle["prerequisites"] = []
        return bundle


def occupation_skill_bundles(
    occupation_id: str, *, limit: int = 100
) -> list[dict[str, Any]]:
    """岗位 requires → 逻辑技能 bundle 列表（权重取自边）。"""
    from backend.kg.pg_store.query import _node_dict

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT s.*, e.id AS edge_id, e.rel_type, e.weight, e.confidence, e.evidence
            FROM kg_edge e
            JOIN kg_node s ON s.id = e.dst_id AND s.type = 'skill_level'
            WHERE e.src_id = %s AND e.rel_type = 'requires'
              AND COALESCE(e.status, 'published') = 'published'
              AND COALESCE(s.status, 'published') = 'published'
            ORDER BY e.weight DESC NULLS LAST, s.name
            LIMIT %s
            """,
            (occupation_id, max(limit * 5, 200)),  # 多取再聚合
        ).fetchall()
    flat = []
    for r in rows:
        node = _node_dict(r)
        node["edge"] = {
            "id": r.get("edge_id"),
            "rel_type": r.get("rel_type") or "requires",
            "weight": r.get("weight"),
            "confidence": r.get("confidence"),
            "evidence": r.get("evidence"),
        }
        flat.append(node)
    bundles = group_nodes_to_bundles(flat)
    return bundles[:limit]


def occupation_skill_composition(occupation_id: str) -> dict[str, Any]:
    """岗位技能构成：逻辑技能 + 边权重 + 权重和（读路径只认边 weight）。"""
    from backend.kg.pg_store.query import get_node

    occ = get_node(occupation_id, scope="public")
    if not occ or occ.get("type") != "occupation":
        raise ValueError("occupation not found")
    skills = occupation_skill_bundles(occupation_id, limit=200)
    weight_sum = 0.0
    weighted = 0
    for s in skills:
        w = s.get("weight")
        if w is not None:
            try:
                weight_sum += float(w)
                weighted += 1
            except (TypeError, ValueError):
                pass
    return {
        "occupation": {
            "id": occ.get("id"),
            "name": occ.get("display_name") or occ.get("name"),
        },
        "skills": skills,
        "skill_count": len(skills),
        "weight_sum": round(weight_sum, 4),
        "weighted_skill_count": weighted,
        "weight_sum_ok": weighted == 0 or 0.85 <= weight_sum <= 1.15,
        "note": "weight 来自 requires 边；节点 attrs.weight_pct 仅历史兼容",
    }


# 课程资源三分类 —— 判定依据是 **attrs.role**，不是 source_system。
#
# 踩过的坑：曾把 MOE_CN 归进「真实课程」，结果点开是教育部课标**目录页**。
# 那 15960 门是课标里的「课目名称」，`role=curriculum_catalog`，
# 本体明确写了「不能单独冒充可学资源」—— 它们是培养方案条目，不是能学的课。
#
#   real     平台真实课程页，点开就能学（中国大学MOOC / 学堂在线）
#   catalog  课标目录条目，点开是专业培养方案，**没有具体课程内容**
#   landing  检索入口，点开是搜索结果页
_ROLE_LEARNABLE = "learnable_resource"
_ROLE_CATALOG = "curriculum_catalog"
_REAL_COURSE_SOURCES = ("ICOURSE163", "XUETANGX", "SMART_EDU_VOC")
_PLATFORM_LABEL = {
    "ICOURSE163": "中国大学MOOC",
    "XUETANGX": "学堂在线",
    "SEARCH_LANDING_CN": "检索入口",
    "MOE_CN": "专业课标目录",
    "SMART_EDU_VOC": "国家智慧教育",
}


def _course_kind(role: str | None, platform: str, match_method: str | None) -> str:
    """课程资源性质。role 优先，其次看来源与匹配方式。"""
    if role == _ROLE_CATALOG:
        return "catalog"
    if match_method == "search_landing" or platform == "SEARCH_LANDING_CN":
        return "landing"
    if role == _ROLE_LEARNABLE and platform in _REAL_COURSE_SOURCES:
        return "real"
    return "real" if platform in _REAL_COURSE_SOURCES else "catalog"


def occupation_courses(occupation_id: str, *, limit_per_skill: int = 5) -> dict[str, Any]:
    """岗位相关课程：occupation -requires-> skill_level -taught_by-> course。

    独立于 `occupation_skill_composition`：岗位详情接口不变，这里单独聚合课程，
    按技能分组，并区分**真实课程**与**检索入口**。
    """
    from backend.kg.pg_store.client import session
    from backend.kg.pg_store.config import attrs_level_int, edge_published, node_published
    from backend.kg.pg_store.query import get_node

    occ = get_node(occupation_id, scope="public")
    if not occ or occ.get("type") != "occupation":
        raise ValueError("occupation not found")

    sql = f"""
        SELECT ({SKILL_KEY_SQL})                AS skill_key,
               ({attrs_level_int('n')})         AS req_level,
               re.weight                        AS skill_weight,
               n.category                       AS category,
               c.id                             AS course_id,
               c.name                           AS course_name,
               c.source_url                     AS url,
               c.source_system                  AS platform,
               c.attrs                          AS course_attrs,
               te.weight                        AS course_weight
        FROM kg_edge re
        JOIN kg_node n  ON n.id = re.dst_id
        JOIN kg_edge te ON te.src_id = n.id AND te.rel_type = 'taught_by'
                       AND {edge_published('te')}
        JOIN kg_node c  ON c.id = te.dst_id AND {node_published('c')}
        WHERE re.src_id = %s AND re.rel_type = 'requires'
          AND {edge_published('re')} AND {node_published('n')}
        ORDER BY re.weight DESC NULLS LAST, te.weight DESC NULLS LAST, c.name
    """
    groups: dict[str, dict[str, Any]] = {}
    real_n = catalog_n = landing_n = 0
    seen: set[tuple[str, str]] = set()
    with session() as conn, conn.cursor() as cur:
        cur.execute(sql, (occupation_id,))
        for r in cur.fetchall():
            key = r["skill_key"]
            if not key:
                continue
            g = groups.setdefault(key, {
                "skill_key": key,
                "required_level": r["req_level"],
                "weight": float(r["skill_weight"]) if r["skill_weight"] is not None else None,
                "category": r["category"],
                "courses": [],
            })
            if (key, r["course_id"]) in seen or len(g["courses"]) >= limit_per_skill:
                continue
            seen.add((key, r["course_id"]))

            attrs = r["course_attrs"]
            if isinstance(attrs, str):
                try:
                    attrs = json.loads(attrs or "{}")
                except Exception:
                    attrs = {}
            attrs = attrs or {}
            platform = r["platform"] or ""
            kind = _course_kind(attrs.get("role"), platform, attrs.get("match_method"))
            if kind == "real":
                real_n += 1
            elif kind == "catalog":
                catalog_n += 1
            else:
                landing_n += 1
            g["courses"].append({
                "id": r["course_id"],
                "name": r["course_name"],
                "url": r["url"],
                "platform": platform,
                "platform_label": _PLATFORM_LABEL.get(platform, platform),
                "kind": kind,
                "learner_count": attrs.get("learner_count"),
                "school": attrs.get("school"),
                "img_url": attrs.get("img_url"),
            })

    items = sorted(groups.values(), key=lambda x: -(x["weight"] or 0))
    return {
        "occupation": {
            "id": occ.get("id"),
            "name": occ.get("display_name") or occ.get("name"),
        },
        "by_skill": items,
        "skill_count": len(items),
        "course_count": real_n + catalog_n + landing_n,
        "real_course_count": real_n,
        "catalog_count": catalog_n,
        "landing_count": landing_n,
        "note": (
            "kind=real 平台真实课程页，点开可学（带 learner_count 判热度）；"
            "kind=catalog 课标目录条目，点开是专业培养方案而非课程内容；"
            "kind=landing 检索入口，点开是搜索结果页。"
            "只有 real 才是真正可学的资源。"
        ),
    }
