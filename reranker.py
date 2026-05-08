"""检索结果重排 (Reranker)

向量检索（embedding）只是一阶检索，能召回但精度有限。常见做法是：
- Stage 1: 向量检索召回 top-N（N 较大，例如 20）
- Stage 2: 用 cross-encoder 模型对 (query, passage) 做精排，输出 top-K（K 较小，例如 5）

本模块的 BGEReranker 用 BAAI/bge-reranker-v2-m3 这种小型 cross-encoder（中英文皆可，
~600MB 左右），放在 `sentence_transformers.CrossEncoder` 接口上。

依赖（可选）：
    pip install sentence-transformers
    pip install transformers   # CrossEncoder 通常自动拉

如果 sentence-transformers 不可用，本模块提供 NoOpReranker（保持原顺序），
不会让 KnowledgeBase.search 崩溃。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("reranker")


class Reranker:
    """统一接口。"""

    name: str = "base"

    def rerank(self, query: str, passages: list[str],
                top_k: int = None) -> list[tuple[int, float]]:
        """返回 [(原始索引, 分数), ...]，按分数降序。"""
        raise NotImplementedError


class NoOpReranker(Reranker):
    """不重排，保持原顺序（占位实现）。"""

    name = "noop"

    def rerank(self, query: str, passages: list[str],
                top_k: int = None) -> list[tuple[int, float]]:
        out = [(i, 1.0 / (i + 1)) for i in range(len(passages))]
        if top_k is not None:
            out = out[:top_k]
        return out


class BGEReranker(Reranker):
    """基于 sentence-transformers CrossEncoder 的 BGE 重排器。

    默认模型：BAAI/bge-reranker-v2-m3（多语言，~600MB，首次会自动下载）
    其他选项：BAAI/bge-reranker-large（中英文，更大）

    输出未经 sigmoid 的 logits 分数；只用相对排序，不用绝对值。
    """

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3",
                 device: str = None, max_length: int = 512):
        try:
            from sentence_transformers import CrossEncoder
        except ImportError:
            raise ImportError(
                "BGEReranker 需要 sentence-transformers，请安装：\n"
                "    pip install sentence-transformers"
            )

        kwargs = {"max_length": max_length}
        if device:
            kwargs["device"] = device
        try:
            self._model = CrossEncoder(model_name, **kwargs)
        except Exception as e:
            raise RuntimeError(f"加载 BGE reranker 模型失败: {e}")

        self.name = f"bge:{model_name}"

    def rerank(self, query: str, passages: list[str],
                top_k: int = None) -> list[tuple[int, float]]:
        if not passages:
            return []
        pairs = [(query, p or "") for p in passages]
        try:
            scores = self._model.predict(pairs, show_progress_bar=False)
        except Exception as e:
            logger.warning("BGE 重排失败，回退到原顺序: %s", e)
            return [(i, 1.0 / (i + 1)) for i in range(len(passages))]

        scored = list(enumerate([float(s) for s in scores]))
        scored.sort(key=lambda x: -x[1])
        if top_k is not None:
            scored = scored[:top_k]
        return scored


def make_reranker(model_name: str = "BAAI/bge-reranker-v2-m3",
                   device: str = None) -> Reranker:
    """工厂方法：能创建 BGE 就创建，否则给 NoOp（不抛错）。"""
    try:
        return BGEReranker(model_name=model_name, device=device)
    except Exception as e:
        logger.info("BGE Reranker 不可用，降级为 NoOp: %s", e)
        return NoOpReranker()
