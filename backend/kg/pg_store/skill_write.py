"""
逻辑技能写入：一次录入 levels.L1..L5 对象 → 拆成多条 skill_level + requires 边。

权重语义：技能在该岗位技能构成中的占比 → 写在 occupation -requires→ skill 边 weight 上。
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from backend.kg.pg_store.client import connect
from backend.kg.pg_store.query import get_node
from backend.kg.pg_store.skill_aggregate import (
    SKILL_KEY_SQL,
    get_skill_bundle,
    skill_key_from_node,
)
from backend.kg.pg_store.skill_taxonomy import to_code
from backend.kg.pg_store.write import create_edge, create_node, patch_node

from backend.kg.pg_store.skill_level_meta import (
    REQUIRED_LEVEL_CODES,
    label_map,
)

_LEVEL_KEYS = REQUIRED_LEVEL_CODES


def _level_labels() -> dict[int, str]:
    return label_map()


def is_skill_bundle_payload(payload: dict[str, Any] | None) -> bool:
    if not payload or not isinstance(payload, dict):
        return False
    kind = str(payload.get("kind") or payload.get("entity_subkind") or "").lower()
    if kind in ("skill_bundle", "skill", "bundle"):
        return True
    if payload.get("expand_levels") or payload.get("levels"):
        # 带 levels 的 skill 创建/更新
        return True
    return False


def _norm_level_code(key: Any) -> str | None:
    s = str(key or "").strip().upper()
    if not s:
        return None
    m = re.match(r"^L?([1-5])$", s)
    if m:
        return f"L{m.group(1)}"
    return None


def normalize_level_obj(raw: Any) -> dict[str, Any]:
    """string → {description}; dict 原样保留（至少保证 description 可取）。"""
    if raw is None:
        return {}
    if isinstance(raw, str):
        return {"description": raw.strip()} if raw.strip() else {}
    if isinstance(raw, dict):
        out = dict(raw)
        if "description" not in out and "desc" in out:
            out["description"] = out.get("desc")
        return out
    return {"description": str(raw)}


def normalize_levels(levels: Any) -> dict[str, dict[str, Any]]:
    """→ {L1: {...}, ...} 仅含有内容的档。"""
    if not levels:
        return {}
    if not isinstance(levels, dict):
        raise ValueError("levels 须为对象，键 L1–L5")
    out: dict[str, dict[str, Any]] = {}
    for k, v in levels.items():
        code = _norm_level_code(k)
        if not code:
            continue
        if v is None or (isinstance(v, str) and not str(v).strip()):
            continue
        obj = normalize_level_obj(v)
        if not obj:
            continue
        if not any(str(x).strip() for x in obj.values() if x is not None):
            continue
        li = int(code[1])
        obj.setdefault("label", _level_labels().get(li, code))
        out[code] = obj
    return out


def normalize_occupation_links(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """
    occupation_links: [{occupation_id, weight?, required_level?}]
    兼容 occupation_ids: string[]
    weight: 0–1 或 0–100（>1 则 /100）
    """
    links = payload.get("occupation_links") or payload.get("links") or []
    out: list[dict[str, Any]] = []
    if isinstance(links, list) and links:
        for item in links:
            if isinstance(item, str):
                out.append({"occupation_id": item.strip(), "weight": None, "required_level": None})
                continue
            if not isinstance(item, dict):
                continue
            oid = (
                item.get("occupation_id")
                or item.get("id")
                or item.get("position_id")
                or ""
            ).strip()
            if not oid:
                continue
            w = item.get("weight")
            if w is not None:
                try:
                    w = float(w)
                    if w > 1.0:
                        w = w / 100.0
                    w = max(0.0, min(1.0, w))
                except (TypeError, ValueError):
                    w = None
            rl = item.get("required_level")
            if rl is not None:
                try:
                    rl = int(str(rl).upper().replace("L", ""))
                    if rl < 1 or rl > 5:
                        rl = None
                except (TypeError, ValueError):
                    rl = None
            out.append(
                {
                    "occupation_id": oid,
                    "weight": w,
                    "required_level": rl,
                }
            )
    # 兼容扁平 occupation_ids
    ids = payload.get("occupation_ids") or []
    if isinstance(ids, str):
        ids = [ids]
    existing = {x["occupation_id"] for x in out}
    for oid in ids or []:
        s = str(oid).strip()
        if s and s not in existing:
            out.append({"occupation_id": s, "weight": None, "required_level": None})
            existing.add(s)
    return out


def resolve_skill_key(payload: dict[str, Any]) -> str:
    key = (
        payload.get("skill_key")
        or payload.get("skill_name")
        or payload.get("name")
        or ""
    )
    key = str(key).strip()
    # 去掉可能的 · Lx 后缀
    key = re.sub(r"\s*·\s*L[1-5]\s*$", "", key, flags=re.I).strip()
    if not key:
        raise ValueError("需要 skill_key 或 name")
    return key


def build_level_node_body(
    *,
    skill_key: str,
    level_code: str,
    level_obj: dict[str, Any],
    region: str,
    base: dict[str, Any],
    status: str = "published",
) -> dict[str, Any]:
    li = int(level_code[1])
    label = level_obj.get("label") or _level_labels().get(li, level_code)
    desc = level_obj.get("description") or level_obj.get("desc")
    name = f"{skill_key} · {level_code}"
    # 稳定 id，便于重复提交 upsert
    sid = f"{skill_key}|{level_code}"
    nid = base.get("id_prefix") or f"CN:skill_level:MANUAL:{uuid.uuid5(uuid.NAMESPACE_URL, sid).hex[:16]}"
    # 若 payload 指定 id 模板
    if base.get("node_ids") and isinstance(base["node_ids"], dict):
        nid = base["node_ids"].get(level_code) or nid

    attrs = dict(base.get("attrs") or {})
    attrs.update(
        {
            "skill_key": skill_key,
            "skill_name": skill_key,
            # scale 记录数据源使用的原刻度，仅溯源；判定一律用 level
            "scale": base.get("scale") or "l1_l5",
            "level": li,
            "level_label": label,
            "level_payload": level_obj,
        }
    )
    # 冗余 level_descriptions 整包便于单节点读
    ld = base.get("_all_level_descriptions") or {}
    if ld:
        attrs["level_descriptions"] = ld

    # 技能大类落 code。列与 attrs 都写：列供读路径与索引用，
    # attrs 供采集库/灌库链路用（SQLite 的 nodes 表没有 category 列）。
    cat = to_code(base.get("category"))
    attrs["category"] = cat

    return {
        "id": nid,
        "type": "skill_level",
        "region": region,
        "name": name,
        "name_zh": name,
        "description": desc,
        "category": cat,
        "attrs": attrs,
        "source_system": base.get("source_system") or "MANUAL",
        "source_url": base.get("source_url") or "manual://admin-skill-bundle",
        "confidence": base.get("confidence") or "manual_seed",
        "status": status,
        # 不在 create_node 里建默认边
        "occupation_ids": [],
    }


def _find_existing_nodes_by_skill_key(skill_key: str, region: str = "CN") -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT n.*
            FROM kg_node n
            WHERE n.type = 'skill_level'
              AND (%s::text IS NULL OR n.region = %s)
              AND ({SKILL_KEY_SQL}) = %s
            """,
            (region, region, skill_key),
        ).fetchall()
    from backend.kg.pg_store.query import _node_dict

    return [_node_dict(r) for r in rows]


