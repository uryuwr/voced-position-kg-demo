"""草稿的清单 / 发布 / 丢弃 —— 草稿态方案 §7。

一个**发布单元** = 一个节点草稿行 + 所有 `unit_id` 指向它的草稿边（约定 `unit_id = src_id`）。
发布在一个事务里按序做完，任一步不通过就整体回滚，库里一个字节都不留。

为什么发布是「删线上行 + 把草稿行原地转正」而不是「用草稿的值 UPDATE 线上行」：
后者要逐列点名，主表加了列而这里忘了加，那一列会在发布时被静默丢掉 —— 影子表方案
就是因为这个被否掉（方案 §1.3），换个写法把同样的坑挖回来没有意义。
"""
from __future__ import annotations

import json
from typing import Any

from backend.kg.pg_store.client import connect
from backend.kg.pg_store.config import DEFAULT_REGION
from backend.kg.pg_store.publish_rules import PublishGateError, validate_publish


class DraftNotFound(LookupError):
    """这个 id 上没有草稿（可能已被别人发布或丢弃）。"""


class DraftConflict(RuntimeError):
    """并发：草稿基于的版本已经不是线上版本了 —— 你编辑期间别人发布过。"""

    def __init__(self, node_id: str, base_version: int | None, cur_version: int | None):
        self.node_id = node_id
        self.base_version = base_version
        self.cur_version = cur_version
        super().__init__(
            f"「{node_id}」在你编辑期间被别人发布过"
            f"（你基于 V{base_version}，线上已是 V{cur_version}）；"
            "请刷新后重新编辑，避免把别人的修改覆盖掉"
        )


class CodeTaken(RuntimeError):
    """业务编码在发布这一刻已被别的已发布记录占用。"""

    def __init__(self, code: str, existing: dict[str, Any]):
        self.code = code
        self.existing = existing
        super().__init__(
            f"编码 {code} 已被「{existing.get('name')}」（id={existing.get('id')}）占用，"
            "改一个编码再发布"
        )


class MissingEndpoints(RuntimeError):
    """草稿边指向的节点还没有发布过 —— 发布出去就是孤儿边。"""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__(
            "以下节点尚未发布，先发布它们再发布本单元："
            + "、".join(missing[:10])
            + ("…" if len(missing) > 10 else "")
        )


def _attrs_code(attrs: Any) -> str:
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except json.JSONDecodeError:
            return ""
    if not isinstance(attrs, dict):
        return ""
    return str(attrs.get("code") or "").strip()


