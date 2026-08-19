"""
审核队列（重构）。

- 新建 / 编辑 / 删除 / 停用 / 发布（启用）→ 一律进待审列表
- 通过：立即应用到正式图数据，并删除提案记录
- 驳回：直接删除提案记录
- 正式节点状态：published | draft | disabled
- BR-08：发布/enable 必须过 publish_rules 门禁；不达标不可置 published
"""
from __future__ import annotations

import json
from typing import Any

from backend.kg.pg_store.client import connect, ensure_schema
from backend.kg.pg_store.publish_rules import (
    PublishGateError,
    assert_publish_allowed,
    validate_publish,
)
from backend.kg.pg_store.skill_write import (
    apply_skill_bundle_create,
    apply_skill_bundle_update,
    is_skill_bundle_payload,
    resolve_skill_key,
)
from backend.kg.pg_store.write import create_edge, create_node, patch_node

# 四维
DIM_TYPES = frozenset({"industry", "major", "occupation", "skill_level"})

# 动作
ACTIONS = frozenset(
    {
        "create",
        "update",
        "delete",
        "disable",
        "enable",  # 发布=enable → status published
    }
)


def ensure_review_schema() -> None:
    ensure_schema()
    with connect() as conn:
        # 新表：精简字段
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kg_change_request (
              id BIGSERIAL PRIMARY KEY,
              entity_kind TEXT NOT NULL,
              action TEXT NOT NULL,
              dim_type TEXT,
              target_id TEXT,
              title TEXT,
              payload JSONB NOT NULL DEFAULT '{}'::jsonb,
              created_by TEXT NOT NULL,
              created_by_name TEXT NOT NULL DEFAULT '',
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_cr_created ON kg_change_request(created_at DESC)"
        )
        conn.commit()


