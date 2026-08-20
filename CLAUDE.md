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

# 闸门（改动相关模块后必跑；都要 PYTHONPATH=. ）
python -X utf8 tests/e2e_robustness.py                    # 每个 GET 接口注入脏参数，只判 5xx
python -X utf8 scripts/verify_write_paths.py              # 21 个写接口各真调一次（GET 那套 fuzz 覆盖不到写路径）
python -X utf8 scripts/verify_skill_name_exposed.py       # 出参有 code 必须配同层展示名（动态：真打接口）
python -X utf8 scripts/verify_skill_name_in_code.py       # 同一条约束的静态版（AST 扫源码，覆盖跑不到的分支）
python -X utf8 scripts/verify_skill_key.py                # skill_key 是 code、改名不换 key
python -X utf8 scripts/verify_draft_leak.py               # 草稿不泄漏到学员端
python -X utf8 scripts/verify_draft_closure_4dim.py       # 四维：编辑→草稿→前台不变→发布→前台变
python -X utf8 scripts/verify_question_pairing.py         # 模型产出能对回技能，没静默降级成模板题
python -X utf8 scripts/verify_delete_with_draft.py        # 删除遇草稿不 500、不留孤儿草稿
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

**`kg_node` / `kg_edge` 的主键是 `(id, is_draft)`，同一 id 有两行**（2026-08-19 起）——管理台的内容编辑先落草稿行（`is_draft=true`，`status` 恒为 `'draft'`，DB 级 CHECK `ck_kg_node_draft_status` 钉住），发布才写回线上行。方案见 `docs/方案-管理台草稿态与发布.md`。三条必守：

- **写路径每条 `UPDATE`/`DELETE` 都要钉 `is_draft`**。漏了就同时打中两行：写 `status` 会撞 CHECK 报 500（吵，好查），写别的列则**静默改坏线上数据**（不吵，最难查）。已经踩过三次：发布 500、删技能 500、删除留下能"复活已删记录"的孤儿草稿。
- **`ON CONFLICT (id)` 一律失效**，要写 `ON CONFLICT (id, is_draft)`。不匹配的 `ON CONFLICT` 会当成普通插入或直接报唯一冲突——**直连改库的脚本和采集脚本绕不过这条**，写批量数据前先确认。
- **读路径分两套口径**：管理台用 `config.prefer_draft(alias)`（草稿优先，实现是 anti-join 而非 `DISTINCT ON`，因为 20+ 处读路径各有自己的 ORDER BY/GROUP BY），前台只读线上行。给前台的 SQL 错加 `prefer_draft` = 草稿泄漏到学员端（`list_skill_bundles` 踩过）。闸门：`scripts/verify_draft_leak.py`、`verify_draft_closure.py`、`verify_delete_with_draft.py`。

另外 PostgreSQL 的外键引用不了部分唯一索引，所以 `kg_edge` 指向 `kg_node` 的两个外键**已删除**，两端存在性不再由库保证——靠 `scripts/check_orphan_edges.py` 兜。

**状态可见性**（`kg/pg_store/config.py`）—— 这里漏过好几次 bug。`archived` 是逻辑删除、任何接口都不返回；`draft`/`disabled` 仅管理台可见；`published` 前后台都可见。**每个 `kg_edge` 查询都要拼 `edge_published(alias)` 或 `edge_not_archived(alias)`**，只过滤节点挡不住边（如两端节点都正常的 `parent_of`）。节点同理用 `node_published`/`node_not_archived`。

**中间件顺序**（`api/main.py`）—— Starlette 中「后 add = 更靠外」。CORS 必须最后 add 包在鉴权外层，否则预检 OPTIONS 被 401、且 401 响应缺 CORS 头，浏览器只报跨域看不到真实原因。`CORS_ORIGINS=*` 时用 `allow_origin_regex` 而非 `allow_origins=["*"]`（后者与 `allow_credentials` 冲突）。

