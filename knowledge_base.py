"""离线知识库 / RAG 检索

复用 vector_memory.py 中的 Embedder（Qwen 优先，sentence-transformers 兜底），
但与"用户对话记忆"完全分离：
- 数据：政策文件、电价文件、行业白皮书等
- 存储：output/knowledge_base/<collection>/  下的 ChromaDB
- 默认共享（所有用户可读），不做用户隔离

主要能力：
- 把任意 .txt / .md / .pdf / .docx / .xlsx 切块入库
- 支持按 source 删除/列出
- 检索时返回带来源的 chunk 列表，便于 LLM 引用
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("knowledge_base")


@dataclass
class KnowledgeBaseConfig:
    base_dir: str = "output/knowledge_base"
    collection_name: str = "kb_default"
    chunk_size: int = 600          # 每块约 600 字符（中文 ≈ 400 字 + 200 重叠）
    chunk_overlap: int = 100
    min_chunk_chars: int = 30
    embedder: Any = None           # 必须传 vector_memory.Embedder 实例
    reranker: Any = None           # 可选：reranker.Reranker 实例（如 BGEReranker）
    rerank_candidate_k: int = 20   # 一阶向量召回的候选数（rerank 之前）


def _split_text(text: str, chunk_size: int, overlap: int, min_chunk: int) -> list[str]:
    """简单按字符切块，尊重段落/句号边界。"""
    if not text:
        return []
    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        # 尝试在段落或句号处切
        if end < n:
            for sep in ["\n\n", "。\n", "\n", "。", "；", ";", ".", " "]:
                idx = text.rfind(sep, start + min_chunk, end)
                if idx > start + min_chunk:
                    end = idx + len(sep)
                    break
        chunk = text[start:end].strip()
        if len(chunk) >= min_chunk:
            chunks.append(chunk)
        if end <= start:
            end = start + chunk_size
        start = max(end - overlap, start + 1)
    return chunks


def _read_file(path: Path) -> str:
    """读取多种格式的文件为纯文本。"""
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".markdown", ".csv", ".log"}:
        for enc in ("utf-8", "gbk", "utf-16"):
            try:
                return path.read_text(encoding=enc)
            except UnicodeDecodeError:
                continue
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        try:
            import pdfplumber
        except ImportError:
            logger.warning("pdfplumber 未安装，无法解析 PDF: %s", path.name)
            return ""
        text = []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                if t.strip():
                    text.append(t)
        return "\n\n".join(text)
    if suffix in {".docx", ".doc"}:
        try:
            from docx import Document
        except ImportError:
            logger.warning("python-docx 未安装，无法解析 DOCX: %s", path.name)
            return ""
        try:
            doc = Document(str(path))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception as e:
            logger.warning("解析 DOCX 失败 %s: %s", path.name, e)
            return ""
    if suffix in {".xlsx", ".xls"}:
        try:
            import pandas as pd
        except ImportError:
            return ""
        try:
            sheets = pd.read_excel(str(path), sheet_name=None)
            parts = []
            for name, df in sheets.items():
                parts.append(f"# Sheet: {name}\n" + df.to_csv(index=False))
            return "\n\n".join(parts)
        except Exception as e:
            logger.warning("解析 XLSX 失败 %s: %s", path.name, e)
            return ""
    # 兜底
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


class KnowledgeBase:
    """RAG 知识库：分块 → 向量化 → 持久化 → 检索（可选重排）。"""

    def __init__(self, config: Optional[KnowledgeBaseConfig] = None):
        self.config = config or KnowledgeBaseConfig()
        self.embedder = self.config.embedder
        self.reranker = self.config.reranker
        self._client = None
        self._collection = None
        self._init_chroma()

    def _init_chroma(self):
        try:
            import chromadb
            from chromadb.config import Settings
        except ImportError:
            logger.error("knowledge_base 需要 chromadb，请 pip install chromadb")
            raise

        base = Path(self.config.base_dir)
        base.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=str(base),
            settings=Settings(anonymized_telemetry=False, allow_reset=True),
        )
        self._collection = self._client.get_or_create_collection(
            name=self.config.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    @property
    def is_ready(self) -> bool:
        return self.embedder is not None and self._collection is not None

    # ---------------- 写入 ----------------
    def index_text(self, text: str, source: str, metadata: Optional[dict] = None,
                    on_progress=None) -> int:
        """直接给一段文本入库，返回新加 chunk 数。"""
        if not self.is_ready:
            return 0
        chunks = _split_text(text, self.config.chunk_size,
                             self.config.chunk_overlap, self.config.min_chunk_chars)
        if not chunks:
            return 0

        # 删除该 source 已存在的旧 chunk（重新索引）
        self.remove_document(source, silent=True)

        ids = [f"{source}#{i}" for i in range(len(chunks))]
        metas = [{**(metadata or {}), "source": source, "chunk_idx": i, "total": len(chunks)}
                 for i in range(len(chunks))]

        # 批量 embed（小批量，避免 API 单次过大）
        batch = 16
        embeddings: list[list[float]] = []
        for i in range(0, len(chunks), batch):
            sub = chunks[i:i + batch]
            embeddings.extend(self.embedder.encode(sub))
            if on_progress:
                on_progress({
                    "phase": "embedding",
                    "step": min(i + batch, len(chunks)),
                    "total": len(chunks),
                    "source": source,
                })

        self._collection.add(ids=ids, documents=chunks, metadatas=metas, embeddings=embeddings)
        if on_progress:
            on_progress({"phase": "done", "total": len(chunks), "source": source})
        return len(chunks)

    def index_file(self, file_path: str, metadata: Optional[dict] = None,
                    on_progress=None) -> int:
        """读取文件 → 切块 → 入库。"""
        p = Path(file_path)
        if not p.exists():
            raise FileNotFoundError(file_path)
        text = _read_file(p)
        if not text.strip():
            return 0
        meta = {"file_size": p.stat().st_size, "file_type": p.suffix.lower(), **(metadata or {})}
        return self.index_text(text, source=p.name, metadata=meta, on_progress=on_progress)

    # ---------------- 读取 / 检索 ----------------
    def search(self, query: str, k: int = 5,
                source_filter: Optional[str] = None,
                rerank: Optional[bool] = None,
                candidate_k: Optional[int] = None) -> list[dict]:
        """两阶段检索：向量召回 → (可选) 重排。

        Args:
            k: 最终返回的 top-K
            source_filter: 仅在指定 source 检索
            rerank: 是否启用 reranker。None=自动（reranker 可用就启用）
            candidate_k: 一阶召回数，rerank 时通常远大于 k（默认 20）
        """
        if not self.is_ready or not query.strip():
            return []

        if rerank is None:
            rerank = self.reranker is not None
        if rerank and self.reranker is None:
            logger.info("rerank 启用但未注入 reranker，已自动跳过")
            rerank = False

        # 一阶召回数
        if rerank:
            recall_k = candidate_k or self.config.rerank_candidate_k
        else:
            recall_k = max(1, k)

        q_emb = self.embedder.encode([query])[0]
        where = {"source": source_filter} if source_filter else None
        try:
            res = self._collection.query(
                query_embeddings=[q_emb],
                n_results=max(1, recall_k),
                where=where,
            )
        except Exception as e:
            logger.warning("KB 检索失败: %s", e)
            return []

        candidates: list[dict] = []
        ids = res.get("ids", [[]])[0]
        docs = res.get("documents", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        dists = res.get("distances", [[]])[0]
        for i in range(len(ids)):
            candidates.append({
                "id": ids[i],
                "content": docs[i],
                "metadata": metas[i] if i < len(metas) else {},
                "vec_score": 1 - dists[i] if i < len(dists) else None,
                "source": (metas[i] if i < len(metas) else {}).get("source", "?"),
            })

        if not rerank or not candidates:
            for c in candidates:
                c["score"] = c.pop("vec_score", None)
            return candidates[:k]

        # 二阶 rerank
        try:
            ranked = self.reranker.rerank(query, [c["content"] for c in candidates], top_k=k)
        except Exception as e:
            logger.warning("rerank 失败，回退向量分: %s", e)
            for c in candidates:
                c["score"] = c.pop("vec_score", None)
            return candidates[:k]

        out: list[dict] = []
        for orig_idx, rerank_score in ranked:
            if 0 <= orig_idx < len(candidates):
                c = candidates[orig_idx]
                c["rerank_score"] = float(rerank_score)
                c["score"] = float(rerank_score)
                # 保留向量分以便观察
                c["vec_score"] = c.get("vec_score")
                out.append(c)
        return out

    def list_documents(self) -> list[dict]:
        """列出所有已入库的源文件汇总。"""
        if not self.is_ready:
            return []
        try:
            data = self._collection.get(include=["metadatas"])
        except Exception:
            return []
        agg: dict[str, dict] = {}
        for m in (data.get("metadatas") or []):
            src = (m or {}).get("source", "?")
            if src not in agg:
                agg[src] = {"source": src, "chunks": 0, "size": (m or {}).get("file_size")}
            agg[src]["chunks"] += 1
        return sorted(agg.values(), key=lambda x: x["source"])

    def remove_document(self, source: str, silent: bool = False) -> int:
        if not self.is_ready:
            return 0
        try:
            existing = self._collection.get(where={"source": source}, include=[])
            ids = existing.get("ids", []) or []
            if ids:
                self._collection.delete(ids=ids)
            return len(ids)
        except Exception as e:
            if not silent:
                logger.warning("删除 source=%s 失败: %s", source, e)
            return 0

    def count(self) -> int:
        if not self.is_ready:
            return 0
        try:
            return self._collection.count()
        except Exception:
            return 0

    def stats(self) -> dict:
        return {
            "ready": self.is_ready,
            "chunks": self.count(),
            "documents": len(self.list_documents()),
            "base_dir": self.config.base_dir,
            "collection": self.config.collection_name,
            "reranker": getattr(self.reranker, "name", None) if self.reranker else None,
        }

    def clear(self):
        if not self.is_ready:
            return
        try:
            self._client.delete_collection(self.config.collection_name)
            self._collection = self._client.get_or_create_collection(
                name=self.config.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as e:
            logger.warning("清空知识库失败: %s", e)
