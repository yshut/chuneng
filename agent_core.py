"""储能配置AGENT - 智能体核心
基于 LLM Function Calling 的 Agent 主循环，支持：
- 流式响应（文本边生成边输出，工具调用累积后一次执行）
- 长期记忆（小端风格三文件）
- 插件式工具扩展（plugins/ 目录自动发现）

工作流：
1. 用户输入 → 写入 memory + 加入 messages
2. 把记忆上下文注入第一条消息后调用 LLM (带 tools 列表)
3. 若 LLM 返回 tool_calls：依次执行每个工具，结果以 role=tool 回填 → 继续循环
4. 若 LLM 返回纯文本 content：作为最终回复返回 + 写入 memory
5. 上限 N 轮防止死循环

使用：
    from config import AgentConfig
    from agent_core import StorageAgent

    agent = StorageAgent(AgentConfig())
    print(agent.chat("月用电50万度，帮我配储能"))

    # 流式
    for ev in agent.chat_stream("看看EMC模式7:3分成"):
        if ev["type"] == "text":
            print(ev["delta"], end="", flush=True)
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from pathlib import Path
from typing import Generator, Optional

from agent_tools import AgentState, ToolRegistry, build_default_registry
from config import AgentConfig
from hier_memory import HierMemoryConfig, HierarchicalMemory, safe_user_id
from llm_client import LLMClient

try:
    from vector_memory import VectorMemory, VectorMemoryConfig, make_embedder
    _HAS_VECTOR = True
except ImportError:
    _HAS_VECTOR = False
    VectorMemory = None
    VectorMemoryConfig = None
    make_embedder = None

try:
    from knowledge_base import KnowledgeBase, KnowledgeBaseConfig
    _HAS_KB = True
except ImportError:
    _HAS_KB = False
    KnowledgeBase = None
    KnowledgeBaseConfig = None

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """你是一个专业的储能项目分析智能助手。你可以通过调用工具帮助用户完成以下工作：

1. 解析电费账单文件（PDF/Word/Excel/图片）或自然语言描述
2. 计算最优储能配置（电池容量、PCS功率、充放电策略）
3. 生成详细的收益分析（NPV、IRR、回收期、敏感性、风险）
4. 资方/客户收益分配（自投/贷款/EMC三种模式）
5. 多方案对比 / A-B 实验，给出投资建议
6. 生成 Markdown 智能报告，导出 Excel
7. 通过 RAG 知识库引用储能行业政策、电价文件等
8. 通过插件扩展更多能力（CO2减排估算、电池选型建议等）

## 工作原则

- **闲聊 / 问候 / 自我介绍优先不调工具**：当用户输入只是问候（如"你好"、"hi"、"在吗"、"早上好"、"hello"）、闲聊（"在干嘛"、"你是谁"、"能干啥"）、表达感谢、或纯粹的元提问（关于你的能力 / 用法）时，**直接用 1-3 句话友好回复**，**禁止调用任何工具**（包括 get_current_state、list_input_files、recall_memory）。等用户明确给出数据、文件、分析需求或具体问题时再开始调工具。
- **先理解再行动**：弄清用户想做什么，再决定调用哪些工具。每次调用工具前用 1 句话简述思路（不必长，但必须有），调用结果出来后做 1 句话反思再决定下一步。
- **必要时多步执行**：分析的标准链路是 加载数据 → optimize_storage → analyze_revenue → analyze_investor_customer → generate_report。
- **复杂任务用子 Agent 协作**：当用户给的是端到端任务（"读文件 → 配储能 → 出报告"），可拆分为：
  1) delegate_to_data_agent("解析数据并总结用电特征")
  2) delegate_to_config_agent("做储能优化和收益分析")
  3) delegate_to_writer_agent("撰写完整 Markdown 报告")
  这些子 Agent 共享同一份内存状态，会自动接力。
