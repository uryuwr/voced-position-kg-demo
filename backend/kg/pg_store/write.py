"""KG 写路径（管理台新建 / 编辑 / 归档）—— **默认写草稿行，不动线上行**。

草稿态方案见 `docs/方案-管理台草稿态与发布.md`。一条记录最多两行：
线上行 `is_draft=false`，草稿行 `is_draft=true` 且 `status` 恒为 `'draft'`。
运营的每个编辑动作都落在草稿行上，线上行只由发布（`draft_publish`）改，
所以「编辑期间前台逐字节不变」。

`to_draft=False` 是给**发布侧**留的口子，只有三类调用方该用它（方案 §4）：
`review.py`（审核通过 = 批准发布）、`publish_rules.try_publish_node`、`draft_publish`。
运营路由一律用默认值。加这个参数而不是复制一套函数，是因为两条路径的字段校验
（编码唯一、attrs.level、门禁）必须共用同一份，分两份迟早只改一边。
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal
import uuid
from datetime import datetime, timezone
from typing import Any

from backend.kg.pg_store.client import connect
from backend.kg.pg_store.config import prefer_draft_edge
from backend.kg.pg_store.query import _node_dict, _rel_dict, get_node

logger = logging.getLogger(__name__)

# 复制线上行 → 草稿行。**不逐列点名**：这份 SQL 要在「主表加了新列」之后依然正确，
# 逐列写的话新列会被静默丢掉（影子表方案就是因为这个被否掉，见方案 §1.3）。
# to_jsonb(n) 拿到整行、覆盖几个控制列、再 populate 回 kg_node 的行类型，
# 列顺序与表定义一致，所以 INSERT 不用写列名。
_COPY_NODE_TO_DRAFT = """
INSERT INTO kg_node
SELECT (jsonb_populate_record(
          n,
          to_jsonb(n) || jsonb_build_object(
            'is_draft', true,
            'status', 'draft',
            'target_status', NULL,
            'base_version', n.version
          )
        )).*
FROM kg_node n
WHERE n.id = %s AND NOT n.is_draft
ON CONFLICT (id, is_draft) DO NOTHING
"""

_COPY_EDGE_TO_DRAFT = """
INSERT INTO kg_edge
SELECT (jsonb_populate_record(
          e,
          to_jsonb(e) || jsonb_build_object(
            'is_draft', true,
            'status', 'draft',
            'target_status', %s,
            'unit_id', e.src_id
          )
        )).*
FROM kg_edge e
WHERE e.id = %s AND NOT e.is_draft
ON CONFLICT (id, is_draft) DO NOTHING
"""

# 发布意图：草稿行的 status 恒为 'draft'，「发布后要变成什么」只能存在 target_status 里。
# 写进 status 的那一刻草稿就被前台的 `status='published'` 查询命中（方案 §0.2）。
#
# **停用 / 启用 / 删除已改成立即生效，不再进草稿**（2026-08-19 需求收窄），
# 所以 target_status 只剩两个用途，别再往里塞第三种意图：
#
#   1. **新建**：只有草稿行的记录，发布时要知道该落成 published 还是 disabled
#      （请求里带 `status` 时记在这里；NULL = 发布成 published）
#   2. **边的墓碑**：技能构成/关联里「移除一项」不能真删线上边（那会立刻对前台生效），
#      落一条 `target_status='archived'` 的草稿边，发布时才归档
_TARGET_STATUSES = ("published", "disabled", "archived")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _target_status_of(status: Any) -> str | None:
    """请求里的 status → 草稿行的 target_status（NULL 表示发布时不改状态）。"""
    st = str(status or "").strip().lower()
    return st if st in _TARGET_STATUSES else None


def resolve_owner_name(
    owner: str | None,
    owner_name: str | None,
    *,
    caller_id: str = "",
    caller_name: str = "",
) -> str | None:
    """负责人姓名：前端没传就由服务端解析。

    前端只需要传 `owner`（UC user_id），姓名这里补。三级，越靠前越可信：

    1. **前端传了就用它** —— 前端手里有 UC 的人员选择器，它给的名字最新。
    2. `owner` 就是当前登录者 → 用 token 里的姓名。这个名字本身来自 UC
       （`uc/client.py` 校验 token 时带回来的），所以不算「猜」，也不用多发请求。
       实际场景里「把负责人设成我自己」占绝大多数，这一级就够了。
    3. 查库里这个 user_id 已经留下过的姓名 —— 每次写入都会记
       `updated_by`/`updated_by_name` 与 `owner`/`owner_name` 两对，等于一份免费
       的用户名缓存。取最近一条。

    都没有就返回 None（列留空），读路径回落显示 user_id。**不编 UC 的接口**：
    本服务的 `uc/client.py` 只有「校验 MAC token」一个能力，没有「按 user_id 反查
    姓名」的接口，`UC_API_HOST` 在默认配置里还是占位符。凭空拼一个 URL 会变成
    干净镜像里静默走降级分支的那类问题 —— 真要支持任意用户，得先有那个接口。
    """
    if owner_name is not None and str(owner_name).strip():
        return str(owner_name).strip()
    oid = str(owner or "").strip()
    if not oid:
        return None
    if oid == str(caller_id or "").strip() and str(caller_name or "").strip():
        return str(caller_name).strip()
    try:
        with connect() as conn:
            row = conn.execute(
                """
                SELECT name FROM (
                  SELECT owner_name AS name, created_at FROM kg_node
                   WHERE owner = %s AND COALESCE(owner_name, '') <> ''
                  UNION ALL
                  SELECT updated_by_name AS name, created_at FROM kg_node
                   WHERE updated_by = %s AND COALESCE(updated_by_name, '') <> ''
                ) t
                ORDER BY created_at DESC NULLS LAST
                LIMIT 1
                """,
                (oid, oid),
            ).fetchone()
        return str(row["name"]).strip() if row and row.get("name") else None
    except Exception:            # 解析姓名失败不该让写入失败
        logger.warning("负责人姓名解析失败，owner=%s，姓名留空", oid, exc_info=True)
        return None


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

    **只返回请求里真的出现过的键。** 这个区别是有语义的，`replace=True` 下：
    键缺席 = 这次不管这类关联；键在但是空列表 = 把这类关联清空。

    原来无条件返回三个键（缺的填 `[]`），于是「只改节点自身字段」的保存
    也会被当成「关联清空」，给每条已有关联建一条 `target_status='archived'`
    的墓碑草稿。技能那条路最明显：改一次描述/负责人，这个技能就从所有岗位的
    技能构成里消失了，而且要到发布时才看得出来。
    """
    links = data.get("links") or data.get("link") or {}
    if not isinstance(links, dict):
        links = {}
    out: dict[str, list[str]] = {}
    for key in ("industry_ids", "major_ids", "occupation_ids"):
        if data.get(key) is not None:
            out[key] = _normalize_id_list(data[key])
        elif links.get(key) is not None:
            out[key] = _normalize_id_list(links[key])
    return out


