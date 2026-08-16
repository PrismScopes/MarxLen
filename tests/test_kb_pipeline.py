# -*- coding: utf-8 -*-
"""
kb 离线数据工程管道综合测试

安全保证：
  - 全部落盘通过 KB_DATA_ROOT / KB_LEGACY_INDEX_DIR 重定向到临时目录；
  - 嵌入调用全部由 FakeEmbedder 替代，不产生任何 API 调用；
  - 不读取、不写入 rag/ 三件套与运行时数据库。

运行方式：
    python tests/test_kb_pipeline.py      # 或经 tests/run_all.py 聚合执行
"""

import hashlib
import os
import sys
import tempfile
import shutil

# ── 环境隔离必须在 import kb 之前完成 ──────────────────────────
_TMP_ROOT = tempfile.mkdtemp(prefix="kb_test_")
os.environ["KB_DATA_ROOT"] = os.path.join(_TMP_ROOT, "data")
os.environ["KB_LEGACY_INDEX_DIR"] = os.path.join(_TMP_ROOT, "legacy_v1")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import numpy as np

# ── FakeEmbedder：确定性假向量，零 API 调用 ─────────────────────
import rag.rag_embed as rag_embed


class FakeEmbedder:
    model = "fake-model"

    def __init__(self, *args, **kwargs):
        pass

    def get_embedding_dim(self):
        return 1024

    def embed_single(self, text):
        return self._vec(text)

    def embed_batch(self, texts):
        return [self._vec(t) for t in texts]

    @staticmethod
    def _vec(text):
        seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], 16)
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(1024).astype(np.float32)
        v /= float(np.linalg.norm(v))
        return v.tolist()


rag_embed.QwenEmbedder = FakeEmbedder

import kb.manifest as M
import kb.cas as C
import kb.chunking as K
import kb.builder as B
import kb.verify as V
import kb.release as R
import kb.seed as S
import kb.eval as E
import kb.paths as P
import kb.watcher as W

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  <- " + str(detail)) if not cond and detail else ""))


def make_ww(files):
    """在临时目录造语料,返回目录"""
    d = os.path.join(_TMP_ROOT, "ww")
    os.makedirs(d, exist_ok=True)
    body = ("生产关系一定要适合生产力性质的规律。" * 8)
    for name, extra in files.items():
        content = ("# %s\n\n" % name) + body + extra + "\n\n" + ("论著内容。" * 10)
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(content)
    return d


def make_legacy_v1():
    """在临时目录造一套假 v1 三件套(只用于 seed 只读统计)"""
    from rag.vector_store import FAISSStore
    d = os.environ["KB_LEGACY_INDEX_DIR"]
    os.makedirs(d, exist_ok=True)
    store = FAISSStore(faiss_path=os.path.join(d, "faiss_index.idx"),
                       db_path=os.path.join(d, "documents.db"))
    store.load()
    store.add(
        uuids=["u1", "u2"],
        texts=["旧文档甲。" * 30, "旧文档乙。" * 30],
        vectors=[[0.1] * 1024, [0.2] * 1024],
        metadatas=[{"title": "甲", "chapter": "", "category": "",
                    "source": "a.md"},
                   {"title": "乙", "chapter": "", "category": "",
                    "source": "b.md"}],
        persist=True,
    )
    import pickle
    from rank_bm25 import BM25Okapi
    with open(os.path.join(d, "bm25_index.pkl"), "wb") as f:
        pickle.dump({"bm25_index": BM25Okapi([["旧", "文档", "甲"],
                                              ["旧", "文档", "乙"]]),
                     "metadata": [{"id": 1, "title": "甲"},
                                  {"id": 2, "title": "乙"}]}, f)
    store.close()
    return d


# ======================================================================
# T1 manifest：扫描 / 哈希 / diff
# ======================================================================