# ── 待发布清单 ────────────────────────────────────────────────
def list_drafts(
    *,
    node_type: str | None = None,
    region: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """运营工作台：每个发布单元一行。

    单元不一定有节点草稿行 —— 只改了技能构成（边）时节点本身没变，
    这种单元也必须出现在清单里，否则那批草稿边永远发不出去。
    """
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 200))
    ntype = (node_type or "").strip().lower() or None
    if ntype in ("all", "*"):
        ntype = None
    reg = None if (region or "").strip().lower() in ("all", "*") else (
        (region or "").strip() or DEFAULT_REGION
    )
    q_like = f"%{(q or '').strip()}%" if (q or "").strip() else None

    where = ["1=1"]
    params: list[Any] = []
    if ntype:
        where.append("COALESCE(dn.type, on_.type) = %s")
        params.append(ntype)
    if reg:
        where.append("COALESCE(dn.region, on_.region) = %s")
        params.append(reg)
    if q_like:
        where.append(
            "(lower(COALESCE(dn.name, on_.name)) LIKE lower(%s) OR lower(u.unit_id) LIKE lower(%s))"
        )
        params.extend([q_like, q_like])
    where_sql = " AND ".join(where)

    base_sql = f"""
        FROM (
          SELECT id AS unit_id FROM kg_node WHERE is_draft
          UNION
          SELECT unit_id FROM kg_edge WHERE is_draft AND unit_id IS NOT NULL
        ) u
        LEFT JOIN kg_node dn  ON dn.id  = u.unit_id AND dn.is_draft
        LEFT JOIN kg_node on_ ON on_.id = u.unit_id AND NOT on_.is_draft
        WHERE {where_sql}
    """
    with connect() as conn:
        total = int(
            (
                conn.execute(f"SELECT count(*) AS c {base_sql}", params).fetchone()
                or {"c": 0}
            )["c"]
            or 0
        )
        rows = conn.execute(
            f"""
            SELECT u.unit_id,
                   COALESCE(dn.name, on_.name)  AS name,
                   COALESCE(dn.type, on_.type)  AS type,
                   COALESCE(dn.region, on_.region) AS region,
                   dn.id IS NOT NULL   AS has_node_draft,
                   on_.id IS NOT NULL  AS has_published,
                   dn.target_status, dn.base_version,
                   on_.version AS published_version,
                   on_.status  AS published_status,
                   COALESCE(dn.updated_by_name, dn.updated_by) AS updated_by_name,
                   dn.created_at,
                   -- 只改了边的单元（技能构成）没有草稿节点行，dn.* 全是 NULL。
                   -- 拿它的草稿边的最新时间来排序与展示，否则这些单元被 NULLS LAST
                   -- 永远压在最后一页，运营根本找不到要发布什么。
                   (SELECT max(de.created_at) FROM kg_edge de
                     WHERE de.is_draft AND de.unit_id = u.unit_id) AS edge_last_at
            {base_sql}
            ORDER BY COALESCE(
                       dn.created_at,
                       (SELECT max(de.created_at) FROM kg_edge de
                         WHERE de.is_draft AND de.unit_id = u.unit_id)
                     ) DESC NULLS LAST, u.unit_id
            LIMIT %s OFFSET %s
            """,
            params + [page_size, (page - 1) * page_size],
        ).fetchall()
        ids = [r["unit_id"] for r in rows]
        edge_stat: dict[str, dict[str, int]] = {}
        if ids:
            for r in conn.execute(
                """
                SELECT unit_id,
                       count(*) FILTER (
                         WHERE COALESCE(target_status,'') <> 'archived'
                       ) AS upsert_c,
                       -- 墓碑（待归档）= 运营在技能构成/关联里移除的那些项，
                       -- 发布后这条关联就从前台消失。停用/删除已改成立即生效，
                       -- 不会再出现在草稿里
                       count(*) FILTER (WHERE target_status = 'archived') AS remove_c,
                       -- 编辑人只作展示用：同一单元的边通常是一个人在改，
                       -- 真要精确到「最后改的那个人」得再套 DISTINCT ON，不值当
                       max(COALESCE(updated_by_name, updated_by)) AS by_name
                FROM kg_edge
                WHERE is_draft AND unit_id = ANY(%s)
                GROUP BY unit_id
                """,
                (ids,),
            ).fetchall():
                edge_stat[r["unit_id"]] = {
                    "edges_upsert": int(r["upsert_c"] or 0),
                    "edges_remove": int(r["remove_c"] or 0),
                    "_by_name": r.get("by_name"),
                }

    items = []
    for r in rows:
        st = dict(
            edge_stat.get(r["unit_id"], {"edges_upsert": 0, "edges_remove": 0})
        )
        by_name = st.pop("_by_name", None)
        stale = (
            r["base_version"] is not None
            and r["published_version"] is not None
            and int(r["base_version"]) != int(r["published_version"])
        )
        created = r.get("created_at") or r.get("edge_last_at")
        has_node_draft = bool(r["has_node_draft"])
        has_edge_draft = bool(st["edges_upsert"] or st["edges_remove"])
        # 改的是什么：节点字段 / 只有关联与技能构成 / 两者都有。
        # 运营在清单上得先看懂「这条要发布的是什么」才敢点发布。
        change_kind = (
            "both" if (has_node_draft and has_edge_draft)
            else ("node" if has_node_draft else "edges")
        )
        items.append(
            {
                "node_id": r["unit_id"],
                "name": r.get("name"),
                "type": r.get("type"),
                "region": r.get("region"),
                # 从未发布过 → 发布后前台才第一次看到它
                "is_new": not r["has_published"],
                "has_node_draft": has_node_draft,
                "change_kind": change_kind,
                "change_label": {
                    "node": "节点字段有改动",
                    "edges": "仅关联/技能构成有改动",
                    "both": "节点字段与关联都有改动",
                }[change_kind],
                "record_status": "draft",
                "target_status": r.get("target_status"),
                "base_version": r.get("base_version"),
                "published_version": r.get("published_version"),
                "published_status": r.get("published_status"),
                # true = 你编辑期间别人发布过，直接发布会回 409
                "stale": bool(stale),
                "updated_by_name": r.get("updated_by_name") or by_name,
                "created_at": created.isoformat() if hasattr(created, "isoformat") else None,
                **st,
            }
        )
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size if total else 0,
    }


