# 职业教育知识图谱

**行业 → 专业 / 岗位 → 技能**（课/证数据保留，优先级后置）  
区划：CN / EU / US  

## 工程隔离

| 目录 | 职责 | 部署 |
| --- | --- | --- |
| **`backend/`** | **独立 API 服务**（对外交付） | **SDP / Docker 只发这个** |
| **`crawlers/`** | 离线采集入库 | 本地/任务机，不进 API 镜像 |
| **`frontend/`** | 个人自测 HTML | **不部署**；正式前端另接 `/v1/**` |
| `schemas/` | 本体与来源登记 | 可随 backend 镜像 |
| `data/` | 原始文件与图库 | 卷挂载 / 外部 PG |
| `pipelines/` | 旧命令兼容 shim | 不写新逻辑 |

正式前端 → 只调 **backend** 的 HTTP 接口（见 `/docs`、`docs/服务端数据与接口设计.md`）。

```
职业教育知识图谱/
├── backend/          # API + KG runtime
├── crawlers/         # 采集 / 入库
├── frontend/         # 自测页面
├── schemas/
├── data/
├── docs/
├── scripts/
├── reports/
└── pipelines/        # 兼容层（勿写新逻辑）
```

## 快速开始

```bash
python -m pip install -r requirements.txt

# Neo4j
$env:COMPOSE_PROJECT_NAME="voced-kg"   # PowerShell
docker compose -p voced-kg up -d

# 图数据 → Neo4j
python -m backend.kg.neo4j_store.migrate

# 启动 API（生产默认不挂自测页）
python -m uvicorn backend.api.main:app --host 0.0.0.0 --port 8088

# 本地要打开 HTML 自测时：
# $env:SERVE_DEV_UI="1"
```

| 地址 | 说明 |
| --- | --- |
| http://127.0.0.1:8088/ | 服务发现 JSON |
| http://127.0.0.1:8088/docs | **对外对接 OpenAPI** |
| http://127.0.0.1:8088/health | 健康检查 |
| http://127.0.0.1:8088/v1/** | 图谱/业务 API |
| `/dev` 等 | 仅 `SERVE_DEV_UI=1` 时（自测） |

## 采集示例

```bash
python -m crawlers.cn.ingest_boss_industry
python -m crawlers.cn.ingest_majors
python -u -m crawlers.cn.run_full_landing
```

## 设计文档

| 文档 | 内容 |
| --- | --- |
| `docs/服务端数据与接口设计.md` | 业务库 + REST |
| `docs/前后台能力对照与数据建模缺口.md` | 产品能力差距 |
| `docs/本期四维范围.md` | 行业·专业·岗位·技能 |
| `docs/CN知识图谱现状与待办.md` | 数据现状 |

## 原则

1. 官方批量 / 公开 API 优先；招聘站验证码走 Playwright 人机协同  
2. 每条节点/边：`source_url` + `license` + `confidence` + `fetched_at`  
3. **backend 不跑爬虫**；**crawlers 不写 HTTP 产品接口**  
