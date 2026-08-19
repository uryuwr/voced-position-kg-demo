"""§2 的 DDL 迁移：幂等性、主键/外键的**实际**状态、`ON CONFLICT` 还接得上。

这三条都只能对着真库断言。理由分别是：

- **幂等**：本项目没有 migration 框架，`SCHEMA_SQL` 每次进程启动都整段执行
  （`client.ensure_schema`）。`ADD PRIMARY KEY` / `ADD CONSTRAINT` 天生不幂等，
  漏了守卫的表现是**服务第二次启动就起不来**——而这在开发机上很容易漏掉，
  因为第一次跑总是成功的。所以要连着跑三次。
- **主键/外键**：读 SQL 文本只能证明「写了」，证明不了「库里真变成了这样」。
  老库上换主键要先 DROP 再 ADD，中间任何一步失败都会留下半成品状态。
- **`ON CONFLICT`**：主键从 `id` 变成 `(id, is_draft)` 后，冲突目标写 `(id)` 的
  upsert 会当场抛 `there is no unique or exclusion constraint matching`
  （§2.1 列了 4 处）。这属于「好查」的错，但要有人真的跑一次 upsert 才会发现。
"""
from __future__ import annotations

import uuid

import pytest


class TestDDL幂等:
    def test_连跑三次ensure_schema不报错(self, db_ready):
        """第二、三次跑的是「已经迁移过的库」，走的正是 §2 ③ 的 IF NOT EXISTS 分支。"""
        from backend.kg.pg_store.client import ensure_schema

        for i in range(3):
            try:
                ensure_schema(force=True)
            except Exception as e:  # noqa: BLE001
                pytest.fail(f"第 {i + 1} 次 ensure_schema 失败：{type(e).__name__}: {e}")


class TestKgNode主键与外键:
    def test_主键含is_draft(self, caps):
        """`(id, is_draft)` 是「同表两行」的地基：主键不换，草稿行插不进去。"""
        assert caps["pk"]["kg_node"] == ["id", "is_draft"], (
            f"kg_node 主键是 {caps['pk']['kg_node']}"
        )

    def test_边主键也含is_draft(self, caps):
        assert caps["pk"]["kg_edge"] == ["id", "is_draft"], (
            f"kg_edge 主键是 {caps['pk']['kg_edge']}"
        )

    def test_两个外键已删(self, caps):
        """§1.2：外键只能引用完整主键或唯一约束，引用不了部分唯一索引，所以只能删。

        删掉之后「边的两端一定存在」变成应用层责任（§7 第 4 步的端点存在性校验 +
        §10.1 的孤儿边闸门）。这里断言的是「代价确实付了」，
        免得出现「主键换了但外键还在」——那种状态下草稿边根本插不进去。
        """
        assert caps["foreign_keys"] == [], f"kg_edge 上还留着外键：{caps['foreign_keys']}"

    def test_业务编码唯一索引排除草稿(self, caps):
        """§2 ④：不排除的话，改编码时草稿行会和**自己的线上行**撞唯一索引，
        运营看到的是「编码已被占用」而占用者就是这条记录自己。"""
        idx = caps["code_unique_indexdef"]
        assert idx, "uq_kg_node_region_type_code 不见了"
        assert "is_draft" in idx, f"唯一索引没排除草稿行：{idx}"

    def test_草稿查询有索引(self, db_ready):
        """35087 行的表上扫全表找草稿，管理台列表每次翻页都要付一次。"""
        from backend.kg.pg_store.client import connect

        with connect() as c:
            names = {
                r["indexname"]
                for r in c.execute(
                    "SELECT indexname FROM pg_indexes WHERE tablename IN ('kg_node','kg_edge')"
                ).fetchall()
            }
        assert "idx_kg_node_draft" in names
        assert "idx_kg_edge_draft_unit" in names

    def test_控制列都在(self, caps):
        assert {"is_draft", "target_status", "base_version"} <= caps["node_columns"]
        assert {"is_draft", "target_status", "unit_id"} <= caps["edge_columns"]

    def test_is_draft非空且默认false(self, db_ready):
        """默认值必须是 false：31 处写入点里有一批（离线灌库、派生元数据）不会显式带
        `is_draft`，默认成 true 的话它们写出来的全是草稿，前台会整片消失。"""
        from backend.kg.pg_store.client import connect

        with connect() as c:
            rows = c.execute(
                "SELECT table_name, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE column_name='is_draft' AND table_name IN ('kg_node','kg_edge')"
            ).fetchall()
        assert len(rows) == 2
        for r in rows:
            assert r["is_nullable"] == "NO", f"{r['table_name']}.is_draft 可为 NULL"
            assert "false" in (r["column_default"] or ""), r["column_default"]