def _delete_requires_into_nodes(node_ids: list[str]) -> None:
    if not node_ids:
        return
    with connect() as conn:
        conn.execute(
            """
            DELETE FROM kg_edge
            WHERE rel_type = 'requires' AND dst_id = ANY(%s)
            """,
            (node_ids,),
        )
        conn.commit()


def apply_skill_bundle_create(
    payload: dict[str, Any],
    *,
    user_id: str,
    user_name: str,
) -> dict[str, Any]:
    """审核通过：拆 levels 入库 + 建 requires 边（权重在边上）。"""
    skill_key = resolve_skill_key(payload)
    levels = normalize_levels(payload.get("levels"))
    if not levels:
        raise ValueError("levels 至少包含一档（L1–L5 对象或字符串）")
    region = (payload.get("region") or "CN").strip() or "CN"
    status = payload.get("status") or "published"
    occ_links = normalize_occupation_links(payload)

    # 校验岗位存在
    for link in occ_links:
        n = get_node(link["occupation_id"])
        if not n:
            raise ValueError(f"关联岗位不存在: {link['occupation_id']}")
        if n.get("type") != "occupation":
            raise ValueError(f"关联目标须为 occupation: {link['occupation_id']}")

    all_ld = {
        code: (obj.get("description") or "")
        for code, obj in levels.items()
        if obj.get("description")
    }
    base = {
        "scale": payload.get("scale") or "l1_l5",
        "source_system": payload.get("source_system") or "MANUAL",
        "source_url": payload.get("source_url") or "manual://admin-skill-bundle",
        "confidence": payload.get("confidence") or "manual_seed",
        "attrs": payload.get("attrs") if isinstance(payload.get("attrs"), dict) else {},
        "category": payload.get("category"),
        "_all_level_descriptions": all_ld,
        "node_ids": payload.get("node_ids"),
    }

    created_nodes: list[dict[str, Any]] = []
    nodes_by_level: dict[str, dict[str, Any]] = {}

    # 若已有同 skill_key 节点，复用 id 做 upsert
    existing = _find_existing_nodes_by_skill_key(skill_key, region)
    exist_by_lv: dict[str, str] = {}
    for n in existing:
        a = n.get("attrs") if isinstance(n.get("attrs"), dict) else {}
        code = (a.get("level_code") or "").upper()
        if not code:
            m = re.search(r"L([1-5])", n.get("name") or "", re.I)
            code = f"L{m.group(1)}" if m else ""
        if code:
            exist_by_lv[code] = n["id"]

    for code in _LEVEL_KEYS:
        if code not in levels:
            continue
        body = build_level_node_body(
            skill_key=skill_key,
            level_code=code,
            level_obj=levels[code],
            region=region,
            base={**base, "node_ids": {code: exist_by_lv[code]} if code in exist_by_lv else base.get("node_ids")},
            status=status,
        )
        if code in exist_by_lv:
            body["id"] = exist_by_lv[code]
        node = create_node(body, user_id=user_id, user_name=user_name)
        created_nodes.append(node)
        nodes_by_level[code] = node

    # 边：每个岗位 × 每个已建档；权重仅写在 required_level（或最高档）
    created_edges: list[dict[str, Any]] = []
    node_ids = [n["id"] for n in created_nodes]
    _delete_requires_into_nodes(node_ids)

    max_lv = max((int(c[1]) for c in nodes_by_level), default=1)
    for link in occ_links:
        oid = link["occupation_id"]
        req = link.get("required_level") or max_lv
        req_code = f"L{req}"
        if req_code not in nodes_by_level:
            # 落在已有最高档
            req_code = f"L{max_lv}"
        for code, node in nodes_by_level.items():
            w = None
            if code == req_code:
                w = link.get("weight")
                if w is None:
                    w = 0.8  # 仅要求档默认权重
            edge = create_edge(
                {
                    "src_id": oid,
                    "dst_id": node["id"],
                    "rel_type": "requires",
                    "region": region,
                    "weight": w,
                    "status": "published",
                    "confidence": "manual_seed",
                    "source_system": "MANUAL",
                    "source_url": "manual://admin-skill-bundle",
                    "evidence": f"技能构成 {skill_key}@{code}"
                    + (f" weight={w}" if w is not None else ""),
                    "attrs": {
                        "skill_key": skill_key,
                        "level_code": code,
                        "required_level": req,
                        "is_required_level": code == req_code,
                    },
                },
                user_id=user_id,
                user_name=user_name,
            )
            created_edges.append(edge)

    bundle = get_skill_bundle(skill_key, region=region)
    return {
        "skill_key": skill_key,
        "nodes": created_nodes,
        "node_ids": node_ids,
        "edges": created_edges,
        "levels": list(nodes_by_level.keys()),
        "occupation_links": occ_links,
        "bundle": bundle,
    }


