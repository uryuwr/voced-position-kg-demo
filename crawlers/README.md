# crawlers · 采集与入库

离线 **下载 / 解析 / 建边 / 报告**（CN / EU / US）。写入 `data/graph/kg.sqlite`，再由 backend 迁入 Neo4j。

## 结构

```
crawlers/
  cn/            # 国内：专业/岗位/技能/行业/课/证 + link_*
  eu/            # ESCO
  us/            # O*NET
  maintenance/   # 标签、清理
  seed/          # HTML 种子导出
  common/        # download 工具
```

## 常用命令（仓库根目录）

```bash
# 行业（Boss 公开接口）
python -m crawlers.cn.ingest_boss_industry

# 专业 / 岗位 / 技能
python -m crawlers.cn.ingest_majors
python -m crawlers.cn.ingest_occupations
python -m crawlers.cn.ingest_skill_standards

# 一键编排（CN）
python -u -m crawlers.cn.run_full_landing

# 同步到 Neo4j
python -m backend.kg.neo4j_store.migrate
```

## 兼容入口

旧命令 `python -m pipelines.cn.*` 仍可用（`pipelines/` 为薄 shim，转发到本目录）。

## 依赖

根目录 `requirements.txt`；PDF 解析需 `pymupdf`（若未装：`pip install pymupdf`）。Playwright 人机协同采集见各 `harvest_*_playwright.py`。