def apply_node_links(
    node_id: str,
    node_type: str,
    link_ids: dict[str, list[str]],
    *,
    user_id: str,
    user_name: str,
    replace: bool = True,
    to_draft: bool = True,
) -> list[dict[str, Any]]:
    """
    按节点类型自动建边（系统填默认字段）。
    replace=True 时把本节点该 rel 的关联**改成**给定这批（多的删、少的加）。

    草稿态下不能再「先 DELETE 全部再重建」：DELETE 会打掉线上行，前台当场少一批关联；
    重建出来的又是草稿行，于是「删了立刻生效、加了要等发布」，一半生效一半不生效。
    所以改成算差集 —— 该加的建草稿边，该删的建一条 `target_status='archived'`
    的草稿边（墓碑），两边都等发布时才落到线上行。
    已经对上的关联不动，免得每次保存都刷出一堆无意义的草稿边。
    """
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
    # 只给**请求里出现过的**那类关联排计划。键缺席就不进 plans ——
    # 否则 replace=True 会拿一个空列表去和现存关联算差集，把它们全标成待归档
    # （见 `extract_link_ids` 的 docstring）。
    if ntype == "major":
        # major -belongs_to→ industry
        if "industry_ids" in link_ids:
            plans.append(("industry", "belongs_to", link_ids["industry_ids"], True))
        # major -prepares_for→ occupation：**专业侧也要能挂岗位**。
        # 原来只有岗位侧的 `major_ids`，专业编辑传 `occupation_ids` 会走到
        # extract_link_ids 拿到值、这里没有对应 plan，于是 HTTP 200、零条边 ——
        # 同一条 `prepares_for` 边，从哪一端维护都该成立
        if "occupation_ids" in link_ids:
            plans.append(("occupation", "prepares_for", link_ids["occupation_ids"], True))
    elif ntype == "occupation":
        # major -prepares_for→ occupation  （对方是 src）
        if "major_ids" in link_ids:
            plans.append(("major", "prepares_for", link_ids["major_ids"], False))
    elif ntype == "skill_level":
        # occupation -requires→ skill
        if "occupation_ids" in link_ids:
            plans.append(("occupation", "requires", link_ids["occupation_ids"], False))
    elif ntype == "industry":
        # 行业暂不强制反向挂边
        plans = []

    for peer_type, rel, ids, self_is_src in plans:
        if not to_draft:
            # 发布 / 审核落地侧：保持原来的「清空重建」，写线上行
            if replace:
                with connect() as conn:
                    col = "src_id" if self_is_src else "dst_id"
                    conn.execute(
                        f"DELETE FROM kg_edge WHERE {col}=%s AND rel_type=%s "
                        f"AND NOT is_draft",
                        (node_id, rel),
                    )
                    conn.commit()
            for peer in ids:
                _ensure_exists(peer, peer_type)
                src, dst = (node_id, peer) if self_is_src else (peer, node_id)
                created.append(
                    create_edge(
                        {**base, "src_id": src, "dst_id": dst, "rel_type": rel},
                        user_id=user_id,
                        user_name=user_name,
                        to_draft=False,
                    )
                )
            continue

        current = _current_link_edges(node_id, rel, self_is_src=self_is_src)
        want = list(dict.fromkeys(ids))
        for peer in want:
            if peer in current:
                continue          # 已经关联着，不必再造一条草稿边
            _ensure_exists(peer, peer_type)
            src, dst = (node_id, peer) if self_is_src else (peer, node_id)
            created.append(
                create_edge(
                    {**base, "src_id": src, "dst_id": dst, "rel_type": rel},
                    user_id=user_id,
                    user_name=user_name,
                )
            )
        if replace:
            for peer, edge_id in current.items():
                if peer not in want:
                    archive_edge(edge_id, user_id=user_id, user_name=user_name)
    return created