# ── 发布 ─────────────────────────────────────────────────────
def _bundle_sibling_draft_ids(node_id: str, *, exclude: str = "") -> list[str]:
    """一个技能的发布单元是**整组档位**，不是单个等级节点。

    编辑技能走 `PATCH /v1/admin/skills/{skill_key}`，一次改动会给 L1–L5 各留一行
    草稿。而发布/丢弃接口是按 node_id 收的，只处理传进来那一个 —— 于是列表页点
    一次「发布」只发掉一档，剩下四档还是旧值，**没有任何报错**：技能就这么半新
    半旧地上线了，待发布页还剩 4 行运营也不知道该不该点。

    所以这里按 `attrs.skill_key` 把同组还有草稿的档位一并捞出来。返回空列表表示
    不是技能、或组里没有别的待发布档位。
    """
    from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL

    with connect() as conn:
        row = conn.execute(
            f"SELECT ({SKILL_KEY_SQL}) AS sk, n.region FROM kg_node n "
            f"WHERE n.id = %s AND n.type = 'skill_level' LIMIT 1",
            (node_id,),
        ).fetchone()
        if not row or not row["sk"]:
            return []
        rows = conn.execute(
            f"""
            SELECT DISTINCT n.id FROM kg_node n
            WHERE n.type = 'skill_level' AND n.is_draft
              AND ({SKILL_KEY_SQL}) = %s
              AND (%s::text IS NULL OR n.region = %s)
              AND n.id <> %s
            ORDER BY n.id
            """,
            (row["sk"], row["region"], row["region"], exclude or node_id),
        ).fetchall()
    return [r["id"] for r in rows]


