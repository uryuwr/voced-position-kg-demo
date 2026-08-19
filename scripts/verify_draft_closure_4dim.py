"""四个管理维度的草稿闭环：编辑 → 进草稿 → 前台仍是旧值 → 发布 → 前台是新值。

`verify_draft_closure.py` 只覆盖「岗位技能构成」这一条路径。这个脚本按**维度**
横着走一遍：行业 / 专业 / 岗位 / 技能，每个维度都要过同一组断言。

跑在 8088 连的那个库上（`backend/.env` 的 DATABASE_URL），用 in-process
TestClient —— 与 8088 同一份代码、同一个库，区别只是不经过网络与 UC 鉴权
（8088 上 AUTH_BYPASS=0，学员端接口一律 401，脚本进不去）。

**每个维度测完都会还原**：把描述改回原值再发布一次，最后核对前台快照与最初
逐字节一致。共享库上跑，不还原就是留脏数据。

    PYTHONPATH=. python -X utf8 scripts/verify_draft_closure_4dim.py
    PYTHONPATH=. python -X utf8 scripts/verify_draft_closure_4dim.py --dim skill
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import warnings
from typing import Any
from urllib.parse import quote

warnings.filterwarnings("ignore")
import backend.settings as _st  # noqa: E402

_st.AUTH_BYPASS = True
_st.AUTH_DEBUG = True
import backend.api.auth as _au  # noqa: E402

_au.AUTH_BYPASS = True
_au.AUTH_DEBUG = True
from fastapi.testclient import TestClient  # noqa: E402

from backend.api.main import app  # noqa: E402
from backend.kg.pg_store.client import connect  # noqa: E402
from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL  # noqa: E402

cli = TestClient(
    app,
    headers={"X-Test-Uid": "0", "X-Test-Uname": "closure4"},
    raise_server_exceptions=False,
)

MARK = "【草稿闭环探针】"
RESULTS: list[tuple[bool, str, str]] = []
_SPEC = app.openapi()


def check(ok: bool, name: str, detail: str = "") -> bool:
    RESULTS.append((ok, name, detail))
    print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def front_snap(subject_id: str) -> dict[str, str]:
    """所有 tag 以「前台」开头的 GET 接口的响应指纹。

    路径/必填 query 里的 id 一律填成被测对象，这样「该对象的详情」也进快照 ——
    只扫列表接口的话，改一条记录的描述在分页之外就看不出来，断言会假绿。
    """
    q = quote(subject_id, safe="")
    out: dict[str, str] = {}
    for path, ops in (_SPEC.get("paths") or {}).items():
        op = (ops or {}).get("get")
        if not op or not any(str(t).startswith("前台") for t in (op.get("tags") or [])):
            continue
        url, qs, invented = path, [], False
        for p in op.get("parameters") or []:
            if p.get("in") == "path":
                url = url.replace("{" + p["name"] + "}", q)
            elif p.get("required") and p.get("in") == "query":
                nm, sch = p["name"], (p.get("schema") or {})
                if "id" in nm.lower():
                    v: Any = subject_id
                else:
                    # 必填但与被测对象无关的参数（典型是检索接口的 q），只能瞎填。
                    # 这种接口返回的是**全局检索结果**，别的记录一变它就变 ——
                    # 共享库上有人同时在编辑，拿它做「逐字节不变」会稳定假红。
                    # 踩过一次：/v1/occupation/requires?q=a 命中一堆岗位，
                    # 被判成「还原后不一致」，其实是别人在改别的数据。
                    invented = True
                    v = 1 if sch.get("type") == "integer" else "a"
                qs.append(f"{nm}={quote(str(v), safe='')}")
        if invented:
            continue          # 仍会被 front_has_mark 扫到，泄漏检查不受影响
        full = url + ("?" + "&".join(qs) if qs else "")
        r = cli.get(full)
        if r.status_code < 500:
            out[full] = hashlib.sha256(r.content).hexdigest()[:16]
    return out


def front_has_mark(subject_id: str) -> bool:
    """前台任意接口的响应里出现探针字样 —— 即「草稿泄漏到学员端」。"""
    q = quote(subject_id, safe="")
    for path, ops in (_SPEC.get("paths") or {}).items():
        op = (ops or {}).get("get")
        if not op or not any(str(t).startswith("前台") for t in (op.get("tags") or [])):
            continue
        url = path
        for p in op.get("parameters") or []:
            if p.get("in") == "path":
                url = url.replace("{" + p["name"] + "}", q)
        if "{" in url:
            continue
        r = cli.get(url, params={"id": subject_id} if "id=" not in url else None)
        if r.status_code < 500 and MARK in r.text:
            return True
    return False


def diff_count(a: dict[str, str], b: dict[str, str]) -> list[str]:
    return sorted(k for k in a if a.get(k) != b.get(k))


def admin_item(node_id: str, ntype: str, name: str = "") -> dict[str, Any]:
    """管理台看到的这条记录。

    **没有 `GET /v1/kg/nodes/{id}`**（只有 PATCH / DELETE），管理台详情是从列表
    接口带 scope=manage 拿的。照着 PATCH 的路径拼 GET 会 404，而 404 的响应体
    是 `{"detail": ...}`，断言「新值在不在响应里」会稳定地判假 —— 看起来像
    「编辑没生效」，其实是探针打错了地址。
    """
    # `q` 匹配名称、**不匹配 id**（拿 id 去搜 total=0）。所以按名字搜，
    # 再在结果里按 id 认人 —— 同名记录不止一条，只靠名字会认错。
    for params in (
        {"type": ntype, "scope": "manage", "q": name, "limit": 50},
        {"type": ntype, "scope": "manage", "status": "draft", "limit": 200},
    ):
        r = cli.get("/v1/kg/nodes", params=params)
        if r.status_code != 200:
            continue
        body = r.json()
        for it in (body.get("items") if isinstance(body, dict) else body) or []:
            if it.get("id") == node_id:
                return it
    return {}


def why_differ(a: dict[str, str], b: dict[str, str], subject: str) -> str:
    """哪些接口、哪些字段变了 —— 只报差异，不猜原因。"""
    ks = diff_count(a, b)
    if not ks:
        return ""
    out = []
    for k in ks[:3]:
        r = cli.get(k)
        try:
            txt = json.dumps(r.json(), ensure_ascii=False)
        except Exception:
            txt = r.text
        hits = [f for f in ("version", "updated_by", "updated_at", "weight_sum",
                            "counts", "child_count", "sort_order")
                if f'"{f}"' in txt]
        out.append(f"{k.split('?')[0]}（含 {'/'.join(hits) if hits else '未知字段'}）")
    return "; ".join(out)


def publish(node_id: str) -> Any:
    return cli.post("/v1/admin/publish/node", params={"node_id": node_id})


def gate_ok(**params: Any) -> tuple[bool, str]:
    r = cli.get("/v1/admin/publish/validate", params=params)
    if r.status_code != 200:
        return False, f"validate HTTP {r.status_code}"
    body = r.json()
    bad = [c for c in (body.get("checks") or []) if not c.get("ok")]
    return (not bad), "; ".join(str(c.get("message"))[:90] for c in bad)


# --------------------------------------------------------------- 选测试对象
def pick_node(ntype: str) -> dict[str, Any] | None:
    """挑一条「已发布、没有待发布草稿、且门禁本来就能过」的记录。

    门禁必须先过一遍：拿一条本来就发不出去的记录测闭环，第 ④ 步会被 BR 拦下，
    看起来像闭环坏了。踩过一次（.NET 岗位有个已停用技能，Σweight 天生 0.85）。
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, name, description FROM kg_node n
            WHERE type = %s AND NOT is_draft
              AND COALESCE(status, 'published') = 'published'
              AND NOT EXISTS (
                SELECT 1 FROM kg_node d WHERE d.id = n.id AND d.is_draft)
              AND NOT EXISTS (
                SELECT 1 FROM kg_edge de WHERE de.unit_id = n.id AND de.is_draft)
            ORDER BY id LIMIT 40
            """,
            (ntype,),
        ).fetchall()
    for r in rows:
        ok, why = gate_ok(node_type=ntype, node_id=r["id"], region="CN")
        if ok:
            return dict(r)
        print(f"    （跳过 {r['name']}：门禁本来就不过 —— {why[:80]}）")
    return None


def pick_skill() -> dict[str, Any] | None:
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT ({SKILL_KEY_SQL}) AS skill_key, count(*) AS c
            FROM kg_node n
            WHERE n.type = 'skill_level' AND NOT n.is_draft AND n.region = 'CN'
              AND COALESCE(n.status, 'published') = 'published'
            GROUP BY 1 HAVING count(*) >= 5
            ORDER BY 1 LIMIT 40
            """
        ).fetchall()
    for r in rows:
        if not r["skill_key"]:
            continue
        ok, why = gate_ok(
            node_type="skill_bundle", skill_key=r["skill_key"], region="CN"
        )
        if ok:
            return {"skill_key": r["skill_key"]}
        print(f"    （跳过 {r['skill_key']}：门禁本来就不过 —— {why[:80]}）")
    return None


