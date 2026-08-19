"""§7 发布与丢弃：套用顺序、并发 409、事前门禁、端点存在性、编码唯一。

这一组的共同性质是**「拒绝的时候不能写库」**。后台发布这类动作出事故的形状很固定：
校验放在写之后，于是「拒绝」变成「写一半又报错」，运营重试时看到的库已经不是原样。
所以每条拒绝类用例都在断言 HTTP 码之外，再断言一次「线上行逐列未变」。

并发 409 单独说一句：两个运营同时改同一条、后提交的把前一个静默冲掉，是这类后台
最典型的事故 —— 出事时没有任何报错，只有「我明明改过」。§7 第 2 步要求
`base_version` ≠ 线上 `version` 时回 409，这里用手工造的落后草稿把它钉住。
"""
from __future__ import annotations

import pytest

from tests import _draft_probe as probe

SENTINEL = probe.SENTINEL

# §7 给的接口清单。实现改路径的话这里要跟着改，但**不要静默放宽**——
# 前端与运营工作台是按这份路径接的。
PUBLISH_NODE = "/v1/admin/publish/node"
PUBLISH_BATCH = "/v1/admin/publish/batch"
DRAFTS_LIST = "/v1/admin/drafts"
DISCARD_DRAFT = "/v1/admin/draft"


def _paths(app) -> dict[str, set[str]]:  # noqa: ANN001
    return {p: set(ops) for p, ops in app.openapi()["paths"].items()}


class _Publish:
    """发布动作的驱动器：路由在就走 HTTP，不在就直接调服务层。

    为什么要这个适配层：§7 的业务规则（并发 409、事前门禁、端点存在性、编码唯一）
    落在 `kg/pg_store/draft_publish.py`，HTTP 路由只是把异常翻成状态码。路由还没接上
    的阶段，仍然应该能验业务规则对不对 —— 否则整组断言要等到最后一刻才开始跑，
    而那时候错早已叠了好几层。

    代价要说清楚：`mode == "service"` 时**异常→状态码的映射是测试这边做的**，
    路由层把 `DraftConflict` 翻成 500 之类的错这组用例查不出来。所以
    `Test发布接口存在` 那条红必须留着，它就是「HTTP 层还没被覆盖」的指示灯。
    """

    def __init__(self, client, mode: str):
        self.client, self.mode = client, mode

    def publish(self, node_id: str) -> tuple[int, str]:
        if self.mode == "http":
            r = self.client.post(PUBLISH_NODE, params={"node_id": node_id})
            return r.status_code, r.text
        return self._service(node_id)

    def _service(self, node_id: str) -> tuple[int, str]:
        from backend.kg.pg_store import draft_publish as dp
        from backend.kg.pg_store.publish_rules import PublishGateError

        try:
            out = dp.publish_node(node_id, user_id="9201", user_name="draft-test")
            return 200, str(out)
        except dp.DraftConflict as e:
            return 409, str(e)
        except dp.DraftNotFound as e:
            return 404, str(e)
        except (dp.CodeTaken, dp.MissingEndpoints) as e:
            return 409 if isinstance(e, dp.CodeTaken) else 400, str(e)
        except PublishGateError as e:
            return 400, str(getattr(e, "violations", e))

    def batch(self, ids: list[str]) -> tuple[int, str]:
        if self.mode == "http":
            r = self.client.post(PUBLISH_BATCH, json={"node_ids": ids})
            return r.status_code, r.text
        from backend.kg.pg_store import draft_publish as dp

        return 200, str(dp.publish_batch(ids, user_id="9201", user_name="draft-test"))

    def drafts(self) -> tuple[int, str]:
        if self.mode == "http":
            r = self.client.get(DRAFTS_LIST)
            return r.status_code, r.text
        from backend.kg.pg_store import draft_publish as dp

        return 200, str(dp.list_drafts())

    def discard(self, node_id: str) -> tuple[int, str]:
        if self.mode == "http":
            r = self.client.delete(DISCARD_DRAFT, params={"node_id": node_id})
            return r.status_code, r.text
        from backend.kg.pg_store import draft_publish as dp

        try:
            return 200, str(dp.discard_draft(node_id, user_id="9201", user_name="t"))
        except dp.DraftNotFound as e:
            return 404, str(e)


