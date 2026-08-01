"""
联网搜索模块
支持多个搜索引擎后端（DuckDuckGo / 预留其他）
"""
import logging
from typing import List, Dict, Optional

from .config_store import get_config

logger = logging.getLogger(__name__)

# 尝试导入搜索引擎
_web_search_available = False

try:
    from ddgs import DDGS
    _web_search_available = True
    logger.info("联网搜索: ddgs 就绪")
except ImportError:
    try:
        from duckduckgo_search import DDGS
        _web_search_available = True
        logger.info("联网搜索: DuckDuckGo 就绪")
    except ImportError:
        logger.warning("联网搜索: 未安装搜索引擎库 (pip install ddgs)")


def web_search(query: str, max_results: Optional[int] = None) -> List[Dict]:
    """执行联网搜索。max_results 不传则取设置项 web_search_results"""
    if not _web_search_available:
        logger.warning("联网搜索: 无可用搜索引擎")
        return []

    if max_results is None:
        max_results = int(get_config().get("web_search_results"))

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        formatted = []
        for r in results:
            formatted.append({
                "title": r.get("title", ""),
                "body": r.get("body", ""),
                "href": r.get("href", ""),
            })
        logger.info(f"联网搜索 '{query[:30]}...' 返回 {len(formatted)} 条结果")
        return formatted
    except Exception as e:
        logger.warning(f"联网搜索失败: {e}")
        return []


def format_search_results(results: List[Dict]) -> str:
    """将搜索结果格式化为文本"""
    if not results:
        return ""
    excerpt_len = int(get_config().get("web_search_excerpt"))
    lines = ["【联网搜索结果】："]
    for i, r in enumerate(results):
        title = r.get("title", "")
        body = r.get("body", "")
        href = r.get("href", "")
        lines.append(f"  [网络 {i+1}] {title}")
        if body:
            lines.append(f"    摘要: {body[:excerpt_len]}")
        if href:
            lines.append(f"    链接: {href}")
    return "\n".join(lines)
