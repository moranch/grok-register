# Multi-Platform Account Registrar - Console Backend
# 基于 Python 3.12 + FastAPI + SQLite
# 对应 Requirement 12 AC2

FROM python:3.12-slim AS base

WORKDIR /app

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 数据目录
RUN mkdir -p /app/data

# 环境变量
ENV HOST=0.0.0.0
ENV PORT=8000
ENV DATABASE_PATH=/app/data/console.db
ENV DATABASE_URL=sqlite:////app/data/console.db
ENV PYTHONPATH=/app

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/api/health/ping || exit 1

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "api.main_app:app", "--host", "0.0.0.0", "--port", "8000"]
