"""走真实 HTTP + SSE 跑一遍测评，校验前端契约（stages / question_end / report）。

用法：python -X utf8 tests/run_assessment_api.py [base_url]
默认对 127.0.0.1:18097 起一个 AUTH_BYPASS 实例（不动 8088 上的正常服务）。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PORT = 18097
BASE = sys.argv[1] if len(sys.argv) > 1 else f"http://127.0.0.1:{PORT}"
H = {"Content-Type": "application/json", "X-Test-Uid": "e2e", "X-Test-Uname": "e2e"}
GOOD = (
    "负责整车故障诊断。客户反馈间歇性熄火但无故障码，我先复现工况，用示波器同步抓取"
    "曲轴位置传感器与点火信号波形，比对正常车型定位到线束接插件虚接，"
    "之后建立该车型排查清单与复检流程，季度返修率从8%降到3%，培训了2名新人。"
)


def sse_post(path: str, body: dict) -> list[tuple[str, dict]]:
    """消费 SSE，返回 [(event, data)]。"""
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), headers=H, method="POST"
    )
    out: list[tuple[str, dict]] = []
    with urllib.request.urlopen(req, timeout=300) as r:
        ev, data = "message", ""
        for raw in r:
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
            elif line == "":
                if data:
                    try:
                        out.append((ev, json.loads(data)))
                    except json.JSONDecodeError:
                        pass
                ev, data = "message", ""
    return out


def get(path: str) -> dict:
    req = urllib.request.Request(BASE + path, headers=H)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def show(events: list[tuple[str, dict]], tag: str) -> dict:
    st: dict = {}
    for ev, d in events:
        if ev == "status":
            print(f"    · {d.get('message')}", flush=True)
        elif ev == "stages":
            st["stages"] = d["stages"]
            print("    阶段:", " | ".join(f"{s['name']}:{s['status']}" for s in d["stages"]), flush=True)
        elif ev == "question":
            st["question"] = d["question"]
        elif ev == "report":
            st["report"] = d["report"]
        elif ev == "session":
            st["session_id"] = d["session_id"]
        elif ev == "done":
            st["question_end"] = d["question_end"]
        elif ev == "error":
            print("    [error]", d.get("message"), flush=True)
    q = st.get("question")
    print(
        f"  [{tag}] question_end={st.get('question_end')}"
        + (f" 下一题=[{q['type']}] {q['skill_key']}" if q else " 无待答题"),
        flush=True,
    )
    return st


def main() -> int:
    proc = None
    if "18097" in BASE:
        log = (ROOT / "tests" / "_e2e_server.log").open("w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(ROOT / "tests" / "_e2e_server.py"), str(PORT)],
            cwd=str(ROOT), stdout=log, stderr=log,
        )
        for _ in range(90):
            time.sleep(1)
            try:
                if urllib.request.urlopen(f"{BASE}/health", timeout=2).status == 200:
                    break
            except Exception:
                continue
    try:
        from backend.kg.pg_store.client import connect

        with connect() as c:
            occ = c.execute(
                """SELECT o.id,o.name,COUNT(*) n FROM kg_edge e
                   JOIN kg_node o ON o.id=e.src_id AND o.type='occupation'
                   WHERE e.rel_type='requires' AND COALESCE(e.status,'published')='published'
                   GROUP BY 1,2 HAVING COUNT(*)>=8 ORDER BY n DESC LIMIT 1"""
            ).fetchone()
        print(f"岗位：{occ['name']}\n", flush=True)

        t = time.time()
        st = show(
            sse_post(
                "/v1/student/assessment/sessions/stream",
                {"occupation_id": occ["id"], "resume_text": GOOD[:120]},
            ),
            f"start {time.time()-t:.1f}s",
        )
        sid = st.get("session_id")
        n = 0
        while st.get("question") and n < 15:
            q = st["question"]
            n += 1
            ans = (
                max(q["options"], key=lambda o: o["level"])["value"]
                if q["type"] == "choice"
                else GOOD
            )
            t = time.time()
            st = show(
                sse_post(f"/v1/student/assessment/sessions/{sid}/answer/stream", {"answer": ans}),
                f"answer#{n} {time.time()-t:.1f}s",
            )

        rep = st.get("report") or {}
        print("\n" + "=" * 56, flush=True)
        print(f"匹配度 {rep.get('match_score')}%（覆盖 {rep.get('coverage')}%）", flush=True)
        print("雷达轴:", (rep.get("radar") or {}).get("categories"), flush=True)
        for s in (rep.get("radar") or {}).get("series", []):
            print(f"   {s['name']}: {s['scores']}", flush=True)
        print("优势:", [x["skill_key"] for x in (rep.get("strengths") or [])[:4]], flush=True)
        print("短板:", [x["skill_key"] for x in (rep.get("gaps") or [])[:4]], flush=True)

        # 契约校验
        ok = {
            "有 session_id": bool(sid),
            "三阶段齐全": len(st.get("stages") or []) == 3,
            "阶段全部 done": all(s["status"] == "done" for s in (st.get("stages") or [])),
            "question_end=True": st.get("question_end") is True,
            "有报告": bool(rep.get("match_score") is not None),
            "雷达双系列": len((rep.get("radar") or {}).get("series") or []) == 2,
            "报告已落库": bool(get(f"/v1/student/diagnosis/report?session_id={sid}")),
            "刷新可恢复": get(f"/v1/student/assessment/sessions/{sid}").get("exists") is True,
        }
        print("\n契约校验：", flush=True)
        for k, v in ok.items():
            print(f"   [{'PASS' if v else 'FAIL'}] {k}", flush=True)
        return 0 if all(ok.values()) else 1
    finally:
        if proc:
            proc.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