def apply_skill_bundle_update(
    skill_key: str,
    payload: dict[str, Any],
    *,
    user_id: str,
    user_name: str,
) -> dict[str, Any]:
    """更新逻辑技能：可增补/改档文案、重建岗链权重。"""
    skill_key = (skill_key or resolve_skill_key(payload)).strip()
    region = (payload.get("region") or "CN").strip() or "CN"
    payload = {**payload, "skill_key": skill_key, "name": payload.get("name") or skill_key}
    # 若未带 levels，保留现有档的 description，只改边
    if not payload.get("levels"):
        existing = _find_existing_nodes_by_skill_key(skill_key, region)
        if not existing:
            raise ValueError(f"逻辑技能不存在: {skill_key}")
        levels: dict[str, Any] = {}
        for n in existing:
            a = n.get("attrs") if isinstance(n.get("attrs"), dict) else {}
            code = (a.get("level_code") or "").upper()
            if not code:
                continue
            levels[code] = a.get("level_payload") or {
                "label": a.get("level_label"),
                "description": n.get("description"),
            }
        payload["levels"] = levels
    return apply_skill_bundle_create(payload, user_id=user_id, user_name=user_name)


def preview_skill_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    """提交前预览将生成的档位与边（不写库）。"""
    skill_key = resolve_skill_key(payload)
    levels = normalize_levels(payload.get("levels"))
    occ_links = normalize_occupation_links(payload)
    return {
        "skill_key": skill_key,
        "level_codes": list(levels.keys()),
        "level_count": len(levels),
        "occupation_count": len(occ_links),
        "occupation_links": occ_links,
        "will_create_nodes": len(levels),
        "will_create_edges": len(levels) * len(occ_links),
    }


