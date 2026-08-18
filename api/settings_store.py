"""
知识库统计与缓存维护

配置项的读写已统一由 rag/config_store.py 负责，本模块只保留
与运行中 RAG 实例强相关的几类操作：统计信息、清除缓存、数据备份。
"""

import os
import time
import zipfile
import logging
from typing import Dict, Optional

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
            "kb_version": "legacy",
            "perf": {"requests": 0},
        }

        if rag_pipeline is None:
            return stats

        # 当前知识库版本（热切换后自动跟随新版本）
        kb_version = getattr(rag_pipeline, "kb_build_id", None)
        if kb_version:
            stats["kb_version"] = kb_version

        # 请求性能汇总（最近 N 次请求的平均耗时，来自 rag/telemetry）
        try:
            from rag.telemetry import perf_recorder
            stats["perf"] = perf_recorder.summary()
        except Exception as e:
            logger.debug(f"性能统计读取失败: {e}")
            stats["perf"] = {"requests": 0}

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

    # ── 数据备份 ─────────────────────────────────────────

    def backup(self, rag_pipeline=None, target_dir: Optional[str] = None) -> Dict:
        """把用户数据打包成带时间戳的 zip 备份

        备份内容(用户生成的数据,不含可重新下载的索引/语料):
          - api/conversations.db     对话记录(含消息树)
          - config.json              用户配置
          - rag/.env                 密钥与端点(注意:备份含敏感信息,
                                      请妥善保管备份文件)
          - rag/cache_answers.db     回答缓存(可选,存在才打包)
          - rag/cache_embeddings.db  嵌入缓存(可选)

        target_dir 缺省为项目根目录(与 .gitignore 的 backup_*/ 匹配)。
        返回 {ok, path, files}。
        """
        from .conversation_store import CONVERSATIONS_DB_PATH
        from rag.config_store import PROJECT_ROOT, CONFIG_PATH, ENV_PATH
        from rag.cache_store import ANSWER_CACHE_PATH, EMBED_CACHE_PATH

        if target_dir is None:
            target_dir = PROJECT_ROOT
        os.makedirs(target_dir, exist_ok=True)

        stamp = time.strftime("%Y%m%d-%H%M%S")
        zip_path = os.path.join(target_dir, f"backup_{stamp}.zip")

        # 需要先 flush 一下 SQLite 的 WAL,保证备份到的是已提交数据
        candidates = [
            ("conversations.db", CONVERSATIONS_DB_PATH, True),
            ("config.json", CONFIG_PATH, True),
            ("rag/.env", ENV_PATH, False),  # 可能不存在(纯 json 配置)
            ("cache_answers.db", ANSWER_CACHE_PATH, False),
            ("cache_embeddings.db", EMBED_CACHE_PATH, False),
        ]

        # 若 pipeline 持有连接,先让 SQLite checkpoint(把 WAL 合并回主库)
        if rag_pipeline is not None:
            for cache in (getattr(rag_pipeline, "answer_cache", None),
                          getattr(getattr(rag_pipeline, "retriever", None),
                                  "embed_cache", None)):
                try:
                    cache._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    pass
        # conversations.db 由 api 层持有连接,这里用独立连接触发一次
        # checkpoint 保证备份到已提交数据(WAL 模式下 zip 只读主库)
        try:
            import sqlite3 as _sqlite3
            _c = _sqlite3.connect(CONVERSATIONS_DB_PATH)
            try:
                _c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                _c.close()
        except Exception:
            pass

        packed = []
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for arcname, path, required in candidates:
                if os.path.exists(path):
                    zf.write(path, arcname)
                    packed.append(arcname)
                elif required:
                    logger.warning(f"备份: 必需文件缺失 {path}")
            zf.writestr("BACKUP_INFO.txt",
                        "MarxLen 数据备份\n生成时间: %s\n"
                        "包含: 对话记录、配置、密钥(请勿外传)、缓存\n"
                        "恢复: 解压后按相同路径放回即可\n"
                        % time.strftime("%Y-%m-%d %H:%M:%S"))

        size = os.path.getsize(zip_path) if os.path.exists(zip_path) else 0
        logger.info(f"备份完成: {zip_path} ({size} 字节, {len(packed)} 个文件)")
        return {"ok": True, "path": zip_path, "files": packed,
                "size": size}