# --------------------------------------------------------------- 维度：节点三种
def run_node_dim(label: str, ntype: str) -> None:
    print(f"\n== {label}（{ntype}）==")
    t = pick_node(ntype)
    if not t:
        check(False, f"{label}：找不到可测对象", "40 条候选里没有门禁能过的")
        return
    nid, old_desc = t["id"], (t.get("description") or "")
    print(f"    对象：{t['name']} / {nid}")

    base = front_snap(nid)
    print(f"    前台快照 {len(base)} 个接口")

    r = cli.patch(f"/v1/kg/nodes/{nid}", json={"description": MARK + old_desc})
    if not check(r.status_code == 200, "① 编辑成功", f"HTTP {r.status_code}"):
        return

    ab = admin_item(nid, ntype, t["name"])
    check(MARK in json.dumps(ab, ensure_ascii=False), "② 管理台立刻看到新值",
          f"keys={sorted(ab)[:6]}" if ab else "列表里没找到这条记录")
    check(
        str(ab.get("record_status")) == "draft" or bool(ab.get("has_draft")),
        "③ 记录进草稿态",
        f"record_status={ab.get('record_status')} has_draft={ab.get('has_draft')}",
    )
    mid = front_snap(nid)
    check(not diff_count(base, mid), "④ 前台逐字节不变", f"{len(diff_count(base, mid))} 个接口变了")
    check(not front_has_mark(nid), "⑤ 前台看不到草稿内容")

    p = publish(nid)
    if not check(p.status_code == 200, "⑥ 发布成功", f"HTTP {p.status_code} {p.text[:150]}"):
        cli.delete("/v1/admin/draft", params={"node_id": nid})
        return
    after = front_snap(nid)
    changed = diff_count(mid, after)
    check(bool(changed), "⑦ 发布后前台变了", f"{len(changed)} 个接口")
    check(front_has_mark(nid), "⑧ 前台看到新值")

    cli.patch(f"/v1/kg/nodes/{nid}", json={"description": old_desc})
    publish(nid)
    fin = front_snap(nid)
    # 只核对「⑦ 里因这次发布而变过的接口」是否回到基线。全量列表类接口
    # （如无参数的 /v1/graph/explore）会因为发布触发 child_count / sort_order
    # 重算而不回退，那是派生展示元数据的正常重算，拿它判还原会稳定假红。
    still = [k for k in changed if fin.get(k) != base.get(k)]
    check(not still, "⑨ 还原后与最初一致", "; ".join(k[:70] for k in still[:3]))


