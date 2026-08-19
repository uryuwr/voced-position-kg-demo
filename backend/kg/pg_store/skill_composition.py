"""技能构成编辑：专业直连技能（covers）与岗位技能（requires）。

对应管理端原型「技能构成」抽屉的交互：
- 底部下拉从**已有技能**里选，点「添加」入列
- 每行 L1–L5 档位按钮选**要求等级**
- 岗位行还有权重输入，并支持一键**归一化**到 1.00

边模型
------
- 岗位：`occupation -requires-> skill_level(选中档)`，带 weight
- 专业：`major -covers-> skill_level(选中档)`，**不带权重**（需求：专业技能不做归一）
  covers 是 schema 里既有的 E4（domain=major, range=skill_level），此前库内 0 条。

「选中等级」用**边指向哪个等级节点**表达：一个技能有 L1–L5 五个 skill_level 节点，
边指向 L3 即表示要求 L3。改等级 = 换边的 dst_id，因此实现为「先删旧边再建新边」。
"""
from __future__ import annotations

from typing import Any

from backend.kg.pg_store.client import connect
from backend.kg.pg_store.config import (
    attrs_level_int,
    edge_not_archived,
    edge_published,
    prefer_draft,
    prefer_draft_edge,
)
from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL

_EP = edge_published("e")
# 管理台口径（draft / disabled 要能看到并编辑，只挡 archived）**不在这里定义**：
# 主线 0a6842a 在本文件加过 `_NOT_ARCHIVED_N = node_not_archived("n")` 来修
# 「边指向已归档技能，构成页照样列出、详情页不算 → 同一岗位两个技能数」，
# 合并后那条 SQL 已换成 `skill_aggregate.composition_rows(scope="manage")`，
# 它的谓词 `_composition_pred('manage')` = `node_not_archived + prefer_draft`，
# 完整覆盖了主线那个意图（还多了草稿优先），所以那个常量成了死代码，删掉。
_LEVEL_N = attrs_level_int("n")
# 管理台口径（`<> 'archived'`）对草稿行同样成立 —— 每处都要跟 prefer_draft 去重，
# 否则技能下拉、档位明细、构成列表里同一条数据出现两次（方案 §6.2）。
_PD = prefer_draft()
_PD_N = prefer_draft("n")

# 技能构成是**边**不是节点字段，所以它的草稿全是草稿边，unit_id = 本节点 id（方案 §3）：
# 「改岗位 A 的技能构成」= 改若干 A -requires-> skill 边，发布 A 时一起生效。
#
# 构成页要给运营看「改完之后长什么样」，所以读的是**草稿视图**：
#   管理台口径（含 draft）+ 草稿优先 + 墓碑算已删。
# 用 edge_published 是错的 —— 草稿边的 status 恒为 'draft'，运营会看不到自己刚改的东西。
_EDGE_DRAFT_VIEW = (
    f"{edge_not_archived('e')} AND {prefer_draft_edge('e')} "
    f"AND COALESCE(e.target_status, '') <> 'archived'"
)

# 节点类型 → (关系, 该类型是否带权重)
_REL = {"major": ("covers", False), "occupation": ("requires", True)}


class CompositionError(ValueError):
    """技能构成操作失败（类型不支持 / 技能不存在 / 等级不存在等）。"""


class SkillExistsError(CompositionError):
    """该技能已在构成中。

    一个实体对一个技能只能有一个要求档——高级技能天然包含低级，同时挂 L1 和 L3
    没有意义，且会让管理台按边求和的权重与前台按 skill_key 聚合的权重对不上。
    """

    def __init__(self, message: str, *, skill_key: str, current_level: int | None):
        super().__init__(message)
        self.skill_key = skill_key
        self.current_level = current_level


def _rel_for(node_type: str) -> tuple[str, bool]:
    if node_type not in _REL:
        raise CompositionError(
            f"仅专业(major)与岗位(occupation)支持技能构成，当前类型：{node_type}"
        )
    return _REL[node_type]