**技能等级**：一技能一档——一个技能在库里是 L1–L5 五个 `skill_level` 节点，读路径按 `attrs.skill_key`（`SKILL_KEY_SQL`）聚合成逻辑 bundle。「要求哪一档」由**边指向哪个等级节点**表达，改档 = **删旧边**建新边。产品档 1(了解)–5(专家)，`attrs.level` 是唯一真源，读路径不做任何刻度换算。档位名称/基准分/行为锚**只能**从 `kg/pg_store/skill_level_meta.py` 或 `GET /v1/student/meta/skill-levels` 读，禁止在业务代码或前端硬编码。

**一个岗位对一个技能只能有一条 `requires` 边**——高档天生覆盖低档，同时要求 L2 和 L3 没有业务含义。这里的陷阱是 edge id 由 `(src, rel, dst)` 决定而 dst 是**具体等级节点**，L2 与 L3 是两个 id，upsert 覆盖不了：重跑采集只会把旧边留在库里，不报错，只是列表页按 `skill_key` 算 7 项、详情页按边算 10 项，同一岗位两个数字。三层都要站住：写路径（`link_boss_skill_chain.stage_apply` 重建前先删该岗位所有 `requires`）、读路径（`skill_aggregate._dedupe_requires_by_skill`，直连改库绕得过写路径）、闸门（`scripts/fix_duplicate_requires.py --dry-run`，有重复 exit 1，铺量批跑完和重灌后各跑一次）。

**国标刻度归一只有一份，在 `kg/level_scale.py`，且必须发生在入库期**——采集端建节点（`crawlers/cn/ingest_skill_standards.py`）与灌库（`pg_store/migrate.py`）都调 `normalize_skill_level_node`，别在第三处另写映射表。刻度方向是**反的**：国标 L1=一级/高级技师=最高 → 产品档 5，L5 → 1；搞反了不报错、不崩，只把高级技师判成入门。`scripts/verify_backfill.py` 里那份期望表是**故意重复**的独立校验，别去 import `level_scale` 把它「去重」掉，否则等于自己比自己。

> 2026-08-14 手工回填 8919 个节点（可评分岗位 117 → 608），08-18 有人重灌了一次库，`migrate.py` 的 `attrs = EXCLUDED.attrs` 把源里的旧形态原样盖回去，数字**逐位退回**，全程零报错。现在归一挂在灌库必经之路上，重灌会自愈——回归闸门是 `scripts/verify_reload_keeps_levels.py`（临时库跑一次 `migrate --clear`，验证产品档还在），改动这条链路后必须跑它。**灌完库跑一次 `scripts/verify_backfill.py after` 逐档核对方向。**

**「能学」= 点开当场就能学**——免登录、免报名、无开课周期。判定只有一处：`config.learnable_course()`（SQL 过滤）与它导出的 `ENROLL_SOURCES`（`skill_aggregate` 判 `kind` 直接 import，别另抄）。资源分四类：`real` 可直学 / `enroll` 要报名或按学期开课 / `catalog` 课标目录条目 / `landing` 检索入口，**只有 real 能算进「有课程」的统计**。这里连着栽了两次，形状一样：先是教育部课标目录冒充课程（15960 条），后是中国大学MOOC 顶着「真课」标签挂在 67 个岗位上、学员点进去是报名墙（129 门）——两批都已 `archived`，用 `scripts/archive_courses.py` 增删（带 `--restore`）。`crawlers/cn/harvest_mooc_courses.py` 与 `harvest_xuetangx_courses.py` 已停用，`scripts/run_full_chain.py` 要 `--with-courses` 才跑课程步骤。

**`advances_to` 是 1:N**——一个岗位有多个向上方向（本方向纵深 / 转管理 / 跨方向转型），2026-08-18 由 1:1 改过来。改之前本体写 1:1、采集端提示词却写「可以有多条向上路径」，读路径按本体 `LIMIT 1`，于是 Java 库里有「全栈/技术经理/架构师」三条边，页面只显示一条。多跳完整链路读 `kg/pg_store/progression.py`（防环+深度上限），单跳读 `goal_overview._next_levels`。**置信度排序不能裸写 `ORDER BY e.confidence`**：那是文本列，升序恰好把最不可信的 `ai_inferred` 排在最前（`derived`/`official` 在后），要用显式 CASE 给序。

