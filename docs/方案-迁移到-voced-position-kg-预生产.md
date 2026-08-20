# 迁移到 voced-position-kg（预生产）执行计划

> 结论先行：**代码侧已基本就绪**，四个问题里只有「库初始化 + 图数据搬运」是真正的工作量，
> 其余三项是清理与补配置。下面每一条都标了**已核实**的依据，没核实的写明「待确认」。
>
> 核对日期：2026-08-20。目标颗粒 `voced-position-kg`（appId `6a79477d81abbd168b76aed2`，
> preproduction envId `6a79477f81abbd168b76aed6`）。

---

## 0. 迁移前必须先做的三件清理（在当前仓库做，别带到新仓库）

| # | 事项 | 为什么 |
|---|---|---|
| C1 | 删 `backend/kg/neo4j_store/`（整个目录） | `client.py` 第 6 行 `from neo4j import GraphDatabase` **没有 try 守卫**，而 `requirements.txt` 里 neo4j 是注释掉的。实测启动不会加载它（运行路径无人 import），但留着就是一颗「谁 import 谁炸」的雷，且干净镜像里必炸 |
| C2 | 删 `backend/kg/graph_store.py` + `backend/kg/query_cli.py` | SQLite 时代的 MVP store，建的是**裸名表 `nodes` / `edges`**。预生产库与 bcs-ai-agent 共用，裸名表是最危险的形态（见 `backend/预生产共用库表名占用清单.md`）。它只被 `query_cli.py` 用，一起删 |
| C3 | 删 dev 库里的历史备份表（不迁走） | `bak_skillkey_{node,prereq,assitem,assq}_20260819`、`kg_edge_bak_s2`、`kg_node_skill_level_bak_20260811`、`kg_edge_dedupe_log`、`biz_learning_path`、`biz_learning_step` —— 代码里已无人引用，合计约 50 MB。**确认 skill_key 迁移无误后**再删 |

`checkpoints` / `checkpoint_*` 是 LangGraph `PostgresSaver` 建的，本项目已改成业务表存断点
（`agent/assessment/store.py`），dev 库里那四张是历史残留；预生产那四张属于 bcs-ai-agent，**不要动**。

---

## 1. 库的初始化

### 1.1 表结构：不需要初始化脚本

启动时 `backend/api/main.py` 的 `_startup()` 跑三个幂等 DDL，**失败就不启动**（刻意的）：

```python
ensure_schema()        # client.py     → kg_node / kg_edge / kg_proposal
ensure_biz_schema()    # biz_ddl.py    → 14 张 biz_* + kg_skill_category + kg_skill_prereq
ensure_review_schema() # review.py     → kg_change_request
```

另有两处**懒建**（首次访问时建，不在启动路径）：
`node_stats.py` → `kg_node_stats`；`item_store.py` → `biz_assessment_item`。

