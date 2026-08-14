# backend 独立部署自检报告

检查日期：2026-08-14 · 分支 `public-main` · 宿主 Python 3.14.5 / Docker 29.1.3
**P0–P3 六项已于同日全部修复并复验，详见文末「修复与复验」。**

**原始结论：代码层面完全独立，但按当时的 `backend/Dockerfile` 构建出的镜像跑不起来** —— 两个阻断点都在打包侧（base 镜像版本、漏登记的依赖），不是代码耦合。修掉这两项后，只有 `backend/` 一个目录（连 `schemas/` 都不要）即可正常提供全部 82 个接口。

## 验证方式

1. 把 `backend/` 拷到空目录、删掉 `.env`，`PYTHONPATH=<该目录>` 导入 `backend.api.main` → 通过（41 routes / 82 paths）
2. AST 扫描 `backend/**/*.py` 的全部 import，比对 `backend/requirements.txt`
3. `docker build -f backend/Dockerfile .` 真实构建，容器内导入 app、`TestClient` 探 `/openapi.json` `/health` `/v1/config` `/api-guide`
4. 容器内 `rm -rf /app/schemas` 后重测

## 阻断问题（镜像当前起不来）

### 1. base 镜像 Python 3.12，代码要求 3.14

`backend/Dockerfile:4` 是 `FROM python:3.12-slim`。容器内 `import backend.api.main` 直接崩：

```
TypeError: unsupported operand type(s) for |: 'types.UnionType' and 'str'
Unable to evaluate type annotation
  "StageParseOutput | StageAssessOutput | 'AssessmentReportOut' | dict[str, Any]"
```

出处 `backend/api/schemas_assessment.py:157`。`X | Y | "字符串前向引用"` 这种混用在 3.12 的 pydantic 求值路径上不成立，3.14 才支持。宿主机是 3.14 所以本地一直看不出来。

改 `FROM python:3.14-slim` 后该错误消失（其余依赖在 3.14 上均有 wheel，构建正常）。

### 2. `python-multipart` 未登记在 `backend/requirements.txt`

`backend/api/routes_student.py:758` 与 `:803` 的简历上传用 `file: UploadFile = File(...)`。FastAPI 在**导入期**就检查 multipart，缺失直接：

```
RuntimeError: Form data requires "python-multipart" to be installed.
```

这不是静默降级而是整个服务起不来。宿主机装了 0.0.32（作为别的包的传递依赖），干净镜像里没有 —— 正是 CLAUDE.md 警告的那类问题。

补上这两项后容器内实测：`IMPORT OK 42 routes / 82 paths`，`/openapi.json` `/health` `/v1/config` `/api-guide` 全 200（`/health` 为 `degraded`，仅因容器未配 `DATABASE_URL`，属预期）。

## 非阻断问题

### 3. Dockerfile 强依赖仓库根作为 build 上下文

`COPY backend/requirements.txt`、`COPY backend /app/backend`、`COPY schemas /app/schemas` 三行都要求 context 是仓库根；其中第 24 行的 `schemas/` 缺失会让 build 直接失败——而它其实是可选的。

实测容器内删掉 `/app/schemas` 后：82 个接口一个不少，只少一条 `/schemas` 静态挂载路由（`main.py:175` 有 `.exists()` 守卫）。

若要做到「只拷 `backend/` 一个目录就能 build」，把该 COPY 改成可选、并让 context 就是 `backend/` 自身即可。当前形态下部署方必须带上仓库根，与「第一原则」有出入。

### 4. `.dockerignore` 的 `__pycache__/` 只匹配根级

镜像内 `/app/backend/__pycache__` 确实存在。Docker 的忽略模式不递归，需写 `**/__pycache__/`。只影响镜像体积与整洁度。

（`.env` 的排除是有效的：`**/.env` 已覆盖，加上 Dockerfile 里 `rm -f` 兜底，镜像内确认只有 `.env.example`。）

### 5. `.env.example` 缺三个变量

