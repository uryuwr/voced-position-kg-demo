# 中国区五维数据获取计划

> 目标：补齐 **专业 → 岗位 → 技能等级 → 课程 → 认证** 国内可溯源子图。  
> 原则：官方公开文件/清单优先；合规先于规模；样例不进正式库；AI 只做对齐与低置信补边。  
> **收录口径：** 见 `docs/职业教育数据范围.md`——**普通本科 + 高职/中职/职业本科**专业均收录；岗位大典作底库，推荐弱化 1/7/8 类；技能扩与专业就业相关的标准。  
> 对照：`schemas/sources.yaml`（MOE_CN / MOHRSS_CN / ONE_PLUS_X 现为 `review`）、`schemas/graph_schema.yaml`。

---

## 1. 五维 × 主数据源 × 获取方式

| 维度 | 主源 | 怎么拿 | 入库 `source_system` | 预期节点形态 |
| --- | --- | --- | --- | --- |
| **专业 major** | 教育部**职教三层目录** + **普通本科**专业目录（研究生后置） | 官网公开发布的 **PDF/Excel/通知附件** → `data/raw/CN/moe/` → 脚本解析 | `MOE_CN` | 节点含 `level` + `education_type`（vocational/general） |
| **岗位 occupation** | 人社部《中华人民共和国职业分类大典》及公开修订 | 官方 **PDF/公开解读材料** → `data/raw/CN/mohrss/` → 解析细类（优先小类/细类代码+名称） | `MOHRSS_CN` | 大典细类/职业 → occupation |
| **技能等级 skill_level** | **国家职业技能标准**（公开文本）+ 等级（五级/四级…或 L1–L4 映射） | 人社部/行业公示的标准 PDF 或目录页 **元数据**（名称、工种、链接） | `MOHRSS_CN` / 标准发布机关 | `技能名 · 等级` 组合节点；等级映射写入 `attrs.scale` |
| **课程 course** | 职业教育**专业教学标准**、国家精品在线开放课 / 公开课元数据 | 只收 **课名 + 专业/岗位关联 + 可访问 URL 或课标编号**；禁止付费全文 | `MOE_CN` / `OPEN_COURSE_CN`（新建） | 必须有 `source_url` 或官方编号 |
| **认证 credential** | **1+X** 证书目录、职业资格/技能等级证书**公开名单** | 教育部/人社部/试点院校联合公布的 **清单 Excel/PDF/网页表格** | `ONE_PLUS_X` / `MOHRSS_CN` | 证书名称 + 官方条目链接 |

### 主链边（缺什么、从哪来）

| 关系 | 理想证据 | 拿不到时 |
| --- | --- | --- |
| `prepares_for` 专业→岗位 | 专业简介「就业面向」、高职专业教学标准就业岗位表述 | AI 读官方描述出候选边 + `ai_inferred` + 原文 evidence；抽检后入库 |
| `requires` 岗位→技能等级 | 国家职业技能标准中的职业功能/技能要求 | 标准 PDF 结构化抽取；无法结构化则试点域人工/半自动 |
| `covers` 专业→技能 | 课标「规格/能力」条目 | 规则映射 + AI 辅助，标 `derived`/`ai_inferred` |
| `taught_by` 技能→课程 | 课标课程设置、公开课简介中的能力点 | 仅白名单课程；无 URL 不入库 |
| `leads_to` / `recognized_by` / `articulates_to` | 1+X 与专业/岗位对应关系公开表、证书面向职业 | 清单字段直接建边；模糊对应走 AI+抽检 |

**不做：** 招聘站批量扒「专业就业方向」、商业课站/题库、灰产「大典数据库」、无 license 的第三方全量包。

---

## 2. 合规闸门（开干前必过）

每个源在 `schemas/sources.yaml` 中更新为 `allowed` 前，完成一张行记录：

| 字段 | 内容 |
| --- | --- |
| 源名称与 URL | 落地页 + 文件直链（若有） |
| 文件类型 | PDF / Excel / HTML 表 |
| 发布机关 | 教育部 / 人社部 / … |
| 转载/使用说明 | 摘录原文要点 |
| robots / 是否需登录 | 结论 |
| 建议 `license` 文案 | 写入节点的 license 字段 |
| 结论 | `allowed` / 仅人工摘录 / 放弃 |

国内源当前为 **`review`**：第一批文件由**人工下载**放入 `data/raw/CN/...`，脚本只解析本地文件，避免自动爬站踩线。

---

## 3. 落地形态（仓库约定）

```
data/raw/CN/
  moe/           # 专业目录、专业教学标准（原始文件 + README 记录出处 URL 与日期）
  mohrss/        # 大典、职业技能标准目录
  credential/    # 1+X、职业资格公开名单
  course/        # 公开课/课标元数据导出
data/staging/CN/ # 解析中间表（CSV/JSONL）
data/graph/kg.sqlite
pipelines/cn/
  ingest_majors.py
  ingest_occupations.py
  ingest_skill_standards.py
  ingest_courses.py
  ingest_credentials.py
  link_prepares_for.py    # 专→岗（规则 + 可选 AI）
reports/cn_ingest_*.json
```

节点/边字段：与现有 `graph_store` 一致；`region=CN`；`confidence ∈ {official, derived, ai_inferred}`（**禁止**再把试点样例标成 official）。

---

## 4. 分阶段执行（一个一个来）

### Phase CN-0 · 合规与目录（0.5–1 天）

