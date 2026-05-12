(() => {
  const $ = (id) => document.getElementById(id);
  const els = {
    userSelect: $("user-select"),
    refreshFilesBtn: $("refresh-files-btn"),
    billUploadInput: $("bill-upload-input"),
    uploadStatus: $("upload-status"),
    billFileSelect: $("bill-file-select"),
    billParseMode: $("bill-parse-mode"),
    parseBillsBtn: $("parse-bills-btn"),
    billStatus: $("bill-status"),
    billProgress: $("bill-progress"),
    billSummary: $("bill-summary"),
    billChart: $("bill-chart"),
    billTouChart: $("bill-tou-chart"),
    billTableWrap: $("bill-table-wrap"),
    tableCount: $("table-count"),
    capacityAnalysisBtn: $("capacity-analysis-btn"),
    capacityPanel: $("capacity-panel"),
    capacityStatus: $("capacity-status"),
    capacityBest: $("capacity-best"),
    capacityTableWrap: $("capacity-table-wrap"),
    toast: $("toast"),
  };

  const urlUser = new URLSearchParams(location.search).get("user");
  let currentUser = urlUser || "main";
  let latestBills = null;
  let latestCapacityAnalysis = null;
  let billProgressItems = new Map();
  let resizeTimer = null;

  function toast(msg, type = "") {
    els.toast.textContent = msg;
    els.toast.className = `toast show ${type}`;
    setTimeout(() => {
      els.toast.className = "toast";
    }, 2400);
  }

  function escapeHtml(value) {
    if (value == null) return "";
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmtNumber(value, digits = 0) {
    const n = Number(value || 0);
    return n.toLocaleString("zh-CN", {
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

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    const digits = value >= 100 || unit === 0 ? 0 : 1;
    return `${value.toFixed(digits)} ${units[unit]}`;
  }

  function formatHttpError(status, statusText, text) {
    if (status === 413) return "文件过大，请控制在 200MB 以内后重试。";

    try {
      const data = JSON.parse(text || "");
      const detail = data.detail || data.error || data.message;
      if (detail) return String(detail);
    } catch (_) {
      // Fall through to plain-text cleanup.
    }

    const plain = String(text || "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
    const suffix = plain ? `：${plain.slice(0, 180)}` : "";
    return `${status || "网络错误"} ${statusText || "请求失败"}${suffix}`;
  }

  async function readErrorMessage(res) {
    const text = await res.text().catch(() => "");
    return formatHttpError(res.status, res.statusText, text);
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (!res.ok) throw new Error(await readErrorMessage(res));
    return res.json();
  }

  async function refreshUsers() {
    const data = await api("/api/users");
    const users = data.users || [];
    if (!users.includes(currentUser)) currentUser = users[0] || "main";
    els.userSelect.innerHTML = users.map((user) =>
      `<option value="${escapeHtml(user)}" ${user === currentUser ? "selected" : ""}>${escapeHtml(user)}</option>`
    ).join("");
  }

  async function resolveCurrentUser() {
    if (urlUser) return;
    const data = await api("/api/me");
    currentUser = data.user_id || currentUser || "main";
  }

  async function refreshBills(selection = null) {
    try {
      const data = await api(`/api/bills?user_id=${encodeURIComponent(currentUser)}`);
      renderBills(data, { selection });
    } catch (e) {
      els.billStatus.innerHTML = `<span style="color:var(--stop)">失败：${escapeHtml(e.message)}</span>`;
      toast(e.message, "error");
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

  async function uploadBills(fileList) {
    if (!fileList || !fileList.length) return;
    const fd = new FormData();
    for (const file of fileList) fd.append("files", file, file.name);
    fd.append("user_id", currentUser);
    fd.append("from_folder", "0");

    renderUploadProgress("准备上传", fileList.length, 0, 0, false);
    els.billUploadInput.disabled = true;
    try {
      const result = await uploadWithProgress("/api/upload", fd, (loaded, total, lengthComputable) => {
        renderUploadProgress("正在上传", fileList.length, loaded, total, lengthComputable);
        if (lengthComputable && loaded >= total) {
          renderUploadPending("上传完成，正在保存");
        }
      });
      const copied = result.copied || [];
      const skipped = result.skipped || [];
      const errors = result.errors || [];
      els.uploadStatus.classList.remove("upload-progress");
      els.uploadStatus.innerHTML =
        `<span style="color:var(--ok)">已上传 ${copied.length}</span>` +
        (skipped.length ? ` <span class="muted">跳过 ${skipped.length}</span>` : "") +
        (errors.length ? ` <span style="color:var(--stop)">错误 ${errors.length}</span>` : "");
      await refreshBills(copied);
      if (copied.length) {
        setSelectedFiles(copied);
        toast("账单已上传", "ok");
      }
    } catch (e) {
      els.uploadStatus.classList.remove("upload-progress");
      els.uploadStatus.innerHTML = `<span style="color:var(--stop)">失败：${escapeHtml(e.message)}</span>`;
      toast(e.message, "error");
    } finally {
      els.billUploadInput.disabled = false;
      els.billUploadInput.value = "";
    }
  }

  function uploadWithProgress(path, fd, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", path);
      xhr.upload.onprogress = (ev) => {
        onProgress(ev.loaded || 0, ev.total || 0, ev.lengthComputable);
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            resolve(JSON.parse(xhr.responseText || "{}"));
          } catch (_) {
            reject(new Error("服务器返回格式异常"));
          }
          return;
        }
        reject(new Error(formatHttpError(xhr.status, xhr.statusText, xhr.responseText)));
      };
      xhr.onerror = () => reject(new Error("网络连接失败，请稍后重试"));
      xhr.onabort = () => reject(new Error("上传已取消"));
      xhr.send(fd);
    });
  }

  function renderUploadProgress(label, fileCount, loaded, total, lengthComputable) {
    const pct = lengthComputable && total > 0
      ? Math.min(100, Math.max(0, Math.round((loaded / total) * 100)))
      : 0;
    const sizeText = lengthComputable && total > 0
      ? `${formatBytes(loaded)} / ${formatBytes(total)}`
      : `已上传 ${formatBytes(loaded)}`;
    const pctText = lengthComputable && total > 0 ? `${pct}%` : "上传中";
    els.uploadStatus.classList.add("upload-progress");
    els.uploadStatus.innerHTML =
      `<span class="upload-progress-head">` +
        `<span>${escapeHtml(label)} ${fileCount} 个文件</span>` +
        `<span>${escapeHtml(pctText)}</span>` +
      `</span>` +
      `<span class="upload-progress-track"><span style="width:${pct}%"></span></span>` +
      `<span class="upload-progress-meta">${escapeHtml(sizeText)}</span>`;
  }

  function renderUploadPending(text) {
    els.uploadStatus.classList.add("upload-progress");
    els.uploadStatus.innerHTML =
      `<span class="upload-progress-head">` +
        `<span>${escapeHtml(text)}</span>` +
        `<span>100%</span>` +
      `</span>` +
      `<span class="upload-progress-track"><span style="width:100%"></span></span>` +
      `<span class="upload-progress-meta">文件已传完，等待服务器处理</span>`;
  }

  async function parseBills() {
    const files = selectedFiles();
    const initialLabel = files.length
      ? `准备解析 ${files.length} 个文件`
      : "准备解析 input/ 目录全部账单文件";

    els.parseBillsBtn.disabled = true;
    clearCapacityAnalysis();
    resetBillProgress(initialLabel);
    els.billStatus.textContent = initialLabel;
    try {
      const res = await fetch("/api/bills/parse/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: currentUser,
          files,
          mode: els.billParseMode.value || "auto",
        }),
      });
      if (!res.ok) throw new Error(await readErrorMessage(res));
      if (!res.body) throw new Error("浏览器不支持流式进度读取");
      await readBillParseStream(res);
      toast("账单解析完成", "ok");
    } catch (e) {
      els.billStatus.innerHTML = `<span style="color:var(--stop)">失败：${escapeHtml(e.message)}</span>`;
      markBillProgressError(e.message);
      toast(e.message, "error");
    } finally {
      els.parseBillsBtn.disabled = false;
    }
  }

  async function readBillParseStream(res) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        consumeStreamBlock(block);
      }
    }
    if (buf.trim()) consumeStreamBlock(buf);
  }

  function consumeStreamBlock(block) {
    const lines = block.split("\n").filter((line) => line.startsWith("data:"));
    if (!lines.length) return;
    const text = lines.map((line) => line.slice(5).trim()).join("");
    try {
      handleBillParseEvent(JSON.parse(text));
    } catch (_) {
      // Ignore malformed stream frames.
    }
  }

  function handleBillParseEvent(ev) {
    if (ev.type === "start") {
      resetBillProgress(`开始解析 ${ev.total || 0} 个文件`, ev.total || 0);
      for (const file of ev.files || []) setBillFileProgress(file, "queued", "等待解析");
    } else if (ev.type === "file_start") {
      updateBillProgress(ev.progress || 0, `正在解析 ${ev.index}/${ev.total}：${ev.file || ""}`);
      setBillFileProgress(ev.file, "running", `解析中 (${ev.index}/${ev.total})`);
    } else if (ev.type === "file_done") {
      updateBillProgress(ev.progress || 0, `已完成 ${ev.index}/${ev.total}：${ev.file || ""}`);
      setBillFileProgress(ev.file, "done", `完成，新增 ${ev.rows_added || 0} 条 · ${ev.parser || "-"}`);
      if (ev.payload) renderBills(ev.payload, { keepProgress: true });
    } else if (ev.type === "file_error") {
      updateBillProgress(ev.progress || 0, `解析失败 ${ev.index}/${ev.total}：${ev.file || ""}`);
      setBillFileProgress(ev.file, "error", ev.message || "解析失败");
    } else if (ev.type === "done") {
      updateBillProgress(100, `解析完成：成功 ${ev.success || 0} 个，失败 ${ev.failed || 0} 个`);
      if (ev.payload) renderBills(ev.payload, { keepProgress: true });
    }
  }

  function resetBillProgress(label, total = 0) {
    billProgressItems = new Map();
    els.billProgress.classList.remove("hidden");
    els.billProgress.innerHTML =
      `<div class="bill-progress-head"><span>${escapeHtml(label)}</span><b>0%</b></div>` +
      `<div class="bill-progress-track"><span style="width:0%"></span></div>` +
      `<div class="bill-progress-list"></div>`;
    if (total) updateBillProgress(0, `等待解析 ${total} 个文件`);
  }

  function updateBillProgress(progress, label) {
    const pct = Math.min(100, Math.max(0, Number(progress || 0)));
    const head = els.billProgress.querySelector(".bill-progress-head span");
    const num = els.billProgress.querySelector(".bill-progress-head b");
    const bar = els.billProgress.querySelector(".bill-progress-track span");
    if (head) head.textContent = label;
    if (num) num.textContent = `${Math.round(pct)}%`;
    if (bar) bar.style.width = `${pct}%`;
    els.billStatus.textContent = label;
  }

  function setBillFileProgress(file, status, message) {
    if (!file) return;
    billProgressItems.set(file, { status, message });
    const list = els.billProgress.querySelector(".bill-progress-list");
    if (!list) return;
    list.innerHTML = Array.from(billProgressItems.entries()).map(([name, item]) =>
      `<div class="bill-progress-item ${escapeHtml(item.status)}">` +
        `<span class="name">${escapeHtml(name)}</span>` +
        `<span class="msg">${escapeHtml(item.message || "")}</span>` +
      `</div>`
    ).join("");
  }

  function markBillProgressError(message) {
    if (els.billProgress.classList.contains("hidden")) resetBillProgress("解析中断");
    updateBillProgress(100, `解析中断：${message}`);
  }

  function renderBills(data, opts = {}) {
    latestBills = data || {};
    const files = latestBills.available_files || [];
    const currentSelection = new Set(selectedFiles());
    const targetSelection = opts.selection
      ? new Set(opts.selection)
      : (currentSelection.size ? currentSelection : new Set(latestBills.files || []));

    els.billFileSelect.innerHTML = files.map((name) =>
      `<option value="${escapeHtml(name)}" ${targetSelection.has(name) ? "selected" : ""}>${escapeHtml(name)}</option>`
    ).join("");

    const rows = latestBills.records || [];
    if (!opts.keepProgress) els.billProgress.classList.add("hidden");

    if (!files.length) {
      els.billStatus.textContent = "input/ 目录暂无可解析账单，请先上传 PDF、Excel 或图片账单。";
    } else if (!rows.length) {
      els.billStatus.textContent = "请选择账单文件后点击开始解析。未选择时会解析全部账单文件。";
    } else {
      const parser = latestBills.parser ? ` · ${latestBills.parser}` : "";
      const fileText = latestBills.files && latestBills.files.length ? ` · ${latestBills.files.length} 个文件` : "";
      els.billStatus.textContent = `${latestBills.msg || "已加载账单数据"}${fileText}${parser}`;
    }

    renderBillSummary(latestBills.summary || {});
    renderBillTable(rows, latestBills.columns || []);
    requestAnimationFrame(() => renderBillCharts(rows, latestBills.summary || {}));
  }

  async function analyzeCapacity() {
    const rows = latestBills && latestBills.records ? latestBills.records : [];
    if (!rows.length) {
      toast("请先解析账单数据", "error");
      return;
    }

    els.capacityAnalysisBtn.disabled = true;
    els.capacityPanel.classList.remove("hidden");
    els.capacityStatus.textContent = "分析中...";
    els.capacityBest.innerHTML = '<div class="muted">正在计算多个储能容量组合...</div>';
    els.capacityTableWrap.innerHTML = "";
    try {
      const data = await api("/api/storage/capacity-analysis", {
        method: "POST",
        body: JSON.stringify({ user_id: currentUser }),
      });
      renderCapacityAnalysis(data);
      toast("容量分析完成", "ok");
    } catch (e) {
      els.capacityStatus.textContent = "分析失败";
      els.capacityBest.innerHTML = `<div style="color:var(--stop)">失败：${escapeHtml(e.message)}</div>`;
      toast(e.message, "error");
    } finally {
      els.capacityAnalysisBtn.disabled = false;
    }
  }

  function clearCapacityAnalysis() {
    latestCapacityAnalysis = null;
    els.capacityPanel.classList.add("hidden");
    els.capacityStatus.textContent = "未分析";
    els.capacityBest.innerHTML = "";
    els.capacityTableWrap.innerHTML = "";
  }

  function renderCapacityAnalysis(data) {
    latestCapacityAnalysis = data || {};
    const best = latestCapacityAnalysis.best || {};
    const rows = latestCapacityAnalysis.results || [];
    els.capacityPanel.classList.remove("hidden");
    els.capacityStatus.textContent = `${rows.length} 个组合 · 正收益 ${latestCapacityAnalysis.positive_count || 0} 个`;
    els.capacityBest.innerHTML =
      `<div class="capacity-callout">` +
        `<div>` +
          `<span>推荐组合</span>` +
          `<b>${fmtNumber(best.battery_capacity_kwh || 0, 0)} kWh / ${fmtNumber(best.inverter_power_kw || 0, 0)} kW</b>` +
        `</div>` +
        `<div><span>储能时长</span><b>${fmtNumber(best.duration_hours || 0, 2)} h</b></div>` +
        `<div><span>总投资</span><b>${fmtCompact(best.total_investment_yuan)} 元</b></div>` +
        `<div><span>年综合收益</span><b>${fmtCompact(best.annual_revenue_yuan)} 元</b></div>` +
        `<div><span>峰谷套利</span><b>${fmtCompact(best.arbitrage_revenue_yuan)} 元</b></div>` +
        `<div><span>需量收益</span><b>${fmtCompact(best.demand_revenue_yuan)} 元</b></div>` +
        `<div><span>收益/容量</span><b>${fmtNumber(best.annual_revenue_per_kwh || 0, 0)} 元/kWh·年</b></div>` +
        `<div><span>回收期</span><b>${best.payback_years == null ? "-" : `${fmtNumber(best.payback_years, 2)} 年`}</b></div>` +
      `</div>` +
      `<p class="capacity-note">${escapeHtml(latestCapacityAnalysis.scoring_basis || "")}</p>`;

    if (!rows.length) {
      els.capacityTableWrap.innerHTML = '<div class="muted" style="padding:14px">暂无容量分析结果</div>';
      return;
    }

    const cols = [
      ["rank", "排名"],
      ["name", "组合"],
      ["battery_capacity_kwh", "电池容量(kWh)"],
      ["inverter_power_kw", "PCS功率(kW)"],
      ["duration_hours", "时长(h)"],
      ["total_investment_yuan", "投资(元)"],
      ["annual_revenue_yuan", "年综合收益(元)"],
      ["arbitrage_revenue_yuan", "峰谷套利(元)"],
      ["demand_revenue_yuan", "需量收益(元)"],
      ["annual_revenue_per_kwh", "收益/容量"],
      ["marginal_revenue_per_kwh", "边际收益"],
      ["payback_years", "回收期(年)"],
      ["utilization_ratio", "利用率"],
    ];
    els.capacityTableWrap.innerHTML =
      '<table class="capacity-table"><thead><tr>' +
      cols.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("") +
      '</tr></thead><tbody>' +
      rows.map((row) => `<tr class="${row.is_best ? "best-row" : ""}">` + cols.map(([key]) => {
        let value = row[key];
        if (value == null) value = "-";
        else if (key === "rank") value = row.is_best ? "推荐" : value;
        else if (typeof value === "number") {
          const digits = ["duration_hours", "payback_years", "annual_revenue_per_kwh", "marginal_revenue_per_kwh"].includes(key) ? 2 : (key === "utilization_ratio" ? 4 : 0);
          value = fmtNumber(value, digits);
        }
        const isNum = typeof row[key] === "number" && key !== "rank";
        return `<td class="${isNum ? "num" : ""}">${escapeHtml(value)}</td>`;
      }).join("") + "</tr>").join("") +
      "</tbody></table>";
  }

  function renderBillSummary(summary) {
    const cards = [
      ["记录数", `${summary.row_count || 0} 条`],
      ["总电量", `${fmtCompact(summary.total_kwh)} kWh`],
      ["总电费", `${fmtCompact(summary.total_amount)} 元`],
      ["平均电价", `${fmtNumber(summary.avg_unit_price || 0, 4)} 元/kWh`],
      ["最大需量", `${fmtNumber(summary.max_demand_kw || 0, 2)} kW`],
      ["账期", summary.start_month ? `${summary.start_month} 至 ${summary.end_month}` : "-"],
    ];
    els.billSummary.innerHTML = cards.map(([label, value]) =>
      `<div class="metric"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`
    ).join("");
  }

  function renderBillCharts(rows, summary) {
    renderMonthlyChart(els.billChart, rows);
    renderTouChart(els.billTouChart, summary.tou || {});
  }

  function renderMonthlyChart(canvas, rows) {
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(300, Math.round(rect.width || canvas.parentElement.clientWidth || 360));
    const height = Math.max(210, Number(canvas.getAttribute("height")) || 240);
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    if (!rows.length) {
      drawEmptyChart(ctx, width, height, "暂无账单数据");
      return;
    }

    const labels = rows.map((row) => String(row["月份"] || "-"));
    const kwh = rows.map((row) => Number(row["总电量(kWh)"] || 0));
    const amount = rows.map((row) => Number(row["总电费(元)"] || 0));
    const maxKwh = Math.max(...kwh, 1);
    const maxAmount = Math.max(...amount, 1);
    const pad = { left: 48, right: 22, top: 26, bottom: 38 };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const groupW = plotW / Math.max(rows.length, 1);
    const barW = Math.max(6, Math.min(22, groupW * 0.28));

    drawAxes(ctx, pad, width, height);
    drawGrid(ctx, pad, width, height, 4);
    ctx.font = "11px sans-serif";
    rows.forEach((_, idx) => {
      const center = pad.left + groupW * idx + groupW / 2;
      const kwhH = (kwh[idx] / maxKwh) * plotH;
      const amountH = (amount[idx] / maxAmount) * plotH;
      ctx.fillStyle = "#4f8cff";
      ctx.fillRect(center - barW - 2, pad.top + plotH - kwhH, barW, kwhH);
      ctx.fillStyle = "#34c759";
      ctx.fillRect(center + 2, pad.top + plotH - amountH, barW, amountH);

      if (rows.length <= 12 || idx % Math.ceil(rows.length / 8) === 0) {
        ctx.save();
        ctx.translate(center, height - 12);
        ctx.rotate(-Math.PI / 8);
        ctx.fillStyle = "#9aa0ad";
        ctx.textAlign = "right";
        ctx.fillText(labels[idx].slice(2), 0, 0);
        ctx.restore();
      }
    });

    ctx.fillStyle = "#9aa0ad";
    ctx.font = "11px sans-serif";
    ctx.fillText(fmtCompact(maxKwh), 8, pad.top + 4);
    drawLegend(ctx, width, [["电量", "#4f8cff"], ["电费", "#34c759"]]);
  }

  function renderTouChart(canvas, tou) {
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(300, Math.round(rect.width || canvas.parentElement.clientWidth || 320));
    const height = Math.max(160, Number(canvas.getAttribute("height")) || 180);
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const items = [
      ["尖峰", Number(tou.peak || 0), "#e3554f"],
      ["高峰", Number(tou.high || 0), "#f5a623"],
      ["平段", Number(tou.flat || 0), "#4f8cff"],
      ["谷段", Number(tou.valley || 0), "#34c759"],
    ];
    const total = items.reduce((sum, item) => sum + item[1], 0);
    if (!total) {
      drawEmptyChart(ctx, width, height, "暂无分时电量");
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

    ctx.font = "12px sans-serif";
    const colW = Math.max(118, Math.floor((width - 32) / 2));
    items.forEach(([label, value, color], idx) => {
      const lx = 16 + (idx % 2) * colW;
      const ly = 84 + Math.floor(idx / 2) * 30;
      ctx.fillStyle = color;
      ctx.fillRect(lx, ly - 9, 9, 9);
      ctx.fillStyle = "#c9d3e0";
      ctx.fillText(`${label} ${fmtNumber(value / total * 100, 1)}%`, lx + 14, ly);
      ctx.fillStyle = "#9aa0ad";
      ctx.fillText(`${fmtCompact(value)} kWh`, lx + 14, ly + 15);
    });
  }

  function drawAxes(ctx, pad, width, height) {
    ctx.strokeStyle = "#303646";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(pad.left, pad.top);
    ctx.lineTo(pad.left, height - pad.bottom);
    ctx.lineTo(width - pad.right, height - pad.bottom);
    ctx.stroke();
  }

  function drawGrid(ctx, pad, width, height, count) {
    ctx.strokeStyle = "#252b38";
    ctx.lineWidth = 1;
    for (let i = 1; i <= count; i += 1) {
      const y = pad.top + ((height - pad.top - pad.bottom) / count) * i;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(width - pad.right, y);
      ctx.stroke();
    }
  }

  function drawLegend(ctx, width, items) {
    let x = Math.max(110, width - 132);
    ctx.font = "12px sans-serif";
    for (const [label, color] of items) {
      ctx.fillStyle = color;
      ctx.fillRect(x, 12, 9, 9);
      ctx.fillStyle = "#c9d3e0";
      ctx.fillText(label, x + 14, 21);
      x += 56;
    }
  }

  function drawEmptyChart(ctx, width, height, text) {
    ctx.fillStyle = "#9aa0ad";
    ctx.font = "13px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(text, width / 2, height / 2);
    ctx.textAlign = "left";
  }

  function renderBillTable(rows, columns) {
    els.tableCount.textContent = `${rows.length || 0} 条`;
    if (!rows.length) {
      els.billTableWrap.innerHTML = '<div class="muted" style="padding:14px">暂无账单表格数据</div>';
      return;
    }
    const cols = columns.length ? columns : Object.keys(rows[0]);
    els.billTableWrap.innerHTML =
      '<table class="bill-table"><thead><tr>' +
      cols.map((col) => `<th>${escapeHtml(col)}</th>`).join("") +
      '</tr></thead><tbody>' +
      rows.map((row) => '<tr>' + cols.map((col) => {
        const value = row[col];
        const isNum = value !== "" && value != null && !Number.isNaN(Number(value));
        const digits = col.includes("平均电价") || col === "功率因数" ? 4 : 2;
        const text = isNum && col !== "月份"
          ? fmtNumber(Number(value), digits)
          : String(value ?? "");
        return `<td class="${isNum && col !== "月份" ? "num" : ""}">${escapeHtml(text)}</td>`;
      }).join("") + "</tr>").join("") +
      "</tbody></table>";
  }

  function bind() {
    els.refreshFilesBtn.addEventListener("click", () => refreshBills());
    els.parseBillsBtn.addEventListener("click", parseBills);
    els.capacityAnalysisBtn.addEventListener("click", analyzeCapacity);
    els.billUploadInput.addEventListener("change", () => uploadBills(els.billUploadInput.files));
    els.userSelect.addEventListener("change", () => {
      currentUser = els.userSelect.value || "main";
      clearCapacityAnalysis();
      refreshBills();
      toast(`已切换到用户：${currentUser}`, "ok");
    });
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (!latestBills) return;
        renderBillCharts(latestBills.records || [], latestBills.summary || {});
      }, 120);
    });
  }

  async function init() {
    bind();
    try {
      await resolveCurrentUser();
      await refreshUsers();
      await refreshBills();
    } catch (e) {
      els.billStatus.innerHTML = `<span style="color:var(--stop)">初始化失败：${escapeHtml(e.message)}</span>`;
      toast(e.message, "error");
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
