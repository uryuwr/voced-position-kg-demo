"""应用装配 + 发布门禁里的纯算法。

中间件顺序在 CLAUDE.md 里被单独点名过：Starlette 中「后 add = 更靠外」，CORS 必须
包在鉴权外层。装反了的症状是浏览器只报跨域、看不到真实的 401 原因 —— 排查成本极高，
但只要 import 一下 app 就能验，不需要起服务。

只 import `app` 对象，不发请求、不连库（conftest 已把 DATABASE_URL 指到死地址）。
"""
from __future__ import annotations

import pytest

from backend.api.main import app


class TestMiddlewareOrder:
    def test_CORS包在鉴权外层(self):
        """user_middleware 是「后 add 的在前」，所以 CORS 必须排在鉴权之前。"""
        names = [m.cls.__name__ for m in app.user_middleware]
        assert "CORSMiddleware" in names, "缺 CORS，浏览器侧全挂"
        assert "UCAuthMiddleware" in names
        assert names.index("CORSMiddleware") < names.index("UCAuthMiddleware"), (
            f"中间件顺序反了（{names}）：预检 OPTIONS 会被鉴权 401，"
            "且 401 响应缺 Access-Control-* 头，浏览器只报跨域"
        )

    def test_通配来源用正则而不是星号列表(self):
        """`allow_origins=['*']` 与 `allow_credentials=True` 组合会回 `ACAO: *`，
        浏览器按规范拒收带凭据的响应 —— 必须用 allow_origin_regex 回显具体 Origin。"""
        cors = next(m for m in app.user_middleware if m.cls.__name__ == "CORSMiddleware")
        opts = cors.kwargs
        if opts.get("allow_credentials"):
            assert opts.get("allow_origins") != ["*"], "带凭据时不能用 allow_origins=['*']"

    def test_预检要允许所有方法与头(self):
        cors = next(m for m in app.user_middleware if m.cls.__name__ == "CORSMiddleware").kwargs
        assert cors.get("allow_methods") == ["*"]
        assert cors.get("allow_headers") == ["*"]


class TestOpenApi:
    def test_契约能生成(self):
        """`/openapi.json` 200 是独立部署自查的三条之一。"""
        schema = app.openapi()
        assert schema["openapi"].startswith("3.")
        assert schema["paths"]

    def test_健康检查与配置接口都在(self):
        paths = set(app.openapi()["paths"])
        assert "/health" in paths
        assert "/v1/config" in paths

    def test_业务路由都挂在v1下(self):
        biz = [p for p in app.openapi()["paths"] if p.startswith("/v1/")]
        assert len(biz) > 20, "路由掉了一大片"


# ── BR-05：技能前置关系不能成环（纯算法）─────────────────────


class TestFindCyclicKeys:
    """`publish_rules._find_cyclic_keys` 不碰库，是发布门禁 BR-05 的判定核心。"""

    @staticmethod
    def find(graph):
        from backend.kg.pg_store.publish_rules import _find_cyclic_keys

        return _find_cyclic_keys(graph)

    def test_空图无环(self):
        assert self.find({}) == set()

    def test_链式无环(self):
        assert self.find({"a": ["b"], "b": ["c"], "c": []}) == set()

    def test_自环(self):
        assert self.find({"a": ["a"]}) == {"a"}

    def test_二元环(self):
        assert self.find({"a": ["b"], "b": ["a"]}) == {"a", "b"}

    def test_三元环(self):
        assert self.find({"a": ["b"], "b": ["c"], "c": ["a"]}) == {"a", "b", "c"}

    def test_环外节点不被牵连(self):
        got = self.find({"a": ["b"], "b": ["c"], "c": ["b"], "d": ["a"]})
        assert {"b", "c"} <= got
        assert "d" not in got, "只指向环、自身不在环上的节点不该被判为成环"

    def test_菱形不算环(self):
        assert self.find({"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": []}) == set()

    def test_两个不相连的环都能找出来(self):
        got = self.find({"a": ["b"], "b": ["a"], "x": ["y"], "y": ["x"]})
        assert got == {"a", "b", "x", "y"}

    def test_指向图外的节点不炸(self):
        assert self.find({"a": ["不存在的技能"]}) == set()

    def test_不改动传入的图(self):
        g = {"a": ["b"], "b": ["a"]}
        snapshot = {k: list(v) for k, v in g.items()}
        self.find(g)
        assert g == snapshot


class TestPackResult:
    @staticmethod
    def pack(checks):
        from backend.kg.pg_store.publish_rules import _pack

        return _pack(checks)

    def test_全通过时放行(self):
        out = self.pack([{"rule": "BR-02", "ok": True}])
        assert out["ok"] is True and out["failed"] == []

    def test_有一条不过就拦下(self):
        out = self.pack([{"rule": "BR-02", "ok": True}, {"rule": "BR-03", "ok": False}])
        assert out["ok"] is False
        assert [c["rule"] for c in out["failed"]] == ["BR-03"]

    def test_缺ok字段按不通过处理(self):
        """门禁必须是「默认拒绝」——检查项忘了写 ok 时不能悄悄放行。"""
        assert self.pack([{"rule": "BR-99"}])["ok"] is False

    def test_没有检查项时视为通过(self):
        assert self.pack([])["ok"] is True

    def test_回显规则范围(self):
        assert "BR-" in self.pack([])["rules"]


class TestPublishGateError:
    def test_门禁不过时抛专用异常便于路由转四百(self, monkeypatch):
        from backend.kg.pg_store import publish_rules

        monkeypatch.setattr(
            publish_rules, "validate_publish",
            lambda **kw: {"ok": False, "failed": [{"rule": "BR-03", "msg": "岗位没有技能要求"}]},
        )
        with pytest.raises(publish_rules.PublishGateError):
            publish_rules.assert_publish_allowed(node_id="x")

    def test_门禁通过时原样返回(self, monkeypatch):
        from backend.kg.pg_store import publish_rules

        monkeypatch.setattr(publish_rules, "validate_publish", lambda **kw: {"ok": True, "failed": []})
        assert publish_rules.assert_publish_allowed(node_id="x")["ok"] is True
