"""节点关联计数：联读批量聚合（非物化表）。"""
from __future__ import annotations

from typing import Any

from backend.kg.pg_store.client import use_conn
from backend.kg.pg_store.config import edge_published, online_only
from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL

# 关联计数不能把归档/草稿边算进去，否则前端显示的关联数虚高
EP_E = edge_published("e")
EP_PF = edge_published("pf")
EP_RQ = edge_published("rq")

_PUB_N = "COALESCE(n.status, 'published') = 'published'"
_PUB_E = "COALESCE(e.status, 'published') = 'published'"
_PUB_O = "COALESCE(o.status, 'published') = 'published'"
_PUB_M = "COALESCE(m.status, 'published') = 'published'"
_PUB_S = "COALESCE(s.status, 'published') = 'published'"
_PUB_I = "COALESCE(i.status, 'published') = 'published'"
# 管理台分支（`NOT IN ('archived','disabled')`）会同时命中草稿行 —— 一条边 JOIN 出两行，
# 关联计数翻倍，而 `industries_for_occupations` 还会把**草稿里的新名字**当成行业名返回，
# 那是货真价实的前台泄漏（`/v1/node?include_counts=1` 就走这条路）。
# 计数与关联名看的是**线上现状**，所以钉线上行，不用 prefer_draft。
_PD_I = online_only("i")
_PD_M = online_only("m")


def _empty_counts() -> dict[str, Any]:
    return {
        "major": 0,
        "occupation": 0,
        "skill": 0,
        "industry": 0,
        "course": 0,
        "level": 0,
        # 原型「岗位模型」列表的「权重和」列（国标权重是否配满 1.00）
        "weight_sum": 0.0,
        # 专业：经岗位两跳聚合的技能数（参考值；「基础技能」列用 skill=直连数）
        "skill_aggregated": 0,
    }


def counts_for_industries(
    ids: list[str], *, conn: object | None = None
) -> dict[str, dict[str, int]]:
    """industry: major（直连 belongs_to）、occupation（直连 belongs_to）。"""
    out = {i: _empty_counts() for i in ids}
    if not ids:
        return out
    with use_conn(conn) as conn:
        # occupation -belongs_to→ industry
        rows = conn.execute(
            f"""
            SELECT e.dst_id AS id, count(DISTINCT e.src_id) AS c
            FROM kg_edge e
            JOIN kg_node o ON o.id = e.src_id AND o.type = 'occupation' AND {_PUB_O}
            WHERE e.rel_type = 'belongs_to' AND {EP_E}
              AND e.dst_id = ANY(%s)
              AND {_PUB_E}
            GROUP BY e.dst_id
            """,
            (ids,),
        ).fetchall()
        for r in rows:
            if r["id"] in out:
                out[r["id"]]["occupation"] = int(r["c"])

        # major -belongs_to→ industry（当前数据可能为 0）
        rows = conn.execute(
            f"""
            SELECT e.dst_id AS id, count(DISTINCT e.src_id) AS c
            FROM kg_edge e
            JOIN kg_node m ON m.id = e.src_id AND m.type = 'major' AND {_PUB_M}
            WHERE e.rel_type = 'belongs_to' AND {EP_E}
              AND e.dst_id = ANY(%s)
              AND {_PUB_E}
            GROUP BY e.dst_id
            """,
            (ids,),
        ).fetchall()
        for r in rows:
            if r["id"] in out:
                out[r["id"]]["major"] = int(r["c"])
    return out


