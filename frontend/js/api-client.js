/**
 * API 客户端：UC MAC Token 鉴权（对齐 bcs-ai-agent）。
 * 开发旁路：/v1/config.auth_bypass 时用 X-Test-Uid / X-Test-Uname。
 */
(function (global) {
  "use strict";

  /* sdp-app-id 由**前端**决定并透传给服务端（同一后端可服务多个前端应用，
     各带各的 app-id）。服务端不再写死、也不再用 .env 兜底。
     需要覆盖时在页面里设 window.__SDP_APP_ID__ 即可。 */
  var DEFAULT_SDP_APP_ID = "e176f53e-01ec-4385-894e-8b35bcfec5fa";

  const LS_UID = "voced_user_id";
  const LS_UNAME = "voced_user_name";

  let _uc = null;
  let _config = null;
  let _userInfo = null;
  let _ready = null;

  function headerVal(s) {
    const v = String(s == null ? "" : s);
    return /^[\x00-\x7F]*$/.test(v) ? v : encodeURIComponent(v);
  }

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = src;
      s.onload = resolve;
      s.onerror = function () {
        reject(new Error("加载脚本失败: " + src));
      };
      document.head.appendChild(s);
    });
  }

  function appId() {
    return String(global.__SDP_APP_ID__ || DEFAULT_SDP_APP_ID || "").trim();
  }

  async function loadConfig() {
    if (_config) return _config;
    const r = await fetch("/v1/config");
    if (!r.ok) throw new Error("获取 /v1/config 失败: " + r.status);
    _config = await r.json();
    return _config;
  }

  /**
   * API 基地址。跨域部署时（前端与接口不同源）在页面里设置：
   *   window.__VOCED_API_BASE__ = "http://192.168.1.50:8088";
   * 不设置则回落当前页面 origin，同源行为与之前完全一致。
   */
  function apiBase() {
    var b = String(window.__VOCED_API_BASE__ || "").replace(/\/+$/, "");
    return b || window.location.origin;
  }
  /** 相对路径补成绝对地址；已是绝对地址则原样返回。 */
  function absUrl(u) {
    return /^https?:\/\//i.test(u) ? u : apiBase() + u;
  }

  async function getAuthHeader(urlPath, method) {
    if (_config && _config.auth_bypass) return null;
    if (!_uc) throw new Error("UC SDK 未初始化");
    const fullUrl = absUrl(urlPath);
    return await _uc.getAuthHeaderAsync({ url: fullUrl, method: method || "GET" });
  }

  async function initAuth(opts) {
    opts = opts || {};
    if (_ready) return _ready;
    _ready = (async function () {
      const cfg = await loadConfig();
      if (cfg.auth_bypass) {
        _userInfo = {
          user_id: localStorage.getItem(LS_UID) || "0",
          real_name: localStorage.getItem(LS_UNAME) || "dev",
        };
        console.info("[auth] AUTH_BYPASS=1，跳过 UC 登录（见 .env）");
        return { bypass: true, user: _userInfo, config: cfg };
      }
      if (!cfg.uc_sdk_url) {
        throw new Error("UC 配置缺少 uc_sdk_url，请在 .env 设置 UC_SDK_URL");
      }
      if (!appId()) {
        throw new Error("缺少 sdp-app-id：请在页面设置 window.__SDP_APP_ID__");
      }
      await loadScript(cfg.uc_sdk_url);
      var UC = SDP.UC.UC;
      _uc = new UC({
        env: cfg.uc_env || "preproduction",
        sdpAppId: appId(),
        autoRefresh: true,
        storageExpire: 2592000,
        minEffectiveTime: 172800,
      });
      var urlParams = new URLSearchParams(window.location.search);
      var uckey = urlParams.get("uckey");
      if (uckey) {
        await _uc.loginByUCKey({ uckey: uckey });
        var cleanUrl = new URL(window.location.href);
        cleanUrl.searchParams.delete("uckey");
        window.history.replaceState({}, "", cleanUrl);
      }
      var isLoggedIn = await _uc.isLogin();
      var e2e =
        urlParams.get("e2e") === "1" || localStorage.getItem("voced_e2e_bypass") === "1";
      if (!isLoggedIn && !e2e) {
        var redirectUri = encodeURIComponent(
          window.location.origin + window.location.pathname
        );
        var loginUrl =
          "https://" +
          cfg.uc_component_host +
          "/?sdp-app-id=" +
          appId() +
          "#/login?sso=false&re_login=true&send_uckey=true&redirect_uri=" +
          redirectUri;
        window.location.href = loginUrl;
        return { redirecting: true };
      }
      if (!isLoggedIn && e2e) {
        _userInfo = { user_id: "0", real_name: "E2E" };
        return { bypass: true, user: _userInfo, config: cfg };
      }
      try {
        var account = _uc.getCurrentAccount();
        _userInfo = await account.getAccountInfo();
      } catch (e) {
        console.warn("getAccountInfo 失败", e);
        _userInfo = { user_id: "?", real_name: "用户" };
      }
      return { user: _userInfo, config: cfg, uc: _uc };
    })();
    return _ready;
  }

  function getUser() {
    if (_userInfo) {
      return {
        id: String(_userInfo.user_id || _userInfo.userId || "0"),
        name:
          _userInfo.real_name ||
          _userInfo.user_name ||
          _userInfo.nick_name ||
          String(_userInfo.user_id || "user"),
      };
    }
    return {
      id: localStorage.getItem(LS_UID) || "0",
      name: localStorage.getItem(LS_UNAME) || "dev",
    };
  }

  function setUser(id, name) {
    if (id != null) localStorage.setItem(LS_UID, String(id).trim() || "0");
    if (name != null) localStorage.setItem(LS_UNAME, String(name).trim() || "dev");
    if (_config && _config.auth_bypass) {
      _userInfo = { user_id: id, real_name: name };
    }
  }

  /** 构造带鉴权的请求头。SSE 流式请求也要用它——EventSource 不支持自定义
   *  header，而 MAC 签名必须放 Authorization，所以流式只能走 fetch + 这套头。 */
  async function buildHeaders(url, method, extra) {
    const h = Object.assign({}, extra || {});
    const m = (method || "GET").toUpperCase();
    const cfg = _config || (await loadConfig().catch(function () { return {}; }));
    if (cfg.auth_bypass) {
      const u = getUser();
      h["X-Test-Uid"] = u.id;
      h["X-Test-Uname"] = headerVal(u.name);
    } else {
      const auth = await getAuthHeader(url, m);
      if (auth) h["Authorization"] = auth;
      const u = getUser();
      if (u.name) h["X-User-Name"] = headerVal(u.name);
      const id = appId();
      if (id) h["sdp-app-id"] = id;
    }
    return h;
  }

  async function apiFetch(url, opts) {
    opts = opts || {};
    const method = (opts.method || "GET").toUpperCase();
    const h = Object.assign({}, opts.headers || {});
    const cfg = _config || (await loadConfig().catch(function () { return {}; }));

    if (cfg.auth_bypass) {
      const u = getUser();
      h["X-Test-Uid"] = u.id;
      h["X-Test-Uname"] = headerVal(u.name);
    } else {
      // MAC 签名必须与真实请求 path+query 一致；去掉 ? 会导致 UC/INVALID_MAC
      // url 形如 /v1/kg/nodes?type=industry&page=1
      const auth = await getAuthHeader(url, method);
      if (auth) h["Authorization"] = auth;
      const u = getUser();
      if (u.name) h["X-User-Name"] = headerVal(u.name);
      // sdp-app-id 一律由前端透传，服务端不再兜底
      const id = appId();
      if (id) h["sdp-app-id"] = id;
    }

    let body = opts.body;
    if (body && typeof body === "object" && !(body instanceof FormData)) {
      h["Content-Type"] = h["Content-Type"] || "application/json";
      body = JSON.stringify(body);
    }
    const res = await fetch(absUrl(url), Object.assign({}, opts, { headers: h, body: body, method: method }));
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch (_) {
      data = text;
    }
    if (!res.ok) {
      let msg =
        (data && data.detail && (data.detail.message || data.detail)) ||
        (data && data.message) ||
        (data && data.error) ||
        "HTTP " + res.status;
      if (typeof msg === "object") msg = JSON.stringify(msg);
      const err = new Error(msg);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  global.VocedApi = {
    initAuth,
    loadConfig,
    getAuthHeader,
    getUser,
    setUser,
    apiFetch,
    buildHeaders,
    appId,
    absUrl,
    headerVal,
    LS_UID,
    LS_UNAME,
  };
})(window);
