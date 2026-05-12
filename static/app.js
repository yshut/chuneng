// 储能 AGENT 前端 —— 异步无锁版本
// SSE 流式聊天 + 并发 fetch 上传 + 多用户隔离
// 共享工具来自 window.AgentCommon（见 common.js）

(() => {
  const C = window.AgentCommon;
  const { $, escapeHtml, fmtNumber, fmtCompact, api, readErrorMessage,
          toast, refreshUsers, resolveCurrentUser, getUrlUser, uploadWithProgress,
          renderUploadProgress, renderUploadPending, bindTabs, skeleton,
          emptyState } = C;

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
  };

  // ---------- 状态 ----------
  const urlUser = getUrlUser();
  let currentUser = urlUser || 'main';
  let chatAbortController = null;
  let currentAsstBubble = null;       // 当前 assistant 主气泡（流式追加）
  let currentAsstContainer = null;    // 当前 assistant 整条消息容器（含工具事件）
  let currentAsstAcc = '';            // 当前气泡累计的文本
  // 用户切换时的并发控制
  let userRefreshSeq = 0;
  // 续接（断线重连）控制
  let liveResumeAbort = false;

  // ---------- 工具函数 ----------
  function scrollChatBottom() {
    requestAnimationFrame(() => {
      els.chatList.scrollTop = els.chatList.scrollHeight;
    });
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

  function hideWelcome() {
    const ws = document.getElementById('welcome-state');
    if (ws) ws.remove();
  }

  function pushUserMessage(text) {
    hideWelcome();
    const inner = makeMsg('user');
    const b = addBubble(inner);
    b.textContent = text;
    scrollChatBottom();
  }

  function startAsstMessage() {
    currentAsstContainer = makeMsg('asst');
    currentAsstBubble = null;
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

  // ---------- 数据加载 ----------
  async function refreshUsersList() {
    currentUser = await refreshUsers(els.userSelect, currentUser);
  }

  async function refreshState() {
    if (!els.stateCard) return;
    els.stateCard.innerHTML = skeleton(7);
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
    if (!els.memoryCard) return;
    els.memoryCard.innerHTML = skeleton(5);
    try {
      const m = await api(`/api/memory?user_id=${encodeURIComponent(currentUser)}`);
      if (!m.enabled) {
        els.memoryCard.innerHTML = emptyState('长期记忆未启用', '当前模型/配置未启用记忆模块');
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

  async function refreshKb() {
    if (!els.kbCard) return;
    els.kbCard.innerHTML = skeleton(4);
    try {
      const k = await api(`/api/kb?user_id=${encodeURIComponent(currentUser)}`);
      if (!k.enabled) {
        els.kbCard.innerHTML = emptyState('知识库未启用', '缺嵌入模型或未安装 chromadb');
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
                  `<span class="kb-del" style="cursor:pointer;color:var(--stop)" data-source="${escapeHtml(src)}" role="button" aria-label="删除 ${escapeHtml(src)}">✕</span></div>`;
        }
        html += '</div>';
      }
      els.kbCard.innerHTML = html;
      els.kbCard.querySelectorAll('[data-source]').forEach((el) => {
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
    const seq = ++userRefreshSeq;
    els.chatList.innerHTML = '';
    try {
      const r = await api(`/api/history?user_id=${encodeURIComponent(currentUser)}`);
      if (seq !== userRefreshSeq) return;
      const msgs = r.messages || [];
      if (!msgs.length) {
        els.chatList.innerHTML =
          '<div class="welcome-state" id="welcome-state">' +
          '<div class="welcome-icon">⚡</div>' +
          '<div class="welcome-title">储能配置智能体</div>' +
          '<div class="welcome-sub">上传电费账单，AI 自动解析、配置储能容量、测算投资收益。支持自然语言交互，随时调整参数。</div>' +
          '<div class="welcome-hints">' +
          '<span class="welcome-hint" data-hint="帮我分析这个月的电费账单">📄 分析电费账单</span>' +
          '<span class="welcome-hint" data-hint="我们工厂月用电50万度，帮我配储能">🔋 配置储能方案</span>' +
          '<span class="welcome-hint" data-hint="测算一下 EMC 模式下的投资收益">💰 测算投资收益</span>' +
          '<span class="welcome-hint" data-hint="对比一下不同容量方案的收益差异">📊 对比方案差异</span>' +
          '</div></div>';
        bindWelcomeHints();
        return;
      }
      for (const m of msgs) {
        if (m.role === 'user') {
          pushUserMessage(m.content || '');
        } else if (m.role === 'assistant') {
          const inner = makeMsg('asst');
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
      if (seq !== userRefreshSeq) return;
      els.chatList.innerHTML =
        '<div style="padding:14px;color:var(--stop)">❌ 加载历史失败：' +
        escapeHtml(e.message) + '</div>';
    }
  }

  async function refreshFiles() {
    if (!els.fileList) return;
    try {
      const s = await api(`/api/state?user_id=${encodeURIComponent(currentUser)}`);
      const files = s.input_files || [];
      if (!files.length) {
        els.fileList.innerHTML = '<li class="muted">（input/ 为空）</li>';
        return;
      }
      els.fileList.innerHTML = files.map((f) =>
        `<li><span class="fname">${escapeHtml(f)}</span>` +
        `<span class="del" data-name="${escapeHtml(f)}" title="删除" role="button" aria-label="删除 ${escapeHtml(f)}">🗑</span></li>`
      ).join('');
      els.fileList.querySelectorAll('[data-name]').forEach((el) => {
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
    } catch (e) {
      els.fileList.innerHTML = '<li>❌ ' + escapeHtml(e.message) + '</li>';
    }
  }

  // ---------- 上传 ----------
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
      refreshFiles();
      refreshState();
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
      const ok = (r.results || []).filter((x) => x.ok).length;
      const fail = (r.results || []).filter((x) => !x.ok).length;
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
      if (!hits.length) {
        els.kbSearchResult.innerHTML = emptyState('无结果', '试试更换关键词或检查文档是否已索引');
        return;
      }
      els.kbSearchResult.innerHTML = hits.map((h) =>
        `<div class="kb-hit"><div class="src">${escapeHtml(h.source || '')}` +
        (h.score != null ? ` · <span class="score">score ${Number(h.score).toFixed(3)}</span>` : '') +
        `</div><div>${escapeHtml((h.text || '').slice(0, 400))}</div></div>`
      ).join('');
    } catch (e) {
      els.kbSearchResult.innerHTML = `<div style="color:var(--stop)">❌ ${escapeHtml(e.message)}</div>`;
    }
  }

  // ---------- 聊天事件渲染（共享给 SSE 和断线续接） ----------
  function handleChatEvent(ev) {
    if (!ev || typeof ev !== 'object') return;
    const t = ev.type;
    if (t === 'user_echo') {
      pushUserMessage(ev.content || '');
      startAsstMessage();
      currentAsstAcc = '';
    } else if (t === 'text') {
      if (!currentAsstContainer) {
        startAsstMessage();
        currentAsstAcc = '';
      }
      const b = ensureAsstBubble();
      currentAsstAcc += ev.delta || '';
      b.textContent = currentAsstAcc;
      scrollChatBottom();
    } else if (t === 'tool') {
      if (!currentAsstContainer) startAsstMessage();
      const args = ev.args ? JSON.stringify(ev.args).slice(0, 220) : '';
      addEvtBlock(currentAsstContainer, 'tool',
        `🔧 调用工具: ${ev.name}`,
        args ? `<code>${escapeHtml(args)}</code>` : '');
      scrollChatBottom();
    } else if (t === 'tool_progress') {
      if (!currentAsstContainer) startAsstMessage();
      const p = ev.progress || {};
      const msg = p.msg || JSON.stringify(p);
      addEvtBlock(currentAsstContainer, 'tool',
        `⏳ ${ev.name} 进度`,
        escapeHtml(msg).slice(0, 200));
      scrollChatBottom();
    } else if (t === 'tool_result') {
      if (!currentAsstContainer) startAsstMessage();
      const result = (ev.result || '').slice(0, 600);
      addEvtBlock(currentAsstContainer, 'tool-result',
        `✓ ${ev.name} 完成`,
        `<code>${escapeHtml(result)}</code>`);
      scrollChatBottom();
    } else if (t === 'tool_error') {
      if (!currentAsstContainer) startAsstMessage();
      addEvtBlock(currentAsstContainer, 'tool-error',
        `⚠️ ${ev.name || '工具'} 出错（重试 ${ev.retry || 0}/${ev.max_retries || 0}）`,
        escapeHtml(ev.error || ev.message || ''));
      scrollChatBottom();
    } else if (t === 'subagent') {
      if (!currentAsstContainer) startAsstMessage();
      const phase = ev.phase || '';
      const role = ev.role || ev.name || '';
      addEvtBlock(currentAsstContainer, 'subagent',
        `🤝 子 Agent [${role}] ${phase}`, escapeHtml(ev.task || ev.msg || ''));
      scrollChatBottom();
    } else if (t === 'reflection') {
      if (!currentAsstContainer) startAsstMessage();
      let reflBlock = currentAsstContainer.querySelector('.evt.reflection.active');
      if (!reflBlock) {
        reflBlock = addEvtBlock(currentAsstContainer, 'reflection active', '💭 反思', '');
        reflBlock.dataset.acc = '';
      }
      reflBlock.dataset.acc += (ev.delta || '');
      reflBlock.innerHTML =
        `<div><span class="label">💭 反思</span></div><div>${escapeHtml(reflBlock.dataset.acc)}</div>`;
      scrollChatBottom();
    } else if (t === 'final') {
      if (currentAsstContainer && !currentAsstAcc) {
        const b = ensureAsstBubble();
        b.textContent = ev.content || '';
        currentAsstAcc = ev.content || '';
      }
    } else if (t === 'error') {
      if (!currentAsstContainer) startAsstMessage();
      addEvtBlock(currentAsstContainer, 'tool-error', '❌ 错误', escapeHtml(ev.message || ''));
    } else if (t === 'done') {
      endAsstMessage();
      currentAsstAcc = '';
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
    currentAsstAcc = '';
    setSending(true);

    chatAbortController = new AbortController();

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
        let idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const block = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const dataLine = block.split('\n').find((l) => l.startsWith('data:'));
          if (!dataLine) continue;
          let payload;
          try { payload = JSON.parse(dataLine.slice(5).trim()); }
          catch { continue; }
          // user_echo 是给断线重连用的，直发场景下我们已经在前面 pushUserMessage 了
          if (payload && payload.type === 'user_echo') continue;
          handleChatEvent(payload);
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
      currentAsstAcc = '';
      setSending(false);
      refreshState();
      refreshMemory();
    }
  }

  // ---------- 断线续接（刷新页面后看到正在生成中的对话） ----------
  async function tryResumeLiveGeneration() {
    if (chatAbortController) return; // 当前页面正在主动聊天，不重复接管
    let snap;
    try {
      snap = await api(`/api/chat/live?user_id=${encodeURIComponent(currentUser)}&since=0`);
    } catch { return; }
    if (!snap || !snap.exists || !Array.isArray(snap.events) || !snap.events.length) return;

    // 已结束 + 历史里已有完整内容 → 不重放（loadHistory 已经把结果显示了）
    const hasDone = snap.events.some((e) => e && e.type === 'done');
    if (!snap.running && hasDone) return;

    // 续接前：把 loadHistory 已经渲染的"本轮 user 气泡及其后"删掉，避免重复
    const lastUserText = snap.last_user_message || '';
    if (lastUserText) {
      const userBubbles = els.chatList.querySelectorAll('.msg.user');
      for (let i = userBubbles.length - 1; i >= 0; i -= 1) {
        const node = userBubbles[i];
        const bubble = node.querySelector('.bubble');
        if (bubble && (bubble.textContent || '').trim() === lastUserText.trim()) {
          // 把它和它之后的所有节点全部移除
          let cur = node;
          while (cur) {
            const next = cur.nextSibling;
            cur.remove();
            cur = next;
          }
          break;
        }
      }
    }

    liveResumeAbort = false;
    setSending(true);

    // 先回放已有的事件
    let lastVersion = snap.version || 0;
    snap.events.forEach((ev) => handleChatEvent(ev));

    // 还在跑就轮询续接（每 700ms 一次）
    while (snap.running && !liveResumeAbort) {
      await new Promise((r) => setTimeout(r, 700));
      try {
        snap = await api(`/api/chat/live?user_id=${encodeURIComponent(currentUser)}&since=${lastVersion}`);
      } catch { break; }
      if (!snap || !snap.exists) break;
      if (snap.events && snap.events.length) {
        snap.events.forEach((ev) => handleChatEvent(ev));
        lastVersion = snap.version;
      }
    }

    endAsstMessage();
    currentAsstAcc = '';
    setSending(false);
    refreshState();
    refreshMemory();
  }

  function setSending(v) {
    els.sendBtn.disabled = v;
    els.stopBtn.disabled = !v;
    els.msgInput.disabled = v;
    els.sendBtn.textContent = v ? '生成中…' : '发送';
  }

  function stopChat() {
    if (chatAbortController) {
      chatAbortController.abort();
      chatAbortController = null;
    }
    liveResumeAbort = true;
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
        toast('已重置', 'ok');
        refreshState();
        refreshMemory();
        refreshFiles();
      } catch (e) { toast(e.message, 'error'); }
    });

    els.userSelect.addEventListener('change', async () => {
      liveResumeAbort = true; // 终止上一个用户的续接轮询
      currentUser = els.userSelect.value;
      await loadHistory();
      refreshState();
      refreshMemory();
      refreshKb();
      refreshFiles();
      tryResumeLiveGeneration();
      toast(`切换到用户：${currentUser}`, 'ok');
    });

    els.addUserBtn.addEventListener('click', async () => {
      const newU = els.newUserInput.value.trim();
      if (!newU) { toast('请输入用户名', 'error'); return; }
      try {
        const r = await api('/api/users', {
          method: 'POST',
          body: JSON.stringify({ user_id: newU }),
        });
        els.newUserInput.value = '';
        currentUser = r.user_id;
        await refreshUsersList();
        loadHistory();
        refreshState();
        refreshMemory();
        refreshKb();
        refreshFiles();
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
    els.refreshKbBtn.addEventListener('click', refreshKb);
    els.refreshFilesBtn.addEventListener('click', refreshFiles);

    // 知识库检索
    els.kbSearchBtn.addEventListener('click', doKbSearch);
    els.kbQuery.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') doKbSearch();
    });

    // Tab 切换（含键盘方向键支持）
    bindTabs('.tab-btn');
    document.querySelectorAll('.tab-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach((b) => {
          b.classList.remove('active');
          b.setAttribute('tabindex', '-1');
          b.setAttribute('aria-selected', 'false');
        });
        btn.classList.add('active');
        btn.setAttribute('tabindex', '0');
        btn.setAttribute('aria-selected', 'true');
        const tab = btn.getAttribute('data-tab');
        document.querySelectorAll('.tab-pane').forEach((p) => {
          p.classList.toggle('hidden', p.getAttribute('data-pane') !== tab);
        });
        if (tab === 'memory') refreshMemory();
        else if (tab === 'kb') refreshKb();
        else if (tab === 'files') refreshFiles();
      });
    });
  }

  // ---------- 欢迎提示 ----------
  function bindWelcomeHints() {
    document.querySelectorAll('.welcome-hint').forEach((el) => {
      el.addEventListener('click', () => {
        const hint = el.getAttribute('data-hint');
        if (hint) {
          els.msgInput.value = hint;
          els.msgInput.focus();
        }
      });
    });
  }
  bindWelcomeHints();

  // ---------- 初始化 ----------
  async function init() {
    bind();
    currentUser = await resolveCurrentUser(urlUser, currentUser);
    await refreshUsersList();
    currentUser = els.userSelect.value || 'main';
    await loadHistory();
    refreshState();
    refreshMemory();
    refreshKb();
    refreshFiles();
    tryResumeLiveGeneration();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
