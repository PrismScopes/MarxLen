"""原文阅读器

向量库里的 chunk 是给检索用的碎片：同一文件的 id 顺序与原文阅读顺序无关，
相邻块之间也没有重叠，靠它拼不出连续正文。所以阅读器的正文直接读原始
Markdown（ww/ 目录），只把向量库当作"跳转锚点"的来源。

定位方式：拿 chunk 文本前 N 字去原文里找。分块时并未改写字符，只是剥掉
标题行并对段落做了 strip，因此差异全部集中在空白字符上——去掉两侧所有
空白后做精确匹配，实测 200 条样本可定位率 99.5%。
"""

import os
import re
import logging
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from .models import ReaderSearchRequest
from rag.config_store import get_config
from rag.query_planner import extract_search_keywords

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reader")
config = get_config()

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_WS = re.compile(r"\s+")
_HEADER = re.compile(r"^(#{1,4})\s+(.+?)\s*$")

# 解析一次 11MB 的原文要几百毫秒，翻页时每次重做不可接受。
# 缓存已解析的文件，按 LRU 淘汰——正文本身就占内存，条数不能开大。
_CACHE_MAX_FILES = 3
_cache: Dict[str, Dict[str, Any]] = {}
_cache_order: List[str] = []
_cache_lock = threading.Lock()


# ======================================================================
# 语料目录与文件访问
# ======================================================================

def corpus_dir() -> Optional[str]:
    """原文目录的绝对路径；目录不存在时返回 None"""
    raw = (config.get("reader_corpus_dir") or "ww").strip()
    path = raw if os.path.isabs(raw) else os.path.join(_PROJECT_ROOT, raw)
    return path if os.path.isdir(path) else None


def _require_corpus() -> str:
    if not config.get("reader_enabled"):
        raise HTTPException(status_code=503, detail="原文阅读器已在设置中关闭")
    path = corpus_dir()
    if path is None:
        raise HTTPException(
            status_code=503,
            detail="原文目录不存在。容器部署需将原文目录挂载进容器，或在设置中修改路径",
        )
    return path


def _resolve(source: str) -> str:
    """把 source 参数解析成语料目录下的真实文件路径

    source 来自 URL，必须先剥成纯文件名再拼接，否则 ../ 可以读到
    语料目录之外的任意文件。
    """
    base = _require_corpus()
    name = os.path.basename((source or "").strip())
    if not name or not name.endswith(".md"):
        raise HTTPException(status_code=400, detail="source 必须是 .md 文件名")

    path = os.path.join(base, name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"原文不存在: {name}")
    return path


# ======================================================================
# 解析与缓存
# ======================================================================

