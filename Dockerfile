FROM python:3.12-slim

WORKDIR /app/rag

# 只安装 pyproject.toml 中列出的依赖（排除 sentence-transformers、chromadb 等未使用的包）
COPY rag/pyproject.toml rag/uv.lock ./
RUN pip install --no-cache-dir uv && \
    uv sync --no-dev --frozen --no-install-project

# 复制项目代码
COPY rag/ ./
COPY api/ ../api/
COPY marxist-rag-ui/ ../marxist-rag-ui/
COPY ww/ ../ww/

# 确保 rag 目录作为 Python 包可导入
RUN touch __init__.py

# 默认启动 Web 服务
CMD ["uv", "run", "python", "-m", "api.main"]