def publish_node(
    node_id: str,
    *,
    user_id: str,
    user_name: str,
    skip_gate: bool = False,
    _in_bundle: bool = False,
) -> dict[str, Any]:
    """发布一个单元。八步全在一个事务里，任一步不过就整体回滚。

    `skip_gate` 只给运维脚本留（批量修数据时门禁会因为**别的**数据不合规而挡住），
    接口层不暴露。
    """
    with connect() as conn:
        # ① 先把这条记录的两行都锁住：两个运营同时点发布时，后到的那个会等，
        #    醒来后 base_version 已经对不上 → 走 409，而不是两个人都以为自己成功了
        conn.execute(
            "SELECT id FROM kg_node WHERE id = %s FOR UPDATE", (node_id,)
        ).fetchall()
        draft = conn.execute(
            "SELECT * FROM kg_node WHERE id = %s AND is_draft", (node_id,)
        ).fetchone()
        online = conn.execute(
            "SELECT * FROM kg_node WHERE id = %s AND NOT is_draft", (node_id,)
        ).fetchone()
        d_edges = conn.execute(
            "SELECT * FROM kg_edge WHERE is_draft AND unit_id = %s ORDER BY id",
            (node_id,),
        ).fetchall()
        if draft is None and not d_edges:
            raise DraftNotFound(node_id)
        if draft is None and online is None:
            # 只剩游离草稿边（节点被物理删了）：发不出去，让运营去丢弃
            raise MissingEndpoints([node_id])

        ntype = (draft or online or {}).get("type")
        region = (draft or online or {}).get("region") or DEFAULT_REGION

        # ② 并发检测：草稿是基于哪个已发布版本改的
        if draft is not None and online is not None:
            base_v = draft.get("base_version")
            cur_v = online.get("version")
            if base_v is not None and int(base_v) != int(cur_v or 0):
                raise DraftConflict(node_id, base_v, cur_v)

        # ③ 业务编码唯一性：唯一索引排除了草稿，所以草稿之间不互斥，得在这里补一次。
        #    只和**线上行**比 —— 那才是真正占着这个编码的记录
        target_status = (draft or {}).get("target_status")
        new_status = target_status or (
            (online or {}).get("status") if online is not None else "published"
        )
        if draft is not None and new_status != "archived":
            code = _attrs_code(draft.get("attrs"))
            if code:
                hit = conn.execute(
                    """
                    SELECT id, name FROM kg_node
                    WHERE NOT is_draft AND id <> %s
                      AND region = %s AND type = %s
                      AND attrs::json->>'code' = %s
                      AND COALESCE(status, 'published') <> 'archived'
                    LIMIT 1
                    """,
                    (node_id, region, ntype, code),
                ).fetchone()
                if hit:
                    raise CodeTaken(code, dict(hit))

        # ④⑤⑥ 套用：线上行有就换掉，没有就把草稿行原地转正
        if draft is not None:
            if online is not None:
                conn.execute(
                    "DELETE FROM kg_node WHERE id = %s AND NOT is_draft", (node_id,)
                )
                new_version = int(online.get("version") or 1) + 1
            else:
                # 新建：第一次发布就是 V1，不该从 V2 起跳
                new_version = int(draft.get("version") or 1)
            conn.execute(
                """
                UPDATE kg_node SET
                  is_draft = false, status = %s, target_status = NULL,
                  base_version = NULL, version = %s,
                  updated_by = %s, updated_by_name = %s
                WHERE id = %s AND is_draft
                """,
                (new_status, new_version, user_id, user_name, node_id),
            )
        else:
            # 只发布了边（技能构成这类）：节点内容没变，但这条记录确实发了一版 ——
            # 版本号跟着 +1，否则运营在列表里看不出「这个岗位的构成刚被改过」，
            # 而且下一个人的草稿 base_version 也就无从对比
            new_version = int((online or {}).get("version") or 1) + (1 if d_edges else 0)
            new_status = (online or {}).get("status") or "published"
            if d_edges:
                conn.execute(
                    "UPDATE kg_node SET version = %s, updated_by = %s, "
                    "updated_by_name = %s WHERE id = %s AND NOT is_draft",
                    (new_version, user_id, user_name, node_id),
                )

        # ⑦ 端点存在性 —— 外键删掉之后，这一步是它的替代（方案 §1.2 / §7 第 4 步）。
        #    注意在套用节点**之后**校验：本单元的节点自己刚转正，它是合法端点
        missing: list[str] = []
        for e in d_edges:
            if (e.get("target_status") or "") == "archived":
                continue
            for kid in (e["src_id"], e["dst_id"]):
                if kid in missing:
                    continue
                ok = conn.execute(
                    "SELECT 1 AS x FROM kg_node WHERE id = %s AND NOT is_draft", (kid,)
                ).fetchone()
                if not ok:
                    missing.append(kid)
        if missing:
            raise MissingEndpoints(missing)

        edges_published = 0
        edges_archived = 0
        for e in d_edges:
            eid = e["id"]
            if (e.get("target_status") or "") in ("archived", "disabled"):
                # 纯状态意图：墓碑（待归档）与「跟着节点一起停用」的级联边都走这里 ——
                # 线上那条边改 status（逻辑删除/停用，与 archive_edge 同口径），草稿行删掉
                intent = e["target_status"]
                conn.execute(
                    "UPDATE kg_edge SET status = %s, updated_by = %s, "
                    "updated_by_name = %s WHERE id = %s AND NOT is_draft",
                    (intent, user_id, user_name, eid),
                )
                conn.execute(
                    "DELETE FROM kg_edge WHERE id = %s AND is_draft", (eid,)
                )
                edges_archived += 1
                continue
            conn.execute("DELETE FROM kg_edge WHERE id = %s AND NOT is_draft", (eid,))
            conn.execute(
                """
                UPDATE kg_edge SET
                  is_draft = false, status = %s, target_status = NULL, unit_id = NULL,
                  updated_by = %s, updated_by_name = %s
                WHERE id = %s AND is_draft
                """,
                (e.get("target_status") or "published", user_id, user_name, eid),
            )
            edges_published += 1

        # ⑧ BR 门禁。**在同一事务里、套用之后、提交之前**跑：
        #    门禁要看的是「发布出去以后长什么样」（新权重、新边），拿套用前的旧内容校
        #    等于放过这一次改动。不合规就抛异常 → 整个事务回滚 → 库里什么都没变，
        #    效果与方案说的「事前拒、不写库」一致，但校的是对的那份内容。
        gate: dict[str, Any] | None = None
        if not skip_gate and new_status == "published":
            gate_type = ntype
            skill_key = None
            if ntype == "skill_level":
                from backend.kg.pg_store.skill_aggregate import skill_key_from_node

                row = conn.execute(
                    "SELECT * FROM kg_node WHERE id = %s AND NOT is_draft", (node_id,)
                ).fetchone()
                if row:
                    skill_key = skill_key_from_node(dict(row))
                gate_type = "skill_bundle"
            gate = validate_publish(
                node_type=gate_type,
                node_id=None if skill_key else node_id,
                skill_key=skill_key,
                region=region,
                action="enable",
                conn=conn,
            )
            if not gate["ok"]:
                conn.rollback()
                raise PublishGateError(gate["failed"])
        conn.commit()

    out = {
        "node_id": node_id,
        "status": new_status,
        "version": new_version,
        "published_node": draft is not None,
        "edges_published": edges_published,
        "edges_archived": edges_archived,
        "gate": gate,
    }

    # 技能：把同组其余待发布档位一并发掉，否则就是静默半发布（见
    # `_bundle_sibling_draft_ids` 的 docstring）。每档各自一个事务 —— 与
    # `publish_batch` 同口径：一档的门禁不过不该拖垮其余，逐项结果放
    # `bundle_siblings` 让前端能显示。
    if ntype == "skill_level" and not _in_bundle:
        sibs = _bundle_sibling_draft_ids(node_id)
        if sibs:
            done: list[dict[str, Any]] = []
            for sid in sibs:
                try:
                    r = publish_node(
                        sid,
                        user_id=user_id,
                        user_name=user_name,
                        skip_gate=skip_gate,
                        _in_bundle=True,
                    )
                    out["edges_published"] += int(r.get("edges_published") or 0)
                    out["edges_archived"] += int(r.get("edges_archived") or 0)
                    done.append({"node_id": sid, "ok": True})
                except (
                    DraftNotFound,
                    DraftConflict,
                    CodeTaken,
                    MissingEndpoints,
                    PublishGateError,
                ) as e:
                    done.append({"node_id": sid, "ok": False, "message": str(e)})
            out["bundle_siblings"] = done
            out["bundle_levels_published"] = 1 + sum(1 for d in done if d["ok"])

    return out


