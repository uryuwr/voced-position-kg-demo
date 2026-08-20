"""
逻辑技能写入：一次录入 levels.L1..L5 对象 → 拆成多条 skill_level + requires 边。

权重语义：技能在该岗位技能构成中的占比 → 写在 occupation -requires→ skill 边 weight 上。
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from backend.kg.pg_store.client import connect
from backend.kg.pg_store.config import prefer_draft
from backend.kg.pg_store.query import get_node
from backend.kg.pg_store.skill_aggregate import (
    SKILL_KEY_SQL,
    SKILL_NAME_SQL,
    get_skill_bundle,
    skill_key_from_node,
    skill_name_from_node,
)
from backend.kg.pg_store.skill_taxonomy import to_code
from backend.kg.skill_key import derive_key, is_valid_key
from backend.kg.pg_store.write import (
    archive_edge,
    create_edge,
    create_node,
    patch_node,
)

# 复用已有档位节点时要看得到草稿行（方案 §6.2）
_PD_N = prefer_draft("n")

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


def _strip_level_suffix(s: str) -> str:
    return re.sub(r"\s*·\s*L[1-5]\s*$", "", str(s or "").strip(), flags=re.I).strip()


def resolve_skill_name(payload: dict[str, Any]) -> str:
    """技能**展示名**（给人看的那个）。

    `skill_key` 也进兜底链，是为了兼容旧调用方：2026-08-19 之前 skill_key 存的
    就是中文名，老脚本/老前端只传这一个字段。但只在它**不是 code** 时才当名字用，
    否则新前端传 code 进来会把技能名改成 `SKxxxxxxxxxx`。
    """
    for cand in (payload.get("skill_name"), payload.get("name")):
        v = _strip_level_suffix(cand)
        if v:
            return v
    legacy = str(payload.get("skill_key") or "").strip()
    if legacy and not is_valid_key(legacy):
        v = _strip_level_suffix(legacy)
        if v:
            return v
    raise ValueError("需要技能名（skill_name 或 name）")


def resolve_skill_key(payload: dict[str, Any]) -> str:
    """技能**聚合主键**，只会是 ASCII code。

    传进来已经是 code 就原样用（PATCH 的路径参数走这条）—— **不重算**：
    key 按初始名字生成，之后改名不换 key，否则改个错别字就等于换主键，
    先修关系与测评题库当场断链。没有 code 才按展示名生成一个。
    """
    k = str(payload.get("skill_key") or "").strip()
    if is_valid_key(k):
        return k
    return derive_key(resolve_skill_name(payload))


def existing_skill_name(skill_key: str, region: str = "CN") -> str:
    """库里这个 key 对应的展示名（取第一个非空）。"""
    for n in _find_existing_nodes_by_skill_key(skill_key, region):
        nm = skill_name_from_node(n)
        if nm:
            return nm
    return ""


def _assert_key_free(skill_key: str, skill_name: str, region: str) -> None:
    """同一个 code 不能落到两个不同的技能名上。

    key 是 md5(名字) 前 10 位（≈1.1e12），5911 个 key 的生日碰撞概率约 1.6e-5 ——
    小，但不是零，而碰撞的后果是两个技能被静默合成一个（读路径按 key 聚合）。
    所以生成后显式查一次：撞上就报错让人改名，不覆盖、不合并。
    """
    from backend.kg.skill_key import normalize_name

    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT ({SKILL_NAME_SQL}) AS nm
            FROM kg_node n
            WHERE n.type = 'skill_level'
              AND (%s::text IS NULL OR n.region = %s)
              AND COALESCE(n.status, 'published') <> 'archived'
              AND ({SKILL_KEY_SQL}) = %s
            """,
            (region, region, skill_key),
        ).fetchall()
    mine = normalize_name(skill_name)
    others = [r["nm"] for r in rows if r["nm"] and normalize_name(r["nm"]) != mine]
    if others:
        raise ValueError(
            f"skill_key 冲突：`{skill_key}` 已被技能「{others[0]}」占用。"
            f"这是哈希碰撞（概率极低），请把「{skill_name}」改个名字再存"
        )


