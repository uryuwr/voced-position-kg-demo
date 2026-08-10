# HITL 待办（需人工介入）

> 无人值守落地已完成「可验收」口径（见 `reports/2026-08-05-CN五维覆盖率验收.md`）。  
> 下表仅剩**官方全量/更高质量**仍依赖人工的事项。

| ID | 事项 | 原因 | 建议动作 | 状态 |
| --- | --- | --- | --- | --- |
| H1 | 课标 PDF 个别 URL 失败 | 源站偶发 522 | 重跑 `python -m pipelines.cn.batch_download_moe_standards` | open（已下 736，残余可补） |
| H2 | 国家职业技能标准 | osta 公开 API 已自动：目录 702 + PDF 702 全下 + 解析入库（CN skill≈8919，requires 岗≈489） | 见下方；仅当后续下载被风控时再人工 | mostly_done |
| H3 | 智慧职教**全量**可学资源列表 | 首页 getData 仅精选 ~30 | 人工导出精品课 CSV 或开放列表接口 | open |
| H4 | 专→岗 AI/规则边抽检 | 无官方全表；`confidence=ai_inferred` | 抽检 `reports/*prepares*` 与图中 pending 边 | open |
| H5 | 可学资源 ↔ 课目标名称对齐 | 规则匹配有噪声 | 抽检 related_to 课目↔资源 | open |
| H6 | API 热更新 | 一般无需 | 改 max_nodes 等后重启 8088 | closed（当前 le=2000 已加载） |

## H2 人机协同（技能标准）

| 步骤 | 谁做 | 说明 |
| --- | --- | --- |
| 目录 702 条 | **自动** | `python -m pipelines.cn.harvest_osta_skill_api` → `data/raw/CN/skill_standards/osta_catalog.json` |
| PDF 批下 | **自动** | `python -m pipelines.cn.batch_download_osta_catalog_pdfs` → `osta_pdfs/` |
| 解析入库 | **自动** | `python -m pipelines.cn.ingest_skill_standards` |
| 滑块/登录拦截 | **你** | 打开 https://www.osta.org.cn/skillStandard 完成验证后创建 `reports/h2_login_ready.flag` 或回复「已登录」 |

列表页与 PDF 接口目前**多数情况无需登录**；只有被风控时才需要你。

## 续跑入口（会话中断用）

```bash
python -u -m pipelines.cn.run_full_landing          # 断点续跑
python -u -m pipelines.cn.run_full_landing --force  # 全量重跑
python -m pipelines.cn.harvest_osta_skill_api
python -m pipelines.cn.batch_download_osta_catalog_pdfs
```

详见 `docs/CN无人值守落地.md`。
