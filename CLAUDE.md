# CLAUDE.md

职业教育知识图谱：**行业 → 专业 / 岗位 → 技能**四维图谱 + 学员端 AI 诊断/测评/学习路径。
Python 3.14 + FastAPI + PostgreSQL(psycopg3) + LangGraph；无前端构建（自测页是裸 HTML）。

## 常用命令

```bash
pip install -r backend/requirements.txt        # 只跑 API 用这个；根 requirements.txt 含爬虫依赖

# 起服务（默认不挂自测页）
python -m uvicorn backend.api.main:app --host 0.0.0.0 --port 8088
# 本地要看自测页：先在 backend/.env 里 SERVE_DEV_UI=1（见下方「配置」——命令行传 env 无效）

python -m backend.kg.pg_store.migrate --clear --region CN   # SQLite → PG 灌图数据
python -X utf8 scripts/verify_backfill.py after             # 灌完必跑：技能档位逐档核对方向

python -X utf8 tests/e2e_skill_level.py        # Playwright E2E（自起 18099 端口实例）
python -X utf8 tests/run_assessment_demo.py    # 命令行跑一次完整测评工作流
python -X utf8 scripts/p0_e2e_acceptance.py    # 接口冒烟
```

无 pytest / lint 配置。`tests/` 与 `scripts/` 都是**可直接执行的脚本**，Windows 下一律加 `-X utf8`（大量中文输出）。

契约看 `/docs`（Swagger，可试调）；改 Pydantic 模型或路由注解即自动更新，不要另写文档。

## 第一原则：backend 必须能独立部署

`backend/` 是本项目唯一的服务端交付物，**只拷这一个目录 + 一串线上 PG 连接串就要能起来**。数据库用线上已有实例，不由本项目部署。因此：

- backend 不得 import `crawlers` / `pipelines` / 仓库根任何模块；也不得在**运行路径**上依赖 `frontend/`、`data/`、`schemas/`、`reports/` 等仓库根目录的文件。确需引用的一律 `.exists()` 守卫并优雅跳过。
- 运行时用到的三方包**必须写进 `backend/requirements.txt`**。本地开发机装了但没登记的包，在干净镜像里会走进 `try/except` 的降级分支，功能悄悄失效且不报错——这是最难查的一类线上问题。新增 `import` 时先确认它在 requirements 里。
- 配置只从环境变量 / `backend/.env` 读，不回落仓库根。
- 改完自查：把 `backend/` 单独拷到空目录，`PYTHONPATH=<该目录> python -m uvicorn backend.api.main:app` 能起、`/health` 能连库、`/openapi.json` 200。

## 目录与依赖方向

```
frontend  ──HTTP──▶  backend          (frontend/ 只是自测页，不部署、不被打包)
crawlers  ──import──▶ backend.kg      (离线采集，不进 API 镜像)
backend   ──不依赖──▶ crawlers
pipelines/  旧路径 shim，勿写新逻辑
```

`backend/` 是唯一对外交付物（`backend/Dockerfile`，在仓库根 build）。**backend 里不要写爬虫，crawlers 里不要写 HTTP 产品接口。**