全部 `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`（31 个索引）+
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`。**没有 `CREATE EXTENSION` / `CREATE SCHEMA` /
`CREATE ROLE`**（已 grep 确认），所以只要一个普通业务账号 + `CREATE` 权限即可，不需要超级用户。

**所以「初始化脚本」= 把服务起起来一次。** 不要另写一份 DDL 脚本——那就是第二份真源，
本项目在「同一判定各写一份」上已经栽过四次。

### 1.2 建表前必须查表名占用（共用库，最大的坑）

预生产 PG（`preproduction_pgsql_bcs_ai_agent` @ `master.preprod.all02.pgsql.sdp:5431`）
**与 bcs-ai-agent 共用**。撞名时 `CREATE TABLE IF NOT EXISTS` **静默跳过**，然后本项目
拿着别人的表结构跑，症状是「列不存在 / 类型不对」，且只在共用库复现。

第一次部署前，在 Pod 里（本地连不上这个库，属访问控制）跑：

```sql
SELECT t.n AS 表名, to_regclass('public.' || t.n) AS 已存在
FROM (VALUES
  ('kg_node'),('kg_edge'),('kg_node_stats'),('kg_proposal'),('kg_change_request'),
  ('kg_skill_category'),('kg_skill_prereq'),
  ('biz_achievement_def'),('biz_assessment_answer'),('biz_assessment_item'),
  ('biz_assessment_question'),('biz_chat_message'),('biz_diagnosis_result'),
  ('biz_diagnosis_session'),('biz_event'),('biz_resume_asset'),
  ('biz_user_achievement'),('biz_user_goal'),('biz_user_learning_plan'),
  ('biz_user_points'),('biz_user_skill')
) AS t(n);
```

全部为 `NULL` 才能放心启动。**任意一个非 NULL 就先停下来查是谁的**。

> ⚠ `backend/预生产共用库表名占用清单.md` 的「本项目占用」表里**漏了 `kg_skill_category`**
> （那张表是后加的）。上面这 21 张才是完整清单，顺手把文档补上。

**彻底隔离的正解是给本项目建独立 schema**（`CREATE SCHEMA voced_kg` + `search_path`），
但那要改 `client.py` 的连接初始化，属于额外改造，本轮不做——本轮靠前缀 + 上面这条 SQL 兜。

### 1.3 图数据搬运：这是真正的工作量

表建好是空的，**图数据不会自己出现**。规模（dev 库实测）：

| 表 | 行数 | 说明 |
|---|---|---|
| `kg_node` | 50 981 线上行 + 2 草稿行 | skill_level 27 876 / course 17 745 / occupation 2 460 / major 2 281 / credential 470 / industry 149 |
| `kg_edge` | 61 572 线上行 + 6 草稿行 | |
| `kg_skill_prereq` | 2 108 | |
| `kg_skill_category` | 12 | 启动时幂等 upsert，**不用迁** |
| `biz_*` 合计 | 616 行 | 学员测评/诊断数据，**建议不迁**（见下） |

有效数据量约 240 MB（含索引 288 MB，索引会重建，不必搬）。

**不能用 `python -m backend.kg.pg_store.migrate`**：那条路读的是 `data/kg.sqlite`（采集库，
在仓库根，不进 backend 交付物），是「采集 → 运行库」的同步工具，不是「库 → 库」的搬运。

推荐 **PG 逻辑导出 / 导入**，只带本项目的表：

```bash
# ① 在能连 dev 库的机器上导出（--data-only，表结构交给服务启动时建）
pg_dump "$DEV_DATABASE_URL" --data-only --no-owner --no-privileges \
  --disable-triggers \
  -t kg_node -t kg_edge -t kg_skill_prereq \
  -Fc -f voced_kg_graph_20260820.dump

# ② 传到能连预生产库的地方（本地连不上，要在集群内的 Pod 或跳板机）
# ③ 先让服务起一次把表建好，再导入
pg_restore --data-only --no-owner --disable-triggers \
  -d "$PREPROD_DATABASE_URL" voced_kg_graph_20260820.dump
