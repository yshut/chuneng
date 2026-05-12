// 储能 AGENT 前端 —— 异步无锁版本
// SSE 流式聊天 + 并发 fetch 上传 + 多用户隔离

(() => {
  const $ = (id) => document.getElementById(id);
  const els = {
    userSelect: $('user-select'),
    newUserInput: $('new-user-input'),
    addUserBtn: $('add-user-btn'),
    chatList: $('chat-list'),
    msgInput: $('msg-input'),
    sendBtn: $('send-btn'),
    stopBtn: $('stop-btn'),
    clearBtn: $('clear-btn'),
    resetBtn: $('reset-btn'),
    fileInput: $('file-input'),
    folderInput: $('folder-input'),
    uploadStatus: $('upload-status'),

    stateCard: $('state-card'),
    refreshStateBtn: $('refresh-state-btn'),
    memoryCard: $('memory-card'),
    refreshMemoryBtn: $('refresh-memory-btn'),
    billFileSelect: $('bill-file-select'),
    parseBillsBtn: $('parse-bills-btn'),
    refreshBillsBtn: $('refresh-bills-btn'),
    billParseMode: $('bill-parse-mode'),
    billStatus: $('bill-status'),
    billProgress: $('bill-progress'),
    billSummary: $('bill-summary'),
    capacityAnalysisBtn: $('capacity-analysis-btn'),
    capacityPanel: $('capacity-panel'),
    capacityStatus: $('capacity-status'),
    capacityBest: $('capacity-best'),
    capacityTableWrap: $('capacity-table-wrap'),
    billChart: $('bill-chart'),
    billTouChart: $('bill-tou-chart'),
    billTableWrap: $('bill-table-wrap'),
    kbCard: $('kb-card'),
    refreshKbBtn: $('refresh-kb-btn'),
    kbFileInput: $('kb-file-input'),
    kbFolderInput: $('kb-folder-input'),
    kbUploadStatus: $('kb-upload-status'),
    kbQuery: $('kb-query'),
    kbK: $('kb-k'),
    kbSearchBtn: $('kb-search-btn'),
    kbSearchResult: $('kb-search-result'),

    fileList: $('file-list'),
    refreshFilesBtn: $('refresh-files-btn'),

    toast: $('toast'),
  };

  // ---------- 状态 ----------
  const urlUser = new URLSearchParams(location.search).get('user');
  let currentUser = urlUser || 'main';
  let chatAbortController = null;
  let currentAsstBubble = null;       // 当前 assistant 主气泡（流式追加）
  let currentAsstContainer = null;    // 当前 assistant 整条消息容器（含工具事件）
  let latestBills = null;
  let latestCapacityAnalysis = null;
  let billProgressItems = new Map();

  // ---------- 工具函数 ----------
  function toast(msg, type = '') {
    els.toast.textContent = msg;
    els.toast.className = 'toast show ' + type;
    setTimeout(() => { els.toast.className = 'toast'; }, 2400);
  }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function scrollChatBottom() {
    requestAnimationFrame(() => {
      els.chatList.scrollTop = els.chatList.scrollHeight;
    });
  }

  function fmtNumber(value, digits = 0) {
    const n = Number(value || 0);
    return n.toLocaleString('zh-CN', {
      maximumFractionDigits: digits,
      minimumFractionDigits: digits,
    });
  }

  function fmtCompact(value) {
    const n = Number(value || 0);
    if (Math.abs(n) >= 100000000) return `${fmtNumber(n / 100000000, 2)}亿`;
    if (Math.abs(n) >= 10000) return `${fmtNumber(n / 10000, 2)}万`;
    return fmtNumber(n, 0);
  }

  function makeMsg(role) {
    const wrap = document.createElement('div');
    wrap.className = 'msg ' + role;
    const tag = document.createElement('span');
    tag.className = role === 'user' ? 'user-tag' : 'asst-tag';
    tag.textContent = role === 'user' ? '🧑' : '🤖';
    const inner = document.createElement('div');
    inner.className = 'msg-inner';
    wrap.appendChild(tag);
    wrap.appendChild(inner);
    els.chatList.appendChild(wrap);
    return inner;
  }

  function addBubble(inner, klass = '') {
    const b = document.createElement('div');
    b.className = 'bubble ' + klass;
    inner.appendChild(b);
    return b;
  }

  function addEvtBlock(inner, type, label, body) {
    const b = document.createElement('div');
    b.className = 'evt ' + type;
    b.innerHTML = `<div><span class="label">${escapeHtml(label)}</span></div>${
      body ? `<div>${body}</div>` : ''
    }`;
    inner.appendChild(b);
    return b;
  }

  function pushUserMessage(text) {
    const inner = makeMsg('user');
    const b = addBubble(inner);
    b.textContent = text;
    scrollChatBottom();
  }

  function startAsstMessage() {
    currentAsstContainer = makeMsg('asst');
    currentAsstBubble = null; // 等第一个 text 事件再创建
    scrollChatBottom();
  }

  function ensureAsstBubble() {
    if (!currentAsstBubble) {
      currentAsstBubble = addBubble(currentAsstContainer);
      currentAsstBubble.classList.add('thinking');
    }
    return currentAsstBubble;
  }

  function endAsstMessage() {
    if (currentAsstBubble) currentAsstBubble.classList.remove('thinking');
    currentAsstBubble = null;
    currentAsstContainer = null;
  }

  // ---------- API ----------
  function formatHttpError(status, statusText, text) {
    if (status === 413) {
      return '文件过大，请控制在 200MB 以内后重试。';
    }

    try {
      const data = JSON.parse(text || '');
      const detail = data.detail || data.error || data.message;
      if (detail) return String(detail);
    } catch (_) {
      // Fall through to plain text cleanup.
    }

    const plain = String(text || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
    const suffix = plain ? `：${plain.slice(0, 180)}` : '';
    return `${status || '网络错误'} ${statusText || '请求失败'}${suffix}`;
  }

  async function readErrorMessage(res) {
    const text = await res.text().catch(() => '');
    return formatHttpError(res.status, res.statusText, text);
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    });
    if (!res.ok) {
      throw new Error(await readErrorMessage(res));
    }
    return res.json();
  }

  async function refreshUsers() {
    const r = await api('/api/users');
    if (!r.users.includes(currentUser)) currentUser = r.current_user || r.users[0] || 'main';
    els.userSelect.innerHTML = '';
    for (const u of r.users) {
      const opt = document.createElement('option');
      opt.value = u; opt.textContent = u;
      if (u === currentUser) opt.selected = true;
      els.userSelect.appendChild(opt);
    }
  }

  async function resolveCurrentUser() {
    if (urlUser) return;
    const r = await api('/api/me');
    currentUser = r.user_id || currentUser || 'main';
  }

  async function refreshState() {
    try {
      const s = await api(`/api/state?user_id=${encodeURIComponent(currentUser)}`);
      const lines = [
        ['用户', s.user_id, true],
        ['是否有数据', s.has_data ? `${s.rows} 行` : '—', s.has_data],
        ['优化已完成', s.has_optimization ? '是' : '否', s.has_optimization],
        ['收益已完成', s.has_revenue ? '是' : '否', s.has_revenue],
        ['资方/客户分配', s.has_investor ? '是' : '否', s.has_investor],
        ['Markdown 报告', s.has_md_report ? '已生成' : '—', s.has_md_report],
        ['ReAct 反思', s.react ? '开启' : '关闭', s.react],
        ['工具数', s.tools_count, true],
        ['input/ 文件数', (s.input_files || []).length, true],
      ];
      els.stateCard.innerHTML = lines.map(([k, v, ok]) =>
        `<div class="row"><span class="k">${escapeHtml(k)}</span>` +
        `<span class="v ${ok ? 'ok' : 'no'}">${escapeHtml(v)}</span></div>`
      ).join('');
    } catch (e) {
      els.stateCard.textContent = '❌ ' + e.message;
    }
  }

  async function refreshMemory() {
    try {
      const m = await api(`/api/memory?user_id=${encodeURIComponent(currentUser)}`);
      if (!m.enabled) {
        els.memoryCard.innerHTML = '<div class="muted">长期记忆未启用</div>';
        return;
      }
      const stats = m.stats || {};
      const facts = m.facts || {};
      const lines = [];
      lines.push('<div class="row"><span class="k">📥 working</span><span class="v">' +
        escapeHtml(stats.working_count ?? '-') + '</span></div>');
      lines.push('<div class="row"><span class="k">📜 summaries</span><span class="v">' +
        escapeHtml(stats.summary_count ?? '-') + '</span></div>');
      lines.push('<div class="row"><span class="k">🔑 facts</span><span class="v">' +
        escapeHtml(stats.fact_count ?? '-') + '</span></div>');
      lines.push('<div class="row"><span class="k">🔧 tool_log</span><span class="v">' +
        escapeHtml(stats.tool_count ?? '-') + '</span></div>');
      const factKeys = Object.keys(facts);
      if (factKeys.length) {
        lines.push('<div class="card-section"><h4>最近 facts</h4>');
        for (const k of factKeys.slice(0, 12)) {
          const v = facts[k];
          const vText = typeof v === 'object' ? JSON.stringify(v) : String(v);
          lines.push(`<div class="row"><span class="k">${escapeHtml(k)}</span>` +
                     `<span class="v">${escapeHtml(vText.slice(0, 60))}</span></div>`);
        }
        lines.push('</div>');
      }
      els.memoryCard.innerHTML = lines.join('');
    } catch (e) {
      els.memoryCard.textContent = '❌ ' + e.message;
    }
  }

  async function refreshBills() {
    if (!els.billStatus) return;
    try {
      const r = await api(`/api/bills?user_id=${encodeURIComponent(currentUser)}`);
      renderBills(r);
    } catch (e) {
      els.billStatus.innerHTML = `<span style="color:var(--stop)">❌ ${escapeHtml(e.message)}</span>`;
    }
  }

  async function parseBills() {
    if (!els.billFileSelect || !els.parseBillsBtn) return;
    const selected = Array.from(els.billFileSelect.selectedOptions || []).map(opt => opt.value);
    const initialLabel = selected.length
      ? `准备解析 ${selected.length} 个文件`
      : '准备解析 input/ 目录所有账单文件';
    els.billStatus.textContent = initialLabel;
    els.parseBillsBtn.disabled = true;
    clearCapacityAnalysis();
    resetBillProgress(initialLabel);
    try {
      const res = await fetch('/api/bills/parse/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_id: currentUser,
          files: selected,
          mode: els.billParseMode.value || 'auto',
        }),
      });
      if (!res.ok) throw new Error(await readErrorMessage(res));
      await readBillParseStream(res);
      refreshState();
      toast('账单解析完成', 'ok');
    } catch (e) {
      els.billStatus.innerHTML = `<span style="color:var(--stop)">❌ ${escapeHtml(e.message)}</span>`;
      markBillProgressError(e.message);
    } finally {
      els.parseBillsBtn.disabled = false;
    }
  }

  async function readBillParseStream(res) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const dataLine = block.split('\n').find(l => l.startsWith('data:'));
        if (!dataLine) continue;
        try {
          handleBillParseEvent(JSON.parse(dataLine.slice(5).trim()));
        } catch (_) {
          // Ignore malformed stream frames.
        }
      }
    }
  }

  function handleBillParseEvent(ev) {
    if (ev.type === 'start') {
      resetBillProgress(`开始解析 ${ev.total || 0} 个文件`, ev.total || 0);
    } else if (ev.type === 'file_start') {
      updateBillProgress(ev.progress || 0, `正在解析 ${ev.index}/${ev.total}：${ev.file || ''}`);
      setBillFileProgress(ev.file, 'running', `解析中 (${ev.index}/${ev.total})`);
    } else if (ev.type === 'file_done') {
      updateBillProgress(ev.progress || 0, `已完成 ${ev.index}/${ev.total}：${ev.file || ''}`);
      setBillFileProgress(ev.file, 'done', `完成，新增 ${ev.rows_added || 0} 条 · ${ev.parser || '-'}`);
      if (ev.payload) renderBills(ev.payload, { keepProgress: true });
    } else if (ev.type === 'file_error') {
      updateBillProgress(ev.progress || 0, `解析失败 ${ev.index}/${ev.total}：${ev.file || ''}`);
      setBillFileProgress(ev.file, 'error', ev.message || '解析失败');
    } else if (ev.type === 'done') {
      updateBillProgress(100, `解析完成：成功 ${ev.success || 0} 个，失败 ${ev.failed || 0} 个`);
      if (ev.payload) renderBills(ev.payload, { keepProgress: true });
    }
  }

  function resetBillProgress(label, total = 0) {
    if (!els.billProgress) return;
    billProgressItems = new Map();
    els.billProgress.classList.remove('hidden');
    els.billProgress.innerHTML =
      `<div class="bill-progress-head"><span>${escapeHtml(label)}</span><b>0%</b></div>` +
      `<div class="bill-progress-track"><span style="width:0%"></span></div>` +
      `<div class="bill-progress-list"></div>`;
    if (total) updateBillProgress(0, `等待解析 ${total} 个文件`);
  }

  function updateBillProgress(progress, label) {
    if (!els.billProgress) return;
    const pct = Math.min(100, Math.max(0, Number(progress || 0)));
    const head = els.billProgress.querySelector('.bill-progress-head span');
    const num = els.billProgress.querySelector('.bill-progress-head b');
    const bar = els.billProgress.querySelector('.bill-progress-track span');
    if (head) head.textContent = label;
    if (num) num.textContent = `${Math.round(pct)}%`;
    if (bar) bar.style.width = `${pct}%`;
    if (els.billStatus) els.billStatus.textContent = label;
  }

  function setBillFileProgress(file, status, message) {
    if (!file || !els.billProgress) return;
    billProgressItems.set(file, { status, message });
    const list = els.billProgress.querySelector('.bill-progress-list');
    if (!list) return;
    list.innerHTML = Array.from(billProgressItems.entries()).map(([name, item]) =>
      `<div class="bill-progress-item ${escapeHtml(item.status)}">` +
        `<span class="name">${escapeHtml(name)}</span>` +
        `<span class="msg">${escapeHtml(item.message || '')}</span>` +
      `</div>`
    ).join('');
  }

  function markBillProgressError(message) {
    if (!els.billProgress) return;
    els.billProgress.classList.remove('hidden');
    updateBillProgress(100, `解析中断：${message}`);
  }

  function renderBills(data, opts = {}) {
    if (!els.billFileSelect || !els.billSummary || !els.billTableWrap) return;
    latestBills = data;
    const files = data.available_files || [];
    const currentSelected = new Set(Array.from(els.billFileSelect.selectedOptions || []).map(opt => opt.value));
    els.billFileSelect.innerHTML = files.map(name =>
      `<option value="${escapeHtml(name)}" ${currentSelected.has(name) ? 'selected' : ''}>${escapeHtml(name)}</option>`
    ).join('');

    const rows = data.records || [];
    if (!opts.keepProgress) {
      els.billProgress.classList.add('hidden');
    }

    if (!files.length) {
      els.billStatus.textContent = 'input/ 目录暂无可解析账单，请先上传 PDF / Excel / 图片文件。';
    } else if (!rows.length) {
      els.billStatus.textContent = '请选择账单文件后点击解析账单。未选择时会解析全部账单文件。';
    } else {
      const parser = data.parser ? ` · ${data.parser}` : '';
      const fileText = data.files && data.files.length ? ` · ${data.files.length} 个文件` : '';
      els.billStatus.textContent = `${data.msg || '已加载账单数据'}${fileText}${parser}`;
    }

    renderBillSummary(data.summary || {});
    renderBillCharts(rows, data.summary || {});
    renderBillTable(rows, data.columns || []);
  }

  function renderBillSummary(summary) {
    const cards = [
      ['记录数', `${summary.row_count || 0} 条`],
      ['总电量', `${fmtCompact(summary.total_kwh)} kWh`],
      ['总电费', `${fmtCompact(summary.total_amount)} 元`],
      ['平均电价', `${fmtNumber(summary.avg_unit_price || 0, 4)} 元/kWh`],
      ['最大需量', `${fmtNumber(summary.max_demand_kw || 0, 2)} kW`],
      ['账期', summary.start_month ? `${summary.start_month} 至 ${summary.end_month}` : '-'],
    ];
    els.billSummary.innerHTML = cards.map(([label, value]) =>
      `<div class="metric"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`
    ).join('');
  }

  function renderBillCharts(rows, summary) {
    if (!els.billChart || !els.billTouChart) return;
    renderMonthlyChart(els.billChart, rows);
    renderTouChart(els.billTouChart, summary.tou || {});
  }

  function renderMonthlyChart(canvas, rows) {
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(280, Math.round(rect.width || canvas.parentElement.clientWidth || 320));
    const height = Math.max(170, Number(canvas.getAttribute('height')) || 180);
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    if (!rows.length) {
      drawEmptyChart(ctx, width, height, '暂无账单数据');
      return;
    }

    const labels = rows.map(r => String(r['月份'] || '-'));
    const kwh = rows.map(r => Number(r['总电量(kWh)'] || 0));
    const amount = rows.map(r => Number(r['总电费(元)'] || 0));
    const maxKwh = Math.max(...kwh, 1);
    const maxAmount = Math.max(...amount, 1);
    const pad = { left: 42, right: 18, top: 22, bottom: 34 };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const groupW = plotW / Math.max(rows.length, 1);
    const barW = Math.max(5, Math.min(18, groupW * 0.28));

    drawAxes(ctx, pad, width, height);
    ctx.font = '11px sans-serif';
    rows.forEach((_, i) => {
      const center = pad.left + groupW * i + groupW / 2;
      const kh = (kwh[i] / maxKwh) * plotH;
      const ah = (amount[i] / maxAmount) * plotH;
      ctx.fillStyle = '#4f8cff';
      ctx.fillRect(center - barW - 2, pad.top + plotH - kh, barW, kh);
      ctx.fillStyle = '#34c759';
      ctx.fillRect(center + 2, pad.top + plotH - ah, barW, ah);
      if (rows.length <= 12 || i % Math.ceil(rows.length / 8) === 0) {
        ctx.save();
        ctx.translate(center, height - 10);
        ctx.rotate(-Math.PI / 8);
        ctx.fillStyle = '#9aa0ad';
        ctx.textAlign = 'right';
        ctx.fillText(labels[i].slice(2), 0, 0);
        ctx.restore();
      }
    });
    drawLegend(ctx, width, [['电量', '#4f8cff'], ['电费', '#34c759']]);
  }

  function renderTouChart(canvas, tou) {
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(280, Math.round(rect.width || canvas.parentElement.clientWidth || 320));
    const height = Math.max(110, Number(canvas.getAttribute('height')) || 120);
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const items = [
      ['尖峰', Number(tou.peak || 0), '#e3554f'],
      ['高峰', Number(tou.high || 0), '#f5a623'],
      ['平段', Number(tou.flat || 0), '#4f8cff'],
      ['谷段', Number(tou.valley || 0), '#34c759'],
    ];
    const total = items.reduce((sum, item) => sum + item[1], 0);
    if (!total) {
      drawEmptyChart(ctx, width, height, '暂无分时电量');
      return;
    }
    let x = 14;
    const y = 26;
    const barW = width - 28;
    const barH = 18;
    items.forEach(([label, value, color]) => {
      const w = barW * value / total;
      ctx.fillStyle = color;
      ctx.fillRect(x, y, w, barH);
      x += w;
    });
    ctx.font = '12px sans-serif';
    let lx = 14;
    items.forEach(([label, value, color]) => {
      ctx.fillStyle = color;
      ctx.fillRect(lx, 62, 9, 9);
      ctx.fillStyle = '#c9d3e0';
      ctx.fillText(`${label} ${fmtNumber(value / total * 100, 1)}%`, lx + 14, 71);
      lx += Math.min(120, Math.max(76, width / 4));
    });
  }

  function drawAxes(ctx, pad, width, height) {
    ctx.strokeStyle = '#303646';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(pad.left, pad.top);
    ctx.lineTo(pad.left, height - pad.bottom);
    ctx.lineTo(width - pad.right, height - pad.bottom);
    ctx.stroke();
  }

  function drawLegend(ctx, width, items) {
    let x = width - 118;
    ctx.font = '12px sans-serif';
    for (const [label, color] of items) {
      ctx.fillStyle = color;
      ctx.fillRect(x, 10, 9, 9);
      ctx.fillStyle = '#c9d3e0';
      ctx.fillText(label, x + 14, 19);
      x += 54;
    }
  }

  function drawEmptyChart(ctx, width, height, text) {
    ctx.fillStyle = '#9aa0ad';
    ctx.font = '13px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(text, width / 2, height / 2);
    ctx.textAlign = 'left';
  }

  function renderBillTable(rows, columns) {
    if (!rows.length) {
      els.billTableWrap.innerHTML = '<div class="muted">暂无账单表格数据</div>';
      return;
    }
    const cols = columns.length ? columns : Object.keys(rows[0]);
    els.billTableWrap.innerHTML =
      '<table class="bill-table"><thead><tr>' +
      cols.map(c => `<th>${escapeHtml(c)}</th>`).join('') +
      '</tr></thead><tbody>' +
      rows.map(row => '<tr>' + cols.map(c => {
        const value = row[c];
        const isNum = typeof value === 'number' || (value !== '' && !Number.isNaN(Number(value)));
        const text = isNum && c !== '月份'
          ? fmtNumber(Number(value), c.includes('平均电价') || c === '功率因数' ? 4 : 2)
          : String(value ?? '');
        return `<td class="${isNum && c !== '月份' ? 'num' : ''}">${escapeHtml(text)}</td>`;
      }).join('') + '</tr>').join('') +
      '</tbody></table>';
  }

  async function analyzeCapacity() {
    if (!els.capacityAnalysisBtn || !els.capacityPanel) return;
    const rows = latestBills && latestBills.records ? latestBills.records : [];
    if (!rows.length) {
      toast('请先解析账单数据', 'error');
      return;
    }

    els.capacityAnalysisBtn.disabled = true;
    els.capacityPanel.classList.remove('hidden');
    els.capacityStatus.textContent = '分析中...';
    els.capacityBest.innerHTML = '<div class="muted">正在计算多个储能容量组合...</div>';
    els.capacityTableWrap.innerHTML = '';
    try {
      const data = await api('/api/storage/capacity-analysis', {
        method: 'POST',
        body: JSON.stringify({ user_id: currentUser }),
      });
      renderCapacityAnalysis(data);
      refreshState();
      toast('容量分析完成', 'ok');
    } catch (e) {
      els.capacityStatus.textContent = '分析失败';
      els.capacityBest.innerHTML = `<div style="color:var(--stop)">❌ ${escapeHtml(e.message)}</div>`;
      toast(e.message, 'error');
    } finally {
      els.capacityAnalysisBtn.disabled = false;
    }
  }

  function clearCapacityAnalysis() {
    latestCapacityAnalysis = null;
    if (!els.capacityPanel) return;
    els.capacityPanel.classList.add('hidden');
    els.capacityStatus.textContent = '未分析';
    els.capacityBest.innerHTML = '';
    els.capacityTableWrap.innerHTML = '';
  }

  function renderCapacityAnalysis(data) {
    if (!els.capacityPanel || !els.capacityBest || !els.capacityTableWrap) return;
    latestCapacityAnalysis = data || {};
    const best = latestCapacityAnalysis.best || {};
    const rows = latestCapacityAnalysis.results || [];
    els.capacityPanel.classList.remove('hidden');
    els.capacityStatus.textContent = `${rows.length} 个组合 · 正收益 ${latestCapacityAnalysis.positive_count || 0} 个`;
    els.capacityBest.innerHTML =
      `<div class="capacity-callout">` +
        `<div>` +
          `<span>推荐组合</span>` +
          `<b>${fmtNumber(best.battery_capacity_kwh || 0, 0)} kWh / ${fmtNumber(best.inverter_power_kw || 0, 0)} kW</b>` +
        `</div>` +
        `<div><span>储能时长</span><b>${fmtNumber(best.duration_hours || 0, 2)} h</b></div>` +
        `<div><span>总投资</span><b>${fmtCompact(best.total_investment_yuan)} 元</b></div>` +
        `<div><span>年净收益</span><b>${fmtCompact(best.annual_revenue_yuan)} 元</b></div>` +
        `<div><span>回收期</span><b>${best.payback_years == null ? '-' : `${fmtNumber(best.payback_years, 2)} 年`}</b></div>` +
        `<div><span>IRR</span><b>${fmtNumber(best.irr_percent || 0, 2)}%</b></div>` +
      `</div>` +
      `<p class="capacity-note">${escapeHtml(latestCapacityAnalysis.scoring_basis || '')}</p>`;

    if (!rows.length) {
      els.capacityTableWrap.innerHTML = '<div class="muted" style="padding:14px">暂无容量分析结果</div>';
      return;
    }

    const cols = [
      ['rank', '排名'],
      ['name', '组合'],
      ['battery_capacity_kwh', '电池容量(kWh)'],
      ['inverter_power_kw', 'PCS功率(kW)'],
      ['duration_hours', '时长(h)'],
      ['total_investment_yuan', '投资(元)'],
      ['annual_revenue_yuan', '年净收益(元)'],
      ['payback_years', '回收期(年)'],
      ['irr_percent', 'IRR(%)'],
      ['npv_yuan', 'NPV(元)'],
      ['score', '评分'],
    ];
    els.capacityTableWrap.innerHTML =
      '<table class="capacity-table"><thead><tr>' +
      cols.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join('') +
      '</tr></thead><tbody>' +
      rows.map((row) => `<tr class="${row.is_best ? 'best-row' : ''}">` + cols.map(([key]) => {
        let value = row[key];
        if (value == null) value = '-';
        else if (key === 'rank') value = row.is_best ? '推荐' : value;
        else if (typeof value === 'number') {
          const digits = ['duration_hours', 'payback_years', 'irr_percent', 'score'].includes(key) ? 2 : 0;
          value = fmtNumber(value, digits);
        }
        const isNum = typeof row[key] === 'number' && key !== 'rank';
        return `<td class="${isNum ? 'num' : ''}">${escapeHtml(value)}</td>`;
      }).join('') + '</tr>').join('') +
      '</tbody></table>';
  }

  async function refreshKb() {
    try {
      const k = await api(`/api/kb?user_id=${encodeURIComponent(currentUser)}`);
      if (!k.enabled) {
        els.kbCard.innerHTML = '<div class="muted">知识库未启用（缺嵌入模型/未安装 chromadb）</div>';
        return;
      }
      const stats = k.stats || {};
      const docs = k.documents || [];
      let html = '<div class="row"><span class="k">总 chunks</span><span class="v">' +
        escapeHtml(stats.total_chunks ?? stats.count ?? '-') + '</span></div>';
      html += '<div class="row"><span class="k">文档数</span><span class="v">' +
        escapeHtml(docs.length) + '</span></div>';
      if (docs.length) {
        html += '<div class="card-section"><h4>已索引文档</h4>';
        for (const d of docs.slice(0, 30)) {
          const src = typeof d === 'string' ? d : (d.source || d.name || JSON.stringify(d));
          html += `<div class="row"><span class="k" style="word-break:break-all;flex:1 1 auto">${escapeHtml(src)}</span>` +
                  `<span class="v" style="cursor:pointer;color:var(--stop)" data-source="${escapeHtml(src)}" class="kb-del">✕</span></div>`;
        }
        html += '</div>';
      }
      els.kbCard.innerHTML = html;
      // 绑定删除
      els.kbCard.querySelectorAll('[data-source]').forEach(el => {
        el.addEventListener('click', async () => {
          const src = el.getAttribute('data-source');
          if (!confirm(`删除知识库文档：${src}？`)) return;
          try {
            await api(`/api/kb/${encodeURIComponent(src)}?user_id=${encodeURIComponent(currentUser)}`,
                       { method: 'DELETE' });
            toast('已删除', 'ok');
            refreshKb();
          } catch (e) { toast(e.message, 'error'); }
        });
      });
    } catch (e) {
      els.kbCard.textContent = '❌ ' + e.message;
    }
  }

  async function loadHistory() {
    els.chatList.innerHTML = '';
    try {
      const r = await api(`/api/history?user_id=${encodeURIComponent(currentUser)}`);
      const msgs = r.messages || [];
      if (!msgs.length) {
        els.chatList.innerHTML =
          '<div class="muted" style="padding:24px;text-align:center;">' +
          '欢迎！这是用户 <b>' + escapeHtml(currentUser) +
          '</b> 的对话窗口。说点什么开始吧。</div>';
        return;
      }
      for (const m of msgs) {
        if (m.role === 'user') {
          pushUserMessage(m.content || '');
        } else if (m.role === 'assistant') {
          const inner = makeMsg('asst');
          // 之前的工具调用：用简化 evt 提示（不再展示完整结果）
          if (m.tool_calls && m.tool_calls.length) {
            addEvtBlock(inner, 'tool',
              `🔧 历史工具调用: ${m.tool_calls.join(', ')}`,
              '<span class="muted">（结果已折叠，仅做提示）</span>');
          }
          if (m.content) {
            const b = addBubble(inner);
            b.textContent = m.content;
          }
        }
      }
      scrollChatBottom();
    } catch (e) {
      els.chatList.innerHTML =
        '<div style="padding:14px;color:var(--stop)">❌ 加载历史失败：' +
        escapeHtml(e.message) + '</div>';
    }
  }

  async function refreshFiles() {
    try {
      const s = await api(`/api/state?user_id=${encodeURIComponent(currentUser)}`);
      const files = s.input_files || [];
      if (!files.length) {
        els.fileList.innerHTML = '<li class="muted">（input/ 为空）</li>';
        return;
      }
      els.fileList.innerHTML = files.map(f =>
        `<li><span class="fname">${escapeHtml(f)}</span>` +
        `<span class="del" data-name="${escapeHtml(f)}" title="删除">🗑</span></li>`
      ).join('');
      els.fileList.querySelectorAll('[data-name]').forEach(el => {
        el.addEventListener('click', async () => {
          const name = el.getAttribute('data-name');
          if (!confirm(`删除文件 input/${name}？`)) return;
          try {
            await api(`/api/input/${encodeURIComponent(name)}`, { method: 'DELETE' });
            toast('已删除', 'ok');
            refreshFiles();
            refreshState();
          } catch (e) { toast(e.message, 'error'); }
        });
      });
    } catch (e) { els.fileList.innerHTML = '<li>❌ ' + escapeHtml(e.message) + '</li>'; }
  }

  // ---------- 上传 ----------
  function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    const digits = value >= 100 || unit === 0 ? 0 : 1;
    return `${value.toFixed(digits)} ${units[unit]}`;
  }

  function renderUploadProgress(statusEl, label, fileCount, loaded, total, lengthComputable) {
    const pct = lengthComputable && total > 0
      ? Math.min(100, Math.max(0, Math.round((loaded / total) * 100)))
      : 0;
    const sizeText = lengthComputable && total > 0
      ? `${formatBytes(loaded)} / ${formatBytes(total)}`
      : `已上传 ${formatBytes(loaded)}`;
    const pctText = lengthComputable && total > 0 ? `${pct}%` : '上传中';
    statusEl.classList.add('upload-progress');
    statusEl.innerHTML =
      `<span class="upload-progress-head">` +
        `<span>${escapeHtml(label)} ${fileCount} 个文件</span>` +
        `<span>${escapeHtml(pctText)}</span>` +
      `</span>` +
      `<span class="upload-progress-track"><span style="width:${pct}%"></span></span>` +
      `<span class="upload-progress-meta">${escapeHtml(sizeText)}</span>`;
  }

  function renderUploadPending(statusEl, text) {
    statusEl.classList.add('upload-progress');
    statusEl.innerHTML =
      `<span class="upload-progress-head">` +
        `<span>${escapeHtml(text)}</span>` +
        `<span>100%</span>` +
      `</span>` +
      `<span class="upload-progress-track"><span style="width:100%"></span></span>` +
      `<span class="upload-progress-meta">文件已传完，等待服务器处理</span>`;
  }

  function uploadWithProgress(path, fd, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', path);
      xhr.upload.onprogress = (ev) => {
        onProgress(ev.loaded || 0, ev.total || 0, ev.lengthComputable);
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            resolve(JSON.parse(xhr.responseText || '{}'));
          } catch (_) {
            reject(new Error('服务器返回格式异常'));
          }
          return;
        }
        reject(new Error(formatHttpError(xhr.status, xhr.statusText, xhr.responseText)));
      };
      xhr.onerror = () => reject(new Error('网络连接失败，请稍后重试'));
      xhr.onabort = () => reject(new Error('上传已取消'));
      xhr.send(fd);
    });
  }

  async function uploadFiles(fileList, fromFolder, statusEl) {
    if (!fileList || !fileList.length) return;
    const fd = new FormData();
    for (const f of fileList) fd.append('files', f, f.name);
    fd.append('user_id', currentUser);
    fd.append('from_folder', fromFolder ? '1' : '0');
    renderUploadProgress(statusEl, '准备上传', fileList.length, 0, 0, false);
    try {
      const r = await uploadWithProgress('/api/upload', fd, (loaded, total, lengthComputable) => {
        renderUploadProgress(statusEl, '正在上传', fileList.length, loaded, total, lengthComputable);
        if (lengthComputable && loaded >= total) renderUploadPending(statusEl, '上传完成，正在保存');
      });
      statusEl.classList.remove('upload-progress');
      statusEl.innerHTML = `<span style="color:var(--ok)">✓ 已上传 ${r.copied.length}</span>` +
        (r.skipped.length ? ` <span class="muted">（跳过 ${r.skipped.length}）</span>` : '') +
        (r.errors && r.errors.length ? ` <span style="color:var(--stop)">错误 ${r.errors.length}</span>` : '');
      refreshFiles(); refreshState();
    } catch (e) {
      statusEl.classList.remove('upload-progress');
      statusEl.innerHTML = `<span style="color:var(--stop)">❌ ${escapeHtml(e.message)}</span>`;
    }
  }

  async function uploadKbFiles(fileList, fromFolder) {
    if (!fileList || !fileList.length) return;
    const fd = new FormData();
    for (const f of fileList) fd.append('files', f, f.name);
    fd.append('user_id', currentUser);
    fd.append('from_folder', fromFolder ? '1' : '0');
    renderUploadProgress(els.kbUploadStatus, '准备上传', fileList.length, 0, 0, false);
    try {
      const r = await uploadWithProgress('/api/kb/index', fd, (loaded, total, lengthComputable) => {
        renderUploadProgress(els.kbUploadStatus, '正在上传', fileList.length, loaded, total, lengthComputable);
        if (lengthComputable && loaded >= total) renderUploadPending(els.kbUploadStatus, '上传完成，正在索引');
      });
      const ok = (r.results || []).filter(x => x.ok).length;
      const fail = (r.results || []).filter(x => !x.ok).length;
      els.kbUploadStatus.classList.remove('upload-progress');
      els.kbUploadStatus.innerHTML = `<span style="color:var(--ok)">✓ 成功 ${ok}</span>` +
        (fail ? ` <span style="color:var(--stop)">失败 ${fail}</span>` : '');
      refreshKb();
    } catch (e) {
      els.kbUploadStatus.classList.remove('upload-progress');
      els.kbUploadStatus.innerHTML = `<span style="color:var(--stop)">❌ ${escapeHtml(e.message)}</span>`;
    }
  }

  // ---------- KB 检索 ----------
  async function doKbSearch() {
    const q = els.kbQuery.value.trim();
    if (!q) return;
    els.kbSearchResult.innerHTML = '<div class="muted">检索中…</div>';
    try {
      const r = await api('/api/kb/search', {
        method: 'POST',
        body: JSON.stringify({ user_id: currentUser, query: q, k: parseInt(els.kbK.value || '5') }),
      });
      const hits = r.hits || [];
      if (!hits.length) { els.kbSearchResult.innerHTML = '<div class="muted">无结果</div>'; return; }
      els.kbSearchResult.innerHTML = hits.map(h =>
        `<div class="kb-hit"><div class="src">${escapeHtml(h.source || '')}` +
        (h.score != null ? ` · <span class="score">score ${Number(h.score).toFixed(3)}</span>` : '') +
        `</div><div>${escapeHtml((h.text || '').slice(0, 400))}</div></div>`
      ).join('');
    } catch (e) {
      els.kbSearchResult.innerHTML = `<div style="color:var(--stop)">❌ ${escapeHtml(e.message)}</div>`;
    }
  }

  // ---------- 聊天 SSE ----------
  async function sendMessage() {
    const text = els.msgInput.value.trim();
    if (!text) return;
    if (chatAbortController) {
      toast('已有进行中的对话，先停止', 'error');
      return;
    }
    els.msgInput.value = '';
    pushUserMessage(text);
    startAsstMessage();
    setSending(true);

    chatAbortController = new AbortController();

    let acc = ''; // 累积 text delta 用于流式
    let curToolEvt = null; // 进行中的工具事件 div

    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: currentUser, message: text }),
        signal: chatAbortController.signal,
      });
      if (!res.ok) throw new Error(await readErrorMessage(res));
      const reader = res.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buf = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        // 按 SSE 切分（\n\n 分事件）
        let idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const block = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const dataLine = block.split('\n').find(l => l.startsWith('data:'));
          if (!dataLine) continue;
          let payload;
          try { payload = JSON.parse(dataLine.slice(5).trim()); }
          catch { continue; }
          handleEvent(payload);
        }
      }
    } catch (e) {
      if (e.name === 'AbortError') {
        addEvtBlock(currentAsstContainer, 'tool-error', '⛔ 已停止', '');
      } else {
        addEvtBlock(currentAsstContainer, 'tool-error', '❌ 请求出错', escapeHtml(e.message));
      }
    } finally {
      chatAbortController = null;
      endAsstMessage();
      setSending(false);
      // 思考结束后异步刷新面板（不阻塞）
      refreshState(); refreshMemory();
    }

    function handleEvent(ev) {
      const t = ev.type;
      if (t === 'text') {
        const b = ensureAsstBubble();
        acc += ev.delta || '';
        b.textContent = acc;
        scrollChatBottom();
      } else if (t === 'tool') {
        const args = ev.args ? JSON.stringify(ev.args).slice(0, 220) : '';
        curToolEvt = addEvtBlock(currentAsstContainer, 'tool',
          `🔧 调用工具: ${ev.name}`,
          args ? `<code>${escapeHtml(args)}</code>` : '');
        scrollChatBottom();
      } else if (t === 'tool_progress') {
        const p = ev.progress || {};
        const msg = p.msg || JSON.stringify(p);
        addEvtBlock(currentAsstContainer, 'tool',
          `⏳ ${ev.name} 进度`,
          escapeHtml(msg).slice(0, 200));
        scrollChatBottom();
      } else if (t === 'tool_result') {
        const result = (ev.result || '').slice(0, 600);
        addEvtBlock(currentAsstContainer, 'tool-result',
          `✓ ${ev.name} 完成`,
          `<code>${escapeHtml(result)}</code>`);
        scrollChatBottom();
      } else if (t === 'tool_error') {
        addEvtBlock(currentAsstContainer, 'tool-error',
          `⚠️ ${ev.name || '工具'} 出错（重试 ${ev.retry || 0}/${ev.max_retries || 0}）`,
          escapeHtml(ev.error || ev.message || ''));
        scrollChatBottom();
      } else if (t === 'subagent') {
        const phase = ev.phase || '';
        const role = ev.role || ev.name || '';
        addEvtBlock(currentAsstContainer, 'subagent',
          `🤝 子 Agent [${role}] ${phase}`, escapeHtml(ev.task || ev.msg || ''));
        scrollChatBottom();
      } else if (t === 'reflection') {
        // reflection 是 LLM 的"思考过程"，使用单独的样式
        let reflBlock = currentAsstContainer.querySelector('.evt.reflection.active');
        if (!reflBlock) {
          reflBlock = addEvtBlock(currentAsstContainer, 'reflection active', '💭 反思', '');
          reflBlock.dataset.acc = '';
        }
        reflBlock.dataset.acc += (ev.delta || '');
        reflBlock.querySelector('.label').nextSibling?.remove();
        reflBlock.innerHTML =
          `<div><span class="label">💭 反思</span></div><div>${escapeHtml(reflBlock.dataset.acc)}</div>`;
        scrollChatBottom();
      } else if (t === 'final') {
        // 最终回复：如果之前没有 text 流，把 final 内容写进 bubble
        if (!acc) {
          const b = ensureAsstBubble();
          b.textContent = ev.content || '';
        }
      } else if (t === 'error') {
        addEvtBlock(currentAsstContainer, 'tool-error', '❌ 错误', escapeHtml(ev.message || ''));
      } else if (t === 'done') {
        // 流结束信号
      }
    }
  }

  function setSending(v) {
    els.sendBtn.disabled = v;
    els.stopBtn.disabled = !v;
    els.msgInput.disabled = v;
    if (v) els.sendBtn.textContent = '生成中…'; else els.sendBtn.textContent = '发送';
  }

  function stopChat() {
    if (chatAbortController) {
      chatAbortController.abort();
      chatAbortController = null;
    }
  }

  // ---------- 事件绑定 ----------
  function bind() {
    els.sendBtn.addEventListener('click', sendMessage);
    els.stopBtn.addEventListener('click', stopChat);
    els.msgInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });

    els.clearBtn.addEventListener('click', async () => {
      if (!confirm('清空当前会话上下文？长期记忆会保留。')) return;
      try {
        await api('/api/clear', { method: 'POST', body: JSON.stringify({ user_id: currentUser }) });
        loadHistory();
        toast('已清空对话', 'ok');
      } catch (e) { toast(e.message, 'error'); }
    });

    els.resetBtn.addEventListener('click', async () => {
      if (!confirm('彻底重置当前用户（清空所有数据 + 记忆）？此操作不可逆。')) return;
      try {
        await api('/api/reset', { method: 'POST', body: JSON.stringify({ user_id: currentUser }) });
        loadHistory();
        clearCapacityAnalysis();
        toast('已重置', 'ok');
        refreshState(); refreshMemory(); refreshFiles();
      } catch (e) { toast(e.message, 'error'); }
    });

    els.userSelect.addEventListener('change', () => {
      currentUser = els.userSelect.value;
      loadHistory();
      clearCapacityAnalysis();
      refreshState(); refreshMemory(); refreshKb(); refreshFiles();
      toast(`切换到用户：${currentUser}`, 'ok');
    });

    els.addUserBtn.addEventListener('click', async () => {
      const newU = els.newUserInput.value.trim();
      if (!newU) { toast('请输入用户名', 'error'); return; }
      try {
        const r = await api('/api/users', {
          method: 'POST',
          body: JSON.stringify({ user_id: newU })
        });
        els.newUserInput.value = '';
        currentUser = r.user_id;
        await refreshUsers();
        loadHistory();
        clearCapacityAnalysis();
        refreshState(); refreshMemory(); refreshKb(); refreshFiles();
        toast(`已新建用户：${currentUser}`, 'ok');
      } catch (e) { toast(e.message, 'error'); }
    });

    // 上传
    els.fileInput.addEventListener('change', () => {
      uploadFiles(els.fileInput.files, false, els.uploadStatus);
      els.fileInput.value = '';
    });
    els.folderInput.addEventListener('change', () => {
      uploadFiles(els.folderInput.files, true, els.uploadStatus);
      els.folderInput.value = '';
    });
    els.kbFileInput.addEventListener('change', () => {
      uploadKbFiles(els.kbFileInput.files, false);
      els.kbFileInput.value = '';
    });
    els.kbFolderInput.addEventListener('change', () => {
      uploadKbFiles(els.kbFolderInput.files, true);
      els.kbFolderInput.value = '';
    });

    // 刷新按钮
    els.refreshStateBtn.addEventListener('click', refreshState);
    els.refreshMemoryBtn.addEventListener('click', refreshMemory);
    if (els.refreshBillsBtn) els.refreshBillsBtn.addEventListener('click', refreshBills);
    if (els.parseBillsBtn) els.parseBillsBtn.addEventListener('click', parseBills);
    if (els.capacityAnalysisBtn) els.capacityAnalysisBtn.addEventListener('click', analyzeCapacity);
    els.refreshKbBtn.addEventListener('click', refreshKb);
    els.refreshFilesBtn.addEventListener('click', refreshFiles);

    // 知识库检索
    els.kbSearchBtn.addEventListener('click', doKbSearch);
    els.kbQuery.addEventListener('keydown', (e) => { if (e.key === 'Enter') doKbSearch(); });

    // Tab 切换
    document.querySelectorAll('.tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const tab = btn.getAttribute('data-tab');
        document.querySelectorAll('.tab-pane').forEach(p => {
          p.classList.toggle('hidden', p.getAttribute('data-pane') !== tab);
        });
        // 切到面板时按需刷新
        if (tab === 'memory') refreshMemory();
        else if (tab === 'kb') refreshKb();
        else if (tab === 'files') refreshFiles();
      });
    });
  }

  // ---------- 初始化 ----------
  async function init() {
    bind();
    await resolveCurrentUser();
    await refreshUsers();
    currentUser = els.userSelect.value || 'main';
    loadHistory();
    refreshState(); refreshMemory(); refreshKb(); refreshFiles();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
