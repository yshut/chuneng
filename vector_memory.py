"""储能配置AGENT - 向量记忆模块
基于 ChromaDB 持久化 + 嵌入模型，实现海量历史的语义检索。

与 FileMemory 互补：
- FileMemory：人类可读的"流水账"，按时间窗口注入 prompt
- VectorMemory：海量历史的语义检索，按相似度返回最相关 K 条

embedding 优先级（自动降级）：
1. Qwen 文本嵌入（DashScope text-embedding-v3，1024 维）—— 无额外依赖
2. sentence-transformers 本地模型 —— 离线可用
3. None —— 禁用，回退到关键词检索

依赖：
    pip install chromadb           # 必需
    pip install sentence-transformers  # 可选（离线 embedding）
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 嵌入器抽象
# ----------------------------------------------------------------------
class Embedder:
    """统一嵌入接口。"""

    name: str = "base"
    dimension: int = 0

    def encode(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class QwenEmbedder(Embedder):
    """通义千问 / DashScope 文本嵌入（OpenAI 兼容 API）。"""

    name = "qwen-text-embedding-v3"
    dimension = 1024

    def __init__(self, api_key: str = None, base_url: str = None,
                 model: str = "text-embedding-v3"):
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("openai SDK 未安装")

        api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        if not api_key:
            raise RuntimeError("缺少 DASHSCOPE_API_KEY 环境变量")
        base_url = base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # DashScope 单次调用建议 ≤ 25 条
        results = []
        for i in range(0, len(texts), 25):
            chunk = texts[i:i + 25]
            for attempt in range(3):
                try:
                    resp = self._client.embeddings.create(
                        model=self._model,
                        input=chunk,
                        encoding_format="float",
                    )
                    results.extend([d.embedding for d in resp.data])
                    break
                except Exception as e:
                    if attempt == 2:
                        raise
                    logger.warning("Qwen embedding 重试 %d/3: %s", attempt + 1, e)
                    time.sleep(1.0 * (attempt + 1))
        return results


class SentenceTransformersEmbedder(Embedder):
    """sentence-transformers 本地嵌入（离线）。"""

    def __init__(self, model_name: str = "shibing624/text2vec-base-chinese"):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise ImportError("sentence-transformers 未安装: pip install sentence-transformers")
        self._model = SentenceTransformer(model_name)
        self.name = f"st-{model_name}"
        self.dimension = self._model.get_sentence_embedding_dimension()

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        embeddings = self._model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        return [e.tolist() for e in embeddings]


def make_embedder(prefer: str = "qwen", llm_config: Any = None) -> Optional[Embedder]:
    """按优先级创建嵌入器。失败返回 None。

    Args:
        prefer: "qwen" 优先 Qwen API；"local" 优先本地 sentence-transformers
        llm_config: 可选的 LLMConfig，会从中读取 api_key / base_url 配置 QwenEmbedder
    """
    api_key = None
    base_url = None
    if llm_config is not None:
        api_key = getattr(llm_config, "api_key", None) or None
        base_url = getattr(llm_config, "base_url", None) or None

    def _qwen():
        return QwenEmbedder(api_key=api_key, base_url=base_url)

    candidates = []
    if prefer == "local":
        candidates = [
            ("local", lambda: SentenceTransformersEmbedder()),
            ("qwen", _qwen),
        ]
    else:
        candidates = [
            ("qwen", _qwen),
            ("local", lambda: SentenceTransformersEmbedder()),
        ]

    for label, factory in candidates:
        try:
            emb = factory()
            logger.info("嵌入器初始化成功: %s (dim=%d)", emb.name, emb.dimension)
            return emb
        except Exception as e:
            logger.info("嵌入器 %s 不可用: %s", label, e)
    logger.warning("所有嵌入器都不可用，向量记忆功能将禁用")
    return None


# ----------------------------------------------------------------------
# 向量记忆主类
# ----------------------------------------------------------------------
@dataclass
class VectorMemoryConfig:
    base_dir: Path = Path("output/memory")
    user_id: str = "main"
    collection_name: str = "memory"
    embedder_prefer: str = "qwen"     # "qwen" / "local"
    auto_index: bool = True            # 是否在 append 时自动嵌入入库
    batch_size: int = 32
    llm_config: Any = None             # 可选：传给 make_embedder 复用 base_url/api_key


class VectorMemory:
    """ChromaDB 持久化向量记忆。"""

    def __init__(self, config: VectorMemoryConfig = None):
        self.config = config or VectorMemoryConfig()
        self._available = False
        self._embedder: Optional[Embedder] = None
        self._client = None
        self._collection = None

        self._ensure_setup()

    @property
    def available(self) -> bool:
        return self._available

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def _ensure_setup(self):
        # 1) ChromaDB
        try:
            import chromadb
        except ImportError:
            logger.warning("chromadb 未安装，向量记忆禁用 (pip install chromadb)")
            return

        # 2) 嵌入器
        self._embedder = make_embedder(
            self.config.embedder_prefer,
            llm_config=self.config.llm_config,
        )
        if self._embedder is None:
            return

        # 3) 持久化客户端（每个 user_id 一个子目录）
        try:
            db_dir = Path(self.config.base_dir) / self.config.user_id / "vector_db"
            db_dir.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(db_dir))
            self._collection = self._client.get_or_create_collection(
                name=self.config.collection_name,
                metadata={"user_id": self.config.user_id, "embedder": self._embedder.name},
            )
            self._available = True
            logger.info(
                "VectorMemory 初始化成功: user=%s, dir=%s, count=%d",
                self.config.user_id, db_dir, self._collection.count()
            )
        except Exception as e:
            logger.warning("ChromaDB 初始化失败: %s", e)
            self._available = False

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def add(self, text: str, metadata: dict = None, doc_id: str = None) -> bool:
        """添加单条记忆。"""
        if not self._available or not text:
            return False
        try:
            doc_id = doc_id or f"{self.config.user_id}_{int(time.time() * 1000)}_{hash(text) & 0xFFFF}"
            embedding = self._embedder.encode([text])[0]
            meta = {"timestamp": time.time(), "user_id": self.config.user_id}
            if metadata:
                # ChromaDB 不接受 None / dict / list，扁平化为 str
                for k, v in metadata.items():
                    if v is None:
                        continue
                    if isinstance(v, (str, int, float, bool)):
                        meta[k] = v
                    else:
                        meta[k] = str(v)
            self._collection.add(
                ids=[doc_id],
                documents=[text],
                embeddings=[embedding],
                metadatas=[meta],
            )
            return True
        except Exception as e:
            logger.warning("添加向量记忆失败: %s", e)
            return False

    def add_batch(self, texts: list[str], metadatas: list[dict] = None) -> int:
        """批量添加。返回成功条数。"""
        if not self._available or not texts:
            return 0
        try:
            embeddings = self._embedder.encode(texts)
            ids = [f"{self.config.user_id}_{int(time.time() * 1000)}_{i}_{hash(t) & 0xFFFF}"
                   for i, t in enumerate(texts)]
            metas = []
            for i, t in enumerate(texts):
                meta = {"timestamp": time.time(), "user_id": self.config.user_id}
                if metadatas and i < len(metadatas) and metadatas[i]:
                    for k, v in metadatas[i].items():
                        if v is None:
                            continue
                        if isinstance(v, (str, int, float, bool)):
                            meta[k] = v
                        else:
                            meta[k] = str(v)
                metas.append(meta)
            self._collection.add(
                ids=ids, documents=texts, embeddings=embeddings, metadatas=metas
            )
            return len(texts)
        except Exception as e:
            logger.warning("批量添加向量记忆失败: %s", e)
            return 0

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    def search(self, query: str, k: int = 5, where: dict = None) -> list[dict]:
        """语义搜索，返回 top-K。

        Returns:
            [{"text": ..., "score": ..., "metadata": {...}, "id": ...}, ...]
        """
        if not self._available or not query:
            return []
        try:
            count = self._collection.count()
            if count == 0:
                return []
            actual_k = min(k, count)
            embedding = self._embedder.encode([query])[0]
            res = self._collection.query(
                query_embeddings=[embedding],
                n_results=actual_k,
                where=where,
                include=["documents", "metadatas", "distances"],
            )
            results = []
            ids = res.get("ids", [[]])[0]
            docs = res.get("documents", [[]])[0]
            metas = res.get("metadatas", [[]])[0] or [{}] * len(docs)
            dists = res.get("distances", [[]])[0] or [0.0] * len(docs)
            for i, doc in enumerate(docs):
                results.append({
                    "id": ids[i] if i < len(ids) else "",
                    "text": doc,
                    "score": round(1.0 - float(dists[i]), 4),  # cosine: 1=最相似
                    "distance": round(float(dists[i]), 4),
                    "metadata": metas[i] if i < len(metas) else {},
                })
            return results
        except Exception as e:
            logger.warning("向量搜索失败: %s", e)
            return []

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------
    def count(self) -> int:
        if not self._available:
            return 0
        try:
            return self._collection.count()
        except Exception:
            return 0

    def stats(self) -> dict:
        return {
            "available": self._available,
            "embedder": self._embedder.name if self._embedder else None,
            "dimension": self._embedder.dimension if self._embedder else 0,
            "user_id": self.config.user_id,
            "count": self.count(),
            "db_dir": str(Path(self.config.base_dir) / self.config.user_id / "vector_db"),
        }

    def clear(self):
        """清空当前 user 的所有向量记忆。"""
        if not self._available:
            return
        try:
            self._client.delete_collection(self.config.collection_name)
            self._collection = self._client.get_or_create_collection(
                name=self.config.collection_name,
                metadata={"user_id": self.config.user_id},
            )
            logger.info("VectorMemory 已清空: user=%s", self.config.user_id)
        except Exception as e:
            logger.warning("清空向量记忆失败: %s", e)
