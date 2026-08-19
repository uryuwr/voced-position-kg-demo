"""草稿泄漏测试 —— 草稿态方案唯一的静默失效点（§10.2 / §12）。

由来
----
草稿与线上同表两行，靠一条不变量把草稿挡在前台之外：**草稿行的 `status` 恒为 `'draft'`**。
全仓约 120 处前台查询已经在过滤 `status='published'`，于是草稿自动被排除，一处都不用改。
反过来说，一旦有人把「发布后应该变成什么」写进草稿行的 `status`，草稿当场对全网可见，
而且不会报任何错 —— 页面上多出一条别人还没写完的数据，谁也不会往这上面想。

所以这条必须有自动化测试。

四段
----
A 手工插草稿           直接改库造出「已发布节点的草稿改名」「只有草稿行的新节点」「草稿边」
B 前台全接口扫描       openapi.json 里每个 GET 都打一遍，响应体里出现哨兵串就算泄漏
C 图检索               /v1/search、/v1/graph/explore 按哨兵名字搜，搜到算泄漏
D 前台快照逐字节对比    造草稿前后各取一遍前台响应，**必须完全相同**（方案 §12 的核心断言）

判定口径：哨兵串出现在任何前台响应里 = FAIL。管理台接口（scope=manage / /v1/admin/*）
**应该**看得到草稿，本测试不扫它们，另有 E 段正向断言管理台确实看得到。

运行：python -X utf8 tests/e2e_draft_leak.py
自起 18200 端口的 AUTH_DEBUG 实例（端口可用 E2E_PORT 覆盖），不影响 8088 上的正常服务。
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
HEAD = {"X-Test-Uid": "9002", "X-Test-Uname": "draftleak"}
LOG = ROOT / "tests" / "_e2e_draft_leak_server.log"

# 哨兵：只出现在草稿行里。前台任何响应里出现它都是泄漏。
SENTINEL = "ZZ草稿哨兵禁止外泄"
NEW_NODE_ID = "ZZ:draft:leak:new-occupation"

_results: list[tuple[str, bool, str]] = []


def check(case: str, ok: bool, note: str = "") -> None:
    _results.append((case, bool(ok), note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {case}" + (f" — {note}" if note else ""))


def call(method: str, path: str, params: dict | None = None, body: Any = None):
    # 路径里的中文必须先 %XX 编码：urllib 对非 ASCII 的 URL 直接抛 UnicodeEncodeError，
    # 那会被当成「连不上」计成 -1，掩盖掉真实状态码
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
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, r.read().decode("utf8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf8", "ignore")
    except Exception as e:  # noqa: BLE001
        return -1, str(e)[:200]


def start_server() -> subprocess.Popen:
    log = LOG.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(ROOT / "tests" / "_e2e_server.py"), str(PORT)],
        cwd=str(ROOT),
        env=dict(os.environ, PYTHONUTF8="1"),
        stdout=log,
        stderr=log,
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


# ── A 造草稿 ─────────────────────────────────────────────────
def purge() -> None:
    from backend.kg.pg_store.client import connect

    with connect() as c:
        c.execute("DELETE FROM kg_edge WHERE is_draft AND unit_id LIKE 'ZZ:draft:leak%'")
        c.execute(
            "DELETE FROM kg_edge WHERE src_id = %s OR dst_id = %s",
            (NEW_NODE_ID, NEW_NODE_ID),
        )
        c.execute("DELETE FROM kg_node WHERE id = %s", (NEW_NODE_ID,))
        c.execute("DELETE FROM kg_node WHERE is_draft AND name LIKE %s", (f"%{SENTINEL}%",))
        c.execute(
            "DELETE FROM kg_edge WHERE is_draft AND COALESCE(evidence,'') LIKE %s",
            (f"%{SENTINEL}%",),
        )
        c.commit()


def seed() -> dict[str, str]:
    """手工插草稿行（不经应用层，模拟最坏情况：直连改库也不能泄漏）。"""
    from backend.kg.pg_store.client import connect
    from backend.kg.pg_store.write import _COPY_NODE_TO_DRAFT

    with connect() as c:
        occ = c.execute(
            "SELECT id, name FROM kg_node WHERE type='occupation' AND NOT is_draft "
            "AND COALESCE(status,'published')='published' "
            "AND EXISTS (SELECT 1 FROM kg_edge e WHERE e.src_id = kg_node.id "
            "            AND e.rel_type='requires' AND NOT e.is_draft) "
            "ORDER BY id LIMIT 1"
        ).fetchone()
        skill = c.execute(
            "SELECT id, name, attrs::json->>'skill_key' AS skill_key FROM kg_node "
            "WHERE type='skill_level' AND NOT is_draft "
            "AND COALESCE(status,'published')='published' "
            "AND COALESCE(attrs::json->>'skill_key','') <> '' ORDER BY id LIMIT 1"
        ).fetchone()
        major = c.execute(
            "SELECT id FROM kg_node WHERE type='major' AND NOT is_draft "
            "AND COALESCE(status,'published')='published' ORDER BY id LIMIT 1"
        ).fetchone()
        assert occ and skill and major, "库里缺基础数据，先灌库"

        # ① 已发布节点的「改名草稿」：线上行保持原名，草稿行叫哨兵名
        c.execute(_COPY_NODE_TO_DRAFT, (occ["id"],))
        c.execute(
            "UPDATE kg_node SET name = %s, description = %s, target_status = 'published' "
            "WHERE id = %s AND is_draft",
            (f"{SENTINEL}·改名", f"{SENTINEL}·简介", occ["id"]),
        )
        # ①b **技能节点的改名草稿**。单独造一条是因为技能走的是另一套读路径
        #     （`list_skill_bundles` 按 skill_key 聚合 5 个档位节点），而聚合路径上
        #     一旦把 prefer_draft 用在前台口径上，就会「留下草稿行、丢掉线上行」，
        #     再被 status='published' 滤掉 —— 整个技能从学员端列表里消失。
        #     只造岗位草稿的话这条路径永远扫不到（实测就是这么漏的）。
        c.execute(_COPY_NODE_TO_DRAFT, (skill["id"],))
        c.execute(
            "UPDATE kg_node SET name = %s, description = %s WHERE id = %s AND is_draft",
            (f"{SENTINEL}·技能改名", f"{SENTINEL}·技能简介", skill["id"]),
        )
        # ② 从未发布过的新节点：只有草稿行
        c.execute(
            """
            INSERT INTO kg_node (id, region, type, name, description, attrs,
              source_system, source_id, source_url, license, fetched_at, confidence,
              status, is_draft, target_status)
            VALUES (%s,'CN','occupation',%s,%s,'{}','MANUAL','zz','manual://zz','internal',
                    '2026-01-01T00:00:00Z','manual_seed','draft',true,'published')
            ON CONFLICT (id, is_draft) DO NOTHING
            """,
            (NEW_NODE_ID, f"{SENTINEL}·新岗位", f"{SENTINEL}·新岗位简介"),
        )
        # ③ 草稿边：新岗位 requires 一个已发布技能（单元 = 新岗位）
        c.execute(
            """
            INSERT INTO kg_edge (id, src_id, dst_id, rel_type, region, weight, evidence,
              attrs, source_system, source_url, license, fetched_at, confidence,
              status, is_draft, target_status, unit_id)
            VALUES (%s,%s,%s,'requires','CN',1.0,%s,'{}','MANUAL','manual://zz','internal',
                    '2026-01-01T00:00:00Z','manual_seed','draft',true,'published',%s)
            ON CONFLICT (id, is_draft) DO NOTHING
            """,
            (
                f"edge:{NEW_NODE_ID}|requires|{skill['id']}",
                NEW_NODE_ID,
                skill["id"],
                f"{SENTINEL}·草稿边",
                NEW_NODE_ID,
            ),
        )
        # ④ 已发布岗位上的「新增技能」草稿边（单元 = 该岗位）
        c.execute(
            """
            INSERT INTO kg_edge (id, src_id, dst_id, rel_type, region, weight, evidence,
              attrs, source_system, source_url, license, fetched_at, confidence,
              status, is_draft, target_status, unit_id)
            VALUES (%s,%s,%s,'requires','CN',0.9,%s,'{}','MANUAL','manual://zz','internal',
                    '2026-01-01T00:00:00Z','manual_seed','draft',true,'published',%s)
            ON CONFLICT (id, is_draft) DO NOTHING
            """,
            (
                f"edge:{occ['id']}|requires|{skill['id']}|zzdraft",
                occ["id"],
                skill["id"],
                f"{SENTINEL}·加边",
                occ["id"],
            ),
        )
        # ⑤ 专业挂新岗位的草稿边（跨类型，两端一个已发布一个未发布）
        c.execute(
            """
            INSERT INTO kg_edge (id, src_id, dst_id, rel_type, region, evidence,
              attrs, source_system, source_url, license, fetched_at, confidence,
              status, is_draft, target_status, unit_id)
            VALUES (%s,%s,%s,'prepares_for','CN',%s,'{}','MANUAL','manual://zz','internal',
                    '2026-01-01T00:00:00Z','manual_seed','draft',true,'published',%s)
            ON CONFLICT (id, is_draft) DO NOTHING
            """,
            (
                f"edge:{major['id']}|prepares_for|{NEW_NODE_ID}",
                major["id"],
                NEW_NODE_ID,
                f"{SENTINEL}·专业挂新岗位",
                major["id"],
            ),
        )
        c.commit()
    return {
        "occ": occ["id"],
        "occ_name": occ["name"],
        "skill": skill["id"],
        "skill_name": skill["name"],
        "skill_key": skill["skill_key"] or skill["name"],
        "major": major["id"],
    }


# ── B 前台全接口扫描 ─────────────────────────────────────────
# 管理台**应该**看得见草稿，不参与泄漏判定。按 tag 认（`管理台 · …`）比按路径前缀准，
# 但路径前缀这一层也留着：openapi 里有历史遗留的无 tag 路由。
_ADMIN_MARKS = ("/v1/admin/", "/v1/review/", "/v1/kg/node-detail")
_ADMIN_TAG = "管理台"

# 这几个响应本身就随时间/环境变（版本号、网关探活、时间戳），不参与「逐字节相同」比较。
# **只能按这个清单排除，不能靠手写一份前台清单**：手写清单漏掉的那个接口就是泄漏点本身。
_UNSTABLE_FOR_SNAPSHOT = (
    "/health",
    "/v1/config",
    "/v1/ai/",
    "/openapi.json",
    "/v1/student/me",          # 积分/徽章带 updated_at
    "/v1/student/diagnosis/resume/sample",
)


def frontend_calls(ids: dict[str, str]) -> list[tuple[str, dict]]:
    """**按真实路由表**摘出前台 GET 并生成调用参数（不传 scope=manage）。

    不手写接口清单：新增一个前台接口如果没进扫描范围，它就是下一个泄漏点，
    而这类泄漏不报错、不崩，只是草稿数据出现在学员端（方案 §10.2）。
    """
    spec = json.loads(urllib.request.urlopen(BASE + "/openapi.json", timeout=30).read())
    subs = {
        "session_id": "999999999",
        "proposal_id": "999999999",
        "change_id": "999999999",
        "step_id": "1",
    }
    fill = {
        "id": ids["occ"],
        "node_id": ids["occ"],
        "occupation_id": ids["occ"],
        "position_id": ids["occ"],
        "major_id": ids["major"],
        "profession_id": ids["major"],
        "industry_id": "",
        "skill_id": ids["skill"],
        "src_id": ids["major"],
        "dst_id": ids["occ"],
        "q": "*",
        "name": "",
        "skill_key": "",
        "type": None,
    }
    out: list[tuple[str, dict]] = []
    for path, ops in spec["paths"].items():
        op = ops.get("get")
        if not op:
            continue
        tags = op.get("tags") or []
        if any(m in path for m in _ADMIN_MARKS) or any(
            str(t).startswith(_ADMIN_TAG) for t in tags
        ):
            continue          # 管理台本就该看得到草稿
        concrete = path
        for k, v in subs.items():
            concrete = concrete.replace("{%s}" % k, v)
        for k, v in (
            ("{node_id:path}", ids["occ"]),
            ("{profession_id:path}", ids["major"]),
            ("{position_id:path}", ids["occ"]),
            ("{skill_key:path}", "安全"),
            ("{skill_key}", "安全"),
            ("{prereq_key:path}", "安全"),
        ):
            concrete = concrete.replace(k, v)
        if "{" in concrete:
            continue
        params: dict[str, Any] = {}
        for p in op.get("parameters", []):
            if p.get("in") != "query":
                continue
            n = p["name"]
            if n in ("page_size", "limit", "max_nodes"):
                params[n] = "200"
            elif n in ("include_counts", "include_links", "include_skills", "aggregate"):
                # 这些开关会走到另一批查询上（关联计数、link_ids、聚合），
                # 默认 false 的话那批查询根本没被扫到 —— 上次就是这么漏掉
                # `/v1/node?include_links=1` 泄漏草稿关联 id 的
                params[n] = "1"
            elif n in fill and fill[n]:
                params[n] = fill[n]
            elif p.get("required"):
                params[n] = (
                    "1" if p.get("schema", {}).get("type") in ("integer", "number") else "*"
                )
        out.append((concrete, params))
        # 关键列表接口再补几组会展开更多数据的组合
        if path in ("/v1/kg/nodes", "/v1/kg/edges"):
            for t in ("occupation", "major", "skill_level", "industry"):
                out.append((concrete, {**params, "type": t, "page_size": "200"}))
    return out


def seg_b(ids: dict[str, str]) -> None:
    print("\n== B 前台全部 GET 接口都不得出现草稿 ==")
    calls = frontend_calls(ids)
    leaks, errs, n = [], [], 0
    for path, params in calls:
        st, body = call("GET", path, params)
        n += 1
        if st >= 500 or st == -1:
            errs.append(f"{st} {path} {params}")
        # 除了哨兵名字，还要盯**只有草稿行的新节点 id**：link_ids / 关联计数这类响应里
        # 只有 id 没有名字，光扫名字抓不到（`/v1/node?include_links=1` 就漏过一次）
        if SENTINEL in body or NEW_NODE_ID in body:
            leaks.append(f"{path} {params}")
    check(
        f"B1 {n} 次前台调用无草稿泄漏", not leaks,
        "; ".join(dict.fromkeys(leaks))[:600] if leaks else f"{n} 次响应均无哨兵串",
    )
    check(
        f"B2 造了草稿之后前台接口不 5xx（{n} 次）", not errs,
        "; ".join(dict.fromkeys(errs))[:400] if errs else "全部 <500",
    )


# ── C 图检索 ─────────────────────────────────────────────────
def seg_c(ids: dict[str, str]) -> None:
    print("\n== C 图检索 / 探索搜不到草稿 ==")
    probes = [
        ("/v1/search", {"q": SENTINEL, "limit": "50"}),
        ("/v1/search", {"q": "ZZ草稿", "limit": "50"}),
        ("/v1/graph/explore", {"q": SENTINEL, "depth": "2", "max_nodes": "200"}),
        ("/v1/graph/explore", {"q": "*", "type": "occupation", "depth": "1", "max_nodes": "500"}),
        ("/v1/nodes", {"q": SENTINEL, "page_size": "200"}),
        ("/v1/kg/nodes", {"type": "occupation", "q": "ZZ草稿", "page_size": "200"}),
        ("/v1/kg/edges", {"node_id": NEW_NODE_ID, "page_size": "200"}),
        ("/v1/node", {"id": NEW_NODE_ID}),
        ("/v1/nodes/" + urllib.parse.quote(NEW_NODE_ID, safe=""), None),
        ("/v1/expand", {"node_id": NEW_NODE_ID, "limit": "50"}),
        ("/v1/occupations/skills", {"occupation_id": ids["occ"], "limit": "200"}),
        ("/v1/majors/occupations", {"major_id": ids["major"], "limit": "200"}),
        ("/v1/student/positions", {"q": "ZZ草稿", "page_size": "100"}),
        ("/v1/student/positions/skill-composition", {"id": ids["occ"]}),
    ]
    leaks = []
    for p, q in probes:
        st, body = call("GET", p, q)
        if SENTINEL in body:
            leaks.append(f"{p} {q}")
    check(
        f"C1 {len(probes)} 个检索/点查路径搜不到草稿", not leaks,
        "; ".join(leaks)[:500] if leaks else "均未命中",
    )
    # 未发布的新节点按 id 直查必须 404 / 空
    st, body = call("GET", "/v1/nodes/" + urllib.parse.quote(NEW_NODE_ID, safe=""))
    check(
        "C2 只有草稿行的新节点按 id 点查不可见", st == 404 or SENTINEL not in body,
        f"HTTP {st}",
    )
    # 技能草稿**不能让这个技能从学员端消失**：聚合读路径上误用 prefer_draft
    # 会「留草稿行、丢线上行」，再被 published 滤掉，整条技能不见了（实测过）
    # page_size 上限 100（le=100），传 200 会 422 —— 别把参数错误当成「技能不见了」
    st, body = call(
        "GET", "/v1/student/skills", {"page_size": "100", "q": ids["skill_key"]}
    )
    check(
        "C4 技能有草稿时，学员端技能库里它还在（且是原名）",
        st == 200 and ids["skill_key"] in body and SENTINEL not in body,
        f"HTTP {st} 期望包含「{ids['skill_key']}」",
    )
    st, body = call(
        "GET", "/v1/student/skills/bundles/" + urllib.parse.quote(ids["skill_key"], safe="")
    )
    check(
        "C5 技能 bundle 详情不返回草稿内容",
        st in (200, 404) and SENTINEL not in body,
        f"HTTP {st}",
    )
    # 线上行仍然是原名（编辑期间前台不空、不变）
    st, body = call("GET", "/v1/node", {"id": ids["occ"]})
    check(
        "C3 被编辑的节点在前台仍是原名（记录不会消失）",
        st == 200 and ids["occ_name"] in body and SENTINEL not in body,
        f"HTTP {st} 期望名「{ids['occ_name']}」",
    )


# ── D 前台快照逐字节对比 ─────────────────────────────────────
_SNAPSHOT_PATHS: list[tuple[str, dict]] = []


def snapshot(ids: dict[str, str]) -> dict[str, str]:
    """把前台每个 GET 的响应原文抓一份。

    清单同样来自**路由表**（`frontend_calls`），不手写 —— 这样新增接口自动进对比范围。
    """
    global _SNAPSHOT_PATHS
    if not _SNAPSHOT_PATHS:
        _SNAPSHOT_PATHS = [
            (p, q)
            for p, q in frontend_calls(ids)
            if not any(u in p for u in _UNSTABLE_FOR_SNAPSHOT)
        ]
    out = {}
    for p, q in _SNAPSHOT_PATHS:
        st, body = call("GET", p, q)
        out[f"{p}?{urllib.parse.urlencode(q)}"] = f"{st}:{body}"
    return out


def seg_d(before: dict[str, str], after: dict[str, str]) -> None:
    print("\n== D 造草稿前后前台响应逐字节相同 ==")
    diff = [k for k in before if before[k] != after.get(k)]
    check(
        f"D1 路由表摘出的 {len(before)} 个前台响应在造草稿前后逐字节相同", not diff,
        "变了：" + "; ".join(diff)[:400] if diff else "逐字节相同",
    )


# ── E 管理台**应该**看得到 ───────────────────────────────────
def seg_e(ids: dict[str, str]) -> None:
    print("\n== E 管理台看得到草稿，且同一记录只有一行 ==")
    st, body = call("GET", "/v1/kg/nodes", {"type": "occupation", "scope": "manage",
                                           "q": "ZZ草稿", "page_size": "50"})
    ok = st == 200 and SENTINEL in body
    check("E1 管理台列表能看到草稿", ok, f"HTTP {st}")
    if ok:
        data = json.loads(body)
        rows = [i for i in data["items"] if i["id"] == ids["occ"]]
        check("E2 被编辑的记录在管理台列表里只有一行", len(rows) == 1, f"{len(rows)} 行")
        if rows:
            check(
                "E3 该行 record_status=draft 且带 target_status",
                rows[0].get("record_status") == "draft"
                and rows[0].get("has_draft") is True,
                f"record_status={rows[0].get('record_status')} has_draft={rows[0].get('has_draft')}",
            )
    st, body = call("GET", "/v1/kg/node-detail", {"id": ids["occ"]})
    ok = st == 200
    d = json.loads(body) if ok else {}
    check(
        "E4 管理台详情返回 published{} + draft{} + record_status",
        ok
        and d.get("record_status") == "draft"
        and isinstance(d.get("draft"), dict)
        and isinstance(d.get("published"), dict)
        and SENTINEL in (d.get("draft") or {}).get("name", "")
        and SENTINEL not in ((d.get("published") or {}).get("name") or ""),
        f"HTTP {st} record_status={d.get('record_status')}",
    )
    st, body = call("GET", "/v1/admin/drafts", {"page_size": "200"})
    ok = st == 200 and NEW_NODE_ID in body
    check("E5 待发布清单里能看到这批草稿单元", ok, f"HTTP {st}")
    if ok:
        items = json.loads(body)["items"]
        new_row = next((i for i in items if i["node_id"] == NEW_NODE_ID), None)
        occ_row = next((i for i in items if i["node_id"] == ids["occ"]), None)
        check(
            "E6 新建单元标 is_new，改边单元统计到 edges_upsert",
            bool(new_row and new_row["is_new"]) and bool(occ_row and occ_row["edges_upsert"] >= 1),
            f"new={new_row and new_row['is_new']} occ_edges={occ_row and occ_row['edges_upsert']}",
        )


def main() -> int:
    purge()
    proc = start_server()
    try:
        from backend.kg.pg_store.client import connect

        with connect() as c:
            occ = c.execute(
                "SELECT id, name FROM kg_node WHERE type='occupation' AND NOT is_draft "
                "AND COALESCE(status,'published')='published' "
                "AND EXISTS (SELECT 1 FROM kg_edge e WHERE e.src_id = kg_node.id "
                "            AND e.rel_type='requires' AND NOT e.is_draft) "
                "ORDER BY id LIMIT 1"
            ).fetchone()
            skill = c.execute(
                "SELECT id FROM kg_node WHERE type='skill_level' AND NOT is_draft "
                "AND COALESCE(status,'published')='published' ORDER BY id LIMIT 1"
            ).fetchone()
            major = c.execute(
                "SELECT id FROM kg_node WHERE type='major' AND NOT is_draft "
                "AND COALESCE(status,'published')='published' ORDER BY id LIMIT 1"
            ).fetchone()
        pre_ids = {
            "occ": occ["id"], "occ_name": occ["name"],
            "skill": skill["id"], "major": major["id"],
        }
        print("\n== A 造草稿（直连改库，绕过应用层）==")
        before = snapshot(pre_ids)
        ids = seed()
        check("A1 草稿行已写入", True, f"单元：{ids['occ']} / {NEW_NODE_ID}")
        seg_b(ids)
        seg_c(ids)
        seg_d(before, snapshot(ids))
        seg_e(ids)
    finally:
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
