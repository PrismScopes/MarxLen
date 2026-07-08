"""
设置存储模块

统一管理 .env 配置文件的读写，以及运行时设置（温度、Top-K 等）。
.env 文件保存在 rag/.env，运行时设置缓存在内存中。
"""

import os
import re
import logging
from typing import Dict, Optional
from dotenv import load_dotenv
from rag.cache_store import AnswerCache, EmbeddingCache

logger = logging.getLogger(__name__)

# .env 文件路径（相对于项目根目录）
_ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rag", ".env")

# 默认设置
_DEFAULT_SETTINGS = {
    "model": "deepseek-chat",
    "api_base_url": "https://api.deepseek.com/v1",
    "embed_api_base_url": "https://api2.aigcbest.top/v1",
    "embed_model": "Qwen/Qwen3-Embedding-0.6B",
    "temperature": 0.3,
    "top_k": 8,
    "fetch_k": 30,
    "enable_reranker": True,
    "default_mode": "general",
}


class SettingsStore:
    """设置存储，读写 .env 文件 + 内存缓存运行时设置"""

    def __init__(self):
        self._runtime: Dict = {}  # 运行时设置（不持久化到 .env）

    # ── 读取 ─────────────────────────────────────────────

    def get_all(self) -> Dict:
        """获取所有设置（.env + 运行时）"""
        load_dotenv(_ENV_PATH, override=True)

        settings = {
            # 模型
            "model": os.getenv("DEEPSEEK_MODEL", _DEFAULT_SETTINGS["model"]),
            "api_base_url": os.getenv("DEEPSEEK_API_BASE_URL", _DEFAULT_SETTINGS["api_base_url"]),
            "embed_api_base_url": os.getenv("EMBED_API_BASE_URL", _DEFAULT_SETTINGS["embed_api_base_url"]),
            "embed_model": os.getenv("EMBED_MODEL", _DEFAULT_SETTINGS["embed_model"]),

            # 运行时
            "temperature": self._runtime.get("temperature", _DEFAULT_SETTINGS["temperature"]),
            "top_k": self._runtime.get("top_k", _DEFAULT_SETTINGS["top_k"]),
            "fetch_k": self._runtime.get("fetch_k", _DEFAULT_SETTINGS["fetch_k"]),
            "enable_reranker": self._runtime.get("enable_reranker", _DEFAULT_SETTINGS["enable_reranker"]),
            "default_mode": self._runtime.get("default_mode", _DEFAULT_SETTINGS["default_mode"]),
        }
        return settings

    def get(self, key: str, default=None):
        return self.get_all().get(key, default)

    # ── 写入 ─────────────────────────────────────────────

    def update(self, updates: Dict) -> Dict:
        """更新设置。
        
        写入 .env 的字段：model, api_base_url, embed_api_base_url, embed_model
        写入内存的字段：temperature, top_k, fetch_k, enable_reranker, default_mode
        """
        env_updates = {}
        runtime_updates = {}

        # 分类
        env_keys = {"model", "api_base_url", "embed_api_base_url", "embed_model"}
        for key, value in updates.items():
            if key in env_keys:
                env_updates[key] = value
            else:
                runtime_updates[key] = value

        # 写入 .env
        if env_updates:
            self._write_env(env_updates)

        # 更新内存
        self._runtime.update(runtime_updates)

        return self.get_all()

    def _write_env(self, updates: Dict):
        """将设置写入 .env 文件"""
        if not os.path.exists(_ENV_PATH):
            logger.warning(f".env 文件不存在: {_ENV_PATH}")
            return

        # env_key -> .env_key 映射
        key_map = {
            "model": "DEEPSEEK_MODEL",
            "api_base_url": "DEEPSEEK_API_BASE_URL",
            "embed_api_base_url": "EMBED_API_BASE_URL",
            "embed_model": "EMBED_MODEL",
        }

        with open(_ENV_PATH, "r", encoding="utf-8") as f:
            content = f.read()

        for key, value in updates.items():
            env_key = key_map.get(key, key.upper())
            # 替换已有的行，或追加
            pattern = rf"^{re.escape(env_key)}=.*"
            replacement = f"{env_key}={value}"
            if re.search(pattern, content, re.MULTILINE):
                content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
            else:
                content += f"\n{replacement}\n"

        with open(_ENV_PATH, "w", encoding="utf-8") as f:
            f.write(content)

        # 重新加载环境变量
        load_dotenv(_ENV_PATH, override=True)
        logger.info(f"已更新 .env: {list(updates.keys())}")

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
