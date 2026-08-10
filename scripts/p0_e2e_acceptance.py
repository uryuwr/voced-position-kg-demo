"""P0 端到端验收：counts / 技能聚合 / 多档审核 / 构成 / 先修 / AI 网关态。"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backend.settings as settings

settings.AUTH_BYPASS = True
import backend.api.auth as auth

auth.AUTH_BYPASS = True

from fastapi.testclient import TestClient

from backend.api.main import app
from backend.kg.pg_store.biz_store import ensure_biz_schema
from backend.kg.pg_store.client import connect

REPORTS = ROOT / "reports"
REPORTS.mkdir(exist_ok=True)


def main() -> int:
    ensure_biz_schema()
    c = TestClient(app)
    h = {"X-Test-Uid": "e2e", "X-Test-Uname": "e2e"}
    results: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append({"name": name, "ok": ok, "detail": detail})
        print(("OK  " if ok else "FAIL"), name, detail[:120])

    # health + AI gateway
    r = c.get("/health")
    check("health", r.status_code == 200, str(r.json().get("ai_gateway")))

    # industries counts
    r = c.get("/v1/student/industries", params={"page": 1, "page_size": 3}, headers=h)
    ok = r.status_code == 200 and "counts" in (r.json().get("items") or [{}])[0]
    check("student industries counts", ok, str((r.json().get("items") or [{}])[0].get("counts")))

    # professions counts
    r = c.get(
        "/v1/student/professions",
        params={"page": 1, "page_size": 3, "q": "软件"},
        headers=h,
    )
    items = r.json().get("items") if r.status_code == 200 else []
    ok = r.status_code == 200 and items and "counts" in items[0]
    check(
        "student professions counts",
        ok,
        str(items[0].get("counts") if items else r.text[:80]),
    )

    # skills bundle
    r = c.get(
        "/v1/student/skills",
        params={"page": 1, "page_size": 2, "view": "bundle"},
        headers=h,
    )
    items = r.json().get("items") if r.status_code == 200 else []
    ok = r.status_code == 200 and items and items[0].get("available_levels") is not None
    check("student skills bundle", ok, f"total={r.json().get('total') if r.status_code==200 else None}")

    # occupation + composition
    r = c.get(
        "/v1/student/positions",
        params={"page": 1, "page_size": 5, "q": "汽车"},
        headers=h,
    )
    pos = (r.json().get("items") or [None])[0] if r.status_code == 200 else None
    check("student positions", bool(pos), str(pos.get("name") if pos else r.text[:60]))
    if pos:
        r2 = c.get(
            "/v1/student/positions/skill-composition",
            params={"id": pos["id"]},
            headers=h,
        )
        check(
            "skill-composition",
            r2.status_code == 200 and "weight_sum" in r2.json(),
            str({k: r2.json().get(k) for k in ("skill_count", "weight_sum")})
            if r2.status_code == 200
            else r2.text[:80],
        )

    # skill bundle create → approve
    with connect() as conn:
        occ = conn.execute(
            "SELECT id FROM kg_node WHERE type='occupation' AND region='CN' LIMIT 1"
        ).fetchone()
    oid = occ["id"] if occ else None
    if oid:
        body = {
            "skill_key": f"__e2e_skill_{datetime.now(timezone.utc).strftime('%H%M%S')}",
            "levels": {
                "L1": {"label": "了解", "description": "e2e L1"},
                "L2": {"description": "e2e L2"},
            },
            "occupation_links": [
                {"occupation_id": oid, "weight": 0.3, "required_level": 2}
            ],
        }
        r = c.post("/v1/admin/skills", json=body, headers=h)
        check("admin skills create pending", r.status_code == 200, str(r.json().get("id")))
        if r.status_code == 200:
            cid = r.json()["id"]
            r2 = c.post(f"/v1/admin/changes/{cid}/approve", headers=h)
            applied = r2.json().get("applied") if r2.status_code == 200 else {}
            check(
                "approve expand levels",
                r2.status_code == 200 and applied.get("skill_bundle"),
                str(applied.get("levels")),
            )
            sk = body["skill_key"]
            # prereq
            r3 = c.post(
                f"/v1/admin/skills/{sk}/prerequisites",
                json={"prereq_skill_key": "通用职业素养", "region": "CN"},
                headers=h,
            )
            check("prereq add", r3.status_code == 200, r3.text[:80])
            # cleanup
            from backend.kg.pg_store.skill_write import _find_existing_nodes_by_skill_key

            nodes = _find_existing_nodes_by_skill_key(sk)
            ids = [n["id"] for n in nodes]
            with connect() as conn:
                if ids:
                    conn.execute("DELETE FROM kg_edge WHERE dst_id = ANY(%s)", (ids,))
                    conn.execute("DELETE FROM kg_node WHERE id = ANY(%s)", (ids,))
                conn.execute(
                    "DELETE FROM kg_skill_prereq WHERE skill_key=%s", (sk,)
                )
                conn.commit()
    else:
        check("admin skills create pending", False, "no occupation")

    # diagnosis (rule path without AI)
    r = c.post(
        "/v1/student/diagnosis/resume",
        json={"content_text": "熟悉 Python 与直播投放数据分析", "target_occupation_id": oid},
        headers=h,
    )
    check(
        "diagnosis resume",
        r.status_code == 200 and r.json().get("parsed_skills"),
        str(r.json().get("agent_meta", {}).get("engine")),
    )

    passed = sum(1 for x in results if x["ok"])
    total = len(results)
    day = datetime.now().strftime("%Y-%m-%d")
    out = {
        "date": day,
        "passed": passed,
        "total": total,
        "all_ok": passed == total,
        "results": results,
    }
    path = REPORTS / f"{day}-P0-e2e-acceptance.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    md = REPORTS / f"{day}-P0-e2e-acceptance.md"
    lines = [
        f"# P0 E2E 验收 {day}",
        "",
        f"**结果：{passed}/{total}** {'全部通过' if passed == total else '有失败'}",
        "",
        "| 项 | 结果 | 详情 |",
        "| --- | --- | --- |",
    ]
    for x in results:
        lines.append(
            f"| {x['name']} | {'✅' if x['ok'] else '❌'} | {x['detail'][:80]} |"
        )
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("report", path)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