**采集跑完必须灌库，否则等于没跑**——`crawlers/` 写的是 SQLite 采集库，PG 才是运行库，两者靠 `python -m backend.kg.pg_store.migrate --region CN` 同步（不带 `--clear` 是增量 upsert）。2026-08-18 那批铺量栽在这：9 个门类进度全是「5 步 OK」、采集库里 4600+ 条 `requires`，而 PG 里除技术与产品全是 0，页面上什么都看不到——每步都成功，链路却是断的。`migrate` 只搬 `attrs`、**不写 `kg_node.category` 列**，而读路径查的是列，所以灌完还要跑 `scripts/migrate_skill_category_to_code.py`（上次少跑一次，12642 个技能显示「待归类」）。这两步已挂进 `scripts/run_full_chain.py` 的 `FINISH_STEPS`，批次末尾自动执行。**另外：铺量跑批期间不要改 `crawlers/` 下的文件**——每步是新起的子进程，改到一半会让后续门类 import 就崩（10 个门类因此整批失败）。

**`skill_key` 是 ASCII code，不是技能名**（2026-08-19 起）——形态 `SK` + md5(NFC 规范化后的名字) 前 10 位，展示名在 `attrs.skill_name`。真源是 `kg/skill_key.py`（`derive_key` 与 `SQL_DERIVE_KEY` 两边逐位一致，实测过）。改之前 key 就是中文名，而 key 要进 URL（`/v1/admin/skills/{skill_key}`）：库里带 `/` 的 26 个、带 `#` `%` 空格 `+` 的各有若干，前端一次双重编码就长出过一个幽灵技能 `3D%25E5%259C%25BA…`（二次解码就是「3D场景搭建」）。三条必守：

- **有 `attrs.skill_key` 就以它为准，永不重算**。key 按初始名字生成，之后**改名不换 key** —— 重算等于改个错别字就换主键，`kg_skill_prereq`（两列）、`biz_assessment_item`、`biz_assessment_question` 当场断链。
- **「按名字反推 key」不能当查找机制**。采集端判断「这技能已经有了吗」要按 `attrs.skill_name` 查库（`link_boss_skill_chain.existing_skill_keys` 就是这么做的）：技能一旦改过名，md5 反推必然失效，会重复建一套节点，且不报错。
- **`SKILL_KEY_SQL` 的兜底是现算 code，不是回落到名字**。同一技能的 5 个档位节点要算出同一个值才能聚成一组，行内唯一共享的东西只有名字，所以随机 id 在 SQL 里没法兜底 —— 这也是选 md5 而非 uuid 的唯一理由。入库期由 `level_scale.normalize_skill_level_node` 盖上（采集与灌库的必经之路，与刻度归一同一个位置）。
- 展示一律用 `skill_name` / `SKILL_NAME_SQL`。搜索也要按名字匹配：`SKILL_KEY_SQL ILIKE` 在改造后**中文一个都搜不到**（下拉框会永远空着）。
- **带了 code 就必须在同一个对象里带展示名**，而且「出参」不止 HTTP 响应，**出向调用也算**——学习计划服务的 payload、画像服务的五维记忆文本都在这条上栽过，错值在本服务任何页面上都看不见。这条连着漏了六轮，每轮形状都不一样，所以改任何返回技能的东西时四处都要过一遍：①数据层的 dict ②响应模型的字段声明（没声明 = 数据层给了也被 Pydantic 静默丢弃）③前端模板 ④发给外部服务的 payload。**同一个 gap 常有两个并存的构造点**（`progression._gap` 与 `goal_overview` 各有一份），改一处漏一处的症状是「详情页有名字、目标卡没有」。
  - 两个闸门是**互补**的，都要跑：`verify_skill_name_exposed.py` 动态打 61 个 GET 接口 + 两条出向 payload，能抓到「声明了但被 Pydantic 丢掉」；`verify_skill_name_in_code.py` 静态扫 AST，能抓到「压根没写」。动态那个的盲区是**探测样本没覆盖的分支等于没测**——专业直连技能 `covers` 就这么漏过：库里只有两条草稿态的 covers 边，闸门挑的已发布专业拿到空数组，一路 PASS，是人打开页面看见 `SKabd68031c5` 才发现的。
  - **拿不到名字时不要静默写 code**。要么回落成 code 但让它显式可见（`display_name` 那套），要么干脆丢弃这一项并把丢弃数记进事件表（`userprofile/sync._writable`）——灌进外部服务的脏文本收不回来。
  - 批量 `code → 名字` 只有一份：`skill_aggregate.resolve_skill_names`（`named_skill_refs` 是它的 `[{skill_key, skill_name}]` 包装）。别在 `kg/pg_store` 里 import `userprofile.skill_display`，依赖方向是反的。
  - **排序键不能是 code**：`ORDER BY skill_key` 等于按 md5 排，同一份数据每次看着都像变了。`industry_graph` 的注释写着「同层按名称」而实现在改造后悄悄分了岔。