@pytest.fixture(scope="module")
def publish_api(request):
    """路由齐了走 HTTP；只有服务层就走服务层；两个都没有才 SKIP。"""
    from tests._draft_probe import get_app, make_client

    have = _paths(get_app())
    want = [
        (PUBLISH_NODE, "post"), (PUBLISH_BATCH, "post"),
        (DRAFTS_LIST, "get"), (DISCARD_DRAFT, "delete"),
    ]
    missing = [f"{m.upper()} {p}" for p, m in want if m not in have.get(p, set())]
    if not missing:
        return _Publish(make_client(), "http")
    try:
        import backend.kg.pg_store.draft_publish as dp  # noqa: F401
    except ImportError:
        pytest.skip(f"§7 尚未实现：路由缺 {missing}，也没有 draft_publish 服务层")
    for fn in ("publish_node", "publish_batch", "list_drafts", "discard_draft"):
        if not hasattr(dp, fn):
            pytest.skip(f"draft_publish 缺 {fn}()；路由也缺 {missing}")
    print(f"\n[publish] 路由缺 {missing} → 本组走服务层直调")
    return _Publish(make_client(), "service")


class Test发布接口存在:
    def test_四个接口都在路由表里(self, app):
        """路由缺失和路由改名是两件事，先把它区分开：失败信息里列出库里现有的
        publish/draft 相关路径，实现方一眼看出是没做还是改了名。"""
        have = _paths(app)
        want = [
            (PUBLISH_NODE, "post"), (PUBLISH_BATCH, "post"),
            (DRAFTS_LIST, "get"), (DISCARD_DRAFT, "delete"),
        ]
        missing = [f"{m.upper()} {p}" for p, m in want if m not in have.get(p, set())]
        near = sorted(p for p in have if "publish" in p or "draft" in p)
        assert not missing, f"缺 {missing}；库里现有相关路径：{near}"


class Test发布套用:
    def test_发布后线上行变_版本加一_草稿行清空(
        self, publish_api, db_ready, real_ids, client
    ):
        """§7 第 5–7 步：线上行用草稿内容更新、`version+1`、草稿行删掉。"""
        occ = real_ids["occupation_id"]
        with probe.LiveRowGuard([occ], [occ]) as g:
            before = dict(g.live_node(occ) or {})
            probe.make_draft_of(occ, name=f"{SENTINEL}待发布")
            st, body = publish_api.publish(occ)
            assert st == 200, f"HTTP {st}: {body[:300]}"
            after = probe.live_row(occ) or {}
            assert after.get("name") == f"{SENTINEL}待发布", "草稿内容没套用到线上行"
            assert int(after.get("version") or 0) == int(before.get("version") or 0) + 1, (
                f"version 没 +1：{before.get('version')} → {after.get('version')}"
            )
            assert not probe.draft_rows_of(occ), "发布后草稿行没清掉"

    def test_新建记录发布时原地转正(self, publish_api, db_ready, client):
        """§5：新建只有草稿行，发布 = 草稿行原地 `is_draft=false`，不需要复制。

        「原地」这件事可观测：转正后 `created_at` 应当还是建草稿那一刻的值。
        复制新行的实现会把它刷新掉，也会让 id 之外的列有机会丢失。

        受试类型故意用 `industry`：BR 门禁只管 major / occupation / skill
        （`validate_publish` 的分支），用岗位会先被 BR-03「没有 requires 权重」拦下，
        测不到「原地转正」这一层。整单元发布另有用例。
        """
        from backend.kg.pg_store.client import connect

        nid = "ZZ:draftprobe:publishnew:1"
        try:
            with connect() as c:
                probe._insert_node(
                    c, nid, "industry", f"{SENTINEL}新建待发布",
                    {"code": "ZZDRAFTPROBENEW"}, is_draft=True,
                )
                c.commit()
                born = c.execute(
                    "SELECT created_at FROM kg_node WHERE id=%s AND is_draft", (nid,)
                ).fetchone()["created_at"]
            st, body = publish_api.publish(nid)
            assert st == 200, f"HTTP {st}: {body[:300]}"
            live = probe.live_row(nid)
            assert live, "发布后没有线上行 —— 新建没转正"
            assert not probe.draft_rows_of(nid), "转正后还留着草稿行（应当是原地翻转）"
            assert live["created_at"] == born, (
                "created_at 被刷新了：说明是新插了一行而不是原地转正，"
                "顺带会丢掉草稿行上未被复制的列"
            )
        finally:
            with connect() as c:
                c.execute("DELETE FROM kg_edge WHERE src_id=%s OR dst_id=%s", (nid, nid))
                c.execute("DELETE FROM kg_node WHERE id=%s", (nid,))
                c.commit()

    def test_新建岗位连同草稿边整单元发布(self, publish_api, db_ready, real_ids, client):
        """§3：一个发布单元 = 一个节点草稿行 + 所有 `unit_id` 指向它的草稿边。

        这条同时验 §7 第 1 步的门禁到底看的是**谁**：新建岗位没有任何线上边，
        权重全在草稿边上。门禁只查线上边的话 Σweight=0、BR-03 必败，
        于是「新建岗位」这条路永远发不出去 —— 而这正是运营最常做的动作之一。
        """
        from backend.kg.pg_store.client import connect

        nid = "ZZ:draftprobe:publishunit:1"
        eid = "ZZ:draftprobe:publishunit:edge:1"
        try:
            with connect() as c:
                probe._insert_node(
                    c, nid, "occupation", f"{SENTINEL}整单元待发布",
                    {"code": "ZZDRAFTPROBEUNIT"}, is_draft=True,
                )
                c.commit()
            probe.make_draft_edge(
                eid, nid, real_ids["skill_id"], "requires", weight=1.0, unit_id=nid
            )
            st, body = publish_api.publish(nid)
            assert st == 200, f"HTTP {st}: {body[:400]}"
            assert probe.live_row(nid), "节点没转正"
            with connect() as c:
                live_edge = c.execute(
                    "SELECT status, is_draft FROM kg_edge WHERE id=%s", (eid,)
                ).fetchall()
            assert [dict(r) for r in live_edge] == [{"status": "published", "is_draft": False}], (
                f"草稿边没跟着单元一起转正：{[dict(r) for r in live_edge]}"
            )
        finally:
            with connect() as c:
                c.execute("DELETE FROM kg_edge WHERE src_id=%s OR dst_id=%s", (nid, nid))
                c.execute("DELETE FROM kg_node WHERE id=%s", (nid,))
                c.commit()

    def test_丢弃草稿只删草稿行(self, publish_api, db_ready, real_ids, client):
        occ = real_ids["occupation_id"]
        with probe.LiveRowGuard([occ], [occ]) as g:
            before = dict(g.live_node(occ) or {})
            probe.make_draft_of(occ, name=f"{SENTINEL}待丢弃")
            st, body = publish_api.discard(occ)
            assert st in (200, 204), f"HTTP {st}: {body[:200]}"
            assert not probe.draft_rows_of(occ), "草稿行没删掉"
            after = probe.live_row(occ) or {}
            diff = {k: (before.get(k), after.get(k)) for k in before
                    if before.get(k) != after.get(k)}
            assert not diff, f"丢弃草稿动到了线上行：{diff}"

    def test_待发布清单列出有草稿的记录(self, publish_api, db_ready, real_ids, client):
        occ = real_ids["occupation_id"]
        with probe.LiveRowGuard([occ], [occ]):
            probe.make_draft_of(occ, name=f"{SENTINEL}待发布清单")
            st, body = publish_api.drafts()
            assert st == 200, f"HTTP {st}: {body[:200]}"
            assert occ in body, "有草稿的记录没出现在待发布清单里"


