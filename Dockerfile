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

# 默认启动 Web 服务
CMD ["uv", "run", "python", "-m", "api.main"]
