"""删除动作遇到未发布草稿时不能 500，且必须把草稿一并丢弃。

背景：主键改成 `(id, is_draft)` 后同一 id 有两行，草稿行 `status` 恒为 `'draft'`
（DB 级 CHECK `ck_kg_node_draft_status` 钉住）。删除路径若不钉 `NOT is_draft`，
就会把 `'archived'` 写到草稿行上撞 CHECK —— 表现是「编辑过某条记录再删它必然 500」，
而删除其实已经部分执行。发布路径踩过同一个坑（用户手点发现的那个 500）。

反过来只钉 `NOT is_draft` 也不够：草稿行留着的话，这条已删记录会继续挂在
「待发布」页，点一下发布就把 status 写回 published —— 草稿能撤销一个「立即生效」
的删除。所以删除必须同时丢弃草稿。

    python -X utf8 scripts/verify_delete_with_draft.py     # 需 PYTHONPATH=.
"""

from __future__ import annotations

import sys
import warnings
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

cli = TestClient(
    app,
    headers={"X-Test-Uid": "0", "X-Test-Uname": "delprobe"},
    raise_server_exceptions=False,
)

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def rows_of(node_id: str) -> dict[bool, str]:
    with connect() as conn:
        return {
            bool(r["is_draft"]): r["status"]
            for r in conn.execute(
                "SELECT is_draft, status FROM kg_node WHERE id = %s", (node_id,)
            ).fetchall()
        }


def draft_unit_ids() -> set[str]:
    r = cli.get("/v1/admin/drafts", params={"limit": 200})
    if r.status_code != 200:
        return set()
    body = r.json()
    items = body.get("items") if isinstance(body, dict) else body
    return {i.get("id") or i.get("unit_id") for i in (items or [])}


# ---------------------------------------------------------------- 场景 1：删节点
print("\n== 1 编辑过的节点再删除（节点 = 行业/专业/岗位）==")
# 不能用「新建一个再发布」来造线上行：新建岗位没有技能，Σweight=0 过不了 BR-03，
# 发布必然 400（已知待决问题，界面上也没提示）。所以拿一条现成的已发布节点，
# 测完把 status 还原 —— 隔离库，软删可逆。
with connect() as conn:
    pick = conn.execute(
        """
        SELECT id, name, status FROM kg_node
        WHERE type = 'occupation' AND NOT is_draft
          AND COALESCE(status, 'published') = 'published'
          AND NOT EXISTS (SELECT 1 FROM kg_node d WHERE d.id = kg_node.id AND d.is_draft)
        ORDER BY id LIMIT 1
        """
    ).fetchone()
if not pick:
    print("  库里找不到「无草稿的已发布岗位」，跳过场景 1")
    sys.exit(2)
nid, nname = pick["id"], pick["name"]
print(f"  拿 {nname} / {nid}")
check(rows_of(nid).get(False) == "published", "起点：只有线上行、状态 published", f"行={rows_of(nid)}")

# 编辑 → 造出草稿行
cli.patch(f"/v1/kg/nodes/{nid}", json={"description": "删除探针：改一下，造草稿行"})
mid = rows_of(nid)
check(
    mid.get(True) == "draft" and mid.get(False) is not None,
    "编辑后同时存在线上行与草稿行",
    f"行={mid}",
)

# 删除前记下**本来就是 published** 的边：下面只还原这些。
# 无条件把该节点所有 archived 边刷回 published，会把探针跑之前就已归档的边一起
# 复活 —— 那是反方向的静默污染，比不还原更难发现。
with connect() as conn:
    _pub_edges = [
        r["id"]
        for r in conn.execute(
            """
            SELECT id FROM kg_edge
            WHERE (src_id=%s OR dst_id=%s) AND NOT is_draft
              AND COALESCE(status,'published')='published'
            """,
            (nid, nid),
        ).fetchall()
    ]

# 删除 —— 这一步在修复前是 500
dele = cli.delete(f"/v1/kg/nodes/{nid}")
check(dele.status_code < 500, "删除不 500（修复前撞 ck_kg_node_draft_status）", f"HTTP {dele.status_code}")
after = rows_of(nid)
check(after.get(False) == "archived", "线上行已归档", f"行={after}")
check(True not in after, "草稿行已被丢弃（否则待发布页能把已删记录复活）", f"行={after}")
check(nid not in draft_unit_ids(), "已删节点不再出现在 /admin/drafts")

with connect() as conn:
    conn.execute(
        "UPDATE kg_node SET status='published' WHERE id=%s AND NOT is_draft", (nid,)
    )
    conn.execute("DELETE FROM kg_node WHERE id=%s AND is_draft", (nid,))
    # **边也要还原**：`archive_node` 会连这个节点的所有边一起归档，只还原节点的话
    # 这个岗位的技能构成、所属专业、晋升链路全部消失，而页面上它还是「已发布」——
    # 一次探针跑完就把一个真实岗位打空了（Java 被这么打空过一次，7 条 requires +
    # 4 条 prepares_for + 2 条 advances_to + 2 条 belongs_to）。
    restored = (
        conn.execute(
            "UPDATE kg_edge SET status='published' "
            "WHERE id = ANY(%s) AND NOT is_draft",
            (_pub_edges,),
        ).rowcount
        if _pub_edges
        else 0
    )
    conn.commit()