class Test并发检测:
    def test_base_version落后时发布回409(self, publish_api, db_ready, real_ids, client):
        """草稿基于 V3 改的，线上已经被别人发到 V4 → 必须 409，不能静默覆盖。"""
        occ = real_ids["occupation_id"]
        with probe.LiveRowGuard([occ], [occ]) as g:
            before = dict(g.live_node(occ) or {})
            cur = int(before.get("version") or 1)
            probe.make_draft_of(occ, name=f"{SENTINEL}落后草稿", base_version=cur - 1)
            st, body = publish_api.publish(occ)
            assert st == 409, (
                f"base_version={cur - 1} vs 线上 version={cur} 却回了 "
                f"HTTP {st}：{body[:250]}"
            )
            after = probe.live_row(occ) or {}
            diff = {k: (before.get(k), after.get(k)) for k in before
                    if before.get(k) != after.get(k)}
            assert not diff, f"409 了但线上行被改了：{diff}"
            assert probe.draft_rows_of(occ), "409 了却把草稿删了 —— 运营的改动丢了"

    def test_base_version与线上一致时可以发布(self, publish_api, db_ready, real_ids, client):
        """反面：并发检测不能把正常发布也拦掉。"""
        occ = real_ids["occupation_id"]
        with probe.LiveRowGuard([occ], [occ]) as g:
            cur = int((g.live_node(occ) or {}).get("version") or 1)
            probe.make_draft_of(occ, name=f"{SENTINEL}同版草稿", base_version=cur)
            st, body = publish_api.publish(occ)
            assert st == 200, f"HTTP {st}: {body[:250]}"


