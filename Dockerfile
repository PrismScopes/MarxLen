FROM python:3.12-slim

WORKDIR /app/rag

# 确保 /app 在 Python 模块搜索路径中
ENV PYTHONPATH=/app

# 只安装 pyproject.toml 中列出的依赖
COPY rag/pyproject.toml rag/uv.lock ./
RUN pip install --no-cache-dir uv && \
    uv sync --no-dev --frozen --no-install-project

# 复制项目代码（ww/ 在 .dockerignore 中排除，不会被打包进去）
COPY rag/ ./
COPY api/ ../api/
COPY marxist-rag-ui/ ../marxist-rag-ui/
# 提示词目录必须一起打包：query_planner 从 /app/Prompt 加载 main_prompt 与
# RAG_prompt，缺失时前置分析会静默降级为单查询检索
COPY Prompt/ ../Prompt/

# 默认启动 Web 服务（生产模式，不开热重载）
CMD ["uv", "run", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
