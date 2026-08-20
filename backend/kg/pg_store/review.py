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
from backend.kg.pg_store.config import prefer_draft, prefer_draft_edge
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
                        f"SELECT type, name FROM kg_node WHERE id=%s "
                        f"AND {prefer_draft()}",
                        (tid,),
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
        # REVIEW_REQUIRED=0：这就是「管理台直接编辑」，按草稿态方案落草稿行，
        # 前台在发布前不变。REVIEW_REQUIRED=1 时本分支不走，approve 才落地（写线上行）。
        applied = _apply(cr, user_id=user_id, user_name=user_name, to_draft=True)
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
    # 审核通过 = 批准发布 → 写线上行。审核功能本轮不在范围内，这条路径**行为不变**，
    # 只是显式把 to_draft=False 写出来，别让它跟着上面直写分支的默认值漂走。
    applied = _apply(cr, user_id=user_id, user_name=user_name, to_draft=False)
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


def _sk_name(skill_key: Any) -> str | None:
    """技能操作回执里的展示名；没有 key 就返回 None，查不到回落成 code。

    运营点完「发布 / 停用 / 删除」看到的就是这个回执。`skill_key` 从 2026-08-19
    起是 `SKxxxxxxxxxx`，只回 code 的话运营无法确认自己操作的是哪个技能。

    连草稿行一起查（`online_only_rows=False`）：审核路径里技能常常只有草稿行
    （新建还没发布就存草稿），只读线上行会拿不到名字。这是管理台回执，
    不存在草稿泄漏到学员端的问题。
    """
    sk = str(skill_key or "").strip()
    if not sk:
        return None
    from backend.kg.pg_store.skill_aggregate import resolve_skill_names

    return resolve_skill_names([sk], online_only_rows=False).get(sk) or sk


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
        return {"status": "published", "gate": gate, "skill_key": skill_key,
                "skill_name": _sk_name(skill_key)}
    if node_id:
        patch_node(
            node_id,
            {"status": "published"},
            user_id=user_id,
            user_name=user_name,
            # 发布侧：直接落线上行（审核路径本轮不改行为）
            to_draft=False,
        )
        return {"status": "published", "gate": gate, "node_id": node_id}
    return {"status": "draft", "gate": gate}


def _publish_or_draft(to_draft: bool, **kwargs: Any) -> dict[str, Any]:
    """草稿模式下**不要**顺手发布。

    原来「新建完立刻试着升 published」是单行模型的做法（内容和状态挤在一行，
    不升就没人看得见）。有了草稿行之后，新建本来就该停在草稿态，
    由运营在待发布清单里确认后发布 —— 顺手发布等于绕过了整个机制。
    """
    if to_draft:
        return {"status": "draft", "gate": None, "note": "已存为草稿，发布请走 POST /v1/admin/publish/node"}
    return _try_publish_after_write(**kwargs)


