"""安全类断言：草稿一寸都不能进前台。这几条红就是产品事故。

方案 §10.2 把它定成「本方案唯一的静默失效点」：草稿行的 `status` 一旦不是
`'draft'`，前台那 ~120 处 `status = 'published'` 的查询立刻命中它 ——
不报错、不崩、没有任何日志，只是学员端出现了运营还没发布的内容。

所以这里的三条断言互为兜底，缺一条就有洞：

1. **遍历真实路由表**扫前台响应（不是手写接口清单，手写必漏，漏的那个就是泄漏点）
2. 每个写路径跑完都验一次「草稿行 status 恒为 draft」这个不变量
3. 每种编辑跑完都验一次「线上行逐列未变」

第 1 条还带一组**正对照**（管理台必须看得见）：只断言「看不见」的测试有一种假绿——
受试对象根本没造出来，于是哪儿都没有它，全绿而一寸没测。
"""
from __future__ import annotations

import pytest

from tests import _draft_probe as probe

SENTINEL = probe.SENTINEL


@pytest.fixture(scope="module")
def sweep_ready():
    """整模块共享一份「已装好草稿受试对象」的库状态。

    装/拆各要动十几行数据，每个用例装一次太慢；而这些用例全是只读扫描，
    共享同一份受试对象不会互相干扰。
    """
    caps = probe.db_capabilities()
    if not caps["has_is_draft"]:
        pytest.skip("kg_node/kg_edge 没有 is_draft 列：方案 §2 的 DDL 尚未落地")
    real = probe.pick_real_ids()
    if not real.get("occupation_id"):
        pytest.skip("挑不到受试岗位")
    fx = probe.install_draft_fixture("row", shadow_of=real["occupation_id"])
    try:
        yield real, fx
    finally:
        probe.remove_draft_fixture()


@pytest.fixture(scope="module")
def swept(sweep_ready, request):
    """跑一轮全量扫描，结果给同模块的多条断言复用。"""
    real, fx = sweep_ready
    client = probe.make_client()
    app = probe.get_app()
    base = probe.run_cases(client, probe.baseline_cases(app, real))
    leak = probe.run_cases(client, probe.leak_cases(app, real, fx))
    ctrl = probe.run_cases(client, probe.admin_control_cases(fx))
    return real, fx, base, leak, ctrl