def t1_manifest():
    d = make_ww({
        "甲书.md": "甲书特有内容" + "甲。" * 50,
        "乙书.md": "乙书特有内容" + "乙。" * 50,
    })
    snap1 = M.scan_sources(d)
    check("t1/扫描到2个文件", len(snap1) == 2)
    root1 = M.manifest_root_hash(snap1)

    # 只改 mtime 不改内容 → 哈希不变
    p = os.path.join(d, "甲书.md")
    st = os.stat(p)
    os.utime(p, (st.st_atime, st.st_mtime + 100))
    snap2 = M.scan_sources(d)
    check("t1/内容未变哈希稳定", snap2["甲书.md"]["sha256"] ==
          snap1["甲书.md"]["sha256"])
    check("t1/根哈希稳定", M.manifest_root_hash(snap2) == root1)

    # 改内容 → changed；加文件 → added
    with open(p, "a", encoding="utf-8") as f:
        f.write("新增一句。")
    with open(os.path.join(d, "丙书.md"), "w", encoding="utf-8") as f:
        f.write("# 丙\n" + "丙。" * 60)
    snap3 = M.scan_sources(d)
    diff = M.diff_manifests(snap1, snap3)
    check("t1/diff识别修改", diff["changed"] == ["甲书.md"], diff)
    check("t1/diff识别新增", diff["added"] == ["丙书.md"], diff)
    check("t1/diff未变正确", diff["unchanged"] == ["乙书.md"], diff)

    # 删除文件
    os.remove(os.path.join(d, "乙书.md"))
    diff2 = M.diff_manifests(snap1, M.scan_sources(d))
    check("t1/diff识别删除", diff2["removed"] == ["乙书.md"], diff2)

    # manifest 落盘/读回
    M.save_manifest(snap1, "t1-test")
    check("t1/manifest落盘读回", M.load_manifest("t1-test") == snap1)


# ======================================================================
# T2 cas：确定性身份
# ======================================================================

def t2_cas():
    a = C.chunk_uuid("同一段文字")
    b = C.chunk_uuid("同一段文字")
    c = C.chunk_uuid("另一段文字")
    check("t2/同文同ID", a == b)
    check("t2/异文异ID", a != c)
    check("t2/ID格式", len(a) == 36)


# ======================================================================
# T3 chunking：分块与元数据
# ======================================================================

def t3_chunking():
    content = ("# 第一章\n\n" + "段落甲内容" + "甲" * 60 + "\n\n" +
               "## 第一节\n\n" + "段落乙内容" + "乙" * 60 + "\n\n")
    chunks = K.split_file_to_chunks(content, "测试.md")
    check("t3/产出2个chunk", len(chunks) == 2, len(chunks))
    check("t3/uuid确定性", chunks[0]["uuid"] ==
          K.split_file_to_chunks(content, "测试.md")[0]["uuid"])
    check("t3/chapter带层级", chunks[1]["metadata"]["chapter"] ==
          "第一章>第一节", chunks[1]["metadata"])
    check("t3/source为文件名", chunks[0]["metadata"]["source"] == "测试.md")
    # 过短段落被过滤
    few = K.split_file_to_chunks("# 标题\n\n太短。\n\n", "短.md")
    check("t3/短段落过滤", few == [], few)


# ======================================================================
# T4 FAISS IDMap 按外部 id 取回向量（builder 增量复制依赖的 API）
# ======================================================================

def t4_faiss_reconstruct():
    import faiss
    dim = 16
    sub = faiss.IndexFlatIP(dim)
    index = faiss.IndexIDMap(sub)
    vecs = np.random.randn(5, dim).astype(np.float32)
    ids = np.array([10, 20, 30, 40, 50], dtype=np.int64)
    index.add_with_ids(vecs, ids)

    # id_map 定位 + 子索引 reconstruct（builder 增量复制所用路径）
    id_map = faiss.vector_to_array(index.id_map)
    pos = int(np.where(id_map == 10)[0][0])
    v = np.zeros(dim, dtype=np.float32)
    sub.reconstruct(pos, v)
    check("t4/id_map定位取回", np.allclose(v, vecs[0]), v[:4])

    # HNSWFlat（实际构建使用的索引类型）同样支持
    h = faiss.IndexHNSWFlat(dim, 32)
    idx2 = faiss.IndexIDMap(h)
    idx2.add_with_ids(vecs, ids)
    id_map2 = faiss.vector_to_array(idx2.id_map)
    pos2 = int(np.where(id_map2 == 40)[0][0])
    v2 = np.zeros(dim, dtype=np.float32)
    idx2.index.reconstruct(pos2, v2)
    check("t4/HNSW子索引取回", np.allclose(v2, vecs[3]))

    # builder._read_base_vectors 端到端验证
    idx_dir = os.path.join(_TMP_ROOT, "t4_index")
    os.makedirs(idx_dir, exist_ok=True)
    faiss.write_index(idx2, os.path.join(idx_dir, "faiss_index.idx"))
    got = B._read_base_vectors(idx_dir, [10, 40, 999])
    check("t4/builder按id取回", set(got.keys()) == {10, 40})
    check("t4/取回向量正确",
          np.allclose(got[10], vecs[0]) and np.allclose(got[40], vecs[3]))


# ======================================================================
# T5 全量构建 → verify → promote → resolve
# ======================================================================

