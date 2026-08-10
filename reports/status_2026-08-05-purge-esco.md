# 状态 · 2026-08-05 · 清样例 + ESCO 官方扩量

## 动作

1. **删除样例污染**：`python -m pipelines.maintenance.purge_sample --also-esco-api-sample`  
   - 去掉 `MANUAL` / `manual_seed` 与旧 ESCO API 小样本  
2. **种子脚本**：`export_html_seed` 默认只写 JSON，**不再默认写入 kg.sqlite**（需 `--write-db`）  
3. **ESCO 官方 API harvest**（非爬站、限速）：  
   - 250 occupations · 4253 skills · 11625 occupation→skill relations  
4. **入库 + Neo4j `--clear` 全量重灌**

## 当前图库（kg.sqlite = Neo4j）

| 指标 | 值 |
| --- | --- |
| nodes | **5925** |
| edges | **86889**（全部 `official` / `REQUIRES`） |
| US / O\*NET | 1422 节点 |
| EU / ESCO | 4503 节点 |
| CN | **0**（样例已清，官方尚未入库） |
| major / course / credential | **0** |

## 未做

- 国内教育部专业 / 人社部职业  
- ESCO 门户 CSV 全量（约 3k 岗 + 1.3 万技能；当前为 API 扩量样本）  
- 课程、认证、跨库 same_as  

## 复现命令

```bash
python -m pipelines.maintenance.purge_sample --also-esco-api-sample
python -m pipelines.eu.download_esco --max-occupations 250 --max-skills 400
python -m pipelines.eu.ingest_esco
python -m pipelines.neo4j_store.migrate --clear
```
