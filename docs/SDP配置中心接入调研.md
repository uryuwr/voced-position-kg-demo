# 从 backend/.env 改读 SDP 配置中心 —— 改动点清单

调研日期：2026-08-14 · 参考实现：`D:\workspace\bcs-ai-agent`（`src/agent/core/sdp_config_client.py`，373 行）
状态：**仅调研，未改任何代码。**

前提：本仓库将转 private，`backend/` 最终整体迁到内网独立仓库 **`voced-position-kg`**（SDP 颗粒同名，已建，见第 9 条）。所以本文所有判断的落点是「搬过去之后还成不成立」，而不是「在当前仓库能不能跑」。已完成的 P1 修复（Dockerfile 只 COPY `backend/`、两种 build 上下文都通）正是为这次搬迁铺路 —— 迁过去后 backend 无论是仓库根还是子目录，构建都不用改。

## 一、bcs-ai-agent 是怎么做的

一句话：**启动时用一个 secret 去 Spring Cloud Config Server 拉一份配置，扁平化成大写下划线的环境变量写进 `os.environ`，后面所有配置读取照旧走环境变量。** 配置中心只是「环境变量的另一个来源」，业务代码零感知。

| 环节 | 做法 |
|---|---|
| 入口 | `src/agent/core/config.py:16-25`：模块顶部先 `load_dotenv(.env.development, override=False)`，再 `bootstrap_sdp_config()`，全程 `try/except` 包住；**在 `Settings` 类定义之前**执行 |
| 唯一 bootstrap 变量 | `SDP_CLOUD_CONFIG_SECRET`（颗粒独立账密）。有值才拉，没有就跳过、完全走本地 |
| 环境名 profile | `APP_ENV` > `SDP_ENV_NAME`（SDP K8s 标准注入）> `SDP_ENV` > `development`；解析结果回写 `APP_ENV` |
| 服务地址 | `https://sdp-cloud-config-server.sdp.101.com`，label `master`，四条路径依次 fallback（`{app}/{profile}/{label}` → `{app}/{profile}` → `{label}/{app}-{profile}.yml` → `{app}-{profile}.yml`） |
| 鉴权 | 双层：平台级 Basic Auth（**代码写死**）+ 两个 header —— `x-sdp-app-name`、`x-sdp-cloud-config-token = sha256(app:secret:ts:SALT).hex() + ":" + ts`；Portal 上的自定义配置文件名走 `x-sdp-cluster` |
| 响应解析 | JSON `propertySources` 倒序合并（靠前优先）→ 点分键还原成嵌套 → 再扁平成 `A_B_C` 大写键；也兼容纯 YAML/JSON。list/dict 值转 JSON 字符串 |
| 写入 | `override=True` 覆盖 `os.environ`，但 `BOOTSTRAP_ENV_KEYS`（secret / APP_ENV / SDP_ENV_NAME / SDP_ENV）永不被远端覆盖 |
| 失败处理 | `SDP_CONFIG_FAIL_FAST = False`，拉不到只 `logger.warning`，降级本地配置继续启动 |
| 依赖 | `httpx` + `pyyaml` |
| 验证工具 | `scripts/verify_sdp_config_source.py`：只放 secret + APP_ENV 进环境、不注入本地业务键，跑一次 bootstrap，看 `LLM_MODEL` / `DATABASE_URL` 等是否出现 —— 出现即证明确实来自远端 |

热更新：**没有**。只在进程启动拉一次，Portal 改完要重启 Pod。

## 二、本项目要改什么

### 1. 新增 `backend/sdp_config.py`（移植）

放 `settings.py` 同级，不建子包 —— 它是配置层不是业务层。整体可照抄，只改常量：

- `SDP_APP_NAME` → 本项目在 SDP 上的应用名（**待确认，见第 9 条**）
- `SDP_CLOUD_CONFIG_FILE` → Portal 上建的配置文件名，缺省 `application`
- URI / label / SALT / 平台 Basic 账密 / token 算法 —— 同一个 SDP 平台，**不要改**

### 2. `backend/settings.py` 的加载顺序 —— 本次唯一有语义冲突的改动

现状 `_load_env_files()`：仓库根 `.env`（`override=False`）→ `backend/.env`（**`override=True`**）。这个 `override=True` 是刻意的，CLAUDE.md 和 `tests/_e2e_server.py` 都围着它写。

