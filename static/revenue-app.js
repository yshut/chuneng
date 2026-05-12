(() => {
  const $ = (id) => document.getElementById(id);
  const els = {
    userSelect: $("user-select"),
    resetDefaultsBtn: $("reset-defaults-btn"),
    calculateBtn: $("calculate-btn"),
    exportReportBtn: $("export-report-btn"),
    reportLinks: $("report-links"),
    historySelect: $("param-history-select"),
    loadHistoryBtn: $("load-history-btn"),
    refreshHistoryBtn: $("refresh-history-btn"),
    paramGrid: $("param-grid"),
    status: $("status"),
    summaryGrid: $("summary-grid"),
    costWrap: $("cost-wrap"),
    loanWrap: $("loan-wrap"),
    loanImpactWrap: $("loan-impact-wrap"),
    cashflowWrap: $("cashflow-wrap"),
    customerWrap: $("customer-wrap"),
    investorWrap: $("investor-wrap"),
    sensitivityWrap: $("sensitivity-wrap"),
    cycleWrap: $("cycle-wrap"),
    paybackMatrixWrap: $("payback-matrix-wrap"),
    shareWrap: $("share-wrap"),
    toast: $("toast"),
  };

  const urlUser = new URLSearchParams(location.search).get("user");
  let currentUser = urlUser || "main";
  let latestParams = {};
  let paramHistory = [];
  let autoUpdating = false;

  const fieldGroups = [
    {
      title: "项目规模",
      fields: [
        ["project_name", "项目名称", "text"],
        ["power_kw", "装机功率(kW)", "number"],
        ["duration_hours", "储能时长(h)", "number"],
        ["capacity_kwh", "额定容量(kWh)", "number"],
        ["dod", "DOD", "number"],
        ["system_efficiency", "系统效率", "number"],
        ["availability", "首年可用率", "number"],
        ["annual_operating_days", "年运行天数", "number"],
        ["annual_degradation", "年衰减率", "number"],
        ["project_years", "运营期(年)", "number"],
      ],
    },
    {
      title: "投资假设",
      fields: [
        ["battery_unit_cost", "电池系统单价(元/Wh)", "number"],
        ["pcs_ems_bms_unit_cost", "PCS/EMS/BMS(元/Wh)", "number"],
        ["cell_cost_yuan_per_wh", "钠离子电芯价格(元/Wh)", "number"],
        ["system_unit_cost_yuan_per_wh", "完整系统单价(元/Wh)", "number"],
        ["cost_basis", "成本口径", "text"],
        ["grid_connection_cost_per_kw", "变配电及并网(元/kW)", "number"],
        ["civil_fire_cost_per_kw", "土建/消防/安装(元/kW)", "number"],
        ["design_supervision_rate", "设计监理费率", "number"],
        ["contingency_rate", "基本预备费率", "number"],
        ["construction_months", "建设期(月)", "number"],
        ["construction_interest_rate", "建设期资金成本", "number"],
      ],
    },
    {
      title: "分时电价与循环",
      fields: [
        ["valley_charge_price", "谷电充电电价(元/kWh)", "number"],
        ["flat_charge_price", "平时充电电价(元/kWh)", "number"],
        ["discharge_price", "峰电放电电价(元/kWh)", "number"],
        ["valley_peak_cycles", "谷峰日循环次数", "number"],
        ["flat_peak_cycles", "平峰日循环次数", "number"],
        ["price_escalation", "收入递增率", "number"],
      ],
    },
    {
      title: "收益与成本",
      fields: [
        ["demand_revenue_per_kw_year", "需量/容量收益(元/kW年)", "number"],
        ["demand_revenue", "需量收益总额(元/年)", "number"],
        ["ancillary_revenue_per_mw_year", "辅助服务收益(元/MW年)", "number"],
        ["ancillary_revenue", "辅助服务总额(元/年)", "number"],
        ["other_revenue", "其他年收入(元/年)", "number"],
        ["variable_om_cost_per_kwh", "可变运维费(元/kWh放电)", "number"],
        ["om_cost_rate", "固定运维费率", "number"],
        ["insurance_land_mgmt_rate", "保险/场地/管理费率", "number"],
        ["om_escalation", "运维成本递增率", "number"],
      ],
    },
    {
      title: "财务与分成",
      fields: [
        ["tax_rate", "所得税率", "number"],
        ["depreciation_years", "折旧年限", "number"],
        ["fixed_asset_residual_rate", "固定资产残值率", "number"],
        ["discount_rate", "折现率/WACC", "number"],
        ["terminal_residual_rate", "期末处置残值率", "number"],
        ["battery_replacement_year", "电池更换年份", "number"],
        ["battery_replacement_cost_rate", "更换成本比例", "number"],
        ["enable_customer_share", "给客户分成", "checkbox"],
        ["customer_share_before_payback", "回本前客户分成", "number"],
        ["customer_share_after_payback", "回本后客户分成", "number"],
      ],
    },
    {
      title: "贷款",
      fields: [
        ["enable_loan", "使用贷款", "checkbox"],
        ["loan_ratio", "贷款比例", "number"],
        ["loan_interest_rate", "贷款利率", "number"],
        ["loan_years", "贷款期限(年)", "number"],
      ],
    },
  ];

  function escapeHtml(value) {
    if (value == null) return "";
    return String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function fmtNumber(value, digits = 0) {
    if (value == null || Number.isNaN(Number(value))) return "-";
    return Number(value).toLocaleString("zh-CN", {
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

  function fmtMoney(value) {
    return `${fmtCompact(value)} 元`;
  }

  function inputFor(key) {
    return els.paramGrid.querySelector(`[data-key="${key}"]`);
  }

  function numberFor(key) {
    const input = inputFor(key);
    const value = Number(input && input.value);
    return Number.isFinite(value) ? value : 0;
  }

  function setInputValue(key, value, digits = 4) {
    const input = inputFor(key);
    if (!input) return;
    const next = Number.isFinite(Number(value)) ? Number(value) : 0;
    input.value = String(Number(next.toFixed(digits)));
  }

  function setInputDisabled(key, disabled) {
    const input = inputFor(key);
    if (!input) return;
    input.disabled = disabled;
    const field = input.closest(".param-field");
    if (field) field.classList.toggle("is-disabled", disabled);
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
      try {
        const data = JSON.parse(text);
        message = data.detail || data.message || message;
      } catch (_) {}
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

  function renderParamHistory() {
    if (!els.historySelect) return;
    if (!paramHistory.length) {
      els.historySelect.innerHTML = '<option value="">暂无历史参数</option>';
      els.historySelect.disabled = true;
      if (els.loadHistoryBtn) els.loadHistoryBtn.disabled = true;
      return;
    }
    els.historySelect.disabled = false;
    if (els.loadHistoryBtn) els.loadHistoryBtn.disabled = false;
    els.historySelect.innerHTML = paramHistory.map((item) => {
      const time = item.saved_at ? item.saved_at.replace("T", " ") : "";
      const label = [time, item.label || item.id].filter(Boolean).join(" | ");
      return `<option value="${escapeHtml(item.id)}">${escapeHtml(label)}</option>`;
    }).join("");
  }

  async function loadParamHistory() {
    try {
      const data = await api(`/api/revenue/params/history?user_id=${encodeURIComponent(currentUser)}`);
      paramHistory = Array.isArray(data.items) ? data.items : [];
      renderParamHistory();
    } catch (e) {
      paramHistory = [];
      renderParamHistory();
    }
  }

  function applyHistoryParams() {
    const id = els.historySelect && els.historySelect.value;
    const item = paramHistory.find((row) => row.id === id);
    if (!item || !item.params) {
      toast("没有可载入的历史参数", "error");
      return;
    }
    renderForm({ ...item.params });
    els.status.textContent = "已载入历史参数，点击“重新测算收益”后生效。";
    toast("历史参数已载入", "ok");
  }

  function renderReportLinks(data) {
    if (!els.reportLinks) return;
    const links = [
      ["ZIP打包下载", data.zip_url],
      ["Word报告", data.docx_url],
      ["MD报告", data.md_url],
    ].filter(([, url]) => !!url);
    if (!links.length) {
      els.reportLinks.classList.add("hidden");
      els.reportLinks.innerHTML = "";
      return;
    }
    els.reportLinks.classList.remove("hidden");
    els.reportLinks.innerHTML = '<div class="report-links-title">报告下载</div>' +
      links.map(([label, url]) =>
        `<a class="btn wide-btn" href="${escapeHtml(url)}" download>${escapeHtml(label)}</a>`
      ).join("") +
      `<div class="report-path">服务器目录：${escapeHtml(data.report_dir || "-")}</div>`;
  }

  function renderForm(params) {
    latestParams = { ...params };
    els.paramGrid.innerHTML = fieldGroups.map((group) => (
      `<section class="param-section"><h3>${escapeHtml(group.title)}</h3><div class="param-section-grid">` +
      group.fields.map(([key, label, type]) => {
        const value = params[key];
        if (type === "checkbox") {
          return `<label class="param-field check-field"><span>${escapeHtml(label)}</span>` +
            `<input data-key="${key}" type="checkbox" ${value ? "checked" : ""}></label>`;
        }
        if (type === "text") {
          return `<label class="param-field full-field"><span>${escapeHtml(label)}</span>` +
            `<input data-key="${key}" type="text" value="${escapeHtml(value ?? "")}"></label>`;
        }
        return `<label class="param-field"><span>${escapeHtml(label)}</span>` +
          `<input data-key="${key}" type="number" step="any" value="${escapeHtml(value ?? "")}"></label>`;
      }).join("") + "</div></section>"
    )).join("");
    bindFormDependencies();
    applyAllDependencies();
  }

  function collectParams() {
    applyAllDependencies();
    const body = { user_id: currentUser };
    els.paramGrid.querySelectorAll("[data-key]").forEach((input) => {
      const key = input.dataset.key;
      if (input.type === "checkbox") {
        body[key] = input.checked;
      } else if (input.type === "number") {
        body[key] = Number(input.value || 0);
      } else {
        body[key] = input.value;
      }
    });
    return body;
  }

  function bindFormDependencies() {
    els.paramGrid.querySelectorAll("[data-key]").forEach((input) => {
      input.addEventListener("input", () => handleParamInput(input.dataset.key));
      input.addEventListener("change", () => handleParamInput(input.dataset.key));
    });
  }

  function handleParamInput(key) {
    if (autoUpdating) return;
    autoUpdating = true;
    try {
      if (key === "power_kw" || key === "duration_hours") {
        updateCapacityFromPowerDuration();
      } else if (key === "capacity_kwh") {
        updateDurationFromCapacityPower();
      } else if (key === "demand_revenue") {
        updateDemandUnitFromTotal();
      } else if (key === "ancillary_revenue") {
        updateAncillaryUnitFromTotal();
      }
      if (!["demand_revenue", "ancillary_revenue"].includes(key)) {
        updateRevenueDerivedTotals();
      }
      updateToggleStates();
    } finally {
      autoUpdating = false;
    }
  }

  function applyAllDependencies() {
    if (autoUpdating) return;
    autoUpdating = true;
    try {
      updateCapacityFromPowerDuration();
      updateRevenueDerivedTotals();
      updateToggleStates();
    } finally {
      autoUpdating = false;
    }
  }

  function updateCapacityFromPowerDuration() {
    const power = numberFor("power_kw");
    const duration = numberFor("duration_hours");
    if (power > 0 && duration > 0) {
      setInputValue("capacity_kwh", power * duration, 2);
    }
  }

  function updateDurationFromCapacityPower() {
    const power = numberFor("power_kw");
    const capacity = numberFor("capacity_kwh");
    if (power > 0 && capacity > 0) {
      setInputValue("duration_hours", capacity / power, 3);
    }
  }

  function updateRevenueDerivedTotals() {
    const power = numberFor("power_kw");
    const demandUnit = numberFor("demand_revenue_per_kw_year");
    const ancillaryUnit = numberFor("ancillary_revenue_per_mw_year");
    if (power > 0 && demandUnit >= 0) {
      setInputValue("demand_revenue", power * demandUnit, 2);
      inputFor("demand_revenue")?.closest(".param-field")?.classList.add("is-derived");
    }
    if (power > 0 && ancillaryUnit >= 0) {
      setInputValue("ancillary_revenue", power / 1000 * ancillaryUnit, 2);
      inputFor("ancillary_revenue")?.closest(".param-field")?.classList.add("is-derived");
    }
  }

  function updateDemandUnitFromTotal() {
    const power = numberFor("power_kw");
    const total = numberFor("demand_revenue");
    if (power > 0 && total >= 0) {
      setInputValue("demand_revenue_per_kw_year", total / power, 4);
    }
  }

  function updateAncillaryUnitFromTotal() {
    const power = numberFor("power_kw");
    const total = numberFor("ancillary_revenue");
    if (power > 0 && total >= 0) {
      setInputValue("ancillary_revenue_per_mw_year", total / (power / 1000), 4);
    }
  }

  function updateToggleStates() {
    const customerShare = !!(inputFor("enable_customer_share") || {}).checked;
    setInputDisabled("customer_share_before_payback", !customerShare);
    setInputDisabled("customer_share_after_payback", !customerShare);

    const loan = !!(inputFor("enable_loan") || {}).checked;
    setInputDisabled("loan_ratio", !loan);
    setInputDisabled("loan_interest_rate", !loan);
    setInputDisabled("loan_years", !loan);
  }

  async function loadModel(forceDefaults = false) {
    els.status.textContent = forceDefaults ? "正在读取推荐配置..." : "正在加载收益测算...";
    try {
      const data = forceDefaults
        ? await api("/api/revenue/model", { method: "POST", body: JSON.stringify({ user_id: currentUser, _sync_to_capacity: true }) })
        : await api(`/api/revenue/model?user_id=${encodeURIComponent(currentUser)}`);
      renderModel(data);
      await loadParamHistory();
      els.status.textContent = data.saved_at ? `已加载本地测算：${data.saved_at}` : "已加载收益测算";
    } catch (e) {
      els.status.textContent = `加载失败：${e.message}`;
      toast(e.message, "error");
    }
  }

  async function calculate() {
    els.calculateBtn.disabled = true;
    els.status.textContent = "正在测算收益...";
    try {
      const data = await api("/api/revenue/model", {
        method: "POST",
        body: JSON.stringify(collectParams()),
      });
      renderModel(data);
      await loadParamHistory();
      els.status.textContent = data.saved_at ? `测算完成并已保存：${data.saved_at}` : "测算完成";
      toast("收益测算完成", "ok");
    } catch (e) {
      els.status.textContent = `测算失败：${e.message}`;
      toast(e.message, "error");
    } finally {
      els.calculateBtn.disabled = false;
    }
  }

  async function exportReport() {
    els.exportReportBtn.disabled = true;
    els.status.textContent = "正在生成详细报告...";
    try {
      const data = await api("/api/revenue/model", {
        method: "POST",
        body: JSON.stringify(collectParams()),
      });
      renderModel(data);
      await loadParamHistory();
      const report = await api("/api/revenue/report", {
        method: "POST",
        body: JSON.stringify({ user_id: currentUser }),
      });
      renderReportLinks(report);
      if (report.zip_url) window.location.href = report.zip_url;
      els.status.textContent = report.generated_at ? `报告已生成：${report.generated_at}` : "报告已生成";
      toast("详细报告已生成", "ok");
    } catch (e) {
      els.status.textContent = `导出失败：${e.message}`;
      toast(e.message, "error");
    } finally {
      els.exportReportBtn.disabled = false;
    }
  }

  function renderModel(data) {
    renderForm(data.params || latestParams);
    const s = data.summary || {};
    const cards = [
      ["配置规模", `${fmtNumber(s.power_kw, 0)} kW / ${fmtNumber(s.capacity_kwh, 0)} kWh`],
      ["综合充放电价差", `${fmtNumber(s.spread_yuan_per_kwh, 3)} 元/kWh`],
      ["首年放电量", `${fmtCompact(s.first_year_discharge_kwh)} kWh`],
      ["峰谷套利毛利", fmtMoney(s.arbitrage_revenue_yuan)],
      ["首年分成前总收入", fmtMoney(s.first_year_gross_revenue_yuan)],
      ["分成前 EBITDA", fmtMoney(s.first_year_ebitda_yuan)],
      ["客户首年收益", fmtMoney(s.customer_first_year_yuan)],
      ["资方首年收益", fmtMoney(s.investor_first_year_yuan)],
      ["静态回收期", s.static_payback_years == null ? "-" : `${fmtNumber(s.static_payback_years, 2)} 年`],
      ["项目 NPV", fmtMoney(s.project_npv_yuan)],
      ["项目 IRR", s.project_irr_percent == null ? "-" : `${fmtNumber(s.project_irr_percent, 2)}%`],
      ["股东 IRR", s.investor_irr_percent == null ? "-" : `${fmtNumber(s.investor_irr_percent, 2)}%`],
    ];
    els.summaryGrid.innerHTML = cards.map(([label, value]) =>
      `<div class="metric"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`
    ).join("");
    renderSimpleTable(els.costWrap, data.cost_breakdown || [], [
      ["item", "成本项目"], ["amount_yuan", "金额(元)"],
    ]);
    renderLoan(data.loan || {});
    renderLoanImpact(data.loan_impact || {});
    renderSimpleTable(els.cashflowWrap, data.cash_flow || [], [
      ["year", "年份"], ["discharge_kwh", "放电量(kWh)"], ["charge_kwh", "充电量(kWh)"],
      ["sale_revenue_yuan", "售电收入(元)"], ["charge_cost_yuan", "充电成本(元)"],
      ["arbitrage_margin_yuan", "套利毛利(元)"], ["demand_revenue_yuan", "需量收益(元)"],
      ["gross_revenue_yuan", "总收入(元)"], ["om_cost_yuan", "运维/保险(元)"],
      ["customer_share_yuan", "原始客户分成(元)"], ["investor_ebitda_yuan", "资方EBITDA(元)"],
      ["tax_yuan", "所得税(元)"], ["loan_interest_yuan", "利息(元)"],
      ["loan_principal_yuan", "还本(元)"], ["investor_cash_flow_yuan", "股东现金流(元)"],
      ["cumulative_investor_yuan", "累计资方现金流(元)"],
    ]);
    renderSimpleTable(els.customerWrap, data.customer_yearly || [], [
      ["year", "年份"], ["loan_status", "贷款状态"], ["loan_cost_yuan", "贷款成本(元)"],
      ["share_ratio_percent", "客户分成(%)"], ["distributable_yuan", "扣贷款后可分配(元)"],
      ["customer_income_yuan", "客户年度收益(元)"], ["cumulative_customer_yuan", "累计客户收益(元)"],
      ["original_customer_income_yuan", "原始客户收益(元)"], ["loan_impact_yuan", "贷款影响减少(元)"],
    ]);
    renderSimpleTable(els.investorWrap, data.investor_yearly || [], [
      ["year", "年份"], ["loan_status", "贷款状态"], ["beginning_loan_balance_yuan", "期初贷款余额(元)"],
      ["loan_cost_yuan", "贷款成本(元)"], ["distributable_yuan", "扣贷款后可分配(元)"],
      ["investor_income_yuan", "资方年度收益(元)"], ["cumulative_investor_income_yuan", "累计资方收益(元)"],
      ["original_investor_income_yuan", "原始资方收益(元)"], ["loan_impact_yuan", "贷款影响减少(元)"],
    ]);
    renderSimpleTable(els.sensitivityWrap, data.sensitivity || [], [
      ["factor", "敏感性因素"], ["change_percent", "变化幅度(%)"], ["annual_net_yuan", "年净收益(元)"], ["payback_years", "回收期(年)"],
    ]);
    renderSimpleTable(els.cycleWrap, data.cycle_sensitivity || [], [
      ["annual_cycles", "年循环次数"], ["discharge_kwh", "放电量(kWh)"], ["arbitrage_revenue_yuan", "套利毛利(元)"],
      ["demand_revenue_yuan", "需量收益(元)"], ["annual_net_yuan", "年净收益(元)"], ["payback_years", "回收期(年)"],
    ]);
    renderPaybackMatrix(data.payback_matrix || {});
    renderSimpleTable(els.shareWrap, data.share_sensitivity || [], [
      ["share_ratio_percent", "回本前客户分成(%)"], ["payback_years", "静态回收期(年)"],
    ]);
  }

  function renderLoan(loan) {
    const rows = [
      ["是否贷款", loan.enabled ? "是" : "否"],
      ["贷款金额", fmtMoney(loan.loan_amount_yuan)],
      ["自有资金", fmtMoney(loan.equity_amount_yuan)],
      ["首年还本付息", fmtMoney(loan.annual_debt_service_yuan)],
      ["总利息", fmtMoney(loan.total_interest_yuan)],
      ["推荐贷款比例", loan.recommended_ratio_range || "-"],
      ["贷款结论", loan.conclusion || "-"],
    ];
    els.loanWrap.innerHTML = rows.map(([label, value]) =>
      `<div class="loan-row"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`
    ).join("");
  }

  function renderLoanImpact(impact) {
    const rows = [
      ["有贷款首年现金流", fmtMoney(impact.loan_first_year_cash_flow_yuan)],
      ["无贷款首年现金流", fmtMoney(impact.no_loan_first_year_cash_flow_yuan)],
      ["首年影响", fmtMoney(impact.first_year_delta_yuan)],
      ["有贷款生命周期总收益", fmtMoney(impact.loan_lifetime_total_yuan)],
      ["无贷款生命周期总收益", fmtMoney(impact.no_loan_lifetime_total_yuan)],
      ["生命周期总收益影响", fmtMoney(impact.lifetime_delta_yuan)],
      ["有贷款股东IRR", impact.loan_shareholder_irr_percent == null ? "-" : `${fmtNumber(impact.loan_shareholder_irr_percent, 2)}%`],
      ["无贷款股东IRR", impact.no_loan_shareholder_irr_percent == null ? "-" : `${fmtNumber(impact.no_loan_shareholder_irr_percent, 2)}%`],
      ["IRR影响", impact.irr_delta_percent == null ? "-" : `${fmtNumber(impact.irr_delta_percent, 2)}%`],
    ];
    els.loanImpactWrap.innerHTML = rows.map(([label, value]) =>
      `<div class="loan-row"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`
    ).join("");
  }

  function renderPaybackMatrix(matrix) {
    const spreads = matrix.spreads || [];
    const rows = matrix.rows || [];
    if (!spreads.length || !rows.length) {
      els.paybackMatrixWrap.innerHTML = '<div class="muted" style="padding:14px">暂无数据</div>';
      return;
    }
    els.paybackMatrixWrap.innerHTML = '<table class="bill-table matrix-table"><thead><tr>' +
      '<th>系统+PCS单价/价差</th>' +
      spreads.map((spread) => `<th>${fmtNumber(spread, 2)}</th>`).join("") +
      '</tr></thead><tbody>' +
      rows.map((row) => '<tr>' +
        `<td>${fmtNumber(row.unit_cost_yuan_per_wh, 2)}</td>` +
        (row.values || []).map((value) => `<td class="num">${value == null ? "-" : fmtNumber(value, 2)}</td>`).join("") +
      '</tr>').join("") +
      "</tbody></table>";
  }

  function renderSimpleTable(target, rows, cols) {
    if (!rows.length) {
      target.innerHTML = '<div class="muted" style="padding:14px">暂无数据</div>';
      return;
    }
    target.innerHTML = '<table class="bill-table"><thead><tr>' +
      cols.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("") +
      '</tr></thead><tbody>' +
      rows.map((row) => "<tr>" + cols.map(([key]) => {
        const raw = row[key];
        const isNum = typeof raw === "number";
        const digits = key.includes("percent") || key.includes("payback") ? 2 : 0;
        const value = isNum ? fmtNumber(raw, digits) : (raw ?? "-");
        return `<td class="${isNum ? "num" : ""}">${escapeHtml(value)}</td>`;
      }).join("") + "</tr>").join("") +
      "</tbody></table>";
  }

  async function init() {
    await resolveCurrentUser();
    await refreshUsers();
    els.userSelect.addEventListener("change", () => {
      currentUser = els.userSelect.value;
      loadParamHistory();
      loadModel();
    });
    els.calculateBtn.addEventListener("click", calculate);
    els.resetDefaultsBtn.addEventListener("click", () => loadModel(true));
    els.exportReportBtn.addEventListener("click", exportReport);
    if (els.refreshHistoryBtn) els.refreshHistoryBtn.addEventListener("click", loadParamHistory);
    if (els.loadHistoryBtn) els.loadHistoryBtn.addEventListener("click", applyHistoryParams);
    await loadParamHistory();
    await loadModel();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