def counts_for_majors(
    ids: list[str], *, conn: object | None = None
) -> dict[str, dict[str, int]]:
    """major: occupation(prepares_for), skill(两跳 distinct skill_key)。"""
    out = {i: _empty_counts() for i in ids}
    if not ids:
        return out
    with use_conn(conn) as conn:
        rows = conn.execute(
            f"""
            SELECT e.src_id AS id, count(DISTINCT e.dst_id) AS c
            FROM kg_edge e
            JOIN kg_node o ON o.id = e.dst_id AND o.type = 'occupation' AND {_PUB_O}
            WHERE e.rel_type = 'prepares_for' AND {EP_E}
              AND e.src_id = ANY(%s)
              AND {_PUB_E}
            GROUP BY e.src_id
            """,
            (ids,),
        ).fetchall()
        for r in rows:
            if r["id"] in out:
                out[r["id"]]["occupation"] = int(r["c"])

        # major → occ → skill_level，按 skill_key 去重
        rows = conn.execute(
            f"""
            SELECT pf.src_id AS id, count(DISTINCT ({SKILL_KEY_SQL})) AS c
            FROM kg_edge pf
            JOIN kg_node o ON o.id = pf.dst_id AND o.type = 'occupation' AND {_PUB_O}
            JOIN kg_edge rq ON rq.src_id = o.id AND rq.rel_type = 'requires' AND {EP_RQ}
              AND COALESCE(rq.status, 'published') = 'published'
            JOIN kg_node n ON n.id = rq.dst_id AND n.type = 'skill_level' AND {_PUB_N}
            WHERE pf.rel_type = 'prepares_for' AND {EP_PF}
              AND pf.src_id = ANY(%s)
              AND COALESCE(pf.status, 'published') = 'published'
            GROUP BY pf.src_id
            """,
            (ids,),
        ).fetchall()
        for r in rows:
            if r["id"] in out:
                # 两跳聚合数另存：运营维护的是直连技能，这个仅作参考
                out[r["id"]]["skill_aggregated"] = int(r["c"])

        # 专业「基础技能」= **直连技能**（covers, E4），即运营在技能构成页维护的那批。
        # 原先这一列取的是「经岗位两跳聚合」数，运营改不动它，数字也对不上构成页。
        rows = conn.execute(
            f"""
            SELECT e.src_id AS id, count(DISTINCT ({SKILL_KEY_SQL})) AS c
            FROM kg_edge e
            JOIN kg_node n ON n.id = e.dst_id AND n.type = 'skill_level' AND {_PUB_N}
            WHERE e.rel_type = 'covers' AND {EP_E}
              AND e.src_id = ANY(%s)
            GROUP BY e.src_id
            """,
            (ids,),
        ).fetchall()
        for r in rows:
            if r["id"] in out:
                out[r["id"]]["skill"] = int(r["c"])
    return out


def counts_for_occupations(
    ids: list[str], *, conn: object | None = None, scope: str = "public"
) -> dict[str, dict[str, int]]:
    """occupation: skill(distinct key), major(逆 prepares_for), industry 数。

    `scope="manage"` 放行 draft / disabled 技能（仍挡 archived），**并且认草稿边**。
    列表这个数字要和详情页、技能构成页对得上 —— 三处口径不同就是「同一岗位三个技能数」。

    谓词不在这里另拼，直接用 `skill_aggregate._composition_pred(scope)` ——
    与 `entity_skill_composition`（详情页/构成页读的那个）**同一份**。
    主线原来是 `node_not_archived("n") if manage else _PUB_N`：思路一致，
    但只管节点、不认草稿边，管理台列表会说 7 项而构成页 8 项。
    """
    out = {i: _empty_counts() for i in ids}
    if not ids:
        return out
    from backend.kg.pg_store.skill_aggregate import _composition_pred

    e_pred, s_pred = _composition_pred(scope)
    # 谓词里用的别名是 e / s，这里的技能节点别名历史上叫 n，统一改名以复用同一份谓词
    with use_conn(conn) as conn:
        rows = conn.execute(
            f"""
            SELECT e.src_id AS id, count(DISTINCT ({SKILL_KEY_SQL.replace("n.", "s.")})) AS c
            FROM kg_edge e
            JOIN kg_node s ON s.id = e.dst_id AND s.type = 'skill_level' AND {s_pred}
            WHERE e.rel_type = 'requires' AND {e_pred}
              AND e.src_id = ANY(%s)
            GROUP BY e.src_id
            """,
            (ids,),
        ).fetchall()
        for r in rows:
            if r["id"] in out:
                out[r["id"]]["skill"] = int(r["c"])

        # 权重和：原型「岗位模型」列表有此列，用于核对国标权重是否配满 1.00
        rows = conn.execute(
            f"""
            SELECT e.src_id AS id, COALESCE(sum(e.weight), 0) AS w
            FROM kg_edge e
            JOIN kg_node s ON s.id = e.dst_id AND s.type = 'skill_level' AND {s_pred}
            WHERE e.rel_type = 'requires' AND {e_pred}
              AND e.src_id = ANY(%s)
            GROUP BY e.src_id
            """,
            (ids,),
        ).fetchall()
        for r in rows:
            if r["id"] in out:
                out[r["id"]]["weight_sum"] = round(float(r["w"] or 0), 2)

        rows = conn.execute(
            f"""
            SELECT e.dst_id AS id, count(DISTINCT e.src_id) AS c
            FROM kg_edge e
            JOIN kg_node m ON m.id = e.src_id AND m.type = 'major' AND {_PUB_M}
            WHERE e.rel_type = 'prepares_for' AND {EP_E}
              AND e.dst_id = ANY(%s)
              AND {_PUB_E}
            GROUP BY e.dst_id
            """,
            (ids,),
        ).fetchall()
        for r in rows:
            if r["id"] in out:
                out[r["id"]]["major"] = int(r["c"])

        # 行业数：直连 + 经专业两跳（去重）
        rows = conn.execute(
            f"""
            SELECT occ_id AS id, count(DISTINCT industry_id) AS c FROM (
              SELECT e.src_id AS occ_id, e.dst_id AS industry_id
              FROM kg_edge e
              JOIN kg_node i ON i.id = e.dst_id AND i.type = 'industry' AND {_PD_I}
                AND COALESCE(i.status, 'published') NOT IN ('archived', 'disabled')
              WHERE e.rel_type = 'belongs_to' AND {EP_E}
                AND e.src_id = ANY(%s)
                AND COALESCE(e.status, 'published') NOT IN ('archived')
              UNION
              SELECT pf.dst_id AS occ_id, e.dst_id AS industry_id
              FROM kg_edge pf
              JOIN kg_node m ON m.id = pf.src_id AND m.type = 'major' AND {_PD_M}
                AND COALESCE(m.status, 'published') NOT IN ('archived', 'disabled')
              JOIN kg_edge e ON e.src_id = m.id AND e.rel_type = 'belongs_to' AND {EP_E}
                AND COALESCE(e.status, 'published') NOT IN ('archived')
              JOIN kg_node i ON i.id = e.dst_id AND i.type = 'industry' AND {_PD_I}
                AND COALESCE(i.status, 'published') NOT IN ('archived', 'disabled')
              WHERE pf.rel_type = 'prepares_for' AND {EP_PF}
                AND pf.dst_id = ANY(%s)
                AND COALESCE(pf.status, 'published') NOT IN ('archived')
            ) t
            GROUP BY occ_id
            """,
            (ids, ids),
        ).fetchall()
        for r in rows:
            if r["id"] in out:
                out[r["id"]]["industry"] = int(r["c"])
    return out


