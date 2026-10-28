/* ==========================================================================
   整体业务流程核查页面（模块二子页）
   - 面板 A：游客 → 人形机器人 信息输入（模拟现场语音上报，POST /api/verify/visitor-message）
   - 面板 B：整体业务流程核查清单 + 结论（GET /api/verify/state）
   - 「机器人代按确认键(K2)」：POST /api/ticket/push {type:key_pressed,key:K2}
     由后台核查服务判定是否放行；不放行则 409 拦截，票务状态不变。
   ========================================================================== */
(function () {
  'use strict';

  var S = { sid: null, strict: false };
  var $ = function (id) { return document.getElementById(id); };
  function gv(id) { var e = $(id); return e ? e.value : ''; }

  /* ------------------------------ HTTP ------------------------------ */
  function req(method, path, body, params) {
    var url = path;
    if (params) url += '?' + Object.keys(params).map(function (k) {
      return k + '=' + encodeURIComponent(params[k]);
    }).join('&');
    return fetch(url, {
      method: method,
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined
    }).then(function (r) { return r.json().catch(function () { return {}; }); });
  }

  /* ------------------------------ 日志 ------------------------------ */
  function ts() {
    var d = new Date();
    return [d.getHours(), d.getMinutes(), d.getSeconds()]
      .map(function (n) { return String(n).padStart(2, '0'); }).join(':');
  }
  function pushLog(box, msg, cls) {
    var el = document.createElement('div');
    el.className = 'l';
    el.innerHTML = '<span class="t"></span><span class="m"></span>';
    el.querySelector('.t').textContent = ts();
    el.querySelector('.m').textContent = msg;
    if (cls) el.querySelector('.m').classList.add(cls);
    box.appendChild(el); box.scrollTop = box.scrollHeight;
  }
  function log(msg, cls) { pushLog($('log'), msg, cls); }
  function logResp(label, r) {
    if (!r) { log(label + ' → 无响应', 'bad'); return false; }
    var msg = label + ' → ' + r.code + (r.message ? ' ' + r.message : '') +
              (r.state ? ' [' + r.state + ' ' + (r.page || '') + ']' : '');
    var ok = (r.code === 0 || r.code === 208);
    log(msg, ok ? (r.code === 208 ? 'dup' : 'ok') : 'bad');
    return ok;
  }

  /* ------------------------------ 游客 → 机器人 ------------------------------ */
  function renderVisitorMessages(messages) {
    var box = $('vlog'); box.innerHTML = '';
    if (!messages || !messages.length) {
      pushLog(box, '暂无游客消息（宽松模式：未上报过游客消息时，核查不强制游客确认）', 'info');
      return;
    }
    messages.forEach(function (m) {
      var tag = m.intent === 'confirm' ? ' [确认]' :
                m.intent === 'cancel' ? ' [取消]' :
                m.intent === 'query' ? ' [咨询]' : '';
      pushLog(box, (m.text || '(空)') + tag, m.intent === 'confirm' ? 'ok' : '');
    });
  }

  function sendVisitorMessage(intent, text) {
    var body = { intent: intent, text: text || '' };
    if (S.sid) body.session_id = S.sid;
    return req('POST', '/api/verify/visitor-message', body).then(function (r) {
      if (r && r.data) {
        renderVisitorMessages(r.data.messages);
        S.strict = !!r.data.strict_mode;
        log('游客消息已上报（intent=' + (intent || 'other') + '）', 'info');
      } else {
        log('上报游客消息失败：' + (r && r.message ? r.message : JSON.stringify(r)), 'bad');
      }
      return refresh();
    });
  }

  /* ------------------------------ 核查清单渲染 ------------------------------ */
  function renderChecklist(data) {
    var box = $('checklist'); box.innerHTML = '';
    if (!data || !data.items) {
      box.innerHTML = '<div class="ci no"><span class="badge">✗</span>' +
        '<span class="lbl">当前无活跃会话</span></div>';
      return;
    }
    data.items.forEach(function (it) {
      var div = document.createElement('div');
      div.className = 'ci ' + (it.ok ? 'ok' : 'no');
      div.innerHTML = '<span class="badge">' + (it.ok ? '✓' : '✗') + '</span>' +
                      '<span class="lbl"></span>';
      div.querySelector('.lbl').textContent = it.label;
      box.appendChild(div);
    });
  }

  function renderVerdict(data) {
    var el = $('verdict');
    if (!data || data.conclusion === 'no_session' || !data.items) {
      el.className = 'verdict warn';
      el.textContent = '当前无活跃会话，请先在「票务系统模块」建立会话并采集需求。';
      return;
    }
    if (data.pass_) {
      el.className = 'verdict pass';
      el.textContent = (data.strict_mode
        ? '✅ 核查通过：游客已确认 + 票价已回传，机器人可代按确认键。'
        : '✅ 核查通过（宽松模式）：本会话尚未上报过游客消息，已兼容旧演示链路。') +
        (data.reason ? ' ' + data.reason : '');
    } else {
      el.className = 'verdict block';
      el.textContent = '⛔ 核查未通过：' + (data.reason || '条件不满足') +
        ' —— 机器人代按确认键将被拦截，票务状态不变。';
    }
  }

  /* ------------------------------ 刷新 ------------------------------ */
  function refresh() {
    return req('GET', '/api/verify/state').then(function (r) {
      var d = r && r.data;
      renderChecklist(d);
      renderVerdict(d);
      S.strict = !!(d && d.strict_mode);
      return req('GET', '/api/session/state');
    }).then(function (r) {
      var s = r && r.session;
      S.sid = s ? s.session_id : null;
      $('s-sid').textContent = s ? s.session_id.slice(-10) : '—';
      $('s-sid').className = s ? 'on' : '';
      $('s-state').textContent = (r && r.state || 'S0') + ' / ' + (r && r.page || 'P-0');
      $('s-order').textContent = s
        ? ((s.session_code === 'day' ? '日场' : s.session_code === 'night' ? '夜场' : '—') +
           ' · ' + s.total_qty + ' 张 · ' +
           (s.total_amount == null ? '金额未回传' : s.total_amount + ' 元')) : '—';
      $('s-mode').textContent = S.strict
        ? '严格（已上报过游客消息，必须确认）'
        : '宽松（未上报过游客消息，兼容旧链路）';
      $('s-mode').className = S.strict ? 'warn' : 'on';
    }).catch(function () {
      $('net').classList.add('off');
      log('刷新失败：无法连接后台服务', 'bad');
    });
  }

  /* ------------------------------ 机器人代按确认键 ------------------------------ */
  function pressK2() {
    if (!S.sid) { log('请先在票务系统模块建立会话', 'bad'); return; }
    var body = { type: 'key_pressed', key: 'K2', session_id: S.sid };
    log('人形机器人操作外接键盘 K2（确认键）→ 提交至票务系统…', 'info');
    return req('POST', '/api/ticket/push', body).then(function (r) {
      var ok = logResp('K2 确认', r);
      if (ok) log('核查放行：确认生效，票务系统已据此推进状态。', 'ok');
      else log('核查拦截：确认未放行，票务系统状态保持不变（门禁由核查服务把关）。', 'bad');
      return refresh();
    });
  }

  /* ------------------------------ 绑定 ------------------------------ */
  function bind() {
    $('btn-send').onclick = function () {
      sendVisitorMessage(gv('intent'), gv('vtext'));
      $('vtext').value = '';
    };
    $('btn-confirm-only').onclick = function () {
      sendVisitorMessage('confirm', '（快捷确认）游客确认购买');
    };
    $('btn-clear').onclick = function () {
      $('vtext').value = '';
      log('本界面仅清空输入框；已上报的游客消息由核查服务持有，会话重置后清空。', 'info');
    };
    $('btn-press-k2').onclick = pressK2;
  }

  /* ------------------------------ 启动 ------------------------------ */
  bind();
  refresh();
  setInterval(refresh, 2500);
})();
