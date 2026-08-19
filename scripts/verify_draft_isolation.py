"""草稿态隔离的**核心断言**：做一组真实编辑，前台响应逐字节不变。

方案 §12 把这条列为整个方案的地基：

> 编辑前后快照 diff —— 对前台全部 GET 接口取响应，做一组覆盖各类型的编辑
> （改名、改 attrs、加边、删边、改权重、归档、新建），再取一次，**逐字节相同**

为什么必须是「逐字节」而不是「关键字段一致」：草稿泄漏的形态千奇百怪 ——
多一条边让某个岗位的技能数 +1、权重和从 1.00 变 1.42、`child_count` 因为草稿行被
算进排序而挪了一位、边 JOIN 端点时多出一行导致 `total` 虚高。逐字节比对不需要
事先想到这些形态，任何一处变了都会被抓住；而列几个字段去比，漏的就是没想到的那种。

跑法
----
    python -X utf8 scripts/verify_draft_isolation.py            # 进程内（快，默认）
    python -X utf8 scripts/verify_draft_isolation.py --http     # 起 18201 端口真服务

进程内模式走的是同一条 ASGI 栈（中间件也跑），差别只在没有 uvicorn 与 socket。
`--http` 用来确认真服务器路径上结论一致（鉴权中间件、raw_path 签名那一层）。

**可重复执行**：所有受试草稿按 id 前缀清理，被改过的线上行逐列写回（`LiveRowGuard`）。
中途 Ctrl-C 也会走 finally 恢复；真出意外没恢复的话，重跑一次即可 ——
恢复用的是「捕获时的整行」，不是「反向操作」。
"""
from __future__ import annotations

import argparse
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

from tests import _draft_probe as probe  # noqa: E402

DEFAULT_PORT = 18201
SENTINEL = probe.SENTINEL

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, note: str = "") -> bool:
    _results.append((name, bool(ok), note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {note}" if note else ""))
    return bool(ok)


# ── 真服务器客户端（--http）──────────────────────────────────


class _Resp:
    def __init__(self, status: int, body: bytes):
        self.status_code, self.content = status, body

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.content or b"null")


class HttpClient:
    """把 `urllib` 包成 TestClient 的形状，让两种模式共用同一套用例。"""

    def __init__(self, base: str, headers: dict[str, str]):
        self.base, self.headers = base, headers

    def _call(self, method: str, path: str, params=None, json_body=None) -> _Resp:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
        head = dict(self.headers)
        data = None
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode()
            head["Content-Type"] = "application/json"
        req = urllib.request.Request(url, method=method, headers=head, data=data)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return _Resp(r.status, r.read())
        except urllib.error.HTTPError as e:
            return _Resp(e.code, e.read())
        except Exception as e:  # noqa: BLE001
            return _Resp(-1, str(e).encode())

    def get(self, path, params=None):
        return self._call("GET", path, params)

    def post(self, path, params=None, json=None):  # noqa: A002
        return self._call("POST", path, params, json)

    def patch(self, path, params=None, json=None):  # noqa: A002
        return self._call("PATCH", path, params, json)

    def put(self, path, params=None, json=None):  # noqa: A002
        return self._call("PUT", path, params, json)

    def delete(self, path, params=None):
        return self._call("DELETE", path, params)


def start_server(port: int) -> subprocess.Popen:
    log = ROOT / "tests" / "_draft_isolation_server.log"
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(ROOT / "tests" / "_e2e_server.py"), str(port)],
        cwd=str(ROOT), env=dict(os.environ, PYTHONUTF8="1"),
        stdout=log.open("w", encoding="utf-8"), stderr=subprocess.STDOUT,
    )
    for _ in range(60):
        time.sleep(1)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                if r.status == 200:
                    return proc
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError(f"服务未在 60s 内就绪，见 {log}")


# ── 编辑动作 ─────────────────────────────────────────────────


