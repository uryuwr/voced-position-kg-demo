"""重构涉及面的契约测试 —— 锁行为，不锁实现。

这批用例针对并行重构改到的三处（鉴权开关、简历关键词表、`get_node` 可见性）。
写的时候前两处还没落地、是红的；本文件最后一次运行时重构已经合入，**全部转绿**。
它们从此是回归网：谁把 `DEBUG` 重新或回 `AUTH_DEBUG`、谁再复制一份关键词表、
谁把 `get_node` 的 scope 去掉，这里立刻报。
"""
from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

import pytest

from backend.agent import diagnose, skill_keywords
from backend.kg.pg_store import biz_store, query

REPO_ROOT = Path(__file__).resolve().parents[2]
SETTINGS_PY = REPO_ROOT / "backend" / "settings.py"


# ══════════════════════════════════════════════════════════════
# 1. settings：AUTH_DEBUG 不被 DEBUG=1 顺带打开
# ══════════════════════════════════════════════════════════════


class TestAuthSwitches:
    """`settings_probe` 见 conftest：隔离 .env 后重新执行 settings.py。"""

    def test_默认两个开关都关(self, settings_probe):
        s = settings_probe()
        assert s.AUTH_BYPASS is False
        assert s.AUTH_DEBUG is False

    def test_旁路开关可开(self, settings_probe):
        assert settings_probe(AUTH_BYPASS="1").AUTH_BYPASS is True

    def test_调试开关可开(self, settings_probe):
        assert settings_probe(AUTH_DEBUG="1").AUTH_DEBUG is True

    def test_两个开关互不影响(self, settings_probe):
        assert settings_probe(AUTH_DEBUG="1").AUTH_BYPASS is False
        assert settings_probe(AUTH_BYPASS="1").AUTH_DEBUG is False

    @pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "on", " on "])
    def test_多种真值写法(self, settings_probe, truthy):
        assert settings_probe(AUTH_BYPASS=truthy).AUTH_BYPASS is True

    @pytest.mark.parametrize("falsy", ["0", "false", "no", "off", "", "随便"])
    def test_多种假值写法(self, settings_probe, falsy):
        assert settings_probe(AUTH_BYPASS=falsy).AUTH_BYPASS is False

    def test_旁路状态会下发给前端(self, settings_probe):
        assert settings_probe(AUTH_BYPASS="1").frontend_config()["auth_bypass"] is True

    def test_DEBUG不再顺带打开鉴权旁路(self, settings_probe):
        """`DEBUG=1` 是通用调试开关，不该悄悄把鉴权旁路一起打开 —— 生产误开一次就是裸奔。"""
        s = settings_probe(DEBUG="1")
        assert s.AUTH_DEBUG is False
        assert s.AUTH_BYPASS is False

    def test_DEBUG开着时显式关掉AUTH_DEBUG要生效(self, settings_probe):
        assert settings_probe(DEBUG="1", AUTH_DEBUG="0").AUTH_DEBUG is False

    def test_DEBUG开着时仍可单独打开AUTH_DEBUG(self, settings_probe):
        assert settings_probe(DEBUG="1", AUTH_DEBUG="1").AUTH_DEBUG is True

    def test_源码里不再把DEBUG或进AUTH_DEBUG(self):
        """行为断言之外再钉一道：别人把 `or _bool("DEBUG")` 加回来时立刻可见。"""
        src = SETTINGS_PY.read_text(encoding="utf-8")
        line = next(ln for ln in src.splitlines() if ln.startswith("AUTH_DEBUG"))
        assert '_bool("DEBUG"' not in line, f"AUTH_DEBUG 又被 DEBUG 顺带打开了：{line}"


# ══════════════════════════════════════════════════════════════
# 2. 简历关键词表：两份合并成一份，取并集
# ══════════════════════════════════════════════════════════════
#
# 曾经有两份互相不知道对方存在的词表：
#   biz_store._SKILL_KW（8 条）与 diagnose._rule_parse 内联 patterns（10 条）
# 合并到 agent/skill_keywords.SKILL_KEYWORDS 后，两条诊断路径必须完全同结果。