def _sk_from(node: dict[str, Any]) -> str:
    """技能节点 → skill_key。口径与 SKILL_KEY_SQL 同源（skill_aggregate 里那一份）。"""
    from backend.kg.pg_store.skill_aggregate import skill_key_from_node

    return skill_key_from_node(node)


def _node(conn, node_id: str) -> dict[str, Any]:
    row = conn.execute(
        f"SELECT id, type, name, region, status, level, attrs, description, "
        f"version, owner_name, is_draft, target_status FROM kg_node WHERE id=%s "
        f"AND COALESCE(status,'published') <> 'archived' AND {_PD}",
        (node_id,),
    ).fetchone()
    if not row:
        raise CompositionError(f"节点不存在：{node_id}")
    return dict(row)


def _header(conn, node: dict[str, Any]) -> dict[str, Any]:
    """构成页头部信息：原型「岗位模型 · 内容运营专员」上方那几项。

    岗位：所属行业 / 关联专业 / 职级 / 薪资 / 状态
    专业：所属行业 / 专业名称 / 状态（需求补充）
    """
    import json as _json

    nid, ntype = node["id"], node["type"]
    try:
        attrs = _json.loads(node["attrs"]) if isinstance(node["attrs"], str) else (node["attrs"] or {})
    except Exception:
        attrs = {}

    industries = conn.execute(
        f"""SELECT n.id, n.name FROM kg_edge e
            JOIN kg_node n ON n.id=e.dst_id AND n.type='industry' AND {_PD_N}
            WHERE e.src_id=%s AND e.rel_type='belongs_to' AND {_EP}""",
        (nid,),
    ).fetchall()
    majors: list[Any] = []
    if ntype == "occupation":
        majors = conn.execute(
            f"""SELECT n.id, n.name FROM kg_edge e
                JOIN kg_node n ON n.id=e.src_id AND n.type='major' AND {_PD_N}
                WHERE e.dst_id=%s AND e.rel_type='prepares_for' AND {_EP}""",
            (nid,),
        ).fetchall()
        # 岗位若无直连行业，回落取所属专业的行业（原型「所属行业」总是有值）
        if not industries and majors:
            industries = conn.execute(
                f"""SELECT DISTINCT n.id, n.name FROM kg_edge e
                    JOIN kg_node n ON n.id=e.dst_id AND n.type='industry' AND {_PD_N}
                    WHERE e.src_id = ANY(%s) AND e.rel_type='belongs_to' AND {_EP}""",
                ([m["id"] for m in majors],),
            ).fetchall()
    elif ntype == "major":
        # 专业下的岗位数，便于构成页顺带看规模
        majors = []
    return {
        "id": nid,
        "type": ntype,
        "name": node.get("name"),
        "status": node.get("status"),
        "level": node.get("level"),
        "version_label": f"V{node.get('version') or 1}",
        "owner_name": node.get("owner_name"),
        "description": node.get("description"),
        "industries": [dict(i) for i in industries],
        "industry_name": "、".join(i["name"] for i in industries) or None,
        "majors": [dict(m) for m in majors],
        "major_name": "、".join(m["name"] for m in majors) or None,
        # 原型头部「职级 / 薪资」
        "level_label": f"L{node.get('level')}" if node.get("level") is not None else None,
        "salary": attrs.get("salary"),
        "demand": attrs.get("demand"),
        "code": attrs.get("code"),
    }