def edits(client, occ: str, spare: str, real: dict) -> list[tuple[str, Any]]:
    """§12 点名的那一组：改名 / 改 attrs / 加边 / 删边 / 改权重 / 归档 / 新建。

    全部走管理台 HTTP 接口 —— 运营只有这一个入口，草稿化必须在这条路上生效。
    直接调 `write.*` 会漏掉路由层（比如 `REVIEW_REQUIRED` 分支）。
    """
    new_id = "ZZ:draftprobe:verifynew:1"

    def _first_edge() -> str | None:
        from backend.kg.pg_store.client import connect

        with connect() as c:
            r = c.execute(
                "SELECT id FROM kg_edge WHERE src_id=%s AND rel_type='requires' "
                "AND NOT is_draft AND COALESCE(status,'published')='published' "
                "ORDER BY id LIMIT 1", (occ,)
            ).fetchone()
        return r["id"] if r else None

    def _comp_item() -> dict | None:
        r = client.get("/v1/admin/composition", params={"node_id": occ})
        items = (r.json() or {}).get("items") or []
        return items[0] if items else None

    return [
        ("改名", lambda: client.patch(
            f"/v1/kg/nodes/{occ}", json={"name": f"{SENTINEL}改名"})),
        ("改 attrs", lambda: client.patch(
            f"/v1/kg/nodes/{occ}", json={"attrs": {"zz_probe": SENTINEL}})),
        ("加边", lambda: client.post("/v1/kg/edges", json={
            "src_id": occ, "dst_id": real["skill_id"], "rel_type": "requires",
            "region": "CN", "weight": 0.13, "source_system": "MANUAL",
            "source_url": "manual://t", "license": "internal"})),
        ("删边", lambda: client.delete(f"/v1/kg/edges/{_first_edge()}")),
        ("改权重", lambda: client.put(
            "/v1/admin/composition", params={"node_id": occ},
            json={"skill_key": (_comp_item() or {}).get("skill_key"),
                  "level": (_comp_item() or {}).get("selected_level"),
                  "weight": 0.42})
         if _comp_item() else None),
        # 「归档」原本在这一组里（断言归档后前台不变）。2026-08-19 需求收窄：
        # 停用/启用/删除改成**立即生效**，归档后前台当场少一条，
        # 留在这里就是锁一个已作废的行为。换成同类的内容编辑「改描述」，
        # 归档的正面行为由 tests/db 的 Test状态动作立即生效 覆盖。
        ("改描述", lambda: client.patch(
            f"/v1/kg/nodes/{occ}", json={"description": f"{SENTINEL}描述"})),
        ("新建", lambda: client.post("/v1/kg/nodes", json={
            "id": new_id, "type": "occupation", "name": f"{SENTINEL}新建",
            "region": "CN", "source_system": "MANUAL", "source_id": new_id,
            "source_url": "manual://t", "license": "internal"})),
    ]


def cleanup_new() -> None:
    from backend.kg.pg_store.client import connect

    with connect() as c:
        c.execute("DELETE FROM kg_edge WHERE src_id LIKE 'ZZ:draftprobe:%%' "
                  "OR dst_id LIKE 'ZZ:draftprobe:%%'")
        c.execute("DELETE FROM kg_node WHERE id LIKE 'ZZ:draftprobe:%%'")
        c.commit()


# ── 各段 ─────────────────────────────────────────────────────


def seg_caps() -> dict:
    print("\n== 0 库能力 ==")
    caps = probe.db_capabilities()
    print(f"  主键 kg_node={caps['pk']['kg_node']} kg_edge={caps['pk']['kg_edge']}")
    print(f"  kg_edge 外键={caps['foreign_keys'] or '已删'}")
    check("§2 DDL 已落地（is_draft 列 + 复合主键）",
          caps["has_is_draft"] and caps["pk_has_is_draft"],
          "" if caps["has_is_draft"] else "没有 is_draft 列，下面的草稿类断言无从验证")
    check("§1.2 两个外键已删", caps["foreign_keys"] == [], str(caps["foreign_keys"]))
    check("§2 ④ 业务编码唯一索引排除草稿", "is_draft" in caps["code_unique_indexdef"],
          caps["code_unique_indexdef"][-60:])
    return caps


