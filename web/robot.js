/* ==========================================================================
   机器人通信接口 · 预留调试台
   作用：在「人形机器人」尚未接入时，提供其侧回调的模拟入口
        （心跳 / 告警 / 出票视觉确认 / 取票），
        并实时展示后台 → 机器人 的转发日志（验证预留接口可用）。
   真实机器人接入后，由机器人 SDK 调用相同端点；本页等价于是机器人的替身。
   ========================================================================== */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  function gv(id) { var e = $(id); return e ? e.value : ''; }
  function chk(id) { var e = $(id); return e ? e.checked : false; }

  function req(method, path, body, params) {
    var url = path;
    if (params) url += '?' + Object.keys(params).map(function (k) {
      return k + '=' + encodeURIComponent(params[k]); }).join('&');
    return fetch(url, {
      method: method,
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined
    }).then(function (r) { return r.json().catch(function () { return {}; }); });
  }

  function log(msg, cls) {
    var d = new Date();
    var t = [d.getHours(), d.getMinutes(), d.getSeconds()]
      .map(function (n) { return String(n).padStart(2, '0'); }).join(':');
    var el = document.createElement('div');
    el.className = 'l';
    el.innerHTML = '<span class="t">' + t + '</span><span class="m"></span>';
    el.querySelector('.m').textContent = msg;
    if (cls) el.querySelector('.m').classList.add(cls);
    var box = $('log'); box.appendChild(el); box.scrollTop = box.scrollHeight;
  }

  function activeSid() {
    return req('GET', '/api/session/state').then(function (r) {
      $('s-sid').textContent = r.session ? r.session.session_id.slice(-10) : '—';
      $('s-state').textContent = (r.state || 'S0') + ' / ' + (r.page || 'P-0');
      return r.session ? r.session.session_id : null;
    });
  }

  function loadContract() {
    req('GET', '/api/robot/interface').then(function (r) {
      $('contract').textContent = JSON.stringify(r.data || {}, null, 2);
    }).catch(function () { $('contract').textContent = '接口未就绪'; });
  }

  function pollForward() {
    req('GET', '/api/robot/status').then(function (r) {
      var list = (r.data && r.data.forward_log) || [];
      if (!list.length) { $('fwd').textContent = '（暂无转发，触发任意业务后这里会出现后台→机器人的消息）'; return; }
      $('fwd').innerHTML = list.slice().reverse().map(function (e) {
        var ts = new Date((e.ts || 0) * 1000).toLocaleTimeString();
        var tag = e.ok ? '<span style="color:#3fb950">OK</span>' : '<span style="color:#f85149">FAIL</span>';
        var pl = JSON.stringify(e.payload || {});
        if (pl.length > 120) pl = pl.slice(0, 120) + '…';
        return '<div class="l"><span class="t">' + ts + '</span><span class="m">' +
               tag + ' ' + e.kind + ' ' + pl + (e.error ? ' [' + e.error + ']' : '') + '</span></div>';
      }).join('');
    }).catch(function () {});
  }

  /* ------------------------------ 按钮绑定 ------------------------------ */
  $('btn-hb').onclick = function () {
    req('POST', '/api/robot/heartbeat', { battery: parseInt(gv('bat'), 10) || 88 })
      .then(function (r) { log('心跳 → ' + JSON.stringify(r), 'ok'); });
  };
  $('btn-alert').onclick = function () {
    req('POST', '/api/robot/alert', { event: gv('ev'), level: gv('lv'), detail: { from: 'debug' } })
      .then(function (r) { log('告警 ' + gv('ev') + ' → ' + JSON.stringify(r), 'ok'); });
  };
  $('btn-verify').onclick = function () {
    activeSid().then(function (sid) {
      if (!sid) { log('无活跃会话，请先在票务系统模块发起一笔交易', 'bad'); return; }
      req('POST', '/api/print/verify', {
        session_id: sid, arrived: chk('arrived'),
        actual_qty: parseInt(gv('aqty'), 10) || 0,
        expected_qty: parseInt(gv('aqty'), 10) || 0,
        paper_empty: chk('pempty'), paper_remaining: 480,
        vision_confidence: parseFloat(gv('conf')) || 0.93
      }).then(function (r) { logResp('视觉确认', r); activeSid(); });
    });
  };
  $('btn-take').onclick = function () {
    activeSid().then(function (sid) {
      if (!sid) { log('无活跃会话', 'bad'); return; }
      req('POST', '/api/session/ticket-taken', null, { session_id: sid })
        .then(function (r) { logResp('取票完成', r); activeSid(); });
    });
  };
  function logResp(label, r) {
    if (!r) { log(label + ' → 无响应', 'bad'); return; }
    log(label + ' → ' + r.code + (r.message ? ' ' + r.message : '') +
        (r.state ? ' [' + r.state + ' ' + (r.page || '') + ']' : ''),
        (r.code === 0 || r.code === 208) ? 'ok' : 'bad');
  }

  /* ------------------------------ 启动 ------------------------------ */
  loadContract();
  activeSid();
  pollForward();
  setInterval(function () { activeSid(); pollForward(); }, 2000);
})();
