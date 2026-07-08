import os
import heapq
import jieba
import logging
import pickle
import requests
import threading
import numpy as np
from typing import List, Dict, Any, Optional
from rank_bm25 import BM25Okapi
from dotenv import load_dotenv

from .rag_embed import QwenEmbedder
from .vector_store import FAISSStore
from .cache_store import EmbeddingCache

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv(override=True)

RAG_DIR = os.path.dirname(os.path.abspath(__file__))
BM25_INDEX_PATH = os.path.join(RAG_DIR, "bm25_index.pkl")
DOCUMENTS_DB_PATH = os.path.join(RAG_DIR, "documents.db")
FAISS_INDEX_PATH = os.path.join(RAG_DIR, "faiss_index.idx")

# 默认配置常量
RERANK_TIMEOUT = int(os.getenv("RERANK_TIMEOUT", "30"))
SCORE_THRESHOLD = float(os.getenv("SCORE_THRESHOLD", "0.0"))


class HybridRetriever:
    """混合检索与重排器 (Dense + Sparse/BM25 + Rerank)

    稠密检索使用 FAISS + SQLite（替代原来的 Qdrant Local），
    启动时 FAISS 和 BM25 采用并发加载（提速 30%）。
    """

    def __init__(self,
                 faiss_path: str = FAISS_INDEX_PATH,
                 db_path: str = DOCUMENTS_DB_PATH):
        self.faiss_path = faiss_path
        self.db_path = db_path

        # 初始化嵌入缓存
        self.embed_cache = EmbeddingCache()

        # ── 并发加载 FAISS 和 BM25 ──────────────────────────
        faiss_loaded = threading.Event()
        bm25_loaded = threading.Event()
        load_errors = []
        _err_lock = threading.Lock()

        def load_faiss():
            try:
                logging.info("正在加载 FAISS 向量索引...")
                self.store = FAISSStore(faiss_path=self.faiss_path, db_path=self.db_path)
                self.store.load()
            except Exception as e:
                with _err_lock:
                    load_errors.append(e)
            finally:
                faiss_loaded.set()

        def load_bm25():
            try:
                self._load_bm25_index()
            except Exception as e:
                with _err_lock:
                    load_errors.append(e)
            finally:
                bm25_loaded.set()

        faiss_thread = threading.Thread(target=load_faiss, daemon=True)
        bm25_thread = threading.Thread(target=load_bm25, daemon=True)
        faiss_thread.start()
        bm25_thread.start()

        # 等待两者都完成
        faiss_loaded.wait()
        bm25_loaded.wait()
        if load_errors:
            raise RuntimeError(f"索引加载失败: {'; '.join(str(e) for e in load_errors)}")

        # 初始化 Embedding 模型
        self.embedder = QwenEmbedder()

        # 初始化 Reranker 配置
        self.rerank_api_base = os.getenv("EMBED_API_BASE_URL", "https://api2.aigcbest.top/v1")
        self.rerank_api_key = os.getenv("RERANK_API_KEY") or os.getenv("EMBED_API_KEY", "")
        self.rerank_model = os.getenv("RERANK_MODEL", "Qwen/Qwen3-Reranker-4B")

    def _load_bm25_index(self):
        """从本地磁盘加载 BM25 索引"""
        if not os.path.exists(BM25_INDEX_PATH):
            raise FileNotFoundError(f"找不到 BM25 索引文件 {BM25_INDEX_PATH}。请先运行 ingest_philosophy.py 进行建库！")

        logging.info(f"正在从 {BM25_INDEX_PATH} 加载 BM25 索引...")
        with open(BM25_INDEX_PATH, 'rb') as f:
            # 安全提示：pickle 来自本地可信数据文件，非用户/网络输入
            data = pickle.load(f)
            self.bm25 = data["bm25_index"]
            # 剥离文本以节省内存（文本已存在 SQLite 中，检索时按 ID 回查）
            docs = []
            for d in data["metadata"]:
                doc = {k: v for k, v in d.items() if k != "text"}
                doc["_text_len"] = len(d.get("text", ""))
                docs.append(doc)
            self.docs = docs  # 全部构建成功后再赋值
        logging.info(f"BM25 索引加载完毕，共包含 {len(self.docs)} 条文档。")

    def normalize_l2(self, vector) -> List[float]:
        """L2 归一化，始终返回 List[float]"""
        arr = np.array(vector, dtype=np.float32)
        norm = np.linalg.norm(arr)
        if norm == 0:
            return np.zeros_like(arr).tolist()
        return (arr / norm).tolist()

    def dense_search(self, query: str, k: int = 10) -> List[Dict[str, Any]]:
        """稠密向量检索 (FAISS + SQLite)，优先使用缓存"""
        # 1. 尝试从缓存获取向量
        cached_vector = self.embed_cache.get(query)
        if cached_vector is not None:
            query_vector = cached_vector
        else:
            # 2. 调用 API 嵌入
            query_vector = self.embedder.embed_single(query)
            query_vector = self.normalize_l2(query_vector)
            # 3. 写入缓存
            self.embed_cache.put(query, query_vector)

        results = self.store.search(query_vector, k=k)
        for r in results:
            r.setdefault("metadata", {})
            for field in ("title", "chapter", "category", "source"):
                if field not in r["metadata"]:
                    r["metadata"][field] = ""
        return results

    def sparse_search(self, query: str, k: int = 10) -> List[Dict[str, Any]]:
        """稀疏检索 (BM25)，文本从 SQLite 实时查询以节省内存"""
        tokenized_query = list(jieba.cut_for_search(query))
        doc_scores = self.bm25.get_scores(tokenized_query)

        # 用堆取 top-k，O(n log k) 优于全排序 O(n log n)
        k = min(k, len(doc_scores))
        top_k_indices = heapq.nlargest(k, range(len(doc_scores)),
                                        key=lambda i: doc_scores[i])

        # 批量查询 SQLite（BM25 位置 i 的 SQLite ID 存储在 docs[i]['id']）
        texts = {}
        if top_k_indices:
            sqlite_ids = [self.docs[idx].get("id", idx + 1) for idx in top_k_indices]
            placeholders = ','.join(['?'] * len(sqlite_ids))
            rows = self.store.conn.execute(
                f"SELECT id, text FROM documents WHERE id IN ({placeholders})",
                sqlite_ids
            ).fetchall()
            texts = {row[0]: row[1] or "" for row in rows}

        results = []
        for idx in top_k_indices:
            doc_payload = self.docs[idx]
            doc_id = f"bm25_doc_{idx}"
            sqlite_id = doc_payload.get("id", idx + 1)
            text = texts.get(sqlite_id, "")

            metadata = {k: v for k, v in doc_payload.items() if not k.startswith("_")}

            results.append({
                "id": doc_id,
                "score": doc_scores[idx],
                "text": text,
                "metadata": metadata
            })
        return results

    def _deduplicate(self, docs: List[Dict]) -> List[Dict]:
        """基于文本内容的去重，保留首次出现的结果"""
        seen = set()
        unique = []
        for doc in docs:
            text = doc.get("text", "")
            # 用文本前 200 字的哈希作为去重依据
            h = hash(text[:200])
            if h not in seen:
                seen.add(h)
                unique.append(doc)
        return unique

    def rrf_combine(self, dense_results: List[Dict], sparse_results: List[Dict], k: int = 60) -> List[Dict]:
        """Reciprocal Rank Fusion (RRF)"""
        rrf_scores = {}
        combined_docs = {}

        def _add_rank(rank: int, doc: Dict):
            doc_id = doc["id"]
            if doc_id not in rrf_scores:
                rrf_scores[doc_id] = 0
                combined_docs[doc_id] = doc
            rrf_scores[doc_id] += 1.0 / (k + rank + 1)

        for rank, doc in enumerate(dense_results):
            _add_rank(rank, doc)
        for rank, doc in enumerate(sparse_results):
            _add_rank(rank, doc)

        sorted_ids = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)

        results = []
        for doc_id in sorted_ids:
            doc = combined_docs[doc_id]
            doc["rrf_score"] = rrf_scores[doc_id]
            results.append(doc)

        return results

    def rerank(self, query: str, documents: List[Dict], top_n: int = 5) -> List[Dict]:
        """调用 Reranker API 进行重排序"""
        if not documents:
            return []

        texts = [doc.get("text", "") for doc in documents]
        if not any(texts):
            logging.warning(f"  Rerank 输入全部为空文本，跳过")
            return documents[:top_n]

        url = f"{self.rerank_api_base}/rerank"
        headers = {
            "Authorization": f"Bearer {self.rerank_api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.rerank_model,
            "query": query,
            "documents": texts
        }

        try:
            response = requests.post(url, headers=headers, json=payload,
                                     timeout=RERANK_TIMEOUT)
            response.raise_for_status()
            data = response.json()

            results = data.get("results", [])
            # API 已按 relevance_score 降序返回，此处仅防御性排序
            if results:
                results.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)

            return [
                {**documents[item["index"]].copy(), "rerank_score": item["relevance_score"]}
                for item in results[:top_n]
            ]

        except requests.Timeout:
            logging.error(f"  Rerank 超时 ({RERANK_TIMEOUT}s), query='{query[:30]}'")
            return documents[:top_n]
        except requests.RequestException as e:
            logging.error(f"  Rerank API 请求失败: {e}, query='{query[:30]}'")
            return documents[:top_n]
        except (KeyError, IndexError) as e:
            logging.error(f"  Rerank 响应格式异常: {e}")
            return documents[:top_n]

    def retrieve(self, query: str, top_k: int = 5, fetch_k: int = 30) -> List[Dict]:
        """
        完整的检索流水线:
        1. BM25 获取 fetch_k 个
        2. 向量检索 获取 fetch_k 个
        3. RRF 融合
        4. **文本去重**（新增）
        5. Rerank 重排提取 top_k 个
        """
        logging.info(f"开始检索: '{query}'")

        sparse_res = self.sparse_search(query, k=fetch_k)
        dense_res = self.dense_search(query, k=fetch_k)
        logging.info(f"  稠密检索: {len(dense_res)} 条 | 稀疏检索: {len(sparse_res)} 条")

        hybrid_res = self.rrf_combine(dense_res, sparse_res)
        logging.info(f"海选融合后共有 {len(hybrid_res)} 个候选片段")

        # 去重（文本内容去重，保留首次出现的）
        de_duped = self._deduplicate(hybrid_res)
        if len(de_duped) < len(hybrid_res):
            logging.info(f"  去重移除 {len(hybrid_res) - len(de_duped)} 条重复片段")

        final_res = self.rerank(query, de_duped, top_n=top_k)
        logging.info(f"Rerank 完成，返回 Top-{len(final_res)} 个结果")

        return final_res


if __name__ == "__main__":
    retriever = HybridRetriever()

    query = "唯物主义是什么？"
    print(f"\n======================================")
    print(f"提问: {query}")
    print(f"======================================\n")

    results = retriever.retrieve(query, top_k=3)

    for i, res in enumerate(results):
        print(f"[{i+1}] 来源: {res['metadata'].get('chapter', '?')} > {res['metadata'].get('title', '?')}")
        print(f"    Rerank得分: {res.get('rerank_score', 0):.4f} | RRF得分: {res.get('rrf_score', 0):.4f}")
        print(f"    内容: {res['text'][:150]}...\n")