def _levels_of(conn, keys: list[str], region: str = "CN") -> dict[str, list[dict[str, Any]]]:
    """取每个 skill_key 的档位明细：产品等级 + 档位文案 + 要求描述。

    描述用于管理端「选档时看要求」，故一并带出，前端不必再逐档发请求。
    """
    if not keys:
        return {}
    rows = conn.execute(
        f"""
        SELECT ({SKILL_KEY_SQL}) AS k,
               n.attrs, n.description, n.id
        FROM kg_node n
        WHERE n.type='skill_level' AND n.region=%s AND ({SKILL_KEY_SQL}) = ANY(%s)
          AND COALESCE(n.status,'published') <> 'archived' AND {_PD_N}
        """,
        (region, keys),
    ).fetchall()
    from backend.kg.pg_store.skill_aggregate import level_from_node
    from backend.kg.pg_store.skill_level_meta import label_map

    labels = label_map()
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        import json as _json

        try:
            a = _json.loads(r["attrs"]) if isinstance(r["attrs"], str) else (r["attrs"] or {})
        except Exception:
            a = {}
        lv = level_from_node({"attrs": a})
        desc = (r["description"] or "").strip()
        bucket = out.setdefault(r["k"], {})
        # 同一技能名被多个岗位共用，各岗位都有独立的 skill_level 节点，
        # 按 skill_key 聚合会出现十几个同档条目 → 按产品等级去重，
        # 并优先保留「像要求描述」的那条（含"能够/掌握"，而非岗位权重串）。
        prev = bucket.get(lv)
        looks_req = bool(desc) and any(w in desc for w in ("能够", "掌握", "熟悉", "了解", "会"))
        if prev is None or (looks_req and not prev.get("_looks_req")):
            bucket[lv] = {
                "level": lv,
                # 产品档文案（了解/掌握/熟练/精通/专家），取自 skill_level_meta
                "level_label": labels.get(lv) if lv else None,
                "node_id": r["id"],
                # 档位要求说明（截断，避免灌入整段国标正文）
                "requirement": desc[:300] or None,
                "_looks_req": looks_req,
            }
        bucket[lv]["node_count"] = bucket[lv].get("node_count", 0) + 1
    final: dict[str, list[dict[str, Any]]] = {}
    for k, bucket in out.items():
        items = sorted(bucket.values(), key=lambda x: (x["level"] or 99))
        for it in items:
            it.pop("_looks_req", None)
        final[k] = items
    return final


