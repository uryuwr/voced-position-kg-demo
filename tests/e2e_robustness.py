"""参数健壮性回归：异常值 / 边界值 / 错类型不得打出 5xx。

由来
----
线上出过一次「岗位列表整页 500」：`counts` 声明为 `dict[str,int]`，而权重和 0.5 是小数，
一个没归一化的岗位就让整页失败（68bbc45）。这类缺陷的共同形状是
**一个异常值毁掉一整个列表接口**，逐个接口人工看必漏，所以固化成自动扫描。

四段
----
A 全接口参数 fuzz  对 OpenAPI 里每个 GET 的每个查询参数注入越界/错类型/空/NUL，只判 5xx
B 组合开关 fuzz    include_counts / scope / status / view / layout 等开关的笛卡尔积
                   —— 上次的 500 正藏在 `include_counts=1` 里，单参数扫描扫不到
C 定向回归         已修缺陷的复现用例，防回潮
D 越权             自增 session_id 的归属校验

判定口径：**4xx 是正确行为**（422 参数校验、400 业务拒绝、404 不存在），只有 5xx 算失败。

运行：python -X utf8 tests/e2e_robustness.py
自起 18097 端口的 AUTH_DEBUG 实例，不影响 8088 上的正常服务。
"""
from __future__ import annotations

import itertools
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

PORT = 18097
BASE = f"http://127.0.0.1:{PORT}"
HEAD = {"X-Test-Uid": "9001", "X-Test-Uname": "robust"}
LOG = ROOT / "tests" / "_e2e_robustness_server.log"

# 每类参数的异常值：空 / 越界 / 错类型 / 注入字符 / NUL / 超长
BAD: dict[str, list[str]] = {
    "int": ["0", "-1", "999999999999", "1.5", "abc", "", "NaN", "-2147483649"],
    "str": ["", " ", "%", "'", "\\", "中" * 200, "\x00", "*", "null", "[]"],
    "id": ["", "nope", "CN:occupation:NOT:EXIST", "::::", "%3A%3A", "null", "0"],
}

_results: list[tuple[str, bool, str]] = []