问题：如果 bootstrap 在它之前跑，远端配置会被 `backend/.env` 全部盖回去，配置中心等于没接。所以必须 **bootstrap 放在 `_load_env_files()` 之后**，且远端 `override=True`。

由此产生优先级变化，需要拍板：

| 方案 | 优先级 | 代价 |
|---|---|---|
| **A（推荐）** 远端最高 | 远端 > `backend/.env` > 进程 env | 实际不冲突：线上镜像里根本没有 `.env`（Dockerfile 已 `rm`），本地开发不配 secret 就不拉。语义最简单 |
| B 本地最高 | `backend/.env` > 远端 > 进程 env | 线上误留一个 `.env` 就静默盖掉配置中心，正是 Dockerfile 注释里警告过的坑 |
| C 加开关 | `SDP_CONFIG_OVERRIDE` env 控制 | 多一个状态维度，排查时要先问「这台机器开没开」 |

无论选哪个，`BOOTSTRAP_ENV_KEYS` 那份保护名单要照搬（含本项目的 `SDP_CLOUD_CONFIG_SECRET`、`APP_ENV`、`SDP_ENV_NAME`）。

### 3. 确认 bootstrap 早于所有模块级常量求值

本项目的配置是**模块级常量**（`API_HOST = os.getenv(...)`），import 即固化，不像 bcs 那样有 `Settings` 类兜底。有 6 处绕过 `settings.py` 直接读环境：

| 文件 | 读的键 |
|---|---|
| `backend/api/main.py:128-130` | `SERVE_DEV_UI` / `CORS_ORIGINS` / `API_PORT` |
| `backend/api/main.py:1407-1408` | `API_HOST` / `API_PORT`（`__main__` 分支） |
| `backend/api/openapi_docs.py:243` | `API_PORT` |
| `backend/kg/pg_store/config.py:15,19,24` | `DATABASE_URL` / `SQLITE_PATH` / `KG_REGION` |
| `backend/kg/neo4j_store/config.py:12-15` | Neo4j 四项（已无引用者，可忽略） |
| `backend/settings.py:132` | `API_VERSION`（在函数体内，运行时读，不受影响） |

好消息：这些模块都靠 `import backend.settings` 触发加载（`pg_store/config.py:12` 有明确注释），所以 **bootstrap 只要写在 `settings.py` 模块体顶部就自动全覆盖**，不用改这 6 处。

但要实测两类直接入口：`python -m backend.kg.pg_store.migrate`、`python -m backend.api.main` —— 确认它们的 import 链确实先经过 `backend.settings`。

### 4. `backend/requirements.txt` 加 `pyyaml`

`httpx` 已有。配置中心正常返回 JSON `propertySources`，YAML 分支是 Portal 直传 `.yml` 时的兜底 —— 按「第一原则」宁可登记也别让它走进 `except` 静默降级。若确认只吃 JSON，就把 `_parse_config_payload` 的 yaml 分支一并删掉，别留没登记依赖的代码路径。

### 5. Portal 上的键名要平铺成大写下划线

扁平化规则是 `a.b.c` → `A_B_C`。本项目的键都是单层大写（`DATABASE_URL`、`LLM_BASE_URL`、`UC_API_HOST`、`BTS_*`、`KG_REGION`、`AUTH_BYPASS`、`REVIEW_REQUIRED`、`CORS_ORIGINS`、`OPENQ_AI_MANAGER`、`LEARNING_PLAN_PATH`、`API_VERSION`），所以 **Portal 里直接按这些名字平铺写**，别用 Spring 风格的 `database.url` —— 虽然也能转出 `DATABASE_URL`，但多一层心智负担且容易和 `database.url.pool` 之类撞名。

注意 `SERVE_DEV_UI` / `AUTH_BYPASS` 这类布尔：扁平化把 YAML 的 `true` 转成字符串 `"true"`，本项目 `_bool()` 认 `("1","true","yes","on")`，兼容。

### 6. 部署侧

- `backend/Dockerfile` **不用改**（已 `rm -f .env`，正好符合「镜像里不带配置」）
- SDP/K8s 注入 `SDP_CLOUD_CONFIG_SECRET`（**走平台密钥，不要写成明文 env**）；`SDP_ENV_NAME` 平台自动注入
- 其余业务变量从部署配置里删掉，改由配置中心下发

### 7. 可观测性（bcs 没做，建议补）