`settings.py` 读了但模板未列：`OPENQ_AI_MANAGER`（用户画像服务地址，留空则匹配度只用测评画像）、`API_VERSION`、`DEBUG`。部署方照模板配会漏掉画像服务。
（`BCS_SDP_APP_ID`、`openq-ai-manager` 是同名别名，不必单列。）

### 6. `requirements.txt` 两处注释已过时

- `langgraph-checkpoint-postgres`：测评断点已改为业务表存储（见 `agent/assessment/store.py` 开头说明），代码里已无 `PostgresSaver` 引用，这个包和它的注释都是死的。
- `psycopg[binary,pool]` 的注释说「`agent/assessment/graph.py` 的 PostgresSaver 连接池要 psycopg_pool」，同上，理由已不成立（psycopg 本身仍必需）。

## 代码耦合检查：干净

- 全量 AST 扫描无一处 import `crawlers` / `pipelines` / `schemas` / `data` / `reports` / `frontend` / `scripts` / `tests`
- 运行路径上三处指向仓库根的路径 —— `FRONTEND_DIR` / `SEED_DIR` / `SCHEMAS_DIR`（`api/main.py:124-126`）—— 全部带 `.exists()` 守卫
- `kg/paths.py` 的 `DATA` / `GRAPH` / `ensure_dirs()` 只被 `kg/query_cli.py` 使用（CLI 工具，不在 API 路径）
- `kg/neo4j_store/` 整个目录无任何引用者，`neo4j` 包在 requirements 里已注释掉，一致
- 三方依赖 14 个，除上述 `python-multipart` 外均已登记
- `settings.py` 的加载顺序对独立部署是正确的：仓库根 `.env` 只做 `override=False` 兜底，`backend/.env` 才是真源

## 修复与复验

| 优先级 | 文件 | 动作 |
|---|---|---|
| P0 | `backend/Dockerfile` | base → `python:3.14-slim`，注释写明为何须 3.13+ |
| P0 | `backend/requirements.txt` | 加 `python-multipart>=0.0.9` |
| P1 | `backend/Dockerfile` | 删掉 `COPY schemas`；镜像只 COPY `backend/`，本体文件改卷挂载 |
| P2 | `.dockerignore` | `__pycache__/` → `**/__pycache__/`（`*.py[cod]`、`*.egg-info` 同改），并把 `schemas/` 移进非交付列表 |
| P2 | `backend/Dockerfile` | 清理层顺带 `find -name __pycache__ -exec rm -rf`——上下文换成「只含 backend 的目录」时根 `.dockerignore` 不生效，pyc 会跟着进来 |
| P2 | `backend/.env.example` | 补 `OPENQ_AI_MANAGER` / `API_VERSION` / `DEBUG` |
| P3 | `backend/requirements.txt` | 删 `langgraph-checkpoint-postgres`，改掉 psycopg 的过时注释 |
| — | `backend/README.md` | 同步 base 版本要求与 schemas 挂载说明 |

复验（两种 build 上下文各跑一遍，均 `--no-cache` 验证过清理层）：

| 上下文 | build | 镜像内容 | 导入 | 端点 |
|---|---|---|---|---|
| 仓库根 | ✅ | `/app` 下只有 `backend`，无 `.env`、无 `__pycache__` | 41 routes / 82 paths | `/openapi.json` `/health` `/v1/config` `/api-guide` `/docs` 全 200 |
| 只含 `backend/` 的目录（含 `.env`，无 `.dockerignore`） | ✅ | 同上，`.env` 被 `rm` 兜底删除 | 41 routes / 82 paths | 同上全 200 |

`/health` 返回 `degraded` 属预期：容器未注入 `DATABASE_URL`，`startup` 的 `ensure_*_schema()` 走 `try/except pass`，不阻塞启动。

未跑的项：`tests/e2e_robustness.py` 等需要起服务并连本地 PG，本次未执行；本轮改动只涉及打包与配置模板，未触碰任何业务代码。