def _current_link_edges(
    node_id: str, rel: str, *, self_is_src: bool
) -> dict[str, str]:
    """本节点该 rel 下**当前有效**的关联：{对方 id: 边 id}。

    「有效」= 草稿优先（运营刚加的算数）+ 墓碑算已删 + 排除 archived。
    """
    self_col = "src_id" if self_is_src else "dst_id"
    peer_col = "dst_id" if self_is_src else "src_id"
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT e.{peer_col} AS peer, e.id AS edge_id
            FROM kg_edge e
            WHERE e.{self_col} = %s AND e.rel_type = %s
              AND COALESCE(e.status, 'published') <> 'archived'
              AND COALESCE(e.target_status, '') <> 'archived'
              AND {prefer_draft_edge('e')}
            """,
            (node_id, rel),
        ).fetchall()
    return {r["peer"]: r["edge_id"] for r in rows}


class CodeConflictError(ValueError):
    """业务编码 attrs.code 在同 region+type 内重复。"""

    def __init__(self, code: str, node_type: str, region: str, existing: dict[str, Any]):
        self.code = code
        self.node_type = node_type
        self.region = region
        self.existing = existing
        super().__init__(
            f"编码 {code} 已被占用：{region}/{node_type} 下已存在"
            f"「{existing.get('name')}」（id={existing.get('id')}）"
        )


def find_by_code(
    code: str,
    node_type: str,
    region: str = "CN",
    *,
    exclude_id: str | None = None,
    conn: Any = None,
) -> dict[str, Any] | None:
    """按 (region, type, attrs.code) 查占用者；归档节点不算占用。

    写入前校验用：这样冲突能返回可读的 409，而不是等数据库唯一索引抛
    IntegrityError（那种报错前端无法解释给运营看）。

    **只看线上行**：草稿之间不互斥（唯一索引也排除了草稿），否则「A 改成 X」的草稿
    会挡住「B 也想改成 X」这种其实还没成立的冲突。真正的互斥在发布事务里再查一次
    （方案 §7 第 3 步）——那时候它才真的要占这个编码。
    """
    code = (code or "").strip()
    if not code:
        return None
    sql = """
        SELECT id, name, status FROM kg_node
        WHERE region = %s AND type = %s
          AND attrs::json->>'code' = %s
          AND COALESCE(status, 'published') <> 'archived'
          AND NOT is_draft
    """
    params: list[Any] = [region or "CN", node_type, code]
    if exclude_id:
        sql += " AND id <> %s"
        params.append(exclude_id)
    sql += " LIMIT 1"
    if conn is not None:
        row = conn.execute(sql, params).fetchone()
    else:
        with connect() as c:
            row = c.execute(sql, params).fetchone()
    return dict(row) if row else None


def _assert_code_free(
    attrs: Any,
    node_type: str,
    region: str,
    *,
    exclude_id: str | None = None,
    conn: Any = None,
) -> None:
    if not isinstance(attrs, dict):
        return
    code = str(attrs.get("code") or "").strip()
    if not code:
        return
    hit = find_by_code(
        code, node_type, region or "CN", exclude_id=exclude_id, conn=conn
    )
    if hit:
        raise CodeConflictError(code, node_type, region or "CN", hit)


def _assert_attrs_sane(attrs: Any, node_type: str) -> None:
    """技能节点的 `attrs.level` 必须是 1–5 的整数。

    NodeCreate.attrs 是自由 `dict[str, Any]`，Pydantic 不管里面的值；而 attrs.level
    是产品档的**唯一真源**，读路径要按它排序、聚合、渲染档位格。写进一个 "L3"
    就会让技能库列表整页 500（读侧已在 config.attrs_level_int 兜底，但脏值仍会
    让该技能的档位凭空消失）。所以写侧拒绝，让运营当场看到 400 而不是事后查页面白屏。
    """
    if not isinstance(attrs, dict) or "level" not in attrs:
        return
    if str(node_type or "").lower() not in ("skill_level", "skill", "skill_bundle"):
        return
    lv = attrs.get("level")
    if lv is None or (isinstance(lv, str) and not lv.strip()):
        return
    # 布尔是 int 的子类，True 会被当成 1 混进来，单独挡掉
    # 只收 int 与纯数字串。**浮点一律拒**（含 `3.0`）——档位本来就没有浮点场景，
    # 入库口要把它挡在外面，而不是替调用方纠正：纠正等于默许上游继续送浮点，
    # 库里迟早又冒出 `3.0`。读侧另有容错（`config.as_level` / `attrs_level_int`
    # 把整数值浮点收成整数），那是给绕过应用层的来路兜底 —— psycopg 把 numeric
    # 读成 Decimal、历史数据、直连改库 —— 两侧宽严不同是有意的。
    ok = isinstance(lv, int) and not isinstance(lv, bool)
    if not ok and isinstance(lv, str) and lv.strip().isdigit():
        lv, ok = int(lv.strip()), True
    if not ok or not 1 <= int(lv) <= 5:
        raise ValueError(
            f"attrs.level 必须是 1–5 的整数（产品档 1 了解 → 5 专家），收到：{attrs.get('level')!r}"
        )
    attrs["level"] = int(lv)   # 顺手把 "3" 归一成 3，库里只存一种形态


def create_node(
    data: dict[str, Any],
    *,
    user_id: str,
    user_name: str,
    to_draft: bool = True,
) -> dict[str, Any]:
    link_ids = extract_link_ids(data)
    body = _strip_link_fields(data)
    nid = (body.get("id") or "").strip() or f"CN:manual:{body['type']}:{uuid.uuid4().hex[:12]}"
    status = body.get("status") or "draft"
    # 写入前校验业务编码唯一性（同 region+type 内），冲突直接抛给上层转 409
    _assert_code_free(
        body.get("attrs"), body["type"], body.get("region") or "CN", exclude_id=nid
    )
    _assert_attrs_sane(body.get("attrs"), body["type"])
    target_status: str | None = None
    if to_draft:
        # 新建 = 只有草稿行、没有线上行：前台查 status='published' 查不到它，天然不可见；
        # 发布时这一行原地 is_draft=false 转正，不需要复制（方案 §5）。
        # 请求里的 status 是「发布后想变成什么」，存进 target_status，**不能写进 status**。
        target_status = _target_status_of(status)
        status = "draft"
    elif str(status).lower() == "published":
        # 直写 published 须过 BR 门禁；无边时门禁必败，先落 draft 由外层 promote
        ntype = (body.get("type") or "").lower()
        if ntype in ("major", "occupation", "skill_level", "skill", "skill_bundle"):
            status = "draft"
            body["status"] = "draft"
    _owner_in = body.get("owner")
    if _owner_in is None:
        _owner_val: str | None = None
        _owner_name_val: str | None = None
    else:
        _owner_val = str(_owner_in).strip()
        _owner_name_val = (
            ""                      # 清空负责人时姓名一起清，否则列表还挂着前任的名字
            if not _owner_val
            else resolve_owner_name(
                _owner_val,
                body.get("owner_name"),
                caller_id=user_id,
                caller_name=user_name,
            )
        )
    row = {
        "id": nid,
        "is_draft": to_draft,
        "target_status": target_status,
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
        # 负责人（原型「负责人」列）。三种语义分清，全靠 NULL 与空串的区别：
        #   不传（None）  = 本次不改；这一行是新建的话回落到创建人
        #   传了 user_id  = 改成这个人，姓名没给就 `resolve_owner_name` 解析
        #   传空串        = 显式清空
        # 不能像原来那样写 `body.get("owner") or user_id` —— 那样「不传」和
        # 「传空」都变成「改成当前操作人」，运营改一次描述就把别人的负责人抢走了。
        "owner": _owner_val,
        "owner_name": _owner_name_val,
        "owner_default": user_id,
        "owner_name_default": user_name,
        # 技能大类。这一列以前不在 INSERT 里 —— 上层把 category 放进 body 也白搭，
        # 静默丢掉，新建的技能一律「待归类」。
        "category": body.get("category"),
    }
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO kg_node (
              id, region, type, name, name_en, name_zh, aliases, description, attrs,
              source_system, source_id, source_url, license, fetched_at, confidence,
              status, updated_by, updated_by_name, owner, owner_name, category,
              is_draft, target_status
            ) VALUES (
              %(id)s, %(region)s, %(type)s, %(name)s, %(name_en)s, %(name_zh)s, %(aliases)s,
              %(description)s, %(attrs)s, %(source_system)s, %(source_id)s, %(source_url)s,
              %(license)s, %(fetched_at)s, %(confidence)s, %(status)s, %(updated_by)s,
              %(updated_by_name)s,
              COALESCE(%(owner)s, %(owner_default)s),
              COALESCE(%(owner_name)s, %(owner_name_default)s),
              %(category)s,
              %(is_draft)s, %(target_status)s
            )
            ON CONFLICT (id, is_draft) DO UPDATE SET
              name = EXCLUDED.name,
              name_en = EXCLUDED.name_en,
              name_zh = EXCLUDED.name_zh,
              aliases = EXCLUDED.aliases,
              description = EXCLUDED.description,
              attrs = EXCLUDED.attrs,
              source_url = EXCLUDED.source_url,
              confidence = EXCLUDED.confidence,
              status = EXCLUDED.status,
              target_status = EXCLUDED.target_status,
              updated_by = EXCLUDED.updated_by,
              updated_by_name = EXCLUDED.updated_by_name,
              -- 只在传了值时覆盖：重复提交/其它 type 的节点不带 category，
              -- 无条件赋值会把已有分类抹成 NULL
              category = COALESCE(EXCLUDED.category, kg_node.category),
              -- 负责人同理，但**不能用 EXCLUDED** —— EXCLUDED.owner 已经被上面的
              -- COALESCE 兜成了创建人，拿它判「有没有传」永远是「传了」，于是每次
              -- 编辑都把负责人改成操作人。这里直接引原始参数（NULL = 没传）。
              owner = COALESCE(%(owner)s, kg_node.owner),
              owner_name = COALESCE(%(owner_name)s, kg_node.owner_name)
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
        to_draft=to_draft,
    )
    node = get_node(nid, scope="any")  # 刚写完读回，可能还是 draft/archived
    assert node is not None
    node = dict(node)
    node["linked_edges"] = edges
    node["link_ids"] = link_ids
    return node


def ensure_node_draft(node_id: str, conn: Any = None) -> dict[str, Any] | None:
    """保证 node_id 有草稿行（copy-on-write），返回草稿行；记录不存在返回 None。

    第一次编辑时把线上行整行复制一份、`base_version` 记住当时的 `version` ——
    发布时拿它和线上行的 version 比，不等就说明「你编辑期间别人发布过」，回 409
    而不是静默覆盖（方案 §7 第 2 步）。
    """
    own = conn is None
    c = conn or connect()
    try:
        c.execute(_COPY_NODE_TO_DRAFT, (node_id,))
        row = c.execute(
            "SELECT * FROM kg_node WHERE id = %s AND is_draft", (node_id,)
        ).fetchone()
        if own:
            c.commit()
    finally:
        if own:
            c.close()
    return dict(row) if row else None


def cascade_edge_status_now(
    node_id: str,
    status: str,
    *,
    user_id: str,
    user_name: str,
) -> list[str]:
    """节点停用/归档/启用时，**立即**把两端连着它的边改成同样的状态，返回被改的边 id。

    修的是一个既有 bug：停用技能节点时没人管指向它的边，于是库里出现
    「`published` 的 requires 边指向 `disabled` 的技能节点」。后果是同一个岗位
    前台按节点状态过滤 → 5 项 Σw=0.81，管理台口径 `<> archived` → 6 项 Σw=1.00，
    运营看到的权重和与学员看到的不是一回事，而且两边都不报错。
    CLAUDE.md 记的是「只过滤节点挡不住边」，这里是它的反面：边过了、节点被停用了。

    停用/删除是立即生效的动作（不进草稿），所以级联也立即写线上行 ——
    只改节点不改边，等于把那个 bug 又造一遍。**草稿边不动**：那是别人还没发布的改动。
    """
    if status == "published":
        # **启用要能撤销停用的级联**，否则停用之后再启用就成了死路：
        # 边还是 disabled → 岗位没有已发布的 requires 边 → BR-03 判「无有效权重」→ 启用 400。
        # 只捞 disabled 的：archived 是运营明确移除掉的关联，不该被「启用节点」顺手复活。
        where, params = (
            "COALESCE(status, 'published') = 'disabled'",
            (status, user_id, user_name, node_id, node_id),
        )
    elif status in ("archived", "disabled"):
        where, params = (
            "COALESCE(status, 'published') NOT IN ('archived', %s)",
            (status, user_id, user_name, node_id, node_id, status),
        )
    else:
        return []
    with connect() as conn:
        rows = conn.execute(
            f"""
            UPDATE kg_edge SET status = %s, updated_by = %s, updated_by_name = %s
            WHERE (src_id = %s OR dst_id = %s)
              AND NOT is_draft
              AND {where}
            RETURNING id
            """,
            params,
        ).fetchall()
        conn.commit()
        # 返回 id 而不是条数：启用时要先恢复边再跑门禁，门禁不过得**精确**回滚这批边
        return [r["id"] for r in rows]


def ensure_edge_draft(
    edge_id: str, conn: Any = None, *, target_status: str | None = "published"
) -> dict[str, Any] | None:
    """保证边有草稿行（copy-on-write），返回草稿行；边不存在返回 None。

    改一条已发布的边（典型：技能构成里调权重）走这里：线上那条一个字节都不动，
    改动落在草稿行上，发布时才替换。
    """
    own = conn is None
    c = conn or connect()
    try:
        c.execute(_COPY_EDGE_TO_DRAFT, (target_status, edge_id))
        row = c.execute(
            "SELECT * FROM kg_edge WHERE id = %s AND is_draft", (edge_id,)
        ).fetchone()
        if own:
            c.commit()
    finally:
        if own:
            c.close()
    return dict(row) if row else None


def patch_edge_draft(
    edge_id: str,
    fields: dict[str, Any],
    *,
    user_id: str,
    user_name: str,
) -> dict[str, Any] | None:
    """改一条边的字段（weight / evidence / attrs …），**落在草稿行上**。

    只有 `weight` / `evidence` / `attrs` / `confidence` / `source_url` 可改；
    改端点等于换一条边，走 create + archive。
    """
    sets, params = [], {"id": edge_id}
    for key in ("weight", "evidence", "confidence", "source_url"):
        if key in fields:
            sets.append(f"{key} = %({key})s")
            params[key] = fields[key]
    if "attrs" in fields:
        sets.append("attrs = %(attrs)s")
        params["attrs"] = _json_or_none(fields["attrs"])
    with connect() as conn:
        draft = ensure_edge_draft(edge_id, conn)
        if draft is None:
            return None
        if sets:
            sets.append("updated_by = %(updated_by)s")
            sets.append("updated_by_name = %(updated_by_name)s")
            params["updated_by"] = user_id
            params["updated_by_name"] = user_name
            conn.execute(
                f"UPDATE kg_edge SET {', '.join(sets)} WHERE id = %(id)s AND is_draft",
                params,
            )
        row = conn.execute(
            "SELECT * FROM kg_edge WHERE id = %s AND is_draft", (edge_id,)
        ).fetchone()
        conn.commit()
    return _rel_dict(row) if row else None


def patch_node(
    node_id: str,
    data: dict[str, Any],
    *,
    user_id: str,
    user_name: str,
    to_draft: bool = True,
) -> dict[str, Any] | None:
    link_ids = extract_link_ids(data)
    # **判「键在不在」，不能判「值空不空」**。原来写的是 `any(link_ids.values())`，
    # 而 `any([[]])` 是 False —— 于是「只传一个空数组」这种请求被当成压根没传关联，
    # `apply_node_links` 根本不会被调用，**清空关联这个动作永远做不到**（HTTP 200、
    # 关联还在）。`extract_link_ids` 只返回请求里真出现过的键，语义就写在它的
    # docstring 里：键缺席=不动，键在但空列表=清空。这里必须与它一致。
    has_links = bool(link_ids)
    body = _strip_link_fields(data)
    if to_draft:
        return _patch_node_draft(
            node_id,
            body,
            link_ids,
            has_links=has_links,
            user_id=user_id,
            user_name=user_name,
        )
    # 直写 status=published → BR-08 门禁
    if str(body.get("status") or "").lower() == "published":
        from backend.kg.pg_store.publish_rules import (
            PublishGateError,
            assert_publish_allowed,
        )
        from backend.kg.pg_store.query import get_node as _get
        from backend.kg.pg_store.skill_aggregate import skill_key_from_node

        # 取**线上行**：默认口径是 prefer_draft，会拿到草稿行，
        # 而这里要用它的 type / region / skill_key 去跑门禁 —— 草稿改过名字的话
        # skill_key 就变了，门禁会校到另一个技能上去
        cur = _get(node_id, scope="online")
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
        "owner",
        "owner_name",
    ):
        if key in body and body[key] is not None:
            fields.append(f"{key} = %({key})s")
            params[key] = body[key]
    # 只改了 owner、没给 owner_name：解析一次，否则名字会停在上一任负责人身上 ——
    # 列表「负责人」列显示的是 owner_name，看起来就像没改成功。
    if "owner" in body and body["owner"] is not None and not body.get("owner_name"):
        nm = resolve_owner_name(
            body["owner"], None, caller_id=user_id, caller_name=user_name
        )
        if nm:
            fields.append("owner_name = %(owner_name)s")
            params["owner_name"] = nm
    if "aliases" in body:
        fields.append("aliases = %(aliases)s")
        params["aliases"] = _json_or_none(body["aliases"])
    if "attrs" in body:
        # 改 code 也要过唯一性校验：排除自身，避免「保存自己」被误判冲突
        from backend.kg.pg_store.query import get_node as _get_node

        _cur = _get_node(node_id, scope="online") or {}
        _assert_code_free(
            body["attrs"],
            _cur.get("type") or body.get("type") or "",
            body.get("region") or _cur.get("region") or "CN",
            exclude_id=node_id,
        )
        _assert_attrs_sane(body["attrs"], _cur.get("type") or body.get("type") or "")
        fields.append("attrs = %(attrs)s")
        params["attrs"] = _json_or_none(body["attrs"])
    if fields:
        # 发布即发版：status 改为 published 时 version+1（原型「版本 V3」）
        if str(body.get("status") or "").lower() == "published":
            fields.append("version = COALESCE(version, 1) + 1")
        fields.append("updated_by = %(updated_by)s")
        fields.append("updated_by_name = %(updated_by_name)s")
        # **必须钉住 `NOT is_draft`**：主键是 (id, is_draft)，同一 id 有两行，
        # 裸 `WHERE id=%s` 会把草稿行一起改。给草稿行写 status='published' 直接撞
        # ck_kg_node_draft_status（实测 500）；就算没有那道 CHECK，也等于把草稿
        # 悄悄发出去了 —— 这条路径是发布侧，只该动线上行。
        # rowcount 因此恒为 0 或 1，`== 0` 才是「这条记录没有线上行」的准确判据。
        sql = f"UPDATE kg_node SET {', '.join(fields)} WHERE id = %(id)s AND NOT is_draft"
        with connect() as conn:
            cur = conn.execute(sql, params)
            if cur.rowcount == 0:
                return None
            conn.commit()
    elif not has_links:
        return get_node(node_id, scope="online")
    else:
        # 仅改关联
        if not get_node(node_id, scope="online"):
            return None

    # 发布侧读回**线上行**（scope=any 会因为 prefer_draft 拿到草稿行，
    # 于是「发布成功」的响应里显示的是还没发布的草稿内容）。
    # scope=online 不带状态过滤，所以归档后的那一行也照样返回得到。
    node = get_node(node_id, scope="online")
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
            to_draft=False,
        )
    out = dict(node)
    if has_links:
        out["linked_edges"] = edges
        out["link_ids"] = link_ids
    return out


def _patch_node_draft(
    node_id: str,
    body: dict[str, Any],
    link_ids: dict[str, list[str]],
    *,
    has_links: bool,
    user_id: str,
    user_name: str,
) -> dict[str, Any] | None:
    """运营编辑：字段全落在草稿行上，线上行一个字节都不动。

    请求里的 `status` 在这里是**发布意图**，写进 `target_status`；草稿行自己的 status
    恒为 'draft'（方案 §0.2）。传 `status=draft` 表示撤销意图（发布时只更新内容、不改状态）。
    所以「归档」= 一条 `target_status='archived'` 的草稿，前台在发布前照常展示旧内容。
    """
    with connect() as conn:
        draft = ensure_node_draft(node_id, conn)
        if draft is None:
            return None                     # 线上行与草稿行都没有 → 404

        fields: list[str] = []
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
            "region",
            "owner",
            "owner_name",
        ):
            if key in body and body[key] is not None:
                fields.append(f"{key} = %({key})s")
                params[key] = body[key]
        if body.get("status") is not None:
            fields.append("target_status = %(target_status)s")
            params["target_status"] = _target_status_of(body["status"])
        if "aliases" in body:
            fields.append("aliases = %(aliases)s")
            params["aliases"] = _json_or_none(body["aliases"])
        if "attrs" in body:
            ntype = draft.get("type") or body.get("type") or ""
            region = body.get("region") or draft.get("region") or "CN"
            # 编码唯一性仍在编辑期就查（只查线上行），让运营当场看到 409；
            # 草稿之间不互斥，发布时再查一次
            _assert_code_free(
                body["attrs"], ntype, region, exclude_id=node_id, conn=conn
            )
            _assert_attrs_sane(body["attrs"], ntype)
            fields.append("attrs = %(attrs)s")
            params["attrs"] = _json_or_none(body["attrs"])

        if fields:
            fields.append("updated_by = %(updated_by)s")
            fields.append("updated_by_name = %(updated_by_name)s")
            conn.execute(
                f"UPDATE kg_node SET {', '.join(fields)} "
                f"WHERE id = %(id)s AND is_draft",
                params,
            )
        conn.commit()

    node = get_node(node_id, scope="any")   # prefer_draft → 读回刚写的草稿行
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
    to_draft: bool = True,
) -> dict[str, Any]:
    src, dst, rel = data["src_id"], data["dst_id"], data["rel_type"]
    eid = (data.get("id") or "").strip() or f"edge:{src}|{rel}|{dst}"
    status = data.get("status") or "draft"
    target_status: str | None = None
    if to_draft:
        # 边的默认意图是「发布后就该生效」——运营在编辑页加一条关联，指望的是发布后它在图上
        target_status = _target_status_of(status) or "published"
        status = "draft"
    row = {
        "id": eid,
        "is_draft": to_draft,
        "target_status": target_status,
        # 发布单元约定 unit_id = src_id：改岗位 A 的技能构成 = 改若干 A→skill 边，
        # 发布 A 时这些边要一起生效（方案 §3）
        "unit_id": src if to_draft else None,
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
        # 端点必须存在。这里**不限 is_draft**：新建节点只有草稿行，
        # 「建节点顺手挂边」是常规操作，限死线上行会让新建节点没法连边。
        # 「发布时两端必须都有线上行」由发布事务把关（方案 §7 第 4 步）。
        for kid in (src, dst):
            if not conn.execute("SELECT 1 FROM kg_node WHERE id = %s", (kid,)).fetchone():
                raise ValueError(f"node not found: {kid}")
        conn.execute(
            """
            INSERT INTO kg_edge (
              id, src_id, dst_id, rel_type, region, weight, evidence, attrs,
              source_system, source_id, source_url, license, fetched_at, confidence,
              status, updated_by, updated_by_name, is_draft, target_status, unit_id
            ) VALUES (
              %(id)s, %(src_id)s, %(dst_id)s, %(rel_type)s, %(region)s, %(weight)s,
              %(evidence)s, %(attrs)s, %(source_system)s, %(source_id)s, %(source_url)s,
              %(license)s, %(fetched_at)s, %(confidence)s, %(status)s,
              %(updated_by)s, %(updated_by_name)s, %(is_draft)s, %(target_status)s,
              %(unit_id)s
            )
            ON CONFLICT (id, is_draft) DO UPDATE SET
              weight = EXCLUDED.weight,
              evidence = EXCLUDED.evidence,
              attrs = EXCLUDED.attrs,
              confidence = EXCLUDED.confidence,
              status = EXCLUDED.status,
              target_status = EXCLUDED.target_status,
              unit_id = EXCLUDED.unit_id,
              updated_by = EXCLUDED.updated_by,
              updated_by_name = EXCLUDED.updated_by_name
            """,
            row,
        )
        conn.commit()
        er = conn.execute(
            "SELECT * FROM kg_edge WHERE id = %s AND is_draft = %s", (eid, to_draft)
        ).fetchone()
    assert er is not None
    return _rel_dict(er)


def archive_node(
    node_id: str, *, user_id: str, user_name: str
) -> dict[str, Any] | None:
    """归档节点（软删）—— **立即生效，不进草稿**。

    2026-08-19 需求收窄：停用 / 启用 / 删除三个动作点了就生效，草稿只管
    「内容长什么样」（节点属性、边、技能构成、技能自身属性）。
    此前实现过「归档落草稿、发布才生效」，已按新范围撤销。
    """
    node = set_node_status_now(
        node_id, "archived", user_id=user_id, user_name=user_name
    )
    if node is not None:
        # 线上行归档了，草稿行还得清掉。否则这条已删记录继续挂在「待发布」页，
        # 点一下发布就把 status 写回 published —— 等于草稿能撤销一个立即生效的删除。
        with connect() as conn:
            conn.execute(
                "DELETE FROM kg_edge WHERE is_draft AND (unit_id = %s OR src_id = %s "
                "OR dst_id = %s)",
                (node_id, node_id, node_id),
            )
            n = conn.execute(
                "DELETE FROM kg_node WHERE id = %s AND is_draft", (node_id,)
            ).rowcount
            conn.commit()
        if n:
            node["discarded_draft"] = True
        return node
    # 没有线上行 = 这条记录从没发布过（只有草稿行）。**这时删除就是丢弃草稿** ——
    # 不这么处理的话「新建一条、还没发布、想删掉」会 404，运营删不掉自己刚建的东西。
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM kg_node WHERE id = %s AND is_draft", (node_id,)
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "DELETE FROM kg_edge WHERE is_draft AND (unit_id = %s OR src_id = %s "
            "OR dst_id = %s)",
            (node_id, node_id, node_id),
        )
        conn.execute("DELETE FROM kg_node WHERE id = %s AND is_draft", (node_id,))
        conn.commit()
    out = _node_dict(dict(row), admin=True)
    out["status"] = "archived"          # 对调用方而言这条记录已经没了
    out["discarded_draft"] = True
    return out


def set_node_status_now(
    node_id: str,
    status: str,
    *,
    user_id: str,
    user_name: str,
) -> dict[str, Any] | None:
    """停用 / 启用 / 归档：**直接改线上行**，并级联它两端的边。

    级联必须一起做，而且也必须立即：只改节点不改边就会留下
    「published 的边指向 disabled 的节点」，前台按节点过滤看不到、管理台看得到，
    同一个岗位两个权重和（见 `cascade_edge_status_now`）。
    """
    node = patch_node(
        node_id,
        {"status": status},
        user_id=user_id,
        user_name=user_name,
        to_draft=False,
    )
    if node and status in ("archived", "disabled", "published"):
        cascaded = cascade_edge_status_now(
            node_id, status, user_id=user_id, user_name=user_name
        )
        if cascaded:
            node = dict(node)
            node["cascaded_edges"] = len(cascaded)
    return node


def archive_edge(
    edge_id: str, *, user_id: str, user_name: str, to_draft: bool = True
) -> bool:
    """归档一条边。草稿态下写的是**墓碑草稿行**，线上那条边发布后才归档。

    三种情形：
    - 已有草稿行 → 把它标成待归档（`target_status='archived'`）
    - 只有草稿行、没有线上行（新建后还没发布的边）→ 直接删草稿行，等于「撤销新建」，
      不留墓碑：一条从未发布的边没有「归档」可言，留着只会在待发布清单里当噪音
    - 只有线上行 → 复制成草稿行并标待归档
    """
    if not to_draft:
        with connect() as conn:
            cur = conn.execute(
                """
                UPDATE kg_edge SET status = 'archived',
                  updated_by = %s, updated_by_name = %s
                WHERE id = %s AND NOT is_draft
                """,
                (user_id, user_name, edge_id),
            )
            conn.commit()
            return cur.rowcount > 0

    with connect() as conn:
        rows = conn.execute(
            "SELECT id, is_draft FROM kg_edge WHERE id = %s", (edge_id,)
        ).fetchall()
        has_draft = any(r["is_draft"] for r in rows)
        has_online = any(not r["is_draft"] for r in rows)
        if not rows:
            return False
        if has_draft and not has_online:
            conn.execute(
                "DELETE FROM kg_edge WHERE id = %s AND is_draft", (edge_id,)
            )
            conn.commit()
            return True
        if not has_draft:
            conn.execute(_COPY_EDGE_TO_DRAFT, ("archived", edge_id))
        cur = conn.execute(
            """
            UPDATE kg_edge SET target_status = 'archived',
              updated_by = %s, updated_by_name = %s
            WHERE id = %s AND is_draft
            """,
            (user_id, user_name, edge_id),
        )
        conn.commit()
        return cur.rowcount > 0