def _biz(text):
    return {s["skill_name"] for s in biz_store._parse_resume_skills(text)}


def _diag(text):
    return {s["skill_name"] for s in diagnose._rule_parse(text)}


SHARED_CASES = [
    ("做过三年直播带货，负责话术打磨", "直播"),
    ("负责千川广告投放，ROI 稳定在 3 以上", "投放"),
    ("熟悉 SQL，独立搭过经营看板与指标体系", "数据"),
    ("写短视频脚本，做内容选题", "内容"),
    ("私域运营，管理十几个社群", "运营"),
    ("Python / Java 后端开发五年", "开发"),
    ("三甲医院护理岗，做过康复陪护", "护理"),
    ("会计岗，负责财务报表与审计对接", "财务"),
]

# 合并前只有 diagnose 那份有的词 —— 合并后 biz_store 侧也必须命中
MERGED_IN_CASES = [
    ("负责汽车涂装与钣金作业", "汽车维修"),
    ("精通 C# 与 .NET Framework", "开发"),
    ("在海事局做过航标维护与航海保障", "航标作业"),
]


@pytest.mark.usefixtures("no_kg_recall")
class TestKeywordTables:
    def test_全仓只有一份词表(self):
        assert not hasattr(biz_store, "_SKILL_KW"), "biz_store 的那份拷贝应当已经删掉"
        assert len(skill_keywords.SKILL_KEYWORDS) == 10

    @pytest.mark.parametrize("text,label", SHARED_CASES)
    def test_共有词两条路径都命中(self, text, label):
        assert label in _biz(text)
        assert label in _diag(text)

    @pytest.mark.parametrize("text,label", MERGED_IN_CASES)
    def test_并集词两条路径都命中(self, text, label):
        assert label in _biz(text), "合并前 biz_store 这条是命不中的"
        assert label in _diag(text)

    @pytest.mark.parametrize("text,_label", SHARED_CASES + MERGED_IN_CASES)
    def test_两条路径对同一文本给出同一结果(self, text, _label):
        assert _biz(text) == _diag(text)

    def test_没命中任何词时给基础分而不是空结果(self):
        assert _biz("今天天气不错") == {skill_keywords.FALLBACK_SKILL_NAME}
        assert _diag("今天天气不错") == {skill_keywords.FALLBACK_SKILL_NAME}

    @pytest.mark.parametrize("text", ["", None, "   "])
    def test_空输入也给保底条目(self, text):
        assert _biz(text) == {skill_keywords.FALLBACK_SKILL_NAME}
        assert _diag(text) == {skill_keywords.FALLBACK_SKILL_NAME}

    def test_大小写不敏感(self):
        assert "开发" in _biz("PYTHON 工程师")
        assert "开发" in _diag("PYTHON 工程师")

    def test_命中多个词时全部记下来(self):
        text = "直播运营，兼做数据分析"
        assert {"直播", "运营", "数据"} <= _biz(text)
        assert _biz(text) == _diag(text)

    def test_档位与分数口径(self):
        hit = biz_store._parse_resume_skills("直播带货")[0]
        assert (hit["level"], hit["score"]) == (skill_keywords.HIT_LEVEL, skill_keywords.HIT_SCORE)
        miss = biz_store._parse_resume_skills("今天天气不错")[0]
        assert (miss["level"], miss["score"]) == (
            skill_keywords.FALLBACK_LEVEL, skill_keywords.FALLBACK_SCORE
        )

    def test_两条路径的证据文案各自保留原措辞(self):
        """前端已经在展示这段文字，合并实现不该顺手改掉。"""
        assert biz_store._parse_resume_skills("直播带货")[0]["evidence"].startswith("简历命中关键词规则：")
        assert diagnose._rule_parse("直播带货")[0]["evidence"].startswith("规则命中：")

    def test_技能库召回优先于关键词(self, monkeypatch):
        monkeypatch.setattr(
            skill_keywords, "kg_recall",
            lambda text, limit=12: [{"skill_name": "配料准备", "level": 2, "score": 40, "evidence": "库"}],
        )
        assert _biz("直播带货") == {"配料准备"}, "命中库内真实技能名时不该再退化到互联网口径词表"
        assert _diag("直播带货") == {"配料准备"}

    def test_库不可达时静默退化到关键词(self):
        """conftest 把 DATABASE_URL 指到了死地址 —— 这里走的就是真实的降级路径。"""
        assert skill_keywords.kg_recall("直播带货") == []
        assert "直播" in {s["skill_name"] for s in skill_keywords.rule_parse_skills("直播带货")}


