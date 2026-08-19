"""技能按 L 分存；读路径按 skill_key 聚合为逻辑技能 bundle。"""
from __future__ import annotations

import json
import math
import re
import urllib.parse
from typing import Any

from backend.kg.pg_store.client import connect
from backend.kg.pg_store.config import DEFAULT_REGION, online_only, prefer_draft
from backend.kg.pg_store.config import ENROLL_SOURCES as _CFG_ENROLL_SOURCES
from backend.kg.pg_store.skill_taxonomy import name_of as _cat_name

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
# 草稿态：管理台口径（`NOT IN ('archived')`）对草稿行也成立，聚合前必须先去重，
# 否则一个技能被编辑过就多出一档、level_count 与「等级完整度 n/5」全错（方案 §6.2）。
_PD_N = prefer_draft("n")
_ONLINE_N = online_only("n")


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
        "category_name": _cat_name(category),
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

    # 行选择条件：**prefer_draft 只能用在管理台口径上**。
    # 与 `status='published'` 组合是致命的：一条记录有草稿时 prefer_draft 留下草稿行、
    # 丢掉线上行，而草稿行的 status 恒为 'draft' 被 published 过滤掉 ——
    # 这个技能就整条从前台列表里消失了。实测过：管理台改一下技能，
    # 学员端技能库里这个技能不见了。前台一律钉线上行。
    row_pick = _ONLINE_N if published_only else _PD_N

    # 节点级 status 条件（聚合前）
    if st and st != "mixed":
        status_clause = "AND COALESCE(n.status, 'published') = %s"
        status_params: list[Any] = [st]
    elif published_only:
        status_clause = f"AND {_PUB_N}"
        status_params = []
    else:
        # 管理端：排除归档，并排掉**待归档的档位墓碑**（清空某档描述 = 移除该档，
        # 落一条 target_status='archived' 的草稿行）。不排掉的话「等级完整度」
        # 那五个 chip 会把已删的档继续显示成有，和编辑页对不上。
        status_clause = (
            "AND COALESCE(n.status, 'published') NOT IN ('archived') "
            "AND COALESCE(n.target_status, '') <> 'archived'"
        )
        status_params = []

    with connect() as conn:
        if occupation_id:
            base_from = f"""
            FROM kg_edge e
            JOIN kg_node n ON n.id = e.dst_id AND n.type = 'skill_level'
                 AND {row_pick}
            WHERE e.src_id = %s AND e.rel_type = 'requires'
              AND COALESCE(e.status, 'published') = 'published'
              {status_clause}
            """
            params_base: list[Any] = [occupation_id, *status_params]
        else:
            base_from = f"""
            FROM kg_node n
            WHERE n.type = 'skill_level' AND {row_pick}
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

        # 排序：最近建/改的技能排最前。逻辑技能是多个档位节点聚合出来的，
        # 取各档 created_at 的 max —— 补档也算「动过」，比 min 更贴「最近在维护什么」。
        # 原先是 `ORDER BY 1`（技能名字母序），新建的技能会散落在几百页中间找不到。
        # NULLS LAST：历史数据 created_at 可能为空，不该压在最新的前面。
        page_sql = f"""
        SELECT {key_expr} AS skill_key,
               count(*) AS level_count,
               max(n.created_at) AS created_at,
               array_agg(n.id ORDER BY {li_expr} NULLS LAST) AS node_ids
        {base_from}
        {filters}
        GROUP BY 1
        ORDER BY max(n.created_at) DESC NULLS LAST, 1
        LIMIT %s OFFSET %s
        """
        page_rows = conn.execute(
            page_sql, (*params_base, *params_f, page_size, offset)
        ).fetchall()

        all_ids: list[str] = []
        key_order: list[str] = []
        key_to_ids: dict[str, list[str]] = {}
        created_by_key: dict[str, Any] = {}
        for r in page_rows:
            sk = r["skill_key"]
            key_order.append(sk)
            ids = list(r["node_ids"] or [])
            key_to_ids[sk] = ids
            all_ids.extend(ids)
            ca = r.get("created_at")
            created_by_key[sk] = ca.isoformat() if hasattr(ca, "isoformat") else ca

        nodes_by_id: dict[str, dict[str, Any]] = {}
        if all_ids:
            from backend.kg.pg_store.query import _node_dict  # local import avoid cycle

            nrows = conn.execute(
                f"SELECT * FROM kg_node WHERE id = ANY(%s) AND "
                + (online_only() if published_only else prefer_draft()),
                (all_ids,),
            ).fetchall()
            for row in nrows:
                # 管理台口径（scope=manage / 指定 status）才带运维元数据；
                # 学员端 /v1/student/skills 走 published_only=True，不带
                nodes_by_id[row["id"]] = _node_dict(row, admin=not published_only)

        # occupation counts for page keys
        occ_counts: dict[str, int] = {}
        if key_order:
            occ_sql = f"""
            SELECT {key_expr} AS skill_key, count(DISTINCT e.src_id) AS c
            FROM kg_edge e
            JOIN kg_node n ON n.id = e.dst_id AND n.type = 'skill_level'
                 AND {_ONLINE_N}
            JOIN kg_node o ON o.id = e.src_id AND o.type = 'occupation'
              AND COALESCE(o.status, 'published') = 'published'
              AND {online_only('o')}
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
            b = assemble_bundle(
                sk,
                nodes,
                region=reg,
                occupation_count=occ_counts.get(sk, 0),
            )
            # 列表按它倒序，前端也要能显示「创建于」
            b["created_at"] = created_by_key.get(sk)
            items.append(b)

        # 管理台（scope=manage / 指定 status）要能看出「这个技能有没有未发布的改动」。
        # 技能库列表走的是本接口，不是四维列表 —— 上次给四维列表加 record_status 时漏了这里，
        # 结果 21074 个技能在页面上全显示「草稿」（前端拿不到 status 就兜底成 draft）。
        attach_bundle_draft_state(items, conn=conn, admin=not published_only)

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
    row_pick = _ONLINE_N if published_only else _PD_N
    if published_only:
        status_sql = "AND COALESCE(n.status, 'published') = 'published'"
    else:
        # 管理台口径要认**节点墓碑**：清掉某一档的描述 = 移除那一档，落成一条
        # `target_status='archived'` 的草稿行（见 `skill_write` 的全量替换）。
        # 不排掉它，运营删完 L5 再点编辑，L5 还在 —— 而且怎么都删不掉，因为
        # 每次保存又生成同一条墓碑。前台不看这条：墓碑要到发布后才生效，
        # 发布前学员照常看到 L5（前台只读线上行，它的 target_status 恒为 NULL）。
        status_sql = (
            "AND COALESCE(n.status, 'published') NOT IN ('archived') "
            "AND COALESCE(n.target_status, '') <> 'archived'"
        )
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT n.*
            FROM kg_node n
            WHERE n.type = 'skill_level' AND {row_pick}
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

        nodes = [_node_dict(r, admin=not published_only) for r in rows]
        occ_row = conn.execute(
            f"""
            SELECT count(DISTINCT e.src_id) AS c
            FROM kg_edge e
            JOIN kg_node n ON n.id = e.dst_id AND n.type = 'skill_level'
                 AND {_ONLINE_N}
            JOIN kg_node o ON o.id = e.src_id AND o.type = 'occupation'
              AND COALESCE(o.status, 'published') = 'published'
              AND {online_only('o')}
            WHERE e.rel_type = 'requires'
              AND COALESCE(e.status, 'published') = 'published'
              AND ({key_expr}) = %s
            """,
            (sk,),
        ).fetchone()
        occ_c = int(occ_row["c"] if occ_row else 0)
        bundle = assemble_bundle(sk, nodes, region=reg, occupation_count=occ_c)
        attach_bundle_draft_state([bundle], conn=conn, admin=not published_only)
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


