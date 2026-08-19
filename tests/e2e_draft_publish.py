"""草稿 → 发布 全链路 E2E（草稿态方案 §11 的 P1/P2 验收项）。

段落
----
A 编辑 → 前台逐字节不变   做一组覆盖各类型的编辑（改名 / 改 attrs / 加边 / 删边 / 改权重 /
                          归档 / 新建），编辑前后把前台响应各取一遍，**必须完全相同**
B 管理台看得到自己改的     列表只有一行、record_status=draft、详情有 published/draft 两份
C 发布                     前台变、version+1、草稿清空、边生效
D 拒绝的四种情形           BR 门禁 / 并发 409 / 编码冲突 / 端点未发布
E 丢弃 + 归档发布          丢弃后前台仍是原样；归档发布后前台真的不返回了
F 闸门                     scripts/check_orphan_edges.py 必须 PASS

运行：python -X utf8 tests/e2e_draft_publish.py
自起 18200 端口实例（E2E_PORT 可覆盖）。测试数据以 `ZZ草稿E2E` 前缀标识，跑完清理。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PORT = int(os.getenv("E2E_PORT") or "18200")
BASE = f"http://127.0.0.1:{PORT}"
HEAD = {"X-Test-Uid": "9004", "X-Test-Uname": "draftpub"}
LOG = ROOT / "tests" / "_e2e_draft_publish_server.log"
TAG = "ZZ草稿E2E"

_results: list[tuple[str, bool, str]] = []


def check(case: str, ok: bool, note: str = "") -> None:
    _results.append((case, bool(ok), note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {case}" + (f" — {note}" if note else ""))


def call(method: str, path: str, params: dict | None = None, body: Any = None):
    url = BASE + urllib.parse.quote(path, safe="/:%")
    if params:
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )
    head = dict(HEAD)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        head["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, headers=head, data=data)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read().decode("utf8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf8", "ignore")
    except Exception as e:  # noqa: BLE001
        return -1, str(e)[:200]


def jcall(*a: Any, **k: Any) -> tuple[int, Any]:
    st, b = call(*a, **k)
    try:
        return st, json.loads(b)
    except Exception:  # noqa: BLE001
        return st, b


def start_server() -> subprocess.Popen:
    log = LOG.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(ROOT / "tests" / "_e2e_server.py"), str(PORT)],
        cwd=str(ROOT), env=dict(os.environ, PYTHONUTF8="1"), stdout=log, stderr=log,
    )
    for _ in range(60):
        time.sleep(1)
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=2) as r:
                if r.status == 200:
                    return proc
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError(f"服务未在 60s 内就绪，见 {LOG}")


def purge() -> None:
    """清掉本测试造的数据。

    ⚠ 删节点**只认 id 前缀，绝不按名字选 id**。按名字选出来的 id 会包含真实节点
    ——本测试正是把某个真实岗位的**草稿行**改成带 TAG 的名字——再按那个 id 去
    `DELETE FROM kg_node WHERE id=...` 就会把它的线上行和全部边一起删掉。
    这个坑在开发本功能时真踩过一次（误删一个岗位 + 16 条边，靠从另一个库读回来才补上）。
    """
    from backend.kg.pg_store.client import connect

    with connect() as c:
        ids = [
            r["id"]
            for r in c.execute(
                "SELECT id FROM kg_node WHERE id LIKE %s", (f"%{TAG}%",)
            ).fetchall()
        ]
        if ids:
            c.execute(
                "DELETE FROM kg_edge WHERE src_id = ANY(%s) OR dst_id = ANY(%s) "
                "OR unit_id = ANY(%s)",
                (ids, ids, ids),
            )
            c.execute("DELETE FROM kg_node WHERE id = ANY(%s)", (ids,))
        # 本测试造过的草稿（改名 / 加边 / 归档）一律回滚掉，线上行不动。
        # **不能只删 is_draft 的**：C 段真的发布过一条边，那条已经转正成线上行，
        # 漏删会把靶子岗位的权重和永久推离 1，下次跑连 fixture 都挑不到它
        c.execute(
            "DELETE FROM kg_edge WHERE COALESCE(evidence,'') LIKE %s", (f"%{TAG}%",)
        )
        c.execute("DELETE FROM kg_node WHERE is_draft AND name LIKE %s", (f"%{TAG}%",))
        c.commit()


def fixture() -> dict[str, Any]:
    """挑一个「技能构成完整、权重和为 1」的已发布岗位当靶子。"""
    from backend.kg.pg_store.client import connect

    with connect() as c:
        row = c.execute(
            """
            SELECT o.id, o.name, o.version,
                   sum(e.weight) AS wsum, count(*) AS ec
            FROM kg_node o
            JOIN kg_edge e ON e.src_id = o.id AND e.rel_type='requires'
                 AND NOT e.is_draft AND COALESCE(e.status,'published')='published'
            WHERE o.type='occupation' AND NOT o.is_draft
              AND COALESCE(o.status,'published')='published'
            GROUP BY o.id, o.name, o.version
            HAVING abs(sum(e.weight) - 1.0) <= 0.01 AND count(*) >= 2
            ORDER BY o.id LIMIT 1
            """
        ).fetchone()
        assert row, "库里找不到权重和为 1 的岗位，无法测 BR-03 门禁"
        edges = c.execute(
            "SELECT id, dst_id, weight FROM kg_edge WHERE src_id=%s AND rel_type='requires' "
            "AND NOT is_draft ORDER BY weight DESC NULLS LAST, id",
            (row["id"],),
        ).fetchall()
        major = c.execute(
            "SELECT id, name FROM kg_node WHERE type='major' AND NOT is_draft "
            "AND COALESCE(status,'published')='published' ORDER BY id LIMIT 1"
        ).fetchone()
        skill = c.execute(
            "SELECT id FROM kg_node WHERE type='skill_level' AND NOT is_draft "
            "AND COALESCE(status,'published')='published' "
            "AND id <> ALL(%s) ORDER BY id LIMIT 1",
            ([e["dst_id"] for e in edges],),
        ).fetchone()
    return {
        "occ": row["id"], "occ_name": row["name"], "occ_version": int(row["version"] or 1),
        "edges": [dict(e) for e in edges], "major": major["id"], "skill": skill["id"],
    }


_SNAP: list[tuple[str, dict]] = []


def snapshot(f: dict[str, Any]) -> dict[str, str]:
    global _SNAP
    if not _SNAP:
        _SNAP = [
            ("/v1/stats", {}),
            ("/v1/kg/nodes", {"type": "occupation", "page_size": "100", "include_counts": "1"}),
            ("/v1/kg/nodes", {"type": "major", "page_size": "100", "include_counts": "1"}),
            ("/v1/kg/nodes", {"type": "skill_level", "page_size": "100"}),
            ("/v1/kg/edges", {"page_size": "200"}),
            ("/v1/kg/edges", {"node_id": f["occ"], "page_size": "200"}),
            ("/v1/node", {"id": f["occ"], "include_counts": "1", "include_links": "1"}),
            ("/v1/node", {"id": f["major"], "include_counts": "1"}),
            ("/v1/occupations/skills", {"occupation_id": f["occ"], "limit": "200"}),
            ("/v1/occupations/requires", {"q": f["occ_name"][:4], "limit": "100"}),
            ("/v1/majors/occupations", {"major_id": f["major"], "limit": "200"}),
            ("/v1/industry-tree", {}),
            ("/v1/search", {"q": f["occ_name"][:2], "limit": "50"}),
            ("/v1/expand", {"node_id": f["occ"], "limit": "100"}),
            ("/v1/student/positions", {"page_size": "100"}),
            ("/v1/student/positions/skill-composition", {"id": f["occ"]}),
            ("/v1/student/skills", {"page_size": "100"}),
            ("/v1/capability", {"major_id": f["major"], "include_skills": "1"}),
            ("/v1/industry-graph", {}),
        ]
    out = {}
    for p, q in _SNAP:
        st, b = call("GET", p, q)
        out[f"{p}?{urllib.parse.urlencode(q)}"] = f"{st}:{b}"
    return out


# ── A 编辑 → 前台不变 ────────────────────────────────────────
def seg_a(f: dict[str, Any]) -> dict[str, Any]:
    print("\n== A 一组编辑动作，前台响应逐字节不变 ==")
    before = snapshot(f)
    made: dict[str, Any] = {}

    st, n = jcall("PATCH", "/v1/kg/nodes/" + f["occ"],
                  body={"name": f"{f['occ_name']}·{TAG}改名",
                        "description": f"{TAG} 描述",
                        "attrs": {"code": f"{TAG}CODE1"}})
    check("A1 改名 + 改 attrs 落草稿", st == 200 and n.get("record_status") == "draft",
          f"HTTP {st} record_status={n.get('record_status') if isinstance(n, dict) else n}")

    st, e = jcall("POST", "/v1/kg/edges",
                  body={"src_id": f["occ"], "dst_id": f["skill"], "rel_type": "requires",
                        "weight": 0.5, "region": "CN", "evidence": f"{TAG} 新增技能"})
    check("A2 加边落草稿", st == 200, f"HTTP {st}")
    made["new_edge"] = (e or {}).get("id") if isinstance(e, dict) else None

    victim = f["edges"][0]["id"]
    st, b = call("DELETE", "/v1/kg/edges/" + victim)
    check("A3 删边落草稿（墓碑）", st == 200, f"HTTP {st}")
    made["tombstone"] = victim

    st, nn = jcall("POST", "/v1/kg/nodes",
                   # id 显式带 TAG：purge 只按 id 前缀删（见 purge 的注释）
                   body={"id": f"{TAG}:new-occupation", "type": "occupation",
                         "name": f"{TAG}新建岗位", "region": "CN",
                         "attrs": {"code": f"{TAG}NEW"}, "source_system": "MANUAL",
                         "source_id": "zz", "source_url": "http://x", "license": "none",
                         "status": "published"})
    check("A4 新建节点只有草稿行", st == 200, f"HTTP {st}")
    made["new_node"] = (nn or {}).get("id") if isinstance(nn, dict) else None

    # 2026-08-19 需求收窄：**归档改成立即生效**，所以它不能再留在这一组
    # 「编辑后前台逐字节不变」的动作里 —— 归档一下前台当场少一条，A6 必然失败。
    # 归档/停用/删除的立即生效由 seg_h 正面覆盖（换了断言方向，不是删测试）。
    st, _ = jcall("PATCH", "/v1/kg/nodes/" + f["occ"], body={"name": f"{f['occ_name']}·{TAG}改名2"})
    check("A5 再改一次名字（仍在草稿里累积，不产生第二行）", st == 200, f"HTTP {st}")

    after = snapshot(f)
    diff = [k for k in before if before[k] != after.get(k)]
    check(f"A6 {len(before)} 个前台响应在这轮编辑前后逐字节相同", not diff,
          "变了：" + "; ".join(diff)[:500] if diff else "全部一致")
    return made


# ── B 管理台 ─────────────────────────────────────────────────
def seg_b(f: dict[str, Any], made: dict[str, Any]) -> None:
    print("\n== B 管理台看得到自己改的 ==")
    st, d = jcall("GET", "/v1/kg/nodes",
                  {"type": "occupation", "scope": "manage", "q": TAG, "page_size": "50"})
    rows = [i for i in (d.get("items") or []) if TAG in (i.get("name") or "")] if st == 200 else []
    check("B1 管理台列表能搜到改后的新名字", len(rows) >= 1, f"HTTP {st} 命中 {len(rows)}")
    # 按**原名**搜：草稿名是「原名·TAG改名」，两行都能被这个关键字命中，
    # 所以「只回一行」才真的证明去重生效（用不带 q 的第一页会因为分页而漏掉靶子）
    st, d = jcall("GET", "/v1/kg/nodes",
                  {"type": "occupation", "scope": "manage",
                   "q": f["occ_name"], "page_size": "200"})
    same = [i for i in (d.get("items") or []) if i["id"] == f["occ"]] if st == 200 else []
    check("B2 同一记录在管理台列表里只有一行（草稿行 + 线上行都能被关键字命中）",
          len(same) == 1, f"{len(same)} 行 HTTP {st}")
    st, d = jcall("GET", "/v1/kg/node-detail", {"id": f["occ"]})
    ok = (
        st == 200 and d.get("record_status") == "draft"
        and TAG in ((d.get("draft") or {}).get("name") or "")
        and TAG not in ((d.get("published") or {}).get("name") or "")
    )
    check("B3 详情返回 published{} / draft{} 两份可对比", ok,
          f"HTTP {st} record_status={d.get('record_status') if isinstance(d, dict) else d}")
    st, d = jcall("GET", "/v1/admin/drafts", {"page_size": "200"})
    units = {i["node_id"]: i for i in (d.get("items") or [])} if st == 200 else {}
    u = units.get(f["occ"]) or {}
    check("B4 待发布清单：岗位单元统计到 1 加 1 删",
          u.get("edges_upsert") == 1 and u.get("edges_remove") == 1,
          f"upsert={u.get('edges_upsert')} remove={u.get('edges_remove')}")
    check("B5 待发布清单包含新建节点", made["new_node"] in units,
          f"清单 {len(units)} 个单元")
    # 归档已改成立即生效 → **不该**再出现在待发布清单里（以前这里断言的正好相反）
    check("B6 归档/停用不再进清单（立即生效）",
          f["major"] not in units or (units.get(f["major"]) or {}).get("change_kind") == "edges",
          str((units.get(f["major"]) or {}).get("change_kind")))


# ── C 发布 ───────────────────────────────────────────────────
def seg_c(f: dict[str, Any], made: dict[str, Any]) -> None:
    print("\n== C 发布 ==")
    # 岗位单元：加了一条 0.5 的边、删了一条，权重和必然不再是 1 → BR-03 应当拦住
    st, d = jcall("POST", "/v1/admin/publish/node", {"node_id": f["occ"]})
    check("C1 权重和不为 1 的岗位被 BR-03 事前拦住（400）",
          st == 400 and (d.get("detail") or {}).get("error") == "gate_failed",
          f"HTTP {st} {json.dumps(d, ensure_ascii=False)[:160]}")
    from backend.kg.pg_store.client import connect

    with connect() as c:
        n = c.execute(
            "SELECT count(*) AS c FROM kg_node WHERE id=%s AND is_draft", (f["occ"],)
        ).fetchone()["c"]
        online_name = c.execute(
            "SELECT name FROM kg_node WHERE id=%s AND NOT is_draft", (f["occ"],)
        ).fetchone()["name"]
    check("C2 被门禁拒后草稿仍在、线上行未变（事务回滚干净）",
          n == 1 and TAG not in online_name, f"草稿行 {n} 条，线上名「{online_name}」")

    # 把新边权重改成能配平的值：原边被删掉了，补上它的权重
    victim_w = float(f["edges"][0]["weight"] or 0)
    st, _ = jcall("POST", "/v1/kg/edges",
                  body={"id": made["new_edge"], "src_id": f["occ"], "dst_id": f["skill"],
                        "rel_type": "requires", "weight": victim_w, "region": "CN",
                        "evidence": f"{TAG} 新增技能（配平）"})
    st, d = jcall("POST", "/v1/admin/publish/node", {"node_id": f["occ"]})
    ok = st == 200 and d.get("status") == "published"
    check("C3 配平后发布成功", ok, f"HTTP {st} {json.dumps(d, ensure_ascii=False)[:200]}")
    if ok:
        check("C4 version+1", d.get("version") == f["occ_version"] + 1,
              f"{f['occ_version']} → {d.get('version')}")
        check("C5 边一起生效：1 条新增 + 1 条归档",
              d.get("edges_published") == 1 and d.get("edges_archived") == 1,
              f"published={d.get('edges_published')} archived={d.get('edges_archived')}")
    with connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT is_draft, status, name, version FROM kg_node WHERE id=%s ORDER BY is_draft",
            (f["occ"],))]
        tomb = c.execute("SELECT status, is_draft FROM kg_edge WHERE id=%s",
                         (made["tombstone"],)).fetchall()
    check("C6 发布后草稿行消失、只剩一行线上行",
          len(rows) == 1 and not rows[0]["is_draft"] and TAG in rows[0]["name"],
          f"{len(rows)} 行 status={rows[0]['status'] if rows else '-'}")
    check("C7 被删的边落成 archived（逻辑删除，未物理删）",
          len(tomb) == 1 and not tomb[0]["is_draft"] and tomb[0]["status"] == "archived",
          str([dict(t) for t in tomb]))
    st, b = call("GET", "/v1/node", {"id": f["occ"]})
    check("C8 前台现在能看到新名字了", st == 200 and TAG in b, f"HTTP {st}")
    st, b = call("GET", "/v1/kg/edges", {"node_id": f["occ"], "page_size": "200"})
    check("C9 前台边列表里已归档的那条不再出现",
          st == 200 and made["tombstone"] not in b, f"HTTP {st}")


# ── D 拒绝的其余情形 ─────────────────────────────────────────
def seg_d(f: dict[str, Any], made: dict[str, Any]) -> None:
    print("\n== D 并发 / 编码冲突 / 端点未发布 ==")
    from backend.kg.pg_store.client import connect

    # 端点未发布：新建岗位上挂了一条指向它的草稿边（由专业侧编辑产生）
    st, _ = jcall("POST", "/v1/kg/edges",
                  body={"src_id": made["new_node"], "dst_id": f["skill"],
                        "rel_type": "requires", "weight": 1.0, "region": "CN",
                        "evidence": f"{TAG} 新岗位技能"})
    st, e = jcall("POST", "/v1/kg/edges",
                  body={"src_id": f["major"], "dst_id": made["new_node"],
                        "rel_type": "prepares_for", "region": "CN",
                        "evidence": f"{TAG} 专业挂新岗位"})
    st, d = jcall("POST", "/v1/admin/publish/node", {"node_id": f["major"]})
    ok = st == 400 and (d.get("detail") or {}).get("error") == "missing_endpoints"
    check("D1 草稿边指向未发布的节点 → 拒绝并指出缺哪个",
          ok and made["new_node"] in json.dumps(d, ensure_ascii=False),
          f"HTTP {st} {json.dumps(d, ensure_ascii=False)[:200]}")

    # 编码冲突：让新建节点占一个已被线上记录占用的 code
    with connect() as c:
        taken = c.execute(
            "SELECT id, attrs::json->>'code' AS code FROM kg_node "
            "WHERE type='occupation' AND NOT is_draft "
            "AND COALESCE(attrs::json->>'code','') <> '' LIMIT 1"
        ).fetchone()
        c.execute(
            "UPDATE kg_node SET attrs = %s WHERE id=%s AND is_draft",
            (json.dumps({"code": taken["code"]}, ensure_ascii=False), made["new_node"]),
        )
        c.commit()
    st, d = jcall("POST", "/v1/admin/publish/node", {"node_id": made["new_node"]})
    check("D2 编码被线上记录占用 → 409",
          st == 409 and (d.get("detail") or {}).get("error") == "code_conflict",
          f"HTTP {st} {json.dumps(d, ensure_ascii=False)[:160]}")

    # 并发：线上 version 被别人推进过
    st, _ = jcall("PATCH", "/v1/kg/nodes/" + f["occ"], body={"description": f"{TAG} 二改"})
    with connect() as c:
        c.execute("UPDATE kg_node SET version = version + 1 WHERE id=%s AND NOT is_draft",
                  (f["occ"],))
        c.commit()
    st, d = jcall("POST", "/v1/admin/publish/node", {"node_id": f["occ"]})
    check("D3 编辑期间别人发布过 → 409 而不是静默覆盖",
          st == 409 and (d.get("detail") or {}).get("error") == "stale_draft",
          f"HTTP {st} {json.dumps(d, ensure_ascii=False)[:160]}")

    # 批量：一次提交混合成功与失败，整体 200、逐项报因
    st, d = jcall("POST", "/v1/admin/publish/batch",
                  body={"node_ids": [f["occ"], made["new_node"], "ZZ:不存在的id"]})
    codes = {i["node_id"]: i["code"] for i in (d.get("items") or [])} if st == 200 else {}
    check("D4 批量发布逐项返回原因，整体 200",
          st == 200 and codes.get(f["occ"]) == "conflict"
          and codes.get("ZZ:不存在的id") == "not_found",
          f"HTTP {st} {json.dumps(codes, ensure_ascii=False)[:200]}")


# ── E 丢弃 + 归档发布 ────────────────────────────────────────
def seg_e(f: dict[str, Any], made: dict[str, Any]) -> None:
    print("\n== E 丢弃草稿 / 归档发布 ==")
    st, b = call("GET", "/v1/node", {"id": f["occ"]})
    before = b
    st, d = jcall("DELETE", "/v1/admin/draft", {"node_id": f["occ"]})
    check("E1 丢弃草稿返回删掉的行数", st == 200 and d.get("nodes_discarded") == 1,
          f"HTTP {st} {json.dumps(d, ensure_ascii=False)[:120]}")
    st, b = call("GET", "/v1/node", {"id": f["occ"]})
    check("E2 丢弃后前台与丢弃前一致（线上行从未被动过）", b == before, "逐字节相同")
    st, _ = jcall("DELETE", "/v1/admin/draft", {"node_id": made["new_node"]})
    # E3/E4 原来验的是「归档进草稿 → 发布后前台才不返回」。2026-08-19 需求收窄后
    # 归档立即生效，所以断言方向反过来：**归档当场就该对前台生效，不需要发布**。
    before_major = call("GET", "/v1/node", {"id": f["major"]})[0]
    st, _ = call("DELETE", "/v1/kg/nodes/" + f["major"])
    check("E3 归档立即生效（不进草稿、无需发布）", st == 200, f"HTTP {st}")
    st, b = call("GET", "/v1/node", {"id": f["major"]})
    check(
        "E4 归档后前台当场不再返回这个专业",
        before_major == 200 and (st == 404 or "not found" in b.lower()),
        f"归档前 HTTP {before_major} → 归档后 HTTP {st}",
    )
    st, d = jcall("GET", "/v1/admin/drafts", {"page_size": "200"})
    unit = next((i for i in (d.get("items") or []) if i["node_id"] == f["major"]), None)
    # 这个单元可能因为 D1 造的草稿边还在清单里，那与归档无关；
    # 要断言的是**归档本身没有产生节点草稿、也没有留下归档意图**
    check(
        "E5 归档没有产生节点草稿 / 归档意图",
        unit is None
        or (unit.get("has_node_draft") is False and unit.get("target_status") is None),
        f"unit={json.dumps(unit, ensure_ascii=False)[:160] if unit else None}",
    )
    # 恢复：archived 是逻辑删除，直接改库还原，别把库越测越脏
    from backend.kg.pg_store.client import connect

    with connect() as c:
        c.execute("UPDATE kg_node SET status='published' WHERE id=%s AND NOT is_draft",
                  (f["major"],))
        c.commit()


# ── G 技能构成（边）也走草稿 ─────────────────────────────────
def seg_g() -> dict[str, Any]:
    """技能构成是**边**不是节点字段，是这次需求最初报的那个 bug：

    「改岗位技能构成后前台仍是已发布内容」—— 从前 `skill_composition` 里是裸 DELETE +
    直写 published 边，绕过了 write.py，所以改完当场生效。这一段专门盯它。
    """
    print("\n== G 技能构成（PUT/DELETE/normalize）也落草稿 ==")
    from backend.kg.pg_store.client import connect

    from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL

    with connect() as c:
        # 必须挑「每个技能只有一条边」的岗位：库里大量国标岗位对同一技能挂了 L1/L2/L3
        # 多条边，而**管理台按边求和、BR-03 按 skill_key 取 max 再求和**——两个口径本来
        # 就打架（见 scripts/dedupe_skill_composition_edges.py 的说明，与草稿态无关）。
        # 拿那种岗位当靶子，归一化到 1 之后门禁照样判 0.56，测的就不是草稿了。
        occ = c.execute(
            f"""
            SELECT o.id, o.name FROM kg_node o
            JOIN kg_edge e ON e.src_id = o.id AND e.rel_type='requires'
                 AND NOT e.is_draft AND COALESCE(e.status,'published')='published'
            JOIN kg_node n ON n.id = e.dst_id AND n.type='skill_level' AND NOT n.is_draft
            WHERE o.type='occupation' AND NOT o.is_draft
              AND COALESCE(o.status,'published')='published'
            GROUP BY 1,2
            HAVING count(*) >= 3 AND count(*) = count(DISTINCT ({SKILL_KEY_SQL}))
            ORDER BY o.id DESC LIMIT 1
            """
        ).fetchone()
        orig = [
            dict(r)
            for r in c.execute(
                "SELECT id, weight, status FROM kg_edge WHERE src_id=%s "
                "AND rel_type='requires' AND NOT is_draft",
                (occ["id"],),
            ).fetchall()
        ]
    ctx = {"occ": occ["id"], "orig": orig}
    # skill_key 取自接口而不是自己拼 attrs：SKILL_KEY_SQL 在 attrs.skill_key 缺失时
    # 会从名字派生，自己拼会拿到 None，接着整段崩在 NoneType 上
    st, cur_comp = jcall("GET", "/v1/admin/composition", {"node_id": occ["id"]})
    have = [i["skill_key"] for i in (cur_comp.get("items") or [])] if st == 200 else []
    st, opts = jcall("GET", "/v1/admin/composition/options", {"limit": "50"})
    cand = [
        o["skill_key"]
        for o in (opts if isinstance(opts, list) else [])
        if o.get("skill_key") and o["skill_key"] not in have and o.get("available_levels")
    ]
    assert have and cand, f"构成段取不到靶子技能：have={have[:3]} cand={cand[:3]}"
    mine = {"k": have[0]}
    other = {"k": cand[0]}
    paths = [
        ("/v1/occupations/skills", {"occupation_id": occ["id"], "limit": "200"}),
        ("/v1/student/positions/skill-composition", {"id": occ["id"]}),
        ("/v1/stats", {}),
        ("/v1/kg/edges", {"node_id": occ["id"], "page_size": "200"}),
        ("/v1/node", {"id": occ["id"], "include_counts": "1"}),
    ]
    snap = lambda: {p: call("GET", p, q)[1] for p, q in paths}  # noqa: E731
    before = snap()
    steps = [
        ("改权重", lambda: call("PUT", "/v1/admin/composition", {"node_id": occ["id"]},
                             {"skill_key": mine["k"], "weight": 0.42})),
        ("加技能", lambda: call("PUT", "/v1/admin/composition", {"node_id": occ["id"]},
                             {"skill_key": other["k"], "weight": 0.1})),
        ("删技能", lambda: call("DELETE", "/v1/admin/composition",
                             {"node_id": occ["id"], "skill_key": mine["k"]})),
        ("归一化", lambda: call("POST", "/v1/admin/composition/normalize",
                             {"node_id": occ["id"]})),
    ]
    bad = [f"{n}→{call_fn()[0]}" for n, call_fn in steps if True]
    bad = [x for x in bad if not x.endswith("→200")]
    check("G1 四种构成编辑都成功", not bad, "; ".join(bad) or "改权重/加/删/归一化 均 200")
    after = snap()
    diff = [p for p in before if before[p] != after[p]]
    check("G2 构成改完前台逐字节不变（这是最初报的那个 bug）", not diff,
          "变了：" + "; ".join(diff)[:300] if diff else f"{len(paths)} 个前台响应一致")
    st, d = jcall("GET", "/v1/admin/composition", {"node_id": occ["id"]})
    keys = [i["skill_key"] for i in (d.get("items") or [])] if st == 200 else []
    check("G3 管理台构成页看的是草稿视图（删掉的不在、加的在、权重和为 1）",
          st == 200 and mine["k"] not in keys and other["k"] in keys
          and abs(float(d.get("weight_sum") or 0) - 1.0) <= 0.005,
          f"HTTP {st} weight_sum={d.get('weight_sum')} 含被删技能={mine['k'] in keys}")
    st, d = jcall("GET", "/v1/admin/drafts", {"page_size": "200"})
    u = next((i for i in (d.get("items") or []) if i["node_id"] == occ["id"]), {})
    check("G4 待发布清单把它列成「只改了边」的单元",
          bool(u) and u.get("has_node_draft") is False and u.get("edges_upsert", 0) >= 1
          and u.get("edges_remove", 0) >= 1,
          f"upsert={u.get('edges_upsert')} remove={u.get('edges_remove')} "
          f"node_draft={u.get('has_node_draft')}")
    st, d = jcall("POST", "/v1/admin/publish/node", {"node_id": occ["id"]})
    check("G5 发布该单元成功（BR-03 在新权重上通过）",
          st == 200 and d.get("edges_published", 0) >= 1,
          f"HTTP {st} {json.dumps(d, ensure_ascii=False)[:180]}")
    after2 = snap()
    check("G6 发布后前台才变", after2 != after, "前台已更新")
    with connect() as c:
        left = c.execute(
            "SELECT count(*) AS c FROM kg_edge WHERE is_draft AND unit_id=%s", (occ["id"],)
        ).fetchone()["c"]
    check("G7 发布后该单元草稿清空", left == 0, f"剩 {left} 条")
    return ctx


def _restore_composition(ctx: dict[str, Any]) -> None:
    """把靶子岗位的 requires 边恢复原样，别把库越测越脏。"""
    from backend.kg.pg_store.client import connect

    if not ctx:
        return
    keep = {e["id"] for e in ctx["orig"]}
    with connect() as c:
        cur = [
            r["id"]
            for r in c.execute(
                "SELECT id FROM kg_edge WHERE src_id=%s AND rel_type='requires' "
                "AND NOT is_draft",
                (ctx["occ"],),
            ).fetchall()
        ]
        extra = [i for i in cur if i not in keep]
        if extra:
            c.execute("DELETE FROM kg_edge WHERE id = ANY(%s)", (extra,))
        for e in ctx["orig"]:
            c.execute(
                "UPDATE kg_edge SET weight=%s, status=%s WHERE id=%s AND NOT is_draft",
                (e["weight"], e["status"], e["id"]),
            )
        c.execute("DELETE FROM kg_edge WHERE is_draft AND unit_id=%s", (ctx["occ"],))
        c.commit()


# ── H 状态动作：停用 / 启用 / 归档 / 删除都立即生效 ───────────
def seg_h() -> None:
    """2026-08-19 需求收窄：这四个动作**不进草稿**，点了就生效。

    上一版这里验的是「删除落成 target_status='deleted' 的草稿、发布时才真删、
    发布前可撤销」。需求改了，断言方向跟着反过来（不是删测试）：
    立即生效 + 不留草稿 + 停用要级联边（那是修过的既有 bug，不能因为改成立即生效就丢掉）。
    """
    print("\n== H 停用/启用/归档/删除 立即生效 ==")
    nid = f"{TAG}:industry-now"
    st, _ = jcall("POST", "/v1/kg/nodes",
                  body={"id": nid, "type": "industry", "name": f"{TAG}状态动作行业",
                        "region": "CN", "source_system": "MANUAL", "source_id": "zz",
                        "source_url": "http://x", "license": "none",
                        "status": "published"})
    st, d = jcall("POST", "/v1/admin/publish/node", {"node_id": nid})
    check("H1 造一个已发布的行业当靶子", st == 200 and d.get("status") == "published",
          f"HTTP {st}")
    q = {"type": "industry", "q": TAG, "page_size": "50"}
    check("H2 前台看得到它", f"{TAG}状态动作行业" in call("GET", "/v1/kg/nodes", q)[1])

    # ① 停用 → 立即
    st, d = jcall("POST", "/v1/admin/changes",
                  body={"entity_kind": "node", "action": "disable",
                        "target_id": nid, "dim_type": "industry"})
    ap = (d or {}).get("applied") or {}
    check("H3 停用立即生效（applied.status=disabled，不是 draft）",
          st == 200 and ap.get("status") == "disabled",
          f"HTTP {st} {json.dumps(ap, ensure_ascii=False)[:140]}")
    check("H4 停用后前台当场看不到", f"{TAG}状态动作行业" not in call("GET", "/v1/kg/nodes", q)[1])
    st, d = jcall("GET", "/v1/admin/drafts", {"q": TAG, "page_size": "50"})
    check("H5 停用没有留草稿",
          not [i for i in (d.get("items") or []) if i["node_id"] == nid], "清单里没有它")

    # ② 启用 → 立即（并且要能把停用时级联关掉的边恢复，否则门禁过不去）
    st, d = jcall("POST", "/v1/admin/changes",
                  body={"entity_kind": "node", "action": "enable",
                        "target_id": nid, "dim_type": "industry"})
    ap = (d or {}).get("applied") or {}
    check("H6 启用立即生效", st == 200 and ap.get("status") == "published",
          f"HTTP {st} {json.dumps(ap, ensure_ascii=False)[:140]}")
    check("H7 启用后前台又看得到", f"{TAG}状态动作行业" in call("GET", "/v1/kg/nodes", q)[1])

    # ③ 归档（DELETE /v1/kg/nodes）→ 立即
    st, _ = call("DELETE", "/v1/kg/nodes/" + nid)
    check("H8 归档立即生效", st == 200, f"HTTP {st}")
    check("H9 归档后前台当场看不到", f"{TAG}状态动作行业" not in call("GET", "/v1/kg/nodes", q)[1])

    # ④ 物理删除（changes delete）→ 立即，库里两行都没了
    st, d = jcall("POST", "/v1/admin/changes",
                  body={"entity_kind": "node", "action": "delete",
                        "target_id": nid, "dim_type": "industry"})
    ap = (d or {}).get("applied") or {}
    check("H10 物理删除立即执行（deleted=true + 条数）",
          st == 200 and ap.get("deleted") is True
          and (ap.get("delete_result") or {}).get("nodes_deleted") == 1,
          f"HTTP {st} {json.dumps(ap, ensure_ascii=False)[:160]}")
    from backend.kg.pg_store.client import connect

    with connect() as c:
        left = c.execute(
            "SELECT count(*) AS c FROM kg_node WHERE id=%s", (nid,)
        ).fetchone()["c"]
    check("H11 库里两行都没了", left == 0, f"剩 {left} 行")


# ── F 闸门 ───────────────────────────────────────────────────
def seg_f() -> None:
    print("\n== F 孤儿边闸门 ==")
    r = subprocess.run(
        [sys.executable, "-X", "utf8", str(ROOT / "scripts" / "check_orphan_edges.py")],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        env=dict(os.environ, PYTHONUTF8="1"),
    )
    check("F1 check_orphan_edges.py 通过", r.returncode == 0,
          (r.stdout or "").strip().splitlines()[-1] if r.stdout else (r.stderr or "")[:200])


def main() -> int:
    purge()
    f = fixture()
    print(f"靶子岗位：{f['occ']}「{f['occ_name']}」V{f['occ_version']}，"
          f"requires 边 {len(f['edges'])} 条")
    proc = start_server()
    gctx: dict[str, Any] = {}
    try:
        made = seg_a(f)
        seg_b(f, made)
        seg_c(f, made)
        seg_d(f, made)
        seg_e(f, made)
        gctx = seg_g()
        seg_h()
        seg_f()
    finally:
        _restore_composition(gctx)
        # 靶子岗位被真发布过，名字/描述/边都变了 —— 还原成测试前的样子
        from backend.kg.pg_store.client import connect

        with connect() as c:
            c.execute(
                "UPDATE kg_node SET name = %s, description = NULL, attrs = '{}', "
                "version = %s WHERE id = %s AND NOT is_draft",
                (f["occ_name"], f["occ_version"], f["occ"]),
            )
            c.execute(
                "UPDATE kg_edge SET status='published' WHERE id = %s AND NOT is_draft",
                (f["edges"][0]["id"],),
            )
            c.commit()
        purge()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    ok = sum(1 for _, p, _ in _results if p)
    print("\n" + "=" * 54)
    print(f"结果：{ok}/{len(_results)} 通过")
    for c, p, n in _results:
        if not p:
            print(f"  FAIL {c} — {n}")
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
