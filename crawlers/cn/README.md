# 国内数据管道（CN）

合规要求见 `schemas/sources.yaml`、`docs/CN五维数据获取计划.md`。

## 已实现

### 专业目录 `ingest_majors`

```bash
python -m pipelines.cn.ingest_majors --dry-run
python -m pipelines.cn.ingest_majors
python -m pipelines.neo4j_store.migrate   # 或 --clear 全量重灌
```

原始文件：`data/raw/CN/moe/`（见该目录 `README.txt`）。

**跨文件重复：** 主键 `level:code`（层次+专业代码）。同层次同码后者覆盖；不同层次/不同码即使同名也分节点。详见 `ingest_majors.py` 模块注释。

### 岗位（大典）`ingest_occupations`

```bash
python -m pipelines.cn.ingest_occupations --dry-run
python -m pipelines.cn.ingest_occupations
python -m pipelines.neo4j_store.migrate
```

原始文件：`data/raw/CN/mohrss/*分类体系表*.pdf`（大典 2022 公示稿）。

**主键：** 细类代码 `N-NN-NN-NN`（如 `2-02-38-01`）。节点 `source_system=MOHRSS_CN`。  
attrs 含大/中/小类路径、GBM、绿色 `is_green` / 数字 `is_digital`。本阶段不建专→岗边。

### 技能等级（试点）`ingest_skill_standards`

```bash
python -m pipelines.cn.ingest_skill_standards --dry-run
python -m pipelines.cn.ingest_skill_standards
python -m pipelines.neo4j_store.migrate
```

原始文件：`data/raw/CN/skill_standards/`。  
试点：人工智能训练师 `4-04-05-05`、计算机程序设计员 `4-04-05-01`。  
技能要求权重表 → `skill_level` + `occupation -requires→ skill_level`。

### 认证（1+X）`ingest_credentials`

```bash
python -m pipelines.cn.ingest_credentials --dry-run
python -m pipelines.cn.ingest_credentials
python -m pipelines.neo4j_store.migrate
```

原始文件：`data/raw/CN/credential/`（一至四批名单）。  
节点 `source_system=ONE_PLUS_X`；本阶段只建证书节点，不建专/岗连边。

### 专→岗试点 `link_prepares_for`

```bash
python -m pipelines.cn.link_prepares_for --dry-run
python -m pipelines.cn.link_prepares_for
python -m pipelines.maintenance.tag_cn_scope   # 有边岗位抬升 recommend_tier
python -m pipelines.neo4j_store.migrate
```

种子：`data/staging/CN/prepares_for_pilot_it.json`（IT 领域人工维护，`confidence=derived`）。  
岗位推荐：`tag_cn_scope` 词表降权保安/保洁等；有 `prepares_for` 入边则升 primary。

### 课程 · 两层模型（见 `docs/课程资源定义.md`）

**A. 课目标（培养方案课目，不可学）** `ingest_courses`  
```bash
python -m pipelines.cn.ingest_courses
```
课标 PDF → `role=curriculum_catalog`（`playable=false`）

**B. 可学资源（必须有 source_url）** `ingest_open_courses`  
```bash
python -m pipelines.cn.ingest_open_courses
```
国家职教智慧教育平台 `getData` → 仿真/慕课/教材页/精品课等外链。

主路径展示应优先 **`learnable_resource`**。

## 待实现

1. 可学资源 **列表接口批量扩量**（首页接口仅精选约数十条）  
2. 课标 PDF 批量（已爬到 ~736 URL）+ 与可学资源对齐  
3. 技能标准规模化；专→岗扩；`taught_by`/`covers`
