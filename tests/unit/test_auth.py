"""backend/api/auth.py —— 免鉴权路径表 + 开发旁路的取用户逻辑。

`AUTH_BYPASS` / `AUTH_DEBUG` 在 auth.py 是**模块级常量**（import 期从 settings 固化），
所以这里直接 monkeypatch 模块常量来枚举组合 —— 测的是「哪些组合会放行」这个行为，
不依赖本机 .env 到底怎么写。重构把开关搬到别处时，这批断言应当照旧成立。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend.api import auth
from backend.api.auth import AuthUser, _should_skip, require_auth_user, require_user


def call(**kw):
    """跑一次 require_auth_user。

    它是 async 但从不真正 await（纯同步逻辑），所以直接 `coro.send(None)` 推到底即可 ——
    既免掉 pytest-asyncio 依赖，又能留在**当前 Context** 里：`asyncio.run` 会把协程包成
    Task 复制一份上下文，函数内 `_current_user.set()` 的效果在外面就看不见了。
    """
    params = {
        "request": None,
        "authorization": None,
        "x_user_name": None,
        "x_test_uid": None,
        "x_test_uname": None,
    }
    params.update(kw)
    coro = require_auth_user(**params)
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    finally:
        coro.close()
    raise AssertionError("require_auth_user 挂起了，说明它开始真的 await —— 请改用事件循环驱动")


@pytest.fixture(autouse=True)
def _clean_context():
    token = auth._current_user.set(None)
    yield
    auth._current_user.reset(token)


class TestSkipList:
    @pytest.mark.parametrize(
        "path",
        ["/", "/health", "/openapi.json", "/docs", "/redoc", "/api-guide"],
    )
    def test_文档与健康检查免鉴权(self, path):
        assert _should_skip(path) is True

    @pytest.mark.parametrize(
        "path",
        ["/docs/oauth2-redirect", "/js/api-client.js", "/admin/index.html",
         "/schemas/graph_schema.yaml", "/v1/config", "/v1/config/anything", "/dev/x"],
    )
    def test_静态与前端配置免鉴权(self, path):
        assert _should_skip(path) is True

    @pytest.mark.parametrize(
        "path",
        ["/v1/student/occupations", "/v1/admin/changes", "/v1/assessment/sessions", "/v1/graph"],
    )
    def test_业务接口必须鉴权(self, path):
        assert _should_skip(path) is False

    def test_前缀匹配必须是路径段而不是裸前缀(self):
        """/v1/configuration 不是 /v1/config，不能被顺带放行。"""
        assert _should_skip("/v1/configuration") is False
        assert _should_skip("/adminx") is False

    def test_带查询串的免鉴权路径也放行(self):
        assert _should_skip("/v1/config?x=1") is True


class TestRequireAuthUser:
    def test_两个开关都关时四百零一(self, monkeypatch):
        monkeypatch.setattr(auth, "AUTH_BYPASS", False)
        monkeypatch.setattr(auth, "AUTH_DEBUG", False)
        with pytest.raises(HTTPException) as e:
            call()
        assert e.value.status_code == 401

    @pytest.mark.parametrize("bypass,debug", [(True, False), (False, True), (True, True)])
    def test_任一开关打开都放行(self, monkeypatch, bypass, debug):
        monkeypatch.setattr(auth, "AUTH_BYPASS", bypass)
        monkeypatch.setattr(auth, "AUTH_DEBUG", debug)
        monkeypatch.setattr(auth.settings, "DEV_USER_ID", "42")
        monkeypatch.setattr(auth.settings, "DEV_USER_NAME", "dev")
        u = call()
        assert (u.user_id, u.user_name) == ("42", "dev")

    def test_旁路时测试头覆盖默认用户(self, monkeypatch):
        monkeypatch.setattr(auth, "AUTH_BYPASS", True)
        u = call(x_test_uid="u9", x_test_uname="%E5%BC%A0%E4%B8%89")
        assert (u.user_id, u.user_name) == ("u9", "张三")

    def test_已鉴权时优先用上下文里的用户(self, monkeypatch):
        monkeypatch.setattr(auth, "AUTH_BYPASS", True)
        auth._current_user.set(AuthUser(user_id="real", user_name="真名"))
        assert call(x_test_uid="fake").user_id == "real"

    def test_展示名可被请求头覆盖(self):
        auth._current_user.set(AuthUser(user_id="u1", user_name="旧名"))
        u = call(x_user_name="%E6%96%B0%E5%90%8D")
        assert (u.user_id, u.user_name) == ("u1", "新名")

    def test_空展示名头不覆盖(self):
        auth._current_user.set(AuthUser(user_id="u1", user_name="旧名"))
        assert call(x_user_name="   ").user_name == "旧名"

    def test_旁路放行的用户会写进上下文(self, monkeypatch):
        monkeypatch.setattr(auth, "AUTH_BYPASS", True)
        monkeypatch.setattr(auth.settings, "DEV_USER_ID", "7")
        call()
        assert auth.get_auth_user().user_id == "7"


class TestRequireUser:
    def test_没有用户时四百零一并给出可读提示(self):
        with pytest.raises(HTTPException) as e:
            require_user()
        assert e.value.status_code == 401
        assert e.value.detail["error"] == "unauthorized"

    def test_有用户时原样返回(self):
        u = AuthUser(user_id="u1", user_name="n")
        auth._current_user.set(u)
        assert require_user() is u