def submit_change(
    *,
    entity_kind: str,
    action: str,
    payload: dict[str, Any],
    user_id: str,
    user_name: str,
    dim_type: str | None = None,
    target_id: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """
    提交变更。
    REVIEW_REQUIRED=0（默认）：校验后立即 _apply 写主库，不进待审。
    REVIEW_REQUIRED=1：写入 kg_change_request，待 approve。
    """
    entity_kind = (entity_kind or "").lower().strip()
    action = (action or "").lower().strip()
    payload = dict(payload or {})
    if entity_kind not in ("node", "edge"):
        raise ValueError("entity_kind must be node|edge")
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {sorted(ACTIONS)}")

    if entity_kind == "node":
        dt = dim_type or payload.get("type")
        if action == "create":
            if is_skill_bundle_payload(payload):
                dim_type = "skill_level"
                try:
                    sk = resolve_skill_key(payload)
                except ValueError as e:
                    raise ValueError(str(e)) from e
                if not payload.get("levels"):
                    raise ValueError("技能 bundle 创建需要 levels（L1–L5 对象）")
                title = title or f"新建技能(多档):{sk}"
            else:
                if dt not in DIM_TYPES:
                    raise ValueError(f"create 仅支持四维: {sorted(DIM_TYPES)}")
                if not (payload.get("name") or "").strip():
                    raise ValueError("create 需要 name")
                dim_type = dt
                title = title or f"新建{dt}:{payload.get('name')}"
        elif action == "update" and is_skill_bundle_payload(payload):
            # target_id = skill_key（逻辑技能，非单节点 id）
            sk = target_id or payload.get("skill_key") or payload.get("name")
            if not sk:
                raise ValueError("技能 bundle 更新需要 target_id=skill_key")
            target_id = str(sk).strip()
            dim_type = "skill_level"
            title = title or f"更新技能(多档):{target_id}"
        else:
            tid = target_id or payload.get("id")
            if not tid:
                raise ValueError(f"{action} 需要 target_id")
            target_id = tid
            # 校验存在（enable 时 skill_key 可能不是节点 id）
            if not (
                action in ("enable", "disable")
                and (dim_type or "").lower()
                in ("skill_level", "skill", "skill_bundle")
            ):
                with connect() as conn:
                    row = conn.execute(
                        "SELECT type, name FROM kg_node WHERE id=%s", (tid,)
                    ).fetchone()
                if not row:
                    raise ValueError("节点不存在")
                if row["type"] not in DIM_TYPES:
                    raise ValueError("仅四维节点可走变更接口")
                dim_type = row["type"]
                title = title or f"{action}:{row['name'] or tid}"
            else:
                dim_type = dim_type or "skill_level"
                title = title or f"{action}:{tid}"
    else:
        # edge
        if action == "create":
            for k in ("src_id", "dst_id", "rel_type"):
                if not payload.get(k):
                    raise ValueError(f"edge create 需要 {k}")
            title = title or f"新建边:{payload.get('rel_type')}"
        else:
            tid = target_id or payload.get("id")
            if not tid:
                raise ValueError(f"edge {action} 需要 target_id")
            target_id = tid
            title = title or f"边{action}:{tid}"

    from backend.settings import REVIEW_REQUIRED

    if not REVIEW_REQUIRED:
        cr = {
            "entity_kind": entity_kind,
            "action": action,
            "dim_type": dim_type,
            "target_id": target_id,
            "title": title,
            "payload": payload,
        }
        applied = _apply(cr, user_id=user_id, user_name=user_name)
        return {
            "id": 0,
            "entity_kind": entity_kind,
            "action": action,
            "dim_type": dim_type,
            "target_id": target_id,
            "title": title,
            "payload": payload,
            "status": "applied",
            "created_by": user_id,
            "created_by_name": user_name,
            "created_at": None,
            "applied": applied,
            "direct": True,
            "review_required": False,
        }

    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO kg_change_request
              (entity_kind, action, dim_type, target_id, title, payload,
               created_by, created_by_name)
            VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            RETURNING *
            """,
            (
                entity_kind,
                action,
                dim_type,
                target_id,
                title,
                json.dumps(payload, ensure_ascii=False),
                user_id,
                user_name,
            ),
        ).fetchone()
        conn.commit()
    out = _cr_dict(row)
    out["direct"] = False
    out["review_required"] = True
    return out


def list_pending(limit: int = 50, dim_type: str | None = None) -> list[dict[str, Any]]:
    with connect() as conn:
        if dim_type:
            rows = conn.execute(
                """
                SELECT * FROM kg_change_request
                WHERE dim_type = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (dim_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM kg_change_request
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
    return [_cr_dict(r) for r in rows]


def get_change(cid: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM kg_change_request WHERE id=%s", (cid,)
        ).fetchone()
    return _cr_dict(row) if row else None


def approve_change(cid: int, *, user_id: str, user_name: str) -> dict[str, Any]:
    """通过并立即生效，然后删除待审记录。"""
    cr = get_change(cid)
    if not cr:
        raise ValueError("待审记录不存在")
    applied = _apply(cr, user_id=user_id, user_name=user_name)
    with connect() as conn:
        conn.execute("DELETE FROM kg_change_request WHERE id=%s", (cid,))
        conn.commit()
    return {"approved": True, "id": cid, "applied": applied}


def reject_change(cid: int) -> dict[str, Any]:
    """驳回：删除待审记录。"""
    with connect() as conn:
        cur = conn.execute("DELETE FROM kg_change_request WHERE id=%s", (cid,))
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError("待审记录不存在")
    return {"rejected": True, "id": cid, "deleted": True}


def _gate_error_message(exc: PublishGateError) -> str:
    parts = [f"{v.get('rule')}: {v.get('message')}" for v in (exc.violations or [])]
    return "发布门禁未通过 — " + ("; ".join(parts) or str(exc))


def _try_publish_after_write(
    *,
    node_type: str | None,
    node_id: str | None,
    skill_key: str | None = None,
    region: str = "CN",
    user_id: str,
    user_name: str,
) -> dict[str, Any]:
    """
    写入后尝试升为 published。
    不达标则保持 draft，返回 gate（不抛错——新建通过后可先落草稿）。
    """
    gate = validate_publish(
        node_type=node_type,
        node_id=node_id,
        skill_key=skill_key,
        region=region,
        action="enable",
    )
    if not gate["ok"]:
        return {"status": "draft", "gate": gate}
    if skill_key and (node_type or "") in (
        "skill_level",
        "skill",
        "skill_bundle",
        "",
    ):
        from backend.kg.pg_store.publish_rules import _set_skill_key_status

        _set_skill_key_status(skill_key, "published", region=region)
        return {"status": "published", "gate": gate, "skill_key": skill_key}
    if node_id:
        patch_node(
            node_id, {"status": "published"}, user_id=user_id, user_name=user_name
        )
        return {"status": "published", "gate": gate, "node_id": node_id}
    return {"status": "draft", "gate": gate}


def _apply(cr: dict[str, Any], *, user_id: str, user_name: str) -> dict[str, Any]:
    kind = cr["entity_kind"]
    action = cr["action"]
    payload = cr.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)

    if kind == "node":
        if action == "create":
            body = dict(payload)
            # 先落 draft，门禁通过后再升 published（BR-08）
            body["status"] = "draft"
            body.setdefault("type", cr.get("dim_type"))
            body.setdefault("region", "CN")
            region = str(body.get("region") or "CN")
            if is_skill_bundle_payload(body):
                result = apply_skill_bundle_create(
                    body, user_id=user_id, user_name=user_name
                )
                sk = result.get("skill_key")
                pub = _try_publish_after_write(
                    node_type="skill_bundle",
                    node_id=None,
                    skill_key=sk,
                    region=region,
                    user_id=user_id,
                    user_name=user_name,
                )
                return {
                    "skill_bundle": True,
                    "skill_key": sk,
                    "nodes": result.get("nodes") or [],
                    "linked_edges": result.get("edges") or [],
                    "levels": result.get("levels") or [],
                    "bundle": result.get("bundle"),
                    **pub,
                }
            # industry_ids / major_ids / occupation_ids 由 create_node 自动建边
            node = create_node(body, user_id=user_id, user_name=user_name)
            ntype = node.get("type") or body.get("type")
            pub = _try_publish_after_write(
                node_type=ntype,
                node_id=node.get("id"),
                region=region,
                user_id=user_id,
                user_name=user_name,
            )
            # 刷新节点状态
            from backend.kg.pg_store.query import get_node

            node = get_node(node["id"]) or node
            return {
                "node": node,
                "linked_edges": node.get("linked_edges") or [],
                **pub,
            }
        tid = cr.get("target_id") or payload.get("id")
        if action == "update":
            body = {k: v for k, v in payload.items() if k not in ("id", "type")}
            # 禁止经 update 直改 published；须走 enable + 门禁
            if str(body.get("status") or "").lower() == "published":
                body = {**body, "status": "draft"}
            if is_skill_bundle_payload(payload) or is_skill_bundle_payload(body):
                sk = tid or payload.get("skill_key")
                result = apply_skill_bundle_update(
                    str(sk),
                    {**payload, **body, "status": body.get("status") or "draft"},
                    user_id=user_id,
                    user_name=user_name,
                )
                return {
                    "skill_bundle": True,
                    "skill_key": result.get("skill_key"),
                    "nodes": result.get("nodes") or [],
                    "linked_edges": result.get("edges") or [],
                    "levels": result.get("levels") or [],
                    "bundle": result.get("bundle"),
                    "status": "draft",
                    "note": "更新后保持/置为草稿，发布请走 enable 审核",
                }
            # 可带 industry_ids / major_ids / occupation_ids 重建关联边
            node = patch_node(tid, body, user_id=user_id, user_name=user_name)
            return {
                "node": node,
                "linked_edges": (node or {}).get("linked_edges") or [],
            }
        if action == "disable":
            from backend.kg.pg_store.query import get_node
            from backend.kg.pg_store.skill_aggregate import skill_key_from_node
            from backend.kg.pg_store.publish_rules import _set_skill_key_status

            n = get_node(tid) if tid else None
            region = (n or {}).get("region") or (payload.get("region") if isinstance(payload, dict) else None) or "CN"
            if n and n.get("type") == "skill_level":
                sk = skill_key_from_node(n)
                _set_skill_key_status(sk, "disabled", region=region)
                return {"skill_key": sk, "status": "disabled"}
            if (cr.get("dim_type") or "").lower() in (
                "skill_level",
                "skill",
                "skill_bundle",
            ) and tid:
                # target_id 为 skill_key
                _set_skill_key_status(str(tid), "disabled", region=region)
                return {"skill_key": str(tid), "status": "disabled"}
            node = patch_node(
                tid, {"status": "disabled"}, user_id=user_id, user_name=user_name
            )
            return {"node": node}
        if action == "enable":
            # BR-08 硬门禁：不达标拒绝发布
            from backend.kg.pg_store.query import get_node
            from backend.kg.pg_store.skill_aggregate import skill_key_from_node

            n = get_node(tid) if tid else None
            ntype = (n or {}).get("type") or cr.get("dim_type")
            region = (n or {}).get("region") or "CN"
            sk = None
            if n and n.get("type") == "skill_level":
                sk = skill_key_from_node(n)
                ntype = "skill_bundle"
            elif (cr.get("dim_type") or "").lower() in (
                "skill_level",
                "skill",
                "skill_bundle",
            ):
                sk = tid
                ntype = "skill_bundle"
            try:
                gate = assert_publish_allowed(
                    node_type=ntype,
                    node_id=None if sk else tid,
                    skill_key=sk,
                    region=region,
                    action="enable",
                )
            except PublishGateError as e:
                raise ValueError(_gate_error_message(e)) from e
            if sk:
                from backend.kg.pg_store.publish_rules import _set_skill_key_status

                _set_skill_key_status(sk, "published", region=region)
                return {
                    "skill_key": sk,
                    "status": "published",
                    "gate": gate,
                }
            node = patch_node(
                tid, {"status": "published"}, user_id=user_id, user_name=user_name
            )
            return {"node": node, "status": "published", "gate": gate}
        if action == "delete":
            from backend.kg.pg_store.query import get_node
            from backend.kg.pg_store.skill_aggregate import skill_key_from_node

            n = get_node(tid) if tid else None
            if n and n.get("type") == "skill_level":
                sk = skill_key_from_node(n)
                try:
                    assert_publish_allowed(
                        node_type="skill_level",
                        skill_key=sk,
                        region=n.get("region") or "CN",
                        action="delete",
                    )
                except PublishGateError as e:
                    raise ValueError(_gate_error_message(e)) from e
            # `deleted` 恒为 bool，条数放 `delete_result`。
            # 原来这里直接 `{"deleted": _physical_delete_node(tid)}` 返回 dict，
            # 而驳回那条路（:266）返回的是 True —— 同一字段两种形状，
            # 响应模型声明的 bool 直接把整个删除接口打成 500。
            return {"deleted": True, "delete_result": _physical_delete_node(tid)}
        raise ValueError(f"unsupported node action {action}")

    if kind == "edge":
        if action == "create":
            body = dict(payload)
            body["status"] = "published"
            body.setdefault("region", "CN")
            edge = create_edge(body, user_id=user_id, user_name=user_name)
            return {"edge": edge}
        tid = cr.get("target_id") or payload.get("id")
        if action == "update":
            return {"edge": _patch_edge(tid, payload, user_id, user_name)}
        if action == "delete":
            return {"deleted": True, "delete_result": _physical_delete_edge(tid)}
        if action in ("disable", "enable"):
            st = "disabled" if action == "disable" else "published"
            return {"edge": _patch_edge(tid, {"status": st}, user_id, user_name)}
        raise ValueError(f"unsupported edge action {action}")

    raise ValueError(f"unsupported entity_kind {kind}")


def _physical_delete_node(node_id: str) -> dict[str, Any]:
    """删除节点及其关联边，不级联删其它节点。"""
    with connect() as conn:
        e = conn.execute(
            "DELETE FROM kg_edge WHERE src_id=%s OR dst_id=%s",
            (node_id, node_id),
        )
        edges_deleted = e.rowcount
        n = conn.execute("DELETE FROM kg_node WHERE id=%s", (node_id,))
        nodes_deleted = n.rowcount
        conn.commit()
    return {
        "node_id": node_id,
        "nodes_deleted": int(nodes_deleted),
        "edges_deleted": int(edges_deleted),
    }


def _physical_delete_edge(edge_id: str) -> dict[str, Any]:
    with connect() as conn:
        cur = conn.execute("DELETE FROM kg_edge WHERE id=%s", (edge_id,))
        conn.commit()
        return {"edge_id": edge_id, "edges_deleted": int(cur.rowcount)}


def _patch_edge(
    edge_id: str, data: dict[str, Any], user_id: str, user_name: str
) -> dict[str, Any]:
    fields = []
    params: dict[str, Any] = {
        "id": edge_id,
        "updated_by": user_id,
        "updated_by_name": user_name,
    }
    for key in ("weight", "evidence", "confidence", "status", "source_url"):
        if key in data and data[key] is not None:
            fields.append(f"{key} = %({key})s")
            params[key] = data[key]
    if "attrs" in data:
        fields.append("attrs = %(attrs)s")
        params["attrs"] = (
            json.dumps(data["attrs"], ensure_ascii=False)
            if not isinstance(data["attrs"], str)
            else data["attrs"]
        )
    if not fields:
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM kg_edge WHERE id=%s", (edge_id,)
            ).fetchone()
        if not row:
            raise ValueError("edge not found")
        return dict(row)
    fields.append("updated_by = %(updated_by)s")
    fields.append("updated_by_name = %(updated_by_name)s")
    sql = f"UPDATE kg_edge SET {', '.join(fields)} WHERE id = %(id)s RETURNING *"
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
        conn.commit()
    if not row:
        raise ValueError("edge not found")
    return dict(row)