- **`skill_id` 是身份，`skill_name` 是展示**（`biz_user_skill`）。这张表的 `skill_name` 列一度两用：2026-08-19 前 key 就是中文名，所以测评行往里塞 code 也没事，改造后学员端直接看到哈希。修法是从 `skill_id`（形如 `skill_key:SKxxxxxxxxxx`、主键的一部分、写入后不变）剥 code —— 见 `skill_display.code_from_skill_id`。**读路径的 SELECT 必须带上 `skill_id`**，少选这一列，`match_with_profile` 的 code 精确匹配整条路径失效，症状是「测过的技能显示未测、匹配度虚低」且不报错。改这条链路的顺序也是固定的：先让读路径认 `skill_id`，再改写路径存名字，反了就断。
- 存量迁移：`scripts/migrate_skill_key_to_code.py`（幂等，dry-run 默认）；闸门 `scripts/verify_skill_key.py`。

**技能分类存 code，不存名字**——`kg_node.category` 存 `TECH`/`OPERATE` 这类 code，展示名连 `kg_skill_category` 表取（读路径统一走 `skill_taxonomy.name_of()`，出参里叫 `category_name`）。真源是 `kg/pg_store/skill_taxonomy.py`，启动时幂等 upsert 进表（只 upsert 不 delete，管理台自建的分类不会被抹）。11 个实类 + 兜底 `UNSORTED`（待归类），刻意做粗；改名只动表，不动 12000 条技能。管理台查字典用 `GET /v1/kg/skill-categories?q=`。改之前库里并存两套中文口径（国标「操作与加工」11 种 + LLM「技术工程」9 种），且 LLM 那 3135 个**只写了 `attrs.category`、列是空的**，读路径查列，页面上全显示「未分类」。新增分类来源时把中文别名加进 `aliases`，`to_code()` 认不出的一律落兜底，不猜。

**边模型**：岗位 `requires` 技能（带 weight，Σ≈1）；专业 `covers` 技能（无权重、不归一）。本体见 `schemas/graph_schema.yaml`；发布门禁规则 BR-01~BR-08 在 `kg/pg_store/publish_rules.py`。

**一条脏数据不能打死一整页**——这个项目已经栽过三次，都是同一个形状：列表接口按行处理，某一行的值超出预期，整页 500。所以：
- 响应模型的数值字段按**实际可能的取值**声明，不要照着"它应该是个计数"想当然写 `int`（`weight_sum` 是小数，害整页岗位列表挂过一次）。
- `attrs` 是无约束的 TEXT/JSON，读路径对它做 `::int` 之类的强转必须带守卫（见 `config.attrs_level_int`），脏值取 NULL 而不是抛错。写路径同时校验（见 `write._assert_attrs_sane`）——采集脚本和直连改库绕得过应用层，两侧都要站得住。
- 新增查询参数一律给 `ge`/`le`/`min_length`；能被外部拿到的自增 id（如 `session_id`）必须校验归属，不存在与越权都回 404。