def _parse(path: str) -> Dict[str, Any]:
    """解析原文，返回段落列表、标题目录与用于定位的无空白全文

    errors="replace" 是必要的：语料由 OCR 产出，个别文件含无法解码的字节，
    整份读失败会让这本书彻底打不开。
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    paragraphs: List[Dict[str, Any]] = []
    toc: List[Dict[str, Any]] = []
    seq = 0
    cursor = 0

    for block in content.split("\n\n"):
        # 用游标顺序推进而不是 content.find(block)：后者每次从头扫描是
        # O(n²)，且遇到重复段落会返回第一次出现的位置，偏移就错了
        block_start = cursor
        cursor += len(block) + 2  # +2 是被 split 吃掉的 "\n\n"

        stripped_left = len(block) - len(block.lstrip())
        text = block.strip()
        if not text:
            continue

        char_start = block_start + stripped_left

        first = text.split("\n", 1)[0].strip()
        m = _HEADER.match(first)
        if m:
            toc.append({
                "level": len(m.group(1)),
                "text": m.group(2).strip(),
                "seq": seq,
                "char_start": char_start,
            })

        paragraphs.append({
            "seq": seq,
            "char_start": char_start,
            "text": text,
            "heading": bool(m),
        })
        seq += 1

    # 段落级的无空白索引：定位时先在全文找到无空白偏移，
    # 再用每段的累计长度换算回段落序号
    ws_offsets: List[int] = []
    acc = 0
    parts = []
    for p in paragraphs:
        ws_offsets.append(acc)
        stripped = _WS.sub("", p["text"])
        parts.append(stripped)
        acc += len(stripped)

    return {
        "paragraphs": paragraphs,
        "toc": toc,
        "total_chars": len(content),
        "ws_full": "".join(parts),
        "ws_offsets": ws_offsets,
    }


def _get_parsed(path: str) -> Dict[str, Any]:
    """取解析结果，命中缓存则直接返回并刷新 LRU 位置

    命中还要比对 mtime 与 size：原文是人工维护的，运行期间被编辑过
    （例如删改敏感内容）而缓存不失效的话，阅读器会一直吐旧正文，
    且定位偏移全部错位，只能靠重启服务恢复。
    """
    key = os.path.abspath(path)
    try:
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None

    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and cached.get("stamp") == stamp:
            _cache_order.remove(key)
            _cache_order.append(key)
            return cached

    # 解析放在锁外：单文件可达 11MB，持锁解析会把并发请求全堵住
    parsed = _parse(path)
    parsed["stamp"] = stamp

    with _cache_lock:
        _cache[key] = parsed
        if key in _cache_order:
            _cache_order.remove(key)
        _cache_order.append(key)
        while len(_cache_order) > _CACHE_MAX_FILES:
            _cache.pop(_cache_order.pop(0), None)
    return parsed


def clear_cache() -> None:
    """清空正文缓存。原文目录变更后需调用"""
    with _cache_lock:
        _cache.clear()
        _cache_order.clear()


# ======================================================================
# 定位
# ======================================================================

def _locate_text(parsed: Dict[str, Any], text: str) -> Dict[str, Any]:
    """在已解析的原文中定位一段文本

    返回 matched / seq / char_start / ambiguous。
    matched=False 表示只能定位到文件，前端应打开原文但不高亮。
    """
    probe_len = int(config.get("reader_probe_len"))
    probe = _WS.sub("", text or "")[:probe_len]
    if len(probe) < 10:
        return {"matched": False, "seq": -1, "char_start": 0, "ambiguous": False}

    ws_full = parsed["ws_full"]
    pos = ws_full.find(probe)
    if pos < 0:
        return {"matched": False, "seq": -1, "char_start": 0, "ambiguous": False}

    # 命中多处时仍取第一处，但要告诉前端这次定位不确定
    ambiguous = ws_full.find(probe, pos + 1) >= 0

    # 无空白偏移换算回段落序号：找最后一个起点不超过 pos 的段落
    offsets = parsed["ws_offsets"]
    lo, hi = 0, len(offsets) - 1
    idx = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if offsets[mid] <= pos:
            idx, lo = mid, mid + 1
        else:
            hi = mid - 1

    para = parsed["paragraphs"][idx]
    return {
        "matched": True,
        "seq": para["seq"],
        "char_start": para["char_start"],
        "ambiguous": ambiguous,
    }


def _chunk_text_by_uuid(doc_uuid: str) -> Optional[Dict[str, str]]:
    """按 uuid 取回 chunk 的文本与所属文件

    复用 RAG 引擎已建立的 SQLite 连接（建连时已设 check_same_thread=False），
    避免为阅读器再开一条连接。
    """
    from api.routes import rag_pipeline

    if rag_pipeline is None:
        raise HTTPException(status_code=503, detail="RAG 引擎尚未初始化")

    row = rag_pipeline.retriever.store.query(
        "SELECT text, source, chapter FROM documents WHERE uuid = ?", (doc_uuid,)
    )
    if not row:
        return None
    row = row[0]
    return {"text": row[0], "source": row[1], "chapter": row[2]}


# ======================================================================
# 接口
# ======================================================================

@router.get("/books")
async def list_books():
    """书目列表。以原文目录的实际文件为准"""
    base = _require_corpus()
    books = []
    for name in sorted(os.listdir(base)):
        if not name.endswith(".md"):
            continue
        full = os.path.join(base, name)
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        books.append({
            "source": name,
            "title": name[:-3],
            "size_kb": round(size / 1024, 1),
        })
    return {"total": len(books), "books": books}


@router.get("/toc")
async def get_toc(source: str = Query(..., description="原文文件名")):
    """标题目录"""
    parsed = _get_parsed(_resolve(source))
    return {
        "source": os.path.basename(source),
        "total_chars": parsed["total_chars"],
        "total_paragraphs": len(parsed["paragraphs"]),
        "toc": parsed["toc"],
    }


@router.get("/content")
async def get_content(
    source: str = Query(..., description="原文文件名"),
    seq: int = Query(0, ge=0, description="起始段落序号"),
    limit: Optional[int] = Query(None, description="本次返回的字数上限"),
):
    """按段落取正文

    以段落序号翻页而不是字符偏移：字符偏移会把段落切断，前端还得
    自己拼；按段落取则每次返回的都是完整段落。
    """
    parsed = _get_parsed(_resolve(source))
    paragraphs = parsed["paragraphs"]

    max_chars = int(config.get("reader_page_chars")) if limit is None else max(500, int(limit))

    out = []
    used = 0
    for p in paragraphs[seq:]:
        # 至少给一段，避免单段超长时返回空列表导致前端翻页卡死
        if out and used + len(p["text"]) > max_chars:
            break
        out.append(p)
        used += len(p["text"])

    next_seq = seq + len(out)
    return {
        "source": os.path.basename(source),
        "seq": seq,
        "next_seq": next_seq,
        "eof": next_seq >= len(paragraphs),
        "total_paragraphs": len(paragraphs),
        "paragraphs": out,
    }


@router.get("/locate")
async def locate(doc_uuid: str = Query(..., description="片段 uuid")):
    """把检索片段定位到原文位置，供来源卡片跳转使用"""
    _require_corpus()

    chunk = _chunk_text_by_uuid(doc_uuid)
    if chunk is None:
        raise HTTPException(status_code=404, detail="片段不存在，可能索引与数据库不一致")

    source = os.path.basename(chunk["source"] or "")
    if not source:
        raise HTTPException(status_code=404, detail="片段未记录来源文件")

    path = _resolve(source)
    parsed = _get_parsed(path)
    result = _locate_text(parsed, chunk["text"])

    if not result["matched"]:
        # 定位失败时退到章节标题，至少不让用户落在文首
        chapter = (chunk["chapter"] or "").split(">")[-1].strip()
        if chapter:
            for entry in parsed["toc"]:
                if entry["text"] == chapter:
                    result = {**result, "seq": entry["seq"],
                              "char_start": entry["char_start"], "fallback": "chapter"}
                    break

    return {
        "source": source,
        "title": source[:-3],
        "total_paragraphs": len(parsed["paragraphs"]),
        **result,
    }


@router.post("/search")
async def search(req: ReaderSearchRequest):
    """在原文中做模糊搜索

    用户给的是"我记得有一段讲……"这类描述，原文里并不存在这串字，
    所以不能做字面查找。流程是：
      LLM 把描述转成经典术语与一句仿原文的命题
        -> 命题走向量通道、术语走 BM25 通道
        -> RRF 融合
        -> 每条结果顺带算出在原文中的位置，前端可直接跳转

    LLM 不可用时退回"直接拿原始描述检索"，结果差一些但不中断。
    """
    _require_corpus()

    from api.routes import rag_pipeline
    if rag_pipeline is None:
        raise HTTPException(status_code=503, detail="RAG 引擎尚未初始化")

    query = (req.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="搜索内容不能为空")

    # 限定在当前书时，先确认这本书确实存在，避免白跑一次检索
    only_source = None
    if req.scope == "current":
        if not req.source:
            raise HTTPException(status_code=400, detail="scope=current 时必须指定 source")
        only_source = os.path.basename(_resolve(req.source))

    top_k = int(config.get("reader_search_top_k"))
    max_kw = int(config.get("reader_search_keywords"))

    # 关键词抽取同样只是结构化抽取，用快模型（见 planner_model 的说明）
    planner_model = (config.get("planner_model") or "").strip()
    extracted = extract_search_keywords(
        sdk_client=rag_pipeline.sdk_client,
        model_name=planner_model or rag_pipeline.model_name,
        query=query,
        max_keywords=max_kw,
    )

    retriever = rag_pipeline.retriever
    dense_query = extracted["proposition"] or query
    sparse_query = " ".join(extracted["keywords"]) or query

    # 限定单本书时要多召回一些，否则按 source 过滤完可能所剩无几
    fetch_k = top_k * (8 if only_source else 3)

    # 单通道失败不该让整次搜索失败：任一路有结果就还能用
    dense_res, sparse_res = [], []
    try:
        dense_res = retriever.dense_search(dense_query, k=fetch_k)
    except Exception as e:
        logger.warning(f"模糊搜索向量通道失败: {e}")
    try:
        sparse_res = retriever.sparse_search(sparse_query, k=fetch_k)
    except Exception as e:
        logger.warning(f"模糊搜索关键词通道失败: {e}")

    if not dense_res and not sparse_res:
        raise HTTPException(status_code=503, detail="检索服务暂时不可用，请稍后重试")

    merged = retriever.rrf_combine(dense_res, sparse_res)

    results = []
    for doc in merged:
        meta = doc.get("metadata", {})
        doc_source = os.path.basename(meta.get("source", "") or "")
        if not doc_source:
            continue
        if only_source and doc_source != only_source:
            continue

        # 顺带把位置算出来：前端拿到就能跳，不必再逐条调 locate
        seq, matched = -1, False
        try:
            parsed = _get_parsed(_resolve(doc_source))
            loc = _locate_text(parsed, doc.get("text", ""))
            seq, matched = loc["seq"], loc["matched"]
        except HTTPException:
            # 库里有记录但 ww/ 下没有这个文件，跳过位置计算仍返回该结果
            pass

        results.append({
            "doc_uuid": doc.get("uuid", ""),
            "source": doc_source,
            "title": doc_source[:-3],
            "chapter": meta.get("chapter", ""),
            "excerpt": (doc.get("text", "") or "")[:200],
            "score": round(float(doc.get("rrf_score", 0.0)), 6),
            "seq": seq,
            "matched": matched,
        })
        if len(results) >= top_k:
            break

    return {
        "query": query,
        "keywords": extracted["keywords"],
        "proposition": extracted["proposition"],
        "llm_ok": extracted["llm_ok"],
        "scope": req.scope,
        "total": len(results),
        "results": results,
    }
