import json
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8088"
H = {"user-id": "u_admin_ui", "user-name": "admin_ui"}


def get_page(path: str):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return r.status, r.headers.get("content-type", ""), len(r.read())


def api(method: str, path: str, data=None):
    headers = dict(H)
    body = None
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main():
    for p in ["/admin", "/admin-console", "/js/api-client.js", "/admin-cytoscape"]:
        st, ct, n = get_page(p)
        print(p, st, ct[:40], n)

    print("dash", api("GET", "/v1/admin/dashboard/summary")["kg_nodes"])
    print("list major", api("GET", "/v1/kg/nodes?type=major&page=1&page_size=3")["total"])
    n = api(
        "POST",
        "/v1/kg/nodes",
        {"type": "major", "name": "管理端联调专业A", "status": "draft", "region": "CN"},
    )
    print("create", n["id"][:50], n.get("status"))
    nid = urllib.parse.quote(n["id"], safe="")
    n2 = api("PATCH", f"/v1/kg/nodes/{nid}", {"description": "updated by smoke"})
    print("patch", n2.get("description"))
    prop = api(
        "POST",
        "/v1/review/proposals",
        {
            "kind": "node",
            "payload": {"type": "occupation", "name": "管理端联调岗位B", "region": "CN"},
        },
    )
    print("proposal", prop["id"], prop["status"])
    dec = api(
        "POST",
        f"/v1/review/proposals/{prop['id']}/decision",
        {"action": "approve"},
    )
    print("approve", dec["status"])
    oa = json.loads(urllib.request.urlopen(BASE + "/openapi.json").read())
    print("openapi", oa["info"]["version"])


if __name__ == "__main__":
    main()
