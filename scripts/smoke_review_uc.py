import json
import urllib.request

BASE = "http://127.0.0.1:8088"
H = {"X-Test-Uid": "265357", "X-Test-Uname": "dev"}


def api(method, path, data=None):
    headers = dict(H)
    body = None
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def main():
    print("config", api("GET", "/v1/config"))
    print("me", api("GET", "/v1/me"))
    print("dash", api("GET", "/v1/admin/dashboard/summary")["pending_proposals"])
    cr = api(
        "POST",
        "/v1/admin/changes",
        {
            "entity_kind": "node",
            "action": "create",
            "dim_type": "major",
            "payload": {"type": "major", "name": "审核流测试专业-将删", "region": "CN"},
        },
    )
    print("submit", cr["id"], cr["action"], cr["title"])
    lst = api("GET", "/v1/admin/changes")
    print("pending", len(lst))
    ap = api("POST", f"/v1/admin/changes/{cr['id']}/approve", {})
    print("approve", ap.get("approved"), (ap.get("applied") or {}).get("node", {}).get("id", "")[:40])
    # submit delete
    nid = ap["applied"]["node"]["id"]
    d = api(
        "POST",
        "/v1/admin/changes",
        {"entity_kind": "node", "action": "delete", "target_id": nid, "payload": {}},
    )
    print("delete submit", d["id"])
    api("POST", f"/v1/admin/changes/{d['id']}/approve", {})
    print("deleted ok")
    # reject path
    cr2 = api(
        "POST",
        "/v1/admin/changes",
        {
            "entity_kind": "node",
            "action": "create",
            "dim_type": "occupation",
            "payload": {"type": "occupation", "name": "驳回测试岗", "region": "CN"},
        },
    )
    print("reject", api("POST", f"/v1/admin/changes/{cr2['id']}/reject", {}))
    print("pending after", len(api("GET", "/v1/admin/changes")))


if __name__ == "__main__":
    main()
