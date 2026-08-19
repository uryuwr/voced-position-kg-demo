"""写接口闸门：每个写接口都真调一次，只判 5xx 与「删得掉」。

**为什么需要这个**（2026-08-19 的两个线上 bug 直接催出来的）：

- `tests/e2e_robustness.py` 按设计只 fuzz **GET**（"对每个 GET 接口的每个参数注入
  越界/错类型/空/NUL"）。写接口一个都不调，所以「响应模型字段类型与实际不符」
  这类 500 它结构上抓不到 —— `PrereqOut.confidence` 声明成 float 而库里存的是
  `manual_seed`，先修技能一存就 500（**写库其实成功了**，报错在拼响应时），
  这个 bug 在仓库里躺着直到运营手点出来。
- `verify_draft_closure_4dim.py` 只做**追加式**编辑（往描述里加一段标记）。
  「清空 / 移除」是另一类操作：清掉 L5 的描述后 L5 删不掉，这条路径以前
  没有任何自动化会走到。
- 37 个写接口里，此前被任何自动化调用过的只有 7 个。

所以这个脚本的取舍：**广度优先**，每个写接口至少调一次真实请求，断言
① 不 5xx（响应模型不符会表现为 5xx）② 该删的删得掉、该拒的拒得掉。
不追求业务断言的深度 —— 深度由 `verify_draft_closure_4dim.py` 负责。

数据全部造在 **region=ZZ** 下，跑完清理。不碰 CN 的真实数据。

一个反复踩的坑：`PATCH /v1/admin/skills/{key}` **没有 region 查询参数**，region 在
请求体里且默认 `"CN"`。把它放 query 会被静默忽略、回落成 CN，于是「删掉某一档」
去 CN 里找 ZZ 的档位、什么也没找到，看起来像功能坏了。凡是这个接口，region 一律写进 body。

    PYTHONPATH=. python -X utf8 scripts/verify_write_paths.py
"""

from __future__ import annotations

import sys
import uuid
import warnings
from typing import Any
from urllib.parse import quote

warnings.filterwarnings("ignore")
import backend.settings as _st  # noqa: E402

_st.AUTH_BYPASS = True
_st.AUTH_DEBUG = True
import backend.api.auth as _au  # noqa: E402

_au.AUTH_BYPASS = True
_au.AUTH_DEBUG = True
from fastapi.testclient import TestClient  # noqa: E402

from backend.api.main import app  # noqa: E402
from backend.kg.pg_store.client import connect  # noqa: E402
from backend.kg.skill_key import derive_key  # noqa: E402

cli = TestClient(
    app,
    headers={"X-Test-Uid": "0", "X-Test-Uname": "wpath"},
    raise_server_exceptions=False,
)

TAG = uuid.uuid5(uuid.NAMESPACE_URL, "verify_write_paths").hex[:8].upper()
REG = "ZZ"
RESULTS: list[tuple[bool, str, str]] = []
CALLED: set[str] = set()