class Test草稿不泄漏到前台:
    def test_正对照_管理台看得见这批草稿(self, swept):
        """先证明受试对象真的存在且可读，否则下面的「看不见」全是假绿。"""
        _, _, _, _, ctrl = swept
        blind = [r.case.label for r in ctrl if SENTINEL not in r.text]
        assert not blind, (
            f"管理台也看不到草稿，说明受试对象没造成功 / 管理台读路径把草稿滤掉了："
            f"{blind}（此时「前台看不见」这条断言没有意义）"
        )

    def test_前台全部GET接口都看不到草稿(self, swept):
        """遍历路由表里所有 `前台 ·` 打头的 GET（外加 POST /v1/graph/expand）。

        两组用例：基线（真实 id，抓「草稿边挂在已发布节点上」这种泄漏）
        + 逐参数替换成草稿 id / 哨兵关键字（抓按 id 点查与按名搜索的泄漏）。
        """
        _, fx, base, leak, _ = swept
        leaks = probe.find_leaks(base + leak, fx.tokens)
        assert not leaks, "草稿泄漏到前台：\n" + "\n".join(leaks[:25])

    def test_草稿把前台接口打死也算泄漏面(self, swept):
        """草稿行是新形态数据，读路径没见过它。一行草稿让整页 500 属于
        CLAUDE.md 那条「一条脏数据不能打死一整页」，判定口径只看 5xx。"""
        _, _, base, leak, _ = swept
        bad = [
            f"{r.case.label} → {r.status} {r.text[:120]}"
            for r in base + leak
            if r.status >= 500
        ]
        assert not bad, "草稿受试对象在位时前台出现 5xx：\n" + "\n".join(bad[:15])

    @pytest.mark.parametrize(
        "path,param",
        [
            ("/v1/node", "id"),
            ("/v1/student/positions/skill-composition", "id"),
            ("/v1/student/positions/courses", "id"),
            ("/v1/student/goal/overview", "occupation_id"),
        ],
    )
    def test_按id点查草稿节点不能返回它(self, swept, path, param):
        """§6.1 点名的两处「按 id 点查且完全没有状态过滤」就藏在这条路上
        （`goal_overview.py` 与 `query.py` 各一处）。

        这两处**本来就是既有 bug**（按 id 已经能读到 archived 数据），
        草稿态只是让它从「能读到归档」升级成「能读到别人还没发布的内容」。
        """
        real, fx, _, _, _ = swept
        client = probe.make_client()
        r = client.get(path, params={param: fx.node_ids["occupation"]})
        body = r.text
        assert SENTINEL not in body, f"HTTP {r.status_code}：{body[:200]}"
        assert probe.ID_PREFIX not in body or param in body, f"HTTP {r.status_code}"

    def test_影子草稿行不会把改名泄漏到前台(self, swept):
        """最锋利的探针：草稿行与线上行**同 id**，只有 name 不同。

        任何一处读路径少了 `is_draft` 过滤（比如 §6.2 的 `DISTINCT ON` 片段被抄到
        前台查询里），前台就会显示哨兵名字。这种泄漏不会让记录数变化，
        只是内容悄悄换成了未发布版本 —— 靠计数或「多出一条」的断言抓不到。
        """
        real, fx, base, leak, _ = swept
        if not fx.shadow_id:
            pytest.skip(f"影子草稿行没造出来：{fx.notes}")
        hits = [
            r.case.label
            for r in base + leak
            if f"{SENTINEL}改名" in r.text
        ]
        assert not hits, f"前台显示了草稿版本的名字：{hits[:10]}"

    def test_线上名字仍然搜得到(self, swept):
        """反面：编辑中的记录**不该从前台消失**（§0.1）。

        影子草稿行在位时，前台按线上名字必须照样搜得到 —— 否则就是把
        「有草稿」实现成了「记录消失」，运营会以为数据被删了。
        """
        real, fx, _, _, _ = swept
        if not fx.shadow_orig_name:
            pytest.skip("没有影子草稿行")
        client = probe.make_client()
        r = client.get("/v1/search", params={"q": fx.shadow_orig_name, "limit": 20})
        assert r.status_code == 200
        assert fx.shadow_id in r.text, (
            f"线上行「{fx.shadow_orig_name}」在有草稿时从前台搜索里消失了"
        )


class Test单行草稿也不能进前台:
    """迁移前的形态：`status='draft'` 的**线上行**（BR-07）。

    这不是历史包袱清理项 —— §5 的注写明库里 credential 那批就是这个形态，
    本机制不追溯改它们。所以「status=draft 的线上行不进前台」这条老规则
    在草稿态上线后**必须继续成立**，属于回归项。
    """

    def test_status为draft的线上行不进前台(self):
        real = probe.pick_real_ids()
        if not real.get("occupation_id"):
            pytest.skip("挑不到受试岗位")
        fx = probe.install_draft_fixture("status")
        try:
            client = probe.make_client()
            app = probe.get_app()
            res = probe.run_cases(client, probe.baseline_cases(app, real))
            res += probe.run_cases(client, probe.leak_cases(app, real, fx))
            leaks = probe.find_leaks(res, fx.tokens)
            assert not leaks, "status='draft' 的线上行泄漏到前台：\n" + "\n".join(leaks[:20])
        finally:
            probe.remove_draft_fixture()