```

四个必须注意的点：

1. **`--data-only`，不要 `--schema-only` 或全量**。全量 dump 会带上 dev 那些备份表和
   `checkpoint_*`，在共用库里就是污染。
2. **草稿行要不要迁**是个决策点。`kg_node` 有 2 行、`kg_edge` 有 6 行 `is_draft=true`，
   那是本地测试留下的未发布改动。建议**导出时过滤掉**（`--data-only` 不支持 WHERE，
   改用 `COPY (SELECT … WHERE NOT is_draft) TO`），预生产从一份干净的线上态开始。
3. **导入后必跑校验**，顺序不能省：
   ```bash
   python -X utf8 scripts/verify_backfill.py after        # 技能档位逐档核对方向（刻度搞反不报错）
   python -X utf8 scripts/migrate_skill_category_to_code.py   # migrate 只搬 attrs、不写 category 列
   python -X utf8 scripts/fix_duplicate_requires.py --dry-run # 有重复 requires 就 exit 1
   python -X utf8 scripts/check_orphan_edges.py               # kg_edge 的两个外键已删，靠这个兜
   python -X utf8 scripts/verify_skill_key.py                 # skill_key 是 code、改名不换 key
   ```
   走 `pg_dump/pg_restore` 是逐字节复制，理论上前两条不会有问题，但**这两条恰恰是
   「不报错、只是数字悄悄错」的类型**，跑一次的成本远低于事后排查。
4. **`biz_*` 建议不迁**。那 616 行是开发期的测评记录（含 `biz_diagnosis_result` 里
   9 份报告快照，其中一部分还是 skill_key 改造前的旧形态）。预生产是给真实用户的，
   带着开发测试数据进去只会让人困惑。空表启动即可。
   若要迁，注意 `biz_user_*` 的 `user_id` 是 UC 用户 id，dev 用的是内测账号，
   预生产 UC 环境不同，迁过去也对不上人。

---

## 2. 配置：需要补进 SDP 配置中心的键

配置真源**已经是 SDP 配置中心**（`backend/sdp_config.py`，启动时用
`SDP_CLOUD_CONFIG_SECRET` 拉取并 `override=True` 注入环境变量）。当前 `/health` 显示
`config.source=remote / profile=development / keys=26`。

`backend/.env` 现在只剩 6 个键，其中只有两个是本地覆盖：

```
SDP_CLOUD_CONFIG_SECRET   ← 拉配置用的凭据，SDP 会注入，.env 里这份仅本地开发
SDP_ENV_NAME              ← 同上
DATABASE_URL              ← 本地覆盖（连本地 docker PG）
SERVE_DEV_UI              ← 本地覆盖（挂自测页）
BASELINE_RO_DATABASE_URL  ← 纯本地，只被 scripts/serve_baseline_readonly.py 用，不进运行路径
VERIFY_TLS                ← 本地关 TLS 校验用
```

### 2.1 preproduction 配置文件：**当前是空的**，要整套写一遍

development 的 `application`（label `master`）里有 28 个键；preproduction / product **仍为空**。
先把 development 那套整体复制过去，再逐个改环境相关的值：

| 键 | 预生产该填什么 | 备注 |
|---|---|---|
| `DATABASE_URL` | 预生产 PG 连接串 | 共用库，见 §1.2 |
| `AUTH_BYPASS` | **`0`** | 这是「跳过审核」开关，不是跳过 UC 鉴权；预生产必须 0 |
| `REVIEW_REQUIRED` | 按产品决定（0=直写 / 1=进待审） | 上线初期建议 `1` |
| `SERVE_DEV_UI` | **`0`** | 自测页不上生产；Dockerfile 里已默认 0，配置中心别覆盖成 1 |
| `AUTH_DEBUG` | `0` | |
| `CORS_ORIGINS` | 真实前端域名，**不要 `*`** | `*` 时代码走 `allow_origin_regex`，能用但不该在预生产放开 |
| `UC_API_HOST` / `UC_COMPONENT_HOST` / `UC_ENV` / `UC_SDK_URL` | 预生产 UC 地址 | UC 环境与 dev 不同 |
| `BTS_ENDPOINT` / `BTS_ACCOUNT` / `BTS_PASSWORD` / `E_AI_SPACE` | 预生产 BTS 与空间 | 学习计划推送 + 用户画像都走它 |
| `OPENQ_AI_MANAGER` | 预生产画像服务 | 缺失只降级（记忆画像失效），不报错 |
| `LLM_BASE_URL` / `GITHUB_TOKEN` / `LLM_MODEL` | 预生产 AI 网关 | 缺失时 `llm_ready()=false`，全部走规则兜底 |
| `KG_REGION` | `CN` | |
| `DEV_USER_ID` / `DEV_USER_NAME` | **建议不要配** | 只在 `AUTH_BYPASS` 下用于伪造身份 |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8088` | Dockerfile 已给默认值 |

### 2.2 development 有、但配置中心里没有的键（当前靠代码默认值）

这些 `settings.py` 会读、配置中心没写。**加粗的两个建议在预生产显式写上**：