def publish_batch(
    node_ids: list[str], *, user_id: str, user_name: str
) -> dict[str, Any]:
    """逐个走各自的事务：一个门禁不过不该拖垮其余（方案 §7 末）。

    返回逐项结果（成功 / 门禁不过 / 409 / 端点缺失 / 编码冲突），
    前端按 `code` 分类展示，不要只看 ok。
    """
    results = []
    ok_c = 0
    # 技能一次编辑留 5 行草稿，而 `publish_node` 现在会把整组一起发。若不先把
    # 同组的其余档位从待办里剔掉，它们会在轮到自己时因为草稿已经没了而报
    # 「没有草稿」，一次「全部发布」显示成「5 项 1 成功 4 失败」，运营以为出错了。
    # 同组关系必须**发布之前**算，发完草稿行就查不到了。
    todo: list[str] = []
    covered: set[str] = set()
    for nid in dict.fromkeys(node_ids):
        if nid in covered:
            continue
        todo.append(nid)
        covered.update(_bundle_sibling_draft_ids(nid))
    for nid in todo:
        try:
            r = publish_node(nid, user_id=user_id, user_name=user_name)
            ok_c += 1
            results.append({"node_id": nid, "ok": True, "code": "published", "detail": r})
        except DraftNotFound:
            results.append(
                {"node_id": nid, "ok": False, "code": "not_found", "message": "没有草稿"}
            )
        except DraftConflict as e:
            results.append(
                {"node_id": nid, "ok": False, "code": "conflict", "message": str(e)}
            )
        except CodeTaken as e:
            results.append(
                {"node_id": nid, "ok": False, "code": "code_conflict", "message": str(e)}
            )
        except MissingEndpoints as e:
            results.append(
                {
                    "node_id": nid,
                    "ok": False,
                    "code": "missing_endpoints",
                    "message": str(e),
                    "missing": e.missing,
                }
            )
        except PublishGateError as e:
            results.append(
                {
                    "node_id": nid,
                    "ok": False,
                    "code": "gate_failed",
                    "message": str(e),
                    "violations": e.violations,
                }
            )
    return {
        "total": len(results),
        "published": ok_c,
        "failed": len(results) - ok_c,
        "items": results,
    }