# ── 技能构成：全站唯一实现 ────────────────────────────────────
#
# 「取一个岗位/专业的技能构成」这件事，草稿态之前在全仓有 29 处独立 SQL、散在 12 个模块，
# 每处各自拼可见性口径。以前所有口径都是 published，抄多份看不出来；草稿态加了
# 「草稿优先」这一维之后立刻分家 —— 实测同一个管理台 scope 下，
# `/v1/admin/composition` 显示改后的权重，而 `/v1/kg/node-detail` 还是发布前那份，
# 运营看到「改了技能构成但数据没进草稿态」。
#
# 所以收敛成下面这一个函数，scope 参数化。**读侧的展示/计算路径一律调它，不要再拼 SQL。**
# 没收进来的三类（有意为之）：
#   - `publish_rules`：门禁必须永远只看**已发布**的事实（BR-03 算的是发布后的 Σweight），
#     口径固定，参数化只会让人误以为它能跟着 scope 变
#   - `skill_write` / `node_layout_meta`：写侧与派生缓存重算，不是展示读路径
#   - `counts` 的批量计数：一次算一批 id，形状是 `GROUP BY src_id`，
#     与「单实体取明细」不是同一个查询；它按 scope 复用本函数的**谓词**（见 _composition_pred）

_COMPOSITION_REL = {"occupation": "requires", "major": "covers"}


