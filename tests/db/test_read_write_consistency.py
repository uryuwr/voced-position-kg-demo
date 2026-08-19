"""同一份数据，多个读路径必须给同一个答案；写语句必须只动它该动的那一行。

两组断言各锁一类**架构性**缺陷，都是实测撞出来的：

一、读侧「同业务场景多数据源」
    「取一个岗位的技能构成」这件事，草稿态之前在全仓有 29 处独立 SQL、12 个模块，
    每处各自拼可见性口径。以前一律 published，抄多份看不出来；草稿态加了
    「草稿优先」这一维之后立刻分家：改完技能构成，`/v1/admin/composition` 变了，
    `/v1/kg/node-detail?scope=manage` 没变 —— 同一个管理台 scope 两个答案，
    运营看着像「改了但没进草稿态」。
    本组断言的是**一致性本身**，不是某个具体数值：谁再新拼一份 SQL，这里就红。

二、写侧漏 `is_draft` 谓词
    主键改成 `(id, is_draft)` 之后，`WHERE id = %s` 会同时命中线上行与草稿行。
    实测：`patch_node(..., {"status":"published"}, to_draft=False)` 给草稿行也写了
    `status='published'`，撞 `ck_kg_node_draft_status` → 发布 500。
    没有那道 CHECK 的话，它就是一次静默的草稿泄漏。
    本组对每个写操作断言「另一行逐列不变」，将来任何人新增写语句漏了谓词都会红。

    python -X utf8 -m pytest tests/db/test_read_write_consistency.py -q
"""
from __future__ import annotations

from typing import Any

import pytest

from backend.kg.pg_store import biz_store as biz
from backend.kg.pg_store import skill_composition as sc
from backend.kg.pg_store.client import connect
from backend.kg.pg_store.counts import counts_for_occupations
from backend.kg.pg_store.node_detail import node_detail
from backend.kg.pg_store.skill_aggregate import (
    entity_skill_composition,
    occupation_skill_bundles,
)
from backend.kg.pg_store.write import (
    archive_node,
    create_edge,
    ensure_node_draft,
    patch_node,
)

USER = {"user_id": "zz-consistency", "user_name": "一致性测试"}


def _rows(node_id: str) -> dict[bool, dict[str, Any]]:
    """该 id 的两行，按 is_draft 索引，**整行所有列**都带出来。"""
    with connect() as c:
        return {
            bool(r["is_draft"]): dict(r)
            for r in c.execute(
                "SELECT * FROM kg_node WHERE id = %s", (node_id,)
            ).fetchall()
        }


