/* ==========================================================================
   票务系统模块 · 设备控制台
   作用：模拟真实票务系统的物理输入端 —— 键盘 / 身份证扫描器 /
        付款码扫描器 / 打印机，向后台服务（模块二）单向推送 7 类消息。
   本页只调用后台对外接口，与真实票务系统接入方式完全一致。
   ========================================================================== */
(function () {
  'use strict';

  // 键盘定义（与 config/rules.json keymap 一致）
  var KEYS = [
    { key: 'K6', cap: '日场',  cls: 'session', sc: 'day' },
    { key: 'K7', cap: '夜场',  cls: 'session', sc: 'night' },
    { key: 'K3', cap: '学生票', cls: 'ticket', tt: 'student' },
    { key: 'K4', cap: '成人票', cls: 'ticket', tt: 'adult' },
    { key: 'K5', cap: '长者票', cls: 'ticket', tt: 'senior' },
    { key: 'K2', cap: '确认',  cls: 'confirm' },
    { key: 'K1', cap: '取消',  cls: 'cancel' }
  ];
  var PRICE = { adult: 150, student: 80, senior: 80, zero: 0 };

  var S = { sid: null, orderNo: null, auto: false };

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
  function push(type, payload) {
    return req('POST', '/api/ticket/push',
      Object.assign({ type: type, session_id: S.sid }, payload || {}));
  }

  /* ------------------------------ 日志 ------------------------------ */
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
  function logResp(label, r) {
    if (!r) { log(label + ' → 无响应', 'bad'); return false; }
    var detail = r.detail ? (' ' + JSON.stringify(r.detail)) : '';
    var msg = label + ' → ' + r.code + (r.message ? ' ' + r.message : '') +
              (r.state ? ' [' + r.state + ' ' + (r.page || '') + ']' : '') + detail;
    var ok = (r.code === 0 || r.code === 208);
    log(msg, ok ? (r.code === 208 ? 'dup' : 'ok') : 'bad');
    return ok;
  }

  /* ------------------------------ 会话状态刷新 ------------------------------ */
  function refresh() {
    return req('GET', '/api/session/state').then(function (r) {
      var s = r.session;
      S.sid = s ? s.session_id : (S.sid && S.auto ? S.sid : null);
      $('s-sid').textContent = s ? s.session_id.slice(-10) : '—';
      $('s-state').textContent = (r.state || 'S0') + ' / ' + (r.page || 'P-0');
      $('s-order').textContent = s
        ? ((s.session_code === 'day' ? '日场' : s.session_code === 'night' ? '夜场' : '—') +
           ' · ' + s.total_qty + ' 张 · ' +
           (s.total_amount == null ? '—' : s.total_amount + ' 元')) : '—';
      $('s-amt').textContent = s ? (s.order_id || '—') : '—';
      $('s-cards').textContent = s ? (s.id_count || 0) + ' 张' : '—';
      return req('GET', '/api/alarm/active').then(function (a) {
        var el = $('s-alarm'), act = a && a.data;
        el.textContent = act ? (act.level + ' ' + act.code + ' ' + act.message) : '无';
        el.className = act ? (act.level === 'L1' ? 'err' : 'warn') : 'on';
      });
    }).catch(function () {
      $('net').classList.add('off');
    }).then(function () { return refreshPaper(); });
  }
  function refreshPaper() {
    return req('GET', '/api/ops/devices').then(function (r) {
      var d = r.data || {};
      $('paper').textContent = d.paper_remaining == null ? '—' : (d.paper_remaining + ' 张');
      $('prn-on').textContent = d.paper_low ? '缺纸预警' : '正常';
      $('prn-on').className = d.paper_low ? 'warn' : 'on';
    }).catch(function () {});
  }

  /* ------------------------------ 设备动作 ------------------------------ */
  function needSession() {
    if (!S.sid) { log('请先点「① 开始新会话」', 'bad'); return false; }
    return true;
  }
  function randId() {
    var s = '5103021990';
    for (var i = 0; i < 8; i++) s += Math.floor(Math.random() * 10);
    return s;
  }
  function randCode() {
    var s = '';
    for (var i = 0; i < 18; i++) s += Math.floor(Math.random() * 10);
    return s;
  }

  function createSession() {
    return req('POST', '/api/session/create').then(function (r) {
      if (r && r.session_id) { S.sid = r.session_id; S.orderNo = null;
        log('会话创建成功 ' + S.sid, 'info'); }
      else log('创建会话失败：' + (r.message || JSON.stringify(r)), 'bad');
      return refresh();
    });
  }

  function pressKey(k) {
    if (!needSession()) return Promise.resolve();
    var p = { key: k.key };
    if (k.sc) p.session_code = k.sc;
    if (k.tt) p.ticket_type = k.tt;
    if ($('echo-bad') && $('echo-bad').checked) p.echo_ok = false;
    return push('key_pressed', p).then(function (r) {
      logResp('按键 ' + k.key, r);
      // 票务系统已知各票种单价：票种/数量确定后即自动算总价并回传后台，
      // 无需再手动点「回传票价」（该按钮保留用于强制重算/补单）。
      if (['K6', 'K7', 'K3', 'K4', 'K5'].indexOf(k.key) >= 0) {
        return autoPriceIfReady();
      }
      return refresh();
    });
  }

  // 票务系统按已知单价自动汇总总价并回传（模拟真实票务系统锁价 S2→S3）
  function autoPriceIfReady() {
    return req('GET', '/api/session/state').then(function (r) {
      var s = r.session;
      if (s && s.items && s.items.length) {
        return buildPrice(false);   // 无订单号：仅锁价/更新总价
      }
      return refresh();
    });
  }

  function scanId() {
    if (!needSession()) return Promise.resolve();
    var age = parseInt(gv('id-age'), 10) || 36;
    return req('GET', '/api/session/state').then(function (r) {
      var cnt = (r.session && r.session.id_count) || 0;
      var idm = randId();
      $('last-id').textContent = idm.slice(0, 6) + '********' + idm.slice(-2);
      // 临时关闭年龄/购票需求校验：不回传 eligible_type，后端不再据此置位不一致标记
      return push('id_card_read', {
        id_masked: idm, age: age,
        locked: true, card_count: cnt + 1, read_status: 'success'
      }).then(function (rr) {
        logResp('读证 #' + (cnt + 1), rr);
        return req('GET', '/api/session/state');
      });
    }).then(function (r) {
      var s = r.session;
      // 所有证件读齐后，自动生成订单并确认，直达扫码支付环节（S7）
      if (s && s.total_qty > 0 && (s.id_count || 0) >= s.total_qty) {
        log('身份证已读齐（' + s.id_count + '/' + s.total_qty + '），自动进入扫码支付环节', 'info');
        return autoAdvanceToPay();
      }
      return refresh();
    });
  }

  // 身份证读齐后：票务系统生成最终订单并自动确认，直达扫码支付（S7）
  function autoAdvanceToPay() {
    return buildPrice(true)                 // 生成最终订单（带 order_id）→ S6
      .then(function () { return pressKey(KEYS[5]); })  // K2 确认 → S6→S7（扫码支付）
      .then(refresh);
  }

  function scanPay() {
    if (!needSession()) return Promise.resolve();
    var status = gv('pay-status') || 'success';
    var code = randCode();
    $('last-code').textContent = code;
    return req('GET', '/api/session/state').then(function (r) {
      var amt = (r.session && r.session.total_amount) || 0;
      return push('pay_result', {
        status: status, trade_no: '420000' + code.slice(-12), amount: amt
      }).then(function (rr) { logResp('扫码支付=' + status, rr); return refresh(); });
    });
  }

  function printCmd() {
    if (!needSession()) return Promise.resolve();
    return req('GET', '/api/session/state').then(function (r) {
      var qty = (r.session && r.session.total_qty) || 1;
      return push('print_command', { qty: qty, action: 'print' })
        .then(function (rr) { logResp('打印指令×' + qty, rr); return refresh(); });
    });
  }

  function buildPrice(order) {
    if (!needSession()) return Promise.resolve();
    return req('GET', '/api/session/state').then(function (r) {
      var s = r.session; if (!s || !s.items || !s.items.length) {
        log('尚未选票种，无法回传票价', 'bad'); return; }
      var total = 0;
      var items = s.items.map(function (it) {
        total += (it.unit_price || PRICE[it.ticket_type] || 0) * it.qty;
        return { ticket_type: it.ticket_type, name: it.name,
                 qty: it.qty, unit_price: it.unit_price, amount: it.amount };
      });
      var body = {
        session_code: s.session_code, items: items,
        total_qty: s.total_qty, total_amount: total,
        remaining: order ? (11998 - s.total_qty) : 11998
      };
      if (order) { S.orderNo = 'TZ' + String(Date.now()).slice(-10); body.order_id = S.orderNo; }
      return push('ticket_price', body).then(function (rr) {
        logResp(order ? '生成订单 ' + S.orderNo : '锁价', rr); return refresh(); });
    });
  }

  /* ------------------------------ 一键演示 ------------------------------ */
  function sleep(ms) { return new Promise(function (res) { setTimeout(res, ms); }); }
  async function autoFlow() {
    if (S.auto) { S.auto = false; $('btn-auto').textContent = '▶ 一键设备演示（完整购票）'; return; }
    S.auto = true; $('btn-auto').textContent = '■ 停止';
    log('开始一键设备演示', 'info');
    try {
      if (!S.sid) { await createSession(); }
      await sleep(500); await pressKey(KEYS[0]);                 // 日场
      await sleep(500); await pressKey(KEYS[3]); await pressKey(KEYS[3]); // 成人×2
      await sleep(400); await buildPrice(false);                 // 锁价
      await sleep(400); await pressKey(KEYS[5]);                 // 确认→S4
      await sleep(400); await scanId(); await sleep(300); await scanId(); // 刷证×2
      await sleep(400); await buildPrice(true);                  // 生成订单
      await sleep(400); await pressKey(KEYS[5]);                 // 确认→S7
      await sleep(400); await scanPay();                         // 扫码支付
      await sleep(400); await printCmd();                        // 打印
      log('票务系统侧流程已走完；「视觉确认 / 取票」请到 /robot/ 完成', 'info');
    } catch (e) { log('演示中断：' + e, 'bad'); }
    S.auto = false; $('btn-auto').textContent = '▶ 一键设备演示（完整购票）';
  }

  /* ------------------------------ 渲染与绑定 ------------------------------ */
  function renderKbd() {
    var box = $('kbd'); box.innerHTML = '';
    KEYS.forEach(function (k) {
      var b = document.createElement('div');
      b.className = 'key ' + k.cls;
      b.innerHTML = '<span class="k">' + k.key + '</span><span class="c">' + k.cap + '</span>';
      b.onclick = function () { pressKey(k); };
      box.appendChild(b);
    });
  }

  function bind() {
    $('btn-create').onclick = function () { createSession(); };
    $('btn-reset').onclick = function () {
      var close = S.sid ? req('POST', '/api/session/close', null,
        { session_id: S.sid, reason: '票务控制台重置' }) : Promise.resolve({});
      close.then(function () {
        return req('POST', '/api/alarm/cancel', null, { operator: 'ticketing' });
      }).then(function () {
        S.sid = null; S.orderNo = null; $('log').innerHTML = '';
        log('已重置', 'info'); return refresh();
      });
    };
    $('btn-id').onclick = scanId;
    $('btn-id-x3').onclick = async function () {
      for (var i = 0; i < 3; i++) { await scanId(); await sleep(250); }
    };
    $('btn-pay').onclick = scanPay;
    $('btn-print').onclick = printCmd;
    $('btn-refill').onclick = function () {
      req('POST', '/api/ops/device/paper/refill').then(function () {
        log('已补纸', 'info'); refreshPaper();
      });
    };
    $('btn-price').onclick = function () { buildPrice(false); };
    $('btn-order').onclick = function () { buildPrice(true); };
    $('btn-auto').onclick = autoFlow;

    // 键盘快捷键
    document.addEventListener('keydown', function (e) {
      if (e.target && /INPUT|SELECT|TEXTAREA/.test(e.target.tagName)) return;
      var map = { '1': 'K1', '2': 'K2', '3': 'K3', '4': 'K4', '5': 'K5', '6': 'K6', '7': 'K7' };
      var k = null;
      if (e.key === 'Escape') k = 'K1';
      else if (e.key === 'Enter') k = 'K2';
      else if (e.altKey && (e.key === 'x' || e.key === 'X')) k = 'K3';
      else if (e.altKey && (e.key === 'c' || e.key === 'C')) k = 'K4';
      else if (e.altKey && (e.key === 'z' || e.key === 'Z')) k = 'K5';
      else if (e.altKey && (e.key === 'r' || e.key === 'R')) k = 'K6';
      else if (e.altKey && (e.key === 'y' || e.key === 'Y')) k = 'K7';
      else if (map[e.key]) k = map[e.key];
      if (k) { e.preventDefault(); var def = KEYS.filter(function (x) { return x.key === k; })[0]; if (def) pressKey(def); }
    });
  }

  /* ------------------------------ 启动 ------------------------------ */
  renderKbd();
  bind();
  refresh();
  setInterval(function () { if (!S.auto) refresh(); }, 3000);
})();
