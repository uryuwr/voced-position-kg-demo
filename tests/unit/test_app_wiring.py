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


# ── 「算不出分」的契约（定案 2026-08）─────────────────────────
#
# 库内 80% 的岗位一项要求档都没配，匹配度是 **null** 而不是 0%。前端按契约
# 决定「显示数字还是显示说明」，所以这几个字段/枚举值少一个就会退回显示 0%
# ——「完全不匹配」和「岗位没配能力要求」在学员眼里是两件完全不同的事。


class TestScoreContract:
    @staticmethod
    def _schema(name: str) -> dict:
        return app.openapi()["components"]["schemas"][name]

    def test_匹配度在契约里是可空的(self):
        for model in ("AssessmentReportOut", "PositionMatchOut"):
            f = self._schema(model)["properties"]["match_score"]
            types = {s.get("type") for s in f.get("anyOf", [f])}
            assert "null" in types, f"{model}.match_score 不可空，前端拿不到「算不出分」"

    def test_匹配度不是必填(self):
        req = self._schema("AssessmentReportOut").get("required") or []
        assert "match_score" not in req, "算不出分时这个键要能是 null"

    @pytest.mark.parametrize("model", ["AssessmentReportOut", "PositionMatchOut"])
    def test_两个入口都带基准缺口字段(self, model):
        props = self._schema(model)["properties"]
        assert "no_baseline_weight" in props, "缺「多少权重因没配要求档而无法评分」"
        assert "no_baseline" in props, "缺「哪些技能无法评分」的清单"
        assert "score_status" in props

    @pytest.mark.parametrize("model", ["ReportItem", "MatchItem"])
    def test_逐项带可评分标记且默认为真(self, model):
        """历史数据没有这个键，默认必须是 true，否则老报告会整份变成「不可评分」。"""
        f = self._schema(model)["properties"]["scorable"]
        assert f.get("default") is True

    def test_算分状态的取值与后端词表同源(self):
        from backend.kg.pg_store.config import SCORE_STATUSES

        f = self._schema("AssessmentReportOut")["properties"]["score_status"]
        assert tuple(f["enum"]) == SCORE_STATUSES

    def test_匹配度来源多了无基准这一档(self):
        f = self._schema("PositionMatchOut")["properties"]["source"]
        assert "no_baseline" in f["enum"], (
            "少了这一档，无基准岗位只能落到 no_overlap，"
            "文案变成「你的画像未覆盖该岗位要求的技能」——把数据缺口说成学员的问题"
        )

    def test_匹配度来源也有配置不全这一档(self):
        """缺口 82% 与 100% 不该一个报「实测」一个报数据缺口 —— 同一逻辑的连续形态。"""
        f = self._schema("PositionMatchOut")["properties"]["source"]
        assert "partial_baseline" in f["enum"]

    def test_降级阈值只在服务端定义一次(self):
        """放前端就成了每个页面各定一次的魔数，迟早不一致。"""
        from backend.kg.pg_store.config import PARTIAL_BASELINE_PCT

        assert PARTIAL_BASELINE_PCT == 30.0
        desc = self._schema("PositionMatchOut")["properties"]["source"].get("description") or ""
        desc += self._schema("PositionMatchOut").get("description") or ""
        assert "30" in desc, "阈值没写进契约说明，前端只能猜"


class TestMatchRouteOrder:
    """`has_baseline` 必须在证据级联**之前**判。

    往下走会被 `covered_count == 0` 误判成 `no_overlap`。这条顺序没法靠单测发请求验
    （要连库、要 Token），所以在源码上钉一道 —— 与 test_uc_tls.py 里那几条同一手法。
    """

    @staticmethod
    def _src() -> str:
        from pathlib import Path

        import backend.api.routes_student as m

        return Path(m.__file__).read_text(encoding="utf-8")

    def test_无基准判定排在诊断与画像级联之前(self):
        src = self._src()
        gate = src.index("has_baseline(required)")
        assert gate < src.index('"source": "diagnosis",'), "无基准判定被排到诊断分支之后了"
        assert gate < src.index('"source": "assessment"')
        assert gate < src.index('"source": "no_overlap"')

    def test_无技能构成的判定仍排在最前(self):
        """「岗位没有技能构成」比「没有要求档」更靠前：连技能都没有就谈不上基准。"""
        src = self._src()
        assert src.index("该岗位尚未配置技能构成") < src.index("has_baseline(required)")

    def test_无基准时明确不是学员的问题(self):
        src = self._src()
        head = src[src.index("has_baseline(required)"):][:600]
        assert '"no_baseline"' in head and "match_score\": None" in head
        assert "未配置能力要求档" in head or "尚未配置能力要求档" in head

    def test_配置不全的降级挂在每条给分的返回路径上(self):
        """漏挂一条，同一个岗位就会因为「测过没测过」一个带提示一个不带。"""
        src = self._src()
        body = src[src.index("def student_position_match"):]
        body = body[: body.index("\n@router.")] if "\n@router." in body else body
        assert body.count("_mark_partial(detail)") == 3, (
            "给分的返回路径有三条（diagnosis / assessment / memory），"
            f"只有 {body.count('_mark_partial(detail)')} 条过了降级"
        )
        assert "degrade_for_baseline_gap" in body, "score_status 也要跟着降级，两个字段口径要一致"

    def test_降级用服务端常量而不是写死的三十(self):
        src = self._src()
        assert "PARTIAL_BASELINE_PCT" in src
        assert "> 30" not in src and "no_baseline_weight > 30" not in src


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
