# 独立部署后端 API（不含 crawlers / frontend）
# 上下文里只需要有 backend/ 一个子目录，仓库根其余目录一概不 COPY：
#   docker build -f backend/Dockerfile -t voced-kg-api .        # 在仓库根
#   docker build -f backend/Dockerfile -t voced-kg-api <只含 backend 的目录>
#
# ⚠ base 必须 3.13+：api/schemas_assessment.py 的联合类型里混了字符串前向引用
#   （`A | B | "C" | dict[...]`），3.12 的 pydantic 求值不了，import 期就 TypeError。
FROM python:3.14-slim

WORKDIR /app

# API_PORT 默认 **8080**：SDP 预生产集群暴露的就是 8080（Kong service 指向 8080），
# 本地开发才用 8088（直接 uvicorn 起，不走这个镜像）。
#
# ⚠ **绑定端口只能来自容器环境变量，不能来自 SDP 配置中心。** 配置中心是 Python
#   进程 import 期才由 sdp_config 拉下来的，而 uvicorn 的 --port 在那之前就定了。
#   在配置中心写 API_PORT 只会影响 openapi_servers() 里的 Swagger servers 列表，
#   **不会改变实际监听端口** —— 配置写 8080、镜像写死 8088 时，集群打 8080
#   直接连不上，且两边看起来都"配对了"。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    SERVE_DEV_UI=0 \
    API_HOST=0.0.0.0 \
    API_PORT=8080

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend
# 双保险：.dockerignore 已排除 .env，这里再删一次，防止在没有 .dockerignore 的
# 上下文里 build 时把配置真源打进镜像——它是 override=True 加载的，
# 进了镜像会盖掉 K8s/SDP 注入的环境变量，线上改配置将不生效。
# 顺带清掉宿主机带进来的 __pycache__：仓库根 .dockerignore 挡得住，但用「只含
# backend 的目录」当上下文时那份 ignore 不在，pyc 会跟着 COPY 进来
RUN rm -f /app/backend/.env \
 && find /app/backend -name __pycache__ -type d -prune -exec rm -rf {} +
# 仓库根的 schemas/ 与 data/ 都**不进镜像**——它们不在运行路径上（api/main.py 对
# /schemas、/seed 的挂载都有 .exists() 守卫，缺失只是少两条静态路由，接口一个不少）。
# 确实要对外暴露本体文件时用卷挂载：  -v $PWD/schemas:/app/schemas

EXPOSE 8080

# **shell form + exec**：原来是 exec form 写死 `--port 8088`，exec form 不做变量展开，
# 所以 K8s/SDP 注入的 API_PORT 一个字都不生效。加 `exec` 让 uvicorn 接管 PID 1，
# 否则 sh 挡在前面，SIGTERM 传不到进程，滚动更新时优雅退出（关连接池）不会执行。
CMD ["sh", "-c", "exec uvicorn backend.api.main:app --host \"${API_HOST:-0.0.0.0}\" --port \"${API_PORT:-8080}\""]
