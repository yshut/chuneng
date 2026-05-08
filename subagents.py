"""多 Agent 协作

把"做完整一份储能分析"拆成 3 个角色：
- DataAgent  ：负责读文件、提取用电、做用电画像（只看到数据相关工具）
- ConfigAgent：负责储能优化、敏感性分析、A/B 实验（看到优化/对比工具）
- WriterAgent：负责生成报告、风险评估、总结（看到报告/导出工具）

主 Agent（Coordinator）通过 3 个 delegate 工具调用它们。
每个子 Agent 共享同一份 AgentState，因此中间结果（df / optimal_config / 报告）
天然在 3 个角色之间流通，不必重新加载。

设计要点：
- SubAgent 是一个轻量级 LLM tool-calling 循环（与 StorageAgent 类似，但工具白名单受限）
- 子 Agent 默认非流式，结果一次性返回；进度通过 on_progress 透传给主 Agent
- 不允许子 Agent 再 delegate 给其他子 Agent（避免环）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from agent_tools import AgentState, ToolRegistry

logger = logging.getLogger("subagents")


# 每个角色可见的工具白名单
DATA_AGENT_TOOLS = {
    "parse_files",
    "parse_natural_language",
    "use_demo_data",
    "get_data_summary",
    "get_current_state",
    "list_input_files",
    "search_knowledge_base",  # 可查电价政策
}

CONFIG_AGENT_TOOLS = {
    "optimize_storage",
    "analyze_revenue",
    "analyze_investor_customer",
    "compare_scenarios",
    "ab_experiment",
    "set_storage_params",
    "set_rate_config",
    "set_investment_mode",
    "get_current_state",
    "search_knowledge_base",
}

WRITER_AGENT_TOOLS = {
    "generate_report",
    "analyze_revenue",
    "analyze_investor_customer",
    "export_excel",
    "get_current_state",
    "get_data_summary",
    "search_knowledge_base",
    "recall_memory",
    "semantic_search_memory",
}


SYSTEM_PROMPTS = {
    "data": (
        "你是【数据 Agent】，负责把用户给的原始信息（文件路径或自然语言描述）"
        "可靠地变成结构化电费数据。你必须：\n"
        "1) 先用 list_input_files / parse_files / parse_natural_language / use_demo_data 之一加载数据；\n"
        "2) 用 get_data_summary 验证数据；\n"
        "3) 简短总结数据特征（峰谷分布、容量等级、需量），不要做储能配置或写报告。\n"
        "如果数据看起来异常，直接报告异常并停止，不要硬上。"
    ),
    "config": (
        "你是【配置 Agent】，专精储能容量与功率配置。前提是数据 Agent 已经加载好电费数据。\n"
        "你需要：\n"
        "1) 先 get_current_state 确认数据已就绪；\n"
        "2) 调用 optimize_storage 给出最优配置；\n"
        "3) 调用 analyze_revenue 输出收益分析；\n"
        "4) 如果用户提到'对比/A vs B/不同方案'，调用 ab_experiment 或 compare_scenarios；\n"
        "5) 用 1 段话总结你最终选择的配置和理由。不要再做报告写作。"
    ),
    "writer": (
        "你是【报告 Agent】，专门把前面 Agent 的分析整合成结构化输出。\n"
        "你需要：\n"
        "1) 先 get_current_state 确认 optimal_config 与 revenue_report 都存在；\n"
        "2) 必要时用 search_knowledge_base 查行业政策/电价文件作引用；\n"
        "3) 调用 generate_report 生成 Markdown 报告；\n"
        "4) 可选 export_excel 导出表格；\n"
        "5) 不要重新做配置优化。"
    ),
}


@dataclass
class SubAgent:
    role: str                                      # data / config / writer
    state: AgentState
    registry: ToolRegistry
    llm: Any                                       # LLMClient
    allowed_tools: set                             # 工具白名单
    system_prompt: str
    max_iter: int = 6
    on_progress: Optional[Callable[[dict], None]] = None  # 由主 Agent 注入

    def _filtered_schemas(self) -> list[dict]:
        return [t.to_openai_schema() for t in self.registry.all()
                if t.name in self.allowed_tools]

    def run(self, task: str) -> dict:
        """同步执行子 Agent，返回结构化结果。"""
        if self.on_progress:
            self.on_progress({"phase": "subagent_start", "role": self.role, "task": task})

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task},
        ]
        tools_schemas = self._filtered_schemas()
        used_tools: list[dict] = []

        for it in range(self.max_iter):
            try:
                resp = self.llm.chat_with_tools(messages, tools_schemas, tool_choice="auto")
            except Exception as e:
                logger.exception("子 Agent %s 调用 LLM 失败", self.role)
                return {"error": f"LLM 调用失败: {e}", "role": self.role}

            # resp 是 OpenAI SDK 的 ChatCompletionMessage 对象
            content = getattr(resp, "content", "") or ""
            tool_calls = getattr(resp, "tool_calls", None) or []

            if not tool_calls:
                if self.on_progress:
                    self.on_progress({"phase": "subagent_finish", "role": self.role, "iter": it + 1})
                return {
                    "role": self.role,
                    "task": task,
                    "result": content,
                    "tools_used": used_tools,
                    "iterations": it + 1,
                }

            # 把 assistant 的工具调用挂回去
            assistant_msg = {"role": "assistant", "content": content, "tool_calls": []}
            for tc in tool_calls:
                fn = getattr(tc, "function", None)
                fn_name = getattr(fn, "name", "") if fn else ""
                fn_args = getattr(fn, "arguments", "{}") if fn else "{}"
                assistant_msg["tool_calls"].append({
                    "id": getattr(tc, "id", None),
                    "type": "function",
                    "function": {"name": fn_name, "arguments": fn_args},
                })
            messages.append(assistant_msg)

            for tc in tool_calls:
                fn = getattr(tc, "function", None)
                name = getattr(fn, "name", "") if fn else ""
                try:
                    args = json.loads(getattr(fn, "arguments", None) or "{}")
                except Exception:
                    args = {}
                if name not in self.allowed_tools:
                    result = json.dumps({"error": f"该工具在 {self.role}-Agent 内不可用: {name}"},
                                         ensure_ascii=False)
                else:
                    if self.on_progress:
                        self.on_progress({
                            "phase": "subagent_tool",
                            "role": self.role,
                            "tool": name,
                            "args": args,
                        })
                    result = self.registry.call(name, args, self.state, on_progress=self.on_progress)
                used_tools.append({"name": name, "args": args})
                messages.append({
                    "role": "tool",
                    "tool_call_id": getattr(tc, "id", None),
                    "name": name,
                    "content": result,
                })

        return {
            "role": self.role,
            "task": task,
            "result": "（达到最大轮数，未给出最终答复）",
            "tools_used": used_tools,
            "iterations": self.max_iter,
        }


def make_subagent(role: str, state: AgentState, registry: ToolRegistry,
                   llm: Any, on_progress=None) -> SubAgent:
    if role == "data":
        return SubAgent(role="data", state=state, registry=registry, llm=llm,
                         allowed_tools=DATA_AGENT_TOOLS,
                         system_prompt=SYSTEM_PROMPTS["data"],
                         on_progress=on_progress)
    if role == "config":
        return SubAgent(role="config", state=state, registry=registry, llm=llm,
                         allowed_tools=CONFIG_AGENT_TOOLS,
                         system_prompt=SYSTEM_PROMPTS["config"],
                         on_progress=on_progress)
    if role == "writer":
        return SubAgent(role="writer", state=state, registry=registry, llm=llm,
                         allowed_tools=WRITER_AGENT_TOOLS,
                         system_prompt=SYSTEM_PROMPTS["writer"],
                         on_progress=on_progress)
    raise ValueError(f"未知子 Agent 角色: {role}")