def _composition_pred(
    scope: str, *, edge: str = "e", node: str = "s"
) -> tuple[str, str]:
    """技能构成的可见性谓词 → (边条件, 技能节点条件)。**口径唯一真源。**

    - `public`：只看已发布的线上边与线上技能节点（前台）
    - `manage`：管理台口径 —— 草稿优先、墓碑（待归档/待删）算已删、排除 archived

    `edge` / `node` 是 SQL 别名：批量聚合类查询（`counts`、能力全景热力矩阵）
    的形状与「单实体取明细」不同，没法直接调 `entity_skill_composition`，
    但**可以共用这份谓词** —— 口径仍然只有一处。
    """
    from backend.kg.pg_store.config import (
        edge_not_archived,
        edge_published,
        node_not_archived,
        node_published,
        online_only,
        online_only_edge,
        prefer_draft,
        prefer_draft_edge,
    )

    if (scope or "").strip().lower() in ("manage", "admin", "all"):
        return (
            f"{edge_not_archived(edge)} AND {prefer_draft_edge(edge)} "
            f"AND COALESCE({edge}.target_status, '') NOT IN ('archived', 'deleted')",
            f"{node_not_archived(node)} AND {prefer_draft(node)}",
        )
    return (
        f"{edge_published(edge)} AND {online_only_edge(edge)}",
        f"{node_published(node)} AND {online_only(node)}",
    )