| 键 | 代码默认 | 预生产要不要显式配 |
|---|---|---|
| **`LEARNING_PLAN_PATH`** | `/v1/internal/job-plans/import` | **要**。学习计划推送的目标路径，对方改路由我们就静默推不出去（`push.py` 里 `not settings.LEARNING_PLAN_PATH` 时直接跳过） |
| **`VERIFY_TLS`** / `TLS_CA_BUNDLE` | `1` / 空 | **要**。内网服务多为自签证书；本地 `.env` 里把它关了，预生产应保持 `1` 并配 CA bundle，而不是继续关校验 |
| `APP_ENV` | 无（回落 `SDP_ENV_NAME`） | 不用，SDP K8s 会注入 `SDP_ENV_NAME` |
| `API_VERSION` | `0.6.1` | 不用 |
| `DEBUG` / `DB_POOL_CHECK` | `0` / `1` | 不用 |
| `LLM_API_KEY` | 回落到 `GITHUB_TOKEN` | 不用，二者取其一 |
| `BTS_API_ENDPOINT` | 回落到 `E_AI_SPACE` | 不用 |
| `BCS_SDP_APP_ID` / `BTS_SDP_APP_ID` | 回落到 `SDP_APP_ID` | 不用 |
| `LLM_TEMPERATURE` / `LLM_REQUEST_TIMEOUT` / `LLM_MAX_OUTPUT_TOKENS` | 有默认 | 配置中心已有，照搬 |

### 2.2b 预生产配置实测复核（2026-08-20，`config fetch` 读到 28 项）

配置已创建，`application` / label `master` / env-type 5 / env-id `6a79477f81abbd168b76aed6`。
**读它不能用 dev 的颗粒 secret**：`fetch_config(profile='preproduction')` 用本地那把
secret 返回 **403**，说明 secret 按环境隔离。要么用 `sdp-portal-cli config fetch`
（走门户 API Key），要么拿预生产自己的 secret。

#### 必改（会导致部署后不可用）

| # | 问题 | 事实 | 处理 |
|---|---|---|---|
| **B1** | **端口对不上** | 预生产集群暴露 **8080**（`cluster ports` 实测，Kong service `service.wx.pre_product.1.voced-position-kg…`）；配置中心 `API_PORT: 8080`；但 `backend/Dockerfile` 的 `CMD` 是 exec form **写死 `--port 8088`**，不做变量展开 → 容器监听 8088，Kong 打 8080，**连不上**。<br>更隐蔽的一层：**配置中心的 `API_PORT` 根本无法影响绑定端口** —— 它是 Python import 期由 `sdp_config` 拉下来的，uvicorn 的 `--port` 早已确定。它在代码里只影响 `openapi_servers()` 的 Swagger servers 列表。 | 已改 Dockerfile：`ENV API_PORT=8080` + `CMD ["sh","-c","exec uvicorn … --port \"${API_PORT:-8080}\""]`。绑定端口以**容器环境变量**为准，配置中心那份只当文档 |
| **B2** | **TLS 校验没配** | `VERIFY_TLS` / `TLS_CA_BUNDLE` **都没写**，代码默认 `VERIFY_TLS=1`（严格校验）。而本地 `.env` 一直是 `0`，**所以内网证书链从没被验证过**。预生产会真的去校验 `betabts.cn.ndhy.com`、`uc-gateway.beta.cn.ndhy.com`、`ai-manager-v2.beta.ndaeweb.com` —— 若是内网自签证书，**UC 鉴权 + BTS 全线失败**（学习计划推不出去、画像取不到、登录校验挂） | 二选一：① 把内网根证书打进镜像并配 `TLS_CA_BUNDLE=/etc/ssl/certs/internal-ca.pem`（推荐）；② 确认这三个域名用的是公网可信证书，则什么都不用做。**不要直接抄本地的 `VERIFY_TLS=0`** |
| **B3** | **`LEARNING_PLAN_PATH` 没配** | 走代码默认 `/v1/internal/job-plans/import` | 跟学习计划服务确认预生产路径；对方改了路由我们是**静默推不出去**（`push.py` 里该值为空直接跳过） |

#### 建议改（不影响可用性）