def occupation_in_industry_sql(alias: str = "kg_node") -> str:
    """「这个岗位属于某个行业吗」的 SQL 片段，供列表接口按行业筛选。

    返回的片段带 **2 个 `%s` 占位**（同一个 industry_id 传两次），交给
    `query.list_nodes(extra_where=..., extra_params=[iid, iid])`。
    industry_id 走占位符，绝不拼进片段 —— 它来自用户输入。

    两条路径必须与 `industries_for_occupations` **完全一致**，否则会出现
    「列表里这个岗位显示所属行业 X，但按 X 筛却筛不到它」这种最难解释的不一致：
      1. 直连  occupation -belongs_to→ industry
      2. 两跳  occupation ←prepares_for- major -belongs_to→ industry
    所以两个函数放在同一个文件里紧挨着，改一个必须回头看另一个。

    口径用**前台可见**（published）：这个片段目前只服务学员端列表。管理台要用的话
    得另给一版 published_only=False 的谓词，别直接复用。
    """
    a = alias
    return f"""
    EXISTS (
      SELECT 1 FROM kg_edge e
      JOIN kg_node i ON i.id = e.dst_id AND i.type = 'industry'
           AND {_PUB_I} AND {_PD_I}
      WHERE e.src_id = {a}.id AND e.rel_type = 'belongs_to'
        AND {EP_E} AND {_PUB_E} AND i.id = %s
    )
    OR EXISTS (
      SELECT 1 FROM kg_edge pf
      JOIN kg_node m ON m.id = pf.src_id AND m.type = 'major'
           AND {_PUB_M} AND {_PD_M}
      JOIN kg_edge e2 ON e2.src_id = m.id AND e2.rel_type = 'belongs_to'
           AND {edge_published("e2")}
           AND COALESCE(e2.status, 'published') = 'published'
      JOIN kg_node i ON i.id = e2.dst_id AND i.type = 'industry'
           AND {_PUB_I} AND {_PD_I}
      WHERE pf.dst_id = {a}.id AND pf.rel_type = 'prepares_for'
        AND {EP_PF} AND COALESCE(pf.status, 'published') = 'published'
        AND i.id = %s
    )
    """