接完之后「这台实例的配置到底来自远端还是本地」会变成排查第一问。建议在 `GET /health` 或 `/v1/config` 加一个 `config_source: remote|local` 和拉取时刻 —— 否则配置中心改了没生效时，只能靠翻日志里那行 `sdp_config_bootstrap_ok`。

### 8. 文档同步

- `CLAUDE.md`「配置」章节：现在写的「真源是 `backend/.env`，`override=True` 会覆盖进程环境变量」在方案 A 下不再准确
- `backend/README.md` 配置表：加配置中心来源说明
- `backend/.env.example`：加 SDP 段（只有 `SDP_CLOUD_CONFIG_SECRET=` 和注释，**不要填值**）
- `tests/_e2e_server.py` 的模块 docstring：它改 `settings` 模块属性的做法仍然有效，但注释里的因果解释要更新

### 9. 前置条件 —— 已用 sdp-portal-cli 查清，大部分已就绪

SDP 颗粒 **`voced-position-kg`** 已存在，PYTHON / K8S_IMAGE，与 bcs-ai-agent 同型：

- appId `6a79477d81abbd168b76aed2`
- `SDP_APP_NAME` 即 `voced-position-kg`

| 环境 | `--env-type` | `--env-id`(oid) | 开通 | 配置中心账密 | 配置文件 |
|---|---|---|---|---|---|
| development | 1 | `6a79477f81abbd168b76aed4` | ✅ | ✅ 已创建 | ❌ 空 |
| test | 2 | `6a79477f81abbd168b76aed5` | ❌ 未开通 | — | — |
| preproduction | 5 | `6a79477f81abbd168b76aed6` | ✅ | ✅ 已创建 | ❌ 空 |
| product | 8 | `6a79477f81abbd168b76aed7` | ✅ | ✅ 已创建 | ❌ 空 |

### 9.1 development 配置已写入（2026-08-14 实操）

`backend/.env` 的 27 个键已同步进 development 的 `application` 文件（label `master`），`config fetch` 读回逐键核对 **27/27 一致，零丢失**。写入时做的 4 处调整：

| 项 | `.env` 原值 | 写入值 | 原因 |
|---|---|---|---|
| `SERVE_DEV_UI` | `1` | `0` | 镜像不含 `frontend/` |
| `openq-ai-manager` | 小写连字符 | `OPENQ_AI_MANAGER` | 扁平化只做 `upper()` 不转连字符，会得到 `OPENQ-AI-MANAGER`，而 Linux 环境变量名大小写敏感，`settings.py` 读不到 |
| `BTS_ACCOUNT` | 尾部 5 个空格 | 已 strip | 避免鉴权串带空格 |
| `NEO4J_*` / `SQLITE_PATH` | 有 | 不写入 | `neo4j_store` 已无引用者 |

`DATABASE_URL` 按原样写了 `localhost:5432` —— **容器里连不上**，上线前要换成 SDP 申请的 PG 实例地址。

### 9.2 踩到的四个坑（都会再遇到，记下来）

1. **`config update` 是 CLI v0.3.4 的 bug**。`dist/src/commands/config.js` 里它把 `envId` 用驼峰塞进 body，而服务端和其它所有命令都用 `query: { env_id }`，直接调必报 `envId may not be empty`。本机已打补丁（原文件备份 `D:\tmp\config.js.bak`），**升级 CLI 会被覆盖**
2. **`file-delete` 需要门户账号**，API Key 认证会被拒；而 `file-create` 不是 upsert（文件已存在直接报错）。所以改内容只能靠 `update`，绕不开第 1 条
3. **`config get` 返回 `undefined`，要用 `config fetch`**。`history` 也查不到这次写入（`total_elements: 0`）
4. **配置中心存的是解析后的 kv，不是原始文本**：写进去的 YAML 会被按字母序重排，**注释全部丢失**。所以别指望在配置里写说明，注释要留在 `.env.example` 或本文档里

### 9.3 还差的两步

1. **secret 取真值** —— `config credential-get` 无论带不带 `--show-secret` 都返回 `null`（拿 bcs-ai-agent 作对照验证过：它线上明明在用 secret，同样返回 `null`），不带参数时显示的 `"***"` 只是 CLI 掩码。**所以从 CLI 无法判断 secret 是否已创建**，只能 `config credential-get-web` 开网页看。没有它就没法做端到端拉取验证
2. **preproduction / product 的配置文件仍为空**，要等 dev 验证通过后再同步（值也不该照抄 dev）