def _cr_dict(row: Any) -> dict[str, Any]:
    if not row:
        return {}
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            pass
    created = row.get("created_at")
    return {
        "id": row["id"],
        "entity_kind": row["entity_kind"],
        "action": row["action"],
        "dim_type": row.get("dim_type"),
        "target_id": row.get("target_id"),
        "title": row.get("title"),
        "payload": payload,
        "status": "pending",  # 队列内仅待审
        "created_by": row.get("created_by"),
        "created_by_name": row.get("created_by_name"),
        "created_at": created.isoformat() if hasattr(created, "isoformat") else str(created or ""),
    }


# ── 兼容旧 proposal API 名称 ─────────────────────────────────


def create_proposal(kind: str, payload: dict, *, user_id: str, user_name: str):
    """旧接口适配 → submit_change。"""
    kind = (kind or "").lower()
    if kind in ("node", "create_node"):
        return submit_change(
            entity_kind="node",
            action="create",
            payload=payload,
            user_id=user_id,
            user_name=user_name,
        )
    if kind in ("edge", "create_edge"):
        return submit_change(
            entity_kind="edge",
            action="create",
            payload=payload,
            user_id=user_id,
            user_name=user_name,
        )
    if kind in ("patch_node", "update_node"):
        return submit_change(
            entity_kind="node",
            action="update",
            payload=payload,
            target_id=payload.get("id") or payload.get("node_id"),
            user_id=user_id,
            user_name=user_name,
        )
    raise ValueError(f"unsupported legacy kind: {kind}")


def list_proposals(status: str | None = "pending", limit: int = 50):
    # 队列只有 pending
    return list_pending(limit=limit)


def get_proposal(pid: int):
    return get_change(pid)


def review_proposal(pid: int, *, action: str, user_id: str, user_name: str, reason=None):
    action = (action or "").lower()
    if action == "approve":
        return approve_change(pid, user_id=user_id, user_name=user_name)
    if action == "reject":
        return reject_change(pid)
    raise ValueError("action must be approve|reject")
