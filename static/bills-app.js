// 账单解析页前端 —— 使用 window.AgentCommon 共享工具
(() => {
  const C = window.AgentCommon;
  const { $, escapeHtml, fmtNumber, fmtCompact, api, readErrorMessage,
          toast, refreshUsers, resolveCurrentUser, uploadWithProgress,
          renderUploadProgress, renderUploadPending,
          renderMonthlyChart, renderTouChart,
          renderCapacityCallout, renderCapacityTable,
          debounce, skeleton, emptyState,
          enableSortInContainer } = C;

  const els = {
    userSelect: $('user-select'),
    refreshFilesBtn: $('refresh-files-btn'),
    billUploadInput: $('bill-upload-input'),
    uploadStatus: $('upload-status'),
    billFileSelect: $('bill-file-select'),
    billParseMode: $('bill-parse-mode'),
    parseBillsBtn: $('parse-bills-btn'),
    billStatus: $('bill-status'),
    billProgress: $('bill-progress'),
    billSummary: $('bill-summary'),
    billChart: $('bill-chart'),
    billTouChart: $('bill-tou-chart'),
    billTableWrap: $('bill-table-wrap'),
    tableCount: $('table-count'),
    capacityAnalysisBtn: $('capacity-analysis-btn'),
    capacityPanel: $('capacity-panel'),
    capacityStatus: $('capacity-status'),
    capacityBest: $('capacity-best'),
    capacityTableWrap: $('capacity-table-wrap'),
  };

  const urlUser = new URLSearchParams(location.search).get('user');
  let currentUser = urlUser || 'main';
  let latestBills = null;
  let billProgressItems = new Map();

  async function refreshUsersList() {
    currentUser = await refreshUsers(els.userSelect, currentUser);
  }

  async function refreshBills(selection) {
    try {
      const data = await api(`/api/bills?user_id=${encodeURIComponent(currentUser)}`);
      renderBills(data, { selection });
    } catch (e) {
      els.billStatus.innerHTML = `<span style="color:var(--stop)">失败：${escapeHtml(e.message)}</span>`;
      toast(e.message, 'error');
    }
  }

  function selectedFiles() {
    return Array.from(els.billFileSelect.selectedOptions || []).map((opt) => opt.value);
  }

  function setSelectedFiles(files) {
    const wanted = new Set(files || []);
    Array.from(els.billFileSelect.options || []).forEach((opt) => {
      opt.selected = wanted.has(opt.value);
    });
  }

  // ---------- 上传 ----------
  async function uploadBills(fileList) {
    if (!fileList || !fileList.length) return;
    const fd = new FormData();
    for (const file of fileList) fd.append('files', file, file.name);
    fd.append('user_id', currentUser);
    fd.append('from_folder', '0');

    renderUploadProgress(els.uploadStatus, '准备上传', fileList.length, 0, 0, false);
    els.billUploadInput.disabled = true;
    try {
      const result = await uploadWithProgress('/api/upload', fd, (loaded, total, lengthComputable) => {
        renderUploadProgress(els.uploadStatus, '正在上传', fileList.length, loaded, total, lengthComputable);
        if (lengthComputable && loaded >= total) {
          renderUploadPending(els.uploadStatus, '上传完成，正在保存');
        }
      });
      const copied = result.copied || [];
      const skipped = result.skipped || [];
      const errors = result.errors || [];
      els.uploadStatus.classList.remove('upload-progress');
      els.uploadStatus.innerHTML =
        `<span style="color:var(--ok)">✓ 已上传 ${copied.length}</span>` +
        (skipped.length ? ` <span class="muted">跳过 ${skipped.length}</span>` : '') +
        (errors.length ? ` <span style="color:var(--stop)">错误 ${errors.length}</span>` : '');
      await refreshBills(copied);
      if (copied.length) {
        setSelectedFiles(copied);
        toast('账单已上传', 'ok');
      }
    } catch (e) {
      els.uploadStatus.classList.remove('upload-progress');
      els.uploadStatus.innerHTML = `<span style="color:var(--stop)">❌ ${escapeHtml(e.message)}</span>`;
      toast(e.message, 'error');
    } finally {
      els.billUploadInput.disabled = false;
      els.billUploadInput.value = '';
    }
  }

  // ---------- 解析（SSE 流式） ----------
  async function parseBills() {
    const files = selectedFiles();
    const initialLabel = files.length
      ? `准备解析 ${files.length} 个文件`
      : '准备解析 input/ 目录全部账单文件';

    els.parseBillsBtn.disabled = true;
    clearCapacityAnalysis();
    resetBillProgress(initialLabel);
    els.billStatus.textContent = initialLabel;
    try {
      const res = await fetch('/api/bills/parse/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_id: currentUser,
          files,
          mode: els.billParseMode.value || 'auto',
        }),
      });
      if (!res.ok) throw new Error(await readErrorMessage(res));
      if (!res.body) throw new Error('浏览器不支持流式进度读取');
      await readBillParseStream(res);
      toast('账单解析完成', 'ok');
    } catch (e) {
      els.billStatus.innerHTML = `<span style="color:var(--stop)">失败：${escapeHtml(e.message)}</span>`;
      markBillProgressError(e.message);
      toast(e.message, 'error');
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
        consumeStreamBlock(block);
      }
    }
    if (buf.trim()) consumeStreamBlock(buf);
  }

  function consumeStreamBlock(block) {
    const lines = block.split('\n').filter((line) => line.startsWith('data:'));
    if (!lines.length) return;
    const text = lines.map((line) => line.slice(5).trim()).join('');
    try {
      handleBillParseEvent(JSON.parse(text));
    } catch (_) {
      /* ignore */
    }
  }

  function handleBillParseEvent(ev) {
    if (ev.type === 'start') {
      resetBillProgress(`开始解析 ${ev.total || 0} 个文件`, ev.total || 0);
      for (const file of ev.files || []) setBillFileProgress(file, 'queued', '等待解析');
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
    billProgressItems = new Map();
    els.billProgress.classList.remove('hidden');
    els.billProgress.innerHTML =
      `<div class="bill-progress-head"><span>${escapeHtml(label)}</span><b>0%</b></div>` +
      `<div class="bill-progress-track"><span style="width:0%"></span></div>` +
      `<div class="bill-progress-list"></div>`;
    if (total) updateBillProgress(0, `等待解析 ${total} 个文件`);
  }

  function updateBillProgress(progress, label) {
    const pct = Math.min(100, Math.max(0, Number(progress || 0)));
    const head = els.billProgress.querySelector('.bill-progress-head span');
    const num = els.billProgress.querySelector('.bill-progress-head b');
    const bar = els.billProgress.querySelector('.bill-progress-track span');
    if (head) head.textContent = label;
    if (num) num.textContent = `${Math.round(pct)}%`;
    if (bar) bar.style.width = `${pct}%`;
    els.billStatus.textContent = label;
  }

  function setBillFileProgress(file, status, message) {
    if (!file) return;
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
    if (els.billProgress.classList.contains('hidden')) resetBillProgress('解析中断');
    updateBillProgress(100, `解析中断：${message}`);
  }

  // ---------- 渲染 ----------
  function renderBills(data, opts = {}) {
    latestBills = data || {};
    const files = latestBills.available_files || [];
    const currentSelection = new Set(selectedFiles());
    const targetSelection = opts.selection
      ? new Set(opts.selection)
      : (currentSelection.size ? currentSelection : new Set(latestBills.files || []));

    els.billFileSelect.innerHTML = files.map((name) =>
      `<option value="${escapeHtml(name)}" ${targetSelection.has(name) ? 'selected' : ''}>${escapeHtml(name)}</option>`
    ).join('');

    const rows = latestBills.records || [];
    if (!opts.keepProgress) els.billProgress.classList.add('hidden');

    if (!files.length) {
      els.billStatus.textContent = 'input/ 目录暂无可解析账单，请先上传 PDF、Excel 或图片账单。';
    } else if (!rows.length) {
      els.billStatus.textContent = '请选择账单文件后点击开始解析。未选择时会解析全部账单文件。';
    } else {
      const parser = latestBills.parser ? ` · ${latestBills.parser}` : '';
      const fileText = latestBills.files && latestBills.files.length ? ` · ${latestBills.files.length} 个文件` : '';
      els.billStatus.textContent = `${latestBills.msg || '已加载账单数据'}${fileText}${parser}`;
    }

    renderBillSummary(latestBills.summary || {});
    renderBillTable(rows, latestBills.columns || []);
    requestAnimationFrame(() => renderBillCharts(rows, latestBills.summary || {}));
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
    renderMonthlyChart(els.billChart, rows, { minWidth: 300, minHeight: 210 });
    renderTouChart(els.billTouChart, summary.tou || {}, { minWidth: 300, minHeight: 160 });
  }

  function renderBillTable(rows, columns) {
    els.tableCount.textContent = `${rows.length || 0} 条`;
    if (!rows.length) {
      els.billTableWrap.innerHTML = emptyState('暂无账单表格数据', '上传并解析账单后将显示明细');
      return;
    }
    const cols = columns.length ? columns : Object.keys(rows[0]);
    els.billTableWrap.innerHTML =
      '<table class="bill-table"><thead><tr>' +
      cols.map((col) => `<th>${escapeHtml(col)}</th>`).join('') +
      '</tr></thead><tbody>' +
      rows.map((row) => '<tr>' + cols.map((col) => {
        const value = row[col];
        const isNum = value !== '' && value != null && !Number.isNaN(Number(value));
        const digits = col.includes('平均电价') || col === '功率因数' ? 4 : 2;
        const text = isNum && col !== '月份'
          ? fmtNumber(Number(value), digits)
          : String(value ?? '');
        return `<td class="${isNum && col !== '月份' ? 'num' : ''}">${escapeHtml(text)}</td>`;
      }).join('') + '</tr>').join('') +
      '</tbody></table>';
    enableSortInContainer(els.billTableWrap);
  }

  // ---------- 容量分析 ----------
  async function analyzeCapacity() {
    const rows = latestBills && latestBills.records ? latestBills.records : [];
    if (!rows.length) {
      toast('请先解析账单数据', 'error');
      return;
    }

    els.capacityAnalysisBtn.disabled = true;
    els.capacityPanel.classList.remove('hidden');
    els.capacityStatus.textContent = '分析中...';
    els.capacityBest.innerHTML = '<div class="muted">正在计算多个储能容量组合...</div>';
    els.capacityTableWrap.innerHTML = skeleton(6);
    try {
      const data = await api('/api/storage/capacity-analysis', {
        method: 'POST',
        body: JSON.stringify({ user_id: currentUser }),
      });
      renderCapacityAnalysis(data);
      toast('容量分析完成', 'ok');
    } catch (e) {
      els.capacityStatus.textContent = '分析失败';
      els.capacityBest.innerHTML = `<div style="color:var(--stop)">失败：${escapeHtml(e.message)}</div>`;
      els.capacityTableWrap.innerHTML = '';
      toast(e.message, 'error');
    } finally {
      els.capacityAnalysisBtn.disabled = false;
    }
  }

  function clearCapacityAnalysis() {
    els.capacityPanel.classList.add('hidden');
    els.capacityStatus.textContent = '未分析';
    els.capacityBest.innerHTML = '';
    els.capacityTableWrap.innerHTML = '';
  }

  function renderCapacityAnalysis(data) {
    const d = data || {};
    const rows = d.results || [];
    els.capacityPanel.classList.remove('hidden');
    els.capacityStatus.textContent = `${rows.length} 个组合 · 正收益 ${d.positive_count || 0} 个`;
    els.capacityBest.innerHTML = renderCapacityCallout(d.best || {}, d.scoring_basis || '');
    els.capacityTableWrap.innerHTML = renderCapacityTable(rows);
    enableSortInContainer(els.capacityTableWrap);
  }

  // ---------- 事件绑定 ----------
  function bind() {
    els.refreshFilesBtn.addEventListener('click', () => refreshBills());
    els.parseBillsBtn.addEventListener('click', parseBills);
    els.capacityAnalysisBtn.addEventListener('click', analyzeCapacity);
    els.billUploadInput.addEventListener('change', () => uploadBills(els.billUploadInput.files));
    els.userSelect.addEventListener('change', () => {
      currentUser = els.userSelect.value || 'main';
      clearCapacityAnalysis();
      refreshBills();
      toast(`已切换到用户：${currentUser}`, 'ok');
    });
    const onResize = debounce(() => {
      if (!latestBills) return;
      renderBillCharts(latestBills.records || [], latestBills.summary || {});
    }, 140);
    window.addEventListener('resize', onResize);
  }

  async function init() {
    bind();
    try {
      currentUser = await resolveCurrentUser(urlUser, currentUser);
      await refreshUsersList();
      await refreshBills();
    } catch (e) {
      els.billStatus.innerHTML = `<span style="color:var(--stop)">初始化失败：${escapeHtml(e.message)}</span>`;
      toast(e.message, 'error');
    }
  }

  document.addEventListener('DOMContentLoaded', init);
})();