| # | 项 | 当前值 | 建议 |
|---|---|---|---|
| S1 | `CORS_ORIGINS` | `'*'` | 收窄到真实前端域名。代码在 `*` 时走 `allow_origin_regex`（能用，不会和 `allow_credentials` 冲突），但预生产放开全域没必要。**目前该环境还没绑域名**（`cluster domains` 返回空），所以先留 `*`，域名定了再收 |
| S2 | `DEV_USER_ID` / `DEV_USER_NAME` | `0` / `dev` | **删掉**。它们只在 `AUTH_BYPASS` 或 `AUTH_DEBUG` 打开时用于伪造身份；现在两者都是 0，无害，但留着等于给「某天误开一次开关」预备好一个可用身份 |
| S3 | `REVIEW_REQUIRED` | `0`（直写主库） | 产品决定。上线初期建议 `1`（进待审队列） |
| S4 | 依赖服务都指向 **beta** | `betabts.cn.ndhy.com` / `*.beta.ndaeweb.com` / `uc-gateway.beta…`，而 `UC_ENV=preproduction` | 确认这是**有意的搭配**（预生产对 beta 是常见做法），不是从 dev 复制时漏改 |
| S5 | `APP_ENV` 未配 | 靠 `SDP_ENV_NAME` 回落 | profile 解析顺序是 `APP_ENV > SDP_ENV_NAME > SDP_ENV > development`。**若 SDP 注入的 `SDP_ENV_NAME` 不等于 `preproduction`，就会去拉错 profile（或 403），然后静默退回代码默认值。** 本地无法验证注入值，**部署后第一件事看 `/health` 的 `config.profile` 是不是 `preproduction`、`config.source` 是不是 `remote`** |

#### 已正确，不用动

`AUTH_BYPASS=0`、`AUTH_DEBUG=0`、`SERVE_DEV_UI=0`、`KG_REGION=CN`、`API_HOST=0.0.0.0`、
LLM 三件套（网关 + 模型 + token）齐备、BTS 四件套齐备、`OPENQ_AI_MANAGER` 有值、
`SDP_APP_ID` 是 BTS/UC 侧的应用 id（与颗粒 appId 不同，正常）。
`DATABASE_URL` 指向 `preproduction_pgsql_bcs_ai_agent`，与 §1.2 的共用库一致。

> 密码里含 `!`，URL 里合法、psycopg 能解析，但**在 shell 里手工拼这个连接串时要用单引号**，
> 否则 bash 的 history expansion 会吃掉它。

### 2.2c 出站 TLS 证书：**全部公网可信，不需要 CA bundle**（2026-08-20 实测）

用 `certifi` 的信任库（= 干净镜像里 httpx 默认用的那份，**刻意不用本机 OS 信任库**，
否则公司电脑装了内网 CA 会给出假的「可信」）逐个握手：

| 域名 | 签发者 | 到期 | 用途 |
|---|---|---|---|
| `betabts.cn.ndhy.com` | ZeroSSL GmbH | 2026-10-13 | BTS 取 token + 业务调用 |
| `uc-gateway.beta.cn.ndhy.com` | ZeroSSL GmbH | 2026-10-12 | UC 鉴权 |
| `uc-component.beta.cn.ndhy.com` | ZeroSSL GmbH | 2026-10-12 | UC 组件 |
| `ai-manager-v2.beta.ndaeweb.com` | TrustAsia | 2027-03-12 | 用户画像 / 记忆 |
| `e-ai-frontend.beta.ndaeweb.com` | TrustAsia | 2027-03-12 | `E_AI_SPACE` |
| `ai-gateway.aiae.ndhy.com` | TrustAsia | 2026-11-02 | LLM 网关 |
| `voced-position-kg.beta.ndaeweb.com` | TrustAsia | 2027-03-12 | **本服务准备绑的域名** |

**结论：`VERIFY_TLS` 保持默认 `1`、`TLS_CA_BUNDLE` 不用配。**（§2.2b 的 B2 解除。）
本地 `.env` 里那个 `VERIFY_TLS=0` 是历史遗留，**不要抄到预生产**。

> 复查节奏：ZeroSSL 那三张 2026-10 到期。证书换发不需要我们改配置（公网 CA 链不变），
> 但如果某天换成内网自签，症状是**所有出站调用突然 SSL 错误**——届时才需要 `TLS_CA_BUNDLE`。

### 2.2d 跨域怎么配（域名 `https://voced-position-kg.beta.ndaeweb.com`）