def check(ok: bool, name: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def call(method: str, path: str, **kw: Any) -> Any:
    """调一次并登记「这个接口被覆盖到了」。"""
    CALLED.add(f"{method.upper()} {kw.pop('_spec', path)}")
    return getattr(cli, method.lower())(path, **kw)


def no5xx(r: Any, name: str) -> bool:
    body = ""
    if r.status_code >= 500:
        body = (r.text or "")[:200]
    return check(r.status_code < 500, name, f"HTTP {r.status_code} {body}".strip())


def q(s: str) -> str:
    return quote(s, safe="")


# ════════════════════════════════════════════════ 节点：建 / 改 / 删
print("\n== 节点 /v1/kg/nodes ==")
ind = call(
    "post",
    "/v1/kg/nodes",
    json={
        "type": "industry",
        "name": f"写路径探针行业{TAG}",
        "region": REG,
        "source_url": "https://example.invalid/wpath",
        "license": "internal",
        "confidence": "manual_seed",
    },
)
no5xx(ind, "POST /v1/kg/nodes（行业）")
ind_id = (ind.json() or {}).get("id") if ind.status_code < 400 else None

occ = call(
    "post",
    "/v1/kg/nodes",
    json={
        "type": "occupation",
        "name": f"写路径探针岗位{TAG}",
        "region": REG,
        "source_url": "https://example.invalid/wpath",
        "license": "internal",
        "confidence": "manual_seed",
        **({"industry_ids": [ind_id]} if ind_id else {}),
    },
    _spec="/v1/kg/nodes",
)
no5xx(occ, "POST /v1/kg/nodes（岗位 + 关联行业）")
occ_id = (occ.json() or {}).get("id") if occ.status_code < 400 else None

if occ_id:
    no5xx(
        call("patch", f"/v1/kg/nodes/{q(occ_id)}", json={"description": "改一下"},
             _spec="/v1/kg/nodes/{node_id}"),
        "PATCH /v1/kg/nodes/{node_id}",
    )
    # 只改自身字段，**不该动关联**（extract_link_ids：键缺席 = 不管这类关联）
    if ind_id:
        with connect() as c:
            n = c.execute(
                "SELECT count(*) c FROM kg_edge WHERE is_draft "
                "AND COALESCE(target_status,'')='archived' AND src_id=%s",
                (occ_id,),
            ).fetchone()["c"]
        check(n == 0, "改节点字段不会把已有关联标成待归档", f"墓碑草稿 {n} 条")

# ════════════════════════════════════════════════ 边：建 / 删
print("\n== 边 /v1/kg/edges ==")
edge_id = None
if ind_id and occ_id:
    e = call(
        "post",
        "/v1/kg/edges",
        json={
            "src_id": occ_id,
            "dst_id": ind_id,
            "rel_type": "belongs_to",
            "region": REG,
            "source_url": "https://example.invalid/wpath",
            "license": "internal",
            "confidence": "manual_seed",
        },
    )
    no5xx(e, "POST /v1/kg/edges")
    edge_id = (e.json() or {}).get("id") if e.status_code < 400 else None
if edge_id:
    no5xx(
        call("delete", f"/v1/kg/edges/{q(edge_id)}", _spec="/v1/kg/edges/{edge_id}"),
        "DELETE /v1/kg/edges/{edge_id}",
    )

# ════════════════════════════════════════════════ 技能：建 / 改 / 删档 / 删
print("\n== 技能 /v1/admin/skills ==")
SK = f"写路径探针技能{TAG}"
# **URL 用 code、body 用名字**：2026-08-19 起 skill_key 是 SKxxxxxxxxxx（服务端按
# 名字生成），拿中文名去拼路径会 404。这两个变量分开正是为了不再混用。
SK_K = derive_key(SK)
sk_body = {
    "skill_key": SK,
    "name": SK,
    "region": REG,
    "category": "TECH",
    "levels": {
        f"L{i}": {"label": f"L{i}", "description": f"{SK} 第{i}档的描述，写够长度过门禁"}
        for i in range(1, 6)
    },
}
no5xx(call("post", "/v1/admin/skills/preview", json=sk_body), "POST /v1/admin/skills/preview")
no5xx(call("post", "/v1/admin/skills", json=sk_body), "POST /v1/admin/skills")


def sk_levels() -> list[str]:
    d = call("get", f"/v1/admin/skills/{q(SK_K)}", params={"region": REG},
             _spec="/v1/admin/skills/{skill_key}").json()
    return sorted(str(x["level"]) for x in (d.get("levels") or []))


check(sk_levels() == ["1", "2", "3", "4", "5"], "新建技能五档齐全", str(sk_levels()))

# **清空某一档 = 移除那一档**。前端在 buildBody() 里 `if (desc)` —— 描述被清空的
# 那档整个不出现在 payload 里，所以「省略」必须被理解成「删掉」。
# 这条路径以前没有任何自动化走过，运营删 L5 删不掉就是这么漏出去的。
cur = call("get", f"/v1/admin/skills/{q(SK_K)}", params={"region": REG},
           _spec="/v1/admin/skills/{skill_key}").json()
lv = {str(x["level"]): (x.get("description") or "") for x in (cur.get("levels") or [])}
lv.pop("5", None)
no5xx(
    call("patch", f"/v1/admin/skills/{q(SK_K)}", params={"region": REG},
         json={"skill_name": SK, "category": "TECH", "levels": lv, "region": REG, "skill_key": SK_K},
         _spec="/v1/admin/skills/{skill_key}"),
    "PATCH /v1/admin/skills/{skill_key}（清空 L5）",
)
check(sk_levels() == ["1", "2", "3", "4"], "清空 L5 后管理台不再返回 L5", str(sk_levels()))
# 再保存一次不该重复生成墓碑（否则待发布页永远清不掉）
call("patch", f"/v1/admin/skills/{q(SK_K)}",
     json={"skill_name": SK, "category": "TECH", "levels": lv, "region": REG, "skill_key": SK_K},
     _spec="/v1/admin/skills/{skill_key}")
with connect() as c:
    tomb = c.execute(
        "SELECT count(*) c FROM kg_node WHERE type='skill_level' AND is_draft "
        "AND COALESCE(target_status,'')='archived' AND name LIKE %s",
        (SK + " %",),
    ).fetchone()["c"]
check(tomb <= 1, "重复保存不会堆积档位墓碑", f"墓碑 {tomb} 条")

# ════════════════════════════════════════════════ 先修：读 / 加 / 整体替换 / 删 / 成环
print("\n== 先修 /v1/admin/skills/{key}/prerequisites ==")
SK2 = f"写路径探针技能B{TAG}"
SK2_K = derive_key(SK2)
call("post", "/v1/admin/skills", json={**sk_body, "skill_key": SK2, "name": SK2},
     _spec="/v1/admin/skills")

no5xx(
    call("get", f"/v1/admin/skills/{q(SK_K)}/prerequisites", params={"region": REG},
         _spec="/v1/admin/skills/{skill_key}/prerequisites"),
    "GET  …/prerequisites",
)
no5xx(
    call("post", f"/v1/admin/skills/{q(SK_K)}/prerequisites",
         json={"prereq_skill_key": SK2_K, "region": REG},
         _spec="/v1/admin/skills/{skill_key}/prerequisites"),
    "POST …/prerequisites（单条）",
)
# PUT 整体替换 —— 这是运营界面「保存先修」按的那个，也是 500 的那个
r = call("put", f"/v1/admin/skills/{q(SK_K)}/prerequisites",
         json={"prereq_skill_keys": [SK2_K], "region": REG},
         _spec="/v1/admin/skills/{skill_key}/prerequisites")
no5xx(r, "PUT  …/prerequisites（整体替换）")
if r.status_code == 200:
    body = r.json()
    ok_shape = isinstance(body, list) and all(
        isinstance(x.get("confidence"), (str, type(None))) for x in body
    )
    check(ok_shape, "先修回执的 confidence 是文本（不是分数）",
          str([x.get("confidence") for x in body])[:80])

# 成环必须在**保存时**就拒掉：等到发布门禁（BR-05）才报太晚，
# 中间这段时间运营以为存好了
r = call("put", f"/v1/admin/skills/{q(SK2_K)}/prerequisites",
         json={"prereq_skill_keys": [SK_K], "region": REG},
         _spec="/v1/admin/skills/{skill_key}/prerequisites")
check(r.status_code == 400, "反向先修成环被保存时拒掉", f"HTTP {r.status_code}")
# 而且被拒之后 SK2 原有的先修列表不能被清空（原实现先 DELETE 再逐条 add）
after = call("get", f"/v1/admin/skills/{q(SK_K)}/prerequisites", params={"region": REG},
             _spec="/v1/admin/skills/{skill_key}/prerequisites").json()
check(len(after) == 1, "成环被拒后另一侧的先修列表仍在", f"{len(after)} 条")
r = call("put", f"/v1/admin/skills/{q(SK_K)}/prerequisites",
         json={"prereq_skill_keys": [SK_K], "region": REG},
         _spec="/v1/admin/skills/{skill_key}/prerequisites")
check(r.status_code == 400, "自己做自己的先修被拒", f"HTTP {r.status_code}")
no5xx(
    call("delete", f"/v1/admin/skills/{q(SK_K)}/prerequisites/{q(SK2_K)}",
         params={"region": REG},
         _spec="/v1/admin/skills/{skill_key}/prerequisites/{prereq_key}"),
    "DELETE …/prerequisites/{prereq_key}",
)

# ════════════════════════════════════════════════ 技能构成
print("\n== 技能构成 /v1/admin/composition ==")
if occ_id:
    no5xx(
        call("put", "/v1/admin/composition",
             params={"node_id": occ_id},
             json={"items": [{"skill_key": SK_K, "weight": 1.0, "required_level": 3}]}),
        "PUT  /v1/admin/composition",
    )
    no5xx(
        call("post", "/v1/admin/composition/normalize", params={"node_id": occ_id}),
        "POST /v1/admin/composition/normalize",
    )
    no5xx(
        call("delete", "/v1/admin/composition",
             params={"node_id": occ_id, "skill_key": SK_K}),
        "DELETE /v1/admin/composition",
    )

# ════════════════════════════════════════════════ 发布 / 丢弃
print("\n== 发布 /v1/admin/publish ==")
if occ_id:
    no5xx(
        call("get", "/v1/admin/publish/validate",
             params={"node_type": "occupation", "node_id": occ_id, "region": REG}),
        "GET  /v1/admin/publish/validate",
    )
    no5xx(
        call("post", "/v1/admin/publish/validate",
             params={"node_type": "occupation", "node_id": occ_id, "region": REG}),
        "POST /v1/admin/publish/validate",
    )
    no5xx(call("post", "/v1/admin/publish/node", params={"node_id": occ_id}),
          "POST /v1/admin/publish/node")
    no5xx(call("post", "/v1/admin/publish/batch", json={"node_ids": [occ_id]}),
          "POST /v1/admin/publish/batch")
    no5xx(call("post", "/v1/admin/publish/demote", params={"node_id": occ_id}),
          "POST /v1/admin/publish/demote")
    no5xx(call("delete", "/v1/admin/draft", params={"node_id": occ_id}),
          "DELETE /v1/admin/draft")

# ════════════════════════════════════════════════ 变更队列
print("\n== 变更 /v1/admin/changes ==")
no5xx(
    call("post", "/v1/admin/changes",
         json={"entity_kind": "node", "action": "update", "dim_type": "occupation",
               "target_id": occ_id or "x", "payload": {"description": "队列探针"}}),
    "POST /v1/admin/changes",
)

# ════════════════════════════════════════════════ 删除（放最后，顺带当清理）
print("\n== 删除 ==")
no5xx(
    call("delete", f"/v1/admin/skills/{q(SK_K)}", params={"region": REG},
         _spec="/v1/admin/skills/{skill_key}"),
    "DELETE /v1/admin/skills/{skill_key}",
)
call("delete", f"/v1/admin/skills/{q(SK2_K)}", params={"region": REG},
     _spec="/v1/admin/skills/{skill_key}")
for nid in (occ_id, ind_id):
    if nid:
        no5xx(
            call("delete", f"/v1/kg/nodes/{q(nid)}", _spec="/v1/kg/nodes/{node_id}"),
            f"DELETE /v1/kg/nodes/{{node_id}}（{nid[:28]}…）",
        )

# ════════════════════════════════════════════════ 清理 + 覆盖率
with connect() as c:
    c.execute("DELETE FROM kg_skill_prereq WHERE region=%s", (REG,))
    c.execute("DELETE FROM kg_edge WHERE region=%s", (REG,))
    c.execute("DELETE FROM kg_node WHERE region=%s", (REG,))
    c.execute("DELETE FROM kg_change_request WHERE title LIKE %s", ("%" + TAG + "%",))
    c.commit()
    left = c.execute("SELECT count(*) c FROM kg_node WHERE region=%s", (REG,)).fetchone()["c"]
print(f"\n清理：region={REG} 残留节点 {left}")

spec = app.openapi()
all_w = {
    f"{m.upper()} {p}"
    for p, ops in (spec.get("paths") or {}).items()
    for m in ops
    if m in ("post", "put", "patch", "delete")
}
# 学员端 LLM / 测评 / 简历那批要真会话与网关，不在这个脚本的射程内，
# 由 tests/run_assessment_demo.py 与 e2e_skill_level.py 覆盖。
skip_prefix = ("POST /v1/student/", "PUT /v1/student/", "DELETE /v1/student/",
               "POST /v1/review/", "POST /v1/graph/expand",
               "POST /v1/admin/changes/{change_id}")
todo = sorted(
    x for x in all_w - CALLED if not x.startswith(skip_prefix)
)
print("\n" + "=" * 62)
bad = [r for r in RESULTS if not r[0]]
print(f"结果：{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
for _, n, d in bad:
    print(f"  FAIL {n} — {d}")
print(f"写接口覆盖：{len(CALLED & all_w)}/{len(all_w)}（本脚本射程内未覆盖 {len(todo)} 个）")
for x in todo:
    print(f"  未覆盖 {x}")
sys.exit(1 if bad else 0)