- **可并行的子任务用 parallel_subagents**：当几个子任务彼此独立（例：data_agent 解析数据 + writer_agent 提前查 KB 政策），用 parallel_subagents 一次性并发跑，节省一半时间。注意不要并发跑两个会写同一份 state 的角色（如两个 config_agent）。
- **遇到工具失败 → 立即修正**：若工具返回 `{"error": ...}`，仔细看错误消息、调整参数后**重新调用同一工具**；不要忽略错误直接给最终答复。系统会限制最多重试 2 次，超出后再换思路。
- **缺 Python 依赖 → 自助安装**：当工具错误信息含 `Missing optional dependency`、`No module named X`、`pip install X` 等关键字（例如解析 .xls 时缺 `xlrd`，或本地嵌入缺 `sentence-transformers`），**先调用 `install_python_package(package="xlrd", reason="...")`**，等返回 `import_ok=true` 后立即重试原工具；不要直接告诉用户"缺包请安装"。仅白名单内的可选依赖可装；安装失败或不在白名单时再走回退方案或告知用户。
- **长期事实显式登记**：若用户给出"长期有效"的关键参数（月用电量、合同容量、投资模式偏好等），用 `set_fact` 工具写进档案，下次会话也会自动注入。
- **A/B 对比就用 ab_experiment**：用户要"两种电价/两种电池/两种投资模式"对比时，优先用 ab_experiment 工具（一次跑两套，输出差异表）。
- **容量测算优先用账单逐月口径**：用户问储能容量、为什么推荐容量不同、或要求参考人工/GPT5.5思路时，优先调用 `analyze_storage_capacity_bill_method`。该工具按“逐月分时电价、峰/尖峰实际可承接电量、系统效率、需量兑现率、边际收益拐点”测算，不要只看旧 `optimize_storage` 的最高 IRR。
- **求"最优解 / 让大模型推荐 / 综合判断 / 我该选哪个"** → 在 `analyze_storage_capacity_bill_method` 时带上 `use_llm_review=true`，或者已经测算过的话调用 `llm_recommend_capacity` 让大模型在候选清单上给出推荐+理由。把用户的预算 / 回收期 / IRR 约束传给 `user_preference`。
- **政策/电价/标准类问题用 search_knowledge_base**：在知识库里检索权威片段后再回答，避免凭空捏造。用户上传的不是账单而是分时电价表、政策、并网/消防/补贴文件时，优先用 `parse_storage_related_documents` 解析并抽取可用于储能测算的参数，再结合知识库检索回答。
- **共享状态**：分析结果会被自动缓存。后续问答（如"回收期多长"）可直接基于已有结果回答，无需重复分析。
- **配置变更要重新分析**：若用户修改了电价、电池成本等参数，必须重新调用 optimize_storage。
- **善用长期记忆**：当用户提到"之前/上次/还记得"时，先 recall_memory 或 semantic_search_memory。
- **回复要简洁专业**：用中文，突出关键数据，必要时用 Markdown 列表。

## 工具调用提示