@pytest.fixture(scope="module")
def edit_path_observations(request):
    """把 §4「运营编辑内容」那一类入口**逐个真跑一遍**，记录每次之后库里的形态。

    为什么要一个共享 fixture 而不是各测各的：这一串动作要在同一个
    `LiveRowGuard` 里跑完再统一还原（`skill_composition` 会裸 DELETE 边），
    跑一次记全量观测、多条断言各看自己关心的那一列，比每条断言各跑一遍安全也快。

    每项观测：
      name        动作名
      error       抛错信息（None=正常返回）
      draft_nodes 该动作之后受试 id 上有几个草稿节点行
      draft_edges 同上，草稿边行
      bad_status  status 不是 draft 的草稿行（§0.2 的不变量）
      live_diff   线上行被改了哪些列（§0.3）
    """
    caps = probe.db_capabilities()
    if not caps["has_is_draft"]:
        pytest.skip("kg_node/kg_edge 没有 is_draft 列：方案 §2 的 DDL 尚未落地")
    real = probe.pick_real_ids()
    if not real.get("occupation_id"):
        pytest.skip("挑不到受试岗位")

    from backend.kg.pg_store import skill_composition as sc
    from backend.kg.pg_store import write
    from backend.kg.pg_store.client import connect

    occ, sk = real["occupation_id"], real["skill_id"]
    new_id = "ZZ:draftprobe:writepath:1"
    comp = sc.get_composition(occ)
    first = (comp.get("items") or [{}])[0]
    first_key = first.get("skill_key")
    # 档位必须从该技能**已配齐的档**里取：一技能一档，随手写 3 会被「没有 L3 档」
    # 挡在业务校验上，于是测不到草稿化那一层
    lvl = first.get("selected_level") or (first.get("available_levels") or [None])[0]

    ops: list[tuple[str, Any]] = [
        ("write.create_node（新建）", lambda: write.create_node(
            {"id": new_id, "type": "occupation", "name": f"{SENTINEL}写路径",
             "region": "CN", "source_system": "MANUAL", "source_id": new_id,
             "source_url": "manual://t", "license": "internal"},
            user_id="9201", user_name="t")),
        ("write.patch_node（改名）", lambda: write.patch_node(
            occ, {"name": f"{SENTINEL}改名"}, user_id="9201", user_name="t")),
        ("write.patch_node（改 attrs）", lambda: write.patch_node(
            occ, {"attrs": {"zz_probe": "1"}}, user_id="9201", user_name="t")),
        ("write.create_edge（加边）", lambda: write.create_edge(
            {"src_id": occ, "dst_id": sk, "rel_type": "requires",
             "region": "CN", "weight": 0.15, "source_system": "MANUAL",
             "source_url": "manual://t", "license": "internal"},
            user_id="9201", user_name="t")),
        ("write.apply_node_links（改关联）", lambda: write.apply_node_links(
            occ, "occupation", {"major_ids": [real["major_id"]]},
            user_id="9201", user_name="t")),
        # write.archive_node 已改成立即生效（见 EDITS 注释），不再属于「不写线上行」这一类
    ]
    if first_key:
        ops += [
            ("skill_composition.set_skill（改档/改权重）", lambda: sc.set_skill(
                occ, first_key, level=lvl, weight=0.3, user_id="9201", user_name="t")),
            ("skill_composition.remove_skill（裸 DELETE）", lambda: sc.remove_skill(
                occ, first_key, user_id="9201", user_name="t")),
        ]
    ops.append(("skill_composition.normalize_weights", lambda: sc.normalize_weights(
        occ, user_id="9201", user_name="t")))
    # §4 明确列了 `skill_write.py:245` —— 那处 `_delete_requires_into_nodes` 是**裸 DELETE
    # 线上边**，走的是「技能多档」编辑入口（POST / PATCH /v1/admin/skills/{skill_key}）。
    # 漏改的后果：运营在技能库里改一下岗位关联，前台的技能构成当场变，完全绕过草稿。
    #
    # 受试用**哨兵 skill_key**，不动库里任何真实技能 —— 这条路径会建/改 skill_level 节点，
    # 拿真实技能当受试对象，恢复不干净就直接污染了图数据。
    from backend.kg.pg_store import skill_write as sw

    probe_key = f"{SENTINEL}多档"
    bundle = {
        "skill_key": probe_key, "name": probe_key, "region": "CN",
        "levels": {"L3": {"label": "掌握", "description": "ZZ 探针档位描述"}},
        "occupation_links": [{"occupation_id": occ, "level_code": "L3", "weight": 0.5}],
    }
    ops += [
        ("skill_write.apply_skill_bundle_create（技能多档新建）",
         lambda: sw.apply_skill_bundle_create(bundle, user_id="9201", user_name="t")),
        ("skill_write.apply_skill_bundle_update（岗链重建·裸 DELETE）",
         lambda: sw.apply_skill_bundle_update(
             probe_key,
             {**bundle, "occupation_links": [
                 {"occupation_id": occ, "level_code": "L3", "weight": 0.7}]},
             user_id="9201", user_name="t")),
    ]

    obs: list[dict] = []
    # 受试 id 全部纳入守卫：`apply_node_links` 会给**对方节点**（专业）也建草稿行，
    # 不看着它的话草稿行会留在库里，下次跑基线就不是原来那个基线了。
    watched = [occ, new_id, real["major_id"]]
    guard = probe.LiveRowGuard(watched, watched)
    with guard:
        try:
            for name, fn in ops:
                # **逐个动作**取前后快照，而不是和最初的基线比：草稿行一旦被前一个动作
                # 造出来就一直在，累计口径下后面每个动作看起来都「有草稿」，
                # 于是漏改的那个动作被前面的动作掩护过去。
                before_live = _live_snapshot(watched)
                before_draft = _draft_counts(watched)
                err = None
                try:
                    fn()
                except Exception as e:  # noqa: BLE001 —— 抛错本身是观测的一部分
                    err = f"{type(e).__name__}: {e}"
                after_live = _live_snapshot(watched)
                after_draft = _draft_counts(watched)
                obs.append({
                    "name": name,
                    "error": err,
                    "draft_delta": (after_draft[0] - before_draft[0],
                                    after_draft[1] - before_draft[1]),
                    "bad_status": probe.bad_status_draft_rows(),
                    "live_node_diff": _dict_diff(before_live[0], after_live[0]),
                    "live_edge_diff": _dict_diff(before_live[1], after_live[1]),
                })
        finally:
            with connect() as c:
                c.execute("DELETE FROM kg_edge WHERE src_id=%s OR dst_id=%s",
                          (new_id, new_id))
                c.execute("DELETE FROM kg_node WHERE id=%s", (new_id,))
                # 技能多档那条路径的节点 id 是内容哈希，按 skill_key 反查才删得净
                ids = [r["id"] for r in c.execute(
                    "SELECT id FROM kg_node WHERE attrs::json->>'skill_key' = %s",
                    (probe_key,),
                ).fetchall()]
                if ids:
                    c.execute(
                        "DELETE FROM kg_edge WHERE src_id = ANY(%s) OR dst_id = ANY(%s)",
                        (ids, ids),
                    )
                    c.execute("DELETE FROM kg_node WHERE id = ANY(%s)", (ids,))
                c.commit()
    return obs