print(f"  已把 {nname} 还原成 published（连同 {restored} 条边）")

# ---------------------------------------------------------------- 场景 2：删技能
print("\n== 2 编辑过的技能再删除（技能 = L1–L5 一整组）==")
# skill_key 用项目里那份唯一定义（attrs.skill_key，见 CLAUDE.md「一技能一档」），
# 不要自己按 " · L5" 拆节点名 —— 拆出来的是节点名不是 key，接口会回 404。
from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL  # noqa: E402

with connect() as conn:
    row = conn.execute(
        f"""
        SELECT ({SKILL_KEY_SQL}) AS skill_key, count(*) AS c
        FROM kg_node n
        WHERE n.type = 'skill_level' AND NOT n.is_draft
          AND COALESCE(n.status, 'published') = 'published'
          AND n.region = 'CN'
          AND NOT EXISTS (
            SELECT 1 FROM kg_edge e
            WHERE (e.src_id = n.id OR e.dst_id = n.id)
              AND COALESCE(e.status, 'published') = 'published'
          )
        GROUP BY 1 HAVING count(*) >= 2
        ORDER BY count(*) DESC LIMIT 1
        """
    ).fetchone()
if not row or not row["skill_key"]:
    print("  库里找不到「无引用的多档技能」，跳过场景 2")
else:
    skill_key = row["skill_key"]
    print(f"  拿 {skill_key}（{row['c']} 个档位，无 published 边引用）")
    # PATCH /admin/skills 是**全量语义**：不带 levels 直接 400（"levels 至少包含一档"）。
    # 所以先 GET 回填，只改 description —— 界面一直带 levels，所以这条没被暴露过。
    cur = cli.get(
        f"/v1/admin/skills/{quote(skill_key, safe='')}", params={"region": "CN"}
    ).json()
    pat = cli.patch(
        f"/v1/admin/skills/{quote(skill_key, safe='')}",
        params={"region": "CN"},
        json={
            "skill_name": cur.get("skill_name") or skill_key,
            "category": cur.get("category"),
            "description": "删除探针：改一下，造草稿行",
            # 注意方向不对称：GET 的 levels 是 **list**，PATCH 要的是 **dict**
            # （{"1": "...", "2": "..."}）。照抄 GET 的结构会 422。
            "levels": {
                str(lv["level"]): (lv.get("description") or "")
                for lv in (cur.get("levels") or [])
            },
        },
    )
    with connect() as conn:
        d = conn.execute(
            f"SELECT count(*) c FROM kg_node n WHERE n.type='skill_level' "
            f"AND ({SKILL_KEY_SQL})=%s AND n.is_draft",
            (skill_key,),
        ).fetchone()["c"]
    check(pat.status_code == 200 and d > 0, "编辑技能造出草稿行", f"patch HTTP {pat.status_code} · 草稿行={d}")

    dele = cli.delete(
        f"/v1/admin/skills/{quote(skill_key, safe='')}", params={"region": "CN"}
    )
    check(dele.status_code < 500, "删技能不 500（修复前必然撞 CHECK）", f"HTTP {dele.status_code}")
    if dele.status_code == 200:
        body = dele.json()
        check(
            body.get("discarded_drafts", 0) > 0,
            "回执里报告了丢弃的草稿数",
            f"discarded_drafts={body.get('discarded_drafts')} archived_nodes={body.get('archived_nodes')}",
        )
    with connect() as conn:
        st = conn.execute(
            f"SELECT n.is_draft, n.status, count(*) c FROM kg_node n "
            f"WHERE n.type='skill_level' AND ({SKILL_KEY_SQL})=%s GROUP BY 1,2",
            (skill_key,),
        ).fetchall()
    left_draft = sum(r["c"] for r in st if r["is_draft"])
    online_arch = [r["c"] for r in st if not r["is_draft"] and r["status"] == "archived"]
    check(left_draft == 0, "技能的草稿行已清空", f"剩余草稿行={left_draft}")
    check(bool(online_arch), "线上档位已全部归档", f"archived 行数={online_arch}")

    with connect() as conn:
        conn.execute(
            f"UPDATE kg_node n SET status='published' WHERE n.type='skill_level' "
            f"AND ({SKILL_KEY_SQL})=%s AND NOT n.is_draft",
            (skill_key,),
        )
        conn.commit()
    print(f"  已把 {skill_key} 恢复成 published（隔离库，避免影响后续测试）")

print("\n" + "=" * 56)
bad = [r for r in RESULTS if not r[0]]
print(f"结果：{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
for _, n, d in bad:
    print(f"  FAIL {n} — {d}")
sys.exit(1 if bad else 0)
