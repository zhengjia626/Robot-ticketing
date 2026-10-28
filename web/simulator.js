/* ==========================================================================
   票务系统模拟器控制台
   作用：代替真实票务系统，按 P1–P7 顺序向售票主机后台服务推送 7 类状态消息，
        右侧 iframe 内嵌显示终端，推送一步即可看到显示屏页面实时变化。
   说明：本页只调用后台服务的对外接口，与真实票务系统的接入方式完全一致。
   ========================================================================== */
(function () {
  'use strict';

  var PRICE = { adult: 150, student: 80, senior: 80, zero: 0 };
  var NAME  = { adult: '成人票', student: '学生票', senior: '长者票', zero: '零元票' };
  var TKEY  = { adult: 'K4', student: 'K3', senior: 'K5', zero: 'K5' };
  var SKEY  = { day: 'K6', night: 'K7' };

  // ?tv=P-3 让右侧终端停在指定页面（评审用，不建立长轮询）
  var TV = new URLSearchParams(location.search).get('tv');

  var S = { sid: null, stepIdx: 0, orderNo: null, auto: false };

  var $ = function (id) { return document.getElementById(id); };

  /* ------------------------------ 配置读取 ------------------------------ */
  function cfg() {
    return {
      session: $('cfg-session').value,
      type: $('cfg-type').value,
      qty: Math.max(1, Math.min(5, parseInt($('cfg-qty').value, 10) || 1)),
      cards: Math.max(1, Math.min(5, parseInt($('cfg-cards').value, 10) || 1)),
      price: PRICE[$('cfg-type').value],
      name: NAME[$('cfg-type').value]
    };
  }
  function randId() {
    var s = '5103021990';
    for (var i = 0; i < 8; i++) s += Math.floor(Math.random() * 10);
    return s;
  }

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
    var t = String(d.getHours()).padStart(2, '0') + ':' +
            String(d.getMinutes()).padStart(2, '0') + ':' +
            String(d.getSeconds()).padStart(2, '0');
    var el = document.createElement('div');
    el.className = 'l';
    el.innerHTML = '<span class="t">' + t + '</span>' +
                   '<span class="m ' + (cls || '') + '"></span>';
    el.querySelector('.m').textContent = msg;
    $('log').appendChild(el);
    $('log').scrollTop = $('log').scrollHeight;
  }
  function logResp(label, r) {
    if (!r) { log(label + ' → 无响应', 'bad'); return false; }
    var code = r.code;
    var detail = r.detail ? (' ' + JSON.stringify(r.detail)) : '';
    var msg = label + ' → ' + code + (r.message ? ' ' + r.message : '') +
              (r.state ? ' [' + r.state + ' ' + (r.page || '') + ']' : '') + detail;
    var ok = (code === 0 || code === 208);
    log(msg, ok ? (code === 208 ? 'dup' : 'ok') : 'bad');
    return ok;
  }

  /* ------------------------------ 步骤定义 ------------------------------ */
  var STEPS = [
    { label: 'P1 创建会话 · 设备自检', run: function () {
        return req('POST', '/api/session/create').then(function (r) {
          if (r && r.session_id) {
            S.sid = r.session_id; S.stepIdx = 1; S.orderNo = null;
            log('会话创建成功 ' + S.sid, 'info');
          }
          return r;
        });
      } },

    { label: 'P2 按【场次】键', run: function () {
        var c = cfg();
        return push('key_pressed', { key: SKEY[c.session], session_code: c.session });
      } },

    { label: 'P2 按【票种】键 ×N', run: function () {
        var c = cfg();
        var chain = Promise.resolve({ code: 0 });
        for (var i = 0; i < c.qty; i++) {
          (function (n) {
            chain = chain.then(function () {
              return push('key_pressed', { key: TKEY[c.type], ticket_type: c.type });
            });
          })(i);
        }
        return chain;
      } },

    { label: 'P2 票务回传票价', run: function () {
        var c = cfg();
        return push('ticket_price', {
          session_code: c.session, total_qty: c.qty,
          total_amount: c.price * c.qty, remaining: 11998,
          items: [{ ticket_type: c.type, name: c.name, qty: c.qty,
                    unit_price: c.price, amount: c.price * c.qty }]
        });
      } },

    { label: 'P2→P3 按【确认】锁库存', run: function () {
        return push('confirm_cancel', { key: 'confirm' });
      } },

    { label: 'P3 刷身份证（实名核验）', run: function () {
        var c = cfg();
        var chain = Promise.resolve({ code: 0 });
        for (var i = 0; i < c.cards; i++) {
          (function (n) {
            chain = chain.then(function () {
              return push('id_card_read', {
                id_masked: randId(), age: c.type === 'student' ? 16 : 36,
                eligible_type: c.type, locked: true, card_count: n + 1
              });
            });
          })(i);
        }
        return chain;
      } },

    { label: 'P3 生成最终订单', run: function () {
        var c = cfg();
        S.orderNo = 'TZ' + String(Date.now()).slice(-10);
        return push('ticket_price', {
          order_id: S.orderNo, session_code: c.session,
          total_amount: c.price * c.qty, remaining: 11998 - c.qty,
          items: [{ ticket_type: c.type, name: c.name, qty: c.qty,
                    unit_price: c.price, amount: c.price * c.qty }]
        });
      } },

    { label: 'P4 按【确认】锁票种数量', run: function () {
        return push('confirm_cancel', { key: 'confirm' });
      } },

    { label: 'P5 支付成功', run: function () {
        var c = cfg();
        return push('pay_result', {
          status: 'success', trade_no: '420000' + String(Date.now()).slice(-12),
          amount: c.price * c.qty
        });
      } },

    { label: 'P6 打印指令下发', run: function () {
        return push('print_command', { qty: cfg().qty, action: 'print' });
      } },

    { label: 'P6 机器人视觉确认出票', run: function () {
        var c = cfg();
        return req('POST', '/api/print/verify', {
          session_id: S.sid, arrived: true, actual_qty: c.qty,
          expected_qty: c.qty, paper_empty: false, paper_remaining: 480,
          vision_confidence: 0.93
        });
      } },

    { label: 'P7 游客取票 · 归档复位', run: function () {
        return req('POST', '/api/session/ticket-taken', null, { session_id: S.sid });
      } }
  ];

  /* ------------------------------ 状态刷新 ------------------------------ */
  // 状态 → 步骤索引（页面刷新后据此自动接回进度）
  var STEP_OF_STATE = { S1: 1, S2: 2, S3: 3, S4: 4, S5: 5, S6: 6,
                        S7: 7, S8: 8, S9: 9, S10: 10, S11: 12 };
  var synced = false;   // 仅首次刷新按后端回填，之后不覆盖用户正在进行的操作

  // 会话号一律以后端为准：刷新 / 重开页面后可自动接回，不必重新建会话
  function syncFromState(r) {
    var s = r.session;
    S.sid = s ? s.session_id : null;
    if (!synced) {
      synced = true;
      S.stepIdx = s ? (STEP_OF_STATE[r.state] != null ? STEP_OF_STATE[r.state] : 0) : 0;
      if (s) {
        log('已接回会话 ' + S.sid + ' [' + (r.state || '') + ' ' + (r.page || '') + ']', 'info');
      }
      renderSteps();
    } else if (!s && S.stepIdx > 0 && !S.auto) {
      // 会话已归档或被重置，进度归零
      S.stepIdx = 0; S.orderNo = null; renderSteps();
    }
  }

  function refresh() {
    return req('GET', '/api/session/state').then(function (r) {
      syncFromState(r);
      var s = r.session;
      $('s-sid').textContent = s ? s.session_id.slice(-10) : '—';
      $('s-state').textContent = (r.state || 'S0') + ' / ' + (r.page || 'P-0');
      $('s-order').textContent = s
        ? ((s.session_code === 'day' ? '日场' : s.session_code === 'night' ? '夜场' : '—') +
           ' · ' + s.total_qty + ' 张 · ' +
           (s.total_amount == null ? '—' : s.total_amount + ' 元')) : '—';
      $('s-amt').textContent = s ? (s.order_id || '—') : '—';
      return req('GET', '/api/alarm/active');
    }).then(function (a) {
      var el = $('s-alarm');
      var act = a && a.data;
      el.textContent = act ? (act.level + ' ' + act.code + ' ' + act.message) : '无';
      el.className = act ? (act.level === 'L1' ? 'err' : 'warn') : 'on';
    }).catch(function () {
      $('net').classList.add('off');
    });
  }

  /* ------------------------------ 步骤渲染 ------------------------------ */
  function renderSteps() {
    var box = $('steps');
    box.innerHTML = '';
    STEPS.forEach(function (st, i) {
      var b = document.createElement('button');
      b.className = 'step' + (i < S.stepIdx ? ' done' : (i === S.stepIdx ? ' next' : ''));
      b.disabled = (i !== S.stepIdx);
      b.innerHTML = '<span class="n">' + (i < S.stepIdx ? '✓' : (i + 1)) + '</span>' +
                    '<span>' + st.label + '</span>';
      b.onclick = function () { runStep(i); };
      box.appendChild(b);
    });
  }

  function runStep(i) {
    var st = STEPS[i];
    if (!st) return Promise.resolve(false);
    return st.run().then(function (r) {
      var ok = logResp(st.label, r);
      if (ok && i === S.stepIdx) S.stepIdx = i + 1;
      renderSteps();
      return refresh().then(function () { return ok; });
    }).catch(function (e) {
      log(st.label + ' → 请求异常 ' + e, 'bad');
      return false;
    });
  }

  /* 确保推进到第 idx 步之前（用于异常注入） */
  function ensure(idx) {
    var chain = Promise.resolve(true);
    function next() {
      if (S.stepIdx >= idx) return Promise.resolve(true);
      return runStep(S.stepIdx).then(function (ok) {
        if (!ok) return false;
        return next();
      });
    }
    return next();
  }

  /* ------------------------------ 异常注入 ------------------------------ */
  var ERRORS = {
    pay_unknown: { need: 9, go: function () {
        return push('pay_result', { status: 'unknown' }).then(function (r) {
          logResp('注入：支付状态不明', r);
          return push('pay_result', { status: 'success', trade_no: '420000DUP' + Date.now() });
        }).then(function (r) {
          logResp('重复推送成功事件（应被拒绝）', r);
        });
      } },
    print_fail: { need: 11, go: function () {
        return runStep(10).then(function () {          // 先下发打印指令
          return req('POST', '/api/print/verify', {
            session_id: S.sid, arrived: false, actual_qty: 0,
            expected_qty: cfg().qty, paper_empty: false, vision_confidence: 0.9
          });
        }).then(function (r) {
          logResp('注入：视觉未识别到票据（第 1 次）', r);
          return req('POST', '/api/print/verify', {
            session_id: S.sid, arrived: false, actual_qty: 0,
            expected_qty: cfg().qty, paper_empty: false, vision_confidence: 0.9
          });
        }).then(function (r) { logResp('注入：仍未出票 → E3', r); });
      } },
    sold_out: { need: 4, go: function () {
        return push('session_sold_out', { session_code: cfg().session, night_available: true })
          .then(function (r) { logResp('注入：场次售罄', r); });
      } },
    echo_fail: { need: 3, go: function () {
        var chain = Promise.resolve();
        for (var i = 1; i <= 3; i++) {
          (function (n) {
            chain = chain.then(function () {
              return push('key_pressed', { key: TKEY[cfg().type],
                                           ticket_type: cfg().type, echo_ok: false });
            }).then(function (r) { logResp('注入：键位回读不一致 第 ' + n + ' 次', r); });
          })(i);
        }
        return chain;
      } },
    paper_empty: { need: 11, go: function () {
        return runStep(10).then(function () {
          return req('POST', '/api/print/verify', {
            session_id: S.sid, arrived: false, actual_qty: 0,
            expected_qty: cfg().qty, paper_empty: true, paper_remaining: 0,
            vision_confidence: 0.95
          });
        }).then(function (r) { logResp('注入：打印机缺纸', r); });
      } },
    short_print: { need: 11, go: function () {
        return runStep(10).then(function () {
          return req('POST', '/api/print/verify', {
            session_id: S.sid, arrived: true, actual_qty: Math.max(1, cfg().qty - 1),
            expected_qty: cfg().qty, paper_empty: false, vision_confidence: 0.9
          });
        }).then(function (r) { logResp('注入：少出票', r); });
      } },
    cancel: { need: 0, go: function () {
        return push('confirm_cancel', { key: 'cancel' })
          .then(function (r) { logResp('按【取消】', r); });
      } },
    alarm_off: { need: 0, go: function () {
        return req('POST', '/api/alarm/cancel', null, { operator: 'simulator' })
          .then(function (r) { logResp('人工取消报警', r); });
      } }
  };

  /* ------------------------------ 交互绑定 ------------------------------ */
  $('btn-auto').onclick = function () {
    if (S.auto) { S.auto = false; $('btn-auto').textContent = '▶ 自动跑完整流程'; return; }
    S.auto = true;
    $('btn-auto').textContent = '■ 停止';
    (function loop() {
      if (!S.auto || S.stepIdx >= STEPS.length) {
        S.auto = false; $('btn-auto').textContent = '▶ 自动跑完整流程';
        log(S.stepIdx >= STEPS.length ? '流程跑完：S11 已完成' : '已停止', 'info');
        return;
      }
      runStep(S.stepIdx).then(function (ok) {
        if (!ok) { S.auto = false; $('btn-auto').textContent = '▶ 自动跑完整流程';
                   log('流程中断，请查看上方错误', 'bad'); return; }
        setTimeout(loop, 900);
      });
    })();
  };

  $('btn-reset').onclick = function () {
    S.auto = false; $('btn-auto').textContent = '▶ 自动跑完整流程';
    var close = S.sid
      ? req('POST', '/api/session/close', null, { session_id: S.sid, reason: '模拟器重置' })
      : Promise.resolve({});
    close.then(function () {
      return req('POST', '/api/alarm/cancel', null, { operator: 'simulator' });
    }).then(function () {
      S.sid = null; S.stepIdx = 0; S.orderNo = null;
      $('log').innerHTML = '';
      log('已重置，可重新开始', 'info');
      renderSteps();
      $('tv').src = '/terminal/?t=' + Date.now();
      return refresh();
    });
  };

  Array.prototype.forEach.call(document.querySelectorAll('[data-err]'), function (b) {
    b.onclick = function () {
      var e = ERRORS[b.dataset.err];
      if (!e) return;
      ensure(e.need).then(function (ok) {
        if (!ok) { log('前置步骤未通过，已中止注入', 'bad'); return; }
        return e.go();
      }).then(function () { renderSteps(); return refresh(); });
    };
  });

  /* ------------------------------ 自定义推送 ------------------------------ */
  var EXAMPLE = {
    key_pressed: { key: 'K4', ticket_type: 'adult', session_code: 'day', echo_ok: true },
    id_card_read: { id_masked: '510302199001011234', age: 36, eligible_type: 'adult',
                    locked: true, card_count: 1, read_status: 'success' },
    ticket_price: { order_id: 'TZ20261027000123', session_code: 'day', total_amount: 300,
                    remaining: 900,
                    items: [{ ticket_type: 'adult', name: '成人票', qty: 2,
                              unit_price: 150, amount: 300 }] },
    pay_result: { status: 'success', trade_no: '4200001234202610270001', amount: 300 },
    confirm_cancel: { key: 'confirm' },
    print_command: { qty: 2, action: 'print' },
    session_sold_out: { session_code: 'day', night_available: true }
  };
  function buildExample() {
    var t = $('cust-type').value;
    var o = Object.assign({ type: t, session_id: S.sid || '<当前会话>' }, EXAMPLE[t]);
    return JSON.stringify(o, null, 2);
  }
  $('cust-type').onchange = function () { $('cust-json').value = buildExample(); };
  $('btn-fill').onclick = function () { $('cust-json').value = buildExample(); };
  $('btn-send').onclick = function () {
    var body;
    try { body = JSON.parse($('cust-json').value); }
    catch (e) { log('JSON 解析失败：' + e.message, 'bad'); return; }

    var needSid = (!body.session_id || body.session_id === '<当前会话>');
    // 前端刚刷新过时 S.sid 可能为空，先向后端确认一次再发
    var ready = (needSid && !S.sid)
      ? refresh().then(function () { return S.sid; })
      : Promise.resolve(S.sid);

    ready.then(function (sid) {
      if (needSid) {
        if (!sid) {
          log('当前没有活跃会话：请先点第 1 步「创建会话」，或在 JSON 里直接写 session_id', 'bad');
          return;
        }
        body.session_id = sid;
      }
      return req('POST', '/api/ticket/push', body).then(function (r) {
        logResp('自定义 ' + body.type, r);
        renderSteps();
        return refresh();
      });
    });
  };

  /* ------------------------------ 启动 ------------------------------ */
  if (TV) $('tv').src = '/terminal/?preview=' + encodeURIComponent(TV);
  $('cust-json').value = buildExample();
  renderSteps();
  refresh();
  setInterval(function () { if (!S.auto) refresh(); }, 3000);
})();
