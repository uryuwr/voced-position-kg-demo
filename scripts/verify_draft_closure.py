"""草稿闭环验收：改技能构成 → 前台不变 → 发布 → 前台才变（验收方视角）。

用**保持 Σweight 不变**的改法（两项间搬权重），否则 BR-03 会拦下发布、验不到成功路径。

上一轮我只把一项权重 +0.07，Σ 变 1.07 被 BR-03 拦下（门禁是对的），
所以没验到「发布成功后前台才变」。这次在两项之间搬权重，Σ 恒定。
"""
from __future__ import annotations

import hashlib
import json
import sys
import warnings

warnings.filterwarnings("ignore")
import backend.settings as _st  # noqa: E402

_st.AUTH_BYPASS = True
_st.AUTH_DEBUG = True
import backend.api.auth as _au  # noqa: E402

_au.AUTH_BYPASS = True
_au.AUTH_DEBUG = True
from fastapi.testclient import TestClient  # noqa: E402

from backend.api.main import app  # noqa: E402

cli = TestClient(app, headers={"X-Test-Uid": "0", "X-Test-Uname": "accept"}, raise_server_exceptions=False)
OCC = "CN:occupation:BOSS:100107"
Q = OCC.replace(":", "%3A")


def snap() -> dict[str, tuple[str, bytes]]:
    spec = app.openapi()
    out = {}
    for path, ops in (spec.get("paths") or {}).items():
        op = (ops or {}).get("get")
        if not op or not any(str(t).startswith("前台") for t in (op.get("tags") or [])):
            continue
        url, qs = path, []
        for p in op.get("parameters") or []:
            if p.get("in") == "path":
                url = url.replace("{" + p["name"] + "}", Q)
            elif p.get("required") and p.get("in") == "query":
                nm, sch = p["name"], (p.get("schema") or {})
                v = OCC if "id" in nm.lower() else (1 if sch.get("type") == "integer" else "a")
                qs.append(f"{nm}={str(v).replace(':', '%3A')}")
        full = url + ("?" + "&".join(qs) if qs else "")
        r = cli.get(full)
        if r.status_code < 500:
            out[full] = (hashlib.sha256(r.content).hexdigest()[:16], r.content)
    return out


def show_diff(a, b, tag):
    ks = sorted(k for k in a if a.get(k, ("",))[0] != b.get(k, ("",))[0])
    print(f"  {tag}: {len(ks)} 个接口不同")
    for k in ks[:4]:
        try:
            ja, jb = json.loads(a[k][1]), json.loads(b[k][1])
        except Exception:
            print(f"    {k[:90]}  (非 JSON)"); continue
        print(f"    {k[:90]}")
        for fld in ("version", "version_label", "updated_by", "weight_sum", "counts"):
            va, vb = _dig(ja, fld), _dig(jb, fld)
            if va != vb:
                print(f"        {fld}: {va!r} → {vb!r}")
    return ks


def _dig(o, key):
    if isinstance(o, dict):
        if key in o:
            return o[key]
        for v in o.values():
            r = _dig(v, key)
            if r is not None:
                return r
    elif isinstance(o, list):
        for v in o[:3]:
            r = _dig(v, key)
            if r is not None:
                return r
    return None


def comp():
    return cli.get(f"/v1/admin/composition?node_id={Q}").json()


def main() -> int:
    c0 = comp()
    items = c0.get("items") or []
    if len(items) < 2:
        print("  样本技能不足 2 项"); return 2
    a, b = items[0], items[1]
    print(f"  受试 {OCC}  Σweight={c0.get('weight_sum')}")
    print(f"  在两项间搬 0.05：{a['skill_key']} {a['weight']}→{round(a['weight']-0.05,4)} / "
          f"{b['skill_key']} {b['weight']}→{round(b['weight']+0.05,4)}")

    s0 = snap()
    print(f"  前台快照 {len(s0)} 个接口\n")

    for it, w in ((a, round(a["weight"] - 0.05, 4)), (b, round(b["weight"] + 0.05, 4))):
        r = cli.put(f"/v1/admin/composition?node_id={Q}&mode=set",
                    json={"skill_key": it["skill_key"], "level": it["selected_level"], "weight": w})
        if r.status_code != 200:
            print(f"  编辑失败 {r.status_code}: {r.text[:160]}"); return 1
    print("  ① 两项都改完（Σ 应仍为原值）")

    s1 = snap()
    d1 = show_diff(s0, s1, "  ② 编辑后前台")

    nd = cli.get(f"/v1/kg/node-detail?id={Q}&scope=manage").json()
    print(f"  ③ record_status={nd.get('record_status')!r} has_draft={nd.get('has_draft')!r}"
          f"  ← 编辑过就该是 draft/True")
    dr = cli.get("/v1/admin/drafts").json()
    # 括号是必须的：条件表达式优先级低于 or，写成 `A or B if C else D`
    # 会被解析成 `(A or B) if C else D` —— dr 是 dict 时恒取 []，判定永远是"不能"。
    rows = dr.get("items") if isinstance(dr, dict) else (dr if isinstance(dr, list) else [])
    hit = [x for x in (rows or []) if OCC in json.dumps(x, ensure_ascii=False)]
    print(f"  ③' /admin/drafts 里能否看到本单元: {'能' if hit else '不能'}")

    p = cli.post(f"/v1/admin/publish/node?node_id={Q}")
    print(f"  ④ 发布 → HTTP {p.status_code}")
    if p.status_code != 200:
        print("     " + p.text[:260])
    s2 = snap()
    d2 = show_diff(s0, s2, "  ⑤ 发布后前台（相对最初）")

    # 复原
    for it in (a, b):
        cli.put(f"/v1/admin/composition?node_id={Q}&mode=set",
                json={"skill_key": it["skill_key"], "level": it["selected_level"],
                      "weight": it["weight"]})
    cli.post(f"/v1/admin/publish/node?node_id={Q}")
    s3 = snap()
    d3 = show_diff(s0, s3, "  ⑥ 复原后（相对最初）")

    ok = len(d1) == 0 and nd.get("record_status") == "draft" and p.status_code == 200 and len(d2) > 0
    print(f"\n{'='*56}\n  {'通过' if ok else '不通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
