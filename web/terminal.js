/* ==========================================================================
   信息显示终端 · 运行时
   原则（方案 9.5 / 11.1 节）：页面内容由后台服务统一推送，终端只渲染、不做业务判断。
   交互：首帧 GET /api/state → 长轮询 GET /api/state/long?since=n → 心跳 POST /api/heartbeat
   ========================================================================== */
(function () {
  'use strict';

  var DESIGN_W = 1228, DESIGN_H = 800;
  var HEARTBEAT_MS = 10000;

  var qs = new URLSearchParams(location.search);
  var TERMINAL_ID = qs.get('tid') || 'matepad-01';
  var PREVIEW = qs.get('preview');      // 评审用：直接预览某一页面
  var DEBUG = qs.get('debug') === '1';  // 评审用：底部页面切换条

  var el = {
    frame: document.getElementById('frame'),
    stepbar: document.getElementById('stepbar'),
    page: document.getElementById('page'),
    statusbar: document.getElementById('statusbar'),
    mask: document.getElementById('mask'),
    devbar: document.getElementById('devbar')
  };

  var state = {
    version: 0,
    snap: null,
    page: null,
    pageEnterAt: 0,
    online: false,
    fails: 0
  };

  /* ------------------------------ 适配缩放 ------------------------------ */
  function fit() {
    var s = Math.min(window.innerWidth / DESIGN_W, window.innerHeight / DESIGN_H);
    el.frame.style.transform = 'translate(-50%,-50%) scale(' + s + ')';
  }
  window.addEventListener('resize', fit);

  /* ------------------------------ 工具 ------------------------------ */
  function esc(v) {
    if (v === null || v === undefined) return '';
    return String(v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function money(n) {
    if (n === null || n === undefined) return '--';
    var v = Number(n);
    return (Math.round(v * 100) / 100).toString();
  }
  function typeSummary(items, cn) {
    if (!items || !items.length) return '';
    var d = '零一二三四五六七八九';
    function num(n) {
      n = Number(n) || 0;
      if (n === 2) return '两';
      if (n < 10) return d[n] || String(n);
      if (n < 20) return '十' + (n % 10 ? d[n % 10] : '');
      return d[Math.floor(n / 10)] + '十' + (n % 10 ? d[n % 10] : '');
    }
    return items.map(function (i) {
      var q = Number(i.qty || 0);
      return esc(i.name || i.ticket_type || '') + (cn ? num(q) : q) + '张';
    }).join('、');
  }
  function api(path, opts) {
    return fetch(path, opts || {});
  }

  /* ------------------------------ 步骤条 ------------------------------ */
  function renderStepbar(d, page) {
    var steps = (d && d.steps) || [];
    if (!steps.length || page === 'P-0') {
      el.stepbar.classList.add('hidden');
      el.stepbar.innerHTML = '';
      return;
    }
    el.stepbar.classList.remove('hidden');
    var activeIdx = -1;
    for (var i = 0; i < steps.length; i++) {
      if (steps[i].key === d.active_step) { activeIdx = i; break; }
    }
    var html = steps.map(function (s, i) {
      var cls = 'step' + (i === activeIdx ? ' active' : (activeIdx >= 0 && i < activeIdx ? ' done' : ''));
      return '<div class="' + cls + '">' +
        '<div class="no">' + (i < activeIdx ? '✓' : (i + 1)) + '</div>' +
        '<div class="txt">' + esc(s.label) + '<small>' + esc(s.label_en || '') + '</small></div>' +
        '</div>';
    }).join('<div class="step-arrow">›</div>');
    el.stepbar.innerHTML = html;
  }

  /* ------------------------------ 状态反馈区 ------------------------------ */
  function renderStatusbar() {
    var dot = state.online ? 'dot' : (state.fails > 1 ? 'dot off' : 'dot wait');
    var txt = state.online ? '服务正常' : (state.fails > 1 ? '连接中断' : '连接中…');
    var now = new Date();
    var hh = String(now.getHours()).padStart(2, '0');
    var mm = String(now.getMinutes()).padStart(2, '0');
    var sid = (state.snap && state.snap.session_id) ? state.snap.session_id.slice(-8) : '—';
    el.statusbar.innerHTML =
      '<div class="left"><span class="' + dot + '"></span><span>' + txt + '</span>' +
      '<span>会话 ' + esc(sid) + '</span><span>v' + state.version + '</span></div>' +
      '<div class="right"><span>人工服务：对讲按钮 / 售票亭右侧窗口</span>' +
      '<span>' + hh + ':' + mm + '</span></div>';
  }

  /* ============================== 页面：P-0 待机欢迎页 ============================== */
  function p0(d) {
    var prices = (d.price_overview || []).map(function (p) {
      var free = Number(p.price) === 0;
      return '<div class="price-item">' +
        '<div class="row">' +
          '<div class="nm">' + esc(p.name) + '<small>' + esc(p.name_en || '') + '</small></div>' +
          '<div class="pr">' + (free ? '免票' : money(p.price)) +
            (free ? '' : '<small>元</small>') + '</div>' +
        '</div>' +
        (p.note ? '<div class="cond" title="' + esc(p.note) + '">' + esc(p.note) + '</div>' : '') +
        '</div>';
    }).join('');
    var flow = (d.steps || []).map(function (s, i) {
      var ico = ['🎤', '🪪', '📱', '🎫'][i] || '•';
      return '<div class="flow-node"><div class="flow-ico">' + ico + '</div>' +
        '<div class="lbl">' + esc(s.label) + '<small>' + esc(s.label_en || '') + '</small></div></div>';
    }).join('<div class="flow-sep">›</div>');

    return '<div class="p0-wrap">' +
      '<div class="p0-col">' +
        '<div class="p0-title">欢迎光临<br>自贡彩灯节<span>ROBOT TICKETING · 智能售票</span></div>' +
        '<div class="p0-hours">🕘 ' + esc(d.service_hours || '') + '</div>' +
        '<div class="price-grid">' + prices + '</div>' +
      '</div>' +
      '<div class="p0-col">' +
        '<div class="h2">购票只需四步</div>' +
        '<div class="flow-row">' + flow + '</div>' +
        '<div class="card p0-welcome">' +
          '<div class="body">' + esc(d.welcome || '') + '</div>' +
          '<div class="meta">' + esc(d.manual_contact || '') + '</div>' +
        '</div>' +
        '<div class="tip-en">' + esc(d.footer || '') + '</div>' +
      '</div>' +
    '</div>';
  }

  /* ============================== 页面：P-1 需求确认页 ============================== */
  function p1(d) {
    var items = (d.items || []).map(function (i) {
      return '<div class="line-item">' +
        '<div class="nm">' + esc(i.name || i.ticket_type) +
          '<small>单价 ' + money(i.unit_price) + ' 元</small></div>' +
        '<div class="unit">× ' + esc(i.qty) + '</div>' +
        '<div class="sub">' + money(i.amount) + ' 元</div>' +
      '</div>';
    }).join('');
    return '' +
      '<div class="h1">' + esc(d.title || '请确认您的购票需求') + '</div>' +
      '<div class="en">' + esc(d.title_en || '') + '</div>' +
      '<div class="p1-list">' + items +
        '<div class="line-item" style="grid-template-columns:1fr auto">' +
          '<div class="nm">场次<small>' + esc(d.session_name || '') + '</small></div>' +
          '<div class="unit">可售余量 ' + (d.remaining === null || d.remaining === undefined ? '—' : esc(d.remaining)) + ' 张</div>' +
        '</div>' +
      '</div>' +
      '<div class="p1-total"><span class="lbl">合计</span>' +
        '<span class="amount">' + money(d.total_amount) + '<span class="unit">元</span></span></div>' +
      '<div class="tip">' + esc(d.tip || '请确认无误后回答是') + '</div>' +
      '<div class="tip-en">' + esc(d.tip_en || '') + '</div>';
  }

  /* ============================== 页面：P-2 身份核验页 ============================== */
  function p2(d) {
    var ok = !!d.verified;
    var illu = ok
      ? '<div class="reader" style="border-style:solid;border-color:var(--ok);animation:none">' +
          '<div style="text-align:center">' +
            '<div style="font-size:6rem;color:var(--ok)">✓</div>' +
            '<div class="maskline" style="margin-top:1rem">' + esc(d.id_masked || '') + '</div>' +
            (d.name_masked ? '<div class="body" style="margin-top:.5rem">' + esc(d.name_masked) + '</div>' : '') +
          '</div></div>'
      : '<div class="reader"><div class="idcard">' +
          '<div class="avatar"></div>' +
          '<div class="lines"><div class="ln w80"></div><div class="ln w60"></div><div class="ln w45"></div></div>' +
        '</div></div>';

    return '' +
      '<div class="h1">' + esc(ok ? '身份证已核验' : (d.title || '请将身份证平放在读卡区')) + '</div>' +
      '<div class="en">' + esc(ok ? 'ID card verified' : (d.title_en || '')) + '</div>' +
      illu +
      (ok ? '<div class="badge ok">✓ ' + esc(d.verified_text || '已核验') + '</div>' : '') +
      (d.need_more > 0
        ? '<div class="badge warn">还需核验 ' + esc(d.need_more) + ' 位游客</div>' : '') +
      '<div class="tip">' +
        esc(d.need_more > 0 ? '请刷下一张身份证' : (d.tip || '请将身份证平放在读卡区')) +
      '</div>' +
      '<div class="en">' +
        esc(d.need_more > 0 ? 'Scan the next ID card' : (d.realname_notice || '')) +
      '</div>';
  }

  /* ============================== 页面：P-3 订单确认页（付款前最后一道关口） ============================== */
  function p3(d) {
    var main = d.main_text || '';
    // 金额高亮：把「总共N元」中的金额标为金色
    main = esc(main).replace(/总共([^，,]+)/, '总共<em>$1</em>');
    var items = (d.items || []).map(function (i) {
      return '<div class="line-item" style="grid-template-columns:1fr auto auto">' +
        '<div class="nm">' + esc(i.name || i.ticket_type) + '</div>' +
        '<div class="unit">× ' + esc(i.qty) + '</div>' +
        '<div class="sub">' + money(i.amount) + ' 元</div></div>';
    }).join('');
    return '' +
      '<div class="en">ORDER CONFIRMATION · 请核对</div>' +
      '<div class="p3-main">' + main + '</div>' +
      '<div class="p3-amount">' + money(d.amount) + '<span class="unit">元</span></div>' +
      '<div class="p1-list">' + items + '</div>';
  }

  /* ============================== 页面：P-4 扫码支付页 ============================== */
  function p4(d) {
    var methods = (d.pay_methods || []).map(function (m, i) {
      return '<div class="m">' + esc(m) +
        '<small>' + esc((d.pay_methods_en || [])[i] || '') + '</small></div>';
    }).join('');
    var sec = Number(d.countdown_sec || 90);
    return '<div class="p4-wrap">' +
      '<div class="scan-illu">' +
        '<div class="phone"><div class="qr"></div></div>' +
        '<div class="scan-window">扫码窗口</div>' +
        '<div class="beam"></div>' +
      '</div>' +
      '<div class="p4-right">' +
        '<div class="en">请扫码支付 / SCAN TO PAY</div>' +
        '<div class="amount">' + money(d.amount) + '<span class="unit">元</span></div>' +
        '<div class="pay-methods">' + methods + '</div>' +
        '<div class="countdown">' +
          '<div class="bar"><i id="cdbar" style="width:100%"></i></div>' +
          '<div class="num" id="cdnum">' + sec + ' s</div>' +
        '</div>' +
        '<div class="tip">' + esc(d.tip || '') + '</div>' +
      '</div>' +
    '</div>' +
    '<input type="hidden" id="cdtotal" value="' + sec + '">';
  }

  function startCountdown() {
    var total = Number((document.getElementById('cdtotal') || {}).value || 0);
    var bar = document.getElementById('cdbar');
    var num = document.getElementById('cdnum');
    if (!total || !bar) return;
    if (window.__cdTimer) clearInterval(window.__cdTimer);
    var t0 = Date.now();
    window.__cdTimer = setInterval(function () {
      var left = Math.max(0, total - Math.floor((Date.now() - t0) / 1000));
      bar.style.width = (left / total * 100) + '%';
      if (num) {
        num.textContent = left > 0 ? left + ' s' : '正在确认支付结果…';
      }
      if (left <= 0) clearInterval(window.__cdTimer);
    }, 1000);
  }

  /* ============================== 页面：P-5 出票中页面 ============================== */
  function p5(d) {
    return '' +
      '<div class="printer-illu">' +
        '<div class="printer">门票打印机<div class="slot"></div></div>' +
        '<div class="ticket"></div>' +
        '<div class="tray">取票托盘</div>' +
      '</div>' +
      '<div class="h1">' + esc(d.title || '正在出票，请稍候') + '</div>' +
      '<div class="en">' + esc(d.title_en || '') + '</div>' +
      '<div class="badge warn">预计还需 ' + esc(d.eta_sec || 15) + ' 秒</div>' +
      '<div class="tip">' + esc(d.tip || '请勿离开，门票出票后机器人将递交给您') + '</div>';
  }

  /* ============================== 页面：P-6 取票完成页 ============================== */
  function p6(d) {
    var items = (d.items || []).map(function (i) {
      return '<div class="line-item" style="grid-template-columns:1fr auto auto">' +
        '<div class="nm">' + esc(i.name || i.ticket_type) + '</div>' +
        '<div class="unit">× ' + esc(i.qty) + '</div>' +
        '<div class="sub">' + money(i.amount) + ' 元</div></div>';
    }).join('');
    return '' +
      '<div class="done-ico">✓</div>' +
      '<div class="h1">' + esc(d.congrats || '购票成功，祝您游览愉快！') + '</div>' +
      '<div class="en">' + esc(d.title_en || '') + '</div>' +
      '<div class="p1-list">' + items + '</div>' +
      '<div class="badge ok">请从 ' + esc(d.gate || '3 号检票口') + ' 入园</div>' +
      '<div class="body">' + esc(d.entry_tip || '') + '</div>';
  }

  /* ============================== 页面：P-7 异常提示页 ============================== */
  function p7(d) {
    return '' +
      '<div class="err-ico">!</div>' +
      '<div class="h1">' + esc(d.title || '抱歉，本次购票未能完成') + '</div>' +
      '<div class="en">' + esc(d.title_en || '') + '</div>' +
      '<div class="err-reason">' + esc(d.reason || '服务暂时不可用') + '</div>' +
      '<div class="err-action">' + esc(d.action || '') + '</div>' +
      '<div class="tip-en">' + esc(d.action_en || '') + '</div>' +
      '<div class="muted" style="font-size:1.75rem">' + esc(d.manual_contact || '') + '</div>';
  }

  var PAGES = { 'P-0': p0, 'P-1': p1, 'P-2': p2, 'P-3': p3, 'P-4': p4, 'P-5': p5, 'P-6': p6, 'P-7': p7 };

  /* ------------------------------ 渲染 ------------------------------ */
  function render(snap) {
    state.snap = snap;
    state.version = snap.version || state.version;
    var page = snap.page || 'P-0';
    var d = snap.data || {};
    var fn = PAGES[page] || p0;

    renderStepbar(d, page);

    var changed = state.page !== page;
    state.page = page;
    if (changed) state.pageEnterAt = Date.now();

    // 立即填充内容（不延迟），再用 CSS 入场动画完成 ≤300ms 的过渡
    el.page.innerHTML = fn(d);
    el.page.classList.remove('anim');
    void el.page.offsetWidth;          // 强制重排以重启动画
    el.page.classList.add('anim');

    if (page === 'P-4' && (changed || !window.__cdTimer)) startCountdown();
    renderStatusbar();
  }

  /* ------------------------------ 网络 ------------------------------ */
  function showMask(on) { el.mask.hidden = !on; }

  function heartbeat() {
    api('/api/heartbeat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ terminal_id: TERMINAL_ID, online: true })
    }).catch(function () { /* 心跳失败由长轮询统一判定 */ });
  }

  function firstFrame() {
    return api('/api/state').then(function (r) { return r.json(); })
      .then(function (j) {
        state.online = true; state.fails = 0; showMask(false);
        state.version = j.version || 0;
        render(j.snapshot || j);
        loop();
      })
      .catch(function () {
        state.online = false; state.fails++;
        showMask(true);
        setTimeout(firstFrame, 3000);
      });
  }

  function loop() {
    api('/api/state/long?since=' + state.version + '&terminal_id=' + encodeURIComponent(TERMINAL_ID),
        { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        state.online = true; state.fails = 0; showMask(false);
        if (j.code === 304 || !j.snapshot) { return loop(); }
        render(j.snapshot);
        return loop();
      })
      .catch(function () {
        state.online = false; state.fails++;
        renderStatusbar();
        showMask(true);
        setTimeout(firstFrame, 3000);
      });
  }

  /* ------------------------------ 评审/演示模式 ------------------------------ */
  var DEMO = {
    'P-0': { page: 'P-0', page_name: '待机欢迎页', state: 'S0', session_id: null, data: {
      title: '机器人售票', welcome: '您好，欢迎来到自贡彩灯节，我是智能售票员，请问需要购票吗？',
      service_hours: '日场 09:00-18:00 / 夜场 18:00-21:00',
      price_overview: [
        { name: '学生票', name_en: 'Student', price: 80, note: '18 岁以下 / 持学生证' },
        { name: '成人票', name_en: 'Adult', price: 150, note: '无特殊限制' },
        { name: '长者票', name_en: 'Senior', price: 80, note: '65 至 70 周岁' },
        { name: '零元票', name_en: 'Free', price: 0, note: '70 周岁以上，同样出票' }
      ],
      steps: [{ key: 'P2', label: '说需求', label_en: 'Tell us' }, { key: 'P3', label: '刷身份证', label_en: 'Scan ID' },
              { key: 'P5', label: '扫码支付', label_en: 'Pay' }, { key: 'P7', label: '取票入园', label_en: 'Collect' }],
      manual_contact: '需帮助请按对讲呼叫按钮，或前往人工售票窗口',
      footer: '1.3 米以下儿童免票 · 实名购票 一人一票' } },

    'P-1': { page: 'P-1', page_name: '需求确认页', state: 'S3', session_id: 'S202610271600000001', data: {
      title: '请确认您的购票需求', title_en: 'Confirm your order', session_code: 'day', session_name: '日场票',
      items: [{ ticket_type: 'adult', name: '成人票', qty: 2, unit_price: 150, amount: 300 }],
      total_qty: 2, total_amount: 300, remaining: 11998,
      tip: '请确认无误后回答是', tip_en: 'Please confirm',
      steps: [{ key: 'P2', label: '说需求', label_en: 'Tell us' }, { key: 'P3', label: '刷身份证', label_en: 'Scan ID' },
              { key: 'P5', label: '扫码支付', label_en: 'Pay' }, { key: 'P7', label: '取票入园', label_en: 'Collect' }],
      active_step: 'P2' } },

    'P-2': { page: 'P-2', page_name: '身份核验页', state: 'S4', session_id: 'S202610271600000001', data: {
      title: '请将身份证平放在读卡区', title_en: 'Place your ID card here',
      verified: false, need_more: 1, id_masked: null,
      tip: '还需核验 1 位游客的身份证，请刷下一张。', realname_notice: '实名购票，一人一票',
      steps: [{ key: 'P2', label: '说需求', label_en: 'Tell us' }, { key: 'P3', label: '刷身份证', label_en: 'Scan ID' },
              { key: 'P5', label: '扫码支付', label_en: 'Pay' }, { key: 'P7', label: '取票入园', label_en: 'Collect' }],
      active_step: 'P3' } },

    'P-2v': { page: 'P-2', page_name: '身份核验页·已核验', state: 'S5', session_id: 'S202610271600000001', data: {
      title: '身份证已核验', title_en: 'Place your ID card here',
      verified: true, verified_done: true, need_more: 0,
      id_masked: '510302******34', name_masked: '张*佳', age: 36,
      verified_text: '已核验', tip: '身份核验完成，请确认订单信息', realname_notice: '已核验 2 张身份证',
      steps: [{ key: 'P2', label: '说需求', label_en: 'Tell us' }, { key: 'P3', label: '刷身份证', label_en: 'Scan ID' },
              { key: 'P5', label: '扫码支付', label_en: 'Pay' }, { key: 'P7', label: '取票入园', label_en: 'Collect' }],
      active_step: 'P3' } },

    'P-3': { page: 'P-3', page_name: '订单确认页', state: 'S6', session_id: 'S202610271600000001', data: {
      title: '订单确认', title_en: 'Order Confirmation',
      main_text: '成人票两张，总共三百元，请确认，如无异议，请扫码支付！',
      main_text_numeric: '成人票2张，总共 300 元', amount: 300,
      items: [{ ticket_type: 'adult', name: '成人票', qty: 2, unit_price: 150, amount: 300 }],
      total_qty: 2, tip: '请确认，如无异议，请扫码支付！',
      steps: [{ key: 'P2', label: '说需求', label_en: 'Tell us' }, { key: 'P3', label: '刷身份证', label_en: 'Scan ID' },
              { key: 'P5', label: '扫码支付', label_en: 'Pay' }, { key: 'P7', label: '取票入园', label_en: 'Collect' }],
      active_step: 'P5' } },

    'P-4': { page: 'P-4', page_name: '扫码支付页', state: 'S7', session_id: 'S202610271600000001', data: {
      title: '请扫码支付', title_en: 'Scan to pay', amount: 300,
      pay_methods: ['微信付款码', '支付宝付款码'], pay_methods_en: ['WeChat Pay', 'Alipay'],
      countdown_sec: 90, tip: '请打开付款码，对准扫码窗口支付三百元。',
      steps: [{ key: 'P2', label: '说需求', label_en: 'Tell us' }, { key: 'P3', label: '刷身份证', label_en: 'Scan ID' },
              { key: 'P5', label: '扫码支付', label_en: 'Pay' }, { key: 'P7', label: '取票入园', label_en: 'Collect' }],
      active_step: 'P5' } },

    'P-5': { page: 'P-5', page_name: '出票中页面', state: 'S9', session_id: 'S202610271600000001', data: {
      title: '正在出票，请稍候', title_en: 'Printing your ticket', eta_sec: 15,
      tip: '请勿离开，门票出票后机器人将递交给您',
      steps: [{ key: 'P2', label: '说需求', label_en: 'Tell us' }, { key: 'P3', label: '刷身份证', label_en: 'Scan ID' },
              { key: 'P5', label: '扫码支付', label_en: 'Pay' }, { key: 'P7', label: '取票入园', label_en: 'Collect' }],
      active_step: 'P7' } },

    'P-6': { page: 'P-6', page_name: '取票完成页', state: 'S11', session_id: 'S202610271600000001', data: {
      title: '购票成功，请取票', title_en: 'Thank you, enjoy your visit',
      congrats: '购票成功，祝您游览愉快！',
      items: [{ ticket_type: 'adult', name: '成人票', qty: 2, unit_price: 150, amount: 300 }],
      total_qty: 2, total_amount: 300, gate: '3 号检票口', entry_tip: '请从 3 号口进入，祝您玩得开心。',
      steps: [{ key: 'P2', label: '说需求', label_en: 'Tell us' }, { key: 'P3', label: '刷身份证', label_en: 'Scan ID' },
              { key: 'P5', label: '扫码支付', label_en: 'Pay' }, { key: 'P7', label: '取票入园', label_en: 'Collect' }],
      active_step: 'P7' } },

    'P-7': { page: 'P-7', page_name: '异常提示页', state: 'E3', session_id: 'S202610271600000001', data: {
      title: '抱歉，本次购票未能完成', title_en: 'Something went wrong',
      reason: '门票打印机暂时无法出票，已通知工作人员',
      action: '请前往人工售票窗口补票，或按对讲按钮呼叫工作人员',
      action_en: 'Please go to the manned ticket window',
      manual_contact: '人工窗口：售票亭右侧 / 对讲按钮呼叫',
      steps: [{ key: 'P2', label: '说需求', label_en: 'Tell us' }, { key: 'P3', label: '刷身份证', label_en: 'Scan ID' },
              { key: 'P5', label: '扫码支付', label_en: 'Pay' }, { key: 'P7', label: '取票入园', label_en: 'Collect' }] } }
  };

  function showDemo(page) {
    var snap = DEMO[page] || DEMO['P-0'];
    snap.version = state.version + 1;
    state.online = true;
    render(snap);
  }

  function buildDevbar() {
    el.devbar.hidden = false;
    el.devbar.innerHTML = Object.keys(DEMO).map(function (p) {
      return '<button data-p="' + p + '">' + p + ' ' + DEMO[p].page_name + '</button>';
    }).join('') + '<button data-live="1">实时</button>';
    el.devbar.addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b) return;
      if (b.dataset.live) { location.href = location.pathname + '?debug=1'; return; }
      location.href = location.pathname + '?preview=' + b.dataset.p + '&debug=1';
    });
  }

  /* ------------------------------ 启动 ------------------------------ */
  fit();
  renderStatusbar();
  setInterval(renderStatusbar, 10000);
  setInterval(heartbeat, HEARTBEAT_MS);

  if (PREVIEW && DEMO[PREVIEW]) {
    showMask(false);
    showDemo(PREVIEW);
    if (DEBUG) buildDevbar();
  } else {
    if (DEBUG) buildDevbar();
    heartbeat();
    firstFrame();
  }
})();
