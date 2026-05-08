"""储能配置AGENT - 长期记忆模块（小端风格）

参考 xiaoduan 项目的记忆机制：纯文本文件 + 字符窗口截断 + 关键词检索。
不依赖 embedding / vector store，但完全够用，可读性极高，方便调试。

文件结构（默认 output/memory/）：
  - 记忆.txt          永久不裁剪，存对话历史
                       [时间戳] 用户: ...
                       [时间戳] 助手: ...
  - 工具总结.txt       1MB 上限，超出保留后 50%
                       [时间戳] 工具名(参数JSON)
  - 内容.txt          10MB 上限，超出保留后 50%
                       [时间戳] 工具名 => 结果摘要

加载到 prompt：取每个文件末尾 N 字符，拼成结构化 context 注入消息。
检索：跨 记忆.txt 关键词匹配 + 评分，返回 top-K 条。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 配置（与小端 local-gateway.js 对齐）
# ----------------------------------------------------------------------
MAX_NEIRONG_BYTES = 10 * 1024 * 1024       # 内容.txt 10MB
MAX_TOOL_SUMMARY_BYTES = 1 * 1024 * 1024   # 工具总结.txt 1MB
KEEP_RATIO = 0.5                            # 超限后保留后 50%

# 加载到 prompt 的字符上限（普通模式 / 复杂模式）
MAX_MEMORY_CHARS = 3000
MAX_NEIRONG_CONTENT_CHARS = 2000
MAX_TOOL_SUMMARY_CHARS = 2000
MAX_MEMORY_CHARS_COMPLEX = 40000
MAX_NEIRONG_CONTENT_CHARS_COMPLEX = 20000
MAX_TOOL_SUMMARY_CHARS_COMPLEX = 5000


@dataclass
class MemoryConfig:
    """记忆配置。"""
    base_dir: Path = Path("output/memory")
    user_id: str = "main"           # 多用户隔离：每个 user 一个子目录
    max_memory_chars: int = MAX_MEMORY_CHARS
    max_content_chars: int = MAX_NEIRONG_CONTENT_CHARS
    max_tool_summary_chars: int = MAX_TOOL_SUMMARY_CHARS
    max_neirong_bytes: int = MAX_NEIRONG_BYTES
    max_tool_summary_bytes: int = MAX_TOOL_SUMMARY_BYTES
    complex_mode: bool = False  # 复杂任务模式：上下文窗口拉大


def safe_user_id(user_id: str) -> str:
    """规范化 user_id，移除路径不安全字符。"""
    if not user_id:
        return "main"
    s = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff-]", "_", str(user_id).strip())
    return s or "main"


class FileMemory:
    """基于文件的长期记忆，xiaoduan 同款机制，支持多用户隔离。

    每个 user_id 一个子目录：
        output/memory/main/记忆.txt
        output/memory/alice/记忆.txt
        output/memory/bob/记忆.txt
    """

    def __init__(self, config: MemoryConfig = None):
        self.config = config or MemoryConfig()
        self.user_id = safe_user_id(self.config.user_id)
        # base_dir/user_id/...
        self.base_dir = Path(self.config.base_dir) / self.user_id
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self.memory_file = self.base_dir / "记忆.txt"
        self.tool_summary_file = self.base_dir / "工具总结.txt"
        self.content_file = self.base_dir / "内容.txt"

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def append_user(self, text: str):
        """记录用户消息到 记忆.txt。"""
        if not text:
            return
        entry = f"[{self._now()}] 用户: {text.strip()}\n"
        self._append(self.memory_file, entry)

    def append_assistant(self, text: str):
        """记录助手最终回复到 记忆.txt。"""
        if not text:
            return
        # 去除 <think>...</think>
        text = re.sub(r"<think[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()
        if not text:
            return
        entry = f"[{self._now()}] 助手: {text}\n"
        self._append(self.memory_file, entry)

    def append_tool_call(self, name: str, args: dict):
        """记录工具调用到 工具总结.txt。"""
        try:
            args_str = json.dumps(args, ensure_ascii=False)
        except Exception:
            args_str = str(args)
        entry = f"[{self._now()}] {name}({args_str})\n"
        self._append(self.tool_summary_file, entry)
        self._rotate(self.tool_summary_file, self.config.max_tool_summary_bytes)

    def append_tool_result(self, name: str, result: str):
        """记录工具结果到 内容.txt。"""
        if not result:
            return
        # 截断单条结果，避免一次性把文件撑爆
        if len(result) > 4000:
            result = result[:4000] + "...(已截断)"
        entry = f"[{self._now()}] {name} => {result}\n"
        self._append(self.content_file, entry)
        self._rotate(self.content_file, self.config.max_neirong_bytes)

    # ------------------------------------------------------------------
    # 读取/加载
    # ------------------------------------------------------------------
    def load_context(self, complex_mode: bool = None) -> str:
        """加载三个文件的尾部内容，拼成 prompt 上下文。"""
        if complex_mode is None:
            complex_mode = self.config.complex_mode

        if complex_mode:
            max_mem = MAX_MEMORY_CHARS_COMPLEX
            max_content = MAX_NEIRONG_CONTENT_CHARS_COMPLEX
            max_tool = MAX_TOOL_SUMMARY_CHARS_COMPLEX
        else:
            max_mem = self.config.max_memory_chars
            max_content = self.config.max_content_chars
            max_tool = self.config.max_tool_summary_chars

        parts = []

        mem = self._read_tail(self.memory_file, max_mem)
        if mem:
            parts.append(f"## 对话记忆\n{mem}")

        content = self._read_tail(self.content_file, max_content)
        if content:
            parts.append(f"## 工具获取的内容记录\n{content}")

        tool_summary = self._read_tail(self.tool_summary_file, max_tool)
        if tool_summary:
            parts.append(f"## 工具调用记录\n{tool_summary}")

        return "\n\n".join(parts)

    def _read_tail(self, path: Path, max_chars: int) -> str:
        """读取文件，最多保留末尾 max_chars 个字符。"""
        try:
            if not path.exists():
                return ""
            content = path.read_text(encoding="utf-8", errors="replace").strip()
            if not content:
                return ""
            if len(content) > max_chars:
                content = content[-max_chars:]
                # 从第一个换行符之后开始（避免半行）
                idx = content.find("\n")
                if 0 < idx < 200:
                    content = content[idx + 1:]
            return content
        except Exception as e:
            logger.warning("读取 %s 失败: %s", path, e)
            return ""

    # ------------------------------------------------------------------
    # 关键词检索（小端 keywordSearch 同款）
    # ------------------------------------------------------------------
    def search_keywords(self, keywords: list[str], limit: int = 3) -> list[dict]:
        """跨 记忆.txt 关键词搜索，返回 top-K 匹配条目。

        匹配规则：
        - 必须同时包含全部 keywords（去除尾部数字后的基础词）
        - 评分：每个关键词命中 +1，每次出现 +0.5
        - 按评分降序，再按时间倒序
        """
        if not keywords:
            return []
        if not self.memory_file.exists():
            return []

        # 处理关键词（小端规则：去除尾部数字）
        keyword_bases = [re.sub(r"\d+$", "", k) for k in keywords if k]
        keyword_bases = [k for k in keyword_bases if k]
        if not keyword_bases:
            return []

        try:
            content = self.memory_file.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning("读取记忆文件失败: %s", e)
            return []

        # 去掉 think 块
        content = re.sub(r"<think[\s\S]*?</think>", "", content, flags=re.IGNORECASE)

        # 按消息头切分：[时间] 用户:/助手:
        msg_re = re.compile(r"^\[.*?\]\s*(用户|助手):\s*", re.MULTILINE)
        positions = [(m.end(), m.group(1)) for m in msg_re.finditer(content)]
        if not positions:
            return []

        entries = []
        for i, (start, role) in enumerate(positions):
            end = positions[i + 1][0] if i + 1 < len(positions) else len(content)
            # 截掉下一条的头
            text = content[start:end]
            # 找 "\n[" 截断
            cut = text.rfind("\n[")
            if cut > 0 and i + 1 < len(positions):
                text = text[:cut]
            entries.append({"role": role, "text": text.strip(), "index": i})

        # 评分
        results = []
        for e in entries:
            score = 0
            hit_count = 0
            related = []
            for kb in keyword_bases:
                if kb in e["text"]:
                    score += 1
                    hit_count += 1
                    related.append(kb)
                    score += len(re.findall(re.escape(kb), e["text"])) * 0.5
            if hit_count == len(keyword_bases):
                results.append({
                    "text": e["text"][:500],
                    "role": e["role"],
                    "index": e["index"],
                    "score": round(score, 2),
                    "matched": related,
                })

        # 按评分排序，再按时间倒序
        results.sort(key=lambda r: (-r["score"], -r["index"]))
        return results[:limit]

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        """返回三个文件的状态。"""
        info = {"user_id": self.user_id, "base_dir": str(self.base_dir)}
        for label, path in [
            ("memory", self.memory_file),
            ("tool_summary", self.tool_summary_file),
            ("content", self.content_file),
        ]:
            if path.exists():
                size = path.stat().st_size
                lines = sum(1 for _ in path.open("r", encoding="utf-8", errors="replace"))
                info[label] = {
                    "path": str(path),
                    "size_bytes": size,
                    "size_kb": round(size / 1024, 1),
                    "lines": lines,
                }
            else:
                info[label] = {"path": str(path), "size_bytes": 0, "lines": 0}
        return info

    @staticmethod
    def list_users(base_dir: str | Path = "output/memory") -> list[str]:
        """列出所有已存在记忆的 user_id。"""
        path = Path(base_dir)
        if not path.exists():
            return []
        users = []
        for sub in sorted(path.iterdir()):
            if sub.is_dir() and not sub.name.startswith("."):
                # 至少有一个记忆文件就算
                if any((sub / f).exists() for f in ("记忆.txt", "工具总结.txt", "内容.txt")):
                    users.append(sub.name)
                else:
                    # 也包括只有 vector_db 的
                    if (sub / "vector_db").exists():
                        users.append(sub.name)
        return users

    def clear(self):
        """清空全部记忆文件。"""
        for f in (self.memory_file, self.tool_summary_file, self.content_file):
            try:
                if f.exists():
                    f.unlink()
            except Exception as e:
                logger.warning("删除 %s 失败: %s", f, e)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _append(path: Path, text: str):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            logger.warning("写入 %s 失败: %s", path, e)

    @staticmethod
    def _rotate(path: Path, max_bytes: int):
        """文件超限时，保留后 KEEP_RATIO 部分（按字节切，避免半字符）。"""
        try:
            if not path.exists():
                return
            size = path.stat().st_size
            if size <= max_bytes:
                return
            data = path.read_bytes()
            keep_from = int(len(data) * (1 - KEEP_RATIO))
            # 找下一个换行符开始
            cut = keep_from
            while cut < len(data) and data[cut:cut + 1] != b"\n":
                cut += 1
            cut += 1
            path.write_bytes(data[cut:])
            logger.info("[轮转] %s: %.1fMB -> 保留后%d%%",
                        path.name, size / 1024 / 1024, int(KEEP_RATIO * 100))
        except Exception as e:
            logger.warning("轮转 %s 失败: %s", path, e)
