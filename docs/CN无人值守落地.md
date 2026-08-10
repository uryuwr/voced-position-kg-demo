# CN 五维无人值守落地

## 目标

完成 CN 五维节点 + 边（专→岗/岗→技/专→课/可学资源）可验收落地；无官方源处用规则/AI 边并记 HITL。

## 唯一入口（会话中断可续跑）

```bash
# 仓库根目录
python -u -m pipelines.cn.run_full_landing

# 从某步重跑（含该步）
python -u -m pipelines.cn.run_full_landing --from-step ingest_courses

# 忽略 checkpoint 全量重跑
python -u -m pipelines.cn.run_full_landing --force
```

## 进度与验收产物

| 文件 | 含义 |
| --- | --- |
| `reports/cn_full_landing.log` | 逐步日志 |
| `reports/cn_full_landing_progress.json` | 当前状态 |
| `reports/cn_full_landing_checkpoint.json` | 已成功步骤（断点） |
| `reports/cn_full_landing_summary.json` | 总结果 |
| `reports/YYYY-MM-DD-CN五维覆盖率验收.md` | 验收报告 |

## 步骤顺序

1. batch_download_moe  
2. batch_download_skill  
3. ingest_courses  
4. ingest_skill_standards  
5. ingest_open_courses  
6. link_prepares_for  
7. link_prepares_for_llm  
8. link_course_resources  
9. tag_cn_scope  
10. neo4j_migrate  
11. coverage_report  

已成功步骤默认跳过；失败会记入 checkpoint.failed 并继续后续步骤。

## Agent 约定

- 用户目标未验收完成前，**不得因旁支问题（UI/空图等）永久停主线**；旁支可顺手修，但主线须有后台编排进程。
- 会话压缩后：先读 `cn_full_landing_progress.json` / log，未 `done` 则再启 `run_full_landing`。
