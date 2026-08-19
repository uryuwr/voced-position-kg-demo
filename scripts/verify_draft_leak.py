"""独立的草稿泄漏验收（验收方视角，不复用实现/测试两方的接口清单）。

与 `verify_draft_isolation.py` 的分工：那个做「编辑前后逐字节相同」，
这个做「造一条草稿后遍历路由表找指纹」，并额外统计**有效样本数**——
第一版就是因为全部请求回 401、空响应里当然没有指纹而假绿，
所以状态码分布与「真正返回了内容的接口数」必须打印出来。两个角度都要留。

做法：插一个名字带唯一指纹的草稿节点 + 一条草稿边 → 遍历 app 路由表里**所有**
前台 GET 接口 → 断言指纹一次都不出现在任何响应体里。

不手写接口清单：手写的一定会漏，漏掉的那个正是泄漏点。
"""
from __future__ import annotations

import json
import sys
import uuid
import warnings

warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient

# 鉴权旁路必须在 import app 之前、且在 dotenv 加载之后于进程内改内存值：
# `.env` 以 override=True 加载，命令行注入 AUTH_BYPASS 无效（CLAUDE.md「配置」）。
# 不开旁路的话每个请求都回 401，**空响应里当然找不到指纹，整个测试变成假绿**——
# 第一版就是这么骗过自己的。
import backend.settings as _settings  # noqa: E402

_settings.AUTH_BYPASS = True
_settings.AUTH_DEBUG = True

import backend.api.auth as _auth  # noqa: E402

_auth.AUTH_BYPASS = True
_auth.AUTH_DEBUG = True

from backend.api.main import app  # noqa: E402
from backend.kg.pg_store.client import connect  # noqa: E402

FP = "ZLEAK" + uuid.uuid4().hex[:8].upper()          # 指纹，正常数据里绝不会有
NODE_ID = f"CN:occupation:{FP}"
SKILL_ID = f"CN:skill_level:{FP}"
EDGE_ID = f"edge:{NODE_ID}|requires|{SKILL_ID}"

# 只扫前台：管理台本来就该看得见草稿
FRONT_TAGS = ("前台",)


def insert_drafts() -> None:
    with connect() as c:
        for nid, ntype, name in (
            (NODE_ID, "occupation", f"{FP}岗位"),
            (SKILL_ID, "skill_level", f"{FP}技能"),
        ):
            c.execute(
                """INSERT INTO kg_node (id, is_draft, region, type, name, status,
                        attrs, source_system, source_id, source_url, license,
                        fetched_at, confidence)
                   VALUES (%s, true, 'CN', %s, %s, 'draft', %s, 'MANUAL', %s,
                           'manual://leakcheck', 'test', '2026-08-18', 'manual_seed')""",
                (nid, ntype, name, json.dumps({"level": 3, "skill_key": f"{FP}技能"}), nid),
            )
        c.execute(
            """INSERT INTO kg_edge (id, is_draft, src_id, dst_id, rel_type, region,
                    weight, status, unit_id, source_system, source_id, source_url,
                    license, fetched_at, confidence)
               VALUES (%s, true, %s, %s, 'requires', 'CN', 0.5, 'draft', %s,
                       'MANUAL', %s, 'manual://leakcheck', 'test', '2026-08-18', 'manual_seed')""",
            (EDGE_ID, NODE_ID, SKILL_ID, NODE_ID, EDGE_ID),
        )
        c.commit()


def cleanup() -> None:
    with connect() as c:
        c.execute("DELETE FROM kg_edge WHERE id = %s", (EDGE_ID,))
        c.execute("DELETE FROM kg_node WHERE id = ANY(%s)", ([NODE_ID, SKILL_ID],))
        c.commit()


def front_get_paths() -> list[str]:
    """从 openapi 现摘前台 GET 接口，路径参数用指纹或常见值填。"""
    spec = app.openapi()
    out = []
    for path, ops in (spec.get("paths") or {}).items():
        op = (ops or {}).get("get")
        if not op:
            continue
        if not any(str(t).startswith(FRONT_TAGS) for t in (op.get("tags") or [])):
            continue
        params = {}
        for p in op.get("parameters") or []:
            if p.get("in") == "path":
                params[p["name"]] = NODE_ID
            elif p.get("required") and p.get("in") == "query":
                nm = p["name"]
                sch = p.get("schema") or {}
                if "id" in nm.lower():
                    params[nm] = NODE_ID
                elif sch.get("type") == "integer":
                    params[nm] = 1
                else:
                    params[nm] = FP
        url = path
        qs = []
        for k, v in params.items():
            if "{" + k + "}" in url:
                url = url.replace("{" + k + "}", str(v).replace(":", "%3A"))
            else:
                qs.append(f"{k}={str(v).replace(':', '%3A')}")
        # 再补一轮：搜索类接口用指纹当关键词，最容易把草稿搜出来
        if "q" not in params:
            qs.append(f"q={FP}")
        out.append(url + ("?" + "&".join(qs) if qs else ""))
    return sorted(set(out))


def main() -> int:
    insert_drafts()
    try:
        cli = TestClient(app, headers={"X-Test-Uid": "0", "X-Test-Uname": "leakcheck"})
        paths = front_get_paths()
        leaked, errored, ok = [], [], 0
        codes, real200, echoed = {}, [], []
        for p in paths:
            try:
                r = cli.get(p)
            except Exception as e:  # noqa: BLE001
                errored.append((p, f"{type(e).__name__}: {e}"[:80]))
                continue
            if r.status_code >= 500:
                errored.append((p, f"HTTP {r.status_code}"))
                continue
            codes[r.status_code] = codes.get(r.status_code, 0) + 1
            # 只认 2xx：4xx 的校验错误会把我传进去的入参原样回显，
            # 那是"回显自己的输入"不是"泄漏库里的草稿"，按泄漏算会造成假红
            if FP in r.text and 200 <= r.status_code < 300:
                leaked.append((p, r.status_code))
            elif FP in r.text:
                echoed.append((p, r.status_code))
            else:
                ok += 1
                if r.status_code == 200 and len(r.text) > 40:
                    real200.append(p)
        print(f"  指纹 {FP}")
        print(f"  扫了 {len(paths)} 个前台 GET 接口：干净 {ok} · 泄漏 {len(leaked)} · 异常 {len(errored)}")
        print(f"  状态码分布: {dict(sorted(codes.items()))}")
        print(f"  其中真正返回了内容(200 且非空)的: {len(real200)} 个 —— 这些才是有效样本")
        print(f"  入参回显(4xx 里出现指纹，非泄漏): {len(echoed)} 个")
        for p, code in leaked:
            print(f"    [泄漏] HTTP {code}  {p[:110]}")
        for p, why in errored[:8]:
            print(f"    [异常] {why}  {p[:100]}")
        print()
        print("  草稿在管理台应当可见（反向对照）：", end=" ")
        r = cli.get(f"/v1/node?id={NODE_ID.replace(':', '%3A')}&scope=manage")
        print("可见 ✓" if FP in r.text else f"不可见 ✗ (HTTP {r.status_code})")
        return 1 if leaked else 0
    finally:
        cleanup()
        print("  已清理测试草稿行")


if __name__ == "__main__":
    sys.exit(main())