class Test草稿行的status恒为draft:
    """§0.2 的不变量。破了就泄漏，所以每个写路径跑完都验一次。"""

    def test_库里当前不存在status不是draft的草稿行(self, db_ready):
        bad = probe.bad_status_draft_rows()
        assert not bad["kg_node"], f"kg_node 有草稿行的 status 不是 draft：{bad['kg_node']}"
        assert not bad["kg_edge"], f"kg_edge 有草稿行的 status 不是 draft：{bad['kg_edge']}"

    def test_每个运营编辑写路径产出的草稿行都是draft(self, edit_path_observations):
        bad = [
            f"{o['name']} 之后：{o['bad_status']}"
            for o in edit_path_observations
            if o["bad_status"]["kg_node"] or o["bad_status"]["kg_edge"]
        ]
        assert not bad, (
            "草稿行的 status 不是 draft —— 前台那 ~120 处 status='published' 查询会立刻"
            "命中它（§0.2）：\n" + "\n".join(bad)
        )

    def test_至少有一个动作真的产出了草稿行(self, edit_path_observations):
        """上一条不能孤立地看：一个草稿行都没产出时，「status 恒为 draft」是空转。

        只要求「至少一个」而不是「每一个」：`normalize_weights` 这种在权重已归一时
        本来就是空操作，要求它产出草稿行是错的要求。
        「每一个动作都不碰线上行」才是那条硬约束，见下一个类。
        """
        made = [o["name"] for o in edit_path_observations if any(o["draft_delta"])]
        assert made, (
            "所有运营编辑动作都没产出草稿行 —— 要么 §4 的草稿化整批没做，"
            "要么受试对象选得不对，此时「status 恒为 draft」这条断言是空转的"
        )

    def test_每个运营编辑动作都不碰线上行(self, edit_path_observations):
        """§0.3 / §4：运营编辑只写草稿行，线上行由发布落地。

        逐个动作前后对比（不是和最初基线比）—— 累计口径下，前一个动作留下的差异会
        被算到后一个动作头上，也会让「本来就没改」的动作看起来是改了。
        """
        bad = []
        for o in edit_path_observations:
            if o["live_node_diff"]:
                bad.append(f"{o['name']} 改了线上节点：{o['live_node_diff']}")
            if o["live_edge_diff"]:
                bad.append(f"{o['name']} 改了线上边：{o['live_edge_diff']}")
        assert not bad, "\n".join(bad)

    def test_编辑动作本身不该抛错(self, edit_path_observations):
        errs = [f"{o['name']} → {o['error']}" for o in edit_path_observations if o["error"]]
        assert not errs, "\n".join(errs)


