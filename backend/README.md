# backend · 独立可部署 API 服务

**本目录是对外交付的唯一后端工程。**
正式环境只部署它；**不包含爬虫、不依赖正式前端、不读仓库根任何文件。**
仓库里的 `frontend/` 仅供本地自测，业务前端通过 HTTP 调 `/v1/**`。

## 职责边界

| 模块 | 是否本服务 |
| --- | --- |
| 知识图谱查询 API（四维图谱 / 图探索 / 管理台） | ✅ |
| 学员端业务（AI 诊断、测评、学习路径、画像） | ✅ |
| 用户登录/注册 | ❌ 用户中心（UC）；本服务只校验 Token 并冗余 `user_id`/`user_name` |
| 数据采集/爬虫 | ❌ 见 `crawlers/`（离线跑，不进镜像） |
| 正式前端 UI | ❌ 外部前端工程 |

## 结构

```
backend/
  api/            # FastAPI 路由 + Pydantic 契约（main.py 为入口）
  kg/pg_store/    # SQL 与业务规则，代码量集中在这里
  agent/          # LLM 诊断与测评（每处调模型都有规则兜底）
  uc/             # UC MAC Token 校验
  bts/            # 服务间鉴权（本服务 → 外部接口）
  userprofile/    # 用户画像（五维记忆）对接
  settings.py     # 集中读配置
  sdp_config.py   # SDP 配置中心客户端（启动时拉配置注入环境变量）
  requirements.txt
  Dockerfile
```

分层方向：`api/` → `kg/pg_store/` → `agent/`。图数据在 `kg_node`/`kg_edge`，运行时业务数据在 `biz_*`。
表结构靠启动时 `ensure_*_schema()` 的幂等 DDL 演进，无 migration 框架。

## 配置

**业务配置的真源是 SDP 配置中心**（颗粒 `voced-position-kg`），不是 `.env`。

启动时若进程环境里有 `SDP_CLOUD_CONFIG_SECRET`，`sdp_config.py` 会把该 profile 下的业务键拉下来注入 `os.environ`；没有 secret 就完全走本地 `.env` / 环境变量。**拉不到不拦启动**，降级继续跑。

```bash
cp backend/.env.example backend/.env    # 填 SDP_CLOUD_CONFIG_SECRET 即可，业务键不用填
```

加载顺序（后者覆盖前者）：

1. 进程环境变量
2. 仓库根 `.env`（仅 monorepo 本地兼容，不覆盖已有键）
3. `backend/.env`
4. **SDP 配置中心**（有 secret 才拉）

**例外**：`backend/.env` 里**显式写出**的键，远端不覆盖 —— 本地调试用。线上镜像没有 `.env`（Dockerfile 已 `rm` + `.dockerignore` 双保险），保护名单为空，配置中心说了算。

排查「配置改了没生效」看 `GET /health` 的 `config` 字段：

```json
{"source": "remote", "keys": 26, "profile": "development",
 "local_override": ["SERVE_DEV_UI"]}
```

`source=local` 说明没吃到配置中心；`local_override` 列出被本地 `.env` 挡下的键。

**无热更新**：改完配置中心必须重启进程。

| 常用变量 | 默认 | 说明 |
| --- | --- | --- |
| `SDP_CLOUD_CONFIG_SECRET` | 空 | 配置中心颗粒账密；**唯一必需的 bootstrap 键**，生产由 SDP 注入 |
| `SDP_ENV_NAME` / `APP_ENV` | `development` | 拉哪套 profile |
| `SERVE_DEV_UI` | `0` | `1` 时托管 `frontend/` 自测页（镜像里没有 frontend，线上恒 0） |
| `CORS_ORIGINS` | `*` | 正式改成业务前端域名，逗号分隔 |
| `DATABASE_URL` | 见 `kg/pg_store/config.py` | PostgreSQL 连接串 |
| `KG_REGION` | `CN` | 默认查询区域；`region=all` 放开 |
| `AUTH_BYPASS` | `0` | `1` 跳过 UC 校验；**生产必须 0** |
| `REVIEW_REQUIRED` | `0` | `0` 无审核直写；`1` 进待审队列 |
| `LLM_BASE_URL` / `LLM_MODEL` / `GITHUB_TOKEN` | 空 | AI 网关；未配齐时诊断/测评走规则降级，不影响启动 |
| `UC_API_HOST` 等 | 见 `.env.example` | 前端 UC，`GET /v1/config` 下发 |

## 本地运行

```bash
pip install -r backend/requirements.txt

# PostgreSQL
docker run -d --name voced-pg \
  -e POSTGRES_USER=voced -e POSTGRES_PASSWORD=<your-password> -e POSTGRES_DB=voced_kg \
  -p 5432:5432 postgres:16-alpine

# 从 SQLite 灌图数据（默认仅 CN）
python -m backend.kg.pg_store.migrate --clear --region CN

python -m uvicorn backend.api.main:app --host 0.0.0.0 --port 8088
```

