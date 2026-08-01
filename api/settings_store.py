"""
知识库统计与缓存维护

配置项的读写已统一由 rag/config_store.py 负责，本模块只保留
与运行中 RAG 实例强相关的两类操作：统计信息、清除缓存。
"""

import os
import logging
from typing import Dict

logger = logging.getLogger(__name__)


class SettingsStore:
    """知识库统计与缓存维护工具"""

    # ── 统计信息 ─────────────────────────────────────────

    def get_stats(self, rag_pipeline=None) -> Dict:
        """获取知识库统计信息"""
        stats = {
            "document_count": 0,
            "vector_count": 0,
            "source_files": 0,
            "cache_embeddings": 0,
            "cache_answers": 0,
        }

        if rag_pipeline is None:
            return stats

        try:
            retriever = rag_pipeline.retriever
            # 向量数
            if hasattr(retriever, 'store') and retriever.store is not None:
                stats["vector_count"] = retriever.store.count()
            # 文档数
            if hasattr(retriever, 'docs') and retriever.docs is not None:
                stats["document_count"] = len(retriever.docs)
            # 来源文件数
            if hasattr(retriever, 'docs') and retriever.docs:
                sources = set()
                for d in retriever.docs:
                    src = d.get("source", "")
                    if src:
                        sources.add(src)
                stats["source_files"] = len(sources)
            # 缓存统计
            if hasattr(rag_pipeline, 'answer_cache'):
                stats["cache_answers"] = rag_pipeline.answer_cache.count()
            if hasattr(retriever, 'embed_cache'):
                stats["cache_embeddings"] = retriever.embed_cache.count()
        except Exception as e:
            logger.warning(f"获取统计信息失败: {e}")

        return stats

    # ── 清除缓存 ─────────────────────────────────────────

    def clear_cache(self, cache_type: str = "all", rag_pipeline=None) -> Dict:
        """清除缓存"""
        result = {"cleared": []}

        if rag_pipeline is None:
            return result

        if cache_type in ("all", "answer"):
            try:
                cache = rag_pipeline.answer_cache
                _path = cache.db_path
                logger.info(f"清除回答缓存: {_path}")
                if hasattr(cache, 'close'):
                    cache.close()
                if os.path.exists(_path):
                    os.remove(_path)
                rag_pipeline.answer_cache = type(cache)()
                result["cleared"].append("answer")
            except Exception as e:
                logger.exception(f"清除回答缓存失败")

        if cache_type in ("all", "embedding"):
            try:
                cache = rag_pipeline.retriever.embed_cache
                _path = cache.db_path
                logger.info(f"清除嵌入缓存: {_path}")
                if hasattr(cache, 'close'):
                    cache.close()
                if os.path.exists(_path):
                    os.remove(_path)
                rag_pipeline.retriever.embed_cache = type(cache)()
                result["cleared"].append("embedding")
            except Exception as e:
                logger.exception(f"清除嵌入缓存失败")

        return result
