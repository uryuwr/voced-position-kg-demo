"""拿**有真实诊断记录的用户**把所有 GET 接口扫一遍，专抓响应校验 500。

为什么需要它（这次栽的坑）
--------------------------
`tests/e2e_robustness.py` 用 uid="0" 做 fuzz，那个用户没有任何诊断记录，
于是 `/positions/match` 的 `if diag:` 分支从来没被走到——12/12 全绿，
而诊断过的岗位实际是 500。同理闭环 e2e 只验「未诊断岗位」那一支。

三处验收都绕开了同一个分支。补上：用真有数据的用户跑，并且**优先挑
诊断过的岗位 id** 作为路径参数，把级联的每一档都覆盖到。

响应校验 500 的特点是「数据到了某个值域才炸」，所以必须拿真实数据打，
构造的假数据打不出来。

用法：python -X utf8 scripts/probe_real_user_500.py [uid]
"""
from __future__ import annotations

import sys
import urllib.parse
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

UID = sys.argv[1] if len(sys.argv) > 1 else "260193631898"


def real_ids() -> dict[str, list[str]]:
    """从库里取真实 id：优先诊断过的岗位（能走进级联第一档）。"""
    from backend.kg.pg_store.client import connect

    out: dict[str, list[str]] = {}
    with connect() as c:
        out["diagnosed_occ"] = [
            r["id"] for r in c.execute(
                """SELECT DISTINCT s.target_occupation_id AS id
                   FROM biz_diagnosis_session s
                   JOIN biz_diagnosis_result r ON r.session_id = s.id
                   WHERE s.user_id = %s AND s.target_occupation_id IS NOT NULL
                   LIMIT 4""",
                (UID,),
            ).fetchall()
        ]
        for key, typ in (("occ", "occupation"), ("major", "major"),
                         ("industry", "industry"), ("skill", "skill_level")):
            out[key] = [
                r["id"] for r in c.execute(
                    "SELECT id FROM kg_node WHERE type=%s "
                    "AND COALESCE(status,'published')='published' LIMIT 2",
                    (typ,),
                ).fetchall()
            ]
        out["skill_key"] = [
            r["k"] for r in c.execute(
                "SELECT DISTINCT (attrs::json->>'skill_key') AS k FROM kg_node "
                "WHERE type='skill_level' AND attrs::json->>'skill_key' IS NOT NULL LIMIT 2"
            ).fetchall()
        ]
        out["session"] = [
            str(r["id"]) for r in c.execute(
                "SELECT id FROM biz_assessment_question WHERE session_id IN "
                "(SELECT id FROM biz_diagnosis_session WHERE user_id=%s) LIMIT 1",
                (UID,),
            ).fetchall()
        ]
    return out


def main() -> int:
    import backend.api.auth as auth

    auth.AUTH_DEBUG = True
    from fastapi.testclient import TestClient

    import backend.api.main as m

    ids = real_ids()
    if not ids["diagnosed_occ"]:
        print(f"用户 {UID} 没有诊断记录，这个探针的核心价值（级联第一档）就没了")
        return 2
    print(f"用户 {UID}：诊断过 {len(ids['diagnosed_occ'])} 个岗位")

    E = urllib.parse.quote
    cli = TestClient(m.app, raise_server_exceptions=False)
    H = {"X-Test-Uid": UID, "X-Test-Uname": "probe"}

    spec = m.app.openapi()
    fails: list[tuple[str, str]] = []
    checked = 0

    # 参数名 → 真实值候选。诊断过的岗位放最前，确保走进 if diag: 分支
    pool: dict[str, list[str]] = {
        "position_id": ids["diagnosed_occ"] + ids["occ"],
        "occupation_id": ids["diagnosed_occ"] + ids["occ"],
        "id": ids["diagnosed_occ"] + ids["occ"] + ids["major"],
        "node_id": ids["occ"] + ids["major"],
        "profession_id": ids["major"],
        "major_id": ids["major"],
        "industry_id": ids["industry"],
        "skill_key": ids["skill_key"],
        "session_id": ids["session"] or ["1"],
        "major": ["软件技术"],
        "q": ["工程"],
    }

    for path, methods in (spec.get("paths") or {}).items():
        op = methods.get("get")
        if not op:
            continue
        params = op.get("parameters") or []
        url = path
        qs: list[str] = []
        skip = False
        for p in params:
            name, loc = p.get("name"), p.get("in")
            if loc == "header":
                continue
            vals = pool.get(name)
            if p.get("required") and not vals:
                skip = True          # 必填参数没有真实值可填，跳过并如实报告
                break
            if not vals:
                continue
            v = vals[0]
            if loc == "path":
                url = url.replace("{" + name + "}", E(str(v), safe=""))
            else:
                qs.append(f"{name}={E(str(v), safe='')}")
        if skip or "{" in url:
            continue
        full = url + ("?" + "&".join(qs) if qs else "")
        r = cli.get(full, headers=H)
        checked += 1
        if r.status_code >= 500:
            body = r.text[:150].replace("\n", " ")
            fails.append((full, f"{r.status_code} {body}"))
            print(f"  [{r.status_code}] {full[:88]}")

    print(f"\n{'=' * 56}")
    print(f"扫了 {checked} 个 GET 接口（带真实诊断数据），5xx {len(fails)} 个")
    for u, why in fails:
        print(f"  {u[:80]}\n      {why}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
