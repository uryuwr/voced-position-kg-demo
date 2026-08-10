"""KG write path on PostgreSQL (manual create / archive)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from backend.kg.pg_store.client import connect, ensure_schema
from backend.kg.pg_store.query import _node_dict, _rel_dict, get_node


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_or_none(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


def _strip_link_fields(data: dict[str, Any]) -> dict[str, Any]:
    """节点本体字段：去掉关联 id 列表（不入库 attrs）。"""
    skip = {
        "industry_ids",
        "major_ids",
        "occupation_ids",
        "links",
        "link",
    }
    return {k: v for k, v in data.items() if k not in skip}


def _normalize_id_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    out = []
    for x in v:
        s = str(x).strip()
        if s and s not in out:
            out.append(s)
    return out


def extract_link_ids(data: dict[str, Any]) -> dict[str, list[str]]:
    """
    客户端简化关联（可多选）：
      industry_ids  — 专业→行业
      major_ids     — 岗位←专业（prepares_for 方向：专业→岗位）
      occupation_ids— 技能←岗位（requires 方向：岗位→技能）
    也可放在 payload.links / payload.link 下。
    """
    links = data.get("links") or data.get("link") or {}
    if not isinstance(links, dict):
        links = {}
    return {
        "industry_ids": _normalize_id_list(
            data.get("industry_ids") or links.get("industry_ids")
        ),
        "major_ids": _normalize_id_list(data.get("major_ids") or links.get("major_ids")),
        "occupation_ids": _normalize_id_list(
            data.get("occupation_ids") or links.get("occupation_ids")
        ),
    }


def apply_node_links(
    node_id: str,
    node_type: str,
    link_ids: dict[str, list[str]],
    *,
    user_id: str,
    user_name: str,
    replace: bool = True,
) -> list[dict[str, Any]]:
    """
    按节点类型自动建边（系统填默认字段）。
    replace=True 时先删本节点上对应 rel 的旧边再重建（编辑关联时用）。
    """
    ensure_schema()
    created: list[dict[str, Any]] = []
    ntype = (node_type or "").lower()
    region = "CN"
    base = {
        "region": region,
        "weight": 0.8,
        "confidence": "manual_seed",
        "status": "published",
        "source_system": "MANUAL",
        "source_url": "manual://admin-link",
        "evidence": "管理端关联选择自动生成",
    }

    def _ensure_exists(nid: str, expect_type: str | None = None) -> None:
        row = get_node(nid)
        if not row:
            raise ValueError(f"关联节点不存在: {nid}")
        if expect_type and row.get("type") != expect_type:
            raise ValueError(
                f"关联节点类型应为 {expect_type}，实际 {row.get('type')}: {nid}"
            )

    # 定义：本节点类型 → (对方类型, rel, 方向 from_self_as_src)
    plans: list[tuple[str, str, list[str], bool]] = []
    if ntype == "major":
        # major -belongs_to→ industry
        plans.append(("industry", "belongs_to", link_ids.get("industry_ids") or [], True))
    elif ntype == "occupation":
        # major -prepares_for→ occupation  （对方是 src）
        plans.append(("major", "prepares_for", link_ids.get("major_ids") or [], False))
    elif ntype == "skill_level":
        # occupation -requires→ skill
        plans.append(
            ("occupation", "requires", link_ids.get("occupation_ids") or [], False)
        )
    elif ntype == "industry":
        # 行业暂不强制反向挂边
        plans = []

    for peer_type, rel, ids, self_is_src in plans:
        if replace:
            with connect() as conn:
                if self_is_src:
                    conn.execute(
                        "DELETE FROM kg_edge WHERE src_id=%s AND rel_type=%s",
                        (node_id, rel),
                    )
                else:
                    conn.execute(
                        "DELETE FROM kg_edge WHERE dst_id=%s AND rel_type=%s",
                        (node_id, rel),
                    )
                conn.commit()
        for peer in ids:
            _ensure_exists(peer, peer_type)
            if self_is_src:
                src, dst = node_id, peer
            else:
                src, dst = peer, node_id
            edge = create_edge(
                {
                    **base,
                    "src_id": src,
                    "dst_id": dst,
                    "rel_type": rel,
                },
                user_id=user_id,
                user_name=user_name,
            )
            created.append(edge)
    return created


def create_node(
    data: dict[str, Any],
    *,
    user_id: str,
    user_name: str,
) -> dict[str, Any]:
    ensure_schema()
    link_ids = extract_link_ids(data)
    body = _strip_link_fields(data)
    nid = (body.get("id") or "").strip() or f"CN:manual:{body['type']}:{uuid.uuid4().hex[:12]}"
    status = body.get("status") or "draft"
    # 直写 published 须过 BR 门禁；先落 draft 再在调用方升权，或此处拦截
    if str(status).lower() == "published":
        ntype = (body.get("type") or "").lower()
        if ntype in ("major", "occupation", "skill_level", "skill", "skill_bundle"):
            # 无边时门禁必败；先以 draft 入库，外层再 promote
            status = "draft"
            body["status"] = "draft"
    row = {
        "id": nid,
        "region": body.get("region") or "CN",
        "type": body["type"],
        "name": body["name"],
        "name_en": body.get("name_en"),
        "name_zh": body.get("name_zh"),
        "aliases": _json_or_none(body.get("aliases")),
        "description": body.get("description"),
        "attrs": _json_or_none(body.get("attrs") or {}),
        "source_system": body.get("source_system") or "MANUAL",
        "source_id": body.get("source_id") or nid,
        "source_url": body.get("source_url") or "manual://admin",
        "license": body.get("license") or "internal",
        "fetched_at": body.get("fetched_at") or _now(),
        "confidence": body.get("confidence") or "manual_seed",
        "status": status,
        "updated_by": user_id,
        "updated_by_name": user_name,
    }
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO kg_node (
              id, region, type, name, name_en, name_zh, aliases, description, attrs,
              source_system, source_id, source_url, license, fetched_at, confidence,
              status, updated_by, updated_by_name
            ) VALUES (
              %(id)s, %(region)s, %(type)s, %(name)s, %(name_en)s, %(name_zh)s, %(aliases)s,
              %(description)s, %(attrs)s, %(source_system)s, %(source_id)s, %(source_url)s,
              %(license)s, %(fetched_at)s, %(confidence)s, %(status)s, %(updated_by)s,
              %(updated_by_name)s
            )
            ON CONFLICT (id) DO UPDATE SET
              name = EXCLUDED.name,
              name_en = EXCLUDED.name_en,
              name_zh = EXCLUDED.name_zh,
              aliases = EXCLUDED.aliases,
              description = EXCLUDED.description,
              attrs = EXCLUDED.attrs,
              source_url = EXCLUDED.source_url,
              confidence = EXCLUDED.confidence,
              status = EXCLUDED.status,
              updated_by = EXCLUDED.updated_by,
              updated_by_name = EXCLUDED.updated_by_name
            """,
            row,
        )
        conn.commit()
    edges = apply_node_links(
        nid,
        body["type"],
        link_ids,
        user_id=user_id,
        user_name=user_name,
        replace=True,
    )
    node = get_node(nid)
    assert node is not None
    node = dict(node)
    node["linked_edges"] = edges
    node["link_ids"] = link_ids
    return node