def build_level_node_body(
    *,
    skill_key: str,
    skill_name: str,
    level_code: str,
    level_obj: dict[str, Any],
    region: str,
    base: dict[str, Any],
    status: str = "published",
) -> dict[str, Any]:
    li = int(level_code[1])
    label = level_obj.get("label") or _level_labels().get(li, level_code)
    desc = level_obj.get("description") or level_obj.get("desc")
    # 节点名用**展示名**：`skill_key` 现在是 SKxxxxxxxxxx，拿它拼名字的话
    # 库里的 name 会变成「SKabd68031c5 · L3」，而 name 是很多兜底路径的最后一根稻草
    name = f"{skill_name} · {level_code}"
    # 稳定 id：由 **code** 派生而非名字 —— 改名不换 id，重复提交仍能 upsert 到同一行
    sid = f"{skill_key}|{level_code}"
    nid = base.get("id_prefix") or f"CN:skill_level:MANUAL:{uuid.uuid5(uuid.NAMESPACE_URL, sid).hex[:16]}"
    # 若 payload 指定 id 模板
    if base.get("node_ids") and isinstance(base["node_ids"], dict):
        nid = base["node_ids"].get(level_code) or nid

    attrs = dict(base.get("attrs") or {})
    attrs.update(
        {
            "skill_key": skill_key,
            "skill_name": skill_name,
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

    out = {
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
        # **不要写 `"occupation_ids": []`**：那不是「这里不管边」，而是
        # 「把这个技能的岗位关联全清掉」（`apply_node_links` replace 语义）。
        # 岗位关联在下面按 occ_links 单独处理，这里让这个键缺席。
    }
    return out


def _find_existing_nodes_by_skill_key(skill_key: str, region: str = "CN") -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT n.*
            FROM kg_node n
            WHERE n.type = 'skill_level'
              AND (%s::text IS NULL OR n.region = %s)
              AND ({SKILL_KEY_SQL}) = %s
              -- 草稿优先：同一档已经有草稿行时要复用它的 id，
              -- 否则第二次编辑会当成"新档"再建一遍
              AND {_PD_N}
            """,
            (region, region, skill_key),
        ).fetchall()
    from backend.kg.pg_store.query import _node_dict

    # 技能 bundle 的写路径属于管理台，要能看到 version / updated_by
    return [_node_dict(r, admin=True) for r in rows]


def _clear_requires_into_nodes(
    node_ids: list[str], *, user_id: str, user_name: str, to_draft: bool = True
) -> None:
    """清掉指向这些档位节点的 requires 边（下面会按新的岗链重建）。

    草稿模式下不能真删线上边 —— 那会让「重建岗链」的删一半立刻对前台生效。
    改成给每条线上边建墓碑草稿（`target_status='archived'`），
    发布对应岗位时才归档。
    """
    if not node_ids:
        return
    if not to_draft:
        with connect() as conn:
            conn.execute(
                """
                DELETE FROM kg_edge
                WHERE rel_type = 'requires' AND dst_id = ANY(%s)
                  AND NOT is_draft
                """,
                (node_ids,),
            )
            conn.commit()
        return
    from backend.kg.pg_store.write import archive_edge

    with connect() as conn:
        ids = [
            r["id"]
            for r in conn.execute(
                """
                SELECT id FROM kg_edge
                WHERE rel_type = 'requires' AND dst_id = ANY(%s)
                  AND COALESCE(status,'published') <> 'archived'
                  AND COALESCE(target_status,'') <> 'archived'
                """,
                (node_ids,),
            ).fetchall()
        ]
    for eid in ids:
        archive_edge(eid, user_id=user_id, user_name=user_name)


def apply_skill_bundle_create(
    payload: dict[str, Any],
    *,
    user_id: str,
    user_name: str,
    to_draft: bool = True,
) -> dict[str, Any]:
    """拆 levels 入库 + 建 requires 边（权重在边上）。

    `to_draft=True`（默认，管理台直接编辑）：L1–L5 节点与 requires 边全部落草稿行，
    前台在发布前看不到任何变化。**边的发布单元是它的 src（岗位）**，不是这个技能
    （方案 §3 约定 unit_id = src_id），所以新建技能 + 挂岗位要按序发布：
    先发技能节点，再发各岗位 —— 反过来会被「端点未发布」拦下（§7 第 4 步、§10.6）。

    `to_draft=False`：审核队列批准后落地，直接写线上行（方案 §4）。
    """
    skill_key = resolve_skill_key(payload)
    skill_name = resolve_skill_name(payload)
    # 碰撞校验**只在新建时跑**。更新（含改名）时这个 key 本来就是这条记录的身份，
    # 照查的话会看到「该 key 已被另一个名字占用」—— 而那个「另一个名字」正是
    # 改名前的自己，于是任何改名都被 400 拦死。
    if not payload.get("_is_update"):
        _assert_key_free(skill_key, skill_name, (payload.get("region") or "CN").strip() or "CN")
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
    # 已经归档 / 已经标了待归档的档位单独记：下面「全量替换」要跳过它们，
    # 否则每次保存都给同一档再落一条 target_status='archived' 的草稿，
    # 待发布页上永远清不掉。
    exist_dead: set[str] = set()
    for n in existing:
        a = n.get("attrs") if isinstance(n.get("attrs"), dict) else {}
        code = (a.get("level_code") or "").upper()
        if not code:
            m = re.search(r"L([1-5])", n.get("name") or "", re.I)
            code = f"L{m.group(1)}" if m else ""
        if code:
            exist_by_lv[code] = n["id"]
            if "archived" in (
                str(n.get("status") or ""),
                str(n.get("target_status") or ""),
            ):
                exist_dead.add(code)

    for code in _LEVEL_KEYS:
        if code not in levels:
            continue
        body = build_level_node_body(
            skill_key=skill_key,
            skill_name=skill_name,
            level_code=code,
            level_obj=levels[code],
            region=region,
            base={**base, "node_ids": {code: exist_by_lv[code]} if code in exist_by_lv else base.get("node_ids")},
            status=status,
        )
        if code in exist_by_lv:
            body["id"] = exist_by_lv[code]
        node = create_node(
            body, user_id=user_id, user_name=user_name, to_draft=to_draft
        )
        created_nodes.append(node)
        nodes_by_level[code] = node

    # 提交了 levels 就按**全量替换**处理：没出现在这次提交里的档位要归档。
    #
    # 为什么必须这样：管理台的技能编辑是一张**全量表单**，五档各一行输入框，
    # 前端在 `buildBody()` 里 `if (desc)` —— 描述被清空的那档**整个不出现在
    # payload 里**。后端若把「未出现」一律当成「保留原档」，就会出现用户报的现象：
    # 清掉 L5 的描述、保存、再点编辑，L5 还在，而且怎么都删不掉。
    # 「删掉某一档」在这个接口里本来就没有别的表达方式。
    #
    # 归档而不是物理删：与项目其它删除一致（`archived` 是逻辑删除，可恢复），
    # 而且走草稿 —— 移除一档是内容编辑，前台要到发布后才少一档。
    # `levels` 为空（只改岗链的那条路，见 apply_skill_bundle_update）不走这里，
    # 否则「只改技能构成」会把五档全归档。
    removed_levels: list[str] = []
    if levels:
        for code, old_id in sorted(exist_by_lv.items()):
            if code in levels or code in exist_dead:
                continue
            patch_node(
                old_id,
                {"status": "archived"},
                user_id=user_id,
                user_name=user_name,
                to_draft=to_draft,
            )
            # 边跟着走：节点归档而边还 published 的话，`edge_published()` 拦不住
            # 那些边，图查询会画出指向不可见节点的断头箭头，按边计数也会比详情多
            with connect() as conn:
                eids = [
                    r["id"]
                    for r in conn.execute(
                        """
                        SELECT id FROM kg_edge
                        WHERE (src_id = %s OR dst_id = %s) AND NOT is_draft
                          AND COALESCE(status, 'published') <> 'archived'
                        """,
                        (old_id, old_id),
                    ).fetchall()
                ]
            for eid in eids:
                archive_edge(
                    eid, user_id=user_id, user_name=user_name, to_draft=to_draft
                )
            removed_levels.append(code)

    # 边：每个岗位 × 每个已建档；权重仅写在 required_level（或最高档）
    created_edges: list[dict[str, Any]] = []
    node_ids = [n["id"] for n in created_nodes]
    # **省略 occupation_links ≠ 清空**。这里原来无条件先清后建，于是任何一次
    # 「只改技能自身属性」的 PATCH（改描述、改分类、改负责人）都会把这个技能
    # 从所有岗位的技能构成里摘掉 —— 草稿态下表现为一堆 target_status='archived'
    # 的墓碑草稿，运营发布时才发现岗位少了技能、Σweight 也不足 1。
    # 与 `levels` 的语义对齐：字段没出现 = 不动；出现且为空列表 = 清空。
    _links_given = any(
        k in payload and payload[k] is not None
        for k in ("occupation_links", "links", "occupation_ids")
    )
    if _links_given:
        _clear_requires_into_nodes(
            node_ids, user_id=user_id, user_name=user_name, to_draft=to_draft
        )

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
                to_draft=to_draft,
            )
            created_edges.append(edge)

    bundle = get_skill_bundle(skill_key, region=region)
    return {
        "skill_key": skill_key,
        # 顶层也要带：调用方（审核回执、管理台 toast）读的是顶层，
        # 不会去 bundle 里翻
        "skill_name": (bundle or {}).get("skill_name") or skill_name,
        "nodes": created_nodes,
        "node_ids": node_ids,
        "edges": created_edges,
        "levels": list(nodes_by_level.keys()),
        "removed_levels": removed_levels,
        "occupation_links": occ_links,
        "bundle": bundle,
    }


def apply_skill_bundle_update(
    skill_key: str,
    payload: dict[str, Any],
    *,
    user_id: str,
    user_name: str,
    to_draft: bool = True,
) -> dict[str, Any]:
    """更新逻辑技能：可增补/改档文案、重建岗链权重。"""
    skill_key = (skill_key or resolve_skill_key(payload)).strip()
    region = (payload.get("region") or "CN").strip() or "CN"
    # 展示名：请求里带了就用（这是「改名」），没带就沿用库里已有的。
    # **不能回落成 skill_key** —— 那是 SKxxxxxxxxxx，一次不带 name 的保存
    # 就把技能名改成了一串哈希，而且不报错。
    _existing_name = ""
    for _n in _find_existing_nodes_by_skill_key(skill_key, region):
        _existing_name = skill_name_from_node(_n)
        if _existing_name:
            break
    payload = {
        **payload,
        "skill_key": skill_key,
        "name": _strip_level_suffix(payload.get("skill_name") or payload.get("name"))
        or _existing_name
        or skill_key,
    }
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
    return apply_skill_bundle_create(
        {**payload, "_is_update": True},
        user_id=user_id,
        user_name=user_name,
        to_draft=to_draft,
    )


def preview_skill_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    """提交前预览将生成的档位与边（不写库）。"""
    skill_key = resolve_skill_key(payload)
    # 预览面板的标题用它。名字取不到就退回 code（预览不该因为缺展示名而 400，
    # 那是提交时才校验的事，见 prepare_submit_payload）
    try:
        skill_name = resolve_skill_name(payload)
    except ValueError:
        skill_name = (
            existing_skill_name(skill_key, (payload.get("region") or "CN").strip() or "CN")
            or skill_key
        )
    levels = normalize_levels(payload.get("levels"))
    occ_links = normalize_occupation_links(payload)
    return {
        "skill_key": skill_key,
        "skill_name": skill_name,
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
    try:
        skill_name = resolve_skill_name(body)
    except ValueError:
        # PATCH 只带 levels、名字全靠路径参数（code）的调用形态：从库里取现有名字。
        # 改造前 skill_key 就是名字，所以这类请求以前是能过的，不能因为改造把它打成 400。
        skill_name = existing_skill_name(
            skill_key, (body.get("region") or "CN").strip() or "CN"
        )
        if not skill_name:
            raise
    levels = normalize_levels(body.get("levels"))
    if not levels:
        raise ValueError("levels 至少包含一档（L1–L5）")
    occ_links = normalize_occupation_links(body)
    return {
        "kind": "skill_bundle",
        "type": "skill_level",
        "expand_levels": True,
        "skill_key": skill_key,
        "skill_name": skill_name,
        "name": body.get("name") or skill_name,
        "region": body.get("region") or "CN",
        "scale": body.get("scale") or "l1_l5",
        "levels": levels,
        # 与路由层保持一致：请求里没出现这两个键就别塞进来，否则
        # `apply_skill_bundle_create` 判不出「不动技能构成」，会按清空处理
        **(
            {
                "occupation_links": occ_links,
                "occupation_ids": [x["occupation_id"] for x in occ_links],
            }
            if any(
                k in body and body[k] is not None
                for k in ("occupation_links", "links", "occupation_ids")
            )
            else {}
        ),
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
        # 只挑线上行。**不钉 `NOT n.is_draft` 会 500**：草稿行 status 恒为 'draft'，
        # 过不了 `<> 'archived'` 这道筛，于是被选进 node_ids，下面 `SET status='archived'`
        # 写到草稿行上就撞 ck_kg_node_draft_status。也就是「编辑过某技能（留下草稿）
        # 再删它」必然报错，而删除动作其实已经部分执行 —— 与发布 500 同一形状。
        # 名字**必须在归档前取**：回执要带展示名，否则运营看到的是
        # 「已删除 SKabd68031c5」，无从确认删对了没有
        rows = conn.execute(
            f"""
            SELECT n.id, ({SKILL_NAME_SQL}) AS nm FROM kg_node n
            WHERE n.type = 'skill_level'
              AND (%s::text IS NULL OR n.region = %s)
              AND ({SKILL_KEY_SQL}) = %s
              AND COALESCE(n.status, 'published') <> 'archived'
              AND NOT n.is_draft
            """,
            (region, region, skill_key),
        ).fetchall()
        node_ids = [r["id"] for r in rows]
        skill_name = next((r["nm"] for r in rows if r["nm"]), "")

        # 草稿行单独取：可能存在「新建的技能还没发布就想删」，此时没有线上行，
        # 删除 = 丢弃草稿（同 write.archive_node 的处理），不能报「技能不存在」。
        draft_rows = conn.execute(
            f"""
            SELECT n.id, ({SKILL_NAME_SQL}) AS nm FROM kg_node n
            WHERE n.type = 'skill_level'
              AND (%s::text IS NULL OR n.region = %s)
              AND ({SKILL_KEY_SQL}) = %s
              AND n.is_draft
            """,
            (region, region, skill_key),
        ).fetchall()
        draft_ids = [r["id"] for r in draft_rows]
        # 只有草稿行的情况（新建未发布就删），名字只能从草稿行取
        if not skill_name:
            skill_name = next((r["nm"] for r in draft_rows if r["nm"]), "")
        if not node_ids and not draft_ids:
            raise ValueError(f"技能不存在或已删除: {skill_key}")

        occ_cnt = int(
            conn.execute(
                """
                SELECT count(DISTINCT e.src_id) AS c FROM kg_edge e
                WHERE e.rel_type = 'requires' AND e.dst_id = ANY(%s)
                  AND COALESCE(e.status, 'published') = 'published'
                  AND NOT e.is_draft
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
              AND NOT is_draft
            """,
            (node_ids, node_ids),
        ).rowcount
        node_n = conn.execute(
            """
            UPDATE kg_node
               SET status = 'archived', updated_by = %s, updated_by_name = %s
             WHERE id = ANY(%s) AND NOT is_draft
            """,
            (user_id or None, user_name or None, node_ids),
        ).rowcount

        # 未发布的改动一并丢弃。留着的话这条已删技能会继续挂在「待发布」页，
        # 点发布等于把它复活 —— 删除是立即生效的动作，不该留个能撤销它的草稿。
        all_ids = node_ids + draft_ids
        conn.execute(
            "DELETE FROM kg_edge WHERE is_draft AND (unit_id = ANY(%s) "
            "OR src_id = ANY(%s) OR dst_id = ANY(%s))",
            (all_ids, all_ids, all_ids),
        )
        dropped = conn.execute(
            "DELETE FROM kg_node WHERE id = ANY(%s) AND is_draft", (all_ids,)
        ).rowcount
        conn.commit()

    return {
        "deleted": True,
        "skill_key": skill_key,
        "skill_name": skill_name or skill_key,
        "archived_nodes": int(node_n or 0),
        "archived_edges": int(edge_n or 0),
        "discarded_drafts": int(dropped or 0),
        "occupations_affected": occ_cnt,
    }
