"""储能配置AGENT - Gradio Web UI

功能：
- 流式对话（边生成边显示）
- 文件上传（PDF/Word/Excel/图片）→ 自动复制到 input/ 并通知 Agent
- 工具调用过程可视化（折叠面板显示每步调用）
- 当前分析状态面板（电费数据、最优配置、报告）
- 长期记忆面板（文本三文件 + 向量库状态）
- 图表面板（年度收益、月度对比、敏感性热力、成本构成、现金流瀑布）
- 工具列表（含插件）
- 多用户切换（user_id）

启动：
    python main.py --web                    # 默认 127.0.0.1:7860
    python main.py --web --port 8080
    python main.py --web --share            # 公网链接
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ======================================================================
# 多用户 Agent 管理
# ======================================================================
class AgentManager:
    """按 user_id 维护 StorageAgent 实例。每个用户独立的状态、记忆、向量库。"""

    def __init__(self, base_config, enable_reranker: bool = False,
                 reranker_model: str = "BAAI/bge-reranker-v2-m3",
                 max_tool_retries: int = 2):
        self.base_config = base_config
        self.enable_reranker = enable_reranker
        self.reranker_model = reranker_model
        self.max_tool_retries = max_tool_retries
        self._agents: dict = {}

    def get(self, user_id: str):
        from agent_core import StorageAgent
        from hier_memory import safe_user_id
        uid = safe_user_id(user_id)
        if uid not in self._agents:
            self._agents[uid] = StorageAgent(
                self.base_config, user_id=uid, verbose=False,
                enable_reranker=self.enable_reranker,
                reranker_model=self.reranker_model,
                max_tool_retries=self.max_tool_retries,
            )
        return self._agents[uid]

    def list_users(self) -> list[str]:
        from hier_memory import HierarchicalMemory
        on_disk = HierarchicalMemory.list_users(Path(self.base_config.output_dir) / "memory")
        in_memory = list(self._agents.keys())
        return sorted(set(on_disk + in_memory + ["main"]))


# ======================================================================
# 格式化辅助函数
# ======================================================================
def _format_state(state) -> str:
    lines = [f"## 📊 当前状态（用户: `{state.user_id}`）\n"]
    if state.electricity_df is not None and not state.electricity_df.empty:
        df = state.electricity_df
        lines.append(f"- ✅ 电费数据已加载（{len(df)} 个月）")
        if "总电量(kWh)" in df.columns:
            total = df["总电量(kWh)"].sum()
            avg = df["总电量(kWh)"].mean()
            lines.append(f"  - 累计用电: **{total:,.0f} kWh**, 月均: **{avg:,.0f} kWh**")
    else:
        lines.append("- ⏳ 电费数据未加载")

    if state.optimal_config:
        c = state.optimal_config
        lines.append("- ✅ 最优配置已计算：")
        lines.append(f"  - 容量 **{c.battery_capacity_kwh:,.0f} kWh** / 功率 **{c.inverter_power_kw:,.0f} kW**")
        lines.append(f"  - 投资 **{c.total_investment:,.0f} 元** / 回收期 **{c.simple_payback_years:.2f} 年** / IRR **{c.irr*100:.2f}%**")
    else:
        lines.append("- ⏳ 最优配置未计算")

    lines.append(f"- {'✅' if state.revenue_report else '⏳'} 收益报告")
    lines.append(f"- {'✅' if state.investor_report else '⏳'} 资方/客户分析（{state.config.investor_config.investment_mode}）")
    lines.append(f"- {'✅' if state.md_report else '⏳'} 智能 Markdown 报告")

    rate = state.config.rate_config
    lines.append("\n## ⚡ 当前电价")
    lines.append(f"- 尖峰 {rate.peak_price} / 高峰 {rate.high_price} / 平段 {rate.flat_price} / 谷段 {rate.valley_price}（元/kWh）")
    lines.append(f"- 需量电费 {rate.demand_charge} 元/kW/月")

    storage = state.config.storage_config
    lines.append("\n## 🔋 储能参数")
    lines.append(f"- 电池成本 {storage.battery_cost_per_kwh} 元/kWh")
    lines.append(f"- 项目寿命 {storage.project_life_years} 年 / 折现率 {storage.discount_rate*100:.1f}%")
    return "\n".join(lines)


def _format_memory(state) -> str:
    lines = [f"## 🧠 长期记忆（用户: `{state.user_id}`）\n"]

    if state.memory is None:
        lines.append("- 分层记忆: ⏳ 未启用")
    else:
        stats = state.memory.stats()
        lines.append("### 📚 分层记忆（MemGPT 风格）")
        lines.append(f"- 目录: `{stats['base_dir']}`")
        w = stats.get("working", {})
        lines.append(
            f"- **WORKING**：{w.get('messages',0)} 条 / "
            f"{w.get('size_kb',0)} KB / 窗口上限 {w.get('window_size',0)}"
        )
        s = stats.get("summaries", {})
        lines.append(f"- **SUMMARIES**：{s.get('count',0)} 段（注入上限 {s.get('max_in_context',0)}）")
        f = stats.get("facts", {})
        lines.append(f"- **FACTS**：{f.get('count',0)} 条 / 最多 {f.get('max',0)}")
        t = stats.get("tool_log", {})
        lines.append(f"- **TOOL_LOG**：{t.get('size_kb',0)} KB / 上限 {t.get('max_mb',0)} MB")

        # 列出最新 facts（前 8 条）
        try:
            facts = state.memory.list_facts()
        except Exception:
            facts = []
        if facts:
            lines.append("\n#### 🧷 最近事实（最新 8 条）")
            for it in facts[:8]:
                lines.append(f"- **{it['key']}** = {it['value']}  _<sub>({it.get('updated_at','')[-8:]})</sub>_")

    lines.append("\n### 🧬 向量记忆（ChromaDB）")
    if state.vector_memory is None or not state.vector_memory.available:
        lines.append("- ⏳ 未启用（缺少 chromadb 或 embedding API）")
    else:
        vstats = state.vector_memory.stats()
        lines.append(f"- 嵌入器: `{vstats['embedder']}`（{vstats['dimension']} 维）")
        lines.append(f"- 向量条数: **{vstats['count']}**")
        lines.append(f"- 数据库: `{vstats['db_dir']}`")

    lines.append("\n### 📚 知识库（共享 RAG）")
    kb = getattr(state, "kb", None)
    if kb is None or not kb.is_ready:
        lines.append("- ⏳ 未启用")
    else:
        ks = kb.stats()
        lines.append(f"- 文档数: **{ks['documents']}** / chunk: **{ks['chunks']}**")
        lines.append(f"- 目录: `{ks['base_dir']}`")
        rr = ks.get("reranker")
        if rr:
            lines.append(f"- 重排器: `{rr}`（启用）")
        else:
            lines.append("- 重排器: 未启用（仅向量分排序）")
    return "\n".join(lines)


def _format_tools(registry) -> str:
    lines = [f"## 🔧 可用工具（共 {len(registry.all())} 个）\n"]
    for t in registry.all():
        lines.append(f"- **{t.name}** — {t.description[:90]}")
    return "\n".join(lines)


def _format_df_preview(df: Optional[pd.DataFrame]) -> str:
    if df is None or df.empty:
        return "（暂无电费数据）"
    return df.head(12).to_markdown(index=False)


def _render_tool_log(tool_log: list, ongoing: bool) -> str:
    if not tool_log:
        return ""
    parts = ["\n\n<details open><summary>🔧 工具调用记录（点击折叠）</summary>\n"]
    for i, t in enumerate(tool_log, 1):
        args_str = json.dumps(t["args"], ensure_ascii=False)
        if len(args_str) > 200:
            args_str = args_str[:200] + "..."
        # 失败标记
        if t.get("exhausted"):
            badge = " ⛔ 重试已用尽"
        elif t.get("error"):
            badge = f" ⚠️ 失败 (第 {t.get('attempt','?')}/{t.get('max_retries','?')} 次)"
        else:
            badge = ""
        parts.append(f"\n**{i}. {t['name']}**{badge}")
        parts.append(f"\n- 参数: `{args_str}`")
        if t.get("error"):
            parts.append(f"\n- 错误: `{str(t['error'])[:200]}`")

        # 子 Agent 阶段
        for sub in t.get("subagent_events", []):
            phase = sub.get("phase", "")
            role = sub.get("role", "?")
            if phase == "subagent_start":
                parts.append(f"\n- ⇨ 委托【{role}-Agent】: {sub.get('task','')[:120]}")
            elif phase == "subagent_tool":
                parts.append(f"\n  - [{role}] tool `{sub.get('tool','')}`")
            elif phase == "subagent_finish":
                parts.append(f"\n- ⇦ 【{role}-Agent】完成（{sub.get('iter',0)}轮）")

        # 进度
        progresses = t.get("progress", [])
        if progresses:
            last = progresses[-1]
            if "step" in last and "total" in last:
                pct = last["step"] / max(last["total"], 1) * 100
                bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
                parts.append(f"\n- 进度: `{bar}` {last['step']}/{last['total']} {last.get('phase','')} {last.get('name','') or last.get('source','')}")
            else:
                parts.append(f"\n- 中间状态: `{json.dumps(last, ensure_ascii=False)[:140]}`")

        if t["result"] is not None:
            r = t["result"][:400]
            if len(t["result"]) > 400:
                r += "..."
            parts.append(f"\n- 结果: `{r}`")
        else:
            parts.append("\n- 结果: ⏳ 执行中...")
    parts.append("\n\n</details>")
    return "".join(parts)


def _render_reflections(reflections: list) -> str:
    if not reflections:
        return ""
    parts = ["\n\n> 💭 **思考链路（ReAct 反思）**\n"]
    for i, r in enumerate(reflections, 1):
        parts.append(f"> {i}. {r}".replace("\n", "\n> "))
    return "\n".join(parts) + "\n"


# ======================================================================
# Gradio 跨版本兼容：5.x 需要 type="messages"，6.x 移除了该参数
# ======================================================================
def _make_chatbot(gr_module, **kwargs):
    """构造 Chatbot，兼容 gradio 5 / 6。"""
    # 6.x 默认就是 messages 格式；5.x 要显式声明
    try:
        import gradio
        major = int(gradio.__version__.split(".")[0])
    except Exception:
        major = 5
    if major < 6:
        kwargs.setdefault("type", "messages")
    # gradio 6 还移除了 avatar_images 等老参数，做一次容错调用
    try:
        return gr_module.Chatbot(**kwargs)
    except TypeError as e:
        # 移除不被支持的关键字逐个重试
        bad = []
        msg = str(e)
        for k in list(kwargs.keys()):
            if f"'{k}'" in msg or f"keyword argument '{k}'" in msg:
                bad.append(k)
        for k in bad:
            kwargs.pop(k, None)
        # 兜底再清掉一些已知 v6 移除项
        for k in ("avatar_images", "show_copy_button", "type"):
            kwargs.pop(k, None)
        return gr_module.Chatbot(**kwargs)


# ======================================================================
# 主入口
# ======================================================================
def build_app(manager: AgentManager, default_user: str = "main"):
    try:
        import gradio as gr
    except ImportError:
        raise ImportError(
            "Gradio 未安装，请运行: pip install gradio\n"
            "或安装完整依赖: pip install -r requirements.txt"
        )

    INPUT_DIR = Path(manager.base_config.input_dir)
    INPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ----------------------------- 处理函数 -----------------------------
    SUPPORTED_EXTS = {".pdf", ".docx", ".doc", ".xlsx", ".xls",
                       ".png", ".jpg", ".jpeg", ".csv", ".txt"}

    def upload_files(files: list, history: list, user_id: str, *, from_folder: bool = False):
        if not files:
            return history, "未选择文件"
        copied: list[str] = []
        skipped: list[str] = []
        for f in files:
            try:
                src = Path(f.name if hasattr(f, "name") else f)
                # 文件夹模式时按扩展名过滤，避免把 .git / .DS_Store 之类拷过来
                if from_folder and src.suffix.lower() not in SUPPORTED_EXTS:
                    skipped.append(src.name)
                    continue
                dst = INPUT_DIR / src.name
                # 同名文件 → 自动加序号避免覆盖
                if dst.exists():
                    stem, suf = dst.stem, dst.suffix
                    i = 1
                    while dst.exists():
                        dst = INPUT_DIR / f"{stem}_{i}{suf}"
                        i += 1
                shutil.copy2(src, dst)
                copied.append(dst.name)
            except Exception as e:
                logger.warning("复制文件失败 %s: %s", f, e)
        if not copied:
            return history, "❌ 没有任何文件被上传（可能都被过滤了）"
        lines = [f"✅ 已上传 {len(copied)} 个文件到 input/："]
        for n in copied[:40]:
            lines.append(f"- {n}")
        if len(copied) > 40:
            lines.append(f"  ...（共 {len(copied)} 个，已省略 {len(copied)-40} 个）")
        if skipped:
            lines.append(f"\n⏭️ 已跳过 {len(skipped)} 个非支持类型文件")
        msg = "\n".join(lines)
        history = history + [
            {"role": "user", "content": f"我刚上传了 {len(copied)} 个文件" + ("（来自文件夹）" if from_folder else "")},
            {"role": "assistant", "content": msg},
        ]
        return history, msg

    def upload_folder(files: list, history: list, user_id: str):
        return upload_files(files, history, user_id, from_folder=True)

    def chat_fn(user_message: str, history: list, user_id: str):
        agent = manager.get(user_id)
        state = agent.state
        if not user_message or not user_message.strip():
            yield (history, _format_state(state), _format_memory(state),
                   _format_df_preview(state.electricity_df), "")
            return

        history = history + [{"role": "user", "content": user_message}]
        history = history + [{"role": "assistant", "content": ""}]

        assistant_text = ""
        tool_log: list = []
        reflections: list[str] = []
        current_reflection = ""

        def _render():
            body = assistant_text
            if current_reflection or reflections:
                all_reflections = reflections + ([current_reflection] if current_reflection else [])
                body += _render_reflections(all_reflections)
            body += _render_tool_log(tool_log, ongoing=True)
            return body

        try:
            for ev in agent.chat_stream(user_message):
                t = ev["type"]
                if t == "text":
                    # 文本进来 → 反思段落已结束
                    if current_reflection:
                        reflections.append(current_reflection)
                        current_reflection = ""
                    assistant_text += ev["delta"]
                elif t == "reflection":
                    current_reflection += ev["delta"]
                elif t == "tool":
                    if current_reflection:
                        reflections.append(current_reflection)
                        current_reflection = ""
                    tool_log.append({
                        "name": ev["name"], "args": ev["args"],
                        "result": None, "progress": [], "subagent_events": [],
                    })
                elif t == "tool_progress":
                    if tool_log and tool_log[-1]["name"] == ev.get("name"):
                        tool_log[-1]["progress"].append(ev.get("progress") or {})
                elif t == "subagent":
                    if tool_log and tool_log[-1]["name"] == ev.get("name"):
                        tool_log[-1]["subagent_events"].append(ev)
                elif t == "tool_error":
                    if tool_log and tool_log[-1]["name"] == ev.get("name"):
                        tool_log[-1]["error"] = ev.get("error")
                        tool_log[-1]["attempt"] = ev.get("attempt")
                        tool_log[-1]["max_retries"] = ev.get("max_retries")
                        tool_log[-1]["exhausted"] = ev.get("exhausted", False)
                elif t == "tool_result":
                    if tool_log and tool_log[-1]["name"] == ev["name"]:
                        tool_log[-1]["result"] = ev["result"]
                elif t == "final":
                    if current_reflection:
                        reflections.append(current_reflection)
                        current_reflection = ""
                    assistant_text = ev["content"] or assistant_text
                elif t == "error":
                    history[-1]["content"] = f"❌ {ev['message']}"
                    yield (history, _format_state(state), _format_memory(state),
                           _format_df_preview(state.electricity_df), "")
                    return

                history[-1]["content"] = _render()
                yield (history, _format_state(state), _format_memory(state),
                       _format_df_preview(state.electricity_df), "")
        except Exception as e:
            logger.exception("对话出错")
            history[-1]["content"] = f"❌ 处理出错: {e}"
            yield (history, _format_state(state), _format_memory(state),
                   _format_df_preview(state.electricity_df), "")

    def clear_chat(history: list, user_id: str):
        agent = manager.get(user_id)
        agent.reset()
        s = agent.state
        return [], _format_state(s), _format_memory(s), _format_df_preview(s.electricity_df), ""

    def reset_all(history: list, user_id: str):
        agent = manager.get(user_id)
        agent.reset_all()
        s = agent.state
        return [], _format_state(s), _format_memory(s), _format_df_preview(s.electricity_df), "✓ 已彻底重置（含长期记忆和向量库）"

    def list_input_files() -> str:
        files = sorted([f for f in INPUT_DIR.glob("*") if f.is_file()])
        if not files:
            return "（input/ 目录为空）"
        return "\n".join(f"- {f.name} ({f.stat().st_size // 1024} KB)" for f in files)

    def refresh_panels(user_id: str):
        agent = manager.get(user_id)
        s = agent.state
        return (
            _format_state(s),
            _format_memory(s),
            _format_df_preview(s.electricity_df),
            _format_tools(agent.registry),
            list_input_files(),
        )

    def switch_user_fn(user_id: str):
        # 触发拿这个用户的 agent（创建/复用）
        agent = manager.get(user_id)
        s = agent.state
        return (
            [],  # 清空聊天框
            _format_state(s),
            _format_memory(s),
            _format_df_preview(s.electricity_df),
            f"已切换到用户：{user_id}",
            gr.update(choices=manager.list_users(), value=user_id),
        )

    def add_new_user_fn(new_user: str, current_user: str):
        new_user = (new_user or "").strip()
        if not new_user:
            return current_user, gr.update(choices=manager.list_users()), "请输入用户名"
        # 触发创建
        manager.get(new_user)
        users = manager.list_users()
        return new_user, gr.update(choices=users, value=new_user), f"✓ 已创建/切换到：{new_user}"

    # ----------- 图表 -----------
    def render_chart(chart_name: str, user_id: str):
        from webui_charts import render
        agent = manager.get(user_id)
        img = render(agent.state, chart_name)
        if img is None:
            return None, "⚠️ 无法生成图表：相关数据未准备好（请先完成分析）"
        return img, "✓ 已生成"

    def render_all_charts(user_id: str):
        from webui_charts import CHART_FUNCTIONS, render
        agent = manager.get(user_id)
        results = {name: render(agent.state, name) for name in CHART_FUNCTIONS}
        return (
            results.get("yearly_revenue"),
            results.get("monthly_revenue"),
            results.get("sensitivity_heatmap"),
            results.get("cost_breakdown_pie"),
            results.get("cashflow_waterfall"),
            "✓ 已生成全部图表" if any(results.values()) else "⚠️ 未生成任何图表（数据未准备好）",
        )

    # ----------- 知识库 -----------
    def kb_list_fn(user_id: str):
        agent = manager.get(user_id)
        kb = agent.state.kb
        if kb is None or not kb.is_ready:
            return "⚠️ 知识库未启用（请确认 `chromadb` 已安装且嵌入模型可用）"
        docs = kb.list_documents()
        s = kb.stats()
        lines = [f"## 📚 知识库\n- 文档数: **{s['documents']}**\n- 总 chunk: **{s['chunks']}**\n- 路径: `{s['base_dir']}`\n"]
        if not docs:
            lines.append("（暂无文档，请上传或调用 `index_document_to_kb`）")
        else:
            lines.append("| 文件 | chunk |\n| --- | --- |")
            for d in docs:
                lines.append(f"| `{d['source']}` | {d['chunks']} |")
        return "\n".join(lines)

    KB_SUPPORTED_EXTS = {".txt", ".md", ".markdown", ".pdf", ".docx", ".xlsx", ".csv"}

    def kb_index_fn(files: list, user_id: str, *, from_folder: bool = False):
        if not files:
            return kb_list_fn(user_id), "未选择文件"
        agent = manager.get(user_id)
        kb = agent.state.kb
        if kb is None or not kb.is_ready:
            return kb_list_fn(user_id), "❌ 知识库未启用"
        msgs: list[str] = []
        ok, fail, skipped = 0, 0, 0
        for f in files:
            src = Path(f.name if hasattr(f, "name") else f)
            if from_folder and src.suffix.lower() not in KB_SUPPORTED_EXTS:
                skipped += 1
                continue
            try:
                n = kb.index_file(str(src))
                msgs.append(f"✅ `{src.name}` 已入库 {n} chunks")
                ok += 1
            except Exception as e:
                msgs.append(f"❌ `{src.name}` 入库失败: {e}")
                fail += 1
        head = f"完成：成功 {ok}，失败 {fail}" + (f"，跳过 {skipped}" if skipped else "")
        # 文件夹模式时 chunk 信息可能很多，截断
        if len(msgs) > 50:
            msgs = msgs[:50] + [f"... 共 {ok + fail} 条，已省略后续 {ok + fail - 50} 条"]
        return kb_list_fn(user_id), head + "\n\n" + "\n".join(msgs)

    def kb_index_folder_fn(files: list, user_id: str):
        return kb_index_fn(files, user_id, from_folder=True)

    def kb_remove_fn(source: str, user_id: str):
        if not source.strip():
            return kb_list_fn(user_id), "请输入要删除的 source"
        agent = manager.get(user_id)
        kb = agent.state.kb
        if kb is None or not kb.is_ready:
            return kb_list_fn(user_id), "❌ 知识库未启用"
        n = kb.remove_document(source.strip())
        return kb_list_fn(user_id), f"✓ 已删除 source=`{source}` 的 {n} 个 chunk"

    def kb_search_fn(query: str, k: int, source_filter: str, user_id: str):
        if not query.strip():
            return "请输入查询内容"
        agent = manager.get(user_id)
        kb = agent.state.kb
        if kb is None or not kb.is_ready:
            return "❌ 知识库未启用"
        sf = source_filter.strip() or None
        hits = kb.search(query, k=int(k), source_filter=sf)
        if not hits:
            return f"未找到与「{query}」相关的内容"
        lines = [f"## 找到 {len(hits)} 个相关片段\n"]
        for i, h in enumerate(hits, 1):
            score = h.get("score") if h.get("score") is not None else 0
            lines.append(f"**{i}. `{h['source']}` · 相关度 {score:.3f}**")
            lines.append(f"> {h['content'][:400]}{'...' if len(h['content']) > 400 else ''}\n")
        return "\n".join(lines)

    # ----------- 语义检索 -----------
    def vector_search_fn(query: str, k: int, role: str, user_id: str):
        if not query.strip():
            return "请输入搜索内容"
        agent = manager.get(user_id)
        if agent.state.vector_memory is None or not agent.state.vector_memory.available:
            return "❌ 向量记忆未启用，请先安装 chromadb 并配置嵌入模型"
        where = None if role == "any" else {"role": role}
        hits = agent.state.vector_memory.search(query, k=int(k), where=where)
        if not hits:
            return f"未找到与「{query}」相关的历史记忆"
        lines = [f"## 找到 {len(hits)} 条相关记忆\n"]
        for i, h in enumerate(hits, 1):
            r = h["metadata"].get("role", "?")
            ts = h["metadata"].get("timestamp", 0)
            from datetime import datetime
            ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"
            lines.append(f"**{i}. [{r}] 相似度: {h['score']:.3f} · {ts_str}**")
            lines.append(f"> {h['text'][:300]}{'...' if len(h['text']) > 300 else ''}\n")
        return "\n".join(lines)

    # ----------------------------- 布局 -----------------------------
    initial_users = manager.list_users()
    if default_user not in initial_users:
        initial_users.insert(0, default_user)

    # theme 参数 Gradio 6.0 起从 Blocks 移到 launch()；这里两边兼容（旧版给 Blocks，新版会忽略并由 launch 接管）
    _theme = gr.themes.Soft(primary_hue="orange")
    _blocks_kwargs = {"title": "储能配置智能体", "fill_height": True}
    try:
        # Gradio < 6 接受 theme 参数
        _blocks_kwargs["theme"] = _theme
        with gr.Blocks(**_blocks_kwargs) as _probe:
            pass
    except TypeError:
        _blocks_kwargs.pop("theme", None)

    with gr.Blocks(**_blocks_kwargs) as demo:
        # 把 theme 挂到 demo 上，launch() 时再传
        demo._explicit_theme = _theme
        gr.Markdown(
            """# 🔋 储能配置智能体