def patch_node(
    node_id: str,
    data: dict[str, Any],
    *,
    user_id: str,
    user_name: str,
) -> dict[str, Any] | None:
    ensure_schema()
    link_ids = extract_link_ids(data)
    has_links = any(link_ids.values())
    body = _strip_link_fields(data)
    # 直写 status=published → BR-08 门禁
    if str(body.get("status") or "").lower() == "published":
        from backend.kg.pg_store.publish_rules import (
            PublishGateError,
            assert_publish_allowed,
        )
        from backend.kg.pg_store.query import get_node as _get
        from backend.kg.pg_store.skill_aggregate import skill_key_from_node

        cur = _get(node_id)
        if not cur:
            return None
        ntype = cur.get("type")
        sk = None
        if ntype == "skill_level":
            sk = skill_key_from_node(cur)
            ntype = "skill_bundle"
        try:
            assert_publish_allowed(
                node_type=ntype,
                node_id=None if sk else node_id,
                skill_key=sk,
                region=cur.get("region") or "CN",
                action="enable",
            )
        except PublishGateError as e:
            msgs = "; ".join(
                f"{v.get('rule')}: {v.get('message')}" for v in e.violations
            )
            raise ValueError(f"发布门禁未通过 — {msgs}") from e
        if sk:
            from backend.kg.pg_store.publish_rules import _set_skill_key_status

            _set_skill_key_status(sk, "published", region=cur.get("region") or "CN")
            # 当前节点已随 skill_key 批量更新；继续走字段补丁（不含 status 重复）
            body = {k: v for k, v in body.items() if k != "status"}

    fields = []
    params: dict[str, Any] = {
        "id": node_id,
        "updated_by": user_id,
        "updated_by_name": user_name,
    }
    for key in (
        "name",
        "name_en",
        "name_zh",
        "description",
        "source_url",
        "confidence",
        "status",
        "region",
    ):
        if key in body and body[key] is not None:
            fields.append(f"{key} = %({key})s")
            params[key] = body[key]
    if "aliases" in body:
        fields.append("aliases = %(aliases)s")
        params["aliases"] = _json_or_none(body["aliases"])
    if "attrs" in body:
        fields.append("attrs = %(attrs)s")
        params["attrs"] = _json_or_none(body["attrs"])
    if fields:
        fields.append("updated_by = %(updated_by)s")
        fields.append("updated_by_name = %(updated_by_name)s")
        sql = f"UPDATE kg_node SET {', '.join(fields)} WHERE id = %(id)s"
        with connect() as conn:
            cur = conn.execute(sql, params)
            if cur.rowcount == 0:
                return None
            conn.commit()
    elif not has_links:
        return get_node(node_id)
    else:
        # 仅改关联
        if not get_node(node_id):
            return None

    node = get_node(node_id)
    if not node:
        return None
    edges: list[dict[str, Any]] = []
    if has_links:
        edges = apply_node_links(
            node_id,
            node.get("type") or "",
            link_ids,
            user_id=user_id,
            user_name=user_name,
            replace=True,
        )
    out = dict(node)
    if has_links:
        out["linked_edges"] = edges
        out["link_ids"] = link_ids
    return out