def seg_leak(client, app, real: dict, mode: str) -> None:
    print(f"\n== 1 泄漏扫描（{mode} 形态）==")
    try:
        fx = probe.install_draft_fixture(
            mode, shadow_of=real["occupation_id"] if mode == "row" else None
        )
    except probe.DraftUnsupported as e:
        check(f"造 {mode} 形态草稿受试对象", False, str(e))
        return
    try:
        ctrl = probe.run_cases(client, probe.admin_control_cases(fx))
        blind = [r.case.label for r in ctrl if SENTINEL not in r.text]
        if not check("正对照：管理台看得见这批草稿", not blind, "; ".join(blind)):
            return   # 正对照不过，「前台看不见」没有意义，不必往下报
        base = probe.run_cases(client, probe.baseline_cases(app, real))
        leak = probe.run_cases(client, probe.leak_cases(app, real, fx))
        n = len(base) + len(leak)
        leaks = probe.find_leaks(base + leak, fx.tokens)
        check(f"前台全部 GET 看不到草稿（{n} 次调用）", not leaks,
              "; ".join(leaks[:6]) if leaks else f"{n} 次全部没有哨兵串")
        bad5 = [f"{r.case.label}→{r.status}" for r in base + leak if r.status >= 500]
        check("草稿在位时前台无 5xx", not bad5, "; ".join(dict.fromkeys(bad5))[:400])
        if fx.shadow_id:
            hits = [r.case.label for r in base + leak if f"{SENTINEL}改名" in r.text]
            check("影子草稿行的改名没泄漏", not hits, "; ".join(hits[:5]))
    finally:
        probe.remove_draft_fixture()


def seg_bytes(client, app, real: dict) -> None:
    print("\n== 2 核心断言：编辑前后前台逐字节不变 ==")
    cases = probe.baseline_cases(app, real)
    snap1 = probe.snapshot(client, cases)
    snap2 = probe.snapshot(client, cases)
    unstable = {k.split(": ")[0] for k in probe.diff_snapshots(snap1, snap2)}
    unstable = {c.label for c in cases if any(c.label == u for u in unstable)}
    if unstable:
        print(f"  （排除 {len(unstable)} 个本身不稳定的接口：{sorted(unstable)}）")
    # 严格判：有接口两次采样就不一致，说明它**本身不可重复**（时间戳 / 随机序 / LLM），
    # 逐字节比对对它无效。这不算草稿泄漏，但必须显式暴露出来 ——
    # 悄悄排除掉的话，核心断言的覆盖面会一点点缩水而没人知道。
    check(f"基线可重复（{len(cases)} 接口，两次采样一致）", not unstable,
          f"这些接口自身不稳定，已从逐字节比对中排除：{sorted(unstable)}"
          if unstable else f"{len(cases)} 个全稳定")

    occ = real["occupation_id"]
    spare = real.get("spare_occupation_id") or occ
    guard = probe.LiveRowGuard([occ, spare], [occ, spare, real.get("major_id", "")])
    with guard:
        base = probe.snapshot(client, cases)
        for name, fn in edits(client, occ, spare, real):
            try:
                r = fn()
            except Exception as e:  # noqa: BLE001
                check(f"「{name}」编辑动作可执行", False, f"{type(e).__name__}: {e}")
                continue
            st = getattr(r, "status_code", None)
            if st is not None and st >= 400:
                check(f"「{name}」编辑动作可执行", False,
                      f"HTTP {st} {getattr(r, 'text', '')[:150]}")
                continue
            after = probe.snapshot(client, cases)
            diff = probe.diff_snapshots(base, after, skip=unstable)
            check(f"「{name}」之后前台逐字节不变", not diff,
                  "; ".join(diff[:4]) if diff else f"{len(cases) - len(unstable)} 个接口一致")
            base = after   # 逐步比对：只报「这一步」新引入的差异
    cleanup_new()


def seg_publish(client, app, real: dict) -> None:
    print("\n== 3 反面：发布之后前台必须变 ==")
    from backend.kg.pg_store import draft_publish as dp

    occ = real["occupation_id"]
    cases = [c for c in probe.baseline_cases(app, real)
             if "search" in c.path or "positions" in c.path]
    guard = probe.LiveRowGuard([occ], [occ])
    with guard:
        before = probe.snapshot(client, cases)
        probe.make_draft_of(occ, name=f"{SENTINEL}已发布")
        try:
            dp.publish_node(occ, user_id="9201", user_name="verify")
        except Exception as e:  # noqa: BLE001
            check("发布单元可执行", False, f"{type(e).__name__}: {e}")
            return
        after = probe.snapshot(client, cases)
        diff = probe.diff_snapshots(before, after)
        check("发布后前台确实变了（否则发布没生效 / 或前台读的不是线上行）",
              bool(diff), f"{len(diff)} 个接口变了" if diff else "一个都没变")
        check("发布后草稿行清空", not probe.draft_rows_of(occ))


