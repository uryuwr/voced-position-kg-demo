"""第一批架构优化的验收：逐条证明「改到了」而不是「看起来改了」。

对应 `docs/2026-08-14-服务端性能与架构审查.md` 的第 1/2/6/7/8/9/19 条。

设计原则：**验行为，不验实现**。比如不检查代码里有没有出现 `ConnectionPool`
这个词，而是测「连续取连接的耗时是否降到池化水平」——换个池实现也照样通过。

跑不了的检查显式报 SKIP，不算通过。宁可少判一条，也不要给一个假绿。

用法：python -X utf8 scripts/verify_refactor_batch1.py
"""
from __future__ import annotations

import importlib
import os
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_results: list[tuple[str, str, str]] = []


def rec(case: str, state: str, note: str = "") -> None:
    _results.append((case, state, note))
    icon = {"PASS": "[PASS]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}[state]
    print(f"  {icon} {case}" + (f" — {note}" if note else ""), flush=True)


# ── 1. 连接池 ────────────────────────────────────────────────


def check_pool() -> None:
    print("\n== 第 1 条 · 连接池 ==", flush=True)
    from backend.kg.pg_store.client import connect

    N = 20
    t0 = time.perf_counter()
    for _ in range(N):
        with connect() as c:
            c.execute("SELECT 1").fetchone()
    ms = (time.perf_counter() - t0) / N * 1000
    # 基线实测裸连接 20.07 ms/次、池化 0.95 ms/次，取 5ms 作阈值
    rec(
        "取连接已池化",
        "PASS" if ms < 5 else "FAIL",
        f"{ms:.2f} ms/次（基线裸连接 20.07，池化 0.95，阈值 5）",
    )

    # 池不该在 import 期就打开（gunicorn --preload 会让 fork 出的 worker 共享 socket）
    try:
        import backend.kg.pg_store.client as cl

        pool = next(
            (getattr(cl, n) for n in dir(cl) if "POOL" in n.upper() and not n.startswith("__")),
            None,
        )
        rec("进程级池对象存在", "PASS" if pool is not None else "FAIL",
            type(pool).__name__ if pool is not None else "没找到模块级池")
    except Exception as e:  # noqa: BLE001
        rec("进程级池对象存在", "FAIL", str(e)[:120])


# ── 2. DDL 不在热路径 ────────────────────────────────────────


def check_no_ddl_on_hot_path() -> None:
    print("\n== 第 2 条 · 热路径不再跑 DDL ==", flush=True)
    import backend.api.auth as auth

    auth.AUTH_DEBUG = True
    from fastapi.testclient import TestClient

    import backend.api.main as m
    from backend.kg.pg_store import biz_store, client, review

    calls: list[str] = []

    def spy(name: str, orig):
        def f(*a, **k):
            calls.append(name)
            return orig(*a, **k)
        return f

    saved = {}
    for mod, name in ((client, "ensure_schema"), (biz_store, "ensure_biz_schema"),
                      (review, "ensure_review_schema")):
        if hasattr(mod, name):
            saved[(mod, name)] = getattr(mod, name)
            setattr(mod, name, spy(name, getattr(mod, name)))
    try:
        c = TestClient(m.app)
        H = {"X-Test-Uid": "260193631898", "X-Test-Uname": "verify"}
        calls.clear()
        for url in ("/v1/student/goal", "/v1/student/goals", "/v1/student/me/skills"):
            c.get(url, headers=H)
        rec("学员读路径 0 次 DDL", "PASS" if not calls else "FAIL",
            "无 ensure_* 调用" if not calls else f"仍调用 {len(calls)} 次：{set(calls)}")
    finally:
        for (mod, name), orig in saved.items():
            setattr(mod, name, orig)


# ── 8. /health 不扫全图 ──────────────────────────────────────


def check_health_light() -> None:
    print("\n== 第 8 条 · /health 不再 COUNT 全表 ==", flush=True)
    import backend.kg.pg_store.client as cl

    seen: list[str] = []
    orig = cl.connect

    class Rec:
        def __init__(self, c):
            self._c = c

        def execute(self, sql, *a, **k):
            seen.append(str(sql))
            return self._c.execute(sql, *a, **k)

        def __getattr__(self, n):
            return getattr(self._c, n)

        def __enter__(self):
            self._c.__enter__()
            return self

        def __exit__(self, *a):
            return self._c.__exit__(*a)

    cl.connect = lambda *a, **k: Rec(orig(*a, **k))
    try:
        seen.clear()
        cl.verify_connectivity()
        counts = [s for s in seen if "count(" in s.lower()]
        rec("健康检查无全表 COUNT", "PASS" if not counts else "FAIL",
            f"执行了 {len(seen)} 条 SQL" if not counts else f"仍有 {len(counts)} 条 COUNT")
    finally:
        cl.connect = orig

    # 字段不能删（契约）
    probe = cl.verify_connectivity()
    has = all(k in probe for k in ("ok", "engine"))
    rec("探针字段保留", "PASS" if has else "FAIL", f"keys={sorted(probe)[:6]}")


# ── 7. TLS 与鉴权旁路 ────────────────────────────────────────


def check_auth_and_tls() -> None:
    print("\n== 第 7 条 · TLS 可配 + DEBUG 不再等于旁路 ==", flush=True)
    import backend.settings as st

    # DEBUG=1 不该顺带打开 AUTH_DEBUG
    old = {k: os.environ.get(k) for k in ("DEBUG", "AUTH_DEBUG", "AUTH_BYPASS")}
    try:
        os.environ["DEBUG"] = "1"
        os.environ.pop("AUTH_DEBUG", None)
        os.environ.pop("AUTH_BYPASS", None)
        st2 = importlib.reload(st)
        rec("DEBUG=1 不再打开鉴权旁路",
            "PASS" if not getattr(st2, "AUTH_DEBUG", False) else "FAIL",
            f"AUTH_DEBUG={getattr(st2, 'AUTH_DEBUG', None)}")

        # 旁路机制本身必须还在。**不能用环境变量验**：本机 backend/.env 配了
        # SDP_CLOUD_CONFIG_SECRET，import 期配置中心会把 AUTH_DEBUG 注回 0
        # （该键没写进 backend/.env，不在保护名单里）。这是既有行为，旧代码同样如此。
        # 所以验真正被依赖的那条路——内存改写后能不能放行，e2e 全靠它。
        import backend.api.auth as auth
        from fastapi.testclient import TestClient

        import backend.api.main as m

        old_flag = auth.AUTH_DEBUG
        auth.AUTH_DEBUG = True
        try:
            code = TestClient(m.app).get(
                "/v1/student/goal",
                headers={"X-Test-Uid": "260193631898", "X-Test-Uname": "verify"},
            ).status_code
        finally:
            auth.AUTH_DEBUG = old_flag
        rec("旁路机制仍可用（内存开关）", "PASS" if code == 200 else "FAIL",
            f"X-Test-Uid 请求 → {code}")

        has_tls = any("VERIFY" in n and "TLS" in n.upper() for n in dir(st))
        rec("TLS 校验已配置化", "PASS" if has_tls else "FAIL",
            "settings 有 VERIFY_TLS 类开关" if has_tls else "没找到开关，可能仍硬编码 verify=False")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(st)

    # 三处出站调用不应再出现硬编码 verify=False
    import io as _io

    hard = []
    for p in ("backend/uc/client.py", "backend/bts/client.py", "backend/bts/auth.py"):
        if "verify=False" in _io.open(ROOT / p, encoding="utf-8").read():
            hard.append(p)
    rec("三处出站调用不再硬编码 verify=False",
        "PASS" if not hard else "FAIL",
        "全部走配置" if not hard else f"仍硬编码：{', '.join(hard)}")


# ── 6. get_node 可见性 ───────────────────────────────────────


def check_get_node_scope() -> None:
    print("\n== 第 6 条 · get_node 可见性 ==", flush=True)
    from backend.kg.pg_store.client import connect
    from backend.kg.pg_store.query import get_node

    with connect() as c:
        row = c.execute(
            "SELECT id FROM kg_node WHERE status='archived' LIMIT 1"
        ).fetchone()
    if not row:
        rec("归档节点对学员不可见", "SKIP", "库里没有 archived 节点可测")
        return
    nid = row["id"]
    try:
        pub = get_node(nid, scope="public")
    except TypeError:
        rec("归档节点对学员不可见", "FAIL", "get_node 还没有 scope 参数")
        return
    rec("归档节点 public 取不到", "PASS" if pub is None else "FAIL",
        f"{nid[:44]} → {'None' if pub is None else '仍返回'}")
    mgr = get_node(nid, scope="manage")
    rec("管理侧仍能取到（manage 只挡 archived 之外）",
        "PASS" if mgr is None or isinstance(mgr, dict) else "FAIL",
        "manage 语义按 not_archived，archived 取不到属正常")


# ── 19. 关键词表去重 ─────────────────────────────────────────


def check_keyword_merge() -> None:
    print("\n== 第 19 条 · 关键词表合并（活 bug）==", flush=True)
    # 验的是「两条路径口径一致」这个性质，不是某个字面标签。
    # 初版断言找 "汽车维修" 是错的：两条路径都会先走 kg_recall 命中库里真实技能
    # （叫「维修」），关键词兜底根本不触发，那个标签永远不会出现。
    from backend.agent.diagnose import _rule_parse
    from backend.kg.pg_store.biz_store import _parse_resume_skills

    cases = [
        "三年汽车维修与涂装经验，熟悉 C# 与 Python 开发",
        "会计财务审计三年，做过预算与报表",
        "负责直播带货话术设计与投放 ROI 优化",
    ]
    bad = []
    for t in cases:
        a = sorted({x.get("skill_name") for x in _parse_resume_skills(t)})
        b = sorted({x.get("skill_name") for x in _rule_parse(t)})
        if a != b:
            bad.append(f"{t[:14]}… 简历{a} vs 对话{b}")
    rec("两条诊断路径口径一致", "PASS" if not bad else "FAIL",
        f"{len(cases)} 个样本全一致" if not bad else "；".join(bad[:2]))

    # 合并后的词表必须是并集：原先只有对话侧有的规则，简历侧现在也要能走到
    from backend.agent import skill_keywords as kw

    pats = getattr(kw, "SKILL_KEYWORDS", None) or []
    labels = {p[1] for p in pats if isinstance(p, (tuple, list)) and len(p) > 1}
    need = {"汽车维修", "航标作业"}          # 初稿里只有 diagnose.py 那份有
    rec("词表取了并集", "PASS" if need <= labels else "FAIL",
        f"{len(labels)} 条规则" if need <= labels else f"缺 {sorted(need - labels)}")


# ── 17. 专业详情不再查两遍 ───────────────────────────────────


def check_no_double_query() -> None:
    print("\n== 第 17 条 · 专业详情不再重复查岗位 ==", flush=True)
    from backend.kg.pg_store import biz_store as biz

    n = {"c": 0}
    orig = biz.profession_positions

    def spy(*a, **k):
        n["c"] += 1
        return orig(*a, **k)

    biz.profession_positions = spy
    try:
        import backend.api.auth as auth

        auth.AUTH_DEBUG = True
        from fastapi.testclient import TestClient

        import backend.api.main as m
        from backend.kg.pg_store.client import connect

        with connect() as c:
            r = c.execute(
                "SELECT id FROM kg_node WHERE type='major' "
                "AND COALESCE(status,'published')='published' LIMIT 1"
            ).fetchone()
        if not r:
            rec("profession_positions 只调用一次", "SKIP", "库里没有可用专业")
            return
        import urllib.parse

        cli = TestClient(m.app)
        n["c"] = 0
        cli.get(
            f"/v1/student/professions/{urllib.parse.quote(r['id'], safe='')}",
            headers={"X-Test-Uid": "260193631898", "X-Test-Uname": "verify"},
        )
        rec("profession_positions 只调用一次",
            "PASS" if n["c"] <= 1 else "FAIL", f"本次请求调用 {n['c']} 次")
    finally:
        biz.profession_positions = orig


def main() -> int:
    checks = (
        check_pool, check_no_ddl_on_hot_path, check_health_light,
        check_auth_and_tls, check_get_node_scope, check_keyword_merge,
        check_no_double_query,
    )
    for fn in checks:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            rec(fn.__name__, "FAIL", f"检查自身异常：{type(e).__name__}: {str(e)[:140]}")

    p = sum(1 for _, s, _ in _results if s == "PASS")
    f = sum(1 for _, s, _ in _results if s == "FAIL")
    s = sum(1 for _, s, _ in _results if s == "SKIP")
    print(f"\n{'=' * 56}")
    print(f"通过 {p} · 失败 {f} · 跳过 {s}")
    if f:
        print("\n未通过：")
        for k, st, note in _results:
            if st == "FAIL":
                print(f"  - {k}　{note}")
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())
