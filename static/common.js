// 储能 AGENT —— 共享工具模块（所有页面共用）
// 通过 window.AgentCommon 暴露：DOM/格式化/HTTP/上传/Toast/图表/用户会话/Debounce 等
(function () {
  'use strict';

  const C = (window.AgentCommon = window.AgentCommon || {});

  // ============================================================
  // DOM helpers
  // ============================================================
  C.$ = (id) => document.getElementById(id);
  C.qs = (sel, root) => (root || document).querySelector(sel);
  C.qsa = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  // ============================================================
  // Format
  // ============================================================
  C.escapeHtml = function escapeHtml(value) {
    if (value == null) return '';
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  };

  C.fmtNumber = function fmtNumber(value, digits = 0) {
    const n = Number(value);
    if (value === null || value === undefined || Number.isNaN(n)) {
      return (0).toLocaleString('zh-CN', {
        maximumFractionDigits: digits,
        minimumFractionDigits: digits,
      });
    }
    return n.toLocaleString('zh-CN', {
      maximumFractionDigits: digits,
      minimumFractionDigits: digits,
    });
  };

  C.fmtNumberOrDash = function fmtNumberOrDash(value, digits = 0) {
    const n = Number(value);
    if (value === null || value === undefined || Number.isNaN(n)) return '-';
    return n.toLocaleString('zh-CN', {
      maximumFractionDigits: digits,
      minimumFractionDigits: digits,
    });
  };

  C.fmtCompact = function fmtCompact(value) {
    const n = Number(value || 0);
    if (Math.abs(n) >= 100000000) return `${C.fmtNumber(n / 100000000, 2)}亿`;
    if (Math.abs(n) >= 10000) return `${C.fmtNumber(n / 10000, 2)}万`;
    return C.fmtNumber(n, 0);
  };

  C.fmtMoney = function fmtMoney(value) {
    return `${C.fmtCompact(value)} 元`;
  };

  C.formatBytes = function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    let v = bytes;
    let u = 0;
    while (v >= 1024 && u < units.length - 1) {
      v /= 1024;
      u += 1;
    }
    const digits = v >= 100 || u === 0 ? 0 : 1;
    return `${v.toFixed(digits)} ${units[u]}`;
  };

  // ============================================================
  // HTTP
  // ============================================================
  C.formatHttpError = function formatHttpError(status, statusText, text) {
    if (status === 413) return '文件过大，请控制在 200MB 以内后重试。';
    try {
      const data = JSON.parse(text || '');
      const detail = data.detail || data.error || data.message;
      if (detail) return String(detail);
    } catch (_) {
      // fall through
    }
    const plain = String(text || '')
      .replace(/<[^>]+>/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
    const suffix = plain ? `：${plain.slice(0, 180)}` : '';
    return `${status || '网络错误'} ${statusText || '请求失败'}${suffix}`;
  };

  C.readErrorMessage = async function readErrorMessage(res) {
    const text = await res.text().catch(() => '');
    return C.formatHttpError(res.status, res.statusText, text);
  };

  C.api = async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    });
    if (!res.ok) throw new Error(await C.readErrorMessage(res));
    return res.json();
  };

  C.errorMessage = function errorMessage(err) {
    if (!err) return '未知错误';
    if (err instanceof Error && err.message) return err.message;
    return String(err);
  };

  C.uploadWithProgress = function uploadWithProgress(path, fd, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', path);
      xhr.upload.onprogress = (ev) => {
        if (onProgress) onProgress(ev.loaded || 0, ev.total || 0, ev.lengthComputable);
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
        reject(new Error(C.formatHttpError(xhr.status, xhr.statusText, xhr.responseText)));
      };
      xhr.onerror = () => reject(new Error('网络连接失败，请稍后重试'));
      xhr.onabort = () => reject(new Error('上传已取消'));
      xhr.send(fd);
    });
  };

  // ============================================================
  // Upload progress UI
  // ============================================================
  C.renderUploadProgress = function renderUploadProgress(statusEl, label, fileCount, loaded, total, lengthComputable) {
    if (!statusEl) return;
    const pct = lengthComputable && total > 0
      ? Math.min(100, Math.max(0, Math.round((loaded / total) * 100)))
      : 0;
    const sizeText = lengthComputable && total > 0
      ? `${C.formatBytes(loaded)} / ${C.formatBytes(total)}`
      : `已上传 ${C.formatBytes(loaded)}`;
    const pctText = lengthComputable && total > 0 ? `${pct}%` : '上传中';
    statusEl.classList.add('upload-progress');
    statusEl.innerHTML =
      `<span class="upload-progress-head">` +
        `<span>${C.escapeHtml(label)} ${fileCount} 个文件</span>` +
        `<span>${C.escapeHtml(pctText)}</span>` +
      `</span>` +
      `<span class="upload-progress-track"><span style="width:${pct}%"></span></span>` +
      `<span class="upload-progress-meta">${C.escapeHtml(sizeText)}</span>`;
  };

  C.renderUploadPending = function renderUploadPending(statusEl, text) {
    if (!statusEl) return;
    statusEl.classList.add('upload-progress');
    statusEl.innerHTML =
      `<span class="upload-progress-head">` +
        `<span>${C.escapeHtml(text)}</span>` +
        `<span>100%</span>` +
      `</span>` +
      `<span class="upload-progress-track"><span style="width:100%"></span></span>` +
      `<span class="upload-progress-meta">文件已传完，等待服务器处理</span>`;
  };

  // ============================================================
  // Toast (基于队列，多次触发不再覆盖闪烁)
  // ============================================================
  const toastState = {
    el: null,
    queue: [],
    busy: false,
    hideTimer: null,
    nextTimer: null,
  };

  function ensureToastEl() {
    if (toastState.el && document.body.contains(toastState.el)) return toastState.el;
    let el = document.getElementById('toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'toast';
      el.className = 'toast';
      el.setAttribute('role', 'status');
      el.setAttribute('aria-live', 'polite');
      document.body.appendChild(el);
    } else {
      el.setAttribute('role', 'status');
      el.setAttribute('aria-live', 'polite');
    }
    toastState.el = el;
    return el;
  }

  function showNextToast() {
    const next = toastState.queue.shift();
    if (!next) {
      toastState.busy = false;
      return;
    }
    toastState.busy = true;
    const el = ensureToastEl();
    el.textContent = next.message;
    el.className = `toast show ${next.type || ''}`.trim();
    clearTimeout(toastState.hideTimer);
    toastState.hideTimer = setTimeout(() => {
      el.classList.remove('show');
      clearTimeout(toastState.nextTimer);
      toastState.nextTimer = setTimeout(showNextToast, 220);
    }, next.duration || 2200);
  }

  C.toast = function toast(message, type = '', duration = 2200) {
    if (message === undefined || message === null) return;
    toastState.queue.push({ message: String(message), type, duration });
    if (!toastState.busy) showNextToast();
  };

  // ============================================================
  // User session
  // ============================================================
  C.resolveCurrentUser = async function resolveCurrentUser(urlUser, fallback = 'main') {
    if (urlUser) return urlUser;
    try {
      const data = await C.api('/api/me');
      return data.user_id || fallback;
    } catch (_) {
      return fallback;
    }
  };

  C.refreshUsers = async function refreshUsers(selectEl, currentUser) {
    const data = await C.api('/api/users');
    const users = data.users || [];
    let active = currentUser;
    if (!users.includes(active)) active = data.current_user || users[0] || 'main';
    if (selectEl) {
      selectEl.innerHTML = users.map((u) =>
        `<option value="${C.escapeHtml(u)}" ${u === active ? 'selected' : ''}>${C.escapeHtml(u)}</option>`
      ).join('');
    }
    return active;
  };

  // ============================================================
  // Debounce
  // ============================================================
  C.debounce = function debounce(fn, wait = 120) {
    let timer = null;
    return function debounced(...args) {
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), wait);
    };
  };

  // ============================================================
  // Canvas / Chart primitives
  // ============================================================
  C.setupCanvas = function setupCanvas(canvas, minWidth, minHeight) {
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const parentW = canvas.parentElement ? canvas.parentElement.clientWidth : minWidth;
    const width = Math.max(minWidth, Math.round(rect.width || parentW || minWidth));
    const height = Math.max(minHeight, Number(canvas.getAttribute('height')) || minHeight);
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    return { ctx, width, height };
  };

  C.drawAxes = function drawAxes(ctx, pad, width, height) {
    ctx.strokeStyle = '#303646';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(pad.left, pad.top);
    ctx.lineTo(pad.left, height - pad.bottom);
    ctx.lineTo(width - pad.right, height - pad.bottom);
    ctx.stroke();
  };

  C.drawGrid = function drawGrid(ctx, pad, width, height, count) {
    ctx.strokeStyle = '#252b38';
    ctx.lineWidth = 1;
    for (let i = 1; i <= count; i += 1) {
      const y = pad.top + ((height - pad.top - pad.bottom) / count) * i;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(width - pad.right, y);
      ctx.stroke();
    }
  };

  C.drawLegend = function drawLegend(ctx, width, items) {
    let x = Math.max(110, width - 132);
    ctx.font = '12px sans-serif';
    for (const [label, color] of items) {
      ctx.fillStyle = color;
      ctx.fillRect(x, 12, 9, 9);
      ctx.fillStyle = '#c9d3e0';
      ctx.fillText(label, x + 14, 21);
      x += 56;
    }
  };

  C.drawEmptyChart = function drawEmptyChart(ctx, width, height, text) {
    ctx.fillStyle = '#9aa0ad';
    ctx.font = '13px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(text, width / 2, height / 2);
    ctx.textAlign = 'left';
  };

  C.renderMonthlyChart = function renderMonthlyChart(canvas, rows, opts) {
    const o = opts || {};
    const { ctx, width, height } = C.setupCanvas(canvas, o.minWidth || 300, o.minHeight || 210);
    if (!rows.length) {
      C.drawEmptyChart(ctx, width, height, '暂无账单数据');
      return;
    }
    const labels = rows.map((r) => String(r['月份'] || '-'));
    const kwh = rows.map((r) => Number(r['总电量(kWh)'] || 0));
    const amount = rows.map((r) => Number(r['总电费(元)'] || 0));
    const maxKwh = Math.max(...kwh, 1);
    const maxAmount = Math.max(...amount, 1);
    const pad = { left: 48, right: 22, top: 26, bottom: 38 };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const groupW = plotW / Math.max(rows.length, 1);
    const barW = Math.max(6, Math.min(22, groupW * 0.28));

    C.drawAxes(ctx, pad, width, height);
    C.drawGrid(ctx, pad, width, height, 4);
    ctx.font = '11px sans-serif';
    rows.forEach((_, idx) => {
      const center = pad.left + groupW * idx + groupW / 2;
      const kwhH = (kwh[idx] / maxKwh) * plotH;
      const amountH = (amount[idx] / maxAmount) * plotH;
      ctx.fillStyle = '#4f8cff';
      ctx.fillRect(center - barW - 2, pad.top + plotH - kwhH, barW, kwhH);
      ctx.fillStyle = '#34c759';
      ctx.fillRect(center + 2, pad.top + plotH - amountH, barW, amountH);
      if (rows.length <= 12 || idx % Math.ceil(rows.length / 8) === 0) {
        ctx.save();
        ctx.translate(center, height - 12);
        ctx.rotate(-Math.PI / 8);
        ctx.fillStyle = '#9aa0ad';
        ctx.textAlign = 'right';
        ctx.fillText(labels[idx].slice(2), 0, 0);
        ctx.restore();
      }
    });
    ctx.fillStyle = '#9aa0ad';
    ctx.font = '11px sans-serif';
    ctx.fillText(C.fmtCompact(maxKwh), 8, pad.top + 4);
    C.drawLegend(ctx, width, [['电量', '#4f8cff'], ['电费', '#34c759']]);
  };

  C.renderTouChart = function renderTouChart(canvas, tou, opts) {
    const o = opts || {};
    const { ctx, width, height } = C.setupCanvas(canvas, o.minWidth || 300, o.minHeight || 160);
    const items = [
      ['尖峰', Number(tou.peak || 0), '#e3554f'],
      ['高峰', Number(tou.high || 0), '#f5a623'],
      ['平段', Number(tou.flat || 0), '#4f8cff'],
      ['谷段', Number(tou.valley || 0), '#34c759'],
    ];
    const total = items.reduce((s, it) => s + it[1], 0);
    if (!total) {
      C.drawEmptyChart(ctx, width, height, '暂无分时电量');
      return;
    }
    let x = 16;
    const y = 32;
    const barW = width - 32;
    const barH = 22;
    items.forEach(([, value, color]) => {
      const w = barW * value / total;
      ctx.fillStyle = color;
      ctx.fillRect(x, y, w, barH);
      x += w;
    });
    ctx.font = '12px sans-serif';
    const colW = Math.max(118, Math.floor((width - 32) / 2));
    items.forEach(([label, value, color], idx) => {
      const lx = 16 + (idx % 2) * colW;
      const ly = 84 + Math.floor(idx / 2) * 30;
      ctx.fillStyle = color;
      ctx.fillRect(lx, ly - 9, 9, 9);
      ctx.fillStyle = '#c9d3e0';
      ctx.fillText(`${label} ${C.fmtNumber(value / total * 100, 1)}%`, lx + 14, ly);
      ctx.fillStyle = '#9aa0ad';
      ctx.fillText(`${C.fmtCompact(value)} kWh`, lx + 14, ly + 15);
    });
  };

  // ============================================================
  // Capacity rendering (bills / capacity 页面共享)
  // ============================================================
  C.renderCapacityCallout = function renderCapacityCallout(best, scoringBasis) {
    const b = best || {};
    return (
      `<div class="capacity-callout">` +
        `<div><span>推荐组合</span><b>${C.fmtNumber(b.battery_capacity_kwh || 0, 0)} kWh / ${C.fmtNumber(b.inverter_power_kw || 0, 0)} kW</b></div>` +
        `<div><span>储能时长</span><b>${C.fmtNumber(b.duration_hours || 0, 2)} h</b></div>` +
        `<div><span>总投资</span><b>${C.fmtCompact(b.total_investment_yuan)} 元</b></div>` +
        `<div><span>年综合收益</span><b>${C.fmtCompact(b.annual_revenue_yuan)} 元</b></div>` +
        `<div><span>峰谷套利</span><b>${C.fmtCompact(b.arbitrage_revenue_yuan)} 元</b></div>` +
        `<div><span>需量收益</span><b>${C.fmtCompact(b.demand_revenue_yuan)} 元</b></div>` +
        `<div><span>收益/容量</span><b>${C.fmtNumber(b.annual_revenue_per_kwh || 0, 0)} 元/kWh·年</b></div>` +
        `<div><span>回收期</span><b>${b.payback_years == null ? '-' : `${C.fmtNumber(b.payback_years, 2)} 年`}</b></div>` +
      `</div>` +
      `<p class="capacity-note">${C.escapeHtml(scoringBasis || '')}</p>`
    );
  };

  C.renderCapacityTable = function renderCapacityTable(rows) {
    if (!rows || !rows.length) {
      return '<div class="muted" style="padding:14px">暂无容量分析结果</div>';
    }
    const cols = [
      ['rank', '排名'],
      ['name', '组合'],
      ['battery_capacity_kwh', '电池容量(kWh)'],
      ['inverter_power_kw', 'PCS功率(kW)'],
      ['duration_hours', '时长(h)'],
      ['total_investment_yuan', '投资(元)'],
      ['annual_revenue_yuan', '年综合收益(元)'],
      ['arbitrage_revenue_yuan', '峰谷套利(元)'],
      ['demand_revenue_yuan', '需量收益(元)'],
      ['annual_revenue_per_kwh', '收益/容量'],
      ['marginal_revenue_per_kwh', '边际收益'],
      ['payback_years', '回收期(年)'],
      ['utilization_ratio', '利用率'],
    ];
    const twoDigitKeys = ['duration_hours', 'payback_years', 'annual_revenue_per_kwh', 'marginal_revenue_per_kwh'];
    return '<table class="capacity-table"><thead><tr>' +
      cols.map(([, label]) => `<th>${C.escapeHtml(label)}</th>`).join('') +
      '</tr></thead><tbody>' +
      rows.map((row) => `<tr class="${row.is_best ? 'best-row' : ''}">` + cols.map(([key]) => {
        let value = row[key];
        if (value == null) value = '-';
        else if (key === 'rank') value = row.is_best ? '推荐' : value;
        else if (typeof value === 'number') {
          const digits = twoDigitKeys.includes(key) ? 2 : (key === 'utilization_ratio' ? 4 : 0);
          value = C.fmtNumber(value, digits);
        }
        const isNum = typeof row[key] === 'number' && key !== 'rank';
        return `<td class="${isNum ? 'num' : ''}">${C.escapeHtml(value)}</td>`;
      }).join('') + '</tr>').join('') +
      '</tbody></table>';
  };

  // ============================================================
  // Keyboard-friendly Tabs（支持方向键切换）
  // ============================================================
  C.bindTabs = function bindTabs(buttonsSelector, paneAttr = 'data-pane') {
    const buttons = C.qsa(buttonsSelector);
    if (!buttons.length) return;
    const panes = C.qsa(`[${paneAttr}]`);

    buttons.forEach((btn, idx) => {
      btn.setAttribute('role', 'tab');
      btn.setAttribute('tabindex', btn.classList.contains('active') ? '0' : '-1');
      btn.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
          e.preventDefault();
          const dir = e.key === 'ArrowRight' ? 1 : -1;
          const next = buttons[(idx + dir + buttons.length) % buttons.length];
          next.focus();
          next.click();
        } else if (e.key === 'Home') {
          e.preventDefault();
          buttons[0].focus();
          buttons[0].click();
        } else if (e.key === 'End') {
          e.preventDefault();
          buttons[buttons.length - 1].focus();
          buttons[buttons.length - 1].click();
        }
      });
    });

    panes.forEach((p) => p.setAttribute('role', 'tabpanel'));
  };

  // ============================================================
  // 表格排序（点击表头，再次点击切换升降序）
  // 工作机制：表头点击后排序所在 tbody 的行，并把箭头加在被点的 th 上
  // ============================================================
  function detectSortValue(td) {
    const text = (td.textContent || '').trim();
    if (text === '' || text === '-') return { num: NaN, text: '' };
    // 剥离千分位 / 单位（kWh, kW, 元, 年, % 等）后尝试 parseFloat
    const cleaned = text.replace(/,/g, '').replace(/[^\d.\-+eE]/g, '');
    const num = parseFloat(cleaned);
    return { num: Number.isFinite(num) ? num : NaN, text };
  }

  C.bindSortableTable = function bindSortableTable(table) {
    if (!table || table.dataset.sortBound === '1') return;
    table.dataset.sortBound = '1';
    const ths = Array.from(table.querySelectorAll('thead th'));
    const tbody = table.querySelector('tbody');
    if (!ths.length || !tbody) return;

    ths.forEach((th, idx) => {
      th.classList.add('sortable');
      th.setAttribute('role', 'columnheader');
      th.setAttribute('aria-sort', 'none');
      th.tabIndex = 0;
      const handler = () => {
        const current = th.getAttribute('aria-sort');
        const dir = current === 'ascending' ? 'descending' : 'ascending';
        ths.forEach((other) => {
          if (other !== th) other.setAttribute('aria-sort', 'none');
        });
        th.setAttribute('aria-sort', dir);
        const sign = dir === 'ascending' ? 1 : -1;
        const rows = Array.from(tbody.querySelectorAll('tr'));
        rows.sort((a, b) => {
          const ta = a.children[idx];
          const tb = b.children[idx];
          if (!ta || !tb) return 0;
          const va = detectSortValue(ta);
          const vb = detectSortValue(tb);
          if (!Number.isNaN(va.num) && !Number.isNaN(vb.num)) return (va.num - vb.num) * sign;
          return va.text.localeCompare(vb.text, 'zh-CN') * sign;
        });
        const frag = document.createDocumentFragment();
        rows.forEach((r) => frag.appendChild(r));
        tbody.appendChild(frag);
      };
      th.addEventListener('click', handler);
      th.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          handler();
        }
      });
    });
  };

  // 在 wrap 容器内自动为所有 bill-table / capacity-table 绑定排序
  C.enableSortInContainer = function enableSortInContainer(container) {
    if (!container) return;
    container.querySelectorAll('table.bill-table, table.capacity-table').forEach((t) => C.bindSortableTable(t));
  };

  // ============================================================
  // 浮动「回到顶部」按钮（长页面用）
  // ============================================================
  C.mountBackToTop = function mountBackToTop(scrollContainer) {
    const target = scrollContainer || window;
    if (document.getElementById('back-to-top')) return;
    const btn = document.createElement('button');
    btn.id = 'back-to-top';
    btn.className = 'back-to-top';
    btn.setAttribute('aria-label', '回到顶部');
    btn.title = '回到顶部';
    btn.textContent = '↑';
    document.body.appendChild(btn);

    const getTop = () => (target === window ? window.scrollY : target.scrollTop);
    const update = () => {
      btn.classList.toggle('show', getTop() > 320);
    };
    const onScroll = C.debounce(update, 80);
    if (target === window) {
      window.addEventListener('scroll', onScroll, { passive: true });
    } else {
      target.addEventListener('scroll', onScroll, { passive: true });
    }
    btn.addEventListener('click', () => {
      if (target === window) window.scrollTo({ top: 0, behavior: 'smooth' });
      else target.scrollTo({ top: 0, behavior: 'smooth' });
    });
    update();
  };

  // ============================================================
  // 骨架屏占位（loading）
  // ============================================================
  C.skeleton = function skeleton(rows = 3) {
    let html = '<div class="skeleton-stack">';
    for (let i = 0; i < rows; i += 1) {
      html += `<div class="skeleton-line" style="width:${60 + Math.floor(Math.random() * 38)}%"></div>`;
    }
    return html + '</div>';
  };

  C.emptyState = function emptyState(text, hint) {
    const hintHtml = hint ? `<div class="empty-state-hint">${C.escapeHtml(hint)}</div>` : '';
    return `<div class="empty-state"><div class="empty-state-text">${C.escapeHtml(text)}</div>${hintHtml}</div>`;
  };

  // ============================================================
  // 通用模态对话框
  // ============================================================
  C.Modal = (function () {
    let openCount = 0;

    function open(opts) {
      const {
        title = '',
        body = '',
        footer = null,
        size = 'md',
        onClose = null,
      } = opts || {};

      const backdrop = document.createElement('div');
      backdrop.className = 'modal-backdrop';
      backdrop.setAttribute('role', 'dialog');
      backdrop.setAttribute('aria-modal', 'true');
      backdrop.setAttribute('aria-label', title || '对话框');

      const dialog = document.createElement('div');
      dialog.className = `modal-dialog modal-${size}`;
      const headerHtml = title
        ? `<div class="modal-header"><h3 class="modal-title">${C.escapeHtml(title)}</h3>` +
          `<button type="button" class="modal-close" aria-label="关闭">×</button></div>`
        : '';
      dialog.innerHTML = `${headerHtml}<div class="modal-body"></div><div class="modal-footer"></div>`;
      backdrop.appendChild(dialog);
      document.body.appendChild(backdrop);

      const bodyEl = dialog.querySelector('.modal-body');
      const footerEl = dialog.querySelector('.modal-footer');
      if (typeof body === 'string') bodyEl.innerHTML = body;
      else if (body instanceof Node) bodyEl.appendChild(body);
      if (typeof footer === 'string') footerEl.innerHTML = footer;
      else if (footer instanceof Node) footerEl.appendChild(footer);
      else if (Array.isArray(footer)) footer.forEach((n) => n && footerEl.appendChild(n));
      else footerEl.style.display = 'none';

      openCount += 1;
      document.documentElement.classList.add('modal-open');

      let closed = false;
      const close = (reason) => {
        if (closed) return;
        closed = true;
        backdrop.classList.add('hide');
        setTimeout(() => {
          backdrop.remove();
          openCount = Math.max(0, openCount - 1);
          if (openCount === 0) document.documentElement.classList.remove('modal-open');
          if (typeof onClose === 'function') onClose(reason);
        }, 160);
      };

      backdrop.addEventListener('click', (e) => {
        if (e.target === backdrop) close('backdrop');
      });
      const closeBtn = dialog.querySelector('.modal-close');
      if (closeBtn) closeBtn.addEventListener('click', () => close('button'));
      const onKey = (e) => {
        if (e.key === 'Escape') {
          close('escape');
          document.removeEventListener('keydown', onKey);
        }
      };
      document.addEventListener('keydown', onKey);

      requestAnimationFrame(() => {
        const focusable = dialog.querySelector(
          'input, select, textarea, button:not(.modal-close)'
        );
        if (focusable) focusable.focus();
      });

      return { close, dialog, body: bodyEl, footer: footerEl, backdrop };
    }

    return { open };
  })();

  // ============================================================
  // LLM 设置（API URL / Key / Model）
  // ============================================================
  C.openLLMSettings = async function openLLMSettings() {
    let config = null;
    try {
      config = await C.api('/api/llm/config');
    } catch (err) {
      C.toast(`读取 LLM 配置失败：${C.errorMessage(err)}`, 'error');
      return;
    }

    const providers = Array.isArray(config.providers) && config.providers.length
      ? config.providers
      : ['qwen', 'wenxin', 'mimo', 'openai_compat'];
    const providerLabels = {
      qwen: '阿里通义千问 (qwen)',
      wenxin: '百度文心 (wenxin)',
      mimo: 'MiMo / OpenAI 兼容中转 (mimo)',
      openai_compat: 'OpenAI 兼容 (openai_compat)',
    };
    const providerDefaults = config.provider_defaults || {};

    const form = document.createElement('form');
    form.className = 'llm-form';
    form.autocomplete = 'off';

    const providerOptions = providers
      .map((p) => {
        const sel = p === config.provider ? 'selected' : '';
        const label = providerLabels[p] || p;
        return `<option value="${C.escapeHtml(p)}" ${sel}>${C.escapeHtml(label)}</option>`;
      })
      .join('');

    const renderBanner = (cfg) => {
      if (!cfg.api_key_set) {
        return `<div class="llm-banner llm-banner-warn">
          <b>尚未配置 API Key。</b>
          请在下面 <code>API Key</code> 输入框填入你的密钥，并确认 <code>Provider</code>（不同厂商的 Key 不能混用）。
          如果你更习惯环境变量，也可以设置 <code>${C.escapeHtml(cfg.env_key_name || 'API_KEY')}</code> 后重启服务。
        </div>`;
      }
      if (!cfg.client_ok) {
        return `<div class="llm-banner llm-banner-warn">
          <b>API Key 已记录但客户端没能初始化：</b>${C.escapeHtml(cfg.client_error || '未知原因')}
        </div>`;
      }
      const src = cfg.api_key_from_env
        ? `环境变量 <code>${C.escapeHtml(cfg.env_key_name || 'API_KEY')}</code>`
        : '保存在本地配置文件';
      return `<div class="llm-banner llm-banner-ok">
        客户端已就绪 · Key 来自 ${src} · 当前模型 <code>${C.escapeHtml(cfg.model || '-')}</code>
      </div>`;
    };

    const keyHint = config.api_key_set
      ? config.api_key_from_env
        ? `当前来自环境变量 <code>${C.escapeHtml(config.env_key_name || 'API_KEY')}</code>：${C.escapeHtml(config.api_key)}（留空表示沿用环境变量）`
        : `当前已配置（脱敏）：${C.escapeHtml(config.api_key)}（留空表示不修改）`
      : '尚未配置，请填入 API Key。';

    form.innerHTML = `
      <div class="llm-banner-wrap">${renderBanner(config)}</div>
      <div class="llm-grid">
        <label class="llm-field">
          <span>Provider</span>
          <select name="provider">${providerOptions}</select>
        </label>
        <label class="llm-field">
          <span>API Base URL</span>
          <input name="base_url" type="text" placeholder="https://api.example.com/v1"
                 value="${C.escapeHtml(config.base_url || '')}" />
        </label>
        <label class="llm-field">
          <span>对话模型 (model)</span>
          <input name="model" type="text" placeholder="例如 mimo-v2.5-pro"
                 value="${C.escapeHtml(config.model || '')}" />
        </label>
        <label class="llm-field">
          <span>视觉模型 (vision_model)</span>
          <input name="vision_model" type="text" placeholder="例如 qwen-vl-max"
                 value="${C.escapeHtml(config.vision_model || '')}" />
        </label>
        <label class="llm-field llm-field-wide">
          <span>API Key</span>
          <div class="llm-key-row">
            <input name="api_key" type="password" placeholder="留空表示不修改" autocomplete="new-password" />
            <button type="button" class="btn btn-ghost llm-key-toggle" aria-label="显示/隐藏">显示</button>
            <button type="button" class="btn btn-ghost llm-key-clear"
                    title="清空已保存的 Key，回退到环境变量">清空</button>
          </div>
          <small class="llm-hint">${keyHint}</small>
        </label>
        <label class="llm-field">
          <span>Temperature</span>
          <input name="temperature" type="number" step="0.05" min="0" max="2"
                 value="${Number(config.temperature ?? 0.7)}" />
        </label>
        <label class="llm-field">
          <span>Max Tokens</span>
          <input name="max_tokens" type="number" step="64" min="64"
                 value="${Number(config.max_tokens ?? 2048)}" />
        </label>
      </div>
      <div class="llm-status" aria-live="polite"></div>
    `;

    const bannerWrap = form.querySelector('.llm-banner-wrap');
    const keyInput = form.querySelector('input[name="api_key"]');
    const providerSelect = form.querySelector('select[name="provider"]');
    const baseUrlInput = form.querySelector('input[name="base_url"]');
    const modelInput = form.querySelector('input[name="model"]');
    const visionInput = form.querySelector('input[name="vision_model"]');
    const keyToggle = form.querySelector('.llm-key-toggle');
    const keyClearBtn = form.querySelector('.llm-key-clear');
    keyToggle.addEventListener('click', () => {
      const isPwd = keyInput.type === 'password';
      keyInput.type = isPwd ? 'text' : 'password';
      keyToggle.textContent = isPwd ? '隐藏' : '显示';
    });

    // 切换 provider 时，把当前还是“旧 provider 默认值”的 base_url/model 自动替换成新 provider 的默认值
    let lastProvider = config.provider;
    providerSelect.addEventListener('change', () => {
      const newProvider = providerSelect.value;
      const oldDef = providerDefaults[lastProvider] || {};
      const newDef = providerDefaults[newProvider] || {};
      const swap = (input, oldVal, newVal) => {
        if (!input) return;
        const cur = (input.value || '').trim();
        if (!cur || cur === (oldVal || '').trim()) {
          input.value = newVal || '';
        }
      };
      swap(baseUrlInput, oldDef.base_url, newDef.base_url);
      swap(modelInput, oldDef.model, newDef.model);
      swap(visionInput, oldDef.vision_model, newDef.vision_model);
      lastProvider = newProvider;
    });

    const statusEl = form.querySelector('.llm-status');
    const setStatus = (text, kind = '') => {
      statusEl.textContent = text || '';
      statusEl.className = `llm-status${kind ? ` llm-status-${kind}` : ''}`;
    };

    const collect = () => {
      const fd = new FormData(form);
      const out = {};
      ['provider', 'base_url', 'model', 'vision_model', 'api_key'].forEach((k) => {
        const v = String(fd.get(k) || '').trim();
        if (v) out[k] = v;
      });
      const t = Number(fd.get('temperature'));
      if (!Number.isNaN(t)) out.temperature = t;
      const m = Number(fd.get('max_tokens'));
      if (!Number.isNaN(m)) out.max_tokens = m;
      return out;
    };

    const testBtn = document.createElement('button');
    testBtn.type = 'button';
    testBtn.className = 'btn';
    testBtn.textContent = '测试连接';

    const saveBtn = document.createElement('button');
    saveBtn.type = 'submit';
    saveBtn.className = 'btn primary-btn';
    saveBtn.textContent = '保存并重载';

    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'btn btn-ghost';
    cancelBtn.textContent = '取消';

    const modal = C.Modal.open({
      title: 'LLM API 配置',
      body: form,
      footer: [cancelBtn, testBtn, saveBtn],
      size: 'lg',
    });

    cancelBtn.addEventListener('click', () => modal.close('cancel'));

    keyClearBtn.addEventListener('click', async () => {
      if (!window.confirm('确定清空已保存的 API Key？将回退到环境变量（如果有）。')) return;
      setStatus('正在清空...', 'info');
      try {
        const res = await C.api('/api/llm/clear-key', { method: 'POST' });
        if (res && res.config) {
          C._updateLLMBadge(res.config);
          if (bannerWrap) bannerWrap.innerHTML = renderBanner(res.config);
        }
        setStatus('已清空保存的 Key', 'ok');
        C.toast('已清空保存的 API Key', 'ok');
      } catch (err) {
        setStatus(`清空失败：${C.errorMessage(err)}`, 'error');
      }
    });

    testBtn.addEventListener('click', async () => {
      setStatus('正在测试...', 'info');
      testBtn.disabled = true;
      try {
        const res = await C.api('/api/llm/test', {
          method: 'POST',
          body: JSON.stringify(collect()),
        });
        if (res.ok) {
          setStatus(`连接成功 ✓ 模型 ${res.model} 回复: "${(res.reply || '').slice(0, 40)}"`, 'ok');
        } else {
          setStatus(`连接失败：${res.error || '未知错误'}`, 'error');
        }
      } catch (err) {
        setStatus(`测试失败：${C.errorMessage(err)}`, 'error');
      } finally {
        testBtn.disabled = false;
      }
    });

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const payload = collect();
      // 防呆：如果当前没有任何 key（既无环境变量也未保存），用户必须填一个
      if (!config.api_key_set && !payload.api_key) {
        setStatus('请先填入 API Key（当前没有任何已生效的 Key）', 'error');
        keyInput.focus();
        return;
      }
      setStatus('保存中...', 'info');
      saveBtn.disabled = true;
      try {
        const res = await C.api('/api/llm/config', {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        const newCfg = (res && res.config) || {};
        C._updateLLMBadge(newCfg);
        if (newCfg.client_ok) {
          C.toast('LLM 配置已保存并就绪', 'ok');
          modal.close('saved');
        } else {
          // 保存成功但客户端没就绪 —— 保留模态展示错误，避免“无声失败”
          if (bannerWrap) bannerWrap.innerHTML = renderBanner(newCfg);
          setStatus(`配置已保存，但客户端未就绪：${newCfg.client_error || '请检查 Key/URL/Provider'}`, 'error');
          C.toast('配置已保存，但还未就绪', 'error');
        }
      } catch (err) {
        setStatus(`保存失败：${C.errorMessage(err)}`, 'error');
        C.toast(`保存失败：${C.errorMessage(err)}`, 'error');
      } finally {
        saveBtn.disabled = false;
      }
    });
  };

  // 更新 topbar 上的模型角标
  C._updateLLMBadge = function _updateLLMBadge(cfg) {
    const badge = document.querySelector('[data-llm-badge]');
    if (!badge) return;
    badge.classList.remove('llm-badge-ok', 'llm-badge-warn');
    if (cfg && cfg.client_ok) {
      badge.textContent = cfg.model || cfg.provider || 'LLM';
      badge.title = `已就绪\nProvider: ${cfg.provider}\nBase URL: ${cfg.base_url || '-'}`;
      badge.classList.add('llm-badge-ok');
    } else if (cfg && cfg.api_key_set) {
      badge.textContent = `${cfg.model || cfg.provider || 'LLM'} · 未就绪`;
      badge.title = `点击设置查看错误：${cfg.client_error || '未知'}`;
      badge.classList.add('llm-badge-warn');
    } else {
      badge.textContent = '未配置 API Key';
      badge.title = '点击右侧 设置 按钮配置 LLM API Key';
      badge.classList.add('llm-badge-warn');
    }
  };

  // 自动在 topbar 上注入"设置"按钮
  C.mountLLMSettingsButton = function mountLLMSettingsButton() {
    if (document.querySelector('[data-llm-settings-mounted]')) return;
    const host = document.querySelector(
      '.user-bar, .page-head .actions, .topbar, .page-head, header'
    );
    if (!host) return;
    const inUserBar = host.classList.contains('user-bar');

    const wrap = document.createElement('div');
    wrap.className = 'llm-settings-wrap' + (inUserBar ? ' llm-settings-inline' : '');
    wrap.setAttribute('data-llm-settings-mounted', '1');
    wrap.innerHTML = `
      <span class="llm-badge" data-llm-badge title="点击右侧设置按钮配置 LLM">加载中...</span>
      <button type="button" class="btn btn-ghost llm-settings-btn"
              data-llm-settings-btn aria-label="LLM 设置" title="LLM API 设置">
        <span aria-hidden="true">⚙</span>
        <span class="llm-settings-text">设置</span>
      </button>
    `;
    host.appendChild(wrap);

    wrap.querySelector('[data-llm-settings-btn]').addEventListener('click', () => {
      C.openLLMSettings();
    });

    C.api('/api/llm/config')
      .then((cfg) => C._updateLLMBadge(cfg))
      .catch(() => {
        const badge = document.querySelector('[data-llm-badge]');
        if (badge) badge.textContent = '未连接';
      });
  };

  // 页面加载后自动挂载
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => C.mountLLMSettingsButton());
  } else {
    C.mountLLMSettingsButton();
  }
})();