class Test编辑不写线上行:
    """§0.3：运营编辑只写草稿行。线上行由发布落地。

    注意 `kg_node` **没有 `updated_at` 列**（只有 `created_at` / `updated_by` /
    `updated_by_name` / `version`），所以「`updated_at` 未变」这条只能升级成
    「整行逐列未变」—— 比原要求更严，也顺手覆盖了 version 被提前 +1 的情形。
    """

    # 2026-08-19 需求收窄：**停用 / 启用 / 删除（含归档）改成立即生效，不进草稿**。
    # 所以「归档」从这一组里移出去了 —— 它现在**应该**改线上行，
    # 留在这里断言「不写线上行」就是在锁一个已经作废的行为。
    # 立即生效由下面 Test状态动作立即生效 正面覆盖（不是删掉，是换了断言方向）。
    EDITS = ["改名", "改attrs", "加边", "删边", "改权重"]

    @pytest.mark.parametrize("kind", EDITS)
    def test_编辑后线上行逐列未变(self, db_ready, caps, real_ids, client, kind):
        if not caps["has_is_draft"]:
            pytest.skip("is_draft 列不存在")
        occ = real_ids["occupation_id"]
        guard = probe.LiveRowGuard([occ], [occ])
        with guard:
            before = dict(guard.live_node(occ) or {})
            assert before, "受试线上行不存在"
            edges_before = _live_edge_map(occ)
            _apply_edit(client, kind, occ, real_ids)
            after = probe.live_row(occ) or {}
            diff = {k: (before.get(k), after.get(k)) for k in before
                    if before.get(k) != after.get(k)}
            assert not diff, f"「{kind}」改到了线上行：{diff}"
            if kind in ("加边", "删边", "改权重"):
                edges_after = _live_edge_map(occ)
                ediff = _dict_diff(edges_before, edges_after)
                assert not ediff, f"「{kind}」改到了线上**边**：{ediff}"


class Test状态动作立即生效:
    """停用 / 启用 / 归档 / 删除**不进草稿**，点了就改线上行（2026-08-19 需求收窄）。

    这一组与 `Test编辑不写线上行` 方向相反，是刻意的：草稿只管「内容长什么样」
    （节点属性 / 边 / 技能构成 / 技能自身属性），状态动作立即生效。
    """

    def test_归档立即改线上行且不留草稿(self, db_ready, caps, real_ids, client):
        if not caps["has_is_draft"]:
            pytest.skip("is_draft 列不存在")
        from backend.kg.pg_store.client import connect

        # 用 industry：它没有 BR 门禁（岗位要 Σweight≈1、专业要 ≥1 岗位），
        # 否则「先发布出来」这一步会被门禁拦下，测的就不是归档了
        nid = "ZZ:draftprobe:status-now"
        client.post("/v1/kg/nodes", json={
            "id": nid, "type": "industry", "name": "ZZ状态动作立即生效", "region": "CN",
            "source_system": "MANUAL", "source_id": nid,
            "source_url": "manual://t", "license": "internal"})
        # 新建只有草稿行 → 先发布出来，才有线上行可归档
        client.post("/v1/admin/publish/node", params={"node_id": nid})
        try:
            r = client.delete(f"/v1/kg/nodes/{nid}")
            assert r.status_code == 200, r.text[:200]
            with connect() as c:
                rows = [dict(x) for x in c.execute(
                    "SELECT is_draft, status FROM kg_node WHERE id=%s ORDER BY is_draft",
                    (nid,)).fetchall()]
            assert rows, "记录不该消失（归档是软删）"
            online = [r for r in rows if not r["is_draft"]]
            assert online and online[0]["status"] == "archived", (
                f"归档应当立即改线上行，实际 {rows}")
            assert not [r for r in rows if r["is_draft"]], (
                f"归档不该留草稿行，实际 {rows}")
        finally:
            with connect() as c:
                c.execute("DELETE FROM kg_edge WHERE src_id=%s OR dst_id=%s", (nid, nid))
                c.execute("DELETE FROM kg_node WHERE id=%s", (nid,))
                c.commit()


