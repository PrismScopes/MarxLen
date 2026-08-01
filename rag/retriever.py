import os
import heapq
import logging
import pickle
import requests
import threading
import concurrent.futures
import numpy as np
from typing import List, Dict, Any, Optional
from rank_bm25 import BM25Okapi
from dotenv import load_dotenv

from .rag_embed import QwenEmbedder
from .vector_store import FAISSStore
from .cache_store import EmbeddingCache
from .config_store import get_config
from .bm25_tokenizer import tokenize_bm25

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv(override=True)

RAG_DIR = os.path.dirname(os.path.abspath(__file__))
BM25_INDEX_PATH = os.path.join(RAG_DIR, "bm25_index.pkl")
DOCUMENTS_DB_PATH = os.path.join(RAG_DIR, "documents.db")
FAISS_INDEX_PATH = os.path.join(RAG_DIR, "faiss_index.idx")


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
        self.config = get_config()

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

        # 两个索引必须来自同一次建库，否则 BM25 命中会回查到错误的文本
        self._verify_index_consistency()

        # 初始化 Embedding 模型
        self.embedder = QwenEmbedder()

        # 初始化 Reranker 配置
        self.rerank_api_base = self.config.get("embed_api_base_url")
        self.rerank_api_key = self.config.get("embed_api_key")
        self.rerank_model = self.config.get("rerank_model")

    def _verify_index_consistency(self):
        """校验 BM25 索引与 SQLite/FAISS 是否来自同一次建库

        BM25 元数据里的 id 指向 SQLite 主键，两者不同步重建时检索会静默返回
        错位的文本。这里在启动阶段就暴露问题，而不是等到用户提问时才出错。

        注意：BM25 元数据在建库时已剥离 text 字段，无法比对正文，
        因此改用 title/source 这两个必然存在的元数据字段做比对。
        """
        doc_count = self.store.conn.execute(
            "SELECT COUNT(*) FROM documents"
        ).fetchone()[0]

        if len(self.docs) != doc_count:
            raise RuntimeError(
                f"索引不一致：BM25 有 {len(self.docs)} 条，SQLite 有 {doc_count} 条。"
                f"请重新运行 ingest_philosophy.py 同时重建两个索引。"
            )

        # 抽查若干条，确认 id 指向的 SQLite 行与 BM25 元数据描述的是同一篇文档
        step = max(1, len(self.docs) // 50)
        mismatched = 0
        checked = 0
        for i in range(0, len(self.docs), step):
            doc = self.docs[i]
            doc_id = doc.get("id")
            if doc_id is None:
                raise RuntimeError(
                    "BM25 元数据缺少 id 字段，属于旧版索引格式，检索会回查到错误文本。"
                    "请重新运行 ingest_philosophy.py 重建索引。"
                )
            row = self.store.conn.execute(
                "SELECT title, source FROM documents WHERE id = ?", (doc_id,)
            ).fetchone()
            checked += 1
            if row is None or row[0] != doc.get("title", "") or row[1] != doc.get("source", ""):
                mismatched += 1

        if mismatched:
            raise RuntimeError(
                f"索引错位：抽查 {checked} 条中有 {mismatched} 条 BM25 元数据与 SQLite 不匹配。"
                f"请重新运行 ingest_philosophy.py 重建索引。"
            )

        # FAISS 里可能残留指向已不存在 SQLite 行的向量（历史建库时批次回滚
        # 但 AUTOINCREMENT 序号已推进所致）。这类命中会在 search() 中被跳过，
        # 不影响正确性，只是稠密召回会略少于 k，因此仅告警不中断。
        vector_count = self.store.count()
        if vector_count > doc_count:
            logging.warning(
                f"FAISS 有 {vector_count} 条向量，SQLite 只有 {doc_count} 条文档，"
                f"其中 {vector_count - doc_count} 条向量无对应文本会被跳过。"
                f"重新运行 ingest_philosophy.py 可消除该差异。"
            )

        logging.info(f"索引一致性校验通过（{doc_count} 条文档，抽查 {checked} 条）")

    def _load_bm25_index(self):
        """从本地磁盘加载 BM25 索引"""
        if not os.path.exists(BM25_INDEX_PATH):
            raise FileNotFoundError(f"找不到 BM25 索引文件 {BM25_INDEX_PATH}。请先运行 ingest_philosophy.py 进行建库！")

        logging.info(f"正在从 {BM25_INDEX_PATH} 加载 BM25 索引...")
        with open(BM25_INDEX_PATH, 'rb') as f:
            # 安全提示：pickle 来自本地可信数据文件，非用户/网络输入
            data = pickle.load(f)
            self.bm25 = data["bm25_index"]
            # 元数据不含正文（建库时已剥离，文本存在 SQLite 中，检索时按 id 回查）
            self.docs = data["metadata"]
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
        use_cache = bool(self.config.get("enable_embed_cache"))
        # 1. 尝试从缓存获取向量
        cached_vector = self.embed_cache.get(query) if use_cache else None
        if cached_vector is not None:
            query_vector = cached_vector
        else:
            # 2. 调用 API 嵌入
            query_vector = self.embedder.embed_single(query)
            query_vector = self.normalize_l2(query_vector)
            # 3. 写入缓存
            if use_cache:
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
        # 与建库端共用 tokenize_bm25：先清洗 LaTeX/HTML 乱码再分词，
        # 保证查询词与索引词表一致
        tokenized_query = tokenize_bm25(query)
        doc_scores = self.bm25.get_scores(tokenized_query)

        # 用堆取 top-k，O(n log k) 优于全排序 O(n log n)
        k = min(k, len(doc_scores))
        top_k_indices = heapq.nlargest(k, range(len(doc_scores)),
                                        key=lambda i: doc_scores[i])

        # 批量查询 SQLite（BM25 位置 i 的 SQLite 主键存储在 docs[i]['id']，
        # 该对应关系由建库脚本保证，并在启动时经 _verify_index_consistency 校验）
        texts = {}
        if top_k_indices:
            sqlite_ids = [self.docs[idx]["id"] for idx in top_k_indices]
            placeholders = ','.join(['?'] * len(sqlite_ids))
            # 一并取 uuid：BM25 元数据里没有它，而原文阅读器要靠 uuid 定位，
            # 缺了会让纯 BM25 命中的来源卡片无法跳转
            # 与 dense_search 并行时共用同一个 SQLite 连接，
            # 走 store.query 以获得锁保护
            rows = self.store.query(
                f"SELECT id, text, uuid FROM documents WHERE id IN ({placeholders})",
                sqlite_ids
            )
            texts = {row[0]: (row[1] or "", row[2] or "") for row in rows}

        results = []
        for idx in top_k_indices:
            doc_payload = self.docs[idx]
            sqlite_id = doc_payload["id"]
            text, uid = texts.get(sqlite_id, ("", ""))

            metadata = {k: v for k, v in doc_payload.items() if k != "id"}

            results.append({
                # 用 SQLite 主键作为文档标识，与稠密检索保持同一 ID 空间，
                # 这样 RRF 才能把两路命中的同一篇文档合并计分
                "id": sqlite_id,
                "uuid": uid,
                "score": doc_scores[idx],
                "text": text,
                "metadata": metadata
            })
        return results

    def _deduplicate(self, docs: List[Dict]) -> List[Dict]:
        """基于文本内容的去重，保留首次出现的结果"""
        seen = set()
        unique = []
        prefix_len = int(self.config.get("dedup_prefix_len"))
        for doc in docs:
            text = doc.get("text", "")
            # 用文本前若干字的哈希作为去重依据
            h = hash(text[:prefix_len])
            if h not in seen:
                seen.add(h)
                unique.append(doc)
        return unique

    def rrf_combine(self, *result_lists: List[Dict], k: Optional[int] = None) -> List[Dict]:
        """Reciprocal Rank Fusion (RRF)，支持任意条检索通道融合

        各通道结果的 id 同为 SQLite 主键，因此同一篇文档被多个通道命中时
        会被合并，分数累加，从而给"多通道共识"的结果加权。

        k 为平滑常数，不传则取设置项 rrf_k。

        用法:
            rrf_combine(dense_res, sparse_res)              # 两路
            rrf_combine(*dense_res_list, sparse_res)        # 多路（HyDE 多命题）
        """
        if k is None:
            k = int(self.config.get("rrf_k"))
        rrf_scores = {}
        combined_docs = {}

        def _add_rank(rank: int, doc: Dict):
            doc_id = doc["id"]
            if doc_id not in rrf_scores:
                rrf_scores[doc_id] = 0
                combined_docs[doc_id] = doc
            elif not combined_docs[doc_id].get("text") and doc.get("text"):
                # 保留有正文的那一份，避免合并后丢失文本导致 rerank 失效
                combined_docs[doc_id] = doc
            rrf_scores[doc_id] += 1.0 / (k + rank + 1)

        for results in result_lists:
            for rank, doc in enumerate(results):
                _add_rank(rank, doc)

        sorted_ids = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)

        results = []
        for doc_id in sorted_ids:
            doc = combined_docs[doc_id]
            doc["rrf_score"] = rrf_scores[doc_id]
            results.append(doc)

        return results

    def rerank(self, query: str, documents: List[Dict], top_n: int = 5) -> List[Dict]:
        """调用 Reranker API 进行重排序

        关闭 enable_reranker 时直接按 RRF 得分截断，省掉一次 API 调用。
        重排完成后按 score_threshold 过滤低相关文档（阈值为 0 表示不过滤）。
        """
        if not documents:
            return []

        if not self.config.get("enable_reranker"):
            logging.info("  重排序已关闭，直接按 RRF 得分取 Top-N")
            return [{**d, "rerank_score": d.get("rrf_score", 0.0)}
                    for d in documents[:top_n]]

        texts = [doc.get("text", "") for doc in documents]
        if not any(texts):
            logging.warning(f"  Rerank 输入全部为空文本，跳过")
            return documents[:top_n]

        timeout = int(self.config.get("rerank_timeout"))
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
                                     timeout=timeout)
            response.raise_for_status()
            data = response.json()

            results = data.get("results", [])
            # API 已按 relevance_score 降序返回，此处仅防御性排序
            if results:
                for r in results:
                    if "relevance_score" not in r and "score" in r:
                        r["relevance_score"] = r["score"]
                results.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)

            ranked = [
                {**documents[item["index"]].copy(), "rerank_score": item["relevance_score"]}
                for item in results[:top_n]
            ]
            return self._filter_by_threshold(ranked)

        except requests.Timeout:
            logging.error(f"  Rerank 超时 ({timeout}s), query='{query[:30]}'")
            return [{**d, "rerank_score": 0.0} for d in documents[:top_n]]
        except requests.RequestException as e:
            logging.error(f"  Rerank API 请求失败: {e}, query='{query[:30]}'")
            return [{**d, "rerank_score": 0.0} for d in documents[:top_n]]
        except (KeyError, IndexError) as e:
            logging.error(f"  Rerank 响应格式异常: {e}")
            return [{**d, "rerank_score": 0.0} for d in documents[:top_n]]

    def _filter_by_threshold(self, ranked: List[Dict]) -> List[Dict]:
        """丢弃重排得分低于阈值的文档

        全部被滤掉时保留得分最高的一条：宁可给模型一条弱相关资料，
        也不要让它彻底无据可依。
        """
        threshold = float(self.config.get("score_threshold"))
        if threshold <= 0 or not ranked:
            return ranked
        kept = [d for d in ranked if d.get("rerank_score", 0.0) >= threshold]
        if len(kept) < len(ranked):
            logging.info(f"  相关度阈值 {threshold} 过滤掉 {len(ranked) - len(kept)} 条")
        return kept or ranked[:1]

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

        # 两路并行：向量检索要等一次嵌入 API 往返，BM25 是本地计算，
        # 串行等于白等前者的网络时间。
        # 注意 rrf_combine 的入参顺序会影响同分文档的先后（实测确有差异），
        # 所以这里用 result() 按固定顺序取回，不能改成 as_completed。
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f_sparse = executor.submit(self.sparse_search, query, fetch_k)
            f_dense = executor.submit(self.dense_search, query, fetch_k)
            sparse_res = f_sparse.result()
            dense_res = f_dense.result()

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

    def retrieve_by_plan(self, plan, top_k: int = 5, fetch_k: int = 30) -> List[Dict]:
        """按检索计划执行多通道检索（RAG_prompt 步骤四）

        与单查询的 retrieve() 相比，本方法把不同类型的查询送入对应通道：
          - 语义向量通道：用假设性命题（HyDE），每条命题独立召回
          - 关键词通道：用经典范畴术语走 BM25
        各通道结果统一由 RRF 融合，被多通道共同命中的文档自然排到前面。

        参数:
            plan: QueryPlan 实例（rag.query_planner）

        分析失败（plan.analysis_ok=False）时退回单查询流程。
        """
        if not getattr(plan, "analysis_ok", False):
            return self.retrieve(plan.question, top_k=top_k, fetch_k=fetch_k)

        dense_queries = plan.dense_queries()
        # 关闭多通道时只保留首条命题，退化为"单查询 + 关键词"两路
        if not self.config.get("enable_multi_channel"):
            dense_queries = dense_queries[:1]
        sparse_query = plan.sparse_query()

        logging.info(f"开始多通道检索: {len(dense_queries)} 条语义命题 + 1 条关键词query")

        # 通道数越多，单通道取的候选数相应减少，避免候选池过度膨胀
        per_channel_k = max(10, fetch_k // max(1, len(dense_queries)))

        # 各通道并行执行：语义通道每条都要等一次嵌入 API 往返，串行时
        # 耗时随命题条数线性增长；它们之间没有依赖，并行后总耗时约等于
        # 最慢的那一路。BM25 是纯本地计算，一并放进去顺带省掉它的时间。
        #
        # 单个通道失败不应让整次检索失败：嵌入 API 偶发抖动时，
        # 只要还有其他通道有结果，检索质量下降但仍可用。
        tasks = [("dense", i, q, per_channel_k)
                 for i, q in enumerate(dense_queries)]
        tasks.append(("sparse", 0, sparse_query, fetch_k))

        def _run(task):
            kind, idx, q, k = task
            try:
                if kind == "dense":
                    return kind, idx, q, self.dense_search(q, k=k), None
                return kind, idx, q, self.sparse_search(q, k=k), None
            except Exception as e:
                return kind, idx, q, None, e

        # 结果要按固定顺序收集：RRF 的名次由列表内顺序决定，
        # 若按线程完成顺序拼接，同样的输入会算出不同的分数
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(tasks)
        ) as executor:
            outcomes = list(executor.map(_run, tasks))

        channels = []
        for kind, idx, q, res, err in outcomes:
            label = f"语义通道{idx + 1}" if kind == "dense" else "关键词通道"
            if err is not None:
                logging.warning(f"  {label} 失败，跳过: {err}")
                continue
            logging.info(f"  {label}: {len(res)} 条 | '{q[:40]}'")
            channels.append(res)

        if not channels:
            logging.error("  所有检索通道均失败，返回空结果")
            return []

        hybrid_res = self.rrf_combine(*channels)
        logging.info(f"多通道融合后共有 {len(hybrid_res)} 个候选片段")

        de_duped = self._deduplicate(hybrid_res)
        if len(de_duped) < len(hybrid_res):
            logging.info(f"  去重移除 {len(hybrid_res) - len(de_duped)} 条重复片段")

        # 重排的基准查询用核心矛盾（若有），它最能代表本次提问的真实意图；
        # 否则退回用户原问题
        rerank_query = plan.core_contradiction or plan.question
        final_res = self.rerank(rerank_query, de_duped, top_n=top_k)
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
