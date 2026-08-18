import os
import sys
import logging

# 将项目根目录加入 sys.path，使 rag/ 包可导入
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routes import router, set_rag_pipeline, conv_store
from api.reader import router as reader_router
from rag.config_store import get_config

config = get_config()

# ── 日志:统一配置(全项目唯一入口) ─────────────────────────
# 各业务模块内部不再调用 basicConfig(否则 import 顺序会反复重置
# 根 logger 配置)。request_id 由 rag/telemetry.py 的过滤器注入。
logging.basicConfig(
    level=getattr(logging, config.get("log_level"), logging.INFO),
    format="[%(asctime)s] [%(levelname)s] [%(module)s] "
           "[rid=%(request_id)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
try:
    from rag.telemetry import RequestIdFilter
    for handler in logging.getLogger().handlers:
        handler.addFilter(RequestIdFilter())
except Exception:
    pass
# 第三方库降噪
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时初始化 RAG 引擎与知识库热更新监听"""
    logger.info("正在初始化 RAG 引擎...")
    pipeline = None
    kb_watcher = None
    try:
        # 版本化知识库:解析发布指针(data/releases.json)统一决定
        # 索引目录与版本号——这是知识库的单一入口,retriever 不再自己
        # 决定回退。无发布记录(裸装未登记)时 index_dir 为 None,
        # 回退 rag/ 传统目录,但会在日志里明确提示登记基线;
        # 指针损坏则保留回退并告警,不让服务起不来。
        index_dir = None
        kb_build_id = None
        if config.get("kb_enabled"):
            try:
                from kb.release import resolve_index_dir
                index_dir, kb_build_id = resolve_index_dir()
                if index_dir is None or kb_build_id is None:
                    logger.warning(
                        "知识库尚未登记基线(无 data/releases.json 或指针为空)。"
                        "正在使用 rag/ 传统目录,建议运行 kb seed 登记基线,"
                        "以获得版本化更新能力")
                else:
                    logger.info("知识库版本: %s (index_dir=%s)",
                                kb_build_id, index_dir)
            except Exception as e:
                logger.warning(f"知识库版本解析失败,回退传统目录: {e}")

        from rag.generator import RAGPipeline
        pipeline = RAGPipeline(index_dir=index_dir, kb_build_id=kb_build_id)
        set_rag_pipeline(pipeline)
        logger.info("RAG 引擎初始化完成")

        # 发布指针变化时,后台加载新索引并原子切换,无需重启服务
        if config.get("kb_hot_reload"):
            try:
                from kb.watcher import KBVersionWatcher
                kb_watcher = KBVersionWatcher(pipeline)
                kb_watcher.start()
            except Exception as e:
                logger.warning(f"知识库热更新监听启动失败: {e}")
    except Exception as e:
        logger.error(f"RAG 引擎初始化失败: {e}")
        # 不阻止应用启动，/api/health 会报告 rag_initialized=False

    # 原文目录缺失只影响阅读器，不该拖垮整个服务，
    # 但必须在启动时就说清楚，否则用户点了跳转才发现打不开
    if config.get("reader_enabled"):
        from api.reader import corpus_dir
        path = corpus_dir()
        if path:
            logger.info(f"原文阅读器已启用，语料目录: {path}")
        else:
            logger.warning(
                "原文目录不存在，阅读器不可用。"
                "容器部署需挂载原文目录，或在设置中修改「原文目录」"
            )
    yield
    # 关闭时释放所有持久化连接
    if kb_watcher is not None:
        kb_watcher.stop()
    conv_store.close()
    if pipeline is not None:
        try:
            pipeline.answer_cache.close()
            pipeline.retriever.embed_cache.close()
            pipeline.retriever.store.close()
        except Exception as e:
            logger.warning(f"关闭 RAG 资源时出错: {e}")
    logger.info("应用关闭")


app = FastAPI(
    title="Marxist RAG API",
    description="马克思主义 RAG 知识库后端服务",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS 中间件 ──────────────────────────────────────────────
# 前后端同源部署，默认只放行本机来源。
# 需要从其他域访问时，在设置页配置"允许的跨域来源"（逗号分隔）。
_cors_origins = config.get("cors_origins") or ""
if _cors_origins.strip():
    allow_origins = [o.strip() for o in _cors_origins.split(",") if o.strip()]
else:
    allow_origins = [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 路由 ─────────────────────────────────────────────────────
app.include_router(router)
app.include_router(reader_router)

# ── 静态文件服务 ─────────────────────────────────────────────
# 注意挂载顺序：根路径 "/" 的挂载会吞掉所有未匹配请求，必须放在最后。
_UI_ROOT = Path(_PROJECT_ROOT) / "marxist-rag-ui"


class NoCacheStaticFiles(StaticFiles):
    """禁用缓存的静态文件服务。

    前端已拆成多个 ES module，而 import 语句里不方便挂版本号，
    浏览器一旦缓存了子模块，改完代码刷新也看不到效果。
    这里统一让 assets 目录下的资源不进缓存。

    如需上线，可改为按内容哈希命名文件并放开长缓存。
    """

    def is_not_modified(self, response_headers, request_headers) -> bool:
        # 始终返回 False，强制重新下载而不是回 304
        return False

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


assets_dir = _UI_ROOT / "assets"
if assets_dir.exists():
    app.mount("/assets", NoCacheStaticFiles(directory=str(assets_dir)), name="assets")
    logger.info(f"静态资源目录挂载: {assets_dir}")
else:
    logger.warning(f"静态资源目录不存在: {assets_dir}")

pages_dir = _UI_ROOT / "pages"
if pages_dir.exists():
    app.mount("/", NoCacheStaticFiles(directory=str(pages_dir), html=True), name="static")
    logger.info(f"静态文件目录挂载: {pages_dir}")
else:
    logger.warning(f"静态文件目录不存在: {pages_dir}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_excludes=[
            "rag/faiss_index.idx",
            "rag/bm25_index.pkl",
            "rag/documents.db",
            "rag/cache_*.db",
            "rag/.venv/*",
        ],
    )