# --------------------------------------------------------------- 维度：技能
def run_skill_dim() -> None:
    print("\n== 技能（skill_level，L1–L5 一整组）==")
    t = pick_skill()
    if not t:
        check(False, "技能：找不到可测对象", "40 条候选里没有门禁能过的")
        return
    sk = t["skill_key"]
    enc = quote(sk, safe="")
    print(f"    对象：{sk}")

    cur = cli.get(f"/v1/admin/skills/{enc}", params={"region": "CN"}).json()
    levels = {
        str(lv["level"]): (lv.get("description") or "") for lv in (cur.get("levels") or [])
    }
    if not levels:
        check(False, "技能：档位为空，无法测", "")
        return
    key = sorted(levels)[0]
    old = levels[key]

    base = front_snap(sk)
    print(f"    前台快照 {len(base)} 个接口")

    # PATCH 是全量语义：不带 levels 直接 400；且 levels 在 GET 里是数组、
    # 在 PATCH 里要传对象（照抄 GET 的结构会 422）。
    body = {
        "skill_name": cur.get("skill_name") or sk,
        "category": cur.get("category"),
        "levels": dict(levels, **{key: MARK + old}),
    }
    r = cli.patch(f"/v1/admin/skills/{enc}", params={"region": "CN"}, json=body)
    if not check(r.status_code == 200, "① 编辑成功", f"HTTP {r.status_code} {r.text[:140]}"):
        return

    adm = cli.get(f"/v1/admin/skills/{enc}", params={"region": "CN"}).json()
    check(MARK in json.dumps(adm, ensure_ascii=False), "② 管理台立刻看到新值")
    check(
        str(adm.get("record_status")) == "draft" or bool(adm.get("has_draft")),
        "③ 记录进草稿态",
        f"record_status={adm.get('record_status')} has_draft={adm.get('has_draft')}",
    )
    mid = front_snap(sk)
    check(not diff_count(base, mid), "④ 前台逐字节不变", f"{len(diff_count(base, mid))} 个接口变了")
    check(not front_has_mark(sk), "⑤ 前台看不到草稿内容")

    with connect() as conn:
        nid = conn.execute(
            f"SELECT n.id FROM kg_node n WHERE n.type='skill_level' "
            f"AND ({SKILL_KEY_SQL})=%s AND NOT n.is_draft LIMIT 1",
            (sk,),
        ).fetchone()
    p = publish(nid["id"]) if nid else None
    if not check(
        bool(p) and p.status_code == 200,
        "⑥ 发布成功",
        f"HTTP {getattr(p, 'status_code', '-')} {getattr(p, 'text', '')[:150]}",
    ):
        return
    after = front_snap(sk)
    changed = diff_count(mid, after)
    check(bool(changed), "⑦ 发布后前台变了", f"{len(changed)} 个接口")
    check(front_has_mark(sk), "⑧ 前台看到新值")

    body["levels"] = dict(levels)
    cli.patch(f"/v1/admin/skills/{enc}", params={"region": "CN"}, json=body)
    if nid:
        publish(nid["id"])
    fin = front_snap(sk)
    still = [k for k in changed if fin.get(k) != base.get(k)]
    check(not still, "⑨ 还原后与最初一致", "; ".join(k[:70] for k in still[:3]))


DIMS = {
    "industry": lambda: run_node_dim("行业", "industry"),
    "major": lambda: run_node_dim("专业", "major"),
    "occupation": lambda: run_node_dim("岗位", "occupation"),
    "skill": run_skill_dim,
}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dim", choices=sorted(DIMS), action="append")
    args = ap.parse_args()
    for name in args.dim or ["industry", "major", "occupation", "skill"]:
        DIMS[name]()

    print("\n" + "=" * 60)
    bad = [r for r in RESULTS if not r[0]]
    print(f"结果：{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    for _, n, d in bad:
        print(f"  FAIL {n} — {d}")
    with connect() as conn:
        left = conn.execute(
            "SELECT (SELECT count(*) FROM kg_node WHERE is_draft) n,"
            " (SELECT count(*) FROM kg_edge WHERE is_draft) e"
        ).fetchone()
    print(f"收尾：库内残留草稿行 kg_node={left['n']} kg_edge={left['e']}")
    sys.exit(1 if bad else 0)
