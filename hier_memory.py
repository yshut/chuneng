"""分层长期记忆 (HierarchicalMemory)

参考 MemGPT / Letta 的思路，把记忆分四层并自动管理：

1. WORKING（近期完整消息）
   - 最近 N 轮 user / assistant / tool_call / tool_result
   - 一字不差，用于注入 prompt
   - 文件：working.jsonl  （JSONL，便于流式读写）

2. SUMMARIES（自动摘要）
   - working 溢出时，最旧 M 条交给 LLM 压成 1 段 ≤120 字摘要
   - 摘要持久化在 summaries.jsonl 里
   - 加载上下文时按时间倒序取最近 K 段

3. FACTS（事实 KV）
   - LLM 从对话里抽取的"长期事实"：工厂参数、用户偏好、决策结果等
   - 同 key 后写覆盖前写，保持最新
   - 文件：facts.json  （key -> {value, updated_at, source}）

4. TOOL_LOG（工具调用日志）
   - 仅记录 tool_call(name, args) + 结果摘要（前 400 字）
   - JSONL，自动按字节轮转
   - 文件：tools.jsonl

LLM 上下文注入顺序（从抽象到具体）：
    [事实档案] → [历史摘要(最近K段)] → [近期完整对话] → [当前用户输入]

依赖：
    无（vector_memory 仍然由 agent_core 单独管理，与本模块解耦）
    LLM：可选，如果没有 llm，会用启发式方法做最简摘要
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def safe_user_id(user_id: str) -> str:
    """规范化 user_id，移除路径不安全字符。"""
    if not user_id:
        return "main"
    s = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff-]", "_", str(user_id).strip())
    return s or "main"


@dataclass
class HierMemoryConfig:
    base_dir: str = "output/memory"
    user_id: str = "main"

    # WORKING 层窗口
    working_window_size: int = 24       # 保留最近 24 条完整消息
    summarize_chunk_size: int = 12      # 每次压缩最旧 12 条

    # SUMMARIES 层
    max_summaries_in_context: int = 6   # 注入 prompt 时最多放最近 6 段摘要
    summary_max_chars: int = 200        # 单段摘要长度上限

    # FACTS 层
    max_facts: int = 50                  # 最多保留 50 条事实

    # TOOL_LOG
    tool_log_max_bytes: int = 2 * 1024 * 1024
    tool_result_truncate: int = 400

    # 上下文注入字符上限
    max_context_chars: int = 5000

    # LLM 客户端（可选）。设置后才会启用 LLM 摘要 + 事实抽取
    llm: Any = None
    llm_temperature: float = 0.3
    llm_max_tokens: int = 800


# JSONL 工具
def _jsonl_append(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _jsonl_read_all(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def _jsonl_write_all(path: Path, items: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


class HierarchicalMemory:
    """分层记忆主类。线程安全（内部加锁）。"""

    def __init__(self, config: HierMemoryConfig = None):
        self.config = config or HierMemoryConfig()
        self.user_id = safe_user_id(self.config.user_id)
        self.base_dir = Path(self.config.base_dir) / self.user_id
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self.working_file = self.base_dir / "working.jsonl"
        self.summary_file = self.base_dir / "summaries.jsonl"
        self.facts_file = self.base_dir / "facts.json"
        self.tool_log_file = self.base_dir / "tools.jsonl"

        self._lock = threading.RLock()

        # 加载到内存（只 working / facts 常驻；summary/tool_log 文件读取）
        self._working: list[dict] = _jsonl_read_all(self.working_file)
        self._facts: dict[str, dict] = self._load_facts()

    # ==================================================================
    # 写入
    # ==================================================================
    def append_user(self, text: str):
        if not text or not text.strip():
            return
        self._add_msg({"role": "user", "content": text.strip()})

    def append_assistant(self, text: str):
        if not text:
            return
        text = re.sub(r"<think[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()
        if not text:
            return
        self._add_msg({"role": "assistant", "content": text})

    def append_tool_call(self, name: str, args: dict):
        try:
            args_str = json.dumps(args, ensure_ascii=False)
        except Exception:
            args_str = str(args)
        # working 也记录工具调用，便于反查
        self._add_msg({"role": "tool_call", "name": name, "args": args_str},
                       trigger_compress=False)
        # tool_log
        with self._lock:
            _jsonl_append(self.tool_log_file, {
                "ts": self._now(),
                "type": "call",
                "name": name,
                "args": args_str,
            })
            self._rotate_tool_log()

    def append_tool_result(self, name: str, result: str):
        if not result:
            return
        snippet = result if len(result) <= self.config.tool_result_truncate \
            else result[:self.config.tool_result_truncate] + "...(已截断)"

        self._add_msg({"role": "tool_result", "name": name, "content": snippet},
                       trigger_compress=False)
        with self._lock:
            _jsonl_append(self.tool_log_file, {
                "ts": self._now(),
                "type": "result",
                "name": name,
                "snippet": snippet,
            })
            self._rotate_tool_log()

    def _add_msg(self, msg: dict, trigger_compress: bool = True):
        msg = dict(msg)
        msg.setdefault("ts", self._now())
        with self._lock:
            self._working.append(msg)
            _jsonl_append(self.working_file, msg)
            if trigger_compress and self._needs_compress():
                # 同步压缩（小开销，~1s LLM）；失败时降级到启发式
                try:
                    self._compress_oldest()
                except Exception as e:
                    logger.warning("记忆压缩失败: %s", e)

    # ==================================================================
    # 压缩 / 抽取（核心）
    # ==================================================================
    def _needs_compress(self) -> bool:
        return len(self._working) > self.config.working_window_size

    def _compress_oldest(self):
        """把最旧 N 条 working 压成一段摘要 + 抽取事实。"""
        n = self.config.summarize_chunk_size
        if len(self._working) <= n:
            return
        chunk = self._working[:n]
        rest = self._working[n:]

        summary, new_facts = self._summarize_with_llm(chunk)
        if not summary:
            summary = self._heuristic_summary(chunk)

        # 写摘要
        summary_obj = {
            "ts": self._now(),
            "summary": summary[:self.config.summary_max_chars],
            "covers_n_msgs": len(chunk),
            "first_ts": chunk[0].get("ts"),
            "last_ts": chunk[-1].get("ts"),
        }
        _jsonl_append(self.summary_file, summary_obj)

        # 合并事实
        if new_facts:
            for k, v in new_facts.items():
                k = str(k).strip()
                v = str(v).strip()
                if not k or not v:
                    continue
                self._facts[k] = {
                    "value": v,
                    "updated_at": self._now(),
                    "source": "auto_extract",
                }
            self._enforce_fact_limit()
            self._save_facts()

        # 重写 working（drop 最旧的 N 条）
        self._working = rest
        _jsonl_write_all(self.working_file, self._working)

    def _summarize_with_llm(self, chunk: list[dict]) -> tuple[str, dict]:
        """用 LLM 把 chunk 压成一段摘要 + 抽事实 KV。"""
        llm = self.config.llm
        if llm is None or not getattr(llm, "available", False):
            return "", {}

        lines = []
        for m in chunk:
            role = m.get("role", "?")
            if role == "tool_call":
                lines.append(f"[tool_call] {m.get('name')}({m.get('args','')[:200]})")
            elif role == "tool_result":
                lines.append(f"[tool_result] {m.get('name')}: {m.get('content','')[:300]}")
            else:
                lines.append(f"[{role}] {m.get('content','')[:500]}")
        convo = "\n".join(lines)

        prompt = (
            "请把下面这段对话片段压缩为 JSON：\n"
            "1) summary: ≤120 字的中文摘要，保留关键决策/数据/工具调用结论\n"
            "2) facts: 抽取持久化的事实键值对 (中文 key，例如 '月用电量','投资模式','电池成本','峰谷价差' 等)，"
            "只放确定且长期有效的，不要把临时疑问当作事实\n"
            "返回严格 JSON：{\"summary\":\"...\",\"facts\":{\"k\":\"v\",...}}\n\n"
            "===对话片段===\n" + convo + "\n=============="
        )

        try:
            resp = llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=self.config.llm_temperature,
                max_tokens=self.config.llm_max_tokens,
            )
            text = (resp or "").strip()
            # 尝试抽 ```json ... ``` 或裸 JSON
            m = re.search(r"\{[\s\S]*\}", text)
            if not m:
                return "", {}
            data = json.loads(m.group(0))
            summary = str(data.get("summary", "")).strip()
            facts = data.get("facts") or {}
            if not isinstance(facts, dict):
                facts = {}
            return summary, facts
        except Exception as e:
            logger.warning("LLM 摘要失败: %s", e)
            return "", {}

    @staticmethod
    def _heuristic_summary(chunk: list[dict]) -> str:
        """LLM 不可用时的兜底：取最早 user 输入 + 最末 assistant 输出片段拼成摘要。"""
        first_user = next((m for m in chunk if m.get("role") == "user"), None)
        last_assist = next((m for m in reversed(chunk) if m.get("role") == "assistant"), None)
        tools = [m.get("name") for m in chunk if m.get("role") == "tool_call"]
        parts = []
        if first_user:
            parts.append(f"用户问：{first_user.get('content','')[:60]}")
        if tools:
            uniq = list(dict.fromkeys(tools))[:5]
            parts.append("用了：" + " / ".join(uniq))
        if last_assist:
            parts.append(f"助手答：{last_assist.get('content','')[:60]}")
        return "；".join(parts) or "（无内容）"

    # ==================================================================
    # 上下文加载
    # ==================================================================
    def load_context(self, max_chars: int = None) -> str:
        """构造给 LLM 的记忆上下文。"""
        max_chars = max_chars or self.config.max_context_chars
        parts: list[str] = []

        # 1) facts
        if self._facts:
            sorted_facts = sorted(
                self._facts.items(),
                key=lambda kv: kv[1].get("updated_at", ""),
                reverse=True,
            )
            parts.append("## 用户档案（关键事实）")
            for k, v in sorted_facts:
                parts.append(f"- **{k}**: {v.get('value','')}")

        # 2) summaries (最近 K 段)
        summaries = _jsonl_read_all(self.summary_file)
        if summaries:
            recent = summaries[-self.config.max_summaries_in_context:]
            parts.append("\n## 历史会话摘要（最近 {} 段）".format(len(recent)))
            for i, s in enumerate(recent, 1):
                parts.append(f"[{i}] {s.get('summary','')}")

        # 3) working
        if self._working:
            parts.append("\n## 近期对话（完整）")
            for m in self._working:
                role = m.get("role", "?")
                if role == "tool_call":
                    parts.append(f"- [tool_call] {m.get('name')}({m.get('args','')[:120]})")
                elif role == "tool_result":
                    parts.append(f"- [tool_result] {m.get('name')}: {m.get('content','')[:150]}")
                else:
                    parts.append(f"- [{role}] {m.get('content','')[:300]}")

        ctx = "\n".join(parts)
        if len(ctx) > max_chars:
            # 从前面截，因为后面是更新的
            ctx = "...(更早内容已省略)\n" + ctx[-max_chars:]
        return ctx

    # ==================================================================
    # 检索
    # ==================================================================
    def search_keywords(self, keywords: list[str], limit: int = 5) -> list[dict]:
        """跨 working + summaries + facts 关键词搜索。"""
        if not keywords:
            return []
        keyword_bases = [re.sub(r"\d+$", "", k).lower() for k in keywords if k]
        keyword_bases = [k for k in keyword_bases if k]
        if not keyword_bases:
            return []

        candidates: list[dict] = []
        # working
        for m in self._working:
            text = m.get("content") or m.get("args") or ""
            candidates.append({
                "source": "working",
                "role": m.get("role"),
                "text": text,
                "ts": m.get("ts", ""),
            })
        # summaries
        for s in _jsonl_read_all(self.summary_file):
            candidates.append({
                "source": "summary",
                "role": "summary",
                "text": s.get("summary", ""),
                "ts": s.get("ts", ""),
            })
        # facts
        for k, v in self._facts.items():
            candidates.append({
                "source": "fact",
                "role": "fact",
                "text": f"{k}: {v.get('value','')}",
                "ts": v.get("updated_at", ""),
            })

        results = []
        for c in candidates:
            txt_low = c["text"].lower()
            score = 0
            hit = 0
            matched = []
            for kb in keyword_bases:
                if kb in txt_low:
                    hit += 1
                    matched.append(kb)
                    score += 1 + len(re.findall(re.escape(kb), txt_low)) * 0.3
            if hit == len(keyword_bases):
                results.append({**c, "score": round(score, 2), "matched": matched})

        results.sort(key=lambda r: (-r["score"], r["ts"]), reverse=False)
        results.sort(key=lambda r: -r["score"])
        return results[:limit]

    # ==================================================================
    # FACTS API（手动管理）
    # ==================================================================
    def set_fact(self, key: str, value: str, source: str = "manual"):
        with self._lock:
            self._facts[str(key).strip()] = {
                "value": str(value).strip(),
                "updated_at": self._now(),
                "source": source,
            }
            self._enforce_fact_limit()
            self._save_facts()

    def get_fact(self, key: str) -> Optional[str]:
        v = self._facts.get(str(key).strip())
        return v.get("value") if v else None

    def remove_fact(self, key: str) -> bool:
        with self._lock:
            if key in self._facts:
                del self._facts[key]
                self._save_facts()
                return True
        return False

    def list_facts(self) -> list[dict]:
        return [
            {"key": k, "value": v.get("value"),
             "updated_at": v.get("updated_at"),
             "source": v.get("source")}
            for k, v in sorted(self._facts.items(),
                                 key=lambda kv: kv[1].get("updated_at", ""),
                                 reverse=True)
        ]

    def _enforce_fact_limit(self):
        if len(self._facts) <= self.config.max_facts:
            return
        # 按 updated_at 倒序保留最新 N 条
        sorted_items = sorted(
            self._facts.items(),
            key=lambda kv: kv[1].get("updated_at", ""),
            reverse=True,
        )[:self.config.max_facts]
        self._facts = dict(sorted_items)

    def _load_facts(self) -> dict:
        if not self.facts_file.exists():
            return {}
        try:
            return json.loads(self.facts_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_facts(self):
        try:
            self.facts_file.parent.mkdir(parents=True, exist_ok=True)
            self.facts_file.write_text(
                json.dumps(self._facts, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("保存 facts.json 失败: %s", e)

    # ==================================================================
    # 维护
    # ==================================================================
    def _rotate_tool_log(self):
        try:
            if not self.tool_log_file.exists():
                return
            size = self.tool_log_file.stat().st_size
            if size <= self.config.tool_log_max_bytes:
                return
            data = self.tool_log_file.read_bytes()
            keep_from = int(len(data) * 0.5)
            cut = keep_from
            while cut < len(data) and data[cut:cut + 1] != b"\n":
                cut += 1
            cut += 1
            self.tool_log_file.write_bytes(data[cut:])
            logger.info("[tool_log 轮转] %s: %.1fMB → 保留后 50%%",
                          self.tool_log_file.name, size / 1024 / 1024)
        except Exception as e:
            logger.warning("轮转 tool log 失败: %s", e)

    def stats(self) -> dict:
        n_summaries = sum(1 for _ in _jsonl_read_all(self.summary_file))
        size_tool = self.tool_log_file.stat().st_size if self.tool_log_file.exists() else 0
        size_working = self.working_file.stat().st_size if self.working_file.exists() else 0
        return {
            "user_id": self.user_id,
            "base_dir": str(self.base_dir),
            "working": {
                "messages": len(self._working),
                "size_kb": round(size_working / 1024, 1),
                "window_size": self.config.working_window_size,
            },
            "summaries": {
                "count": n_summaries,
                "max_in_context": self.config.max_summaries_in_context,
            },
            "facts": {
                "count": len(self._facts),
                "max": self.config.max_facts,
            },
            "tool_log": {
                "size_kb": round(size_tool / 1024, 1),
                "max_mb": round(self.config.tool_log_max_bytes / 1024 / 1024, 1),
            },
        }

    def clear(self):
        with self._lock:
            for f in (self.working_file, self.summary_file,
                       self.facts_file, self.tool_log_file):
                try:
                    if f.exists():
                        f.unlink()
                except Exception as e:
                    logger.warning("删除 %s 失败: %s", f, e)
            self._working = []
            self._facts = {}

    def force_compress(self):
        """手动触发一次压缩（即使 working 没溢出）。"""
        with self._lock:
            if len(self._working) >= 2:
                # 尽量压一半
                n = max(self.config.summarize_chunk_size,
                        len(self._working) // 2)
                old_chunk = self.config.summarize_chunk_size
                self.config.summarize_chunk_size = min(n, len(self._working))
                try:
                    self._compress_oldest()
                finally:
                    self.config.summarize_chunk_size = old_chunk

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def list_users(base_dir: str | Path = "output/memory") -> list[str]:
        path = Path(base_dir)
        if not path.exists():
            return []
        users = []
        for sub in sorted(path.iterdir()):
            if sub.is_dir() and not sub.name.startswith("."):
                # 新格式或老格式都算
                markers = ["working.jsonl", "facts.json", "summaries.jsonl",
                            "记忆.txt", "工具总结.txt", "内容.txt"]
                if any((sub / f).exists() for f in markers):
                    users.append(sub.name)
                elif (sub / "vector_db").exists():
                    users.append(sub.name)
        return users
