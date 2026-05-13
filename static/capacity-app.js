// 储能容量配置分析页前端 —— 使用 window.AgentCommon 共享工具
(() => {
  const C = window.AgentCommon;
  const { $, escapeHtml, fmtNumber, fmtCompact, api,
          toast, refreshUsers, resolveCurrentUser,
          renderCapacityCallout, renderCapacityTable,
          skeleton, emptyState, enableSortInContainer } = C;

  const els = {
    userSelect: $('user-select'),
    refreshBtn: $('refresh-btn'),
    analyzeBtn: $('analyze-btn'),
    capacityInput: $('capacity-input'),
    durationInput: $('duration-input'),
    status: $('status'),
    loadSummary: $('load-summary'),
    capacityStatus: $('capacity-status'),
    capacityBest: $('capacity-best'),
    capacityTableWrap: $('capacity-table-wrap'),
    llmPrefInput: $('llm-pref-input'),
    llmOnAnalyze: $('llm-on-analyze'),
    llmReviewBtn: $('llm-review-btn'),
    llmRecommendation: $('llm-recommendation'),
  };
  const urlUser = new URLSearchParams(location.search).get('user');
  let currentUser = urlUser || 'main';

  async function refreshUsersList() {
    currentUser = await refreshUsers(els.userSelect, currentUser);
  }

  function parseList(text) {
    return String(text || '')
      .split(/[,，\s]+/)
      .map((x) => Number(x))
      .filter((x) => Number.isFinite(x) && x > 0);
  }

  async function loadCapacity() {
    els.status.textContent = '正在加载容量分析结果...';
    els.capacityTableWrap.innerHTML = skeleton(5);
    try {
      const data = await api(`/api/storage/capacity-analysis?user_id=${encodeURIComponent(currentUser)}`);
      renderCapacity(data);
      els.status.textContent = data.saved_at ? `已加载本地历史结果：${data.saved_at}` : (data.msg || '已加载');
    } catch (e) {
      els.status.textContent = `加载失败：${e.message}`;
      els.capacityTableWrap.innerHTML = '';
      toast(e.message, 'error');
    }
  }

  async function analyzeCapacity() {
    els.analyzeBtn.disabled = true;
    els.status.textContent = '正在扫描容量组合...';
    els.capacityTableWrap.innerHTML = skeleton(6);
    try {
      const body = { user_id: currentUser };
      const capacities = parseList(els.capacityInput.value);
      const durations = parseList(els.durationInput.value);
      if (capacities.length) body.capacities_kwh = capacities;
      if (durations.length) body.durations_hours = durations;
      const wantLLM = els.llmOnAnalyze && els.llmOnAnalyze.checked;
      if (wantLLM) {
        body.use_llm_review = true;
        const pref = (els.llmPrefInput && els.llmPrefInput.value || '').trim();
        if (pref) body.user_preference = pref;
        els.status.textContent = '正在扫描组合 + 等待 AI 评审...';
      }
      const data = await api('/api/storage/capacity-analysis', {
        method: 'POST',
        body: JSON.stringify(body),
      });
      renderCapacity(data);
      els.status.textContent = data.saved_at ? `分析完成并已保存：${data.saved_at}` : '分析完成';
      toast(wantLLM ? '容量分析 + AI 评审完成' : '容量配置分析完成', 'ok');
    } catch (e) {
      els.status.textContent = `分析失败：${e.message}`;
      els.capacityTableWrap.innerHTML = '';
      toast(e.message, 'error');
    } finally {
      els.analyzeBtn.disabled = false;
    }
  }

  async function llmReview() {
    els.llmReviewBtn.disabled = true;
    const oldText = els.llmReviewBtn.textContent;
    els.llmReviewBtn.textContent = 'AI 正在评审...';
    els.status.textContent = '正在请求大模型综合评审...';
    try {
      const body = { user_id: currentUser, apply: true };
      const pref = (els.llmPrefInput && els.llmPrefInput.value || '').trim();
      if (pref) body.user_preference = pref;
      const data = await api('/api/storage/capacity-analysis/llm-review', {
        method: 'POST',
        body: JSON.stringify(body),
      });
      renderCapacity(data);
      const review = data.llm_review || {};
      els.status.textContent = review.ok
        ? `AI 评审完成 · 推荐索引 ${review.chosen_index}（${review.agrees_with_numerical ? '与数值模型一致' : '已覆盖数值最优解'}）`
        : `AI 评审未能完成：${review.error || '未知原因'}`;
      toast(review.ok ? 'AI 评审完成' : `AI 评审未完成：${review.error || '未知'}`, review.ok ? 'ok' : 'error');
    } catch (e) {
      els.status.textContent = `AI 评审失败：${e.message}`;
      toast(e.message, 'error');
    } finally {
      els.llmReviewBtn.disabled = false;
      els.llmReviewBtn.textContent = oldText;
    }
  }

  async function fetchAndRenderDiagnose(targetEl) {
    if (!targetEl) return;
    try {
      const diag = await api('/api/llm/diagnose');
      const envRows = Object.entries(diag.env_vars || {}).map(([prov, info]) => `
        <tr>
          <td>${escapeHtml(prov)}</td>
          <td><code>${escapeHtml(info.env_key || '-')}</code></td>
          <td>${info.present ? `<span class="ok">已设置</span> (${info.length} 字符, ${escapeHtml(info.masked || '')})` : '<span class="bad">未设置</span>'}</td>
        </tr>`).join('');
      const suggestions = (diag.suggestions || []).map((s) => `<li>${escapeHtml(s)}</li>`).join('');
      targetEl.innerHTML = `
        <div class="llm-diag">
          <p><b>初始化状态：</b>${diag.ok ? '<span class="ok">就绪</span>' : `<span class="bad">不可用 — ${escapeHtml(diag.init_reason || '未知')}</span>`}</p>
          <p><b>当前 Provider：</b>${escapeHtml(diag.provider || '-')} ｜ <b>Base URL：</b>${escapeHtml(diag.current_config?.base_url || '-')}</p>
          <p><b>持久化文件：</b><code>${escapeHtml(diag.persisted_file || '-')}</code> ${diag.persisted_exists ? '存在' : '不存在'}</p>
          <table class="llm-diag-table">
            <thead><tr><th>Provider</th><th>环境变量名</th><th>状态</th></tr></thead>
            <tbody>${envRows}</tbody>
          </table>
          ${suggestions ? `<div class="llm-diag-tips"><b>修复建议：</b><ol>${suggestions}</ol></div>` : ''}
        </div>`;
    } catch (e) {
      targetEl.innerHTML = `<p class="muted">诊断获取失败：${escapeHtml(e.message)}</p>`;
    }
  }

  function renderLLMRecommendation(review, rows) {
    if (!review) {
      els.llmRecommendation.innerHTML = '';
      return;
    }
    if (!review.ok) {
      els.llmRecommendation.innerHTML = `
        <div class="llm-rec-card llm-rec-error">
          <div class="llm-rec-head"><span class="llm-rec-badge">AI 评审</span><b>未完成</b></div>
          <p class="muted">${escapeHtml(review.error || '大模型未能给出有效推荐，已回退到数值最优解。')}</p>
          <div class="llm-diag-wrap">
            <div class="llm-diag-loading muted">正在加载诊断信息...</div>
          </div>
        </div>`;
      const diagWrap = els.llmRecommendation.querySelector('.llm-diag-wrap');
      if (diagWrap) fetchAndRenderDiagnose(diagWrap);
      return;
    }
    const idx = Number.isFinite(review.chosen_index) ? review.chosen_index : -1;
    const chosen = (rows || [])[idx] || review.chosen_config || {};
    const agrees = review.agrees_with_numerical;
    const keyMetrics = (review.key_metrics || []).map((s) => `<li>${escapeHtml(String(s))}</li>`).join('');
    const risks = (review.risks || []).map((s) => `<li>${escapeHtml(String(s))}</li>`).join('');
    const backup = (review.backup_recommendations || []).map((b) => {
      const bi = Number.isFinite(b.index) ? b.index : -1;
      const r = (rows || [])[bi] || {};
      const cap = r.battery_capacity_kwh ? `${fmtCompact(r.battery_capacity_kwh)} kWh / ${fmtCompact(r.inverter_power_kw)} kW` : `索引 ${bi}`;
      return `<li><b>${escapeHtml(cap)}</b>：${escapeHtml(b.scenario || '')}</li>`;
    }).join('');
    const nextSteps = (review.next_steps || []).map((s) => `<li>${escapeHtml(String(s))}</li>`).join('');

    els.llmRecommendation.innerHTML = `
      <div class="llm-rec-card">
        <div class="llm-rec-head">
          <span class="llm-rec-badge">AI 推荐</span>
          <b>${escapeHtml(chosen.battery_capacity_kwh ? `${fmtCompact(chosen.battery_capacity_kwh)} kWh / ${fmtCompact(chosen.inverter_power_kw)} kW` : '已推荐方案')}</b>
          <span class="llm-rec-tag ${agrees ? 'ok' : 'override'}">${agrees ? '与数值模型一致' : '覆盖了数值最优解'}</span>
        </div>
        <div class="llm-rec-metrics">
          <span>年化收益 <b>${fmtCompact(chosen.annual_revenue_yuan || 0)} 元</b></span>
          <span>回收期 <b>${fmtNumber(chosen.payback_years, 2)} 年</b></span>
          <span>IRR <b>${fmtNumber(chosen.irr_percent, 2)}%</b></span>
          <span>NPV <b>${fmtCompact(chosen.npv_yuan || 0)} 元</b></span>
        </div>
        ${review.reasoning ? `<p class="llm-rec-text"><b>推荐理由：</b>${escapeHtml(review.reasoning)}</p>` : ''}
        ${review.comparison ? `<p class="llm-rec-text"><b>方案对比：</b>${escapeHtml(review.comparison)}</p>` : ''}
        ${keyMetrics ? `<div class="llm-rec-block"><b>亮点</b><ul>${keyMetrics}</ul></div>` : ''}
        ${risks ? `<div class="llm-rec-block"><b>风险提示</b><ul>${risks}</ul></div>` : ''}
        ${backup ? `<div class="llm-rec-block"><b>备选方案</b><ul>${backup}</ul></div>` : ''}
        ${nextSteps ? `<div class="llm-rec-block"><b>下一步建议</b><ul>${nextSteps}</ul></div>` : ''}
        ${review.model ? `<p class="muted llm-rec-foot">模型：${escapeHtml(String(review.model))}${review.user_preference ? ` · 偏好：${escapeHtml(review.user_preference)}` : ''}</p>` : ''}
      </div>`;
  }

  function renderLoadSummary(profile) {
    const p = profile || {};
    const cards = [
      ['日均用电', `${fmtCompact(p.daily_kwh)} kWh`],
      ['日峰高电量', `${fmtCompact(p.daily_peak_high_kwh)} kWh`],
      ['日谷段电量', `${fmtCompact(p.daily_valley_kwh)} kWh`],
      ['最大需量', `${fmtNumber(p.max_demand_kw || 0, 2)} kW`],
    ];
    els.loadSummary.innerHTML = cards.map(([label, value]) =>
      `<div class="metric"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`
    ).join('');
  }

  function renderCapacity(data) {
    const d = data || {};
    const rows = d.results || [];
    const best = d.best || rows.find((row) => row.is_best) || {};
    renderLoadSummary(d.load_profile || {});
    els.capacityStatus.textContent = `${rows.length || 0} 个组合 · 正收益 ${d.positive_count || 0} 个`;
    if (!rows.length) {
      els.capacityBest.innerHTML = emptyState('暂无容量分析结果', '请先在账单页解析数据，再返回此页重新分析');
      els.capacityTableWrap.innerHTML = '';
      els.llmRecommendation.innerHTML = '';
      return;
    }
    els.capacityBest.innerHTML = renderCapacityCallout(best, d.scoring_basis || '');
    renderLLMRecommendation(d.llm_review, rows);
    els.capacityTableWrap.innerHTML = renderCapacityTable(rows);
    enableSortInContainer(els.capacityTableWrap);
  }

  async function init() {
    currentUser = await resolveCurrentUser(urlUser, currentUser);
    await refreshUsersList();
    els.userSelect.addEventListener('change', () => {
      currentUser = els.userSelect.value;
      loadCapacity();
    });
    els.refreshBtn.addEventListener('click', loadCapacity);
    els.analyzeBtn.addEventListener('click', analyzeCapacity);
    if (els.llmReviewBtn) els.llmReviewBtn.addEventListener('click', llmReview);
    loadCapacity();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