def t5_full_build():
    ww = make_ww({
        "甲书.md": "甲。" * 60,
        "乙书.md": "乙。" * 60,
    })
    builder = B.IndexBuilder(full=True, ww_dir=ww)
    meta = builder.build()
    bid = meta["build_id"]
    check("t5/全量构建chunk数", meta["counts"]["new_chunks"] > 0, meta["counts"])
    check("t5/全量无复制", meta["counts"]["copied"] == 0)
    check("t5/全量嵌入成功", meta["counts"]["failed"] == 0)

    result = V.verify_build(bid, ww_dir=ww)
    check("t5/verify通过", result["ok"], result["checks"])
    check("t5/定位命中率", result["checks"]["locate"]["rate"] >= 0.95,
          result["checks"]["locate"])

    check("t5/未发布前解析为空", R.resolve_index_dir() == (None, None))

    pr = R.promote(bid)
    check("t5/promote成功", pr.get("ok"), pr)

    index_dir, cur = R.resolve_index_dir()
    check("t5/发布后解析正确", cur == bid and os.path.isdir(index_dir),
          (index_dir, cur))

    # 在线检索器能否加载该版本
    from rag.retriever import HybridRetriever
    ret = HybridRetriever(index_dir=index_dir)
    check("t5/检索器加载版本目录", ret.kb_build_id == bid)
    res = ret.sparse_search("生产关系 生产力", k=3)
    check("t5/稀疏检索有结果", len(res) >= 1, res[:1])
    ret.close()
    return bid


# ======================================================================
# T6 增量构建：复制未变、嵌入新增、热切换
# ======================================================================

def t6_incremental(base_bid):
    ww = make_ww({
        "甲书.md": "甲。" * 60,
        "乙书.md": "乙。" * 60,
    })
    # 改乙书、新增丙书；甲书未变
    with open(os.path.join(ww, "乙书.md"), "a", encoding="utf-8") as f:
        f.write("乙书新增段落" + "新。" * 50)
    with open(os.path.join(ww, "丙书.md"), "w", encoding="utf-8") as f:
        f.write("# 丙\n\n" + "丙书内容" + "丙。" * 60)

    builder = B.IndexBuilder(ww_dir=ww)   # 默认增量,基线=当前发布版
    meta = builder.build()
    bid = meta["build_id"]
    check("t6/判定为增量", meta["type"] == "incremental", meta["type"])
    check("t6/基线正确", meta["base_build_id"] == base_bid)
    check("t6/复制了未变文件chunk", meta["counts"]["copied"] > 0,
          meta["counts"]["copied"])
    check("t6/新增文件产生新chunk", meta["counts"]["new_chunks"] > 0,
          meta["counts"]["new_chunks"])
    check("t6/无嵌入失败", meta["counts"]["failed"] == 0)

    result = V.verify_build(bid, ww_dir=ww)
    check("t6/verify通过", result["ok"], result["checks"])
    check("t6/三方数量一致",
          result["checks"]["consistency"]["ok"],
          result["checks"]["consistency"])

    # 嵌入缓存跨构建生效：再全量建一次应全部缓存命中
    builder2 = B.IndexBuilder(full=True, ww_dir=ww)
    meta2 = builder2.build()
    check("t6/重建全部命中嵌入缓存",
          meta2["counts"]["embed_cache_hits"] == meta2["counts"]["new_chunks"],
          meta2["counts"])

    # promote + 热切换逻辑
    pr = R.promote(bid)
    check("t6/promote成功", pr.get("ok"), pr)

    class FakePipeline:
        def __init__(self):
            self.kb_build_id = base_bid
            self.swapped = []

        def swap_retriever(self, r, bid):
            self.kb_build_id = bid
            self.swapped.append(bid)

    import rag.retriever as rag_ret
    real_retriever = rag_ret.HybridRetriever
    rag_ret.HybridRetriever = lambda index_dir=None: _FakeRetriever(index_dir)
    try:
        pipe = FakePipeline()
        watcher = W.KBVersionWatcher(pipe)
        ok = watcher.reload_now()
        check("t6/热切换执行", ok and pipe.kb_build_id == bid, pipe.swapped)
        ok2 = watcher.reload_now()
        check("t6/同版本跳过", ok2 and len(pipe.swapped) == 1)
    finally:
        rag_ret.HybridRetriever = real_retriever

    # 回滚
    rb = R.rollback()
    check("t6/回滚到上一版", rb.get("ok") and rb["current"] == base_bid, rb)
    check("t6/回滚后解析",
          R.resolve_index_dir()[1] == base_bid)
    # 恢复发布到新版本
    R.promote(bid)
    return bid


class _FakeRetriever:
    def __init__(self, index_dir=None):
        self.index_dir = index_dir

    def close(self):
        pass


# ======================================================================
# T7 seed：把假 v1 三件套登记为基线（只读统计）
# ======================================================================

