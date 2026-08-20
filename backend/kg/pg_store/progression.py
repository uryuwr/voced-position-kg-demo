"""岗位晋升链路：从一个岗位出发，沿 advances_to 展开出**多条**完整路径。

为什么单独一个模块
------------------
`goal_overview._next_level` 只取一跳、且 `LIMIT 1` —— 那是「学习目标」卡片的
形态（当前目标 → 下一级）。岗位详情页要的是另一回事：把这个岗位所有向上方向
摊开给人看，每条还要能一路走到头。两者数据同源但形状不同，塞进一个函数会让
两边的裁剪逻辑互相打架，所以分开。

1:N 而不是 1:1
--------------
`schemas/graph_schema.yaml` 早期把 `advances_to` 定义成「1:1 有向」，读路径据此
只取一条，于是 Java 明明有「全栈 / 技术经理 / 架构师」三条向上路径，页面上只显示
一条。现实里一个技术岗至少有三个方向：本方向纵深、转管理、跨方向转型。
2026-08-18 改为 1:N。

环与爆炸的防护
--------------
`advances_to` 由 LLM 推断，不保证无环（A→B→A 出现过职级判定不一致的情况）。
展开时按 path 内已访问集合剪枝；深度与总路径数都设上限，否则一个枢纽岗位
（如「技术经理」入边多、出边也多）能展开出上百条路径把响应撑爆。
"""
from __future__ import annotations

import json
from typing import Any

from backend.kg.pg_store.client import connect
from backend.kg.pg_store.config import attrs_level_int, edge_published, node_published
from backend.kg.pg_store.occupation_level_meta import level_code as occ_level_code
from backend.kg.pg_store.occupation_level_meta import level_name as occ_level_name
from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL, SKILL_NAME_SQL
from backend.kg.pg_store.skill_level_meta import label_map
from backend.kg.pg_store.skill_taxonomy import name_of

_EP = edge_published("e")
_ND = node_published("d")
_LEVEL_D = attrs_level_int("d")

# 展开上限。深度 4 已能覆盖「工程师→经理→总监→CTO」这种最长的现实链条；
# 再深多半是数据里的噪音环。
MAX_DEPTH = 4
MAX_PATHS = 12


def _occ_brief(row: dict[str, Any]) -> dict[str, Any]:
    """岗位摘要。level_code / level_name 一律派生，**不入库** ——
    双写会不一致，前端自己拼 'L'+level 又会在 level 为空时显示 Lundefined。"""
    lv = row.get("level")
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "level": lv,
        "level_code": occ_level_code(lv),
        "level_name": occ_level_name(lv),
        "description": row.get("description"),
    }


def _out_edges(conn, occ_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            f"""
            SELECT d.id, d.name, d.description,
                   COALESCE(d.level, {_LEVEL_D}) AS level,
                   e.confidence, e.evidence, e.attrs AS edge_attrs
            FROM kg_edge e
            JOIN kg_node d ON d.id = e.dst_id AND d.type = 'occupation' AND {_ND}
            WHERE e.src_id = %s AND e.rel_type = 'advances_to' AND {_EP}
            ORDER BY COALESCE(d.level, {_LEVEL_D}) NULLS LAST, d.name
            """,
            (occ_id,),
        ).fetchall()
    ]