# ── 丢弃 ─────────────────────────────────────────────────────
def discard_draft(node_id: str, *, user_id: str, user_name: str) -> dict[str, Any]:
    """丢弃一个单元的草稿：节点草稿行 + 该单元的草稿边一起删，线上行不动。

    没有 revision 留档（方案 §9），丢了就找不回来 —— 前端要二次确认。
    """
    _ = (user_id, user_name)
    with connect() as conn:
        online = conn.execute(
            "SELECT 1 AS x FROM kg_node WHERE id = %s AND NOT is_draft", (node_id,)
        ).fetchone()
        n = conn.execute(
            "DELETE FROM kg_node WHERE id = %s AND is_draft", (node_id,)
        ).rowcount
        e = conn.execute(
            "DELETE FROM kg_edge WHERE is_draft AND unit_id = %s", (node_id,)
        ).rowcount
        if not online:
            # 这条记录从未发布过，丢弃草稿等于它整个消失。**别人单元里指向它的草稿边
            # 也必须一起清掉** —— 端点没了，那些边永远发不出去（发布时会被
            # 「端点未发布」拦下），只会在库里当孤儿，孤儿边闸门当场报红。
            e += conn.execute(
                "DELETE FROM kg_edge WHERE is_draft AND (src_id = %s OR dst_id = %s)",
                (node_id, node_id),
            ).rowcount
        if not n and not e:
            raise DraftNotFound(node_id)
        conn.commit()

    # 技能同理：丢弃一档会把同组另外四档的草稿留在待发布页，运营以为撤销了、
    # 其实撤了五分之一（发布侧的对称问题，见 `_bundle_sibling_draft_ids`）
    for sid in _bundle_sibling_draft_ids(node_id):
        with connect() as conn:
            n += conn.execute(
                "DELETE FROM kg_node WHERE id = %s AND is_draft", (sid,)
            ).rowcount
            e += conn.execute(
                "DELETE FROM kg_edge WHERE is_draft AND unit_id = %s", (sid,)
            ).rowcount
            conn.commit()

    return {
        "node_id": node_id,
        "nodes_discarded": int(n or 0),
        "edges_discarded": int(e or 0),
    }
