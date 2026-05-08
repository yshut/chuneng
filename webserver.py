"""FastAPI Web 服务 - 替代 Gradio UI，无锁并发体验。

特点：
- 流式聊天用 SSE（Server-Sent Events）；前端用 EventSource 接收。
- 长任务（如对话）放在线程池里跑，主事件循环始终空闲，
  思考期间点任意按钮/上传/切用户都能立即响应。
- 上传支持 multipart 多文件 + 文件夹（前端用 webkitdirectory）。
- 单页静态 UI（static/index.html）+ 异步 fetch API。
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent_core import StorageAgent
from config import AgentConfig
from hier_memory import HierarchicalMemory, safe_user_id

logger = logging.getLogger(__name__)

INPUT_DIR = Path("input")
INPUT_DIR.mkdir(exist_ok=True)
STATIC_DIR = Path(__file__).parent / "static"

SUPPORTED_UPLOAD_EXTS = {".pdf", ".docx", ".doc", ".xlsx", ".xls",
                         ".png", ".jpg", ".jpeg", ".csv", ".txt"}
SUPPORTED_KB_EXTS = {".txt", ".md", ".markdown", ".pdf", ".docx", ".xlsx", ".csv"}


# ======================================================================
# AgentManager（多用户隔离）
# ======================================================================
class AgentManager:
    def __init__(self, config: AgentConfig, **kwargs):
        self.config = config
        self.kwargs = kwargs
        self.agents: dict[str, StorageAgent] = {}
        self._lock = threading.Lock()

    def get(self, user_id: str) -> StorageAgent:
        uid = safe_user_id(user_id)
        with self._lock:
            if uid not in self.agents:
                logger.info("创建新 Agent: user_id=%s", uid)
                self.agents[uid] = StorageAgent(
                    config=self.config, user_id=uid, **self.kwargs
                )
            return self.agents[uid]

    def list_users(self) -> list[str]:
        try:
            users = HierarchicalMemory.list_users()
        except Exception:
            users = []
        # 合并已加载的 user_id
        with self._lock:
            for uid in self.agents.keys():
                if uid not in users:
                    users.append(uid)
        if not users:
            users = ["main"]
        return sorted(set(users))


# ======================================================================
# 工具函数
# ======================================================================
def _state_summary(agent: StorageAgent) -> dict:
    s = agent.state
    has_data = s.electricity_df is not None and not s.electricity_df.empty
    return {
        "user_id": agent.user_id,
        "has_data": has_data,
        "rows": int(len(s.electricity_df)) if has_data else 0,
        "has_optimization": s.optimal_config is not None,
        "has_revenue": s.revenue_report is not None,
        "has_investor": s.investor_report is not None,
        "has_md_report": s.md_report is not None,
        "input_files": sorted([p.name for p in INPUT_DIR.glob("*") if p.is_file()]),
        "tools_count": len(agent.registry.all()),
        "react": agent.enable_react,
    }


def _memory_summary(agent: StorageAgent) -> dict:
    if agent.state.memory is None:
        return {"enabled": False}
    try:
        return {
            "enabled": True,
            "stats": agent.state.memory.stats(),
            "facts": agent.state.memory.list_facts(),
        }
    except Exception as e:
        return {"enabled": True, "error": str(e)}


def _kb_summary(agent: StorageAgent) -> dict:
    kb = agent.state.kb
    if kb is None or not getattr(kb, "is_ready", False):
        return {"enabled": False}
    try:
        stats = kb.stats() if hasattr(kb, "stats") else {}
        docs = kb.list_documents() if hasattr(kb, "list_documents") else []
        return {"enabled": True, "stats": stats, "documents": docs}
    except Exception as e:
        return {"enabled": True, "error": str(e)}


def _save_upload(file: UploadFile, allowed_exts: set[str] | None) -> tuple[Path | None, str]:
    """保存上传文件到 INPUT_DIR；同名自动加序号。返回 (Path, msg)。"""
    raw_name = Path(file.filename or "").name
    if not raw_name:
        return None, "文件名为空"
    suffix = Path(raw_name).suffix.lower()
    if allowed_exts is not None and suffix and suffix not in allowed_exts:
        return None, f"不支持的扩展名: {suffix}"
    dst = INPUT_DIR / raw_name
    if dst.exists():
        stem, suf = dst.stem, dst.suffix
        i = 1
        while dst.exists():
            dst = INPUT_DIR / f"{stem}_{i}{suf}"
            i += 1
    try:
        with open(dst, "wb") as fp:
            shutil.copyfileobj(file.file, fp)
    except Exception as e:
        return None, f"写入失败: {e}"
    return dst, "ok"


# ======================================================================
# FastAPI App
# ======================================================================
def create_app(manager: AgentManager) -> FastAPI:
    app = FastAPI(title="储能 AGENT", version="2.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # --------------------------------------------------------------
    # 主页
    # --------------------------------------------------------------
    @app.get("/")
    async def index():
        idx = STATIC_DIR / "index.html"
        if idx.exists():
            return FileResponse(str(idx))
        return JSONResponse({"error": "static/index.html not found"}, status_code=500)

    # --------------------------------------------------------------
    # 用户 / 状态 / 记忆 / 知识库
    # --------------------------------------------------------------
    @app.get("/api/users")
    async def get_users():
        return {"users": manager.list_users()}

    @app.post("/api/users")
    async def create_user(req: dict):
        uid = (req.get("user_id") or "").strip()
        if not uid:
            raise HTTPException(400, "user_id required")
        manager.get(uid)
        return {"ok": True, "user_id": safe_user_id(uid), "users": manager.list_users()}

    @app.get("/api/state")
    async def get_state(user_id: str = "main"):
        agent = await asyncio.to_thread(manager.get, user_id)
        return _state_summary(agent)

    @app.get("/api/memory")
    async def get_memory(user_id: str = "main"):
        agent = await asyncio.to_thread(manager.get, user_id)
        return _memory_summary(agent)

    @app.get("/api/kb")
    async def get_kb(user_id: str = "main"):
        agent = await asyncio.to_thread(manager.get, user_id)
        return _kb_summary(agent)

    @app.get("/api/history")
    async def get_history(user_id: str = "main", limit: int = 100):
        """返回当前会话的 user/assistant 消息历史，用于浏览器刷新/切用户后恢复显示。

        - 跳过 system prompt 与 tool 消息（这些不直接展示给用户）
        - 同时附带每个 assistant 消息已知的工具调用名（仅展示用，不还原工具结果详情）
        """
        agent = await asyncio.to_thread(manager.get, user_id)
        msgs = list(getattr(agent, "messages", []) or [])
        out: list[dict] = []
        # 跳过首条 system
        for m in msgs:
            if not isinstance(m, dict):
                # 可能是 OpenAI SDK 对象，转 dict
                try:
                    m = m.model_dump()  # pydantic v2
                except Exception:
                    try:
                        m = m.dict()
                    except Exception:
                        m = {"role": getattr(m, "role", "?"),
                             "content": getattr(m, "content", "") or ""}
            role = m.get("role")
            if role == "system":
                continue
            if role == "tool":
                continue
            content = m.get("content") or ""
            tool_calls = m.get("tool_calls") or []
            tool_names = []
            try:
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        tool_names.append((tc.get("function") or {}).get("name") or tc.get("name"))
                    else:
                        fn = getattr(tc, "function", None)
                        tool_names.append(getattr(fn, "name", None) if fn else None)
            except Exception:
                pass
            tool_names = [t for t in tool_names if t]
            # 不展示空 assistant + 无 tool_calls 的占位项
            if role == "assistant" and not content and not tool_names:
                continue
            out.append({
                "role": role,
                "content": content,
                "tool_calls": tool_names,
            })
        if limit and len(out) > limit:
            out = out[-limit:]
        return {"messages": out, "total": len(out)}

    # --------------------------------------------------------------
    # 聊天（SSE 流式）
    # --------------------------------------------------------------
    @app.post("/api/chat")
    async def chat(req: dict):
        user_id = req.get("user_id", "main")
        message = (req.get("message") or "").strip()
        if not message:
            raise HTTPException(400, "empty message")

        agent = await asyncio.to_thread(manager.get, user_id)
        loop = asyncio.get_event_loop()

        async def event_stream() -> AsyncGenerator[str, None]:
            queue: asyncio.Queue = asyncio.Queue()
            SENTINEL = object()

            def producer():
                try:
                    for ev in agent.chat_stream(message):
                        # 过滤内部魔法事件
                        et = ev.get("type") if isinstance(ev, dict) else None
                        if et and et.startswith("_"):
                            continue
                        loop.call_soon_threadsafe(queue.put_nowait, ev)
                except Exception as e:
                    logger.exception("chat_stream 异常")
                    loop.call_soon_threadsafe(
                        queue.put_nowait, {"type": "error", "message": str(e)}
                    )
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, SENTINEL)

            t = threading.Thread(target=producer, daemon=True)
            t.start()

            try:
                while True:
                    ev = await queue.get()
                    if ev is SENTINEL:
                        break
                    payload = json.dumps(ev, ensure_ascii=False, default=str)
                    yield f"data: {payload}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
            except asyncio.CancelledError:
                logger.info("SSE 客户端断开")
                raise

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # 禁用 nginx buffering
            },
        )

    @app.post("/api/clear")
    async def clear_chat(req: dict):
        user_id = req.get("user_id", "main")
        agent = await asyncio.to_thread(manager.get, user_id)
        # 仅清当前会话上下文（保留长期记忆）
        try:
            agent.messages = agent.messages[:1]  # 保留 system prompt
        except Exception:
            pass
        return {"ok": True}

    @app.post("/api/reset")
    async def reset_all(req: dict):
        user_id = req.get("user_id", "main")
        agent = await asyncio.to_thread(manager.get, user_id)
        await asyncio.to_thread(agent.reset_all)
        return {"ok": True, "state": _state_summary(agent)}

    # --------------------------------------------------------------
    # 文件上传（input/）—— 多文件 / 文件夹
    # --------------------------------------------------------------
    @app.post("/api/upload")
    async def upload(
        files: list[UploadFile] = File(...),
        user_id: str = Form("main"),
        from_folder: str = Form("0"),
    ):
        from_folder_b = from_folder in ("1", "true", "True")
        copied: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []
        # 文件夹模式：按白名单过滤；单文件模式：宽松（如果用户选了非白名单也允许）
        allowed = SUPPORTED_UPLOAD_EXTS if from_folder_b else None
        for f in files:
            dst, msg = _save_upload(f, allowed)
            if dst is None:
                if msg.startswith("不支持的扩展名"):
                    skipped.append(Path(f.filename or "").name)
                else:
                    errors.append(f"{f.filename}: {msg}")
                continue
            copied.append(dst.name)
        return {
            "ok": True,
            "copied": copied,
            "skipped": skipped,
            "errors": errors,
            "total": len(copied),
        }

    @app.delete("/api/input/{name}")
    async def delete_input_file(name: str):
        # 只允许删除 INPUT_DIR 下的文件
        target = (INPUT_DIR / name).resolve()
        if INPUT_DIR.resolve() not in target.parents and target != INPUT_DIR.resolve() / name:
            raise HTTPException(400, "非法路径")
        if not target.exists() or not target.is_file():
            raise HTTPException(404, "文件不存在")
        try:
            target.unlink()
            return {"ok": True}
        except Exception as e:
            raise HTTPException(500, str(e))

    # --------------------------------------------------------------
    # 知识库
    # --------------------------------------------------------------
    @app.post("/api/kb/index")
    async def kb_index(
        files: list[UploadFile] = File(...),
        user_id: str = Form("main"),
        from_folder: str = Form("0"),
    ):
        from_folder_b = from_folder in ("1", "true", "True")
        agent = await asyncio.to_thread(manager.get, user_id)
        kb = agent.state.kb
        if kb is None or not getattr(kb, "is_ready", False):
            raise HTTPException(400, "知识库未启用")

        results: list[dict] = []
        for f in files:
            name = Path(f.filename or "").name
            ext = Path(name).suffix.lower()
            if from_folder_b and ext not in SUPPORTED_KB_EXTS:
                results.append({"name": name, "ok": False, "msg": "扩展名不支持，已跳过"})
                continue
            # 写到临时目录再 index
            try:
                with tempfile.NamedTemporaryFile(
                    delete=False, suffix=ext or ".bin"
                ) as tmp:
                    shutil.copyfileobj(f.file, tmp)
                    tmp_path = Path(tmp.name)
            except Exception as e:
                results.append({"name": name, "ok": False, "msg": f"写入失败: {e}"})
                continue
            try:
                # 改名为原名以便 KB 内 source 字段是真实文件名
                final_path = tmp_path.parent / name
                try:
                    if final_path.exists():
                        final_path.unlink()
                    tmp_path.rename(final_path)
                except Exception:
                    final_path = tmp_path  # 回退
                n = await asyncio.to_thread(kb.index_file, str(final_path))
                results.append({"name": name, "ok": True, "chunks": int(n)})
            except Exception as e:
                results.append({"name": name, "ok": False, "msg": str(e)})
            finally:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    pass
                try:
                    if 'final_path' in locals() and final_path.exists() and final_path != tmp_path:
                        final_path.unlink()
                except Exception:
                    pass
        return {"results": results, "kb": _kb_summary(agent)}

    @app.post("/api/kb/search")
    async def kb_search(req: dict):
        user_id = req.get("user_id", "main")
        query = (req.get("query") or "").strip()
        k = int(req.get("k") or 5)
        if not query:
            raise HTTPException(400, "empty query")
        agent = await asyncio.to_thread(manager.get, user_id)
        kb = agent.state.kb
        if kb is None or not getattr(kb, "is_ready", False):
            raise HTTPException(400, "KB 未启用")
        try:
            hits = await asyncio.to_thread(kb.search, query, k)
        except TypeError:
            hits = await asyncio.to_thread(lambda: kb.search(query, k=k))
        # hits 元素可能是 dict 或自定义类
        norm = []
        for h in hits or []:
            if isinstance(h, dict):
                norm.append(h)
            else:
                norm.append({
                    "score": getattr(h, "score", None),
                    "text": getattr(h, "text", None) or getattr(h, "content", None),
                    "source": getattr(h, "source", None),
                })
        return {"hits": norm}

    @app.delete("/api/kb/{source}")
    async def kb_remove(source: str, user_id: str = "main"):
        agent = await asyncio.to_thread(manager.get, user_id)
        kb = agent.state.kb
        if kb is None or not getattr(kb, "is_ready", False):
            raise HTTPException(400, "KB 未启用")
        try:
            await asyncio.to_thread(kb.remove_document, source)
            return {"ok": True, "kb": _kb_summary(agent)}
        except Exception as e:
            raise HTTPException(500, str(e))

    return app


# ======================================================================
# 启动入口（被 main.py 调用）
# ======================================================================
def launch(config: AgentConfig, host: str = "127.0.0.1", port: int = 7860,
           default_user: str = "main", **kwargs):
    """启动 FastAPI Web UI。"""
    try:
        import uvicorn
    except ImportError:
        raise ImportError(
            "FastAPI Web UI 依赖 uvicorn，未安装。请运行：\n"
            "  pip install fastapi uvicorn[standard] python-multipart"
        )

    manager = AgentManager(config=config, **kwargs)
    # 预热默认用户
    try:
        manager.get(default_user)
    except Exception as e:
        logger.warning("预热默认用户失败: %s", e)

    app = create_app(manager)

    print()
    print("=" * 60)
    print("🚀 储能 AGENT Web 启动中（FastAPI / 异步无锁）")
    print(f"   访问: http://{host}:{port}")
    print(f"   默认用户: {default_user}")
    print("   按 Ctrl+C 停止")
    print("=" * 60)
    print()

    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)