`test` 环境未开通；测试环境要跑得先 `apps env-open`。

## 三、平台凭据是否由 SDP 自动注入 —— 用 sdp-portal-cli 实查

**结论：不会。SDP 在构建或部署时都不注入 `SDP_PLATFORM_CONFIG_USERNAME` / `SDP_PLATFORM_CONFIG_PASSWORD`。**

这两个名字压根不是 SDP 平台的契约名，是 bcs 作者自己起的 Python 常量名，把 Java 框架 nd-rest（`WafEnvironmentPostProcessor`）的内置默认值硬编码搬了过来。

实查证据（appId `6a46379d76c0c1729f188ee1` = bcs-ai-agent，开发环境）：

| 查什么 | 命令 | 结果 |
|---|---|---|
| Portal 对颗粒暴露的凭据字段 | `config credential-get` | 只有 `{application, env, secret}` 三个字段 —— 即 `SDP_CLOUD_CONFIG_SECRET` 的来源，**没有** platform username/password |
| 配置中心里存了什么 | `config get --label master --file-name application` | 39 个键，全是业务键（`DATABASE_URL` / `LLM_*` / `BCS_*` / `UC_*` / `BTS_*`…），**没有任何 `SDP_PLATFORM_*`**，也没有 `SDP_CLOUD_CONFIG_SECRET` 自己 |
| 构建阶段的可注入项 | `build config get PYTHON K8S_IMAGE` | 只有构建表单字段（版本号 / Git 地址 / 分支 / 基础镜像），无环境变量注入位 |
| 部署阶段 | `cluster create-config`、`cluster info` | 只有集群类型与实例信息，无环境变量清单 |
| 全仓库出现位置 | grep bcs-ai-agent | 只出现在 `sdp_config_client.py:51-52`、它的探针脚本和单测里；**任何部署文档、Portal 返回、配置中心内容都没有** |

顺带验证到两件事：

1. **配置中心的键就是大写下划线平铺的**（`DATABASE_URL: xxx`、`LLM_MODEL: glm-5.2`），证实第 5 条的建议可行 —— 直接按本项目现有 env 名写即可，不必用 Spring 点分风格
2. **`SDP_CLOUD_CONFIG_SECRET` 和 profile 也要手动注入**。bcs 的 `docs/部署-docker-run.md` 那张表里，「平台 / 本地 Docker」场景的最少注入就是 `SDP_CLOUD_CONFIG_SECRET` + `SDP_ENV_NAME`（或 `APP_ENV`）—— 平台场景也没省掉。配置文件名 `application`、label `master` 与代码常量完全对上

### 由此修正的安全判断

先前按「public 仓库」提的三方案已作废 —— 本仓库将转 private，且 backend 最终迁到内网的 `voced-position-kg`。结论变成：**照抄 bcs 的写法即可，平台 Basic 账密与 SALT 直接写死常量，不必外置。**

两个理由：

1. **权限模型上它们本就不是关键密钥。** 配置中心是双层鉴权，平台 Basic 只是第一道门，第二道 `x-sdp-cloud-config-token` 必须用每个颗粒各自的 secret 才算得出来。单独拿到平台 Basic 和 SALT 拉不到任何颗粒的配置
2. **仓库不再公开。** 外置这三个常量的唯一收益是防公网泄露；转 private 后这个收益归零，却要多背 3 个部署期变量和一条"缺值就静默降级"的排查路径

真正要守住的是 **secret 本身**：它绝不能进代码、进 `.env.example`、进任何文档，只能由 SDP 部署配置注入。⚠️ bcs-ai-agent 的 `README.md:415` 就把该颗粒的真实 secret 明文写进了仓库文档 —— 内网仓库也不该这么写，本项目别学。

## 四、工作量估计

| 项 | 量 |
|---|---|
| 移植 `sdp_config.py` + 常量改造 + 凭据外置 | ~380 行新文件 |
| `settings.py` 加载顺序 | ~15 行 |
| `requirements.txt` / `.env.example` / 三处文档 | 零散 |
| 验证脚本（移植 `verify_sdp_config_source.py`） | ~80 行 |
| Portal 侧建应用/配置/secret | 不在代码里 |

代码改动集中在两个文件，风险主要不在代码量，而在**优先级语义**（第 2 条）和**凭据外置**（第三节）这两个决策上。