def list_skill_options(
    q: str | None = None, region: str = "CN", limit: int = 50
) -> list[dict[str, Any]]:
    """可选技能（下拉用）：按 skill_key 聚合，附各技能已配齐的等级与档位要求。"""
    kw = f"%{(q or '').strip()}%"
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT ({SKILL_KEY_SQL}) AS skill_key,
                   min(n.category) AS category
            FROM kg_node n
            WHERE n.type='skill_level' AND n.region=%s
              AND COALESCE(n.status,'published') <> 'archived' AND {_PD_N}
              AND (%s = '%%%%' OR ({SKILL_KEY_SQL}) ILIKE %s)
            GROUP BY 1
            ORDER BY 1
            LIMIT %s
            """,
            (region, kw, kw, limit),
        ).fetchall()
        keys = [r["skill_key"] for r in rows]
        detail = _levels_of(conn, keys, region)

    out = []
    for r in rows:
        levels = detail.get(r["skill_key"], [])
        # 该技能已配齐的产品档，前端据此决定 L1–L5 哪些按钮可选
        plv = sorted({x["level"] for x in levels if x["level"]})
        out.append(
            {
                "skill_key": r["skill_key"],
                "category": r["category"],
                "available_levels": plv,
                "levels": levels,          # 含每档 level_label 与 requirement
                "level_completeness": f"{len(plv)}/5",
            }
        )
    return out


def get_composition(node_id: str) -> dict[str, Any]:
    """当前技能构成：每项含 skill_key、该技能全部等级、选中等级、权重。"""
    with connect() as conn:
        node = _node(conn, node_id)
        rel, weighted = _rel_for(node["type"])
        # 构成页与管理台详情、列表计数必须给同一个答案 → 走唯一实现（scope=manage）。
        # 这里要的是**逐边**明细（每行带 edge_id 与选中档），所以取 composition_rows
        # 而不是 bundle 形态。
        from backend.kg.pg_store.skill_aggregate import composition_rows

        raw = composition_rows(node_id, scope="manage", rel_type=rel, conn=conn)
        rows = [
            {
                "edge_id": (r.get("edge") or {}).get("id"),
                "weight": (r.get("edge") or {}).get("weight"),
                "dst_id": r.get("id"),
                "skill_key": _sk_from(r),
                "category": r.get("category"),
                "attrs": r.get("attrs"),
                "skill_node_name": r.get("name"),
                # 指向不可见（停用/归档）技能节点的异常边：标出来给运营看，
                # 前台看不到这条技能，管理台看得到 —— 不标就永远没人发现
                "dangling": bool(r.get("dangling")),
                "endpoint_status": r.get("endpoint_status"),
            }
            for r in raw
        ]

        keys = [r["skill_key"] for r in rows]
        # 档位明细（含每档要求描述），供前端选档时展示
        level_detail = _levels_of(conn, keys, node["region"] or "CN")
        all_levels = {
            k: sorted({x["level"] for x in v if x["level"]}) for k, v in level_detail.items()
        }
        # 先修：复用 skill_prereq.prereq_map，同一条连接内查完。
        # 语义是「不限本节点技能集」——见该函数 docstring
        from backend.kg.pg_store.skill_prereq import prereq_map

        pmap = prereq_map(conn, keys, region=node["region"] or None)

    from backend.kg.pg_store.skill_aggregate import level_from_node

    items = []
    wsum = 0.0
    import json as _json

    for r in rows:
        raw_w = r["weight"]
        w = float(raw_w or 0)         # Σ 里 NULL 当 0
        wsum += w
        try:
            _a = _json.loads(r["attrs"]) if isinstance(r["attrs"], str) else (r["attrs"] or {})
        except Exception:
            _a = {}
        sel = level_from_node({"attrs": _a})
        avail = all_levels.get(r["skill_key"], [])
        items.append(
            {
                "edge_id": r["edge_id"],
                "skill_key": r["skill_key"],
                "category": r["category"],
                # 原型第一列副行展示「先修」；不限本节点技能集内
                "prereqs": pmap.get(r["skill_key"], []),
                "skill_level_id": r["dst_id"],
                # 该技能配齐的全部档 int 1–5（用于渲染档位按钮的可选性）
                "available_levels": avail,
                # 各档要求说明：选档时给运营看「这一档要求什么」
                "levels": level_detail.get(r["skill_key"], []),
                # 用户选中的档（边指向哪个等级节点）
                "selected_level": sel,
                # 没配权重就是 None，不要显示成 0 —— 详情页（bundle 侧）给的是 None，
                # 这里给 0 的话「四处一致」就差在这一个字段上
                "weight": (w if raw_w is not None else None) if weighted else None,
                "weight_pct": round(w * 100) if weighted and w else None,
                "dangling": bool(r.get("dangling")),
                "endpoint_status": r.get("endpoint_status"),
            }
        )
    with connect() as conn:
        header = _header(conn, node)
    return {
        # 头部：原型构成页上方的 所属行业 / 关联专业 / 职级 / 薪资 / 状态
        "node": header,
        "relation": rel,
        "weighted": weighted,
        "items": items,
        "weight_sum": round(wsum, 4) if weighted else None,
        "normalized": bool(weighted and 0.995 <= wsum <= 1.005),
        "can_normalize": weighted,
        "counts": {"skill": len(items)},
    }


def _find_level_node(conn, skill_key: str, level: int | None, region: str) -> dict[str, Any]:
    """定位 skill_key 在指定产品等级(1-5)的 skill_level 节点。

    level 为空时取该技能最高档——运营只选技能不选档位时的默认行为。
    """
    rows = conn.execute(
        f"""
        SELECT n.id, {_LEVEL_N} AS level, n.attrs
        FROM kg_node n
        WHERE n.type='skill_level' AND n.region=%s AND ({SKILL_KEY_SQL})=%s
          AND COALESCE(n.status,'published') <> 'archived'
        """,
        (region, skill_key),
    ).fetchall()
    if not rows:
        raise CompositionError(f"技能不存在：{skill_key}")

    cand = sorted(((r["level"] or 0, dict(r)) for r in rows), key=lambda x: -x[0])
    if level:
        for lv, r in cand:
            if lv == int(level):
                return r
        have = sorted({lv for lv, _ in cand if lv})
        raise CompositionError(
            f"技能「{skill_key}」没有 L{level} 档；已配齐的档位：{have or '无'}"
        )
    return cand[0][1]


def set_skill(
    node_id: str,
    skill_key: str,
    *,
    level: int | None = None,
    weight: float | None = None,
    only_if_absent: bool = False,
    user_id: str,
    user_name: str,
) -> dict[str, Any]:
    """添加/更新一项技能。改等级即换边端点，故先删同 skill_key 的旧边再建。

    only_if_absent=True 用于「添加」入口：该技能已在构成中就报错，而不是静默改档。
    改档入口（点档位按钮）保持默认 False。
    """
    from backend.kg.pg_store.write import archive_edge, create_edge, patch_edge_draft

    with connect() as conn:
        node = _node(conn, node_id)
        rel, weighted = _rel_for(node["type"])
        cur_edges = _edges_of_skill(conn, node_id, rel, skill_key)
        if only_if_absent and cur_edges:
            lv = cur_edges[0]["level"]
            raise SkillExistsError(
                f"技能「{skill_key}」已在构成中"
                + (f"（当前 L{lv}）" if lv else "")
                + "；一个技能只保留一个要求档，如需调整请直接点档位按钮",
                skill_key=skill_key,
                current_level=lv,
            )
        if level is None and cur_edges:
            # **只改权重时不许动档位。** 管理台的权重输入框失焦后发的是
            # `PUT {skill_key, weight}`，不带 level；而 `_find_level_node(level=None)`
            # 的语义是「取该技能最高档」——于是「把权重从 0.2 改成 0.25」会顺手把
            # 要求档从 L3 跳到 L5。UI 上一点就中，接口单测看不出来（单测都会传 level）。
            # 已在构成里的技能，level 缺省就是「保持现状」。
            target = {"id": cur_edges[0]["dst_id"]}
        else:
            target = _find_level_node(conn, skill_key, level, node["region"] or "CN")

    if weighted:
        if weight is not None:
            w = float(weight)
        else:
            # **改档不该丢权重**：管理台点 L4 只发 {skill_key, level}，不带 weight。
            # 以前这里直接落 None，于是「改一下档位」把该技能的权重清成空，
            # Σweight 从 1.00 掉到 0.80，BR-03 当场不过 —— UI 上一点就能撞到。
            w = next(
                (
                    float(e["weight"])
                    for e in cur_edges
                    if e.get("weight") is not None
                ),
                None,
            )
    else:
        w = None                      # 专业 covers 不带权重
    keep: dict[str, Any] | None = next(
        (e for e in cur_edges if e["dst_id"] == target["id"]), None
    )
    # 改档 = 换边的端点：旧档那条建墓碑草稿（线上行留着，发布时才归档），
    # 新档那条建草稿边。**不能像以前那样裸 DELETE 线上边** —— 那会让「删」立刻
    # 对前台生效、「加」却要等发布，一个动作一半生效。
    for e in cur_edges:
        if keep is not None and e["edge_id"] == keep["edge_id"]:
            continue
        archive_edge(e["edge_id"], user_id=user_id, user_name=user_name)
    if keep is not None:
        # 档位没变、只调权重：原地改草稿行的 weight
        patch_edge_draft(
            keep["edge_id"],
            {"weight": w} if weighted else {},
            user_id=user_id,
            user_name=user_name,
        )
    else:
        create_edge(
            {
                "src_id": node_id,
                "dst_id": target["id"],
                "rel_type": rel,
                "region": node["region"] or "CN",
                # 专业侧不带权重（需求：专业技能不做归一化）
                "weight": w,
                # 发布后这条边应当是 published；草稿行自己的 status 恒为 draft
                "status": "published",
                "confidence": "manual_seed",
                "source_system": "MANUAL",
                "source_url": "manual://admin-composition",
                "evidence": f"管理端技能构成编辑：{node['name']} → {skill_key}",
            },
            user_id=user_id,
            user_name=user_name,
        )
    return get_composition(node_id)


def _edges_of_skill(
    conn, node_id: str, rel: str, skill_key: str
) -> list[dict[str, Any]]:
    """本节点当前**有效**指向该 skill_key 的边（草稿优先、墓碑算已删），按档降序。"""
    rows = conn.execute(
        f"""
        SELECT e.id AS edge_id, e.dst_id, e.weight, e.is_draft,
               {_LEVEL_N} AS level
        FROM kg_edge e
        JOIN kg_node n ON n.id = e.dst_id AND n.type='skill_level' AND {_PD_N}
        WHERE e.src_id=%s AND e.rel_type=%s AND ({SKILL_KEY_SQL})=%s
          AND {_EDGE_DRAFT_VIEW}
        ORDER BY 5 DESC NULLS LAST
        """,
        (node_id, rel, skill_key),
    ).fetchall()
    return [dict(r) for r in rows]


def remove_skill(
    node_id: str, skill_key: str, *, user_id: str, user_name: str
) -> dict[str, Any]:
    """移除一项技能：该 skill_key 的每条边都建一条待归档的草稿（墓碑）。

    从前这里是**裸 DELETE 线上边**，绕过了 write.py，所以「移除技能」当场对前台生效。
    现在线上行留着不动，发布该岗位/专业时才归档 —— 这是本需求最初报的那个 bug。
    """
    from backend.kg.pg_store.write import archive_edge

    with connect() as conn:
        node = _node(conn, node_id)
        rel, _w = _rel_for(node["type"])
        cur_edges = _edges_of_skill(conn, node_id, rel, skill_key)
    if not cur_edges:
        raise CompositionError(f"该节点下未找到技能：{skill_key}")
    for e in cur_edges:
        archive_edge(e["edge_id"], user_id=user_id, user_name=user_name)
    return get_composition(node_id)


def normalize_weights(node_id: str, *, user_id: str, user_name: str) -> dict[str, Any]:
    """把岗位技能权重按比例缩放到和为 1.00（仅岗位；专业无权重）。

    等比缩放而非平均分配：保留运营已设定的相对重要性。
    末位吸收舍入误差，确保和精确为 1.00。
    """
    from backend.kg.pg_store.write import patch_edge_draft

    with connect() as conn:
        node = _node(conn, node_id)
        rel, weighted = _rel_for(node["type"])
        if not weighted:
            raise CompositionError("专业技能不带权重，无需归一化")
        # 按**草稿视图**算：运营刚加/刚删的技能要参与归一，否则归完还是不等于 1
        rows = conn.execute(
            f"""SELECT e.id, e.weight FROM kg_edge e
                WHERE e.src_id=%s AND e.rel_type=%s AND {_EDGE_DRAFT_VIEW}
                ORDER BY e.weight DESC NULLS LAST, e.id""",
            (node_id, rel),
        ).fetchall()
        if not rows:
            raise CompositionError("该岗位还没有技能，无法归一化")

        total = sum(float(r["weight"] or 0) for r in rows)
        n = len(rows)
        if total <= 0:
            # 全为空/0：退化为均分，否则无从推断相对重要性
            base = round(1.0 / n, 4)
            new = [base] * n
        else:
            new = [round(float(r["weight"] or 0) / total, 4) for r in rows]
        new[-1] = round(1.0 - sum(new[:-1]), 4)   # 末位吸收舍入误差

    # 归一化也是编辑动作：逐条落到草稿行，线上权重发布后才变
    for r, w in zip(rows, new):
        patch_edge_draft(
            r["id"], {"weight": w}, user_id=user_id, user_name=user_name
        )
    out = get_composition(node_id)
    out["normalized_from"] = round(total, 4)
    return out
