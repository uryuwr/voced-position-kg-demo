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
# 双保险：.dockerignore 已排除 .env，这里再删一次，防止在没有 .dockerignore 的
# 上下文里 build 时把配置真源打进镜像——它是 override=True 加载的，
# 进了镜像会盖掉 K8s/SDP 注入的环境变量，线上改配置将不生效。
RUN rm -f /app/backend/.env
# 只为挂 /schemas 静态本体文件；缺失时代码有 .exists() 守卫，可按需删掉此行
COPY schemas /app/schemas
# 运行时图数据可通过卷挂载 data/
# COPY data /app/data

EXPOSE 8088

CMD ["uvicorn", "backend.api.main:app", "--host", "0.0.0.0", "--port", "8088"]
