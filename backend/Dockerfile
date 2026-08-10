# 独立部署后端 API（不含 crawlers / frontend）
# build 上下文：仓库根目录
#   docker build -f backend/Dockerfile -t voced-kg-api .
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    SERVE_DEV_UI=0 \
    API_HOST=0.0.0.0 \
    API_PORT=8088

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend
COPY schemas /app/schemas
# 运行时图数据可通过卷挂载 data/
# COPY data /app/data

EXPOSE 8088

CMD ["uvicorn", "backend.api.main:app", "--host", "0.0.0.0", "--port", "8088"]
