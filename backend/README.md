# backend · 独立可部署 API 服务

**本目录是对外交付的后端工程。**  
正式环境只部署 API；**不包含爬虫、不依赖正式前端。**  
仓库里的 `frontend/` 仅供你本地自测页面，业务前端通过 HTTP 调 `/v1/**`。

## 职责边界

| 模块 | 是否本服务 |
| --- | --- |
| 知识图谱查询 API | ✅ |
| 业务 API（诊断/路径等，见 `docs/服务端数据与接口设计.md`） | ✅（规划/实现中） |
| 用户登录/注册 | ❌ 用户中心；本服务只认 Token，冗余 `user_id`/`user_name` |
| 数据采集/爬虫 | ❌ 见 `crawlers/`（离线跑，不进 SDP 镜像） |
| 正式前端 UI | ❌ 外部前端工程 |

## 结构

```
backend/
  api/main.py          # FastAPI 入口
  kg/                  # 图存储与查询
  requirements.txt     # 独立依赖
  Dockerfile           # 独立镜像
  README.md
```

## 本地运行

在**仓库根目录**：

```bash
pip install -r backend/requirements.txt

# PostgreSQL（图读路径）
docker run -d --name voced-pg \
  -e POSTGRES_USER=voced -e POSTGRES_PASSWORD=<your-password> -e POSTGRES_DB=voced_kg \
  -p 5432:5432 postgres:16-alpine

# 从 SQLite 灌入（默认仅 CN）
python -m backend.kg.pg_store.migrate --clear --region CN

# 纯 API（对接方/SDP 同款）
set SERVE_DEV_UI=0
set DATABASE_URL=postgresql://voced:<your-password>@localhost:5432/voced_kg
python -m uvicorn backend.api.main:app --host 0.0.0.0 --port 8088
```

| 地址 | 说明 |
| --- | --- |
| `GET /` | 服务发现 JSON（含本机/局域网 docs 地址） |
| `GET /health` | 健康检查（含 PG） |
| `GET /docs` | **Swagger UI**（类 Java Swagger，可试调；局域网 IP 可开） |
| `GET /redoc` | ReDoc 阅读文档 |
| `GET /openapi.json` | OpenAPI 3 JSON（可导入 Apifox/Postman） |
| `GET /api-guide` | 简要对接说明 HTML |
| `GET /v1/**` | 业务与图谱 API |

### 临时身份请求头（未接公司 UC）

业务接口（`/v1/**`）**必填**请求头：

| Header | 说明 |
| --- | --- |
| `user-id` | 用户 ID（前端传入） |
| `user-name` | 用户显示名 |

Swagger 右上角 **Authorize** 填写后全局生效。接入 UC 后改为服务端鉴权，将废弃前端传 uid。

**文档会随代码自动更新**：改 Pydantic 模型 / 路由注解 / `summary`·`description` 后刷新 `/docs` 即可，勿另维护过时 Word。

## 本地自测页（可选，非部署物）

```bash
# PowerShell
$env:SERVE_DEV_UI="1"
python -m uvicorn backend.api.main:app --host 0.0.0.0 --port 8088
```

| 地址 | 说明 |
| --- | --- |
| `/admin` · `/admin-console` | **管理端控制台**（看板/四维/建数/审核） |
| `/admin-cytoscape` | 图谱探索自测 |
| `/dev` | 规划工作台壳 |
| `/cn-sources` | 来源目录页 |

## Docker / SDP

```bash
# 在仓库根
docker build -f backend/Dockerfile -t voced-kg-api .
docker run --rm -p 8088:8088 ^
  -e SERVE_DEV_UI=0 ^
  -e CORS_ORIGINS=https://your-frontend.example.com ^
  -e DATABASE_URL=postgresql://user:pass@host:5432/voced_kg ^
  -e KG_REGION=CN ^
  voced-kg-api
```

SDP：只发布 **backend 镜像 + 环境变量 + PG 连接**；不要打 crawlers、不要挂 frontend。

## 配置（独立部署）

配置文件在 **本目录**，不依赖仓库根：

```bash
cp backend/.env.example backend/.env   # 编辑后启动
# 或 K8s/Compose 用环境变量注入，可不挂 .env 文件
```

| 文件 | 说明 |
| --- | --- |
| `backend/.env.example` | 模板（可提交） |
| `backend/.env` | 本地/环境真源（**勿提交**） |
| `backend/settings.py` | 读取顺序：环境变量 → 可选仓库根 `.env` 兜底 → **`backend/.env` 覆盖** |

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SERVE_DEV_UI` | `0` | `1` 时托管 `frontend/` 自测页 |
| `CORS_ORIGINS` | `*` | 正式请改成业务前端域名，逗号分隔 |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8088` | 监听 |
| `DATABASE_URL` | 见 `kg/pg_store/config.py` | PostgreSQL 连接串 |
| `KG_REGION` | `CN` | 默认查询区域；`region=all` 可放开 |
| `AUTH_BYPASS` | `0` | `1` 跳过 UC；生产必须 `0` |
| `REVIEW_REQUIRED` | `0` | `0` 无审核直写；`1` 进待审 |
| `UC_API_HOST` 等 | 见 `.env.example` | 前端 UC；`GET /v1/config` 下发 |

改 `backend/.env` 后**重启** API 进程。

## 对接文档

- 接口与表结构：`docs/服务端数据与接口设计.md`
- 能力差距：`docs/前后台能力对照与数据建模缺口.md`
- 在线契约：部署后打开 `/docs`