class Test事前门禁:
    def test_权重和远离1的草稿发布被拒且不写库(
        self, publish_api, db_ready, real_ids, client
    ):
        """BR-03 是 Σweight≈1（±0.01）。§7 第 1 步要求**事前**拦。

        现在 `demote_noncompliant` 是事后扫描已经发布出去的不合规数据 ——
        也就是说不合规内容已经在前台露过脸了。事前拦本来就该如此。
        """
        occ = real_ids["occupation_id"]
        with probe.LiveRowGuard([occ], [occ]) as g:
            before = dict(g.live_node(occ) or {})
            edges_before = _edge_weights(occ)
            probe.make_draft_of(occ, name=f"{SENTINEL}门禁不过")
            # 草稿边把权重堆到远离 1（每条 0.9，至少两条 → Σ≥1.8）
            for i, eid in enumerate(edges_before):
                probe.make_draft_edge(
                    f"ZZ:draftprobe:badweight:{i}",
                    occ, _edge_dst(eid), "requires", weight=0.9, unit_id=occ,
                )
            st, body = publish_api.publish(occ)
            assert st in (400, 409, 422), (
                f"权重和远离 1 的草稿被放行了：HTTP {st} {body[:250]}"
            )
            after = probe.live_row(occ) or {}
            diff = {k: (before.get(k), after.get(k)) for k in before
                    if before.get(k) != after.get(k)}
            assert not diff, f"门禁拒绝了但线上行被改了：{diff}"
            assert _edge_weights(occ) == edges_before, "门禁拒绝了但线上边被改了"

    def test_门禁拒绝时要说清是哪条规则(self, publish_api, db_ready, real_ids, client):
        """运营看不懂「发布失败」，只看得懂「权重和 1.8，要求 1.00±0.01」。"""
        occ = real_ids["occupation_id"]
        with probe.LiveRowGuard([occ], [occ]):
            probe.make_draft_of(occ, name=f"{SENTINEL}门禁提示")
            for i, eid in enumerate(_edge_weights(occ)):
                probe.make_draft_edge(
                    f"ZZ:draftprobe:badweight:{i}",
                    occ, _edge_dst(eid), "requires", weight=0.9, unit_id=occ,
                )
            st, body = publish_api.publish(occ)
            if st < 400:
                pytest.skip("门禁没拦住，另有用例管这件事")
            assert "BR-03" in body or "权重" in body, f"拒绝原因不可读：{body[:250]}"


class Test端点存在性:
    def test_草稿边指向未发布的草稿节点时发布被拒并指出缺哪个(
        self, publish_api, db_ready, real_ids, client
    ):
        """§7 第 4 步 —— 外键被删掉之后，这一步就是它的替代品（§1.2 / §10.1）。

        场景是 §10.6 的跨单元依赖：新建专业 + 同时挂新建岗位，两个单元要按序发布。
        先发的那个引用了对方尚未发布的草稿节点，必须拒绝**并说明缺哪个**，
        否则运营只能靠猜顺序。
        """
        from backend.kg.pg_store.client import connect

        occ = real_ids["occupation_id"]
        dangling = "ZZ:draftprobe:dangling:1"
        try:
            with connect() as c:
                probe._insert_node(
                    c, dangling, "skill_level", f"{SENTINEL}未发布技能",
                    {"skill_key": f"{SENTINEL}未发布技能", "level": 3}, is_draft=True,
                )
                c.commit()
            with probe.LiveRowGuard([occ], [occ]) as g:
                before = dict(g.live_node(occ) or {})
                probe.make_draft_of(occ, name=f"{SENTINEL}端点缺失")
                probe.make_draft_edge(
                    "ZZ:draftprobe:edge:dangling", occ, dangling, "requires",
                    weight=0.2, unit_id=occ,
                )
                st, body = publish_api.publish(occ)
                assert st in (400, 409, 422), (
                    f"引用了未发布草稿节点的边被放行：HTTP {st} {body[:250]}"
                )
                assert dangling in body, f"没指出缺哪个端点：{body[:300]}"
                after = probe.live_row(occ) or {}
                diff = {k: (before.get(k), after.get(k)) for k in before
                        if before.get(k) != after.get(k)}
                assert not diff, f"拒绝了但线上行被改了：{diff}"
        finally:
            with connect() as c:
                c.execute("DELETE FROM kg_edge WHERE src_id=%s OR dst_id=%s",
                          (dangling, dangling))
                c.execute("DELETE FROM kg_node WHERE id=%s", (dangling,))
                c.commit()

    def test_发布后不留孤儿边(self, publish_api, db_ready, real_ids, client):
        """§10.1：外键删了之后「边的两端一定存在」只剩应用层保证，发布后要复查。"""
        occ = real_ids["occupation_id"]
        with probe.LiveRowGuard([occ], [occ]):
            probe.make_draft_of(occ, name=f"{SENTINEL}孤儿边检查")
            publish_api.publish(occ)
        assert _orphan_edges() == [], f"发布路径跑过之后出现孤儿边：{_orphan_edges()[:10]}"