LLM Function Calling · 流式响应 · ReAct 反思 · 多 Agent 协作 · 向量记忆 · RAG 知识库 · 图表可视化 · 插件扩展
"""
        )

        # 顶部用户选择
        with gr.Row():
            user_dropdown = gr.Dropdown(
                label="👤 当前用户",
                choices=initial_users,
                value=default_user,
                allow_custom_value=False,
                scale=2,
            )
            new_user_input = gr.Textbox(label="➕ 创建新用户", placeholder="user_id", scale=2)
            new_user_btn = gr.Button("创建并切换", scale=1)
            user_status = gr.Markdown("")

        with gr.Tabs():
            # ====================== Tab 1：对话 ======================
            with gr.Tab("💬 对话"):
                with gr.Row():
                    with gr.Column(scale=3):
                        chatbot = _make_chatbot(gr, label="对话", height=560,
                                                  show_copy_button=True,
                                                  avatar_images=(None, "🔋"))
                        with gr.Row():
                            user_input = gr.Textbox(
                                placeholder="例如：月用电50万度，最大需量2000kW，帮我配储能",
                                scale=8, container=False, lines=2,
                            )
                            send_btn = gr.Button("发送", variant="primary", scale=1)

                        with gr.Row():
                            clear_btn = gr.Button("清空对话")
                            reset_btn = gr.Button("彻底重置（当前用户）", variant="stop")
                            refresh_btn = gr.Button("刷新面板")

                        with gr.Accordion("📂 文件上传（自动放入 input/）", open=False):
                            gr.Markdown("**📄 上传单个或多个文件**（pdf/docx/xlsx/csv/png/jpg/txt）")
                            file_upload = gr.File(
                                label="点击或拖入文件（可多选）",
                                file_count="multiple",
                                file_types=[".pdf", ".docx", ".doc", ".xlsx", ".xls",
                                             ".png", ".jpg", ".jpeg", ".csv", ".txt"],
                            )
                            gr.Markdown("**📁 上传整个文件夹**（递归读取所有支持的文件，自动跳过 .git 等）")
                            folder_upload = gr.File(
                                label="点击选择整个目录",
                                file_count="directory",
                            )
                            upload_status = gr.Markdown()

                        gr.Examples(
                            examples=[
                                "我们工厂月用电50万度，最大需量2000kW，帮我配储能",
                                "用示例数据演示一下完整流程",
                                "委托数据 Agent 解析 input/ 然后让配置 Agent 优化，再让报告 Agent 出报告",
                                "做一次 A/B 实验：电池成本 800 vs 1200 元/kWh",
                                "做一次 A/B 实验：自投模式 vs EMC 7:3 分成",
                                "对比 磷酸铁锂(成本800) vs 三元(成本1200,循环4000)",
                                "搜知识库：江苏工商业分时电价规定",
                                "如果电池降到800元/kWh回收期会怎么变？",
                                "估算一下年 CO2 减排量",
                                "出个完整报告并导出 Excel",
                                "还记得我之前问的什么吗",
                            ],
                            inputs=user_input,
                            label="试试这些",
                        )

                    with gr.Column(scale=2):
                        with gr.Tabs():
                            with gr.Tab("📊 状态"):
                                state_md = gr.Markdown("")
                                df_preview = gr.Markdown(label="电费数据预览")
                            with gr.Tab("🧠 记忆"):
                                memory_md = gr.Markdown("")
                            with gr.Tab("🔧 工具"):
                                tools_md = gr.Markdown("")
                            with gr.Tab("📂 文件"):
                                files_md = gr.Markdown("")

            # ====================== Tab 2：图表 ======================
            with gr.Tab("📈 图表"):
                gr.Markdown("## 投资分析图表\n基于当前最优配置和收益报告生成图表。如果按钮点击后无图，请先在对话框中完成分析。")
                with gr.Row():
                    render_all_btn = gr.Button("🎨 生成全部图表", variant="primary")
                    chart_status = gr.Markdown("")

                with gr.Row():
                    with gr.Column():
                        gr.Markdown("### 📅 年度收益曲线")
                        yearly_img = gr.Image(label="", show_label=False, type="pil")
                    with gr.Column():
                        gr.Markdown("### 📊 月度对比")
                        monthly_img = gr.Image(label="", show_label=False, type="pil")

                with gr.Row():
                    with gr.Column():
                        gr.Markdown("### 🔥 敏感性热力图")
                        sens_img = gr.Image(label="", show_label=False, type="pil")
                    with gr.Column():
                        gr.Markdown("### 🥧 成本构成")
                        pie_img = gr.Image(label="", show_label=False, type="pil")

                gr.Markdown("### 💸 现金流瀑布")
                waterfall_img = gr.Image(label="", show_label=False, type="pil")

            # ====================== Tab 3：语义检索 ======================
            with gr.Tab("🔍 语义检索"):
                gr.Markdown("## 向量记忆语义检索\n基于嵌入向量检索整段历史，比关键词更智能。")
                with gr.Row():
                    sem_query = gr.Textbox(label="检索内容", placeholder="例如：当时讨论 EMC 分成的方案", scale=4)
                    sem_k = gr.Slider(1, 20, value=5, step=1, label="返回数量", scale=1)
                    sem_role = gr.Radio(["any", "user", "assistant"], value="any", label="角色", scale=1)
                    sem_btn = gr.Button("搜索", variant="primary", scale=1)
                sem_result = gr.Markdown("")

            # ====================== Tab 4：知识库 (RAG) ======================
            with gr.Tab("📚 知识库"):
                gr.Markdown(
                    "## 离线知识库（RAG）\n"
                    "把储能行业政策、电价文件、白皮书等导入向量库，对话时 Agent 会自动 `search_knowledge_base` 引用。"
                )
                with gr.Row():
                    kb_status_md = gr.Markdown(value="（点击下方刷新查看状态）")
                with gr.Row():
                    kb_refresh_btn = gr.Button("刷新", variant="secondary")
                    kb_action_status = gr.Markdown("")

                with gr.Accordion("📥 上传并索引文件（支持 .txt / .md / .pdf / .docx / .xlsx）", open=True):
                    gr.Markdown("**📄 选文件**（可多选）")
                    kb_files = gr.File(
                        label="点击或拖入文件",
                        file_count="multiple",
                        file_types=[".txt", ".md", ".markdown", ".pdf", ".docx", ".xlsx", ".csv"],
                    )
                    kb_index_btn = gr.Button("开始索引选中的文件", variant="primary")
                    gr.Markdown("---\n**📁 选文件夹**（递归读取整个目录中所有支持的文件）")
                    kb_folder = gr.File(
                        label="点击选择整个目录",
                        file_count="directory",
                    )
                    kb_folder_btn = gr.Button("开始索引整个文件夹", variant="primary")

                with gr.Accordion("🗑️ 删除某个 source", open=False):
                    kb_remove_input = gr.Textbox(label="source 文件名", placeholder="例如：江苏分时电价.pdf")
                    kb_remove_btn = gr.Button("删除", variant="stop")

                gr.Markdown("---")
                gr.Markdown("### 🔎 知识库检索（直接搜索，不走 LLM）")
                with gr.Row():
                    kb_query = gr.Textbox(label="查询", placeholder="例如：江苏工商业分时电价 谷段时段", scale=4)
                    kb_k = gr.Slider(1, 15, value=5, step=1, label="返回数", scale=1)
                    kb_source_filter = gr.Textbox(label="限定 source（可选）", scale=2)
                    kb_search_btn = gr.Button("搜索", variant="primary", scale=1)
                kb_search_result = gr.Markdown("")

        # ----------------------------- 绑定 -----------------------------
        # 用户切换
        user_dropdown.change(
            switch_user_fn,
            inputs=[user_dropdown],
            outputs=[chatbot, state_md, memory_md, df_preview, user_status, user_dropdown],
        ).then(
            kb_list_fn,
            inputs=[user_dropdown],
            outputs=kb_status_md,
        )
        new_user_btn.click(
            add_new_user_fn,
            inputs=[new_user_input, user_dropdown],
            outputs=[user_dropdown, user_dropdown, user_status],
        ).then(
            switch_user_fn,
            inputs=[user_dropdown],
            outputs=[chatbot, state_md, memory_md, df_preview, user_status, user_dropdown],
        )

        # 对话
        send_btn.click(
            chat_fn,
            inputs=[user_input, chatbot, user_dropdown],
            outputs=[chatbot, state_md, memory_md, df_preview, upload_status],
        ).then(lambda: "", outputs=user_input)

        user_input.submit(
            chat_fn,
            inputs=[user_input, chatbot, user_dropdown],
            outputs=[chatbot, state_md, memory_md, df_preview, upload_status],
        ).then(lambda: "", outputs=user_input)

        clear_btn.click(
            clear_chat,
            inputs=[chatbot, user_dropdown],
            outputs=[chatbot, state_md, memory_md, df_preview, upload_status],
        )
        reset_btn.click(
            reset_all,
            inputs=[chatbot, user_dropdown],
            outputs=[chatbot, state_md, memory_md, df_preview, upload_status],
        )
        refresh_btn.click(
            refresh_panels,
            inputs=[user_dropdown],
            outputs=[state_md, memory_md, df_preview, tools_md, files_md],
        )
        file_upload.upload(
            upload_files,
            inputs=[file_upload, chatbot, user_dropdown],
            outputs=[chatbot, upload_status],
        )
        folder_upload.upload(
            upload_folder,
            inputs=[folder_upload, chatbot, user_dropdown],
            outputs=[chatbot, upload_status],
        )

        # 图表
        render_all_btn.click(
            render_all_charts,
            inputs=[user_dropdown],
            outputs=[yearly_img, monthly_img, sens_img, pie_img, waterfall_img, chart_status],
        )

        # 语义检索
        sem_btn.click(
            vector_search_fn,
            inputs=[sem_query, sem_k, sem_role, user_dropdown],
            outputs=sem_result,
        )

        # 知识库
        kb_refresh_btn.click(
            kb_list_fn,
            inputs=[user_dropdown],
            outputs=kb_status_md,
        )
        kb_index_btn.click(
            kb_index_fn,
            inputs=[kb_files, user_dropdown],
            outputs=[kb_status_md, kb_action_status],
        )
        kb_folder_btn.click(
            kb_index_folder_fn,
            inputs=[kb_folder, user_dropdown],
            outputs=[kb_status_md, kb_action_status],
        )
        kb_remove_btn.click(
            kb_remove_fn,
            inputs=[kb_remove_input, user_dropdown],
            outputs=[kb_status_md, kb_action_status],
        )
        kb_search_btn.click(
            kb_search_fn,
            inputs=[kb_query, kb_k, kb_source_filter, user_dropdown],
            outputs=kb_search_result,
        )

        # 启动时填充面板
        demo.load(
            refresh_panels,
            inputs=[user_dropdown],
            outputs=[state_md, memory_md, df_preview, tools_md, files_md],
        ).then(
            kb_list_fn,
            inputs=[user_dropdown],
            outputs=kb_status_md,
        )

    return demo


def launch(config=None, port: int = 7860, share: bool = False, host: str = "127.0.0.1",
           default_user: str = "main", enable_reranker: bool = False,
           reranker_model: str = "BAAI/bge-reranker-v2-m3",
           max_tool_retries: int = 2):
    """主启动函数（被 main.py 调用）。"""
    from config import AgentConfig
    config = config or AgentConfig()
    manager = AgentManager(
        config,
        enable_reranker=enable_reranker,
        reranker_model=reranker_model,
        max_tool_retries=max_tool_retries,
    )
    # 预创建默认用户（避免首次访问慢）
    default_agent = manager.get(default_user)
    if not default_agent.available:
        print("⚠️  LLM 未启用（缺少 API Key），Web UI 仍可启动但无法对话。")
        print("   请设置环境变量 DASHSCOPE_API_KEY 后重启。")

    demo = build_app(manager, default_user=default_user)
    # Gradio 6+：theme 参数移到 launch()。两边兼容。
    launch_kwargs = dict(server_name=host, server_port=port, share=share, inbrowser=True)
    theme = getattr(demo, "_explicit_theme", None)
    if theme is not None:
        try:
            demo.queue().launch(theme=theme, **launch_kwargs)
            return
        except TypeError:
            pass
    demo.queue().launch(**launch_kwargs)


if __name__ == "__main__":
    launch()
