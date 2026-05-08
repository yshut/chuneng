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
  let currentUser = 'main';
  let chatAbortController = null;
  let currentAsstBubble = null;       // 当前 assistant 主气泡（流式追加）
  let currentAsstContainer = null;    // 当前 assistant 整条消息容器（含工具事件）

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
  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    });
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      throw new Error(`${res.status} ${res.statusText} ${text}`);
    }
    return res.json();
  }

  async function refreshUsers() {
    const r = await api('/api/users');
    els.userSelect.innerHTML = '';
    for (const u of r.users) {
      const opt = document.createElement('option');
      opt.value = u; opt.textContent = u;
      if (u === currentUser) opt.selected = true;
      els.userSelect.appendChild(opt);
    }
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
  async function uploadFiles(fileList, fromFolder, statusEl) {
    if (!fileList || !fileList.length) return;
    statusEl.textContent = `上传中 (${fileList.length})...`;
    const fd = new FormData();
    for (const f of fileList) fd.append('files', f, f.name);
    fd.append('user_id', currentUser);
    fd.append('from_folder', fromFolder ? '1' : '0');
    try {
      const res = await fetch('/api/upload', { method: 'POST', body: fd });
      if (!res.ok) throw new Error(await res.text());
      const r = await res.json();
      statusEl.innerHTML = `<span style="color:var(--ok)">✓ 已上传 ${r.copied.length}</span>` +
        (r.skipped.length ? ` <span class="muted">（跳过 ${r.skipped.length}）</span>` : '') +
        (r.errors && r.errors.length ? ` <span style="color:var(--stop)">错误 ${r.errors.length}</span>` : '');
      refreshFiles(); refreshState();
    } catch (e) {
      statusEl.innerHTML = `<span style="color:var(--stop)">❌ ${escapeHtml(e.message)}</span>`;
    }
  }

  async function uploadKbFiles(fileList, fromFolder) {
    if (!fileList || !fileList.length) return;
    els.kbUploadStatus.textContent = `索引中 (${fileList.length})...`;
    const fd = new FormData();
    for (const f of fileList) fd.append('files', f, f.name);
    fd.append('user_id', currentUser);
    fd.append('from_folder', fromFolder ? '1' : '0');
    try {
      const res = await fetch('/api/kb/index', { method: 'POST', body: fd });
      if (!res.ok) throw new Error(await res.text());
      const r = await res.json();
      const ok = (r.results || []).filter(x => x.ok).length;
      const fail = (r.results || []).filter(x => !x.ok).length;
      els.kbUploadStatus.innerHTML = `<span style="color:var(--ok)">✓ 成功 ${ok}</span>` +
        (fail ? ` <span style="color:var(--stop)">失败 ${fail}</span>` : '');
      refreshKb();
    } catch (e) {
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
      if (!res.ok) throw new Error(await res.text());
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
        toast('已重置', 'ok');
        refreshState(); refreshMemory(); refreshFiles();
      } catch (e) { toast(e.message, 'error'); }
    });

    els.userSelect.addEventListener('change', () => {
      currentUser = els.userSelect.value;
      loadHistory();
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
    await refreshUsers();
    currentUser = els.userSelect.value || 'main';
    loadHistory();
    refreshState(); refreshMemory(); refreshKb(); refreshFiles();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