class Test管理台不重复:
    """§6.2：草稿行也满足管理台口径（`status <> 'archived'`），同一记录会出现两行。"""

    def test_编辑后管理台列表里仍只有一行(self, db_ready, caps, real_ids, client):
        if not caps["has_is_draft"]:
            pytest.skip("is_draft 列不存在")
        occ = real_ids["occupation_id"]
        guard = probe.LiveRowGuard([occ], [occ])
        with guard:
            _apply_edit(client, "改名", occ, real_ids)
            r = client.get(
                "/v1/kg/nodes",
                params={"type": "occupation", "scope": "manage", "q": SENTINEL,
                        "page_size": "50"},
            )
            assert r.status_code == 200, r.text[:200]
            items = r.json().get("items") or []
            same = [x for x in items if x.get("id") == occ]
            assert len(same) == 1, (
                f"同一记录在管理台列表里出现 {len(same)} 行（§6.2 的 DISTINCT ON 没生效）"
            )

    def test_管理台把它标成草稿态(self, db_ready, caps, real_ids, client):
        """§0：管理台展示的「记录状态」= 最新版本的状态，有草稿行就是「草稿」。

        字段名由实现定，这里只要求**某个字段能把「这条有草稿」表达出来**：
        `record_status` / `has_draft` / `is_draft` 任一即可。
        """
        if not caps["has_is_draft"]:
            pytest.skip("is_draft 列不存在")
        occ = real_ids["occupation_id"]
        guard = probe.LiveRowGuard([occ], [occ])
        with guard:
            _apply_edit(client, "改名", occ, real_ids)
            r = client.get("/v1/kg/node-detail", params={"id": occ})
            assert r.status_code == 200, r.text[:200]
            body = r.json()
            flat = _flatten(body)
            marks = {
                k: v for k, v in flat.items()
                if k.split(".")[-1] in ("record_status", "has_draft", "is_draft", "status")
            }
            assert marks, f"管理台详情里没有任何「有草稿」的标记：{sorted(flat)[:30]}"
            drafty = any(
                (v is True) or (isinstance(v, str) and v == "draft") for v in marks.values()
            )
            assert drafty, f"有草稿却没标出来：{marks}"

    def test_管理台详情能同时给出线上与草稿两份(self, db_ready, caps, real_ids, client):
        """§6.3：详情返回 `published{}` + `draft{}`，供「改前 / 改后」对比。"""
        if not caps["has_is_draft"]:
            pytest.skip("is_draft 列不存在")
        occ = real_ids["occupation_id"]
        guard = probe.LiveRowGuard([occ], [occ])
        with guard:
            before_name = (guard.live_node(occ) or {}).get("name")
            _apply_edit(client, "改名", occ, real_ids)
            r = client.get("/v1/kg/node-detail", params={"id": occ})
            assert r.status_code == 200, r.text[:200]
            flat = _flatten(r.json())
            names = {k: v for k, v in flat.items() if k.endswith("name")}
            assert any(v == before_name for v in names.values()), (
                f"详情里拿不到线上（改前）名字「{before_name}」，改前/改后无从对比：{names}"
            )
            assert any(isinstance(v, str) and SENTINEL in v for v in names.values()), (
                f"详情里拿不到草稿（改后）名字：{names}"
            )


# ── 编辑动作与小工具 ─────────────────────────────────────────