def _apply(
    cr: dict[str, Any], *, user_id: str, user_name: str, to_draft: bool = False
) -> dict[str, Any]:
    """把一条变更落到库里。

    进草稿的只有**内容类动作**（`to_draft=True` 时生效）：
    节点基础属性、边、技能构成、技能自身属性。它们落草稿行，并且不顺手尝试发布 ——
    发布是运营的独立动作。

    **状态类动作立即生效，永远不进草稿**（2026-08-19 需求收窄）：
    `disable` 直接改线上行 status 并级联边；`delete` 直接物理删；
    `enable` 有草稿就发布那份草稿（管理台「发布」按钮走的就是它），没草稿就直接置 published。
    """
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
                    body, user_id=user_id, user_name=user_name, to_draft=to_draft
                )
                sk = result.get("skill_key")
                pub = _publish_or_draft(
                    to_draft,
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
                    "skill_name": _sk_name(sk),
                    "nodes": result.get("nodes") or [],
                    "linked_edges": result.get("edges") or [],
                    "levels": result.get("levels") or [],
                    "bundle": result.get("bundle"),
                    **pub,
                }
            # industry_ids / major_ids / occupation_ids 由 create_node 自动建边
            node = create_node(
                body, user_id=user_id, user_name=user_name, to_draft=to_draft
            )
            ntype = node.get("type") or body.get("type")
            pub = _publish_or_draft(
                to_draft,
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
                    to_draft=to_draft,
                )
                return {
                    "skill_bundle": True,
                    "skill_key": result.get("skill_key"),
                    "skill_name": result.get("skill_name") or _sk_name(result.get("skill_key")),
                    "nodes": result.get("nodes") or [],
                    "linked_edges": result.get("edges") or [],
                    "levels": result.get("levels") or [],
                    "bundle": result.get("bundle"),
                    "status": "draft",
                    "note": "更新后保持/置为草稿，发布请走 enable 审核",
                }
            # 可带 industry_ids / major_ids / occupation_ids 重建关联边
            node = patch_node(
                tid, body, user_id=user_id, user_name=user_name, to_draft=to_draft
            )
            return {
                "node": node,
                "linked_edges": (node or {}).get("linked_edges") or [],
            }
        if action == "disable":
            # **停用立即生效**（2026-08-19 需求收窄）：草稿只管内容，不管状态动作。
            # 但级联必须一起做 —— 只改节点不改边就会留下「published 的边指向 disabled
            # 的节点」，前台 5 项 Σ0.81 / 管理台 6 项 Σ1.00，两边都不报错。
            from backend.kg.pg_store.query import get_node
            from backend.kg.pg_store.skill_aggregate import skill_key_from_node
            from backend.kg.pg_store.write import set_node_status_now

            n = get_node(tid) if tid else None
            region = (n or {}).get("region") or (payload.get("region") if isinstance(payload, dict) else None) or "CN"
            sk = None
            if n and n.get("type") == "skill_level":
                sk = skill_key_from_node(n)
            elif (cr.get("dim_type") or "").lower() in (
                "skill_level",
                "skill",
                "skill_bundle",
            ) and tid:
                sk = str(tid)   # target_id 就是 skill_key
            if sk:
                # 技能是逻辑实体：一个 skill_key 有 L1–L5 五个节点，逐个停用 + 级联边。
                # 不能只调 `_set_skill_key_status`（它只改节点，边不管）
                ids = _skill_key_node_ids(sk, region)
                if not ids:
                    raise ValueError(f"技能不存在：{sk}")
                cascaded = 0
                for nid in ids:
                    r = set_node_status_now(
                        nid, "disabled", user_id=user_id, user_name=user_name
                    )
                    cascaded += int((r or {}).get("cascaded_edges") or 0)
                return {
                    "skill_key": sk,
                    "skill_name": _sk_name(sk),
                    "status": "disabled",
                    "node_ids": ids,
                    "note": f"已停用 {len(ids)} 个档位，并同步停用 {cascaded} 条关联边",
                }
            node = set_node_status_now(
                tid, "disabled", user_id=user_id, user_name=user_name
            )
            if not node:
                raise ValueError("节点不存在")
            return {"node": node, "status": "disabled"}
        if action == "enable":
            # BR-08 硬门禁：不达标拒绝发布
            from backend.kg.pg_store.query import get_node, unit_draft_kinds
            from backend.kg.pg_store.skill_aggregate import skill_key_from_node

            # **有草稿就走草稿发布**，不要在这里另做一次「只翻 status」的发布。
            # 两条发布入口两套逻辑的后果实测过：管理台点「发布」走到这里，
            # 只把线上行 status 置 published、version +1，草稿一动不动 ——
            # 于是改的名字没生效，而且 base_version(1) ≠ version(2)，
            # 那份草稿从此永远 409，运营再也发不出去。
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

            # 这次要发布哪些**发布单元**：普通节点就是它自己；
            # 技能是逻辑实体，`target_id` 传的是 **skill_key**（不是节点 id），
            # 单元是它的 L1–L5 五个节点。上一版只用 `tid` 去查草稿，
            # skill_key 当然查不到任何单元 → 技能库点「发布」走进下面的老路径
            # （只翻线上行 status），草稿一直留着，页面永远显示草稿、发不出去。
            unit_ids = _skill_key_node_ids(sk, region) if sk else ([tid] if tid else [])
            drafted = list(unit_draft_kinds(unit_ids))
            if drafted:
                from backend.kg.pg_store.draft_publish import publish_node

                results = [
                    publish_node(i, user_id=user_id, user_name=user_name)
                    for i in drafted
                ]
                last = results[-1]
                return {
                    "node": get_node(tid, scope="online") if not sk else None,
                    "skill_key": sk,
                    "skill_name": _sk_name(sk),
                    "node_ids": drafted if sk else None,
                    "status": last.get("status"),
                    "gate": last.get("gate"),
                    "note": (
                        f"已发布 {len(drafted)} 个单元的草稿（内容 + 关联一起生效）"
                        if sk
                        else "已发布该单元的草稿（内容 + 关联一起生效）"
                    ),
                }
            # 没有草稿 = 纯状态动作「启用」。**先把停用时级联关掉的边恢复，再跑门禁**：
            # 门禁看的是库里已发布的事实，边还是 disabled 的话 BR-03 判「无有效权重」，
            # 于是「停用 → 启用」变成死路（实测 400）。门禁不过就精确回滚这批边。
            from backend.kg.pg_store.write import cascade_edge_status_now

            restored: list[str] = []
            if unit_ids and (n or {}).get("status") == "disabled":
                for nid in unit_ids:
                    restored += cascade_edge_status_now(
                        nid, "published", user_id=user_id, user_name=user_name
                    )
            try:
                gate = assert_publish_allowed(
                    node_type=ntype,
                    node_id=None if sk else tid,
                    skill_key=sk,
                    region=region,
                    action="enable",
                )
            except PublishGateError as e:
                if restored:
                    # 门禁没过 → 把刚恢复的边按原样关回去，别留下「节点还停用、边却启用了」
                    with connect() as conn:
                        conn.execute(
                            "UPDATE kg_edge SET status='disabled' "
                            "WHERE id = ANY(%s) AND NOT is_draft",
                            (restored,),
                        )
                        conn.commit()
                raise ValueError(_gate_error_message(e)) from e
            if sk:
                from backend.kg.pg_store.publish_rules import _set_skill_key_status

                _set_skill_key_status(sk, "published", region=region)
                return {
                    "skill_key": sk,
                    "skill_name": _sk_name(sk),
                    "status": "published",
                    "gate": gate,
                }
            # 走 set_node_status_now 而不是裸 patch_node：启用要**级联把边也恢复**，
            # 否则「停用 → 启用」之后边还是 disabled，岗位没有已发布的 requires 边，
            # 下次发布/门禁又判不过（停用那次的级联把它们关掉了）
            from backend.kg.pg_store.write import set_node_status_now

            node = set_node_status_now(
                tid, "published", user_id=user_id, user_name=user_name
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
            # **删除立即执行**（2026-08-19 需求收窄）：不再落「发布时才删」的意图。
            # 顺带把这条记录的草稿一起清掉 —— 记录都没了，留着草稿就是孤儿
            # （`_physical_delete_node` 两行一起删，见它的 docstring）。
            # `deleted` 恒为 bool，条数放 `delete_result`。原来这里直接
            # `{"deleted": _physical_delete_node(tid)}` 返回 dict，而驳回那条路
            # （:266）返回的是 True —— 同一字段两种形状，响应模型声明的 bool
            # 直接把整个删除接口打成 500。
            return {"deleted": True, "delete_result": _physical_delete_node(tid)}
        raise ValueError(f"unsupported node action {action}")

    if kind == "edge":
        if action == "create":
            body = dict(payload)
            body["status"] = "published"
            body.setdefault("region", "CN")
            edge = create_edge(
                body, user_id=user_id, user_name=user_name, to_draft=to_draft
            )
            return {"edge": edge}
        tid = cr.get("target_id") or payload.get("id")
        if action == "update":
            if to_draft:
                from backend.kg.pg_store.write import patch_edge_draft

                edge = patch_edge_draft(
                    tid, payload, user_id=user_id, user_name=user_name
                )
                if edge is None:
                    raise ValueError("edge not found")
                return {"edge": edge, "status": "draft"}
            return {"edge": _patch_edge(tid, payload, user_id, user_name)}
        if action == "delete":
            # 删除立即执行（同节点删除）。注意与 `DELETE /v1/kg/edges/{id}`（archive_edge）
            # 的区别：那是**归档一条边**，属于「边」这一类内容编辑，仍然走草稿墓碑；
            # 这里是**物理删除**，点了就没了
            return {"deleted": True, "delete_result": _physical_delete_edge(tid)}
        if action in ("disable", "enable"):
            st = "disabled" if action == "disable" else "published"
            return {"edge": _patch_edge(tid, {"status": st}, user_id, user_name)}
        raise ValueError(f"unsupported edge action {action}")

    raise ValueError(f"unsupported entity_kind {kind}")


def _skill_key_node_ids(skill_key: str, region: str = "CN") -> list[str]:
    """一个 skill_key 下的全部档位节点 id（草稿优先去重）。"""
    from backend.kg.pg_store.config import prefer_draft
    from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL

    with connect() as conn:
        return [
            r["id"]
            for r in conn.execute(
                f"""
                SELECT n.id FROM kg_node n
                WHERE n.type='skill_level' AND (%s::text IS NULL OR n.region = %s)
                  AND ({SKILL_KEY_SQL}) = %s
                  AND COALESCE(n.status,'published') <> 'archived'
                  AND {prefer_draft('n')}
                ORDER BY n.id
                """,
                (region, region, skill_key),
            ).fetchall()
        ]


def _physical_delete_node(node_id: str) -> dict[str, Any]:
    """删除节点及其关联边，不级联删其它节点。

    物理删除是**两行一起删**（线上行 + 草稿行 + 该单元的草稿边）—— 这里不加
    `is_draft` 谓词是有意的，留着草稿行就成了「指向不存在记录的孤儿草稿」。
    但**计数只数线上行**：运营删的是一条记录，报 2 会让人以为顺带删了别的东西。
    """
    with connect() as conn:
        # 先删草稿（不计数），再删线上行（计数）
        conn.execute(
            "DELETE FROM kg_edge WHERE is_draft AND (src_id=%s OR dst_id=%s OR unit_id=%s)",
            (node_id, node_id, node_id),
        )
        e = conn.execute(
            "DELETE FROM kg_edge WHERE NOT is_draft AND (src_id=%s OR dst_id=%s)",
            (node_id, node_id),
        )
        edges_deleted = e.rowcount
        conn.execute("DELETE FROM kg_node WHERE id=%s AND is_draft", (node_id,))
        n = conn.execute(
            "DELETE FROM kg_node WHERE id=%s AND NOT is_draft", (node_id,)
        )
        nodes_deleted = n.rowcount
        conn.commit()
    return {
        "node_id": node_id,
        "nodes_deleted": int(nodes_deleted),
        "edges_deleted": int(edges_deleted),
    }


def _physical_delete_edge(edge_id: str) -> dict[str, Any]:
    """物理删边：两行一起删，但只数线上行（同 `_physical_delete_node`）。"""
    with connect() as conn:
        conn.execute("DELETE FROM kg_edge WHERE id=%s AND is_draft", (edge_id,))
        cur = conn.execute(
            "DELETE FROM kg_edge WHERE id=%s AND NOT is_draft", (edge_id,)
        )
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
                f"SELECT * FROM kg_edge WHERE id=%s AND {prefer_draft_edge('kg_edge')}",
                (edge_id,),
            ).fetchone()
        if not row:
            raise ValueError("edge not found")
        return dict(row)
    fields.append("updated_by = %(updated_by)s")
    fields.append("updated_by_name = %(updated_by_name)s")
    # 审核落地侧只改线上行。不钉 `NOT is_draft` 的话这条 UPDATE 会连草稿行一起改，
    # 写 status='published' 当场撞 ck_kg_edge_draft_status
    sql = (
        f"UPDATE kg_edge SET {', '.join(fields)} "
        f"WHERE id = %(id)s AND NOT is_draft RETURNING *"
    )
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