要看自测页，在 `backend/.env` 里写 `SERVE_DEV_UI=1`（命令行传环境变量无效——`.env` 是 `override=True` 加载的）。

| 地址 | 说明 |
| --- | --- |
| `GET /` | 服务发现 JSON（含本机/局域网 docs 地址） |
| `GET /health` | 健康检查（PG 连通性 + AI 网关就绪态 + **配置来源**） |
| `GET /docs` | **Swagger UI**（可试调） |
| `GET /redoc` · `/openapi.json` | ReDoc / OpenAPI 3 JSON（可导入 Apifox、Postman） |
| `GET /api-guide` | 简要对接说明 HTML |
| `GET /v1/**` | 业务与图谱 API |

**文档随代码自动更新**：改 Pydantic 模型 / 路由注解 / `summary`·`description` 后刷新 `/docs` 即可，不要另写文档。

### 鉴权

业务接口（`/v1/**`）需 UC MAC Token：

```
Authorization: MAC id="...",nonce="...",mac="..."
```

由 UC 登录后前端 SDK 生成。Swagger 右上角 **Authorize** 填入后全局生效。

本地开发可设 `AUTH_BYPASS=1`，改用 `X-Test-Uid` / `X-Test-Uname` 两个头；**生产必须 `AUTH_BYPASS=0`**。可选 `X-User-Name`（`encodeURIComponent`）补充展示名。

免鉴权路径：`/`、`/health`、`/docs`、`/redoc`、`/openapi.json`、`/api-guide`。

### 自测页（`SERVE_DEV_UI=1` 时，非部署物）

| 地址 | 说明 |
| --- | --- |
| `/admin` · `/admin-console` | 管理端控制台（看板/四维/建数/审核） |
| `/admin-cytoscape` · `/kg-explorer` | 图谱探索 |
| `/student` | 学员端 |
| `/assessment` | 测评 |
| `/capability` | 能力画像 |
| `/dev` | 规划工作台壳 |
| `/cn-sources` | 来源目录页 |

## Docker / SDP

```bash
# 在仓库根（上下文里只需有 backend/ 一个子目录，其余目录都不进镜像）
docker build -f backend/Dockerfile -t voced-kg-api .

# 走配置中心：只注入 secret 与 profile，业务配置全部下发
docker run --rm -p 8088:8088 ^
  -e SDP_CLOUD_CONFIG_SECRET=<颗粒账密> ^
  -e SDP_ENV_NAME=development ^
  voced-kg-api

# 或不走配置中心，逐项注入
docker run --rm -p 8088:8088 ^
  -e CORS_ORIGINS=https://your-frontend.example.com ^
  -e DATABASE_URL=postgresql://user:pass@host:5432/voced_kg ^
  -e KG_REGION=CN ^
  voced-kg-api
```

SDP：只发布 **backend 镜像 + secret + PG 连接**；不要打 crawlers、不要挂 frontend。

base 镜像须 **Python 3.13+**（`api/schemas_assessment.py` 的联合类型里混了字符串前向引用，3.12 在 import 期就 `TypeError`）。仓库根的 `schemas/` 不进镜像，要对外暴露本体文件就挂卷：`-v $PWD/schemas:/app/schemas`。

### 改完自查（独立部署硬性要求）

把 `backend/` 单独拷到空目录，确认还能起：

```bash
PYTHONPATH=<该目录> python -m uvicorn backend.api.main:app
# /health 能连库、/openapi.json 200
```

运行时用到的三方包**必须写进 `requirements.txt`**——本地装了但没登记的包，在干净镜像里会走进 `try/except` 的降级分支，功能悄悄失效且不报错。

## 测试

```bash
python -X utf8 scripts/p0_e2e_acceptance.py    # 接口冒烟
python -X utf8 tests/e2e_robustness.py         # 参数 fuzz + 开关组合，只判 5xx（改完必跑）
python -X utf8 tests/e2e_skill_level.py        # Playwright E2E（自起 18099 端口）
python -X utf8 tests/run_assessment_demo.py    # 命令行跑一次完整测评工作流
```

无 pytest / lint 配置，都是可直接执行的脚本；Windows 下一律加 `-X utf8`。

## 对接文档

- 接口与表结构：`docs/服务端数据与接口设计.md`
- 共用库建表约束：`backend/预生产共用库表名占用清单.md`（预生产 PG 与 bcs-ai-agent 共用，建表前必查）
- 配置中心接入：`docs/SDP配置中心接入调研.md`
- 在线契约：部署后打开 `/docs`