def composition_rows(
    node_id: str,
    *,
    scope: str = "public",
    rel_type: str | None = None,
    conn: Any = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """技能构成的**边级**原始行（每条 requires/covers 边一行），按 scope 取可见性。

    返回的每行都是 `_node_dict` 后的技能节点 + `edge` 字典（id/weight/confidence/…），
    直接可喂给 `group_nodes_to_bundles`。需要 bundle 形态的用
    `entity_skill_composition`，需要逐边明细的（构成编辑页）用本函数。
    """
    from backend.kg.pg_store.client import use_conn
    from backend.kg.pg_store.query import _node_dict

    e_pred, s_pred = _composition_pred(scope)
    manage = (scope or "").strip().lower() in ("manage", "admin", "all")
    with use_conn(conn) as c:
        if not rel_type:
            trow = c.execute(
                "SELECT type FROM kg_node WHERE id = %s LIMIT 1", (node_id,)
            ).fetchone()
            ntype = (trow or {}).get("type") or ""
            rel_type = _COMPOSITION_REL.get(ntype, "requires")
        rows = c.execute(
            f"""
            SELECT s.*, e.id AS edge_id, e.rel_type, e.weight, e.confidence,
                   e.evidence, e.is_draft AS edge_is_draft, e.target_status AS edge_target
            FROM kg_edge e
            JOIN kg_node s ON s.id = e.dst_id AND s.type = 'skill_level' AND {s_pred}
            WHERE e.src_id = %s AND e.rel_type = %s AND {e_pred}
            ORDER BY e.weight DESC NULLS LAST, s.name, s.id
            {"LIMIT %s" if limit else ""}
            """,
            (node_id, rel_type, limit) if limit else (node_id, rel_type),
        ).fetchall()
    out = []
    for r in rows:
        node = _node_dict(r, admin=manage)
        st = r.get("status") or "published"
        node["edge"] = {
            "id": r.get("edge_id"),
            "rel_type": r.get("rel_type") or rel_type,
            "weight": r.get("weight"),
            "confidence": r.get("confidence"),
            "evidence": r.get("evidence"),
            "is_draft": bool(r.get("edge_is_draft")),
        }
        if manage:
            # 「边是 published、指向的技能节点却被停用/草稿」是一种**存量异常**：
            # 前台按节点状态过滤看不到这条技能（5 项 Σ0.81），管理台口径 `<> archived`
            # 看得到（6 项 Σ1.00）—— 同一个岗位两个权重和。
            # **不在读侧偷偷过滤掉**：那样运营永远看不到异常，数据只会越烂。
            # 这里标出来，让构成页/详情页能显式提示「这条边指向不可见的技能」。
            node["endpoint_status"] = st
            node["dangling"] = st != "published"
        out.append(node)
    return out


def entity_skill_composition(
    node_id: str,
    *,
    scope: str = "public",
    conn: Any = None,
    limit: int | None = 100,
    rel_type: str | None = None,
) -> list[dict[str, Any]]:
    """岗位/专业的技能构成（bundle 形态）—— **全站唯一实现**。

    `scope="public"` 只看已发布边（前台）；`scope="manage"` 草稿优先 + 墓碑算已删
    （管理台）。同一实体在同一 scope 下，不论从哪个接口读，结果必须一致 ——
    这正是收敛它的理由。
    """
    rows = composition_rows(
        node_id,
        scope=scope,
        rel_type=rel_type,
        conn=conn,
        # 多取再聚合：一个 skill_key 可能有多条档位边
        limit=max((limit or 100) * 5, 200),
    )
    # 主线（0a6842a）在读路径加的去重：同一技能只保留最高要求档，权重合并过来。
    # 写路径已在重建前清旧边，但直连改库/其它采集脚本/历史数据都绕得过应用层 ——
    # 两侧都要站得住（与 attrs.level 脏值防护同一原则）。
    # **必须放在唯一实现里**：它原来长在 `occupation_skill_bundles` 里，
    # 那个函数已并进本函数；不接过来的话「同一技能挂 L2+L3」会让详情页按边算 10 项、
    # 列表页按 skill_key 算 7 项，主线刚修好的对不上又回来了。
    bundles = group_nodes_to_bundles(_dedupe_requires_by_skill(rows))
    # 异常标记按 skill_key 汇总上来：bundle 里任一档的边指向不可见节点就标 dangling
    # （用**去重前**的 rows 算：被去掉的低档边指向停用节点，同样是要让运营看见的异常）
    bad = {}
    for r in rows:
        if r.get("dangling"):
            bad.setdefault(skill_key_from_node(r), []).append(
                {"node_id": r.get("id"), "endpoint_status": r.get("endpoint_status")}
            )
    for b in bundles:
        hit = bad.get(b.get("skill_key"))
        if hit:
            b["dangling"] = True
            b["dangling_endpoints"] = hit
    return bundles[: limit] if limit else bundles


def attach_bundle_draft_state(
    bundles: list[dict[str, Any]], *, conn: Any = None, admin: bool = False
) -> None:
    """给 bundle 补 `status` / `record_status` / `has_draft` / `draft_change`（原地改）。

    技能是**逻辑实体**：一个 skill_key 由 L1–L5 五个节点组成，草稿态是按节点记的。
    聚合规则（契约里也写了这一条）：**任一档有草稿 → 整个 bundle 记 `draft`**。
    运营看的是「这个技能我改过没有」，不需要知道是哪一档在草稿里。

    `status` 一律按**线上行**聚合。不这么做的话：管理台口径下 `prefer_draft` 拿到的是
    草稿行（status 恒为 'draft'），聚合出来永远是 draft —— 和「库里 21074 个技能全是
    published」的事实对不上，运营分不清哪个技能真被改过。
    """
    if not admin or not bundles:
        return
    from backend.kg.pg_store.client import use_conn
    from backend.kg.pg_store.query import unit_draft_kinds

    ids = [i for b in bundles for i in (b.get("node_ids") or []) if i]
    if not ids:
        return
    kinds = unit_draft_kinds(ids, conn)
    with use_conn(conn) as c:
        rows = c.execute(
            "SELECT id, status, version, owner, owner_name, updated_by, updated_by_name "
            "FROM kg_node WHERE id = ANY(%s) AND NOT is_draft",
            (ids,),
        ).fetchall()
    online = {r["id"]: (r["status"] or "published") for r in rows}
    meta = {r["id"]: dict(r) for r in rows}
    for b in bundles:
        nids = [i for i in (b.get("node_ids") or []) if i]
        sts = [online[i] for i in nids if i in online]
        # 没有任何线上行 = 这个技能是新建的，只有草稿
        b["status"] = aggregate_bundle_status([{"status": x} for x in sts]) if sts else "draft"
        kind = next((kinds[i] for i in nids if i in kinds), None)
        b["has_draft"] = kind is not None
        b["record_status"] = "draft" if kind else b["status"]
        if kind:
            b["draft_change"] = kind
        # 版本 / 负责人 / 最近修改人：bundle 没有自己的行，取各档线上行聚合 ——
        # **版本取最大档**（任一档发过一版，这个逻辑技能就到了那一版）。
        # 不返回的话前端会 `|| "V1"` 兜出一个恒为 V1 的假版本号，
        # 与「状态列恒显示草稿」是同一个形状：接口不给、前端编一个看起来像真的值。
        ms = [meta[i] for i in nids if i in meta]
        if ms:
            vs = [int(m["version"] or 1) for m in ms]
            b["version"] = max(vs)
            b["version_label"] = f"V{max(vs)}"
            b["owner"] = next((m["owner"] for m in ms if m.get("owner")), None)
            b["owner_name"] = next(
                (m["owner_name"] or m["owner"] for m in ms if m.get("owner_name") or m.get("owner")),
                None,
            )
            b["updated_by_name"] = next(
                (m["updated_by_name"] or m["updated_by"] for m in ms
                 if m.get("updated_by_name") or m.get("updated_by")),
                None,
            )


def occupation_skill_bundles(
    occupation_id: str, *, limit: int = 100, scope: str = "public"
) -> list[dict[str, Any]]:
    """岗位 requires → 逻辑技能 bundle 列表（权重取自边）。

    保留这个名字是因为调用点多（学员端、报告、诊断）；实现已并入
    `entity_skill_composition`，口径只有那一份。

    `scope="manage"` 按管理台口径放行 draft / disabled 技能（仍挡 archived），
    默认 public 只给 published。**口径必须跟着调用方走**：管理台的岗位详情、
    技能构成、列表计数如果各用各的，同一个岗位就会显示出三个技能数 ——
    库里正好有 64 个 draft、5 个 disabled、30 个 archived 技能，够把三个数字全岔开。
    """
    return entity_skill_composition(
        occupation_id, scope=scope, limit=limit, rel_type="requires"
    )


def _dedupe_requires_by_skill(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一岗位同一技能只保留**最高要求档** —— 高档天生覆盖低档。

    为什么读路径也要做：写路径已在 link_boss_skill_chain 重建前清旧边，但
    直连改库、其它采集脚本、历史数据都绕得过应用层，两侧都要站得住
    （与 attrs.level 脏值防护同一原则）。

    症状很隐蔽：`.NET Core开发` 同时挂 L2 和 L3 时，列表页按 skill_key 聚合算 7 项、
    详情页按边算 10 项，两处数字对不上，而且低档那条往往 weight 为空，
    白白拉低权重和。

    保留规则：档位高者优先；档位相同则取有权重的那条。权重取各档之和
    （低档边通常无权重，不会影响 Σ≈1）。
    """
    best: dict[str, dict[str, Any]] = {}
    for n in nodes:
        key = skill_key_from_node(n)
        if not key:
            continue
        lv = level_from_node(n) or 0
        w = (n.get("edge") or {}).get("weight")
        prev = best.get(key)
        if prev is None:
            best[key] = n
            continue
        prev_lv = level_from_node(prev) or 0
        prev_w = (prev.get("edge") or {}).get("weight")
        take_new = (lv, w is not None) > (prev_lv, prev_w is not None)
        keep, drop = (n, prev) if take_new else (prev, n)
        # 权重合并到保留项，避免丢掉挂在被丢弃那档上的权重
        kw, dw = (keep.get("edge") or {}).get("weight"), (drop.get("edge") or {}).get("weight")
        if dw is not None:
            keep.setdefault("edge", {})["weight"] = float(kw or 0) + float(dw)
        best[key] = keep
    return list(best.values())


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


# 课程资源分类 —— 判定依据是 **attrs.role**，不是 source_system。
#
#   real     点开**当场就能学**：免登录、免报名、无开课周期
#   enroll   是真课程，但要登录报名 / 按学期开课，往期只剩介绍页
#   catalog  课标目录条目，点开是专业培养方案，**没有具体课程内容**
#   landing  检索入口，点开是搜索结果页
#
# 这套口径是被两次打脸打出来的，别再往回改：
# 1. 曾把 MOE_CN 归进「真实课程」，点开是教育部课标**目录页** —— 那 15960 门是
#    课标里的「课目名称」，`role=curriculum_catalog`，本体写明「不能单独冒充可学资源」。
# 2. `real` 当初只保证「是个课程页」，却被当成「点开可学」，于是 129 门中国大学MOOC
#    顶着「真课」标签挂在 67 个岗位上，学员点进去全是报名墙，还有一批往期课程已结课。
#    2026-08-18 起 MOOC / 学堂在线归入 enroll，库里那批已 archived
#    （`scripts/archive_courses.py`，要恢复加 --restore）。
_ROLE_LEARNABLE = "learnable_resource"
_ROLE_CATALOG = "curriculum_catalog"
# ⚠ 新增课程来源时**必须同步这两个元组之一**，否则 _course_kind 会落到 fallback 分支
# 把它判成 catalog，默认过滤掉 —— 节点入了库但页面上看不到（已踩过一次：OFFICIAL_DOCS）。
_REAL_COURSE_SOURCES = ("SMART_EDU_VOC", "OFFICIAL_DOCS")
# 从 config 借用，不另抄一份：过滤层（learnable_course 的 SQL）与展示层（这里判
# kind）必须同源，否则只同步一处就会出现「列表里没有、详情里却标着可直接学」。
_ENROLL_COURSE_SOURCES = _CFG_ENROLL_SOURCES
# 课标条目自带的 source_url 是「专业教学标准」大类目录页，对学员无意义；
# 真要展示时改用按课程名生成的检索地址
_MOOC_SEARCH = "https://www.icourse163.org/search.htm?search="
_PLATFORM_LABEL = {
    "ICOURSE163": "中国大学MOOC",
    "XUETANGX": "学堂在线",
    "SEARCH_LANDING_CN": "检索入口",
    "MOE_CN": "专业课标目录",
    "SMART_EDU_VOC": "国家智慧教育",
    "OFFICIAL_DOCS": "官方文档",
}


def _course_kind(role: str | None, platform: str, match_method: str | None) -> str:
    """课程资源性质。role 优先，其次看来源与匹配方式。"""
    if role == _ROLE_CATALOG:
        return "catalog"
    if match_method == "search_landing" or platform == "SEARCH_LANDING_CN":
        return "landing"
    if platform in _ENROLL_COURSE_SOURCES:
        return "enroll"
    if role == _ROLE_LEARNABLE and platform in _REAL_COURSE_SOURCES:
        return "real"
    return "real" if platform in _REAL_COURSE_SOURCES else "catalog"


def occupation_courses(
    occupation_id: str,
    *,
    limit_per_skill: int = 5,
    include_catalog: bool = False,
) -> dict[str, Any]:
    """岗位相关课程：occupation -requires-> skill_level -taught_by-> course。

    独立于 `occupation_skill_composition`：岗位详情接口不变，这里单独聚合课程。

    **默认排除课标目录条目**（`include_catalog=False`）：那 15960 门 MOE_CN 课程的
    `source_url` 全指向教育部「职业教育专业教学标准」的**大类列表页**，点进去是
    「农林牧渔大类 / 资源环境与安全大类 / …」，与具体技能毫无关系 —— 对学员零价值。

    它们在**专业培养方案**语境下有意义（major 的课程体系），但在「学员想学这个技能」
    的语境下不该出现。实测技术类 112 个有资源的岗位中，没有任何一个是「只剩课标目录」，
    所以排除它不会让任何岗位变空。

    要看课标体系传 `include_catalog=True`（管理台/专业维度用）；届时返回的
    `search_url` 是按课程名生成的慕课检索地址，比那个大类目录页有用。
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
    real_n = enroll_n = catalog_n = landing_n = 0
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
                "category_name": _cat_name(r["category"]),
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
            if kind == "catalog":
                catalog_n += 1
                if not include_catalog:
                    continue  # 计数保留（便于观察数据缺口），但不返回给学员
            elif kind == "real":
                real_n += 1
            elif kind == "enroll":
                enroll_n += 1
            else:
                landing_n += 1
            url = r["url"]
            search_url = None
            if kind == "catalog":
                # 课标条目那个大类目录页没意义，给个按课程名的检索地址兜底
                search_url = _MOOC_SEARCH + urllib.parse.quote(str(r["course_name"] or ""))
            g["courses"].append({
                "id": r["course_id"],
                "name": r["course_name"],
                "url": url,
                "search_url": search_url,
                "platform": platform,
                "platform_label": _PLATFORM_LABEL.get(platform, platform),
                "kind": kind,
                "learner_count": attrs.get("learner_count"),
                "school": attrs.get("school"),
                "img_url": attrs.get("img_url"),
            })

    # catalog 被过滤后可能出现空组，不返回空壳
    items = sorted((g for g in groups.values() if g["courses"]),
                   key=lambda x: -(x["weight"] or 0))
    return {
        "occupation": {
            "id": occ.get("id"),
            "name": occ.get("display_name") or occ.get("name"),
        },
        "by_skill": items,
        "skill_count": len(items),
        "course_count": real_n + enroll_n + catalog_n + landing_n,
        "real_course_count": real_n,
        "enroll_count": enroll_n,
        "catalog_count": catalog_n,
        "catalog_hidden": (not include_catalog) and catalog_n > 0,
        "landing_count": landing_n,
        "note": (
            "kind=real 免登录免报名、点开当场能学；"
            "kind=enroll 是真课程但要报名或按学期开课（MOOC 类）；"
            "kind=catalog 课标目录条目，点开是专业培养方案而非课程内容；"
            "kind=landing 检索入口，点开是搜索结果页。"
            "只有 real 才是真正可学的资源。"
        ),
    }