# ══════════════════════════════════════════════════════════════
# 3. get_node 的可见性 scope
# ══════════════════════════════════════════════════════════════
#
# 过滤发生在 SQL 里，所以断言分两层：
#   a) 谓词确实拼进了 SQL（用 conn= 注入假连接，不连库）
#   b) 拼出来的谓词与 config.py 里的可见性函数同源（不是手写的字符串）

ROW = {
    "id": "CN:occupation:mohrss:X",
    "region": "CN",
    "type": "occupation",
    "name": "混凝土工",
    "source_id": "X",
    "attrs": "{}",
    "status": "published",
}


_DEFAULT = object()


class RecordingConn:
    def __init__(self, row=_DEFAULT):
        self.row = dict(ROW) if row is _DEFAULT else row
        self.sql = ""
        self.params = None

    def execute(self, sql, params=None):
        self.sql = str(sql)
        self.params = params
        return self

    def fetchone(self):
        return self.row


class TestGetNodeScope:
    def test_签名带scope且默认管理台口径(self):
        p = inspect.signature(query.get_node).parameters
        assert "scope" in p
        assert p["scope"].default == "manage"
        assert p["scope"].kind is inspect.Parameter.KEYWORD_ONLY, "scope 必须是关键字参数，别挤掉既有位置参数"

    def test_原有单参调用保持可用(self):
        c = RecordingConn()
        node = query.get_node("CN:occupation:mohrss:X", conn=c)
        assert node["id"] == ROW["id"] and node["type"] == "occupation"

    def test_查不到时返回None(self):
        assert query.get_node("不存在", conn=RecordingConn(row=None)) is None

    def test_按主键查(self):
        c = RecordingConn()
        query.get_node("CN:occupation:mohrss:X", conn=c)
        assert c.params == ("CN:occupation:mohrss:X",)

    def test_前台口径只放行已发布(self):
        from backend.kg.pg_store.config import node_published

        c = RecordingConn()
        query.get_node("x", scope="public", conn=c)
        assert node_published() in c.sql, "public 必须拼 node_published()，不能手写状态串"

    def test_管理台口径只挡归档(self):
        from backend.kg.pg_store.config import node_not_archived

        for scope in ("manage", "", None, "MANAGE", "乱写"):
            c = RecordingConn()
            query.get_node("x", scope=scope, conn=c)
            assert node_not_archived() in c.sql, f"scope={scope!r} 应回落到管理台口径"

    def test_any口径不过滤状态但仍要定死取哪一行(self):
        """`any` 的含义是「不按 status 过滤」，不是「什么都不加」。

        草稿态之后同一 id 在库里最多有两行（线上行 + 草稿行），裸 `WHERE id=%s`
        配 `fetchone()` 会**随机**拿到其中一行 —— `any` 的用途正是「刚写完读回来」，
        随机取行等于有一半概率读到另一个版本。所以状态谓词一个都不能有，
        但必须带 `prefer_draft`（草稿优先）把取哪一行定下来。
        """
        from backend.kg.pg_store.config import prefer_draft

        c = RecordingConn()
        query.get_node("x", scope="any", conn=c)
        assert "status" not in c.sql, "any 不该有任何状态谓词"
        assert prefer_draft() in c.sql, "两行存储下必须定死取哪一行"

    def test_scope取值大小写与空白不敏感(self):
        from backend.kg.pg_store.config import node_published

        c = RecordingConn()
        query.get_node("x", scope="  PUBLIC  ", conn=c)
        assert node_published() in c.sql

    def test_归档节点在前台口径下被SQL挡掉(self):
        """假连接不会执行 WHERE，所以这里断言的是「谓词在」；真过滤由 PG 完成。"""
        c = RecordingConn(row={**ROW, "status": "archived"})
        query.get_node("x", scope="public", conn=c)
        assert "'published'" in c.sql and "COALESCE" in c.sql