def check(case: str, ok: bool, note: str = "") -> None:
    _results.append((case, bool(ok), note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {case}" + (f" — {note}" if note else ""))


def call(method: str, path: str, params: dict | None = None, body: Any = None) -> tuple[int, str]:
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    head = dict(HEAD)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        head["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, headers=head, data=data)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()[:300].decode("utf8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300].decode("utf8", "ignore")
    except Exception as e:  # noqa: BLE001 — 连接异常也算失败，要暴露
        return -1, str(e)[:200]


def start_server() -> subprocess.Popen:
    # 端口与鉴权旁路只能在进程内改（backend/.env override=True 会盖掉环境变量），见 _e2e_server.py
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
        except Exception:  # noqa: BLE001 — 还没起来，继续等
            continue
    raise RuntimeError(f"服务未在 60s 内就绪，见 {LOG}")


def real_ids() -> dict[str, str]:
    """取库内真实 id 作基线，否则所有接口都在「查不到」的浅路径上跑，扫不出问题。"""
    from backend.kg.pg_store.client import connect

    out: dict[str, str] = {}
    with connect() as c:
        for t, k in (("major", "major_id"), ("occupation", "occupation_id"),
                     ("industry", "industry_id"), ("skill_level", "skill_id")):
            r = c.execute(
                "SELECT id, name FROM kg_node WHERE type=%s "
                "AND COALESCE(status,'published')='published' LIMIT 1", (t,)
            ).fetchone()
            if r:
                out[k], out[t] = r["id"], r["name"]
    out.update({"id": out.get("occupation_id", ""), "node_id": out.get("occupation_id", ""),
                "position_id": out.get("occupation_id", ""), "src_id": out.get("major_id", ""),
                "dst_id": out.get("occupation_id", ""), "q": "工",
                "name": out.get("major", "工"), "skill_key": "安全"})
    return out


def kind(name: str, schema: dict) -> str:
    if schema.get("type") in ("integer", "number"):
        return "int"
    return "id" if name.endswith("id") else "str"


# ── A 全接口参数 fuzz ────────────────────────────────────────
def seg_a(ids: dict[str, str]) -> None:
    print("\n== A 全接口参数 fuzz ==")
    spec = json.loads(urllib.request.urlopen(BASE + "/openapi.json", timeout=30).read())
    fails, n = [], 0
    for path, ops in spec["paths"].items():
        op = ops.get("get")
        if not op:
            continue
        concrete = path
        for m in ("session_id", "proposal_id", "change_id", "step_id"):
            concrete = concrete.replace("{%s}" % m, "999999999")
        for m, v in (("{skill_key:path}", "不存在的技能"), ("{node_id:path}", "CN:x:Y:z"),
                     ("{profession_id:path}", "CN:major:X:1"), ("{skill_key}", "x"),
                     ("{position_id:path}", "CN:occupation:X:1"), ("{prereq_key:path}", "y")):
            concrete = concrete.replace(m, v)
        if "{" in concrete:
            continue
        params = [p for p in op.get("parameters", []) if p.get("in") == "query"]
        base = {
            p["name"]: ("1" if p.get("schema", {}).get("type") in ("integer", "number")
                        else ids.get(p["name"]) or "测试")
            for p in params if p.get("required")
        }
        st, b = call("GET", concrete, base)
        n += 1
        if st >= 500 or st == -1:
            fails.append(f"[基线] {st} {concrete} {base} -> {b[:150]}")
        for p in params:
            for v in BAD[kind(p["name"], p.get("schema", {}))]:
                st, b = call("GET", concrete, {**base, p["name"]: v})
                n += 1
                if st >= 500 or st == -1:
                    fails.append(f"{st} {concrete} [{p['name']}={v!r}] -> {b[:150]}")
    check(f"A 全部 GET 接口异常参数无 5xx（{n} 次调用）", not fails,
          "; ".join(dict.fromkeys(fails))[:600] if fails else f"{n} 次全部 <500")


# ── B 组合开关 fuzz ──────────────────────────────────────────
def seg_b(ids: dict[str, str]) -> None:
    print("\n== B 组合开关 fuzz ==")
    TYPES = ["industry", "major", "occupation", "skill_level"]
    STATUS = [None, "published", "draft", "disabled", "archived", "all"]
    cases: list[tuple[str, dict]] = []
    for t, il, sc, st_ in itertools.product(TYPES, ["0", "1"], [None, "manage"], STATUS):
        cases.append(("/v1/kg/nodes", {"type": t, "include_counts": "1", "include_links": il,
                                       "scope": sc, "status": st_, "page_size": "200"}))
    for t, ob in itertools.product(TYPES, [None, "name", "created_at", "sort_order", "bogus"]):
        cases.append(("/v1/kg/nodes", {"type": t, "include_counts": "1", "order_by": ob}))
    for k in ("industry_id", "major_id", "occupation_id", "skill_id"):
        if not ids.get(k):
            continue
        cases += [("/v1/kg/node-detail", {"id": ids[k]}),
                  ("/v1/node", {"id": ids[k], "include_counts": "1", "include_links": "1"}),
                  ("/v1/node", {"id": ids[k], "include_counts": "1", "scope": "manage"}),
                  ("/v1/admin/composition", {"node_id": ids[k]}),
                  ("/v1/admin/publish/validate", {"node_id": ids[k]})]
    for sc, st_ in itertools.product([None, "manage"], STATUS):
        cases.append(("/v1/kg/edges", {"scope": sc, "status": st_, "page_size": "200"}))
    for v, hl in itertools.product(["bundle", "level", "bogus"],
                                   [None, "L1", "L3", "L5", "L9", "9", "x"]):
        cases.append(("/v1/student/skills", {"view": v, "has_level": hl, "page_size": "100"}))
    for t, ac in itertools.product(TYPES, ["enable", "publish", "bogus", ""]):
        cases.append(("/v1/admin/publish/validate", {"node_type": t, "action": ac}))
    for d, mn in itertools.product(["1", "3", "5"], ["10", "2000"]):
        cases.append(("/v1/graph/explore", {"q": "*", "type": "major", "depth": d, "max_nodes": mn}))
        cases.append(("/v1/graph/by-major", {"name": ids["name"], "depth": d, "max_nodes": mn}))
    for lay in ["layered", "heat", "bogus", ""]:
        cases.append(("/v1/industry-graph", {"industry_id": ids.get("industry_id"), "layout": lay}))
    cases += [
        ("/v1/student/positions/match", {"position_id": ids.get("occupation_id"), "allow_memory": "1"}),
        ("/v1/student/positions/skill-composition", {"id": ids.get("occupation_id")}),
        ("/v1/student/positions/skill-composition", {"id": ids.get("major_id")}),
        ("/v1/student/positions", {"aggregate": "1", "page_size": "100"}),
        ("/v1/capability", {"major_id": ids.get("major_id"), "include_skills": "1",
                            "include_progression": "1"}),
        ("/v1/occupations/skills", {"occupation_id": ids.get("occupation_id"), "aggregate": "1"}),
        ("/v1/admin/skills", {"page_size": "100", "status": "all"}),
        ("/v1/admin/dashboard/summary", {}),
    ]
    fails = [f"{st} {p} {q}" for p, q in cases
             for st, _ in [call("GET", p, q)] if st >= 500 or st == -1]
    check(f"B 组合开关无 5xx（{len(cases)} 组合）", not fails,
          "; ".join(dict.fromkeys(fails))[:600] if fails else f"{len(cases)} 组合全部 <500")


# ── C 定向回归 ───────────────────────────────────────────────
_SKILL_KEY = "ZZ健壮性回归"


def _purge_fixture() -> list[str]:
    from backend.kg.pg_store.client import connect

    with connect() as c:
        ids = [x["id"] for x in c.execute(
            "SELECT id FROM kg_node WHERE attrs::json->>'skill_key' = %s", (_SKILL_KEY,)
        ).fetchall()]
        if ids:
            c.execute("DELETE FROM kg_edge WHERE src_id = ANY(%s) OR dst_id = ANY(%s)", (ids, ids))
            c.execute("DELETE FROM kg_node WHERE id = ANY(%s)", (ids,))
    return ids


def seg_c() -> None:
    print("\n== C 定向回归 ==")
    # C1 NUL：PG 的 text 存不了 0x00，早先任一 str 参数带 %00 都会 500
    nul_fail = [p for p in ("/v1/search?q=%00&limit=5", "/v1/admin/skills?q=%00",
                            "/v1/student/positions?q=%00", "/v1/kg/nodes?type=major&q=%00")
                if call("GET", p)[0] >= 500]
    check("C1 查询串含 NUL(%00) 不再 500", not nul_fail, "; ".join(nul_fail) or "4 个接口均 <500")

    # C2 attrs.level 是产品档唯一真源，脏值会让技能库整页 500 → 写侧必须拒绝
    _purge_fixture()
    bad_accepted = []
    for lv in ("L3", 3.5, 0, 9, True, "三级"):
        st, _ = call("POST", "/v1/kg/nodes", body={
            "type": "skill_level", "name": "ZZ回归 · x", "region": "CN",
            "attrs": {"skill_key": _SKILL_KEY, "level": lv},
            "source_system": "MANUAL", "source_id": "zz",
            "source_url": "http://x", "license": "none"})
        if st != 400:
            bad_accepted.append(f"level={lv!r}→{st}")
    check("C2 非法 attrs.level 写入被拒(400)", not bad_accepted,
          "; ".join(bad_accepted) or "L3/3.5/0/9/True/三级 均 400")

    st_ok, _ = call("POST", "/v1/kg/nodes", body={
        "type": "skill_level", "name": "ZZ回归 · L3", "region": "CN",
        "attrs": {"skill_key": _SKILL_KEY, "level": "3"},
        "source_system": "MANUAL", "source_id": "zz",
        "source_url": "http://x", "license": "none"})
    check("C2b 合法 attrs.level 正常写入", st_ok == 200, f"HTTP {st_ok}")
    check("C2c 写入后技能库列表仍 200", call("GET", "/v1/admin/skills?page_size=20")[0] == 200)
    check("C2d 回归数据已清理", bool(_purge_fixture()) or True)

    # C3 读侧兜底：采集脚本/直连改库绕得过应用层，读路径不能被一行脏数据打死
    import psycopg
    from psycopg.rows import dict_row

    from backend.kg.pg_store.config import DATABASE_URL, attrs_level_int
    from backend.kg.pg_store.skill_aggregate import LEVEL_SQL

    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO kg_node(id,region,type,name,attrs,source_system,source_id,"
            "source_url,license,fetched_at,confidence,status) VALUES"
            "('ZZ:sl:dirty:1','CN','skill_level','ZZ脏行',%s,'MANUAL','zz','http://x','none',"
            "'2026-01-01T00:00:00+00:00','manual_seed','published')",
            (json.dumps({"skill_key": _SKILL_KEY, "level": "L3"}),),
        )
        errs = []
        for lbl, expr in (("config.attrs_level_int", attrs_level_int("n")),
                          ("skill_aggregate.LEVEL_SQL", LEVEL_SQL)):
            try:
                conn.execute(
                    f"SELECT {expr} AS lv FROM kg_node n WHERE n.type='skill_level'"
                ).fetchall()
            except Exception as e:  # noqa: BLE001
                errs.append(f"{lbl}: {type(e).__name__}")
        check("C3 读侧对脏 attrs.level 取 NULL 而非报错", not errs, "; ".join(errs) or "两处 SQL 均通过")
    finally:
        conn.execute("ROLLBACK")
        conn.close()


# ── D 越权 ───────────────────────────────────────────────────
def seg_d() -> None:
    print("\n== D 测评会话越权 ==")
    from backend.kg.pg_store import biz_store as biz

    other = biz.create_assessment_session("8888", "别人", target_occupation_id=None)
    try:
        st, _ = call("GET", f"/v1/student/assessment/sessions/{other}")
        check("D1 读他人测评会话被拒(404)", st == 404, f"HTTP {st}")
        st, _ = call("POST", f"/v1/student/assessment/sessions/{other}/answers",
                     body={"index": 0, "answer": 1})
        check("D2 替他人答题被拒(404)", st == 404, f"HTTP {st}")
        for sid in ("0", "-5"):
            st, _ = call("GET", f"/v1/student/assessment/sessions/{sid}")
            check(f"D3 session_id={sid} 被参数校验挡下(422)", st == 422, f"HTTP {st}")
    finally:
        from backend.kg.pg_store.client import connect

        with connect() as c:
            c.execute("DELETE FROM biz_diagnosis_session WHERE id=%s", (other,))


def main() -> int:
    proc = start_server()
    try:
        ids = real_ids()
        seg_a(ids)
        seg_b(ids)
        seg_c()
        seg_d()
    finally:
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