当前 `main.py` 的 CORS kwargs 是 `allow_credentials=True` + `allow_methods/headers/expose_headers=["*"]`，
CORS 中间件最后 add（包在鉴权外层）。**实测结论如下。**

#### 自定义请求头：已经全支持，不用改代码

Starlette 的 `allow_headers=["*"]` 语义是**回显浏览器 `Access-Control-Request-Headers` 里的那些头**，
不是回一个字面 `*`，所以它与 `allow_credentials=True` 兼容（不像 `allow_origins=["*"]` 会冲突）。
实测预检：

```
Access-Control-Request-Headers: authorization,sdp-app-id,sdp-biz-type,x-request-id,content-type
→ 200
  Access-Control-Allow-Headers: authorization,sdp-app-id,sdp-biz-type,x-request-id,content-type
  Access-Control-Allow-Credentials: true
  Access-Control-Allow-Methods: DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT
  Access-Control-Max-Age: 600
  Vary: Origin
```

`sdp-app-id` / `sdp-biz-type` 原样通过。**新增任何 `sdp-*` / `x-*` 头都不需要改服务端**——
这正是用 `*` 而不是枚举白名单的好处：对方加一个头我们不用跟着发版。

服务端自己也读 `sdp-app-id`（`api/auth.py:189`、`uc/client.py`、`bts/client.py`），口径一致。

#### `CORS_ORIGINS` 填什么：**四种写错的方式，全都静默失败**

`allow_origins` 是**字符串精确相等**匹配 `Origin` 请求头，而 `Origin` 头永远是
`scheme://host[:port]`——没有路径、没有结尾斜杠。实测（照搬 `main.py` 的 kwargs 逻辑）：

| `CORS_ORIGINS` | 预检 | 结果 |
|---|---|---|
| `*` | 200 | ✅ 走 `allow_origin_regex=".*"`，回显具体 Origin |
| `https://voced-position-kg.beta.ndaeweb.com` | 200 | ✅ **正确写法** |
| `https://voced-position-kg.beta.ndaeweb.com/` | **400** | ★ 多一个斜杠就不匹配 |
| `voced-position-kg.beta.ndaeweb.com` | **400** | ★ 少了 scheme |
| `http://voced-position-kg.beta.ndaeweb.com` | **400** | ★ scheme 不同算不同 origin |
| `https://a.example.com,https://b.example.com` | 200 | ✅ 逗号分隔多个 |

失败时**响应里干脆没有 `Access-Control-Allow-Origin`**，浏览器只报「跨域」，
看不到「你把域名写错了」——所以这一格是"要么对、要么查半天"。

#### 关键一问：`CORS_ORIGINS` 要填的是**前端的 origin**，不是 API 自己的域名

同源请求不走 CORS。`https://voced-position-kg.beta.ndaeweb.com` 是**API 的**域名：

- 如果学员端/管理台页面也部署在这个域名下（同源）→ **CORS 根本不参与**，
  `CORS_ORIGINS` 填什么都无所谓。但预生产 `SERVE_DEV_UI=0`，本服务不吐页面，
  所以只有把前端静态资源也挂到同一域名（不同路径）才成立。
- 如果前端在别的域名（大概率）→ **要填前端那个 origin**，把 API 自己的域名填进去没有用。

> **待确认：前端页面部署在哪个域名？** 这决定 `CORS_ORIGINS` 的值。
> 域名没定之前先留 `*` 是可以的（当前就是），定了立刻收窄。

#### 收窄前值得知道的风险量级

现在 `*` + `allow_credentials=True` 会回显任意 Origin 并允许带凭据。听起来很危险，
但本服务的身份是 `Authorization: MAC …`，**由前端 JS 主动加，不是 cookie 自动携带**——
恶意站点拿不到别人的 token，所以实际可利用面比"允许所有来源 + cookie"小得多。
仍然建议收窄：`allow_credentials=True` 同时也允许 cookie 通道，且这是预生产。

#### 两个还没验证的点（绑域名后必须验）

1. **Kong / ingress 会不会自己也加一份 CORS 头。** API 网关常自带 CORS 插件，
   两份 `Access-Control-Allow-Origin` 会让浏览器直接拒收（规范要求恰好一个）。
   绑完域名用 `curl -i -X OPTIONS -H "Origin: …" -H "Access-Control-Request-Method: GET"`
   打真实域名，**数一下 ACAO 出现了几次**。