class TestOnConflict跟着主键改:
    """§2.1：4 处 upsert 的冲突目标要跟着主键改，否则当场抛异常。"""

    def test_灌库路径的upsert不报错(self, db_ready):
        """`migrate.insert_nodes` / `insert_edges` —— 重灌库必经之路。

        在事务里跑完就回滚，不留数据；报错才是我们要抓的东西。
        """
        from backend.kg.pg_store import migrate
        from backend.kg.pg_store.client import connect

        tag = f"ZZ:onconflict:{uuid.uuid4().hex[:8]}"
        node = {
            "id": f"{tag}:n1", "region": "CN", "type": "occupation", "name": "ZZ冲突目标",
            "name_en": None, "name_zh": None, "aliases": None, "description": None,
            "attrs": {}, "source_system": "MANUAL", "source_id": tag,
            "source_url": "manual://t", "license": "internal",
            "fetched_at": "2026-08-18T00:00:00Z", "confidence": "manual_seed",
        }
        node2 = {**node, "id": f"{tag}:n2"}
        edge = {
            "id": f"{tag}:e1", "src_id": node["id"], "dst_id": node2["id"],
            "rel_type": "related_to", "region": "CN", "weight": 0.5, "evidence": None,
            "attrs": {}, "source_system": "MANUAL", "source_id": tag,
            "source_url": "manual://t", "license": "internal",
            "fetched_at": "2026-08-18T00:00:00Z", "confidence": "manual_seed",
        }
        conn = connect()
        try:
            migrate.insert_nodes(conn, [node, node2])
            migrate.insert_nodes(conn, [node, node2])   # 第二遍走 DO UPDATE
            migrate.insert_edges(conn, [edge])
            migrate.insert_edges(conn, [edge])
        finally:
            conn.rollback()
            conn.close()

    def test_写路径的节点upsert不报错(self, db_ready):
        """`write.create_node` —— 管理台新建/保存走它。"""
        from backend.kg.pg_store.client import connect
        from backend.kg.pg_store.write import create_node

        nid = f"ZZ:onconflict:{uuid.uuid4().hex[:8]}"
        try:
            create_node(
                {"id": nid, "type": "occupation", "name": "ZZ冲突目标 · 岗位",
                 "region": "CN", "source_system": "MANUAL", "source_id": nid,
                 "source_url": "manual://t", "license": "internal"},
                user_id="9201", user_name="draft-test",
            )
            create_node(
                {"id": nid, "type": "occupation", "name": "ZZ冲突目标 · 岗位改名",
                 "region": "CN", "source_system": "MANUAL", "source_id": nid,
                 "source_url": "manual://t", "license": "internal"},
                user_id="9201", user_name="draft-test",
            )
        finally:
            with connect() as c:
                c.execute("DELETE FROM kg_edge WHERE src_id=%s OR dst_id=%s", (nid, nid))
                c.execute("DELETE FROM kg_node WHERE id=%s", (nid,))
                c.commit()

    def test_写路径的边upsert不报错(self, db_ready, real_ids):
        """`write.create_edge` —— 管理台建边、以及 `apply_node_links` 自动挂边走它。"""
        from backend.kg.pg_store.client import connect
        from backend.kg.pg_store.write import create_edge

        src, dst = real_ids["occupation_id"], real_ids["skill_id"]
        eid = f"ZZ:onconflict:{uuid.uuid4().hex[:8]}"
        try:
            for w in (0.11, 0.22):
                create_edge(
                    {"id": eid, "src_id": src, "dst_id": dst, "rel_type": "requires",
                     "region": "CN", "weight": w, "source_system": "MANUAL",
                     "source_url": "manual://t", "license": "internal"},
                    user_id="9201", user_name="draft-test",
                )
        finally:
            with connect() as c:
                c.execute("DELETE FROM kg_edge WHERE id=%s", (eid,))
                c.commit()

    def test_同一id的线上行与草稿行能共存(self, db_ready, caps, real_ids):
        """这是主键换成 `(id, is_draft)` 的**唯一目的**，直接对着库验一次。

        换主键失败时，症状不是报错而是「编辑保存后什么都没发生」——
        草稿行插不进去、异常被上层 try/except 吃掉。
        """
        if not caps["has_is_draft"]:
            pytest.skip("is_draft 列不存在")
        from backend.kg.pg_store.client import connect

        nid = real_ids["occupation_id"]
        with connect() as c:
            row = c.execute(
                "SELECT * FROM kg_node WHERE id=%s AND NOT is_draft", (nid,)
            ).fetchone()
            assert row, "受试线上行不见了"
            d = dict(row)
            d["is_draft"] = True
            d["status"] = "draft"
            d["name"] = "ZZ双行共存探针"
            keys = [k for k in d if k in caps["node_columns"]]
            try:
                c.execute(
                    f"INSERT INTO kg_node ({', '.join(keys)}) "
                    f"VALUES ({', '.join('%(' + k + ')s' for k in keys)})",
                    {k: d[k] for k in keys},
                )
                n = c.execute(
                    "SELECT COUNT(*) AS c FROM kg_node WHERE id=%s", (nid,)
                ).fetchone()["c"]
                assert n == 2, f"同一 id 只存下 {n} 行"
            finally:
                c.execute("DELETE FROM kg_node WHERE id=%s AND is_draft", (nid,))
                c.commit()