后端分层：`api/`（路由 + Pydantic 契约）→ `kg/pg_store/`（SQL 与业务规则，代码量集中在这里）→ `agent/`（LLM 诊断与测评）。图数据在 `kg_node`/`kg_edge`，运行时业务数据在 `biz_*` 表（`kg/pg_store/biz_ddl.py`），两者分离。表结构靠启动时 `ensure_*_schema()` 的幂等 DDL 演进，没有 migration 框架——加列写 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`。

## 配置

真源是 **`backend/.env`**，`settings.py` 以 `override=True` 加载它，**会覆盖进程环境变量**。所以 `AUTH_DEBUG=1 API_PORT=18099 python -m ...` 这类命令行注入无效；测试要改配置走 `tests/_e2e_server.py` 的做法（dotenv 加载完成后改内存值）。改 `.env` 后必须重启进程。

关键开关：`SERVE_DEV_UI`（挂 `frontend/`）、`AUTH_BYPASS`（生产必须 0）、`REVIEW_REQUIRED`（0=直写，1=进待审队列）、`DATABASE_URL`、`KG_REGION`、`LLM_BASE_URL`/`GITHUB_TOKEN`/`LLM_MODEL`。

## 必须遵守的约定

**状态可见性**（`kg/pg_store/config.py`）—— 这里漏过好几次 bug。`archived` 是逻辑删除、任何接口都不返回；`draft`/`disabled` 仅管理台可见；`published` 前后台都可见。**每个 `kg_edge` 查询都要拼 `edge_published(alias)` 或 `edge_not_archived(alias)`**，只过滤节点挡不住边（如两端节点都正常的 `parent_of`）。节点同理用 `node_published`/`node_not_archived`。

**中间件顺序**（`api/main.py`）—— Starlette 中「后 add = 更靠外」。CORS 必须最后 add 包在鉴权外层，否则预检 OPTIONS 被 401、且 401 响应缺 CORS 头，浏览器只报跨域看不到真实原因。`CORS_ORIGINS=*` 时用 `allow_origin_regex` 而非 `allow_origins=["*"]`（后者与 `allow_credentials` 冲突）。

**技能等级**：一技能一档——一个技能在库里是 L1–L5 五个 `skill_level` 节点，读路径按 `attrs.skill_key`（`SKILL_KEY_SQL`）聚合成逻辑 bundle。「要求哪一档」由**边指向哪个等级节点**表达，改档 = 删旧边建新边。产品档 1(了解)–5(专家)，`attrs.level` 是唯一真源，读路径不做任何刻度换算。档位名称/基准分/行为锚**只能**从 `kg/pg_store/skill_level_meta.py` 或 `GET /v1/student/meta/skill-levels` 读，禁止在业务代码或前端硬编码。

**国标刻度归一只有一份，在 `kg/level_scale.py`，且必须发生在入库期**——采集端建节点（`crawlers/cn/ingest_skill_standards.py`）与灌库（`pg_store/migrate.py`）都调 `normalize_skill_level_node`，别在第三处另写映射表。刻度方向是**反的**：国标 L1=一级/高级技师=最高 → 产品档 5，L5 → 1；搞反了不报错、不崩，只把高级技师判成入门。`scripts/verify_backfill.py` 里那份期望表是**故意重复**的独立校验，别去 import `level_scale` 把它「去重」掉，否则等于自己比自己。

> 2026-08-14 手工回填 8919 个节点（可评分岗位 117 → 608），08-18 有人重灌了一次库，`migrate.py` 的 `attrs = EXCLUDED.attrs` 把源里的旧形态原样盖回去，数字**逐位退回**，全程零报错。现在归一挂在灌库必经之路上，重灌会自愈——回归闸门是 `scripts/verify_reload_keeps_levels.py`（临时库跑一次 `migrate --clear`，验证产品档还在），改动这条链路后必须跑它。**灌完库跑一次 `scripts/verify_backfill.py after` 逐档核对方向。**

**边模型**：岗位 `requires` 技能（带 weight，Σ≈1）；专业 `covers` 技能（无权重、不归一）。本体见 `schemas/graph_schema.yaml`；发布门禁规则 BR-01~BR-08 在 `kg/pg_store/publish_rules.py`。

**一条脏数据不能打死一整页**——这个项目已经栽过三次，都是同一个形状：列表接口按行处理，某一行的值超出预期，整页 500。所以：
- 响应模型的数值字段按**实际可能的取值**声明，不要照着"它应该是个计数"想当然写 `int`（`weight_sum` 是小数，害整页岗位列表挂过一次）。
- `attrs` 是无约束的 TEXT/JSON，读路径对它做 `::int` 之类的强转必须带守卫（见 `config.attrs_level_int`），脏值取 NULL 而不是抛错。写路径同时校验（见 `write._assert_attrs_sane`）——采集脚本和直连改库绕得过应用层，两侧都要站得住。
- 新增查询参数一律给 `ge`/`le`/`min_length`；能被外部拿到的自增 id（如 `session_id`）必须校验归属，不存在与越权都回 404。

改完跑 `python -X utf8 tests/e2e_robustness.py`：它对每个 GET 接口的每个参数注入越界/错类型/空/NUL，并扫开关组合，只判 5xx。

**id 生成**用 `kg/provenance.py` 的 `make_node_id` / `make_edge_id`，不要手拼。每条节点/边必带 `source_url` + `license` + `confidence` + `fetched_at`。

**LLM 一律可降级**：`agent/` 下每处调模型都有规则兜底路径（`llm_ready()` 为 false 时生效），内测环境网关常为空。新增 AI 功能必须带降级分支，且流式与非流式两条路径产出同样的事件序列（见 `agent/stream.py`）。结构化产出类任务（出题、判分）用 `get_chat_model(fast=True)` 关闭深度思考。

**鉴权**：UC MAC Token 中间件写 ContextVar，路由用 `Depends(require_auth_user)` 取。签名校验用 `scope["raw_path"]`（保留 %XX 编码），不能用已解码的 `request.url.path`。用户中心不在本服务，只认 Token 并冗余 `user_id`/`user_name`。

## 文档

设计与方案文档在 `docs/`（36 篇，中文）。新的分析/方案产出写进 `docs/`，不要堆在对话或 README 里。接口与表结构：`docs/服务端数据与接口设计.md`；目录职责：`docs/工程目录说明.md`。