def create_edge(
    data: dict[str, Any],
    *,
    user_id: str,
    user_name: str,
) -> dict[str, Any]:
    ensure_schema()
    src, dst, rel = data["src_id"], data["dst_id"], data["rel_type"]
    eid = (data.get("id") or "").strip() or f"edge:{src}|{rel}|{dst}"
    status = data.get("status") or "draft"
    row = {
        "id": eid,
        "src_id": src,
        "dst_id": dst,
        "rel_type": rel,
        "region": data.get("region") or "CN",
        "weight": data.get("weight"),
        "evidence": data.get("evidence"),
        "attrs": _json_or_none(data.get("attrs") or {}),
        "source_system": data.get("source_system") or "MANUAL",
        "source_id": data.get("source_id"),
        "source_url": data.get("source_url") or "manual://admin",
        "license": data.get("license") or "internal",
        "fetched_at": data.get("fetched_at") or _now(),
        "confidence": data.get("confidence") or "manual_seed",
        "status": status,
        "updated_by": user_id,
        "updated_by_name": user_name,
    }
    with connect() as conn:
        # endpoints must exist
        for kid in (src, dst):
            if not conn.execute("SELECT 1 FROM kg_node WHERE id = %s", (kid,)).fetchone():
                raise ValueError(f"node not found: {kid}")
        conn.execute(
            """
            INSERT INTO kg_edge (
              id, src_id, dst_id, rel_type, region, weight, evidence, attrs,
              source_system, source_id, source_url, license, fetched_at, confidence,
              status, updated_by, updated_by_name
            ) VALUES (
              %(id)s, %(src_id)s, %(dst_id)s, %(rel_type)s, %(region)s, %(weight)s,
              %(evidence)s, %(attrs)s, %(source_system)s, %(source_id)s, %(source_url)s,
              %(license)s, %(fetched_at)s, %(confidence)s, %(status)s,
              %(updated_by)s, %(updated_by_name)s
            )
            ON CONFLICT (id) DO UPDATE SET
              weight = EXCLUDED.weight,
              evidence = EXCLUDED.evidence,
              attrs = EXCLUDED.attrs,
              confidence = EXCLUDED.confidence,
              status = EXCLUDED.status,
              updated_by = EXCLUDED.updated_by,
              updated_by_name = EXCLUDED.updated_by_name
            """,
            row,
        )
        conn.commit()
        er = conn.execute("SELECT * FROM kg_edge WHERE id = %s", (eid,)).fetchone()
    assert er is not None
    return _rel_dict(er)


def archive_node(node_id: str, *, user_id: str, user_name: str) -> dict[str, Any] | None:
    return patch_node(
        node_id, {"status": "archived"}, user_id=user_id, user_name=user_name
    )


def archive_edge(edge_id: str, *, user_id: str, user_name: str) -> bool:
    ensure_schema()
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE kg_edge SET status = 'archived',
              updated_by = %s, updated_by_name = %s
            WHERE id = %s
            """,
            (user_id, user_name, edge_id),
        )
        conn.commit()
        return cur.rowcount > 0
