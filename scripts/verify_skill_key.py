"""skill_key 去中文化的专项闸门（2026-08-19）。

方案见 `backend/kg/skill_key.py` 的模块 docstring。这里验的是那套约定在**全链路**
上真的成立，而不只是函数单测过：

- 身份 = 库里存的 `attrs.skill_key`，一经分配永不重算 → **改名不换 key**
- 查找 = 按 `attrs.skill_name` 查库，**不是** md5 反推（改名后反推必然失效）
- md5 只是种子：新建时生成初始 key、SQL 给尚未写 key 的行兜底

跑在 8088 连的那个库上；写操作全造在 region=ZZ，跑完清理。

    PYTHONPATH=. python -X utf8 scripts/verify_skill_key.py
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
from backend.kg.skill_key import KEY_RE, derive_key  # noqa: E402

cli = TestClient(
    app,
    headers={"X-Test-Uid": "0", "X-Test-Uname": "skprobe"},
    raise_server_exceptions=False,
)
TAG = uuid.uuid5(uuid.NAMESPACE_URL, "verify_skill_key").hex[:8].upper()
REG = "ZZ"
R: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    R.append((bool(ok), name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def q(s: str) -> str:
    return quote(s, safe="")


def mk(name: str, region: str = REG) -> Any:
    return cli.post(
        "/v1/admin/skills",
        json={
            "name": name,
            "skill_name": name,
            "region": region,
            "category": "TECH",
            "levels": {
                f"L{i}": {"label": f"L{i}", "description": f"{name} 第{i}档描述够长过门禁"}
                for i in range(1, 6)
            },
        },
    )


# ══════════════════════════════════════════════ 1 全库没有中文 key
print("\n== 1 全库口径 ==")
with connect() as c:
    total = c.execute("SELECT count(*) c FROM kg_node WHERE type='skill_level'").fetchone()["c"]
    bad = c.execute(
        "SELECT count(*) c FROM kg_node WHERE type='skill_level' "
        "AND COALESCE(attrs::json->>'skill_key','') !~ '^SK[0-9a-f]{10}$'"
    ).fetchone()["c"]
    noname = c.execute(
        "SELECT count(*) c FROM kg_node WHERE type='skill_level' "
        "AND COALESCE(btrim(attrs::json->>'skill_name'),'') = ''"
    ).fetchone()["c"]
check(bad == 0, "attrs.skill_key 全是 code 形态", f"{bad}/{total} 不合规")
# skill_name 是展示名的唯一来源；缺了页面就只剩 SKxxxx
check(noname == 0, "attrs.skill_name 全部有值", f"{noname}/{total} 缺失")

for t, cols in (
    ("kg_skill_prereq", ("skill_key", "prereq_skill_key")),
    ("biz_assessment_item", ("skill_key",)),
    ("biz_assessment_question", ("skill_key",)),
):
    for col in cols:
        with connect() as c:
            n = c.execute(
                f"SELECT count(*) c FROM {t} "
                f"WHERE {col} IS NOT NULL AND {col} !~ '^SK[0-9a-f]{{10}}$'"
            ).fetchone()["c"]
        check(n == 0, f"{t}.{col} 全是 code 形态", f"{n} 条不合规")

# ══════════════════════════════════════════════ 2 URL 往返
print("\n== 2 用 code 走完四个接口 ==")
NAME = f"key探针{TAG}"
KEY = derive_key(NAME)
r = mk(NAME)
check(r.status_code == 200, "新建技能（不传 skill_key）", f"HTTP {r.status_code}")
check(
    (r.json().get("applied") or {}).get("skill_key") == KEY,
    "服务端生成的 key = md5(名字)",
    f"回执 {(r.json().get('applied') or {}).get('skill_key')} / 期望 {KEY}",
)
check(bool(KEY_RE.match(KEY)), "key 是纯字母数字（可安全进 URL）", KEY)
d = cli.get(f"/v1/admin/skills/{q(KEY)}", params={"region": REG})
check(d.status_code == 200, "GET 详情", f"HTTP {d.status_code}")
check(
    (d.json() or {}).get("skill_name") == NAME,
    "详情里的展示名是中文名，不是 code",
    str((d.json() or {}).get("skill_name")),
)
check(
    cli.get(f"/v1/admin/skills/{q(KEY)}/prerequisites", params={"region": REG}).status_code == 200,
    "GET 先修",
)

# ══════════════════════════════════════════════ 3 改名不换 key
print("\n== 3 改名不换 key（这条塌了先修与题库就会断链）==")
NAME2 = f"key探针改名后{TAG}"
cur = cli.get(f"/v1/admin/skills/{q(KEY)}", params={"region": REG}).json()
lv = {str(x["level"]): (x.get("description") or "") for x in (cur.get("levels") or [])}
rn = cli.patch(
    f"/v1/admin/skills/{q(KEY)}",
    json={"skill_key": KEY, "skill_name": NAME2, "name": NAME2, "category": "TECH",
          "levels": lv, "region": REG},
)
check(rn.status_code == 200, "改名", f"HTTP {rn.status_code}")
after = cli.get(f"/v1/admin/skills/{q(KEY)}", params={"region": REG})
check(after.status_code == 200, "改名后仍能用原 key 访问", f"HTTP {after.status_code}")
check(
    (after.json() or {}).get("skill_name") == NAME2,
    "展示名已更新",
    str((after.json() or {}).get("skill_name")),
)
check(
    derive_key(NAME2) != KEY,
    "新名字的 md5 与原 key 不同（正因如此才不能靠反推查找）",
    f"{derive_key(NAME2)} vs {KEY}",
)
with connect() as c:
    stored = c.execute(
        "SELECT DISTINCT attrs::json->>'skill_key' k FROM kg_node "
        "WHERE type='skill_level' AND region=%s AND attrs::json->>'skill_name'=%s",
        (REG, NAME2),
    ).fetchall()
check([x["k"] for x in stored] == [KEY], "库里存的 key 没变", str([x["k"] for x in stored]))

# ══════════════════════════════════════════════ 4 先修跟着 code 走
print("\n== 4 先修用 code ==")
NAME_B = f"key探针B{TAG}"
KEY_B = derive_key(NAME_B)
mk(NAME_B)
pr = cli.put(
    f"/v1/admin/skills/{q(KEY)}/prerequisites",
    json={"prereq_skill_keys": [KEY_B], "region": REG},
)
check(pr.status_code == 200, "写先修", f"HTTP {pr.status_code}")
got = cli.get(f"/v1/admin/skills/{q(KEY)}", params={"region": REG}).json()
pres = got.get("prerequisites") or []
check(
    [p.get("skill_key") for p in pres] == [KEY_B],
    "详情里的先修 key 正确",
    str([p.get("skill_key") for p in pres]),
)
check(
    [p.get("name") for p in pres] == [NAME_B],
    "先修显示的是名字不是 code",
    str([p.get("name") for p in pres]),
)

# ══════════════════════════════════════════════ 5 同名重复入库不产生第二个技能
print("\n== 5 同名再建一次（采集重跑的等价场景）==")
with connect() as c:
    before = c.execute(
        "SELECT count(DISTINCT attrs::json->>'skill_key') c FROM kg_node "
        "WHERE type='skill_level' AND region=%s", (REG,)
    ).fetchone()["c"]
mk(NAME_B)
with connect() as c:
    after_n = c.execute(
        "SELECT count(DISTINCT attrs::json->>'skill_key') c FROM kg_node "
        "WHERE type='skill_level' AND region=%s", (REG,)
    ).fetchone()["c"]
check(before == after_n, "同名重复提交不新增技能", f"{before} → {after_n}")

# ══════════════════════════════════════════════ 6 哈希碰撞被拒
print("\n== 6 哈希碰撞 ==")
# 直接伪造：把 B 的 key 塞给一个不同名字的新技能，服务端应当拒绝而不是静默合并
NAME_C = f"key探针C{TAG}"
rc = cli.post(
    "/v1/admin/skills",
    json={"name": NAME_C, "skill_name": NAME_C, "skill_key": KEY_B, "region": REG,
          "category": "TECH",
          "levels": {"L1": {"label": "L1", "description": f"{NAME_C} 第1档描述够长过门禁"}}},
)
check(
    rc.status_code >= 400,
    "拿别的技能的 key 建新技能被拒（不静默合并）",
    f"HTTP {rc.status_code} {rc.text[:110]}",
)

# ══════════════════════════════════════════════ 7 中文关键词仍能搜到
print("\n== 7 搜索 ==")
s1 = cli.get("/v1/admin/skills", params={"q": "key探针", "page_size": 20, "region": REG})
names = [x.get("skill_name") for x in (s1.json().get("items") or [])] if s1.status_code == 200 else []
check(s1.status_code == 200 and any(TAG in (n or "") for n in names),
      "技能库按中文名搜得到", f"HTTP {s1.status_code} 命中 {len(names)}")
s2 = cli.get("/v1/admin/composition/options", params={"q": "key探针", "limit": 20, "region": REG})
opts = s2.json() if s2.status_code == 200 else []
check(s2.status_code == 200 and any(TAG in (x.get("skill_name") or "") for x in opts),
      "下拉候选按中文名搜得到，且带展示名", f"HTTP {s2.status_code} 命中 {len(opts)}")
s3 = cli.get("/v1/admin/skills", params={"q": KEY, "page_size": 5, "region": REG})
check(s3.status_code == 200 and (s3.json().get("total") or 0) >= 1, "按 code 也搜得到")

# ══════════════════════════════════════════════ 清理
with connect() as c:
    c.execute("DELETE FROM kg_skill_prereq WHERE region=%s", (REG,))
    c.execute("DELETE FROM kg_edge WHERE region=%s", (REG,))
    c.execute("DELETE FROM kg_node WHERE region=%s", (REG,))
    c.commit()
    left = c.execute("SELECT count(*) c FROM kg_node WHERE region=%s", (REG,)).fetchone()["c"]
print(f"\n清理：region={REG} 残留 {left}")

print("\n" + "=" * 60)
bad_r = [x for x in R if not x[0]]
print(f"结果：{len(R) - len(bad_r)}/{len(R)} 通过")
for _, n, d in bad_r:
    print(f"  FAIL {n} — {d}")
sys.exit(1 if bad_r else 0)
