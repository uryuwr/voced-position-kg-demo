"""
发布业务规则 BR-01~BR-08。

BR-01 skill_level_meta 全局定义
BR-02 专业 ≥1 岗位
BR-03 岗位 Σweight≈1（±0.01）
BR-04 技能 L1–L5 行为描述齐全
BR-05 先修无环
BR-06 删除技能引用校验
BR-07 草稿不进前台/图（published_only）
BR-08 发布门禁
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from backend.kg.pg_store.client import connect, use_conn
from backend.kg.pg_store.skill_aggregate import (
    SKILL_KEY_SQL,
    SKILL_NAME_SQL,
    skill_key_from_node,
)
from backend.kg.pg_store.skill_level_meta import REQUIRED_LEVEL_CODES

WEIGHT_TOLERANCE = 0.01
# 门禁提示语里给运营看的状态中文名。只在报错文案里用，不参与任何判定 ——
# 判定一律走 config 的 node_published / edge_published 那套谓词。
_STATUS_ZH = {"disabled": "已停用", "draft": "草稿", "archived": "已删除"}
_PUB_N = "COALESCE(n.status, 'published') = 'published'"
_PUB_E = "COALESCE(e.status, 'published') = 'published'"
_PUB_O = "COALESCE(o.status, 'published') = 'published'"


class PublishGateError(ValueError):
    def __init__(self, violations: list[dict[str, Any]]):
        self.violations = violations
        super().__init__(
            "; ".join(f"{v.get('rule')}: {v.get('message')}" for v in violations)
            or "publish gate failed"
        )


def _maybe_json(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v


def check_br02_major(major_id: str, *, conn: Any = None) -> list[dict[str, Any]]:
    # conn 可传：发布事务里要在**未提交**的状态上跑门禁（方案 §7 第 1 步）。
    # 自己 connect() 的话看不见同一事务里刚套用的草稿，门禁校的是旧内容，
    # 于是「改完权重去发布」会拿着改之前的 Σweight 判定，放过真正不合规的那次。
    with use_conn(conn) as conn:
        row = conn.execute(
            f"""
            SELECT count(DISTINCT e.dst_id) AS c
            FROM kg_edge e
            JOIN kg_node o ON o.id = e.dst_id AND o.type = 'occupation' AND {_PUB_O}
            WHERE e.src_id = %s AND e.rel_type = 'prepares_for' AND {_PUB_E}
            """,
            (major_id,),
        ).fetchone()
    c = int(row["c"] if row else 0)
    ok = c >= 1
    return [
        {
            "rule": "BR-02",
            "ok": ok,
            "message": (
                f"专业发布：已关联 {c} 个岗位"
                if ok
                else "专业发布条件未满足：须含 ≥1 已发布岗位（prepares_for）"
            ),
            "detail": {"occupation_count": c},
        }
    ]


def check_br03_occupation(
    occupation_id: str, *, conn: Any = None
) -> list[dict[str, Any]]:
    with use_conn(conn) as conn:
        rows = conn.execute(
            f"""
            SELECT ({SKILL_KEY_SQL}) AS skill_key, max(e.weight) AS w
            FROM kg_edge e
            JOIN kg_node n ON n.id = e.dst_id AND n.type = 'skill_level' AND {_PUB_N}
            WHERE e.src_id = %s AND e.rel_type = 'requires' AND {_PUB_E}
              AND e.weight IS NOT NULL
              AND NOT e.is_draft
            GROUP BY 1
            """,
            (occupation_id,),
        ).fetchall()
        # 被排除的技能单独查一次，**只为把话说清**。这里连着栽过：运营在页面上
        # 看到「权重和 1.00」（管理台列表口径含 disabled）、点发布却被告知
        # Σweight=0.8500，而 0.85 这个数字页面上任何地方都没有，也无从知道是哪
        # 一项被扣掉了。差额的来源只有一种：边指向的技能节点不是 published
        # （停用或已删）—— 那档技能对学员不可见，它的权重是真的丢了，所以门禁
        # 扣掉它是对的，缺的是解释。
        excluded = conn.execute(
            f"""
            SELECT ({SKILL_KEY_SQL}) AS skill_key,
                   ({SKILL_NAME_SQL}) AS skill_name,
                   COALESCE(n.status, 'published') AS skill_status,
                   max(e.weight) AS w
            FROM kg_edge e
            JOIN kg_node n ON n.id = e.dst_id AND n.type = 'skill_level'
              AND NOT n.is_draft
              AND COALESCE(n.status, 'published') NOT IN ('published', 'archived')
            WHERE e.src_id = %s AND e.rel_type = 'requires' AND {_PUB_E}
              AND e.weight IS NOT NULL
              AND NOT e.is_draft
            GROUP BY 1, 2, 3
            ORDER BY 4 DESC
            """,
            (occupation_id,),
        ).fetchall()
    if not rows:
        return [
            {
                "rule": "BR-03",
                "ok": False,
                "message": "岗位权重归一：无有效 requires.weight，无法发布",
                "detail": {"weight_sum": 0.0, "skill_count": 0},
            }
        ]
    total = 0.0
    for r in rows:
        try:
            total += float(r["w"])
        except (TypeError, ValueError):
            pass
    ok = abs(total - 1.0) <= WEIGHT_TOLERANCE
    ex_items = [
        {
            "skill_key": str(r["skill_key"]),
            # 这条消息的**全部价值就是「是哪几项被扣掉了」**，所以必须是展示名：
            # 「已排除 SKa1fa1d005d（已停用）」等于没说，运营还得再去查这串哈希
            "skill_name": str(r["skill_name"] or r["skill_key"]),
            "status": str(r["skill_status"]),
            "weight": round(float(r["w"] or 0), 4),
        }
        for r in excluded
        if r["skill_key"]
    ]
    ex_sum = round(sum(i["weight"] for i in ex_items), 4)
    ex_note = ""
    if not ok and ex_items:
        names = "、".join(
            f"{i['skill_name']}（{_STATUS_ZH.get(i['status'], i['status'])}，权重"
            f"{i['weight']:g}）"
            for i in ex_items[:4]
        )
        more = f" 等 {len(ex_items)} 项" if len(ex_items) > 4 else ""
        ex_note = (
            f"。已排除 {len(ex_items)} 项非发布态技能{more}，合计 {ex_sum:g}："
            f"{names} —— 这些技能对学员不可见，权重不计入。"
            f"请把它们启用，或从技能构成里移除并把权重补回 1"
        )
    return [
        {
            "rule": "BR-03",
            "ok": ok,
            "message": (
                f"岗位权重归一：Σweight={total:.4f}"
                + ("" if ok else f"（须在 1±{WEIGHT_TOLERANCE} 内）")
                + ex_note
            ),
            "detail": {
                "weight_sum": round(total, 4),
                "skill_count": len(rows),
                "tolerance": WEIGHT_TOLERANCE,
                "excluded_skills": ex_items,
                "excluded_weight_sum": ex_sum,
            },
        }
    ]


def _level_descriptions_for_skill_key(
    skill_key: str, region: str = "CN", *, conn: Any = None
) -> dict[str, str]:
    with use_conn(conn) as conn:
        rows = conn.execute(
            f"""
            -- **不能排除 disabled**：门禁评判的是「发布出去以后长什么样」，而发布
            -- 一个技能就是把它 L1–L5 全置为 published（`_set_skill_key_status`）。
            -- 把 disabled 挡在外面 = 门禁看不见自己要评判的那些行，于是对一个五档
            -- 描述齐全的已停用技能报「缺少 L1, L2, L3, L4, L5」，运营对着档位明细
            -- 里明明存在的五条描述无从下手。只排除 archived（逻辑删除，发布不会
            -- 把它捞回来）。
            -- `NOT n.is_draft` 是为了确定性：同一 id 有线上行与草稿行两行，下面
            -- 用 setdefault 收集，不钉住就变成「谁先被扫到算谁」。改状态的
            -- `_set_skill_key_status` 也只动线上行，两边口径一致。
            SELECT n.name, n.description, n.attrs
            FROM kg_node n
            WHERE n.type = 'skill_level'
              AND COALESCE(n.status, 'published') <> 'archived'
              AND NOT n.is_draft
              AND (%s::text IS NULL OR n.region = %s)
              AND ({SKILL_KEY_SQL}) = %s
            """,
            (region, region, skill_key),
        ).fetchall()
    found: dict[str, str] = {}
    for r in rows:
        attrs = _maybe_json(r.get("attrs")) or {}
        if not isinstance(attrs, dict):
            attrs = {}
        ld = attrs.get("level_descriptions")
        if isinstance(ld, dict):
            for k, v in ld.items():
                code = str(k).upper()
                if code in REQUIRED_LEVEL_CODES and v and str(v).strip():
                    found[code] = str(v).strip()
        code = str(attrs.get("level_code") or "").upper()
        if not re.match(r"^L[1-5]$", code):
            m = re.search(r"L([1-5])", r.get("name") or "", re.I)
            code = f"L{m.group(1)}" if m else ""
        desc = (r.get("description") or "").strip()
        if (
            code in REQUIRED_LEVEL_CODES
            and len(desc) >= 12
            and desc.count("·") < 3
            and not desc.startswith("权重")
        ):
            found.setdefault(code, desc)
    return found


def _skill_name(skill_key: str, *, conn: Any = None) -> str:
    """门禁文案用的技能展示名；查不到回落成 code。

    门禁消息是运营唯一的排错线索，「缺少 L2, L4」不说是哪个技能等于没说，
    而说成 `SKa1fa1d005d` 还得再查一次库。
    """
    from backend.kg.pg_store.skill_aggregate import resolve_skill_names

    return resolve_skill_names([skill_key], conn).get(skill_key) or skill_key


def check_br04_skill(
    skill_key: str, *, region: str = "CN", conn: Any = None
) -> list[dict[str, Any]]:
    found = _level_descriptions_for_skill_key(skill_key, region, conn=conn)
    missing = [c for c in REQUIRED_LEVEL_CODES if c not in found]
    ok = len(missing) == 0
    nm = _skill_name(skill_key, conn=conn)
    return [
        {
            "rule": "BR-04",
            "ok": ok,
            "message": (
                "技能行为必填：L1–L5 描述齐全"
                if ok
                else f"技能行为必填：「{nm}」缺少 {', '.join(missing)}"
            ),
            "detail": {
                "skill_key": skill_key,
                "skill_name": nm,
                "present": sorted(found.keys()),
                "missing": missing,
            },
        }
    ]


def check_br05_prereq_acyclic(
    skill_key: str, *, region: str = "CN", conn: Any = None
) -> list[dict[str, Any]]:
    with use_conn(conn) as conn:
        rows = conn.execute(
            "SELECT skill_key, prereq_skill_key FROM kg_skill_prereq WHERE region=%s",
            (region,),
        ).fetchall()
        # 名字要在 with 块内取：`use_conn(None)` 的连接出块即关，
        # 块外再拿这个 conn 去查就是「操作已关闭的连接」
        nm = _skill_name(skill_key, conn=conn)
    graph: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        graph[str(r["skill_key"])].append(str(r["prereq_skill_key"]))

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = defaultdict(int)

    def dfs(u: str) -> bool:
        color[u] = GRAY
        for v in graph.get(u, []):
            if color[v] == GRAY:
                return True
            if color[v] == WHITE and dfs(v):
                return True
        color[u] = BLACK
        return False

    # 仅检查从 skill_key 出发是否成环
    cyclic = False
    if skill_key in graph or any(skill_key in vs for vs in graph.values()):
        color.clear()
        cyclic = dfs(skill_key) if skill_key in graph else False
        # 若作为 prereq 被引用，也检查是否存在回到自己的路径
        if not cyclic and skill_key in graph:
            cyclic = dfs(skill_key)

    ok = not cyclic and skill_key not in graph.get(skill_key, [])
    return [
        {
            "rule": "BR-05",
            "ok": ok,
            "message": "先修无环" if ok else f"「{nm}」的先修成环，拒绝",
            "detail": {"skill_key": skill_key, "skill_name": nm},
        }
    ]


def check_br06_skill_delete(
    skill_key: str, *, region: str = "CN", conn: Any = None
) -> list[dict[str, Any]]:
    with use_conn(conn) as conn:
        node_ids = [
            r["id"]
            for r in conn.execute(
                f"""
                SELECT n.id FROM kg_node n
                WHERE n.type='skill_level'
                  AND ({SKILL_KEY_SQL}) = %s
                  AND (%s::text IS NULL OR n.region = %s)
                  AND NOT n.is_draft
                """,
                (skill_key, region, region),
            ).fetchall()
        ]
        requires_c = 0
        course_c = 0
        if node_ids:
            requires_c = int(
                conn.execute(
                    """
                    SELECT count(*) AS c FROM kg_edge
                    WHERE rel_type='requires' AND dst_id = ANY(%s)
                      AND COALESCE(status,'published') NOT IN ('archived')
                    """,
                    (node_ids,),
                ).fetchone()["c"]
            )
            course_c = int(
                conn.execute(
                    """
                    SELECT count(*) AS c FROM kg_edge
                    WHERE rel_type IN ('taught_by','related_to')
                      AND (src_id = ANY(%s) OR dst_id = ANY(%s))
                      AND COALESCE(status,'published') NOT IN ('archived')
                    """,
                    (node_ids, node_ids),
                ).fetchone()["c"]
            )
        prereq_as_target = int(
            conn.execute(
                """
                SELECT count(*) AS c FROM kg_skill_prereq
                WHERE region=%s AND prereq_skill_key=%s
                """,
                (region, skill_key),
            ).fetchone()["c"]
        )
    detail = {
        "requires_edges": requires_c,
        "course_edges": course_c,
        "prereq_as_required_by": prereq_as_target,
        "node_count": len(node_ids),
    }
    ok = requires_c == 0 and course_c == 0 and prereq_as_target == 0
    return [
        {
            "rule": "BR-06",
            "ok": ok,
            "message": (
                "删除引用校验通过"
                if ok
                else "删除引用校验失败：仍有 requires / 课程边 / 被先修引用"
            ),
            "detail": detail,
        }
    ]


def validate_publish(
    *,
    node_type: str | None,
    node_id: str | None = None,
    skill_key: str | None = None,
    region: str = "CN",
    action: str = "enable",
    conn: Any = None,
) -> dict[str, Any]:
    ntype = (node_type or "").lower()
    checks: list[dict[str, Any]] = []

    if action == "delete":
        sk = skill_key
        if not sk and node_id:
            from backend.kg.pg_store.query import get_node

            n = get_node(node_id, conn=conn)
            if n and n.get("type") == "skill_level":
                sk = skill_key_from_node(n)
            elif ntype in ("skill_level", "skill_bundle", "skill"):
                sk = node_id
        if sk:
            checks.extend(check_br06_skill_delete(sk, region=region, conn=conn))
        else:
            checks.append(
                {
                    "rule": "BR-06",
                    "ok": True,
                    "message": "非技能删除，跳过 BR-06",
                    "detail": {},
                }
            )
        return _pack(checks)

    if ntype == "major" and node_id:
        checks.extend(check_br02_major(node_id, conn=conn))
    elif ntype == "occupation" and node_id:
        checks.extend(check_br03_occupation(node_id, conn=conn))
    elif ntype in ("skill_level", "skill", "skill_bundle") or skill_key:
        sk = skill_key
        if not sk and node_id:
            from backend.kg.pg_store.query import get_node

            n = get_node(node_id, conn=conn)
            if n:
                sk = skill_key_from_node(n) if n.get("type") == "skill_level" else node_id
        if sk:
            checks.extend(check_br04_skill(sk, region=region, conn=conn))
            checks.extend(check_br05_prereq_acyclic(sk, region=region, conn=conn))
        else:
            checks.append(
                {
                    "rule": "BR-04",
                    "ok": False,
                    "message": "无法解析 skill_key",
                    "detail": {},
                }
            )
    else:
        checks.append(
            {
                "rule": "BR-08",
                "ok": True,
                "message": f"类型 {ntype or '-'} 无额外维度门禁",
                "detail": {},
            }
        )
    return _pack(checks)


def _pack(checks: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [c for c in checks if not c.get("ok")]
    return {
        "ok": len(failed) == 0,
        "checks": checks,
        "failed": failed,
        "rules": "BR-02~BR-06 / BR-08",
    }


def assert_publish_allowed(**kwargs: Any) -> dict[str, Any]:
    result = validate_publish(**kwargs)
    if not result["ok"]:
        raise PublishGateError(result["failed"])
    return result


def list_skill_keys(region: str = "CN") -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT ({SKILL_KEY_SQL}) AS skill_key
            FROM kg_node n
            WHERE n.type = 'skill_level'
              AND (%s::text IS NULL OR n.region = %s)
              AND COALESCE(n.status, 'published') NOT IN ('archived')
            """,
            (region, region),
        ).fetchall()
    return [r["skill_key"] for r in rows if r.get("skill_key")]


