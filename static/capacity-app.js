(() => {
  const $ = (id) => document.getElementById(id);
  const els = {
    userSelect: $("user-select"),
    refreshBtn: $("refresh-btn"),
    analyzeBtn: $("analyze-btn"),
    capacityInput: $("capacity-input"),
    durationInput: $("duration-input"),
    status: $("status"),
    loadSummary: $("load-summary"),
    capacityStatus: $("capacity-status"),
    capacityBest: $("capacity-best"),
    capacityTableWrap: $("capacity-table-wrap"),
    toast: $("toast"),
  };
  const urlUser = new URLSearchParams(location.search).get("user");
  let currentUser = urlUser || "main";

  function escapeHtml(value) {
    if (value == null) return "";
    return String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function fmtNumber(value, digits = 0) {
    const n = Number(value || 0);
    return n.toLocaleString("zh-CN", { maximumFractionDigits: digits, minimumFractionDigits: digits });
  }

  function fmtCompact(value) {
    const n = Number(value || 0);
    if (Math.abs(n) >= 100000000) return `${fmtNumber(n / 100000000, 2)}亿`;
    if (Math.abs(n) >= 10000) return `${fmtNumber(n / 10000, 2)}万`;
    return fmtNumber(n, 0);
  }

  function toast(message, type = "") {
    els.toast.textContent = message;
    els.toast.className = `toast show ${type}`;
    setTimeout(() => { els.toast.className = "toast"; }, 2200);
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      let message = text.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
      try { message = JSON.parse(text).detail || JSON.parse(text).message || message; } catch (_) {}
      throw new Error(message || `${res.status} ${res.statusText}`);
    }
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

  function parseList(text) {
    return String(text || "").split(/[,，\s]+/).map((x) => Number(x)).filter((x) => Number.isFinite(x) && x > 0);
  }

  async function loadCapacity() {
    els.status.textContent = "正在加载容量分析结果...";
    try {
      const data = await api(`/api/storage/capacity-analysis?user_id=${encodeURIComponent(currentUser)}`);
      renderCapacity(data);
      els.status.textContent = data.saved_at ? `已加载本地历史结果：${data.saved_at}` : (data.msg || "已加载");
    } catch (e) {
      els.status.textContent = `加载失败：${e.message}`;
      toast(e.message, "error");
    }
  }

  async function analyzeCapacity() {
    els.analyzeBtn.disabled = true;
    els.status.textContent = "正在扫描容量组合...";
    try {
      const body = { user_id: currentUser };
      const capacities = parseList(els.capacityInput.value);
      const durations = parseList(els.durationInput.value);
      if (capacities.length) body.capacities_kwh = capacities;
      if (durations.length) body.durations_hours = durations;
      const data = await api("/api/storage/capacity-analysis", {
        method: "POST",
        body: JSON.stringify(body),
      });
      renderCapacity(data);
      els.status.textContent = data.saved_at ? `分析完成并已保存：${data.saved_at}` : "分析完成";
      toast("容量配置分析完成", "ok");
    } catch (e) {
      els.status.textContent = `分析失败：${e.message}`;
      toast(e.message, "error");
    } finally {
      els.analyzeBtn.disabled = false;
    }
  }

  function renderLoadSummary(profile) {
    const p = profile || {};
    const cards = [
      ["日均用电", `${fmtCompact(p.daily_kwh)} kWh`],
      ["日峰高电量", `${fmtCompact(p.daily_peak_high_kwh)} kWh`],
      ["日谷段电量", `${fmtCompact(p.daily_valley_kwh)} kWh`],
      ["最大需量", `${fmtNumber(p.max_demand_kw || 0, 2)} kW`],
    ];
    els.loadSummary.innerHTML = cards.map(([label, value]) =>
      `<div class="metric"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`
    ).join("");
  }

  function renderCapacity(data) {
    const rows = data.results || [];
    const best = data.best || rows.find((row) => row.is_best) || {};
    renderLoadSummary(data.load_profile || {});
    els.capacityStatus.textContent = `${rows.length || 0} 个组合 · 正收益 ${data.positive_count || 0} 个`;
    if (!rows.length) {
      els.capacityBest.innerHTML = '<div class="muted">暂无容量分析结果，请先解析账单后重新分析。</div>';
      els.capacityTableWrap.innerHTML = "";
      return;
    }
    els.capacityBest.innerHTML =
      `<div class="capacity-callout">` +
        `<div><span>推荐组合</span><b>${fmtNumber(best.battery_capacity_kwh || 0, 0)} kWh / ${fmtNumber(best.inverter_power_kw || 0, 0)} kW</b></div>` +
        `<div><span>储能时长</span><b>${fmtNumber(best.duration_hours || 0, 2)} h</b></div>` +
        `<div><span>总投资</span><b>${fmtCompact(best.total_investment_yuan)} 元</b></div>` +
        `<div><span>年综合收益</span><b>${fmtCompact(best.annual_revenue_yuan)} 元</b></div>` +
        `<div><span>峰谷套利</span><b>${fmtCompact(best.arbitrage_revenue_yuan)} 元</b></div>` +
        `<div><span>需量收益</span><b>${fmtCompact(best.demand_revenue_yuan)} 元</b></div>` +
        `<div><span>收益/容量</span><b>${fmtNumber(best.annual_revenue_per_kwh || 0, 0)} 元/kWh·年</b></div>` +
        `<div><span>回收期</span><b>${best.payback_years == null ? "-" : `${fmtNumber(best.payback_years, 2)} 年`}</b></div>` +
      `</div><p class="capacity-note">${escapeHtml(data.scoring_basis || "")}</p>`;
    const cols = [
      ["rank", "排名"], ["name", "组合"], ["battery_capacity_kwh", "电池容量(kWh)"],
      ["inverter_power_kw", "PCS功率(kW)"], ["duration_hours", "时长(h)"],
      ["total_investment_yuan", "投资(元)"], ["annual_revenue_yuan", "年综合收益(元)"],
      ["arbitrage_revenue_yuan", "峰谷套利(元)"], ["demand_revenue_yuan", "需量收益(元)"],
      ["annual_revenue_per_kwh", "收益/容量"], ["marginal_revenue_per_kwh", "边际收益"],
      ["payback_years", "回收期(年)"], ["utilization_ratio", "利用率"],
    ];
    els.capacityTableWrap.innerHTML =
      '<table class="capacity-table"><thead><tr>' +
      cols.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("") +
      '</tr></thead><tbody>' +
      rows.map((row) => `<tr class="${row.is_best ? "best-row" : ""}">` + cols.map(([key]) => {
        let value = row[key];
        if (value == null) value = "-";
        else if (key === "rank" && row.is_best) value = "推荐";
        else if (typeof value === "number") value = fmtNumber(value, ["duration_hours", "payback_years", "annual_revenue_per_kwh", "marginal_revenue_per_kwh"].includes(key) ? 2 : (key === "utilization_ratio" ? 4 : 0));
        return `<td class="${typeof row[key] === "number" && key !== "rank" ? "num" : ""}">${escapeHtml(value)}</td>`;
      }).join("") + "</tr>").join("") +
      "</tbody></table>";
  }

  async function init() {
    await resolveCurrentUser();
    await refreshUsers();
    els.userSelect.addEventListener("change", () => {
      currentUser = els.userSelect.value;
      loadCapacity();
    });
    els.refreshBtn.addEventListener("click", loadCapacity);
    els.analyzeBtn.addEventListener("click", analyzeCapacity);
    loadCapacity();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