def prepare_submit_payload(body: dict[str, Any]) -> dict[str, Any]:
    """规范化进审核队列的 payload。"""
    skill_key = resolve_skill_key(body)
    levels = normalize_levels(body.get("levels"))
    if not levels:
        raise ValueError("levels 至少包含一档（L1–L5）")
    occ_links = normalize_occupation_links(body)
    return {
        "kind": "skill_bundle",
        "type": "skill_level",
        "expand_levels": True,
        "skill_key": skill_key,
        "skill_name": skill_key,
        "name": body.get("name") or skill_key,
        "region": body.get("region") or "CN",
        "scale": body.get("scale") or "l1_l5",
        "levels": levels,
        "occupation_links": occ_links,
        "occupation_ids": [x["occupation_id"] for x in occ_links],
        # 分类一律归一成 code 再进队列。管理台传 code、脚本可能传中文名、
        # 外部调用可能什么都不传 —— 三条路都收敛到同一套 code，认不出的落兜底，
        # 不猜也不硬塞（`to_code` 是唯一的映射处）。
        "category": to_code(body.get("category")),
        "source_system": body.get("source_system") or "MANUAL",
        "source_url": body.get("source_url"),
        "confidence": body.get("confidence") or "manual_seed",
        "attrs": body.get("attrs") if isinstance(body.get("attrs"), dict) else {},
        "description": body.get("description"),
    }


def archive_skill_bundle(
    skill_key: str,
    *,
    region: str = "CN",
    user_id: str = "",
    user_name: str = "",
) -> dict[str, Any]:
    """删除逻辑技能：把该 skill_key 的**全部档位节点与关联边**标为 archived。

    软删而非物理删除，理由与项目其它删除一致：`archived` 是逻辑删除，任何读接口
    都不返回，但记录还在、可恢复。物理删会带走岗位的 requires 边，误删就找不回来了。

    边必须一起归档 —— 节点 archived 而边还 published 的话，`edge_published()`
    过滤不掉那些边，图查询会画出指向不可见节点的断头箭头；管理台按边计数也会
    比详情页多出来（3D设计师就是这么出现「列表 1 项、详情 0 项」的）。

    返回被引用情况，供前端提示「该技能还挂在 N 个岗位上」——**不阻止删除**：
    要不要删是运营的判断，接口只负责把后果说清楚。
    """
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT n.id FROM kg_node n
            WHERE n.type = 'skill_level'
              AND (%s::text IS NULL OR n.region = %s)
              AND ({SKILL_KEY_SQL}) = %s
              AND COALESCE(n.status, 'published') <> 'archived'
            """,
            (region, region, skill_key),
        ).fetchall()
        node_ids = [r["id"] for r in rows]
        if not node_ids:
            raise ValueError(f"技能不存在或已删除: {skill_key}")

        occ_cnt = int(
            conn.execute(
                """
                SELECT count(DISTINCT e.src_id) AS c FROM kg_edge e
                WHERE e.rel_type = 'requires' AND e.dst_id = ANY(%s)
                  AND COALESCE(e.status, 'published') = 'published'
                """,
                (node_ids,),
            ).fetchone()["c"]
            or 0
        )
        edge_n = conn.execute(
            """
            UPDATE kg_edge SET status = 'archived'
            WHERE (src_id = ANY(%s) OR dst_id = ANY(%s))
              AND COALESCE(status, 'published') <> 'archived'
            """,
            (node_ids, node_ids),
        ).rowcount
        node_n = conn.execute(
            """
            UPDATE kg_node
               SET status = 'archived', updated_by = %s, updated_by_name = %s
             WHERE id = ANY(%s)
            """,
            (user_id or None, user_name or None, node_ids),
        ).rowcount
        conn.commit()

    return {
        "deleted": True,
        "skill_key": skill_key,
        "archived_nodes": int(node_n or 0),
        "archived_edges": int(edge_n or 0),
        "occupations_affected": occ_cnt,
    }