def _diff(a: dict[str, Any], b: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    keys = set(a) | set(b)
    return {k: (a.get(k), b.get(k)) for k in keys if a.get(k) != b.get(k)}


@pytest.fixture()
def occ() -> str:
    """挑一个「每个技能只有一条边」的已发布岗位，跑完把它恢复原样。

    必须挑单边岗位：库里大量国标岗位对同一技能挂了 L1/L2/L3 三条边，
    而管理台按边求和、BR-03 按 skill_key 取 max —— 那是另一个既有口径问题，
    混进来会让这里的断言测不到该测的东西。
    """
    from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL

    with connect() as c:
        row = c.execute(
            f"""
            SELECT o.id FROM kg_node o
            JOIN kg_edge e ON e.src_id = o.id AND e.rel_type='requires'
                 AND NOT e.is_draft AND COALESCE(e.status,'published')='published'
            JOIN kg_node n ON n.id = e.dst_id AND n.type='skill_level' AND NOT n.is_draft
            WHERE o.type='occupation' AND NOT o.is_draft
              AND COALESCE(o.status,'published')='published'
            GROUP BY 1
            HAVING count(*) >= 3 AND count(*) = count(DISTINCT ({SKILL_KEY_SQL}))
            ORDER BY o.id LIMIT 1
            """
        ).fetchone()
        assert row, "库里没有可用的靶子岗位（每技能一条边、Σ 正常）"
        oid = row["id"]
        snapshot = [
            dict(r)
            for r in c.execute(
                "SELECT id, weight, status FROM kg_edge WHERE src_id=%s "
                "AND rel_type='requires' AND NOT is_draft",
                (oid,),
            ).fetchall()
        ]
        node = dict(
            c.execute(
                "SELECT name, description, attrs, version, status FROM kg_node "
                "WHERE id=%s AND NOT is_draft",
                (oid,),
            ).fetchone()
        )
    yield oid
    with connect() as c:
        c.execute(
            "DELETE FROM kg_edge WHERE is_draft AND (unit_id=%s OR src_id=%s)", (oid, oid)
        )
        c.execute("DELETE FROM kg_node WHERE id=%s AND is_draft", (oid,))
        keep = {e["id"] for e in snapshot}
        extra = [
            r["id"]
            for r in c.execute(
                "SELECT id FROM kg_edge WHERE src_id=%s AND rel_type='requires' "
                "AND NOT is_draft",
                (oid,),
            ).fetchall()
            if r["id"] not in keep
        ]
        if extra:
            c.execute("DELETE FROM kg_edge WHERE id = ANY(%s)", (extra,))
        for e in snapshot:
            c.execute(
                "UPDATE kg_edge SET weight=%s, status=%s WHERE id=%s AND NOT is_draft",
                (e["weight"], e["status"], e["id"]),
            )
        c.execute(
            "UPDATE kg_node SET name=%s, description=%s, attrs=%s, version=%s, "
            "status=%s WHERE id=%s AND NOT is_draft",
            (node["name"], node["description"], node["attrs"], node["version"],
             node["status"], oid),
        )
        c.commit()


# ── 一、读侧一致性 ───────────────────────────────────────────
class Test同一实体多读路径必须一致:
    def _manage_views(self, oid: str) -> dict[str, Any]:
        """所有**管理台**口径的技能构成视图，归一成 {skill_key: 权重} 便于比对。"""
        det = node_detail(oid)
        return {
            "node_detail": {
                s["skill_key"]: s.get("weight") for s in (det.get("skills") or [])
            },
            "admin_composition": {
                i["skill_key"]: i.get("weight")
                for i in (sc.get_composition(oid).get("items") or [])
            },
            "entity_skill_composition": {
                b["skill_key"]: b.get("weight")
                for b in entity_skill_composition(oid, scope="manage", limit=500)
            },
        }

    def _public_views(self, oid: str) -> dict[str, Any]:
        return {
            "entity_skill_composition": {
                b["skill_key"]: b.get("weight")
                for b in entity_skill_composition(oid, scope="public", limit=500)
            },
            "occupation_skill_bundles": {
                b["skill_key"]: b.get("weight")
                for b in occupation_skill_bundles(oid, limit=500)
            },
            "position_skills": {
                s["skill_key"]: s.get("weight")
                for s in biz.position_skills(oid, limit=500)
            },
        }

    def test_编辑前所有读路径一致(self, occ):
        mv, pv = self._manage_views(occ), self._public_views(occ)
        assert len(set(map(str, mv.values()))) == 1, f"管理台各视图不一致：{mv}"
        assert len(set(map(str, pv.values()))) == 1, f"前台各视图不一致：{pv}"
        assert str(list(mv.values())[0]) == str(list(pv.values())[0]), "无草稿时两侧应相同"

    def test_改权重后管理台一致_前台一致地保持旧值(self, occ):
        before_public = self._public_views(occ)["entity_skill_composition"]
        key = next(iter(before_public))
        old_w = before_public[key]
        new_w = round((old_w or 0.1) + 0.05, 4)

        sc.set_skill(occ, key, level=None, weight=new_w, **USER)

        mv = self._manage_views(occ)
        # ① 管理台三条路径必须一致，且都反映新权重
        assert len({str(v) for v in mv.values()}) == 1, (
            "管理台读路径分家了（谁又自己拼了一份 SQL？）：\n"
            + "\n".join(f"  {k}: {v}" for k, v in mv.items())
        )
        assert mv["node_detail"].get(key) == new_w, (
            f"管理台详情没反映草稿权重：{mv['node_detail'].get(key)} != {new_w}"
        )
        # ② 前台三条路径必须一致，且都还是旧权重（草稿未发布）
        pv = self._public_views(occ)
        assert len({str(v) for v in pv.values()}) == 1, f"前台读路径分家了：{pv}"
        assert pv["entity_skill_composition"].get(key) == old_w, "草稿泄漏到前台了"

    def test_列表计数与构成页数量一致(self, occ):
        key = next(iter(self._public_views(occ)["entity_skill_composition"]))
        # 删掉一项 → 管理台计数应当少 1，前台计数不变
        pub_before = counts_for_occupations([occ])[occ]["skill"]
        sc.remove_skill(occ, key, **USER)
        mng = counts_for_occupations([occ], scope="manage")[occ]["skill"]
        pub = counts_for_occupations([occ])[occ]["skill"]
        items = len(sc.get_composition(occ)["items"])
        detail = len(node_detail(occ)["skills"])
        assert mng == items == detail, (
            f"管理台三处数量不一致：counts={mng} 构成页={items} 详情={detail}"
        )
        assert pub == pub_before, "前台计数在发布前就变了"


class Test只看草稿筛选按发布单元:
    """`status=draft` 必须筛得到「只改了技能构成」的记录。

    实测过的缺口：改一项技能权重（节点行一个字没动）后，
    列表不筛选时那行 `record_status='draft'` 显示正确，但按 `status=draft` 筛
    `total=0` —— 显示对了、筛选漏了，而「只改技能构成」是运营最常做的操作。
    口径与 `unit_draft_kinds` 同源（都引 `config.has_draft_edges`）。
    """

    def test_只改技能构成也能被草稿筛选捞到(self, occ):
        from backend.kg.pg_store.query import list_nodes

        def draft_hit() -> tuple[int, bool]:
            d = list_nodes(
                node_type="occupation", scope="manage", status="draft", page_size=200
            )
            return d["total"], any(i["id"] == occ for i in d["items"])

        assert draft_hit() == (0, False) or not draft_hit()[1], "开测前不该有草稿"
        key = entity_skill_composition(occ, scope="public", limit=500)[0]["skill_key"]
        sc.set_skill(occ, key, level=None, weight=0.42, **USER)
        total, hit = draft_hit()
        assert hit, "只改技能构成的记录没被 status=draft 筛到"
        # total 与实际行数必须一致（同一个 where_sql 同时给 COUNT 和 SELECT）
        d = list_nodes(
            node_type="occupation", scope="manage", status="draft", page_size=200
        )
        assert d["total"] == len(d["items"]), (
            f"total={d['total']} 但只列出 {len(d['items'])} 行"
        )

    def test_published筛选不把草稿算进来也不重复计数(self, occ):
        from backend.kg.pg_store.query import list_nodes

        before = list_nodes(
            node_type="occupation", scope="manage", status="published", page_size=1
        )["total"]
        patch_node(occ, {"name": "ZZ筛选口径"}, **USER)   # 造一个节点草稿行
        after = list_nodes(
            node_type="occupation", scope="manage", status="published", page_size=1
        )["total"]
        assert before == after, (
            f"有草稿后 published 的总数变了：{before} → {after}"
            "（草稿行被算进来、或同一记录数了两次）"
        )


# ── 二、写侧行定位 ───────────────────────────────────────────
class Test写语句只动它该动的那一行:
    """每个写操作跑完，逐列比对**另一行**：变了就是漏了 is_draft 谓词。"""

    def test_编辑写草稿行_线上行逐列不变(self, occ):
        before = _rows(occ)
        patch_node(occ, {"name": "ZZ一致性改名"}, **USER)
        after = _rows(occ)
        assert _diff(before[False], after[False]) == {}, "编辑动到了线上行"
        assert after[True]["name"] == "ZZ一致性改名"

    def test_发布写线上行_不碰草稿行的status(self, occ):
        """这是发布 500 的那个形状：UPDATE 漏了 NOT is_draft，把草稿行也写成 published。"""
        patch_node(occ, {"name": "ZZ一致性改名2"}, **USER)
        before = _rows(occ)
        # to_draft=False 是发布侧路径（review._apply / publish_rules 走它）
        patch_node(occ, {"status": "published"}, to_draft=False, **USER)
        after = _rows(occ)
        d_draft = _diff(before[True], after[True])
        assert d_draft == {}, f"发布动到了草稿行（漏 NOT is_draft）：{d_draft}"
        assert after[False]["version"] == before[False]["version"] + 1, "线上行没发版"
        assert after[True]["status"] == "draft", "草稿行的 status 不再是 draft"

    def test_归档立即改线上行_不留草稿(self, occ):
        """2026-08-19 需求收窄：归档/停用/删除**立即生效**。

        这条原来断言的是「归档只写草稿行、线上行逐列不变」。需求改了，
        断言方向跟着反过来：线上行的 status 当场变 archived，且不留草稿行。
        """
        before = _rows(occ)
        archive_node(occ, **USER)
        after = _rows(occ)
        assert after[False]["status"] == "archived", "归档没有立即改线上行"
        assert True not in after, f"归档不该留草稿行：{list(after)}"
        # 除了 status / updated_by* 之外不该动别的列
        moved = {
            k for k in before[False]
            if before[False].get(k) != after[False].get(k)
        } - {"status", "updated_by", "updated_by_name"}
        assert not moved, f"归档顺手改了别的列：{moved}"

    def test_发布草稿后只剩一行且内容来自草稿(self, occ):
        from backend.kg.pg_store.draft_publish import publish_node

        patch_node(occ, {"description": "ZZ一致性描述"}, **USER)
        publish_node(occ, **USER)
        rows = _rows(occ)
        assert set(rows) == {False}, "发布后草稿行没清掉"
        assert rows[False]["description"] == "ZZ一致性描述"

    def test_丢弃草稿后线上行逐列不变(self, occ):
        from backend.kg.pg_store.draft_publish import discard_draft

        before = _rows(occ)
        patch_node(occ, {"name": "ZZ一致性待丢弃"}, **USER)
        discard_draft(occ, **USER)
        after = _rows(occ)
        assert set(after) == {False}
        assert _diff(before[False], after[False]) == {}, "丢弃草稿动到了线上行"

    def test_技能构成编辑只动草稿边(self, occ):
        with connect() as c:
            before = {
                r["id"]: dict(r)
                for r in c.execute(
                    "SELECT * FROM kg_edge WHERE src_id=%s AND NOT is_draft", (occ,)
                ).fetchall()
            }
        key = next(iter(entity_skill_composition(occ, scope="public", limit=500)[0:1]))[
            "skill_key"
        ] if False else entity_skill_composition(occ, scope="public", limit=500)[0]["skill_key"]
        sc.set_skill(occ, key, level=None, weight=0.42, **USER)
        sc.normalize_weights(occ, **USER)
        with connect() as c:
            after = {
                r["id"]: dict(r)
                for r in c.execute(
                    "SELECT * FROM kg_edge WHERE src_id=%s AND NOT is_draft", (occ,)
                ).fetchall()
            }
        assert set(before) == set(after), "线上边被增删了"
        for eid, row in before.items():
            assert _diff(row, after[eid]) == {}, f"线上边 {eid} 被改了：{_diff(row, after[eid])}"

    def test_publish_rules置状态只动线上行(self, occ):
        from backend.kg.pg_store.publish_rules import _set_status

        ensure_node_draft(occ)
        before = _rows(occ)
        _set_status(occ, "published")
        after = _rows(occ)
        assert _diff(before[True], after[True]) == {}, "_set_status 动到了草稿行"

    def test_新建边只落草稿行(self, occ):
        with connect() as c:
            skill = c.execute(
                "SELECT id FROM kg_node WHERE type='skill_level' AND NOT is_draft "
                "AND id <> ALL(%s) LIMIT 1",
                ([r["dst_id"] for r in c.execute(
                    "SELECT dst_id FROM kg_edge WHERE src_id=%s AND NOT is_draft", (occ,)
                ).fetchall()],),
            ).fetchone()["id"]
        e = create_edge(
            {"src_id": occ, "dst_id": skill, "rel_type": "requires", "weight": 0.1,
             "region": "CN", "evidence": "ZZ一致性新边"},
            **USER,
        )
        with connect() as c:
            rows = [
                dict(r)
                for r in c.execute(
                    "SELECT is_draft, status, unit_id FROM kg_edge WHERE id=%s", (e["id"],)
                ).fetchall()
            ]
        assert len(rows) == 1 and rows[0]["is_draft"] is True, f"新建边不该落线上行：{rows}"
        assert rows[0]["status"] == "draft" and rows[0]["unit_id"] == occ