2. **预检会不会被网关拦在到达应用之前。** 本服务已经把 CORS 放在鉴权外层（`main.py:160` 那段
   注释说明了为什么），但如果 Kong 上还有一层鉴权插件，不带 `Authorization` 的 OPTIONS
   可能在网关就被 401，应用侧的正确配置根本没机会生效。

### 2.3 两个操作层面的坑（已在记忆里，这里重申）

- **`sdp config update` 有 bug**（0.3.4 / 0.4.0 / 0.4.1 均未修），症状 `envId may not be empty`。
  升级 CLI 后必须重打补丁：`dist/src/commands/config.js` 的 update 段 `path:` 下面加
  `query: { env_id: envId, label },`。本机 0.4.0 已打。
- 配置中心存的是**解析后的 kv**：写进去的 YAML 会按字母序重排、**注释全丢**。读回用
  `config fetch`（`config get` 返回 undefined）。
- 各环境的 `SDP_CLOUD_CONFIG_SECRET` 是否已创建，**从 CLI 无法判断**
  （`credential-get` 永远返回 null，`"***"` 只是掩码）。要开网页 `credential-get-web`。
  **预生产 secret 没建好，服务会以 `config.source=local` 起来、悄悄用代码默认值跑**——
  部署后第一件事就是看 `/health` 的 `config.source` 是不是 `remote`。

---

## 3. 预生产只连一个库：代码兼容性

**结论：完全兼容，不需要改代码。** 「基准库 / 开发库分离」纯粹是本地开发的安排：

| 事实 | 依据 |
|---|---|
| 运行路径只认一个 `DATABASE_URL` | `settings.py` 只读这一个；`client.py` 的连接池只建一个 |
| `BASELINE_RO_DATABASE_URL` 不在运行路径上 | 全仓 grep 只有 `scripts/serve_baseline_readonly.py` 读它，那是本地起只读实例的脚本 |
| 8090 那个「基准库实例」是本地脚本起的 | 同上；backend 里没有任何双库/读写分离逻辑 |
| 没有 replica / readonly 路由 | grep 无 `read_only` / `replica` / `readonly` 的连接分支（只读事务仅出现在检索助手方案里，尚未实现） |

迁移动作：**新仓库的 `.env.example` 里不要出现 `BASELINE_RO_DATABASE_URL`**，
`scripts/serve_baseline_readonly.py` 与 `scripts/init_baseline.py` 留在开发仓库，不跟着 backend 走。

> 顺带一条与此相关的运维约定：本地 8088 连 dev 库、8090 连基准库，**这是刻意的**，
> 迁移期间不要为了「统一」去改本地 `.env`。

---

## 4. backend 独立部署 & requirements 完整性

### 4.1 依赖剥离：已经干净（实测）

- **`backend/` 不 import 仓库根任何模块**：`grep -rnE "^\s*(from|import)\s+(crawlers|pipelines|schemas|data|reports|scripts|tests)\b" backend/` **零命中**。
- 7 处 `ROOT = parents[2/3]`，逐个看过，**没有一处是运行必需**：
  - `api/main.py:28` 把仓库根加进 `sys.path`（迁移后无意义，可留可删）
  - `api/main.py:132-134` `FRONTEND_DIR` / `SEED_DIR` / `SCHEMAS_DIR` —— 挂载前都有
    `.exists()` 守卫，缺失只是少几条静态路由，接口一个不少
  - `kg/paths.py`、`kg/pg_store/query.py`、`kg/pg_store/migrate.py` —— 采集/迁移工具用，
    不在 API 运行路径
  - `kg/neo4j_store/*` —— 见 C1，直接删
- `Dockerfile` 只 `COPY backend`，且**双保险删掉 `.env`**（它是 `override=True` 加载的，
  进了镜像会盖掉 SDP 注入的环境变量，线上改配置将不生效）。`.dockerignore` 也挡了 `.env`。