class Test业务编码唯一:
    def test_草稿改code撞另一条线上行时发布被拒(
        self, publish_api, db_ready, real_ids, client
    ):
        """§7 第 3 步：§2 ④ 的唯一索引排除了草稿，所以草稿之间不互斥，
        「撞不撞」得在发布时对**线上行**再查一次。

        漏了这一步的表现不是报错而是 `IntegrityError` 500（唯一索引兜底），
        运营看到的是「服务器错误」而不是「编码已被占用」。
        """
        from backend.kg.pg_store.client import connect

        occ = real_ids["occupation_id"]
        other = real_ids.get("spare_occupation_id")
        if not other:
            pytest.skip("挑不到第二个受试岗位")
        with connect() as c:
            row = c.execute(
                "SELECT attrs FROM kg_node WHERE id=%s AND NOT is_draft", (other,)
            ).fetchone()
        import json as _json

        attrs = _json.loads(row["attrs"] or "{}") if row else {}
        taken = str(attrs.get("code") or "").strip()
        if not taken:
            pytest.skip("受试对照岗位没有 attrs.code，撞不出冲突")
        with probe.LiveRowGuard([occ, other], [occ]) as g:
            before = dict(g.live_node(occ) or {})
            probe.make_draft_of(
                occ, name=f"{SENTINEL}撞编码",
                attrs=_json.dumps({"code": taken}, ensure_ascii=False),
            )
            st, body = publish_api.publish(occ)
            assert st in (400, 409, 422), (
                f"编码 {taken} 已被 {other} 占用，却回了 HTTP {st}：{body[:250]}"
            )
            assert st != 500
            after = probe.live_row(occ) or {}
            diff = {k: (before.get(k), after.get(k)) for k in before
                    if before.get(k) != after.get(k)}
            assert not diff, f"拒绝了但线上行被改了：{diff}"


class Test批量发布:
    def test_一个不合规不拖垮其余(self, publish_api, db_ready, real_ids, client):
        """§7 末：批量逐个走各自事务，返回逐项结果。"""
        occ = real_ids["occupation_id"]
        other = real_ids.get("spare_occupation_id")
        if not other:
            pytest.skip("挑不到第二个受试岗位")
        with probe.LiveRowGuard([occ, other], [occ, other]):
            probe.make_draft_of(occ, name=f"{SENTINEL}批量正常")
            probe.make_draft_of(other, name=f"{SENTINEL}批量落后",
                               base_version=-1)   # 必然 409
            st, body = publish_api.batch([occ, other])
            assert st == 200, f"HTTP {st}: {body[:250]}"
            assert occ in body and other in body, "批量结果没有逐项返回"
            assert probe.live_row(occ)["name"] == f"{SENTINEL}批量正常", (
                "同批里有一个 409，把正常那个也拖垮了"
            )


# ── 小工具 ───────────────────────────────────────────────────


def _edge_weights(nid: str) -> dict[str, float | None]:
    from backend.kg.pg_store.client import connect

    with connect() as c:
        return {
            r["id"]: (None if r["weight"] is None else float(r["weight"]))
            for r in c.execute(
                "SELECT id, weight FROM kg_edge WHERE src_id=%s AND rel_type='requires' "
                "AND NOT is_draft", (nid,)
            ).fetchall()
        }


def _edge_dst(edge_id: str) -> str:
    from backend.kg.pg_store.client import connect

    with connect() as c:
        r = c.execute("SELECT dst_id FROM kg_edge WHERE id=%s LIMIT 1", (edge_id,)).fetchone()
    return r["dst_id"] if r else ""


def _orphan_edges() -> list[str]:
    """src/dst 在 kg_node 里找不到线上行的边。外键删了之后只能这样查。"""
    from backend.kg.pg_store.client import connect

    with connect() as c:
        return [
            r["id"]
            for r in c.execute(
                """
                SELECT e.id FROM kg_edge e
                WHERE NOT e.is_draft AND (
                  NOT EXISTS (SELECT 1 FROM kg_node n WHERE n.id=e.src_id AND NOT n.is_draft)
                  OR NOT EXISTS (SELECT 1 FROM kg_node n WHERE n.id=e.dst_id AND NOT n.is_draft)
                ) LIMIT 20
                """
            ).fetchall()
        ]