改完跑 `python -X utf8 tests/e2e_robustness.py`：它对每个 GET 接口的每个参数注入越界/错类型/空/NUL，并扫开关组合，只判 5xx。

**id 生成**用 `kg/provenance.py` 的 `make_node_id` / `make_edge_id`，不要手拼。每条节点/边必带 `source_url` + `license` + `confidence` + `fetched_at`。

**LLM 一律可降级**：`agent/` 下每处调模型都有规则兜底路径（`llm_ready()` 为 false 时生效），内测环境网关常为空。新增 AI 功能必须带降级分支，且流式与非流式两条路径产出同样的事件序列（见 `agent/stream.py`）。结构化产出类任务（出题、判分）用 `get_chat_model(fast=True)` 关闭深度思考。

**降级是静默的，所以要有闸门盯着**——降级路径本身没错，错在「本该走模型却整批降级」看不出来：HTTP 200、日志无异常，只有 `meta.engine` 从 `llm` 变成 `llm_partial`、`fallback_count` 等于题数，题目从情境判断悄悄变成「你在 X 上处于哪一档」的自评模板。2026-08-20 这么坏了一天：出题提示词里给模型看的是**技能名**（喂 `SKa1fa1d005d` 进去它会照着哈希编题），而匹配还在 `gen.get(code)`，模型回的自然是它看到的名字，于是每批产出全对不上。当时抽查的两个岗位都命中题库缓存，看起来一切正常。三条：

- **给模型看的标识和拿去查的标识必须是同一个**。现在靠 `bank._pair_generated` 三路匹配（`no` 序号 → 名字/code → 按出现顺序），提示词里带 `[序号]` 做锚点。
- **题库缓存只缓存内容，身份归当次请求**（`bank._from_cache`）。`biz_assessment_item.payload` 是出题那一刻整个题目 dict 的快照，里面也有一份 `skill_key`；迁移脚本刷了列没刷 JSON，命中缓存就把中文 key、空 skill_name 原样吐给学员端。同形的还有改档后缓存带着旧 `required_level`。存量修复：`scripts/fix_assessment_payload_skill.py`。
- 闸门 `scripts/verify_question_pairing.py`：六种回话形态的匹配 + 真出一批题断言 `fallback_count=0`（**必须 `use_cache=False`**，命中缓存等于没验；无网关时明确报 SKIP 而不算通过）。

**双键的画像 map 不能直接上屏**——`skill_display.profile_levels` 故意给同一技能建两个键（code 与名字），因为 `match_with_profile._user_level_for` 有两条匹配路径（测评行按 code 精确、简历行按名字模糊）。凡拿它**计数或渲染**的地方都要先过 `canonical_levels` 折叠，否则每个技能列两遍、计数翻倍（`/v1/student/profile` 的 `counts.assessment` 与 `counts.merged` 曾双双翻倍，5 个技能显示 10）。同一个数在路由里有走缓存、不走缓存两条路，**两处都要改**，漏一处的症状是「有时 5 有时 10」。

**鉴权**：UC MAC Token 中间件写 ContextVar，路由用 `Depends(require_auth_user)` 取。签名校验用 `scope["raw_path"]`（保留 %XX 编码），不能用已解码的 `request.url.path`。用户中心不在本服务，只认 Token 并冗余 `user_id`/`user_name`。

## 文档

设计与方案文档在 `docs/`（36 篇，中文）。新的分析/方案产出写进 `docs/`，不要堆在对话或 README 里。接口与表结构：`docs/服务端数据与接口设计.md`；目录职责：`docs/工程目录说明.md`。