**自查动作（迁移后必做一次）**：把 `backend/` 单独拷到空目录，
`PYTHONPATH=<该目录> python -m uvicorn backend.api.main:app`，
验 `/health` 能连库、`/openapi.json` 200。

### 4.2 requirements：一处待补，一处误导

用 AST 扫了 `backend/**/*.py` 的全部第三方 import，逐个比对 `requirements.txt`：

| import | 状态 |
|---|---|
| fastapi / uvicorn / pydantic / dotenv / psycopg / psycopg_pool / httpx / yaml / pypdf / fitz / docx / langchain_core / langchain_openai / langgraph | ✅ 已登记 |
| `starlette` | ⚠️ **未显式登记**。它是 fastapi 的传递依赖，实际总会装上，但 `api/auth.py` 直接 import 它 —— 按「运行路径 import 到的包必须登记」的规矩应该补一行 `starlette>=0.40.0` |
| `neo4j` | ⚠️ requirements 里是**注释行** `# neo4j>=5.26.0`，而 `kg/neo4j_store/client.py` 裸 import 它。当前无人 import 该模块所以不炸（实测启动后 `sys.modules` 里没有 neo4j），但这是 C1 要删掉它的理由 |

另外三处**故意的 try/except 降级** import，包都已登记（这条最容易出线上事故——
本地装了但没登记的包，在干净镜像里会走进降级分支，功能悄悄失效且不报错）：
`pypdf` / `pymupdf` / `python-docx`（简历解析）。

### 4.3 Dockerfile 的一个待确认点

`FROM python:3.14-slim`，且注释写明 **base 必须 3.13+**：
`api/schemas_assessment.py` 的联合类型里混了字符串前向引用，3.12 的 pydantic 求值不了，
import 期就 `TypeError`。

**待确认**：SDP 的 PYTHON / K8S_IMAGE 颗粒是否允许自带 Dockerfile、还是套用平台标准
base image。若平台强制 3.11/3.12，要么申请换 base，要么改掉那处联合类型写法。
这件事**要在申请构建流水线之前问清楚**，否则会在第一次构建时才发现。

---

## 5. 执行顺序（建议）

1. **清理**：C1 / C2（删 neo4j_store、graph_store、query_cli），补 `starlette` 到 requirements，
   补 `kg_skill_category` 到表名占用清单
2. **确认 base image**（§4.3）—— 这是唯一可能推翻打包方案的未知项
3. `backend/` 整体搬到 `voced-position-kg`，跑一次 §4.1 的独立部署自查
4. **写 preproduction 配置**（§2.1），确认 secret 已创建（网页 `credential-get-web`）
5. 部署一版，**第一件事看 `/health`**：`config.source` 必须是 `remote`、
   `postgresql.ok=true`、`ai_gateway.enabled` 符合预期
6. 在 Pod 里跑 §1.2 的表名占用 SQL，确认零冲突后重启让 DDL 落地
7. **搬图数据**（§1.3），导入后跑那五条校验
8. 跑一遍闸门（`tests/e2e_robustness.py`、`scripts/verify_write_paths.py`、
   `verify_skill_name_exposed.py`、`verify_skill_key.py`、`verify_draft_leak.py`、
   `verify_draft_closure_4dim.py`、`verify_question_pairing.py`）——
   这些脚本留在开发仓库，指向预生产地址跑

## 6. 已知风险清单

| 风险 | 影响 | 缓解 |
|---|---|---|
| 共用库表名撞车 | 静默拿别人的表结构跑，只在预生产复现 | §1.2 的 SQL，部署前必跑 |
| SDP secret 没建好 | 服务用代码默认值起来，不报错 | 部署后看 `/health` 的 `config.source` |
| base image 版本低于 3.13 | 构建期 `TypeError`，服务起不来 | §4.3，构建前确认 |
| 图数据没搬 | 表建好了、页面全空，接口 200 | §1.3；判据是 `kg_node` 线上行 ≈ 5 万 |
| 刻度/分类校验没跑 | 数字悄悄错（高级技师判成入门、技能显示「待归类」） | §1.3 第 3 点的五条命令 |
| `AUTH_BYPASS` 配成 1 | 内容改动绕过审核直写主库 | §2.1；预生产必须 0 |
