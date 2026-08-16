# -*- coding: utf-8 -*-
"""
语料分块 —— 与 rag/ingest_philosophy.py 相同的规则，重构为可复用纯函数

分块规则与 v1 完全一致（LangChain 标题切分 + 段落聚合 + 固定大小滑动），
保证新旧 chunk 的语义单元不变。与 v1 的区别只在：
  - 不再生成 uuid4，改由 kb.cas 按文本内容确定性生成
  - 支持"只处理指定的部分文件"（增量构建的入口）
"""

import logging
import os
import re
from typing import Dict, List, Optional

from langchain_text_splitters import MarkdownHeaderTextSplitter

from .cas import chunk_uuid

logger = logging.getLogger(__name__)

DUPLICATE_PATTERN = re.compile(r"\(1\)\.md$")

# ── 分块参数（与 v1 一致） ──────────────────────────────────────
PARAGRAPH_SEP = "\n\n"
MAX_CHUNK_SIZE = 800
FIXED_CHUNK_SIZE = 500
OVERLAP_RATIO = 0.2
MIN_CHUNK_LEN = 50


def chunking_params() -> Dict:
    """当前分块参数（写进 build.json 血缘，参数变化时禁止增量复用）"""
    return {
        "paragraph_sep": PARAGRAPH_SEP,
        "max_chunk_size": MAX_CHUNK_SIZE,
        "fixed_chunk_size": FIXED_CHUNK_SIZE,
        "overlap_ratio": OVERLAP_RATIO,
        "min_chunk_len": MIN_CHUNK_LEN,
    }


def split_file_to_chunks(content: str, filename: str) -> List[Dict]:
    """把单个文件的内容切分成 chunk 列表

    返回:
        [{"uuid": 确定性 chunk_id, "text": ..., "metadata": {...}}, ...]

    说明：title / chapter / category 的推导规则与
    rag/ingest_philosophy.py::parse_markdown_with_langchain 完全一致。
    """
    headers_to_split_on = [
        ("#", "Header 1"),
        ("##", "Header 2"),
    ]
    markdown_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=headers_to_split_on)
    md_header_splits = markdown_splitter.split_text(content)

    overlap = int(FIXED_CHUNK_SIZE * OVERLAP_RATIO)
    chunks: List[Dict] = []

    for split in md_header_splits:
        text = split.page_content.strip()
        if not text:
            continue

        metadata = split.metadata
        h1 = metadata.get("Header 1", "未分类")
        h2 = metadata.get("Header 2", "")

        chapter = f"{h1}>{h2}" if h2 else h1
        title = h2 if h2 else (h1 if h1 != "未分类" else filename.replace(".md", ""))

        paragraphs = [p.strip() for p in text.split(PARAGRAPH_SEP) if p.strip()]

        for para in paragraphs:
            if len(para) < MIN_CHUNK_LEN:
                continue

            if len(para) <= MAX_CHUNK_SIZE:
                chunks.append(_make_chunk(para, title, chapter, h1, filename))
            else:
                start = 0
                while start < len(para):
                    end = start + FIXED_CHUNK_SIZE
                    sub_text = para[start:end].strip()
                    if len(sub_text) >= MIN_CHUNK_LEN:
                        chunks.append(_make_chunk(
                            sub_text, title, chapter, h1, filename))
                    start += FIXED_CHUNK_SIZE - overlap

    return chunks


def _make_chunk(text: str, title: str, chapter: str,
                category: str, source: str) -> Dict:
    return {
        "uuid": chunk_uuid(text),
        "text": text,
        "metadata": {
            "title": title,
            "chapter": chapter,
            "category": category,
            "source": source,
        },
    }


def process_files(ww_dir: str,
                  filenames: Optional[List[str]] = None) -> List[Dict]:
    """扫描语料目录并分块

    参数:
        filenames: 只处理这些文件（增量构建）。None 表示处理全部。

    返回: 所有 chunk 的扁平列表（文件顺序固定，保证可复现）。
    """
    if not os.path.isdir(ww_dir):
        raise FileNotFoundError(f"语料目录不存在: {ww_dir}")

    if filenames is None:
        md_files = sorted(
            f for f in os.listdir(ww_dir)
            if f.endswith(".md") and not DUPLICATE_PATTERN.search(f)
        )
    else:
        md_files = [f for f in filenames
                    if f.endswith(".md") and not DUPLICATE_PATTERN.search(f)]

    all_chunks: List[Dict] = []
    for filename in md_files:
        file_path = os.path.join(ww_dir, filename)
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            logger.error("读取文件失败，跳过: %s - %s", filename, e)
            continue
        chunks = split_file_to_chunks(content, filename)
        logger.info("  %s -> %d 个片段", filename, len(chunks))
        all_chunks.extend(chunks)
    return all_chunks
