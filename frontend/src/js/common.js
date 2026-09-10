/**
 * HalfCart 前端公共工具 — common.js
 * ==================================
 * goPage / getQueryParam / request / Token管理 / requireAuth
 */
(function () {

  // ══════════════════════════════════════
  // 1. 页面跳转 & URL 参数
  // ══════════════════════════════════════

  window.goPage = function (page, params) {
    params = params || {};
    if ((page === 'login.html' || page === 'register.html') && !params.redirect) {
      params.redirect = encodeURIComponent(location.href);
    }
    var pairs = [];
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      if (v !== null && v !== undefined) pairs.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
    });
    var url = page;
    if (pairs.length > 0) url += '?' + pairs.join('&');
    location.href = url;
  };

  window.getQueryParam = function (key) {
    var m = location.search.match(new RegExp('[?&]' + key + '=([^&]*)'));
    return m ? decodeURIComponent(m[1]) : null;
  };

  // ══════════════════════════════════════
  // 2. Token 管理
  // ══════════════════════════════════════

  var TK = 'halfcart_token', RT = 'halfcart_refresh', UK = 'halfcart_user';
  window.getToken = function () { return localStorage.getItem(TK); };
  window.setToken = function (t) { localStorage.setItem(TK, t); };
  window.getRefreshToken = function () { return localStorage.getItem(RT); };
  window.setRefreshToken = function (t) { localStorage.setItem(RT, t); };
  window.clearToken = function () { localStorage.removeItem(TK); localStorage.removeItem(RT); localStorage.removeItem(UK); };
  window.getUser = function () { try { var u = JSON.parse(localStorage.getItem(UK)); if (u && u.user_id && !u.userId) { u.userId = u.user_id; } return u; } catch (e) { return null; } };
  window.setUser = function (u) { localStorage.setItem(UK, JSON.stringify(u)); };

  // ══════════════════════════════════════
  // 3. 登录检查 — 未登录跳转登录页
  // ══════════════════════════════════════

  window.requireAuth = function () {
    var token = getToken();
    if (!token) {
      // 用 replace 而非 href，避免「未登录页」残留历史栈
      // 同时保留 redirect 参数，让登录后能跳回当前页面
      var redirect = encodeURIComponent(location.href);
      location.replace('login.html?redirect=' + redirect);
      return false;
    }
    return true;
  };

  // ══════════════════════════════════════
  // 4. snake_case → camelCase
  // ══════════════════════════════════════

  function toCamel(s) { return s.replace(/_([a-z])/g, function (_, c) { return c.toUpperCase(); }); }
  window.convertKeys = function (obj) {
    if (Array.isArray(obj)) return obj.map(convertKeys);
    if (obj && typeof obj === 'object' && !(obj instanceof Date)) {
      var r = {};
      Object.keys(obj).forEach(function (k) { r[toCamel(k)] = convertKeys(obj[k]); });
      return r;
    }
    return obj;
  };

  // ══════════════════════════════════════
  // 5. API 基础地址 (支持 data-api-base 属性)
  // ══════════════════════════════════════

  var API_BASE = document.documentElement.dataset.apiBase || 'http://localhost:8000';

  // ══════════════════════════════════════
  // 6. 静默 Token 刷新 (防 401 踢人)
  // ══════════════════════════════════════

  var _refreshing = false;
  var _refreshPromise = null;

  async function trySilentRefresh() {
    var refreshToken = getRefreshToken();
    if (!refreshToken) return false;

    // 防止并发刷新
    if (_refreshing && _refreshPromise) return _refreshPromise;
    _refreshing = true;

    _refreshPromise = (async function () {
      try {
        var resp = await fetch(API_BASE + '/api/v1/auth/refresh', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: refreshToken })
        });
        if (resp.status !== 200) return false;
        var data = await resp.json();
        if (data && data.code === 0 && data.data) {
          if (data.data.accessToken) setToken(data.data.accessToken);
          if (data.data.refreshToken) setRefreshToken(data.data.refreshToken);
          return true;
        }
        return false;
      } catch (e) {
        console.error('[refresh] 静默刷新失败:', e);
        return false;
      } finally {
        _refreshing = false;
        _refreshPromise = null;
      }
    })();

    return _refreshPromise;
  }

  // ══════════════════════════════════════
  // 7. HTTP 请求封装 (含静默刷新 + 防循环踢人)
  // ══════════════════════════════════════

  window.request = async function (url, options) {
    options = options || {};
    var headers = options.headers || {};
    var token = getToken();
    if (token) headers['Authorization'] = 'Bearer ' + token;
    headers['Content-Type'] = headers['Content-Type'] || 'application/json';

    var fullUrl = API_BASE + url;
    console.log('[request]', options.method || 'GET', fullUrl);

    var controller = new AbortController();
    var timeout = (options && options.timeout) || 30000;
    var tid = setTimeout(function () { controller.abort(); }, timeout);
    var resp;
    try {
      resp = await fetch(fullUrl, {
        method: options.method || 'GET',
        headers: headers,
        body: options.body ? JSON.stringify(options.body) : undefined,
        signal: controller.signal
      });
    } catch (e) {
      clearTimeout(tid);
      if (e.name === 'AbortError') {
        console.warn('[request TIMEOUT/ABORT]', fullUrl);
        // timeout/abort 不弹 toast, 由调用方自行处理
      } else {
        console.error('[request FAIL]', fullUrl, e);
        if (typeof vant !== 'undefined') vant.showToast('网络异常，请检查后端服务是否启动');
      }
      throw e;
    }
    clearTimeout(tid);

    // ── 401: 静默刷新 → 重试 → 失败再踢 ──
    if (resp.status === 401) {
      // 排除刷新接口自身 (避免死循环)
      var isRefreshCall = (url.indexOf('/auth/refresh') !== -1);
      if (!isRefreshCall) {
        var ok = await trySilentRefresh();
        if (ok) {
          // 刷新成功 → 用新 Token 重试原请求 (仅重试一次)
          console.log('[request] Token 已刷新，重试请求...');
          var newToken = getToken();
          headers['Authorization'] = 'Bearer ' + newToken;
          try {
            resp = await fetch(fullUrl, {
              method: options.method || 'GET',
              headers: headers,
              body: options.body ? JSON.stringify(options.body) : undefined,
              signal: new AbortController().signal
            });
            // 重试成功后不再走下面的 401 逻辑
            if (resp.status !== 401) {
              // fall through to normal response handling
            } else {
              // 重试后仍是 401 → Refresh Token 也过期了
              clearToken();
              location.replace('login.html');
              throw new Error('登录已过期');
            }
          } catch (e) {
            clearToken();
            location.replace('login.html');
            throw new Error('网络异常');
          }
        } else {
          // 刷新失败 → 真正踢出
          clearToken();
          location.replace('login.html');
          throw new Error('登录已过期');
        }
      } else {
        // 刷新接口本身 401 → Refresh Token 已失效
        clearToken();
        location.replace('login.html');
        throw new Error('登录已过期');
      }
    }

    if (resp.status === 403) {
      if (typeof vant !== 'undefined') vant.showToast('无权限执行此操作');
      throw new Error('无权限');
    }

    var data;
    try { data = await resp.json(); } catch (e) { data = null; }

    if (resp.status >= 500) {
      if (typeof vant !== 'undefined') vant.showToast('服务器繁忙，请稍后重试');
      throw new Error('服务器错误');
    }
    if (resp.status >= 400) {
      var errMsg = (data && data.message) || '请求参数错误';
      if (typeof vant !== 'undefined') vant.showToast(errMsg);
      throw new Error(errMsg);
    }

    if (data && data.data) data.data = convertKeys(data.data);
    return data;
  };

  // ══════════════════════════════════════
  // 7. 注入 Vue 全局属性
  // ══════════════════════════════════════

  if (typeof Vue !== 'undefined' && Vue.createApp) {
    var _origCreateApp = Vue.createApp;
    Vue.createApp = function () {
      var app = _origCreateApp.apply(this, arguments);
      app.config.globalProperties.goPage = window.goPage;
      app.config.globalProperties.request = window.request;
      app.config.globalProperties.getQueryParam = window.getQueryParam;
      app.config.globalProperties.getUser = window.getUser;
      return app;
    };
  }

  // ══════════════════════════════════════
  // 8. 流水类型 → 图标映射 (PRD 4.10 资金流水)
  // ══════════════════════════════════════

  window.getFlowIcon = function(type) {
    var map = {
      1: {icon:'💳', cls:'freeze'},   // 冻结
      2: {icon:'↩️', cls:'refund'},  // 退款
      3: {icon:'💰', cls:'income'},  // 订单收入
      4: {icon:'💸', cls:'income'},  // 核销入账
      5: {icon:'⚙', cls:'outcome'}, // 提现支出
      6: {icon:'🏦', cls:'outcome'}, // 其他支出
      7: {icon:'✅', cls:'income'},  // 完成入账
      8: {icon:'↩️', cls:'refund'},  // 退款到账
      9: {icon:'⚖', cls:'refund'}    // 纠纷退款
    };
    return map[type] || {icon:'📋', cls:'income'};
  };

  // ══════════════════════════════════════
  // 9. 高德地图配置 (统一引用)
  // ══════════════════════════════════════

  window.AMAP_CONFIG = {
    webKey: 'fcbf19bd914f78c16ec6fe4c15bd571a',
    jsCode: 'f140d721cc9f7a1b9bd7cac1ffb57809',
  };

})();