class TestGetNodeCallSites:
    """学员/诊断侧按 id 取点必须走 public —— 否则猜到 id 就能读到未发布的节点。"""

    @pytest.mark.parametrize(
        "src",
        [
            REPO_ROOT / "backend" / "kg" / "pg_store" / "biz_store.py",
            REPO_ROOT / "backend" / "agent" / "tools_kg.py",
        ],
    )
    def test_学员侧取点都带scope(self, src):
        import re

        text = src.read_text(encoding="utf-8")
        bare = [
            m.group(0)
            for m in re.finditer(r"get_node\((?![^)]*scope=)[^)]*\)", text)
            if "def get_node" not in m.group(0)
        ]
        assert not bare, f"{src.name} 里还有不带 scope 的 get_node 调用：{bare[:3]}"


# ══════════════════════════════════════════════════════════════
# 4. 重构不该改到的公共面
# ══════════════════════════════════════════════════════════════


class TestPublicSurface:
    """连接池 / DDL 移出热路径这类改动，不该改变这些模块的对外形状。"""

    def test_连接入口仍是connect并且可作上下文管理器(self):
        from backend.kg.pg_store import client

        assert callable(client.connect)
        assert hasattr(client, "ensure_schema")

    def test_建表语句是幂等的(self):
        """没有 migration 框架，DDL 靠幂等语句演进 —— 移出热路径后仍要能重复执行。"""
        from backend.kg.pg_store.biz_ddl import BIZ_SCHEMA_SQL
        from backend.kg.pg_store.client import SCHEMA_SQL

        for sql in (SCHEMA_SQL, BIZ_SCHEMA_SQL):
            for stmt in sql.split(";"):
                s = stmt.strip().upper()
                if s.startswith("CREATE TABLE"):
                    assert "IF NOT EXISTS" in s, stmt[:80]
                if s.startswith("CREATE INDEX") or s.startswith("CREATE UNIQUE INDEX"):
                    assert "IF NOT EXISTS" in s, stmt[:80]
                if s.startswith("ALTER TABLE") and " ADD COLUMN" in s:
                    assert "IF NOT EXISTS" in s, stmt[:80]

    def test_数据库连接串只从环境变量读不回落仓库根(self):
        from backend.kg.pg_store import config

        assert config.DATABASE_URL == os.environ["DATABASE_URL"]

    def test_backend不import仓库根模块(self):
        """第一原则：backend/ 只拷这一个目录就要能起来。"""
        leaked = {m for m in sys.modules if m.split(".")[0] in ("crawlers", "pipelines")}
        assert not leaked, f"单测只 import 了 backend.*，却带进了 {leaked}"

    def test_运行时用到的三方包都登记在部署清单里(self):
        """本地装了但没登记的包，在干净镜像里会走进 try/except 降级分支，功能悄悄失效。"""
        req = (REPO_ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8").lower()
        for pkg in ("fastapi", "psycopg", "python-dotenv", "httpx", "pyyaml",
                    "langchain-openai", "langgraph", "pypdf", "python-docx", "python-multipart"):
            assert pkg in req, f"{pkg} 没写进 backend/requirements.txt"

    def test_pytest不在部署清单里(self):
        """backend/requirements.txt 是镜像清单，只能有运行时依赖。"""
        req = (REPO_ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8").lower()
        assert "pytest" not in req
        dev = (REPO_ROOT / "requirements-dev.txt").read_text(encoding="utf-8").lower()
        assert "pytest" in dev
