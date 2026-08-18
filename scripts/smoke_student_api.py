import json
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8088"
H = {"user-id": "u_stu_1", "user-name": "student_demo"}


def req(method: str, path: str, data=None):
    headers = dict(H)
    body = None
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            raw = json.loads(raw)
        except Exception:
            pass
        return e.code, raw


def main():
    print("tags", [t["name"] for t in json.loads(urllib.request.urlopen(BASE + "/openapi.json").read())["tags"]])
    st, d = req("GET", "/v1/student/professions?page=1&page_size=3")
    print("professions", st, d.get("total") if isinstance(d, dict) else d)
    pid = d["items"][0]["id"] if st == 200 and d.get("items") else None
    if pid:
        st, det = req("GET", "/v1/student/professions/" + urllib.parse.quote(pid, safe=""))
        print("prof detail", st, "positions", len(det.get("positions") or []) if isinstance(det, dict) else det)
        occs = det.get("positions") or []
        if occs:
            oid = occs[0]["id"]
            st, g = req("PUT", "/v1/student/goal", {"occupation_id": oid, "major_id": pid})
            print("goal", st, g.get("occupation_name") if isinstance(g, dict) else g)
            st, r = req(
                "POST",
                "/v1/student/diagnosis/resume",
                {
                    "content_text": "负责直播带货与投放，月均ROI达标，写短视频脚本，做数据分析看板。",
                    "target_occupation_id": oid,
                },
            )
            print("resume diag", st, (r.get("report") or {}).get("match_score") if isinstance(r, dict) else r)
            # 学习计划：需要 session_id，从上面这次简历诊断拿
            sess_id = (r.get("session_id") if isinstance(r, dict) else None)
            if sess_id:
                st, p = req(
                    "POST", "/v1/student/goal/learning-plan", {"session_id": sess_id}
                )
                # 502 = 学习空间未配置/不可用，冒烟环境常态，不当失败
                print("learning-plan", st,
                      {k: p.get(k) for k in ("plan_id", "created", "phases_count", "tasks_count")}
                      if st == 200 and isinstance(p, dict) else p)
    st, me = req("GET", "/v1/student/me")
    print("me", st, me.get("points") if isinstance(me, dict) else me, "badges", len(me.get("badges") or []) if isinstance(me, dict) else None)
    st, dash = req("GET", "/v1/admin/dashboard/summary")
    print("dashboard", st, dash if st != 200 else {k: dash[k] for k in list(dash)[:6]})
    # graph module still there
    st, s = req("GET", "/v1/search?q=" + urllib.parse.quote("计算机") + "&limit=2")
    print("graph search", st, len(s) if isinstance(s, list) else s)


if __name__ == "__main__":
    main()
