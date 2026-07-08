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

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(module)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时初始化 RAG 引擎"""
    logger.info("正在初始化 RAG 引擎...")
    try:
        from rag.generator import RAGPipeline
        pipeline = RAGPipeline()
        set_rag_pipeline(pipeline)
        logger.info("RAG 引擎初始化完成")
    except Exception as e:
        logger.error(f"RAG 引擎初始化失败: {e}")
        # 不阻止应用启动，/api/health 会报告 rag_initialized=False
    yield
    # 关闭 SQLite 连接
    conv_store.close()
    logger.info("应用关闭")


app = FastAPI(
    title="Marxist RAG API",
    description="马克思主义 RAG 知识库后端服务",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS 中间件 ──────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 路由 ─────────────────────────────────────────────────────
app.include_router(router)

# ── 静态文件服务 ─────────────────────────────────────────────
pages_dir = Path(_PROJECT_ROOT) / "marxist-rag-ui" / "pages"
if pages_dir.exists():
    app.mount("/", StaticFiles(directory=str(pages_dir), html=True), name="static")
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