def dup_requires_groups() -> int:
    """同一（实体, 关系, 技能）挂了多条线上 requires/covers 边的组数。

    为什么自己数而不是只看闸门脚本的退出码：`dedupe_skill_composition_edges.py`
    在 dry-run 下**永远 exit 0**（有重复也只是打印一份清单），当闸门用是失效的。
    §12 要的性质是「发布重建边之后没**新造出**重复边」，那就直接数。
    """
    from backend.kg.pg_store.client import connect

    with connect() as c:
        return c.execute(
            """
            SELECT COUNT(*) AS c FROM (
              SELECT e.src_id, e.rel_type, n.attrs::json->>'skill_key' AS sk
              FROM kg_edge e
              JOIN kg_node n ON n.id = e.dst_id AND NOT n.is_draft
              WHERE e.rel_type IN ('requires','covers') AND NOT e.is_draft
                AND COALESCE(e.status,'published') = 'published'
                AND n.attrs::json->>'skill_key' IS NOT NULL
              GROUP BY 1, 2, 3 HAVING COUNT(*) > 1
            ) t
            """
        ).fetchone()["c"]


def seg_gates(dup_before: int | None = None) -> None:
    """§12 末尾那几条闸门：发布路径跑过之后它们仍要通过。"""
    print("\n== 4 既有闸门（发布路径跑过之后）==")
    if dup_before is not None:
        now = dup_requires_groups()
        check("发布路径没新造出「同一技能多条 requires 边」",
              now <= dup_before, f"重复组 {dup_before} → {now}")
    # 方案里写的 `scripts/fix_duplicate_requires.py` **不存在**，
    # 实际的同名闸门是 dedupe_skill_composition_edges.py（默认 dry-run，--apply 才写库）。
    for script, args, name in (
        ("dedupe_skill_composition_edges.py", [], "同一技能多条 requires 边（dry-run）"),
        ("verify_backfill.py", ["after"], "技能档位逐档核对方向"),
        ("check_orphan_edges.py", [], "孤儿边闸门"),
    ):
        p = ROOT / "scripts" / script
        if not p.exists():
            check(f"{name}（{script}）", False, "脚本不存在")
            continue
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(p), *args],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=600,
        )
        tail = " ".join((r.stdout or r.stderr or "").split())[-200:]
        check(f"{name}（{script}）", r.returncode == 0, f"exit={r.returncode} {tail}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--http", nargs="?", const=DEFAULT_PORT, type=int, default=None,
                    help=f"起真服务并走 HTTP（默认端口 {DEFAULT_PORT}）")
    ap.add_argument("--skip-gates", action="store_true", help="跳过第 4 段闸门脚本")
    args = ap.parse_args()

    proc = None
    if args.http:
        print(f"起服务：127.0.0.1:{args.http}")
        proc = start_server(args.http)
        client = HttpClient(f"http://127.0.0.1:{args.http}",
                            {"X-Test-Uid": "9201", "X-Test-Uname": "draft-verify"})
    else:
        client = probe.make_client()
    app = probe.get_app()

    try:
        caps = seg_caps()
        real = probe.pick_real_ids()
        if not real.get("occupation_id"):
            check("挑到受试岗位", False, "库里没有「3–8 条 requires 边的已发布岗位」")
            return 1
        print(f"  受试岗位：{real['occupation_name']}（{real['occupation_id']}）")
        cleanup_new()
        dup_before = dup_requires_groups() if caps["has_is_draft"] else None
        seg_leak(client, app, real, "status")
        if caps["has_is_draft"]:
            seg_leak(client, app, real, "row")
        seg_bytes(client, app, real)
        if caps["has_is_draft"]:
            seg_publish(client, app, real)
        if not args.skip_gates:
            seg_gates(dup_before)
    finally:
        cleanup_new()
        probe.remove_draft_fixture()
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    ok = sum(1 for _, p, _ in _results if p)
    print("\n" + "=" * 60)
    print(f"结果：{ok}/{len(_results)} 通过")
    for name, passed, note in _results:
        if not passed:
            print(f"  FAIL {name} — {note}")
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