- [ ] 锁定 5 个「第一批必下文件」清单（专业目录最新版 + 大典公开版 + 1+X 最新名单 + 1 份 IT 类职业技能标准 + 1 份高职计算机类专业教学标准）
- [ ] 更新 `sources.yaml` 对应条目备注与状态
- [ ] `data/raw/CN/**/README.md`：每个文件一行「URL · 下载日 · 文件名 · 哈希」

### Phase CN-1 · 专业轴（1–2 天）★ 优先

- [x] 解析教育部专业目录 → `major`（职教 2021 + 2025 增补 + 本科 2026）
- [x] 主键 `level:code` 去重；脚本 `python -m pipelines.cn.ingest_majors`
- [ ] Admin：`region=CN, type=major, depth=0` 可浏览（需 API 重启后验）

**交付：** `nodes` 中 CN major ≫ 0，且 `source_system=MOE_CN`。  
**报告：** `reports/ingest_cn_majors.json`

### Phase CN-2 · 岗位轴（2–3 天）

- [x] 大典 PDF 表格/结构化抽取 → occupation（细类优先）
- [x] 保留大典代码于 `source_id` / `attrs`
- [x] 与 Phase CN-1 暂不强制全量连边

**交付：** CN occupation 可查询；可与欧/美岗位日后 `same_as`（本阶段不做跨库）。  
**报告：** `reports/ingest_cn_occupations.json`（1639 细类，`MOHRSS_CN`）  
**命令：** `python -m pipelines.cn.ingest_occupations`

### Phase CN-3 · 技能等级（2–4 天，可先 IT/AI 试点）

- [x] 选定试点工种/职业（计算机程序设计员、人工智能训练师）
- [x] 从标准中抽「技能要求」→ `skill_level`（名称 · 等级；权重表）
- [x] `requires`：试点岗位 → 技能等级（`official`）

**交付：** 至少 **1 条完整「岗→技」可溯源链**（中国源）。  
**报告：** `reports/ingest_cn_skill_standards.json`  
**命令：** `python -m pipelines.cn.ingest_skill_standards`

### Phase CN-4 · 认证（1–2 天）

- [x] 1+X 职业技能等级证书公开名单入库 → `credential`（一至四批，去重约 470）
- [ ] 有「面向职业/专业」字段则写 `recognized_by` / `articulates_to`（名单无此字段，后置）

**报告：** `reports/ingest_cn_credentials.json`  
**命令：** `python -m pipelines.cn.ingest_credentials`

### Phase CN-5 · 课程（2–3 天）

- [x] 专业教学标准中的课程设置 → course（高职专科 IT 试点 6 专业）
- [ ] 可选：国家精品课元数据（必须有 URL）
- [x] 专业—课程 `related_to`（课标）；`taught_by`/`covers` 待技能细条后补

**报告：** `reports/ingest_cn_courses.json`  
**命令：** `python -m pipelines.cn.ingest_courses`

### Phase CN-6 · 主链闭环试点（2–3 天）

范围建议（可调）：

- **10 个高职/本科 IT 相关专业**  
- **15–20 个大典相关职业**  
- **技能 L 若干 + 课程 30–50 + 认证 10–20**  

边优先级：

1. 课标/清单里写死的 → `official`/`derived`  
2. 专→岗：就业面向文本 → 规则 + AI 候选 + **人工抽检表**（抽检通过才批量入）  
3. 禁止无 evidence 的 AI 边进 Neo4j 展示层（可先停在 staging）

**交付：** Admin 中国区可走通  
`专业 →(1–2 跳)→ 岗位 → 技能`，部分到课/证。

### Phase CN-7 · 扩量与质检（持续）

- [ ] 专业/岗位全量目录稳定更新脚本（版本年 diff）  
- [ ] 覆盖率报表：CN × 五维 节点数、边数、official 占比  
- [ ] 断链、孤立 major、无 source_url 的 course 扫描  

---

## 5. 人机分工

| 角色 | 职责 |
| --- | --- |
| **人** | 确认官方文件、下载落盘、1+X/课标字段是否可建边、AI 边抽检 |
| **脚本** | 解析 PDF/Excel、校验、去重、写 SQLite/Neo4j、报告 |
| **AI** | 表头映射、专业就业面向→岗位候选、中文技能表述归并（输出候选 JSON） |

---

## 6. 验收标准（中国五维「齐」的定义）

**最小齐（MVP）：**

- [ ] 五类节点在 CN 均 **> 0**，且非 `manual_seed`  
- [ ] 至少存在路径：`major -prepares_for→ occupation -requires→ skill_level`（边可追溯）  
- [ ] 至少存在：`skill_level` 与 `course` 或 `credential` 之一的官方/课标边  
- [ ] Admin：地区=国内，类型=专业/岗位，查询非空且详情里有 source  

**完整齐（目标）：**

- 专业四层次目录覆盖 + 大典细类覆盖 + 技能标准规模化 + 1+X 主名单 + 课标课程规模化  
- 主链四类关系均有 `official|derived` 主体，AI 边占比可控并带抽检报告  

---

## 7. 建议启动顺序（执行时一次只开一个 Phase）

```
CN-0 合规清单与文件落盘
  → CN-1 教育部专业目录（立刻让「国内+专业」可用）
  → CN-2 大典岗位
  → CN-3 试点职业技能标准（岗→技）
  → CN-4 1+X 认证
  → CN-5 课标课程
  → CN-6 IT 试点主链闭环
  → CN-7 扩量与质检
```

**下一步若开工：** 从 **CN-0 + CN-1** 开始——你确认/提供教育部专业目录官方文件（或允许的公开下载链接），脚本只解析 `data/raw/CN/moe/` 本地文件并入库。