def industries_for_occupations(
    ids: list[str],
    *,
    published_only: bool = False,
    conn: object | None = None,
) -> dict[str, list[dict[str, str | None]]]:
    """
    occupation → industries[]（稳定序：name, id）。

    来源：
    1) 直连 occupation -belongs_to→ industry
    2) 经专业 occupation ←prepares_for- major -belongs_to→ industry

    管理列表默认 published_only=False，避免岗位/专业为 draft 时看不到所属行业名。
    """
    out: dict[str, list[dict[str, str | None]]] = {i: [] for i in ids}
    if not ids:
        return out
    if published_only:
        i_ok = _PUB_I
        m_ok = _PUB_M
        e_ok = _PUB_E
    else:
        i_ok = "COALESCE(i.status, 'published') NOT IN ('archived', 'disabled')"
        m_ok = "COALESCE(m.status, 'published') NOT IN ('archived', 'disabled')"
        e_ok = "COALESCE(e.status, 'published') NOT IN ('archived')"

    with use_conn(conn) as conn:
        # 直连
        rows = conn.execute(
            f"""
            SELECT e.src_id AS occ_id, i.id, i.name
            FROM kg_edge e
            JOIN kg_node i ON i.id = e.dst_id AND i.type = 'industry' AND {i_ok}
                 AND {_PD_I}
            WHERE e.rel_type = 'belongs_to' AND {EP_E}
              AND e.src_id = ANY(%s)
              AND {e_ok}
            ORDER BY e.src_id, i.name, i.id
            """,
            (ids,),
        ).fetchall()
        # 经专业两跳
        rows2 = conn.execute(
            f"""
            SELECT pf.dst_id AS occ_id, i.id, i.name
            FROM kg_edge pf
            JOIN kg_node m ON m.id = pf.src_id AND m.type = 'major' AND {m_ok}
                 AND {_PD_M}
            JOIN kg_edge e ON e.src_id = m.id AND e.rel_type = 'belongs_to' AND {EP_E} AND {e_ok}
            JOIN kg_node i ON i.id = e.dst_id AND i.type = 'industry' AND {i_ok}
                 AND {_PD_I}
            WHERE pf.rel_type = 'prepares_for' AND {EP_PF}
              AND pf.dst_id = ANY(%s)
              AND COALESCE(pf.status, 'published') NOT IN ('archived')
            ORDER BY pf.dst_id, i.name, i.id
            """,
            (ids,),
        ).fetchall()

    seen: dict[str, set[str]] = {i: set() for i in ids}
    for r in list(rows) + list(rows2):
        oid = r["occ_id"]
        if oid not in out:
            continue
        iid = r["id"]
        if iid in seen[oid]:
            continue
        seen[oid].add(iid)
        out[oid].append({"id": iid, "name": r["name"]})
    # 按 name 稳定排序
    for oid in out:
        out[oid].sort(key=lambda x: ((x.get("name") or ""), x.get("id") or ""))
    return out


def attach_counts_by_type(
    nodes: list[dict[str, Any]],
    *,
    node_type: str | None = None,
    conn: object | None = None,
    scope: str = "public",
) -> list[dict[str, Any]]:
    """就地/拷贝附加 counts（及岗位 industries）。

    `scope=manage` 时技能数/权重和按草稿视图算，与构成页、详情页同源。
    """
    if not nodes:
        return nodes
    ntype = node_type or nodes[0].get("type")
    ids = [n["id"] for n in nodes if n.get("id")]
    cmap: dict[str, dict[str, int]] = {}
    ind_map: dict[str, list[dict[str, str | None]]] = {}
    if ntype == "industry":
        cmap = counts_for_industries(ids, conn=conn)
    elif ntype == "major":
        cmap = counts_for_majors(ids, conn=conn)
    elif ntype == "occupation":
        cmap = counts_for_occupations(ids, conn=conn, scope=scope)
        ind_map = industries_for_occupations(ids, conn=conn)
    else:
        for n in nodes:
            n.setdefault("counts", _empty_counts())
        return nodes

    for n in nodes:
        nid = n.get("id")
        n["counts"] = cmap.get(nid, _empty_counts()) if nid else _empty_counts()
        if ntype == "occupation" and nid:
            inds = ind_map.get(nid) or []
            n["industries"] = inds
            if inds:
                n["industry_id"] = inds[0].get("id")
                n["industry_name"] = inds[0].get("name")
            else:
                n["industry_id"] = None
                n["industry_name"] = None
    return nodes


def counts_for_node(node_id: str, node_type: str | None = None) -> dict[str, Any]:
    """单节点 counts（+ 岗位 industries）。"""
    from backend.kg.pg_store.query import get_node

    n = get_node(node_id)
    if not n:
        return {"counts": _empty_counts()}
    t = node_type or n.get("type")
    attach_counts_by_type([n], node_type=t)
    return {
        "counts": n.get("counts") or _empty_counts(),
        "industries": n.get("industries"),
        "industry_id": n.get("industry_id"),
        "industry_name": n.get("industry_name"),
    }