def t7_seed():
    legacy = make_legacy_v1()
    ww = make_ww({"旧书.md": "旧。" * 60})
    # 模拟首次登记:清掉前序测试产生的发布指针与 seed 登记
    if os.path.exists(P.RELEASES_PATH):
        os.remove(P.RELEASES_PATH)
    result = S.seed(force=True, ww_dir=ww)
    check("t7/seed登记成功", result.get("ok"), result)
    check("t7/统计只读取到2条", result.get("documents") == 2, result)

    # 指针初始指向 seed,resolve 应解析到 legacy 目录
    check("t7/指针指向seed", R.current_build_id() == P.SEED_BUILD_ID)
    index_dir, bid = R.resolve_index_dir()
    check("t7/seed解析到legacy目录",
          bid == P.SEED_BUILD_ID and index_dir == legacy, (index_dir, bid))

    # 以 seed 为基线做增量 → 必须退化为全量(seed 无法 chunk 级复用)
    builder = B.IndexBuilder(ww_dir=ww)
    meta = builder.build()
    check("t7/seed基线退化为全量", meta["type"] == "full", meta["type"])


# ======================================================================
# T8 golden 评估（monkeypatch 检索器，零 API）
# ======================================================================

def t8_eval():
    # 造一个已构建版本(复用 T5 的产物目录结构:直接构建一个)
    ww = make_ww({"丁书.md": "丁。" * 60})
    meta = B.IndexBuilder(full=True, ww_dir=ww).build()
    V.verify_build(meta["build_id"], ww_dir=ww)

    # 写临时 golden
    os.makedirs(P.EVAL_DIR, exist_ok=True)
    with open(P.GOLDEN_PATH, "w", encoding="utf-8") as f:
        f.write('{"question": "问题一", "expected_sources": ["丁书.md"],'
                ' "difficulty": "easy"}\n')
        f.write('{"question": "问题二", "expected_sources": ["不存在.md"],'
                ' "difficulty": "hard"}\n')

    class FakeRet:
        def dense_search(self, q, k=10):
            return [{"id": 1, "uuid": "u", "score": 0.9, "text": "x",
                     "metadata": {"source": "丁书.md"}}]

        def sparse_search(self, q, k=10):
            return [{"id": 2, "uuid": "u2", "score": 1.0, "text": "y",
                     "metadata": {"source": "戊书.md"}}]

        def rrf_combine(self, dense, sparse):
            return dense + sparse

    import rag.retriever as rag_ret
    real = rag_ret.HybridRetriever
    rag_ret.HybridRetriever = lambda index_dir=None: FakeRet()
    try:
        result = E.evaluate(meta["build_id"], meta["index_dir"])
    finally:
        rag_ret.HybridRetriever = real

    check("t8/评估2题", result["questions"] == 2, result)
    check("t8/recall=0.5", result["recall_at_k"] == 0.5, result)
    # 问题一 rank1 (rr=1.0),问题二未命中 (rr=0) => mrr=0.5
    check("t8/mrr=0.5", result["mrr"] == 0.5, result)

    # 对比门禁
    good = {"mrr": 0.8, "recall_at_k": 0.8, "questions": 2}
    check("t8/指标下降拒绝", not E.compare(
        {"mrr": 0.6, "recall_at_k": 0.5, "questions": 2},
        good)["ok"])
    check("t8/指标持平放行", E.compare(
        {"mrr": 0.78, "recall_at_k": 0.75, "questions": 2},
        good)["ok"])
    check("t8/无基线直接放行", E.compare(
        {"mrr": 0.1, "questions": 2}, None)["ok"])


# ======================================================================
# T9 gc：保留策略
# ======================================================================

def t9_gc():
    # T5/T6/T7 已产生多个构建;执行 dry-run 检查保留集合
    result = R.gc(keep=3, dry_run=True)
    releases = R.load_releases()
    keep_ids = set(releases["history"][:3]) | {releases["current"]}
    removed = result["removed"]
    check("t9/gc不删保留版本", not any(k in keep_ids for k in removed),
          (removed, keep_ids))
    check("t9/gc只删builds下旧目录",
          all(os.path.isdir(os.path.join(P.BUILDS_DIR, k)) for k in removed))


# ======================================================================

def main():
    try:
        t1_manifest()
        t2_cas()
        t3_chunking()
        t4_faiss_reconstruct()
        base_bid = t5_full_build()
        t6_incremental(base_bid)
        t7_seed()
        t8_eval()
        t9_gc()
    finally:
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)

    fails = [n for n, ok in RESULTS if not ok]
    print("\n" + "=" * 60)
    print("总计 %d 项,通过 %d 项,失败 %d 项" %
          (len(RESULTS), len(RESULTS) - len(fails), len(fails)))
    if fails:
        print("失败项: %s" % ", ".join(fails))
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
