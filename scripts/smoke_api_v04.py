"""Smoke test API v0.4: headers, docs, read/write/review."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8088"
# Header values: keep ASCII in this smoke script (urllib latin-1); real clients/UTF-8 OK
H = {"user-id": "u_test", "user-name": "tester"}


def get(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            pass
        return e.code, body


def post(url: str, data: dict, headers: dict | None = None):
    h = {**(headers or {}), "Content-Type": "application/json"}
    req = urllib.request.Request(
        url, data=json.dumps(data, ensure_ascii=False).encode(), headers=h, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read().decode())


def main() -> None:
    q_comp = urllib.parse.quote("计算机")
    print("no auth search", get(f"{BASE}/v1/search?q={q_comp}&limit=2")[0])
    st, d = get(f"{BASE}/v1/search?q={q_comp}&limit=2", H)
    print("auth search", st, (d[0]["name"] if isinstance(d, list) and d else d))

    print("docs", urllib.request.urlopen(f"{BASE}/docs").status)
    print("guide", urllib.request.urlopen(f"{BASE}/api-guide").status)
    oa = json.loads(urllib.request.urlopen(f"{BASE}/openapi.json").read())
    print("openapi paths", sorted(oa["paths"].keys())[:15])
    print("securitySchemes", list((oa.get("components") or {}).get("securitySchemes") or {}))

    root = json.loads(urllib.request.urlopen(f"{BASE}/").read())
    print("servers", root.get("docs", {}).get("servers"))
    print("auth_temp", root.get("auth_temp"))

    st, d = get(f"{BASE}/v1/industries/tree?limit=20", H)
    print("industry tree", st, d.get("meta") if isinstance(d, dict) else d)

    q = urllib.parse.quote("软件")
    st, d = get(f"{BASE}/v1/majors/occupations?q={q}&limit=3", H)
    print("majors occ", st, len(d) if isinstance(d, list) else d)

    st, d = get(f"{BASE}/v1/occupations/skills?q={q}&limit=3", H)
    print("occ skills", st, len(d) if isinstance(d, list) else d)

    st, n = post(
        f"{BASE}/v1/kg/nodes",
        {"type": "major", "name": "临时测试专业X", "status": "draft"},
        H,
    )
    print("create node", st, n.get("id"), n.get("status"))

    st, prop = post(
        f"{BASE}/v1/review/proposals",
        {"kind": "node", "payload": {"type": "occupation", "name": "临时测试岗位Y"}},
        H,
    )
    print("proposal", prop.get("id"), prop.get("status"))
    st, dec = post(
        f"{BASE}/v1/review/proposals/{prop['id']}/decision",
        {"action": "approve"},
        H,
    )
    print("approve", st, dec.get("status"), (dec.get("applied") or {}).keys())


if __name__ == "__main__":
    main()