def _apply_edit(client, kind: str, occ: str, real: dict) -> None:
    """把「一组覆盖各类型的编辑」（§12）落成具体的管理台调用。

    走 HTTP 而不是直接调 `write.*`：管理台是唯一的运营入口，草稿化要在这条路上生效。
    """
    if kind == "改名":
        r = client.patch(f"/v1/kg/nodes/{occ}", json={"name": f"{SENTINEL}改名"})
    elif kind == "改attrs":
        r = client.patch(f"/v1/kg/nodes/{occ}", json={"attrs": {"zz_probe": SENTINEL}})
    elif kind == "加边":
        r = client.post("/v1/kg/edges", json={
            "src_id": occ, "dst_id": real["skill_id"], "rel_type": "requires",
            "region": "CN", "weight": 0.13, "source_system": "MANUAL",
            "source_url": "manual://t", "license": "internal"})
    elif kind == "删边":
        eid = next(iter(_live_edge_map(occ)), None)
        if not eid:
            pytest.skip("受试岗位没有可删的已发布边")
        r = client.delete(f"/v1/kg/edges/{eid}")
    elif kind == "改权重":
        comp = client.get("/v1/admin/composition", params={"node_id": occ})
        items = (comp.json() or {}).get("items") or []
        if not items:
            pytest.skip("受试岗位没有技能构成")
        it = items[0]
        r = client.put("/v1/admin/composition", params={"node_id": occ}, json={
            "skill_key": it["skill_key"],
            "level": it.get("selected_level") or 3,
            "weight": 0.42})
    elif kind == "归档":
        r = client.delete(f"/v1/kg/nodes/{occ}")
    else:  # pragma: no cover
        raise AssertionError(kind)
    assert r.status_code < 500, f"「{kind}」调用本身 5xx：{r.status_code} {r.text[:200]}"


def _live_snapshot(ids: list[str]) -> tuple[dict, dict]:
    """这批 id 上的**线上**节点行与线上边，取影响读路径的列。"""
    from backend.kg.pg_store.client import connect

    with connect() as c:
        nodes = {
            (r["id"]): (r["name"], r["status"], r["attrs"], r["version"], r["description"])
            for r in c.execute(
                "SELECT id, name, status, attrs, version, description FROM kg_node "
                "WHERE id = ANY(%s) AND NOT is_draft", (ids,)
            ).fetchall()
        }
        edges = {
            r["id"]: (r["src_id"], r["dst_id"], r["rel_type"],
                      None if r["weight"] is None else float(r["weight"]), r["status"])
            for r in c.execute(
                "SELECT id, src_id, dst_id, rel_type, weight, status FROM kg_edge "
                "WHERE (src_id = ANY(%s) OR dst_id = ANY(%s)) AND NOT is_draft", (ids, ids)
            ).fetchall()
        }
    return nodes, edges


def _draft_counts(ids: list[str]) -> tuple[int, int]:
    from backend.kg.pg_store.client import connect

    with connect() as c:
        n = c.execute(
            "SELECT COUNT(*) AS c FROM kg_node WHERE is_draft AND id = ANY(%s)", (ids,)
        ).fetchone()["c"]
        e = c.execute(
            "SELECT COUNT(*) AS c FROM kg_edge WHERE is_draft "
            "AND (src_id = ANY(%s) OR dst_id = ANY(%s))", (ids, ids)
        ).fetchone()["c"]
    return int(n), int(e)


def _live_edge_map(nid: str) -> dict[str, tuple]:
    """该节点相关的**线上**边（不含草稿行），取影响读路径的几列。"""
    from backend.kg.pg_store.client import connect

    caps = probe.db_capabilities()
    sql = (
        "SELECT id, src_id, dst_id, rel_type, weight, status FROM kg_edge "
        "WHERE (src_id=%s OR dst_id=%s)"
    )
    if caps["has_is_draft"]:
        sql += " AND NOT is_draft"
    with connect() as c:
        return {
            r["id"]: (r["src_id"], r["dst_id"], r["rel_type"],
                      None if r["weight"] is None else float(r["weight"]), r["status"])
            for r in c.execute(sql, (nid, nid)).fetchall()
        }


def _dict_diff(a: dict, b: dict) -> dict:
    out = {}
    for k in set(a) | set(b):
        if a.get(k) != b.get(k):
            out[k] = (a.get(k), b.get(k))
    return out


def _flatten(obj, prefix: str = "") -> dict:
    out: dict = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:5]):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out
