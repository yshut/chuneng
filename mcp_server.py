"""储能配置AGENT - MCP (Model Context Protocol) 服务器

把本项目所有 Agent 工具暴露为 MCP 协议，让 Claude Desktop / Cursor / 其他 MCP
客户端可以直接调用。

启动：
    python main.py --mcp
    python main.py --mcp --user alice            # 指定用户
    # 或直接：
    python mcp_server.py

依赖：
    pip install mcp  # 官方 MCP Python SDK

在 Claude Desktop / Cursor 中接入（mcp_servers 配置示例）：
    {
      "mcpServers": {
        "storage-agent": {
          "command": "python",
          "args": ["c:/Users/yshut/Desktop/文档/储能AGENT/mcp_server.py"],
          "env": {
            "DASHSCOPE_API_KEY": "sk-xxx"
          }
        }
      }
    }
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional

# 项目根加入 sys.path（直接 python mcp_server.py 启动时也能 import 模块）
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

logger = logging.getLogger("storage-agent-mcp")


def _check_mcp() -> bool:
    try:
        import mcp  # noqa: F401
        return True
    except ImportError:
        print(
            "[错误] 未安装 mcp 包。请运行:\n  pip install mcp\n",
            file=sys.stderr,
        )
        return False


async def run_server(user_id: str = "main", config=None):
    """运行 MCP stdio server。"""
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool as McpTool, TextContent

    from agent_core import StorageAgent
    from config import AgentConfig
    from hier_memory import safe_user_id

    user_id = safe_user_id(user_id)

    # 创建 Agent（会自动初始化所有工具、记忆、向量库）
    agent_config = config or AgentConfig()
    agent = StorageAgent(agent_config, user_id=user_id, verbose=False)

    server = Server(
        name="storage-agent",
        version="1.0.0",
    )

    # ---------------- 列出所有工具 ----------------
    @server.list_tools()
    async def handle_list_tools() -> list[McpTool]:
        tools = []
        for t in agent.registry.all():
            tools.append(McpTool(
                name=t.name,
                description=t.description,
                inputSchema=t.parameters or {"type": "object", "properties": {}},
            ))
        logger.info("MCP list_tools 返回 %d 个工具", len(tools))
        return tools

    # ---------------- 调用工具 ----------------
    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
        logger.info("MCP call_tool: %s args=%s", name, arguments)

        # 同步工具，扔到线程池避免阻塞 stdio 事件循环
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: agent.registry.call(name, arguments or {}, agent.state),
        )

        # MCP 返回 TextContent
        return [TextContent(type="text", text=result)]

    # ---------------- Resources（可选）：暴露记忆和状态 ----------------
    try:
        from mcp.types import Resource

        @server.list_resources()
        async def handle_list_resources() -> list[Resource]:
            resources = []
            mem_dir = Path(agent_config.output_dir) / "memory" / user_id
            # 新分层记忆文件
            for fname, label, mime in (
                ("working.jsonl", "working 区（近期完整对话）", "application/jsonl"),
                ("summaries.jsonl", "summaries（自动摘要）", "application/jsonl"),
                ("facts.json", "facts（事实档案）", "application/json"),
                ("tools.jsonl", "工具调用日志", "application/jsonl"),
            ):
                fpath = mem_dir / fname
                if fpath.exists():
                    resources.append(Resource(
                        uri=f"file://{fpath.as_posix()}",
                        name=f"{user_id}/{fname}",
                        description=f"{user_id} 的{label}",
                        mimeType=mime,
                    ))
            return resources

        @server.read_resource()
        async def handle_read_resource(uri: str) -> str:
            uri_str = str(uri)
            if uri_str.startswith("file://"):
                path = Path(uri_str[7:])
                if path.exists() and path.is_file():
                    return path.read_text(encoding="utf-8", errors="replace")
            return ""
    except Exception as e:
        logger.debug("Resource 注册失败（旧版 mcp 可能不支持）: %s", e)

    # ---------------- 启动 stdio 服务 ----------------
    logger.info(
        "MCP server 启动: user_id=%s, tools=%d, vector_memory=%s",
        user_id,
        len(agent.registry.all()),
        bool(agent.state.vector_memory and agent.state.vector_memory.available),
    )

    async with stdio_server() as (read, write):
        from mcp.server import NotificationOptions
        from mcp.server.models import InitializationOptions
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="storage-agent",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def main():
    import argparse
    parser = argparse.ArgumentParser(description="储能配置AGENT - MCP 服务器")
    parser.add_argument("--user", default="main", help="user_id（多用户隔离）")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    # 注意：MCP 用 stdio 通信，所有日志必须打到 stderr
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if not _check_mcp():
        sys.exit(1)

    try:
        asyncio.run(run_server(user_id=args.user))
    except KeyboardInterrupt:
        logger.info("MCP server 已停止")
    except Exception as e:
        logger.exception("MCP server 异常退出: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