def _skill_map(conn, occ_id: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT ({SKILL_KEY_SQL}) AS skill_key,
               ({SKILL_NAME_SQL}) AS skill_name, n.category,
               {attrs_level_int('n')} AS required_level, e.weight
        FROM kg_edge e
        JOIN kg_node n ON n.id = e.dst_id AND n.type = 'skill_level'
                      AND {node_published('n')}
        WHERE e.src_id = %s AND e.rel_type = 'requires' AND {_EP}
        """,
        (occ_id,),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = r["skill_key"]
        prev = out.get(key)
        # 同一 skill_key 只保留要求最高的那档。写路径与读路径都做过去重，
        # 这里再兜一次：直连改库绕得过应用层。
        if prev is None or (r["required_level"] or 0) > (prev["required_level"] or 0):
            out[key] = dict(r)
    return out


def _gap(cur: dict[str, dict], nxt: dict[str, dict], labels: dict, limit: int = 6) -> list[dict]:
    """进阶要补的技能 = 目标岗位要求里，当前岗位没有的 / 要求更高的。"""
    gaps = []
    for key, s in sorted(nxt.items(), key=lambda kv: -(float(kv[1].get("weight") or 0))):
        c = cur.get(key)
        if c is None or (s.get("required_level") or 0) > (c.get("required_level") or 0):
            gaps.append({
                "skill_key": key,
                # 展示名必须一起给：key 现在是 SKxxxxxxxxxx，前端拿它渲染就是一串哈希
                "skill_name": s.get("skill_name") or key,
                "category": s.get("category"),
                "category_name": name_of(s.get("category")),
                "required_level": s.get("required_level"),
                "required_label": labels.get(s.get("required_level") or 0),
                "current_required_level": (c or {}).get("required_level"),
                "weight": float(s.get("weight") or 0),
            })
        if len(gaps) >= limit:
            break
    return gaps


def _direction(from_fam: str | None, to_fam: str | None, to_name: str) -> str:
    """给路径起个人看得懂的方向名。岗位族取自 advance 阶段写进边 attrs 的
    from_family/to_family；缺失时退回按目标岗位名判断。"""
    mgmt_kw = ("经理", "总监", "主管", "负责人", "CTO", "CEO", "厂长", "店长", "院长", "校长")
    if any(k in (to_name or "") for k in mgmt_kw):
        return "管理路线"
    # 「架构师」在岗位族里是独立的一族（架构设计），按族比对会判成跨方向转型，
    # 但它其实是本方向走到高处 —— 岗位族的划分粒度表达不了这层，按名字兜一道。
    expert_kw = ("架构", "专家", "科学家", "首席")
    if any(k in (to_name or "") for k in expert_kw):
        return "技术纵深"
    if from_fam and to_fam and from_fam == to_fam:
        return "本方向纵深"
    if from_fam and to_fam:
        return "跨方向转型"
    return "向上发展"


def occupation_progressions(
    occupation_id: str,
    *,
    max_depth: int = MAX_DEPTH,
    max_paths: int = MAX_PATHS,
) -> dict[str, Any]:
    """岗位的全部晋升路径。每条路径是一串岗位，逐跳给出要补的技能。"""
    from backend.kg.pg_store.query import get_node

    occ = get_node(occupation_id, scope="public")
    if not occ or occ.get("type") != "occupation":
        raise ValueError("occupation not found")

    labels = label_map()
    paths: list[dict[str, Any]] = []
    # 起点岗位的 level 兼容两种存法：level 列（历史）与 attrs.level（现真源）
    occ_d = dict(occ)
    if occ_d.get("level") is None:
        a = occ_d.get("attrs")
        if isinstance(a, str):
            try:
                a = json.loads(a or "{}")
            except Exception:
                a = {}
        try:
            occ_d["level"] = int((a or {}).get("level"))
        except (TypeError, ValueError):
            occ_d["level"] = None
    start_brief = _occ_brief(occ_d)

    with connect() as conn:
        skills_cache: dict[str, dict[str, dict]] = {occupation_id: _skill_map(conn, occupation_id)}

        def skills_of(oid: str) -> dict[str, dict]:
            if oid not in skills_cache:
                skills_cache[oid] = _skill_map(conn, oid)
            return skills_cache[oid]

        def walk(cur_id: str, visited: set[str], hops: list[dict], depth: int) -> None:
            if len(paths) >= max_paths:
                return
            nxts = _out_edges(conn, cur_id) if depth < max_depth else []
            # 走到头（无出边 / 到达深度上限）就把这条路径收下
            live = [n for n in nxts if n["id"] not in visited]
            if not live:
                if hops:
                    first, last = hops[0], hops[-1]
                    paths.append({
                        "target": last["to"],
                        "direction": first["direction"],
                        "depth": len(hops),
                        "hops": hops,
                    })
                return
            for n in live:
                if len(paths) >= max_paths:
                    return
                attrs = n.get("edge_attrs")
                if isinstance(attrs, str):
                    try:
                        attrs = json.loads(attrs or "{}")
                    except Exception:
                        attrs = {}
                attrs = attrs or {}
                hop = {
                    # 第一跳的起点是查询岗位本身，之后每跳接着上一跳的终点
                    "from": hops[-1]["to"] if hops else start_brief,
                    "to": _occ_brief(n),
                    "direction": _direction(attrs.get("from_family"), attrs.get("to_family"), n["name"]),
                    "confidence": n.get("confidence"),
                    "evidence": n.get("evidence"),
                    "unlock_skills": _gap(skills_of(cur_id), skills_of(n["id"]), labels),
                }
                walk(n["id"], visited | {n["id"]}, hops + [hop], depth + 1)

        walk(occupation_id, {occupation_id}, [], 0)

    # 长链在前：同一起点下「工程师→经理→总监」比「工程师→全栈」信息量大
    paths.sort(key=lambda p: (-p["depth"], p["target"].get("level") or 0))
    return {
        "occupation": start_brief,
        "paths": paths,
        "path_count": len(paths),
        "truncated": len(paths) >= max_paths,
        "note": (
            "一个岗位可以有多条向上路径（本方向纵深 / 管理路线 / 跨方向转型），"
            "每条路径逐跳给出要补的技能。晋升边由 LLM 推断（confidence=ai_inferred），"
            "evidence 里是判定依据。"
        ),
    }