- 用户给文件路径 → 调用 parse_files（或委托 data_agent）
- 用户给分时电价/政策/并网/补贴等非账单文件 → 调用 parse_storage_related_documents
- 用户口述用电情况 → 调用 parse_natural_language
- 用户没数据想看演示 → 调用 use_demo_data
- 用户想出完整报告 → 委托 writer_agent，或直接 generate_report + export_excel
- 用户问"政策怎么规定" → search_knowledge_base
- 用户问"按账单参考GPT5.5思路算容量/为什么容量不一样" → analyze_storage_capacity_bill_method
- 用户说"让大模型推荐 / 给我最优解 / 综合判断哪个方案" → analyze_storage_capacity_bill_method(use_llm_review=true) 或 llm_recommend_capacity
- 用户问"两种方案哪个好" → ab_experiment 或 compare_scenarios
- 用户想回忆过去 → recall_memory / semantic_search_memory"""


class StorageAgent:
    """储能配置智能体 - 基于 LLM Function Calling，支持流式 + 长期记忆 + 插件。"""

    def __init__(self, config: AgentConfig = None, registry: ToolRegistry = None,
                 max_iterations: int = 10, verbose: bool = True,
                 enable_memory: bool = True,
                 enable_vector_memory: bool = True,
                 enable_kb: bool = True,
                 enable_react: bool = True,
                 enable_reranker: bool = False,            # 首次使用需下载 ~600MB 模型
                 reranker_model: str = "BAAI/bge-reranker-v2-m3",
                 max_tool_retries: int = 2,                 # 单工具失败后的最大自动重试次数
                 user_id: str = "main",
                 plugins_dir: str | Path = "plugins"):
        self.config = config or AgentConfig()
        self.user_id = safe_user_id(user_id)
        self.state = AgentState(config=self.config)
        self.state.user_id = self.user_id
        self.max_iterations = max_iterations
        self.verbose = verbose
        self.enable_memory = enable_memory
        self.enable_vector_memory = enable_vector_memory
        self.enable_kb = enable_kb
        self.enable_react = enable_react
        self.enable_reranker = enable_reranker
        self.reranker_model = reranker_model
        self.max_tool_retries = max_tool_retries

        # 工具注册表（默认 + 插件）
        self.registry = registry or build_default_registry()
        self.plugins_dir = Path(plugins_dir)
        if self.plugins_dir.exists():
            n = self.registry.load_plugins(self.plugins_dir)
            if n > 0:
                logger.info("从 %s 加载了 %d 个插件工具", self.plugins_dir, n)

        # LLM 客户端
        self.llm = LLMClient(self.config.llm_config)

        # 注入 LLM 到 state，供工具内部使用
        if self.llm.available:
            try:
                from llm_document_parser import LLMDocumentParser
                from llm_report_generator import LLMReportGenerator
                self.state.llm_parser = LLMDocumentParser(
                    self.llm, self.state.parser, self.state.extractor
                )
                self.state.llm_report_gen = LLMReportGenerator(self.llm)
            except Exception as e:
                logger.warning("LLM 辅助组件初始化失败: %s", e)

        # 把 LLM 客户端 + 工具注册表注入到 state，供子 Agent 委托工具使用
        self.state.llm_client = self.llm
        self.state.tool_registry = self.registry

        # 长期记忆（按 user_id 隔离）+ 知识库（共享）
        self._init_memory()
        self._init_kb()

        # 对话历史（每轮 chat 都会重新构造，加上记忆上下文）
        self._reset_messages()

    # ------------------------------------------------------------------
    # 多用户切换
    # ------------------------------------------------------------------
    def switch_user(self, new_user_id: str):
        """切换 user_id：重新加载该用户的记忆，清空当前对话。"""
        new_user_id = safe_user_id(new_user_id)
        if new_user_id == self.user_id:
            return
        self.user_id = new_user_id
        self.state.user_id = new_user_id
        self._init_memory()
        self._reset_messages()
        logger.info("已切换到用户: %s", new_user_id)

    def _init_memory(self):
        """根据当前 user_id 初始化分层记忆 + 向量记忆。"""
        mem_dir = Path(self.config.output_dir) / "memory"
        if self.enable_memory:
            self.state.memory = HierarchicalMemory(HierMemoryConfig(
                base_dir=str(mem_dir),
                user_id=self.user_id,
                llm=self.llm if getattr(self.llm, "available", False) else None,
            ))
        else:
            self.state.memory = None

        if self.enable_vector_memory and _HAS_VECTOR:
            try:
                provider = (self.config.llm_config.provider or "").lower()
                prefer = "qwen" if provider == "qwen" else "local"
                self.state.vector_memory = VectorMemory(VectorMemoryConfig(
                    base_dir=mem_dir, user_id=self.user_id,
                    embedder_prefer=prefer,
                ))
                if not self.state.vector_memory.available:
                    self.state.vector_memory = None
            except Exception as e:
                logger.warning("向量记忆初始化失败: %s", e)
                self.state.vector_memory = None
        else:
            self.state.vector_memory = None

    def _init_kb(self):
        """初始化共享知识库（所有用户共享）。"""
        if not (self.enable_kb and _HAS_KB):
            self.state.kb = None
            return
        try:
            # 嵌入器优先级：
            # - provider=qwen → Qwen API（自带 embedding）
            # - 其他（mimo/openai_compat/wenxin）→ 中转大概率没 embedding，优先本地
            provider = (self.config.llm_config.provider or "").lower()
            prefer = "qwen" if provider == "qwen" else "local"
            embedder = None
            if make_embedder is not None:
                embedder = make_embedder(prefer=prefer, llm_config=self.config.llm_config)
            if embedder is None:
                logger.warning(
                    "KB 嵌入模型不可用（provider=%s）。"
                    "如需本地嵌入：pip install sentence-transformers", provider
                )
                self.state.kb = None
                return

            # 可选 reranker（首次会下载模型，失败不影响 KB 主功能）
            reranker = None
            if self.enable_reranker:
                try:
                    from reranker import make_reranker
                    reranker = make_reranker(self.reranker_model)
                    logger.info("KB reranker: %s", getattr(reranker, "name", "?"))
                except Exception as e:
                    logger.info("Reranker 未启用（%s），将仅用向量分", e)

            kb_dir = Path(self.config.output_dir) / "knowledge_base"
            self.state.kb = KnowledgeBase(KnowledgeBaseConfig(
                base_dir=str(kb_dir),
                collection_name="kb_default",
                embedder=embedder,
                reranker=reranker,
            ))
        except Exception as e:
            logger.warning("知识库初始化失败: %s", e)
            self.state.kb = None

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return self.llm.available

    def reset(self):
        """清空当前会话对话（保留分析状态和长期记忆文件）。"""
        self._reset_messages()

    def reset_all(self):
        """彻底重置：清空对话、分析结果，并清空当前用户的长期记忆（含向量库）。"""
        self._reset_messages()
        # 先清空当前用户的记忆文件 + 向量库
        if self.state.memory:
            self.state.memory.clear()
        if self.state.vector_memory and self.state.vector_memory.available:
            self.state.vector_memory.clear()

        # 重建 state
        self.state = AgentState(config=self.config)
        self.state.user_id = self.user_id
        # 重新注入 LLM 客户端 / 工具注册表（子 Agent + 并行调度依赖）
        self.state.llm_client = self.llm
        self.state.tool_registry = self.registry
        if self.llm.available:
            try:
                from llm_document_parser import LLMDocumentParser
                from llm_report_generator import LLMReportGenerator
                self.state.llm_parser = LLMDocumentParser(
                    self.llm, self.state.parser, self.state.extractor
                )
                self.state.llm_report_gen = LLMReportGenerator(self.llm)
            except Exception:
                pass
        # 重新挂记忆 + 知识库
        self._init_memory()
        self._init_kb()

    def chat(self, user_input: str) -> str:
        """单轮对话（非流式）。返回最终文本。"""
        if not self.llm.available:
            return "❌ LLM 不可用，请先配置 API Key（DASHSCOPE_API_KEY 或 BAIDU_API_KEY）。"

        self._before_user_input(user_input)

        # 非流式：把流式事件全部消费完，取最终 content
        final_text = ""
        for ev in self._run_loop_stream():
            if ev["type"] == "final":
                final_text = ev["content"]

        if final_text:
            if self.state.memory:
                self.state.memory.append_assistant(final_text)
            if self.state.vector_memory and self.state.vector_memory.available:
                self.state.vector_memory.add(
                    final_text, metadata={"role": "assistant", "user_id": self.user_id}
                )
        return final_text

    def chat_stream(self, user_input: str) -> Generator[dict, None, None]:
        """流式对话生成器。

        yield 的事件字典：
            {"type": "text",   "delta": "...", "iteration": int}            # 文本增量
            {"type": "tool",   "name": "...", "args": {...}}                  # 即将执行工具
            {"type": "tool_progress", "name": "...", "progress": {...}}       # 工具中间进度
            {"type": "tool_result",   "name": "...", "result": "..."}         # 工具结果
            {"type": "subagent",      "role": "...", "phase": "..."}          # 子 Agent 阶段
            {"type": "reflection",    "delta": "..."}                          # ReAct 反思
            {"type": "final",  "content": "..."}                              # 最终回复
            {"type": "error",  "message": "..."}
        """
        if not self.llm.available:
            yield {"type": "error", "message": "❌ LLM 不可用，请配置 API Key"}
            return

        self._before_user_input(user_input)

        final_text = ""
        for ev in self._run_loop_stream():
            yield ev
            if ev["type"] == "final":
                final_text = ev["content"]

        if final_text:
            if self.state.memory:
                self.state.memory.append_assistant(final_text)
            if self.state.vector_memory and self.state.vector_memory.available:
                self.state.vector_memory.add(
                    final_text, metadata={"role": "assistant", "user_id": self.user_id}
                )

    def run_interactive(self):
        """交互式终端循环（流式输出）。"""
        print("=" * 64)
        print("    储能配置智能体（Tool-Calling Agent + 流式 + 长期记忆 + 插件）")
        print("=" * 64)
        if not self.llm.available:
            print("❌ LLM 未启用。请配置环境变量 DASHSCOPE_API_KEY 后重试。")
            return

        print("可直接说话，例如：")
        print("  - '我们工厂月用电50万度，最大需量2000kW，帮我配储能'")
        print("  - '解析 input/ 目录下的所有文件'")
        print("  - '试试EMC模式 7:3 分成'")
        print("  - '电池降到800元再算一次'")
        print("  - '出个完整报告'")
        print("  - '还记得我之前问的电池成本吗'  ← 触发长期记忆检索")
        print("命令：")
        print("  /reset           清空当前对话（保留长期记忆）")
        print("  /reset-all       彻底重置当前用户（含长期记忆和向量库）")
        print("  /state           查看当前分析状态")
        print("  /memory          查看长期记忆状态（文本+向量）")
        print("  /tools           列出所有可用工具（含插件）")
        print("  /user            查看当前用户")
        print("  /switch <id>     切换到另一个用户")
        print("  /users           列出所有有记忆的用户")
        print("  /kb              查看知识库已索引文档")
        print("  /kb-add <path>   把文件加入知识库")
        print("  /react on|off    切换 ReAct 反思模式（默认开）")
        print("  /quit            退出")
        print(f"\n当前用户: {self.user_id}")
        print("=" * 64)

        while True:
            try:
                user_input = input("\n你> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见！")
                break

            if not user_input:
                continue
            if user_input in ("/quit", "/exit", "quit", "exit"):
                print("再见！")
                break
            if user_input == "/reset":
                self.reset()
                print("✓ 已清空当前对话")
                continue
            if user_input == "/reset-all":
                self.reset_all()
                print("✓ 已彻底重置（含长期记忆）")
                continue
            if user_input == "/state":
                from agent_tools import _tool_get_current_state
                s = _tool_get_current_state(self.state)
                print(json.dumps(s, ensure_ascii=False, indent=2, default=str))
                continue
            if user_input == "/memory":
                if self.state.memory:
                    print(json.dumps(self.state.memory.stats(), ensure_ascii=False, indent=2))
                else:
                    print("（未启用长期记忆）")
                continue
            if user_input == "/tools":
                for t in self.registry.all():
                    print(f"  - {t.name}: {t.description[:80]}")
                print(f"\n共 {len(self.registry.all())} 个工具")
                continue
            if user_input == "/user":
                print(f"当前用户: {self.user_id}")
                continue
            if user_input.startswith("/switch "):
                new_id = user_input[len("/switch "):].strip()
                if new_id:
                    self.switch_user(new_id)
                    print(f"✓ 已切换到用户: {self.user_id}")
                else:
                    print("用法：/switch <user_id>")
                continue
            if user_input == "/users":
                users = HierarchicalMemory.list_users(Path(self.config.output_dir) / "memory")
                if users:
                    print("已有记忆的用户：")
                    for u in users:
                        marker = " *" if u == self.user_id else ""
                        print(f"  - {u}{marker}")
                else:
                    print("（暂无任何用户记忆）")
                continue
            if user_input == "/kb":
                if self.state.kb is None:
                    print("（知识库未启用）")
                else:
                    docs = self.state.kb.list_documents()
                    print(json.dumps(self.state.kb.stats(), ensure_ascii=False, indent=2))
                    for d in docs:
                        print(f"  - {d['source']}: {d['chunks']} chunks")
                continue
            if user_input.startswith("/kb-add "):
                path = user_input[len("/kb-add "):].strip().strip('"')
                if not path:
                    print("用法：/kb-add <文件路径>")
                elif self.state.kb is None:
                    print("知识库未启用")
                else:
                    try:
                        n = self.state.kb.index_file(
                            path,
                            on_progress=lambda p: print(f"  ↳ {p}", flush=True),
                        )
                        print(f"✓ 已索引 {n} 个 chunk")
                    except Exception as e:
                        print(f"❌ 入库失败: {e}")
                continue
            if user_input.startswith("/react"):
                arg = user_input[len("/react"):].strip().lower()
                if arg in ("on", ""):
                    self.enable_react = True
                    print("✓ ReAct 反思已开启")
                elif arg == "off":
                    self.enable_react = False
                    print("✓ ReAct 反思已关闭")
                else:
                    print(f"当前 ReAct: {'ON' if self.enable_react else 'OFF'}")
                continue

            # 流式输出
            try:
                print("\n助手> ", end="", flush=True)
                in_reflection = False
                for ev in self.chat_stream(user_input):
                    t = ev["type"]
                    if t == "text":
                        if in_reflection:
                            print("\n助手> ", end="", flush=True)
                            in_reflection = False
                        print(ev["delta"], end="", flush=True)
                    elif t == "reflection":
                        if not in_reflection:
                            print("\n  [反思] ", end="", flush=True)
                            in_reflection = True
                        print(ev["delta"], end="", flush=True)
                    elif t == "tool" and self.verbose:
                        print(f"\n  [tool] {ev['name']}({json.dumps(ev['args'], ensure_ascii=False)[:120]})",
                              flush=True)
                    elif t == "tool_progress" and self.verbose:
                        p = ev.get("progress") or {}
                        if "step" in p and "total" in p:
                            print(f"\n    ↳ 进度 {p['step']}/{p['total']} {p.get('phase','')} {p.get('name','') or p.get('source','')}",
                                  flush=True)
                        else:
                            print(f"\n    ↳ {json.dumps(p, ensure_ascii=False)[:120]}", flush=True)
                    elif t == "subagent" and self.verbose:
                        role = ev.get("role", "?")
                        phase = ev.get("phase", "")
                        if phase == "subagent_start":
                            print(f"\n  ⇨ 委托给【{role}-Agent】: {ev.get('task','')[:80]}", flush=True)
                        elif phase == "subagent_tool":
                            print(f"\n     [{role}] tool {ev.get('tool')}", flush=True)
                        elif phase == "subagent_finish":
                            print(f"\n  ⇦ 【{role}-Agent】完成（{ev.get('iter', 0)}轮）", flush=True)
                    elif t == "tool_error" and self.verbose:
                        attempt = ev.get("attempt")
                        max_r = ev.get("max_retries")
                        if ev.get("exhausted"):
                            print(f"\n  ⛔ {ev['name']} 重试已用尽（{attempt}>{max_r}），将改用其他方式",
                                  flush=True)
                        else:
                            print(f"\n  ⚠️  {ev['name']} 失败 ({attempt}/{max_r})：{str(ev.get('error',''))[:140]}，准备修正参数重试",
                                  flush=True)
                    elif t == "tool_result" and self.verbose:
                        snippet = ev["result"][:200].replace("\n", " ")
                        print(f"\n  [result] {snippet}{'...' if len(ev['result']) > 200 else ''}",
                              flush=True)
                        print("助手> ", end="", flush=True)
                    elif t == "error":
                        print(f"\n❌ {ev['message']}", flush=True)
                print()  # 换行
            except Exception as e:
                logger.exception("处理出错")
                print(f"\n❌ 处理出错: {e}")

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _reset_messages(self):
        """重置 messages 列表（仅放系统提示，每次 chat 时再加记忆）。"""
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    def _replace_context_message(self, memory_ctx: str):
        """Insert or remove the dynamic memory/RAG context system message."""
        self.messages = [
            m for m in self.messages
            if not (
                m.get("role") == "system"
                and str(m.get("content", "")).startswith("## 运行时上下文")
            )
        ]
        if memory_ctx:
            self.messages.insert(1, {
                "role": "system",
                "content": f"## 运行时上下文\n{memory_ctx}",
            })

    # 简短问候 / 闲聊 / 元提问 —— 这类输入不注入历史上下文，避免诱导模型继续之前的工具流
    _SMALL_TALK_PATTERNS = (
        "你好", "您好", "哈喽", "嗨", "嘿", "hello", "hi", "hey",
        "在吗", "在么", "在不在", "早上好", "中午好", "下午好", "晚上好",
        "你是谁", "你能干嘛", "你能做什么", "能干啥", "有什么功能",
        "怎么用", "如何使用", "怎么开始", "test", "ping",
        "谢谢", "感谢", "thanks", "thank you", "ok", "好的", "嗯", "收到",
    )

    @classmethod
    def _is_small_talk(cls, text: str) -> bool:
        s = (text or "").strip().lower()
        if not s:
            return False
        # 长度阈值：≤ 12 字 / 30 字符，且没有典型业务关键词
        if len(s) > 30:
            return False
        business_keywords = (
            "电费", "电价", "账单", "储能", "电池", "容量", "功率", "峰谷", "需量",
            "收益", "回收期", "irr", "npv", "投资", "报告", "文件", "解析", "分析",
            ".pdf", ".xlsx", ".xls", ".docx", ".csv", ".png", ".jpg",
            "kwh", "kw·h", "kva", "mw",
        )
        if any(kw in s for kw in business_keywords):
            return False
        return any(p in s for p in cls._SMALL_TALK_PATTERNS) or len(s) <= 4

    def _before_user_input(self, user_input: str):
        """收到用户输入时：写入记忆、把记忆上下文注入到 user message。"""
        small_talk = self._is_small_talk(user_input)

        memory_ctx_parts = []
        if not small_talk and self.state.memory:
            ctx = self.state.memory.load_context()
            if ctx:
                memory_ctx_parts.append(ctx)

        # 1.5) 用向量库做一次预召回，把最相似的 3 条历史也带上（闲聊时跳过）
        if not small_talk and self.state.vector_memory and self.state.vector_memory.available:
            try:
                hits = self.state.vector_memory.search(user_input, k=3)
                if hits:
                    parts = ["## 语义相关的历史记忆（向量召回）"]
                    for h in hits:
                        role = h["metadata"].get("role", "?")
                        score = h["score"]
                        parts.append(f"- [score={score}] {role}: {h['text'][:200]}")
                    memory_ctx_parts.append("\n".join(parts))
            except Exception as e:
                logger.debug("向量预召回失败: %s", e)

        memory_ctx = "\n\n".join(memory_ctx_parts)
        self._replace_context_message(memory_ctx)
        if small_talk:
            content = f"{user_input}\n\n（这是一次问候 / 闲聊 / 元提问，请直接友好回复，不要调用任何工具。）"
        else:
            content = user_input

        self.messages.append({"role": "user", "content": content})

        # 2) 写入文本记忆
        if self.state.memory:
            self.state.memory.append_user(user_input)
        # 3) 写入向量记忆
        if self.state.vector_memory and self.state.vector_memory.available:
            self.state.vector_memory.add(
                user_input, metadata={"role": "user", "user_id": self.user_id}
            )

    def _call_tool_with_progress(self, name: str, args: dict
                                   ) -> Generator[dict, None, str]:
        """在后台线程跑工具，主线程边 yield 进度边等结果。

        进度事件：
            {"type": "tool_progress", "name": ..., "progress": {...}}
            {"type": "subagent", "role": ..., ...}
        最后返回工具的最终字符串结果（通过 generator return value 不太友好，
        所以我们用一个魔法事件 {"type": "_done", "result": ...} 表示结束）。
        """
        q: queue.Queue = queue.Queue()

        def on_progress(p: dict):
            # 子 Agent 进度做语义升级，方便 UI 区分
            phase = (p or {}).get("phase", "")
            if phase.startswith("subagent_") or "role" in (p or {}):
                q.put({"type": "subagent", "name": name, **p})
            else:
                q.put({"type": "tool_progress", "name": name, "progress": p})

        result_holder = {"result": None, "error": None}

        def worker():
            try:
                r = self.registry.call(name, args, self.state, on_progress=on_progress)
                result_holder["result"] = r
            except Exception as e:
                result_holder["error"] = e
            finally:
                q.put({"type": "_done"})

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        while True:
            try:
                ev = q.get(timeout=0.5)
            except queue.Empty:
                if not t.is_alive():
                    break
                continue
            if ev.get("type") == "_done":
                break
            yield ev

        t.join(timeout=2.0)
        if result_holder["error"]:
            yield {
                "type": "tool_result",
                "name": name,
                "result": json.dumps({"error": str(result_holder["error"])}, ensure_ascii=False),
            }
            return
        # 通过最后一条事件返回结果
        yield {"type": "_result", "result": result_holder["result"] or ""}

    @staticmethod
    def _detect_tool_error(result_text: str) -> Optional[str]:
        """识别工具是否失败。返回错误信息（用于注入修正提示），否则返回 None。"""
        if not result_text:
            return None
        # 主路径：JSON 含 "error"
        try:
            obj = json.loads(result_text)
            if isinstance(obj, dict):
                if "error" in obj and obj["error"]:
                    return str(obj["error"])
                # 工具返回 {"msg": "..."} 通常是成功
        except Exception:
            pass
        # 备用路径：以 {"error": 开头（即使后续被截断）
        head = result_text.strip()[:80]
        if head.startswith('{"error"'):
            return head
        return None

    def _execute_tool_calls(self, tool_calls: list,
                              failure_count: dict
                              ) -> Generator[dict, None, tuple]:
        """执行一批 tool_calls，发布事件。

        返回 (round_failures, round_exhausted) - 通过最后一条 _summary 事件传递。
        """
        round_failures: list[dict] = []
        round_exhausted: list[str] = []

        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}

            yield {"type": "tool", "name": name, "args": args}

            if self.state.memory:
                self.state.memory.append_tool_call(name, args)

            tool_result_text = ""
            for ev in self._call_tool_with_progress(name, args):
                if ev.get("type") == "_result":
                    tool_result_text = ev["result"]
                elif ev.get("type") == "tool_result":
                    tool_result_text = ev["result"]
                    yield ev
                else:
                    yield ev

            err = self._detect_tool_error(tool_result_text)
            if err:
                failure_count[name] = failure_count.get(name, 0) + 1
                if failure_count[name] > self.max_tool_retries:
                    round_exhausted.append(name)
                else:
                    round_failures.append({
                        "name": name,
                        "args": args,
                        "error": err,
                        "attempt": failure_count[name],
                    })
                yield {
                    "type": "tool_error",
                    "name": name,
                    "args": args,
                    "error": err,
                    "attempt": failure_count[name],
                    "max_retries": self.max_tool_retries,
                    "exhausted": name in round_exhausted,
                }

            if self.state.memory:
                self.state.memory.append_tool_result(name, tool_result_text)

            yield {"type": "tool_result", "name": name, "result": tool_result_text}
            self.messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "name": name,
                "content": tool_result_text,
            })

        # 用魔法事件返回总结
        yield {"type": "_round_summary",
                "failures": round_failures,
                "exhausted": round_exhausted}

    def _run_loop_stream(self) -> Generator[dict, None, None]:
        """流式主循环。yield 事件字典。"""
        tools_schema = self.registry.schemas()
        any_tool_used_in_round = False  # 给 ReAct 反思用
        # 单次 chat 内的工具失败计数：{tool_name: 失败次数}
        tool_failure_count: dict[str, int] = {}

        for iteration in range(self.max_iterations):
            text_buf: list[str] = []
            tool_calls = None

            try:
                for ev in self.llm.chat_with_tools_stream(
                    messages=self.messages,
                    tools=tools_schema,
                    temperature=self.config.llm_config.temperature,
                ):
                    if ev["type"] == "text":
                        text_buf.append(ev["delta"])
                        yield {"type": "text", "delta": ev["delta"], "iteration": iteration}
                    elif ev["type"] == "tool_calls":
                        tool_calls = ev["tool_calls"]
                    elif ev["type"] == "done":
                        pass
            except Exception as e:
                logger.exception("LLM 流式调用失败")
                yield {"type": "error", "message": f"LLM 调用出错: {e}"}
                return

            full_text = "".join(text_buf)

            # 把 assistant 消息加入历史
            assistant_msg = {"role": "assistant", "content": full_text}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            self.messages.append(assistant_msg)

            # 没有工具调用 → 终止
            if not tool_calls:
                yield {"type": "final", "content": full_text}
                return

            any_tool_used_in_round = True

            # 执行所有工具调用（带进度透传 + 失败检测）
            round_failures: list[dict] = []
            round_exhausted: list[str] = []
            for ev in self._execute_tool_calls(tool_calls, tool_failure_count):
                if ev.get("type") == "_round_summary":
                    round_failures = ev["failures"]
                    round_exhausted = ev["exhausted"]
                else:
                    yield ev

            # 决定下一阶段的 prompt：失败 → 修正模式；成功 → 常规反思
            should_reflect = (
                self.enable_react
                and iteration < self.max_iterations - 1
                and any_tool_used_in_round
            )
            if should_reflect:
                if round_failures and not round_exhausted:
                    # 触发修正：给 LLM 看明确的错误并要求调整参数重试
                    fail_lines = []
                    for f in round_failures:
                        fail_lines.append(
                            f"- 工具 `{f['name']}` 第 {f['attempt']}/{self.max_tool_retries} 次失败：{f['error']} "
                            f"（参数：{json.dumps(f['args'], ensure_ascii=False)[:200]}）"
                        )
                    prompt = (
                        "以下工具调用失败，请仔细分析错误原因，**调整参数后重新调用同一个工具**，"
                        "或换用更合适的工具。**不要在没有修复前直接给最终答复**。\n\n"
                        + "\n".join(fail_lines)
                    )
                elif round_exhausted:
                    # 重试用尽：让 LLM 做兜底
                    prompt = (
                        "以下工具达到最大重试次数仍失败：" + ", ".join(round_exhausted) +
                        "。请改用其他工具/方法，或直接告知用户哪些信息缺失、需要他们补充。"
                    )
                else:
                    prompt = (
                        "请基于刚才的工具结果，先做 1-2 句反思（结果是否合理、是否需要修正参数/再调用其他工具），"
                        "然后给出明确的下一步动作。如果已经够了，请直接给出最终答复。"
                    )
                self.messages.append({"role": "user", "content": prompt})
                reflection_buf: list[str] = []
                next_tool_calls = None
                try:
                    for ev in self.llm.chat_with_tools_stream(
                        messages=self.messages,
                        tools=tools_schema,
                        temperature=self.config.llm_config.temperature,
                    ):
                        if ev["type"] == "text":
                            reflection_buf.append(ev["delta"])
                            yield {"type": "reflection", "delta": ev["delta"]}
                        elif ev["type"] == "tool_calls":
                            next_tool_calls = ev["tool_calls"]
                except Exception as e:
                    logger.exception("ReAct 反思失败")
                    yield {"type": "error", "message": f"反思阶段出错: {e}"}
                    return

                full_reflection = "".join(reflection_buf)
                # 反思消息也加入历史
                reflect_msg = {"role": "assistant", "content": full_reflection}
                if next_tool_calls:
                    reflect_msg["tool_calls"] = next_tool_calls
                self.messages.append(reflect_msg)

                if not next_tool_calls:
                    # 反思后直接终止：把反思作为最终答复
                    yield {"type": "final", "content": full_reflection}
                    return

                # 反思中产生了新的工具调用 → 走同一个 helper（享受失败检测与重试计数）
                for ev in self._execute_tool_calls(next_tool_calls, tool_failure_count):
                    if ev.get("type") == "_round_summary":
                        # 反思中的失败下一轮主循环再处理
                        pass
                    else:
                        yield ev

        yield {
            "type": "final",
            "content": "（已达到最大工具调用轮数，建议拆分任务后再次询问）",
        }