def demote_noncompliant(
    *,
    region: str = "CN",
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """
    扫描 major / occupation / skill_key，不达标则 status→draft。
    单连接批量扫描，避免 Windows 临时端口耗尽。
    """
    summary: dict[str, Any] = {
        "major": {"checked": 0, "demoted": 0, "ids": []},
        "occupation": {"checked": 0, "demoted": 0, "ids": []},
        "skill": {"checked": 0, "demoted": 0, "keys": []},
        "dry_run": dry_run,
        "region": region,
    }

    with connect() as conn:
        # 顺序：occupation → skill → major（专业依赖已发布岗位，须后置）

        # ── BR-03：岗位 requires 权重按 skill_key 聚合 ──
        occ_rows = conn.execute(
            f"""
            SELECT o.id AS occ_id, ({SKILL_KEY_SQL}) AS skill_key, max(e.weight) AS w
            FROM kg_node o
            LEFT JOIN kg_edge e
              ON e.src_id = o.id AND e.rel_type = 'requires'
              AND COALESCE(e.status, 'published') = 'published'
              AND e.weight IS NOT NULL
            LEFT JOIN kg_node n
              ON n.id = e.dst_id AND n.type = 'skill_level'
              AND COALESCE(n.status, 'published') = 'published'
            WHERE o.type = 'occupation' AND o.region = %s
              AND COALESCE(o.status, 'published') = 'published'
            GROUP BY o.id, 2
            """,
            (region,),
        ).fetchall()
        occ_sum: dict[str, float] = defaultdict(float)
        occ_has_weight: dict[str, bool] = defaultdict(bool)
        for r in occ_rows:
            oid = r["occ_id"]
            if r["skill_key"] is None or r["w"] is None:
                continue
            try:
                occ_sum[oid] += float(r["w"])
                occ_has_weight[oid] = True
            except (TypeError, ValueError):
                pass
        all_occ = conn.execute(
            """
            SELECT id FROM kg_node
            WHERE type='occupation' AND region=%s
              AND COALESCE(status,'published')='published'
            """,
            (region,),
        ).fetchall()
        occ_fail: list[str] = []
        for r in all_occ:
            oid = r["id"]
            summary["occupation"]["checked"] += 1
            if not occ_has_weight.get(oid):
                occ_fail.append(oid)
                continue
            if abs(occ_sum.get(oid, 0.0) - 1.0) > WEIGHT_TOLERANCE:
                occ_fail.append(oid)
        if limit is not None:
            occ_fail = occ_fail[:limit]
        summary["occupation"]["demoted"] = len(occ_fail)
        summary["occupation"]["ids"] = occ_fail[:50]
        if not dry_run and occ_fail:
            conn.execute(
                # 降级只针对已发布的线上行；草稿行的 status 恒为 draft，动它就违约
                "UPDATE kg_node SET status='draft' WHERE id = ANY(%s) AND NOT is_draft",
                (occ_fail,),
            )

        # ── BR-04：技能 L1–L5 描述 ──
        skill_rows = conn.execute(
            f"""
            SELECT ({SKILL_KEY_SQL}) AS skill_key, n.name, n.description, n.attrs
            FROM kg_node n
            WHERE n.type = 'skill_level'
              AND n.region = %s
              AND COALESCE(n.status, 'published') NOT IN ('archived', 'disabled')
            """,
            (region,),
        ).fetchall()
        skill_found: dict[str, set[str]] = defaultdict(set)
        all_keys: set[str] = set()
        for r in skill_rows:
            sk = r.get("skill_key")
            if not sk:
                continue
            all_keys.add(sk)
            attrs = _maybe_json(r.get("attrs")) or {}
            if not isinstance(attrs, dict):
                attrs = {}
            ld = attrs.get("level_descriptions")
            if isinstance(ld, dict):
                for k, v in ld.items():
                    code = str(k).upper()
                    if code in REQUIRED_LEVEL_CODES and v and str(v).strip():
                        skill_found[sk].add(code)
            code = str(attrs.get("level_code") or "").upper()
            if not re.match(r"^L[1-5]$", code):
                m = re.search(r"L([1-5])", r.get("name") or "", re.I)
                code = f"L{m.group(1)}" if m else ""
            desc = (r.get("description") or "").strip()
            if (
                code in REQUIRED_LEVEL_CODES
                and len(desc) >= 12
                and desc.count("·") < 3
                and not desc.startswith("权重")
            ):
                skill_found[sk].add(code)

        # 已 published 的 skill_key（至少有一档 published）
        pub_keys = {
            r["skill_key"]
            for r in conn.execute(
                f"""
                SELECT DISTINCT ({SKILL_KEY_SQL}) AS skill_key
                FROM kg_node n
                WHERE n.type = 'skill_level' AND n.region = %s
                  AND COALESCE(n.status, 'published') = 'published'
                """,
                (region,),
            ).fetchall()
            if r.get("skill_key")
        }

        # BR-05 先修环：整图一次 DFS 找环上节点
        pr_rows = conn.execute(
            "SELECT skill_key, prereq_skill_key FROM kg_skill_prereq WHERE region=%s",
            (region,),
        ).fetchall()
        graph: dict[str, list[str]] = defaultdict(list)
        for r in pr_rows:
            graph[str(r["skill_key"])].append(str(r["prereq_skill_key"]))
        cyclic_keys = _find_cyclic_keys(graph)

        skill_fail: list[str] = []
        for sk in sorted(pub_keys):
            summary["skill"]["checked"] += 1
            present = skill_found.get(sk, set())
            missing = [c for c in REQUIRED_LEVEL_CODES if c not in present]
            if missing or sk in cyclic_keys:
                skill_fail.append(sk)
        if limit is not None:
            skill_fail = skill_fail[:limit]
        summary["skill"]["demoted"] = len(skill_fail)
        summary["skill"]["keys"] = skill_fail[:50]
        if not dry_run and skill_fail:
            # 批量：凡 skill_key 在 fail 列表中的 skill_level → draft
            batch = 200
            for i in range(0, len(skill_fail), batch):
                chunk = skill_fail[i : i + batch]
                conn.execute(
                    f"""
                    UPDATE kg_node n SET status = 'draft'
                    WHERE n.type = 'skill_level'
                      AND n.region = %s
                      AND ({SKILL_KEY_SQL}) = ANY(%s)
                      AND NOT n.is_draft
                    """,
                    (region, chunk),
                )

        # ── BR-02：专业须 ≥1 已发布岗位（在岗位/技能降级之后执行）──
        total_majors = conn.execute(
            """
            SELECT count(*) AS c FROM kg_node
            WHERE type='major' AND region=%s
              AND COALESCE(status,'published')='published'
            """,
            (region,),
        ).fetchone()["c"]
        majors = conn.execute(
            """
            SELECT m.id
            FROM kg_node m
            WHERE m.type = 'major' AND m.region = %s
              AND COALESCE(m.status, 'published') = 'published'
              AND NOT EXISTS (
                SELECT 1 FROM kg_edge e
                JOIN kg_node o ON o.id = e.dst_id AND o.type = 'occupation'
                  AND COALESCE(o.status, 'published') = 'published'
                WHERE e.src_id = m.id AND e.rel_type = 'prepares_for'
                  AND COALESCE(e.status, 'published') = 'published'
              )
            """,
            (region,),
        ).fetchall()
        major_ids = [r["id"] for r in majors]
        if limit is not None:
            major_ids = major_ids[:limit]
        summary["major"]["checked"] = int(total_majors)
        summary["major"]["demoted"] = len(major_ids)
        summary["major"]["ids"] = major_ids[:50]
        if not dry_run and major_ids:
            conn.execute(
                "UPDATE kg_node SET status='draft' WHERE id = ANY(%s) AND NOT is_draft",
                (major_ids,),
            )

        if not dry_run:
            conn.commit()

    for k in ("major", "occupation"):
        summary[k]["sample_failed"] = summary[k]["ids"][:10]
    summary["skill"]["sample_failed"] = summary["skill"]["keys"][:10]
    return summary


def _find_cyclic_keys(graph: dict[str, list[str]]) -> set[str]:
    """返回参与有向环的节点集合。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = defaultdict(int)
    cyclic: set[str] = set()
    stack: list[str] = []

    def dfs(u: str) -> None:
        color[u] = GRAY
        stack.append(u)
        for v in graph.get(u, []):
            if color[v] == GRAY:
                # 环：stack 中 v 之后的都在环上
                if v in stack:
                    idx = stack.index(v)
                    cyclic.update(stack[idx:])
                cyclic.add(u)
            elif color[v] == WHITE:
                dfs(v)
                if v in cyclic:
                    cyclic.add(u)
        stack.pop()
        color[u] = BLACK

    for node in list(graph.keys()):
        if color[node] == WHITE:
            dfs(node)
    return cyclic


def _set_status(node_id: str, status: str) -> None:
    with connect() as conn:
        conn.execute(
            # 发布侧只动线上行：不钉 NOT is_draft 会把草稿行的 status 一起改，
            # 写 published 直接撞 ck_kg_node_draft_status（草稿 status 恒为 draft）
            "UPDATE kg_node SET status=%s WHERE id=%s AND NOT is_draft",
            (status, node_id),
        )
        conn.commit()


def _set_skill_key_status(skill_key: str, status: str, *, region: str = "CN") -> int:
    with connect() as conn:
        cur = conn.execute(
            f"""
            UPDATE kg_node n SET status = %s
            WHERE n.type = 'skill_level'
              AND (%s::text IS NULL OR n.region = %s)
              AND ({SKILL_KEY_SQL}) = %s
              AND NOT n.is_draft
            """,
            (status, region, region, skill_key),
        )
        conn.commit()
        return int(cur.rowcount or 0)


def try_publish_node(
    node_id: str,
    *,
    user_id: str,
    user_name: str,
    region: str = "CN",
) -> dict[str, Any]:
    """尝试将节点/逻辑技能置为 published；**门禁不过则原样不动**，只回 gate。

    2026-08-19 改：失败时不再把记录降成 `draft`。原来那样做有两个坏处 ——
    一是点一次失败的「发布」就会把一条 `disabled`（已停用）记录变成草稿，运营
    丢掉了停用状态而且没被告知；二是 `status` 从此兼任「上次发布试过但没过」的
    标记，而草稿态方案里 `status` 只表达可见性，是否有待发布内容看
    `is_draft` 那一行。失败原因回在 `gate.failed` 里，前端照原样显示即可。
    """
    from backend.kg.pg_store.query import get_node
    from backend.kg.pg_store.write import patch_node

    n = get_node(node_id)
    if not n:
        raise ValueError("node not found")
    ntype = n.get("type")
    if ntype == "skill_level":
        sk = skill_key_from_node(n)
        gate = validate_publish(
            node_type="skill_bundle", skill_key=sk, region=region, action="enable"
        )
        # 回执带展示名：运营点「启用」看到的就是它，只给 code 无从确认操作对象
        sk_name = _skill_name(sk, conn=None)
        if not gate["ok"]:
            return {"status": str(n.get("status") or "draft"), "gate": gate,
                    "skill_key": sk, "skill_name": sk_name}
        _set_skill_key_status(sk, "published", region=region)
        return {"status": "published", "gate": gate, "skill_key": sk,
                "skill_name": sk_name}

    gate = validate_publish(
        node_type=ntype, node_id=node_id, region=region, action="enable"
    )
    if not gate["ok"]:
        return {"status": str(n.get("status") or "draft"), "gate": gate,
                "node_id": node_id}
    patch_node(
        node_id,
        {"status": "published"},
        user_id=user_id,
        user_name=user_name,
        to_draft=False,
    )
    return {"status": "published", "gate": gate, "node_id": node_id}
