# 职业教育知识图谱 API

版本 0.6.3　·　75 个端点（另有 26 个内部接口未收录）

> 本文由 `scripts/openapi_to_md.py` 从 `/openapi.json` 生成，**不要手工编辑**。
> 契约的唯一真源是代码里的 Pydantic 模型与路由注解；改代码后重新生成即可。
>
> 只收录**前端页面实际调用**的接口；运维、离线工具、内部调试接口不在此列。

## 公共约定

**鉴权**　所有 `/v1/**` 端点都走 UC MAC Token，请求头：

| 请求头 | 必填 | 说明 |
| --- | --- | --- |
| `Authorization` | 是 | UC MAC Token；签名基于 `raw_path`（保留 %XX 编码） |
| `X-User-Name` | | 可选展示名，中文需 `encodeURIComponent` |
| `sdp-app-id` | 是 | 应用标识，由前端透传，服务端不写死 |

以下端点文档中不再重复列出这几项。

**状态可见性**　`published` 前后台都可见；`draft`/`disabled` 仅管理台可见；
`archived` 是逻辑删除，任何接口都不返回。

**技能等级**　产品档 1–5（1 了解 → 5 专家）。一个技能在库里是 5 个 `skill_level`
节点，读路径按 `attrs.skill_key` 聚合成逻辑 bundle；「要求哪一档」由边指向哪个
等级节点表达。

---

## 目录

- 前台 · 岗位探索与详情（21）
- 前台 · AI 诊断（11）
- 前台 · 学习路径（1）
- 前台 · 我的（3）
- 管理台 · 运营看板（2）
- 管理台 · 审核发布（5）
- 管理台 · 技能多档（10）
- 管理台 · 数据列表（8）
- 系统（1）
- 前台 · 图谱检索（8）
- 管理台 · 数据维护（5）
- 数据模型

## 前台 · 岗位探索与详情

### `GET` /v1/student/goal

**当前学习目标岗位**

对齐 state.goal；未设置返回 null。

**响应**：`GoalOut`

---

### `PUT` /v1/student/goal

**设定学习目标岗位**

对齐 setGoal：锁定目标并记成就 first_goal。

**请求体**：`GoalPutBody`

**响应**：`GoalOut`

---

### `DELETE` /v1/student/goal

**清除学习目标**

对齐 clearGoal。传 occupation_id 只删该目标，否则清空全部。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `occupation_id` | query | string |  | 只清除该岗位目标 |

**响应**：`ClearGoalOut`

---

### `GET` /v1/student/goal/diagnosed

**已诊断/锁定过的岗位列表**

「岗位学习与自适应路径」页的一级视图：列出该用户做过诊断或锁定过的岗位，每项带岗位基础信息（名称/职级/职责）、**当前匹配度**、**诊断时间**与测评次数。

点某一项后再用 `GET /goal/overview?occupation_id=...` 取该岗位的详情卡片。

**分页返回** `{items, total, page, page_size, pages}`：每换一次目标就多一条记录，合并/排序/切片都在 SQL 层完成。活跃目标置顶，其余按最近诊断时间倒序；刚锁定尚未测评的岗位 `match_score` 为 null，`plan_id` 未生成时为空串。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `page` | query | integer |  | ≥1，默认 `1` |
| `page_size` | query | integer |  | ≥1，≤100，默认 `12` |

**响应**：`DiagnosedOccupationListOut`

---

### `POST` /v1/student/goal/learning-plan

**基于诊断短板生成学习计划并推送到学习空间**

对应报告页「基于短板一键生成个人自适应学习计划」。

本服务按诊断结果编排出**阶段任务树**（技能按短板优先排序、按技能大类分阶段、有课程边的挂上可学资源），推送给学习空间服务，返回它的 `plan_id`。

**计划内容与学习进度都不在本服务**：这里只保存一行关联记录，同时把 `plan_id` 写进该次诊断报告的 `learning_plan_id`，两处都能查到。

**幂等**：一次诊断对应一条路径。同一个 `session_id` 重复调用不会产生新计划，返回 `created=false` 与原有 `plan_id`；重新测评会产生新会话，推送时自动带上换代关系，旧计划被归档并在 `superseded_plan_id` 里返回。

**没有本地兜底**：学习空间不可用时返回 502，不会发一个假的 `plan_id`——假 id 会让学员点进去看到空白页。前端需要按错误码给文案，并允许稍后重试（记录已存为 `push_status=failed`，重试不会产生重复计划）。

**请求体**：`LearningPlanBody`

**响应**：`LearningPlanCreatedOut`

---

### `GET` /v1/student/goal/learning-plans

**我的学习计划关联记录**

按岗位查该学员生成过的学习计划 id（内容在外部服务，这里只存关联）。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `occupation_id` | query | string |  |  |

**响应**：`LearningPlanItem` 数组

---

### `GET` /v1/student/goal/overview

**目标概览（当前目标 + 晋升路径 + 测评结果）**

「岗位学习与自适应路径」页顶部卡片的数据源，一次取齐三块：

- **当前活跃目标**：岗位名/职级/职责/归属专业、技能项数
- **下一级成长目标**：沿 `advances_to` 的下一级岗位，及进阶需补的关键技能
- **测评结果**：该用户针对**这个岗位**最近一次报告（匹配度/雷达/优势/短板）

三者都按岗位绑定，换目标即换整套数据。

`advances_to` 是 **1:N**：`next_levels` 给出全部向上方向，`next_level` 是其中第一条（兼容旧前端）。要看多跳完整链路用 `GET /v1/student/positions/progressions`。

注意：晋升边目前只覆盖约 5% 的岗位，多数情况下 `next_levels` 为空数组，前端应隐藏该区块而非报错。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `occupation_id` | query | string |  | 留空取当前活跃目标 |

**响应**：`GoalOverviewOut`

---

### `GET` /v1/student/goals

**我的全部目标（活跃 + 历史）**

一人可锁定多个岗位目标，其中至多一个 `status=active`。换目标时旧目标转为 `archived` 而非删除，其测评结果与进度仍可回看。

**响应**：`GoalItem` 数组

---

### `GET` /v1/student/industries

**行业列表（分页）**

探索筛选项；树形仍可用图检索 `GET /v1/industries/tree`。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `q` | query | string |  | 名称关键字 |
| `page` | query | integer |  | ≥1，默认 `1` |
| `page_size` | query | integer |  | ≥1，≤200，默认 `50` |
| `region` | query | string |  | 默认 CN |

**响应**：`IndustryListOut`

---

### `GET` /v1/student/meta/skill-categories

**技能类目字典**

**响应**：`SkillCategory` 数组

---

### `GET` /v1/student/meta/skill-levels

**技能等级字典 L1–L5**

对齐原型 SKILL_LEVEL_META：了解/掌握/熟练/精通/专家。

**响应**：`SkillLevelMeta` 数组

---

### `GET` /v1/student/positions

**岗位列表**

`q` 岗位名关键词、`industry_id` 具体行业，两者可单用也可叠加（AND）。

行业只按 **id** 筛，不做名字模糊匹配 —— 行业名里带斜杠（「互联网/AI」「电子/通信/半导体」），模糊匹配会串味。前端用法：`GET /v1/student/industries?q=关键词` 出下拉候选，选中后把 id 传进来。

岗位归属行业有两条路径，筛选与列表里回显的 `industries` **口径一致**：直连 `occupation -belongs_to→ industry`，以及经专业两跳 `occupation ←prepares_for- major -belongs_to→ industry`。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `q` | query | string |  | 岗位名关键词，模糊匹配 |
| `industry_id` | query | string |  | 行业节点 id（精确匹配）；候选见 `GET /v1/student/industries?q=` |
| `page` | query | integer |  | ≥1，默认 `1` |
| `page_size` | query | integer |  | ≥1，≤100，默认 `20` |
| `region` | query | string |  |  |

**响应**：`PositionListOut`

---

### `GET` /v1/student/positions/courses

**岗位相关课程（按技能分组）**

沿 occupation -requires-> skill_level -taught_by-> course 聚合课程资源。

**与岗位详情接口相互独立**：详情接口不返回课程，本接口专供「岗位相关课程」卡片。

`kind` 区分资源性质：`real` = 平台真实课程（带 `learner_count` 可判热度）；`landing` = 检索入口（课程库无覆盖时的兜底，点开是搜索页而非课程）。前端务必分开展示，否则「有 N 门课」会掩盖资源质量差异。

**课标目录条目默认不返回**（`include_catalog=false`）：那批 MOE_CN 课程的链接全指向教育部「专业教学标准」大类列表页，与具体技能无关，对学员没有意义。`catalog_count` 仍会给出数量、`catalog_hidden` 标记是否被隐藏，便于观察数据缺口。要看课标体系（管理台/专业维度）传 `include_catalog=true`。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | query | string | 是 | 岗位节点 id |
| `limit_per_skill` | query | integer |  | 每个技能最多返回几门课（≥1，≤20，默认 `5`） |
| `include_catalog` | query | boolean |  | 是否返回课标目录条目。默认 false —— 它们点开是专业教学标准目录页，不是课程（默认 `False`） |

**响应**：`PositionCoursesOut`

---

### `GET` /v1/student/positions/match

**岗位匹配度（岗位详情页用）**

**级联取数，按证据强度从高到低**，命中即返回，不做无谓计算：

| 优先级 | source | 来源 | 是否调模型 |
| --- | --- | --- | --- |
| 1 | `diagnosis` | 该用户对**这个岗位**最近一次诊断报告的 match_score | 否 |
| 2 | `assessment` | 测评沉淀的实测技能画像（biz_user_skill）现算 | 否 |
| 3 | `memory` | 五维记忆经图谱召回 + 模型定级推断 | **是**（约 5–10s） |
| 4 | `none` | 无任何证据 | 否 |

级联之前还有一道判定：该岗位**一项技能都没配要求档**时直接返回 `source=no_baseline`、`match_score=null` —— 没有基准就算不出达标率，此时显示的是数据缺口而不是学员水平。

级联之后还有一道**服务端降级**：岗位缺要求档的技能权重占比**超过 30%**（阈值是服务端常量 `PARTIAL_BASELINE_PCT`，不要在前端各定一份）时，`source` 与 `score_status` 都改成 `partial_baseline`，`match_score` 仍照给 —— 已配置的那部分是真实依据，但只依据 18% 的权重算出的 50% 会被学员读成「我匹配一半」，必须由服务端明确标成「仅供参考」。证据强度这时看 `estimated` / `diagnosis` / `profile` 三个字段。

做过诊断就直接用报告里的数——它由学员实际作答算出，比任何实时推断都准，也省掉一次模型调用。因此**列表页不再展示匹配度**（避免整页触发模型），只在进入岗位详情时按需计算。

算法与诊断报告同源：单项达标率 `min(用户档/要求档, 1)`，总分 `Σ(达标率×权重)/Σ权重×100`。`estimated=true` 表示是推断值而非实测。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `position_id` | query | string | 是 | 岗位节点 id（含冒号，用 query 传） |
| `limit` | query | integer |  | 参与比对的技能条数上限（≥1，≤200，默认 `50`） |
| `allow_memory` | query | boolean |  | 无实测证据时是否允许用五维记忆推断（会调模型，较慢）（默认 `True`） |

**响应**：`PositionMatchOut`

---

### `GET` /v1/student/positions/progressions

**岗位晋升链路（多条，可多跳）**

从该岗位出发沿 `advances_to` 展开出**全部**向上路径，每条可多跳（如 Java → 技术经理 → CTO），逐跳给出要补的技能。

**与岗位详情接口相互独立**，加卡片不动既有契约。

`advances_to` 是 **1:N**：一个岗位有多个向上方向（本方向纵深 / 管理路线 / 跨方向转型），`direction` 给出分类，可直接用作 tab 标题。早期本体误定为 1:1、读路径 `LIMIT 1`，导致 Java 有三条向上路径却只显示一条。

晋升边由 LLM 推断（`confidence=ai_inferred`），`evidence` 是判定依据。覆盖率仍低（约 5% 的岗位有晋升边），无数据时 `paths` 为空数组而非报错。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | query | string | 是 | 岗位节点 id |
| `max_depth` | query | integer |  | 最大跳数，超过多为数据噪音（≥1，≤6，默认 `4`） |
| `max_paths` | query | integer |  | 最多返回几条路径（≥1，≤50，默认 `12`） |

**响应**：`PositionProgressionsOut`

---

### `GET` /v1/student/positions/skill-composition

**岗位技能构成（query id，推荐）**

逻辑技能 + weight_sum；权重只认 requires 边。id 含冒号时用本接口。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | query | string | 是 | 岗位节点 id |

**响应**：`SkillCompositionOut`

---

### `GET` /v1/student/positions/{position_id}

**岗位详情 + 技能要求**

对齐 vPosition：岗位含 industries / counts；skills 默认按 skill_key 聚合为逻辑技能（含 levels / level_descriptions），前端无需再 group。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `position_id` | path | string | 是 |  |
| `aggregate` | query | boolean |  | True=逻辑技能聚合；False=skill_level 扁平行（默认 `True`） |

**响应**：`PositionDetailOut`

---

### `GET` /v1/student/professions

**专业列表（探索首页）**

对应原型「搜索专业 / 专业卡片列表」。底层 type=major。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `q` | query | string |  | 搜索专业 / 关键词 |
| `page` | query | integer |  | ≥1，默认 `1` |
| `page_size` | query | integer |  | ≥1，≤100，默认 `20` |
| `region` | query | string |  |  |

**响应**：`ProfessionListOut`

---

### `GET` /v1/student/professions/{profession_id}

**专业详情 + 岗位 + 成长阶梯**

对齐 vProfession：专业信息、对口岗位、ladder。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `profession_id` | path | string | 是 |  |

**响应**：`ProfessionDetailOut`

---

### `GET` /v1/student/skills

**技能库列表（分页，默认逻辑技能聚合）**

view=bundle（默认）：按 skill_key 聚合，一页一行逻辑技能；view=level：原始 skill_level 扁平行。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `q` | query | string |  |  |
| `page` | query | integer |  | ≥1，默认 `1` |
| `page_size` | query | integer |  | ≥1，≤100，默认 `20` |
| `region` | query | string |  |  |
| `view` | query | string |  | bundle \| level（默认 `bundle`） |
| `occupation_id` | query | string |  | 仅该岗位 requires 覆盖的技能 |
| `has_level` | query | string |  | 至少含该档，如 L3 |

**响应**：`SkillListOut`

---

### `GET` /v1/student/skills/bundles/{skill_key}

**逻辑技能详情（L1–L5 聚合）**

skill_key 或 bundle:{region}:{key}；返回 levels / level_descriptions / counts。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `skill_key` | path | string | 是 |  |
| `region` | query | string |  |  |

**响应**：`SkillOut`

---

## 前台 · AI 诊断

### `POST` /v1/student/assessment/sessions/questions/stream

**① 出题 · 一条长连接推完全部题目（SSE）**

建会话 → 解析简历 → 规划题数 → 生成并**陆续推送全部题目**。

事件顺序：`session` → `stage`(解析) → `plan`(含确定题数) → `stage`(出题) → `question` × N → `question_end`。

前端把 `question` 事件压进本地队列，一次展示一道；收到 `question_end` 即表示题目出完（它是服务端的确定信号，比按题数判断可靠——模型出题失败降级时实际条数可能少于计划）。

**请求体**：`StartBody`

**响应**：`text/event-stream`（SSE 长连接）

`text/event-stream`。每个事件的 `data` 是下列之一，按 `type` 区分：

- `session` —— SseSessionEvent
- `stage` —— SseStageEvent
- `plan` —— SsePlanEvent
- `question` —— SseQuestionEvent
- `question_end` —— SseQuestionEndEvent
- `error` —— SseErrorEvent

---

### `GET` /v1/student/assessment/sessions/{session_id}

**测评 · 当前状态（刷新恢复）**

三阶段状态 + 全部题目 + 已作答 + 下一道未答题。状态来自业务表（biz_assessment_question / biz_assessment_answer），刷新恢复只是两条普通查询。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `session_id` | path | integer | 是 | 测评会话 id（≥1） |
| `occupation_id` | query | string |  | 兜底目标岗位；会话已落库岗位时以库为准 |

**响应**：`AssessmentStateOut`

---

### `POST` /v1/student/assessment/sessions/{session_id}/answers

**② 答题 · 提交一题（即答即走）**

选择题**当场判分**（选项自带档位，纯查表）；问答题落库后交后台线程判分，接口立即返回——学员不该为了等模型判分卡在这一题上。

返回 `progress.grading` 表示还有几道在后台判分；结算接口会等它们收尾。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `session_id` | path | integer | 是 | 测评会话 id（≥1） |

**请求体**：`AnswerBody`

**响应**：`AnswerAcceptedOut`

---

### `POST` /v1/student/assessment/sessions/{session_id}/report/stream

**③ 结算 · 生成综合能力报告（SSE）**

等待后台判分收尾（推 `stage` 进度），再聚合实测结果与岗位标准，产出匹配度 / 双系列雷达 / 优势 / 短板，并落库到诊断报告。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `session_id` | path | integer | 是 | 测评会话 id（≥1） |
| `occupation_id` | query | string |  | 兜底目标岗位；会话已落库岗位时以库为准，此参数忽略 |

**响应**：`text/event-stream`（SSE 长连接）

`text/event-stream`。事件按 `type` 区分：`stage`（判分进度）→ `report`（完整报告）；异常走 `error`。

---

### `POST` /v1/student/diagnosis/chat/sessions

**开启对话测评会话**

对齐 vDiagChat：创建会话并返回首问。

**请求体**：`ChatSessionBody`

**响应**：`ChatSessionOut`

---

### `POST` /v1/student/diagnosis/chat/sessions/{session_id}/messages

**提交对话回答**

学员回复后规则打分并结束会话，返回报告。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `session_id` | path | integer | 是 |  |

**请求体**：`ChatMessageBody`

**响应**：`ChatMessageOut`

---

### `GET` /v1/student/diagnosis/report

**能力诊断报告**

对齐 vDiagReport：匹配度、雷达、缺口。可按 session_id 或最近一次。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `session_id` | query | integer |  |  |
| `occupation_id` | query | string |  | 无报告时按画像+岗位现算 |

**响应**：`DiagnosisReportOut`

---

### `POST` /v1/student/diagnosis/resume

**简历智能诊断**

对齐 vDiagResume：粘贴简历 → 规则解析技能 → 可选对标岗位出报告。

**请求体**：`ResumeDiagBody`

**响应**：`DiagnosisReportOut`

---

### `POST` /v1/student/diagnosis/resume/extract

**上传简历文件 · 仅抽取文本（不触发诊断）**

测评工作流的第 1 步需要的是**简历原文**，随后由工作流自己解析画像；而 `POST /diagnosis/resume/upload` 会直接跑完旧的一次性诊断流程，两者副作用不同，故单独提供一个只抽文本的入口（共用同一个解析器）。

支持 PDF / DOCX / TXT，≤20MB；扫描件类图片型 PDF 提取不到文字会返回 400。

**响应**：`ResumeExtractOut`

---

### `GET` /v1/student/diagnosis/resume/sample

**范例简历（一键体验用）**

对应原型「使用标准范例简历一键体验解析」。范例文本刻意使用库内真实存在的技能名（配料准备 / 搅拌操作 / 泵送操作 …），因此解析后能命中技能库并算出有意义的匹配度。

**响应**：`ResumeSampleOut`

---

### `POST` /v1/student/diagnosis/resume/upload

**上传简历文件诊断（PDF / DOCX / TXT）**

对应原型「拖拽简历文件到此处」：`multipart/form-data` 上传，服务端抽取文本后走与 `POST /diagnosis/resume` 相同的诊断流程。

- 支持 **PDF / DOCX / TXT**，单文件 ≤ 20MB
- PDF 优先 pypdf 抽取，失败回退 PyMuPDF；**扫描件/图片型 PDF 无法提取文字**，此时返回 400 并提示改用文本粘贴
- 未显式传 `target_occupation_id` 时自动取当前锁定目标岗位

返回结构与 `POST /diagnosis/resume` 一致，额外带 `source_file`。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `target_occupation_id` | query | string |  | 对标岗位 id；缺省取当前锁定目标 |

**响应**：`DiagnosisReportOut`

---

## 前台 · 学习路径

### `GET` /v1/student/learn/resources

**学习资源列表**

对齐资源卡片；当前映射 KG course 节点。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `skill_id` | query | string |  | 关联技能 id（提示用） |
| `q` | query | string |  | 资源标题关键字 |
| `page` | query | integer |  | ≥1，默认 `1` |
| `page_size` | query | integer |  | ≥1，≤100，默认 `20` |

**响应**：`ResourceListOut`

---

## 前台 · 我的

### `GET` /v1/student/me

**我的主页摘要**

对齐 vMe：目标、成长值、徽章、技能画像、当前路径。

**响应**：`MeOut`

---

### `GET` /v1/student/me/badges

**成就定义列表**

全部徽章配置；已解锁见 GET /me.badges。

**响应**：`BadgeDefOut` 数组

---

### `GET` /v1/student/me/skills

**我的技能画像**

**响应**：`UserSkillItem` 数组

---

## 管理台 · 运营看板

### `GET` /v1/admin/ai-gateway

**AI 网关就绪态（非 UC）**

**响应**：`AiGatewayOut`

---

### `GET` /v1/admin/dashboard/summary

**运营看板摘要**

**响应**：`AdminDashboardOut`

---

## 管理台 · 审核发布

### `GET` /v1/admin/changes

**待审变更列表**

队列内仅待审；通过/驳回后记录删除。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `dim_type` | query | string |  | 按四维类型过滤 |
| `limit` | query | integer |  | ≥1，≤200，默认 `50` |

**响应**：`ChangeOut` 数组

---

### `POST` /v1/admin/changes

**提交变更（默认直写；REVIEW_REQUIRED=1 时进待审）**

四维节点与边的 新建/编辑/删除/停用/发布 均走此接口。

- **REVIEW_REQUIRED=0（默认）**：服务端立即写主库，`status=applied`，发布仍受 BR 门禁；不入待审队列。
- **REVIEW_REQUIRED=1**：入待审，须 approve 后生效。

**请求体**：`ChangeSubmitBody`

**响应**：`ChangeOut`

---

### `POST` /v1/admin/changes/{change_id}/approve

**审核通过并生效**

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `change_id` | path | integer | 是 |  |

**响应**：`ChangeApprovedOut`

---

### `POST` /v1/admin/changes/{change_id}/reject

**驳回（删除待审记录）**

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `change_id` | path | integer | 是 |  |

**响应**：`ChangeRejectedOut`

---

### `GET` /v1/admin/edges/review

**低置信/AI 边抽检列表**

默认 confidence=ai_inferred；可筛 prepares_for / requires。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `confidence` | query | string |  | 默认 `ai_inferred` |
| `rel_type` | query | string |  |  |
| `page` | query | integer |  | ≥1，默认 `1` |
| `page_size` | query | integer |  | ≥1，≤100，默认 `20` |
| `region` | query | string |  | 默认 `CN` |

**响应**：`EdgeReviewListOut`

---

## 管理台 · 技能多档

### `GET` /v1/admin/skills

**逻辑技能列表（管理端，含草稿）**

按 skill_key 聚合。默认 scope=manage 可见 draft/disabled；可按 status=published|draft|disabled 筛选。含聚合 status 字段。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `q` | query | string |  |  |
| `page` | query | integer |  | ≥1，默认 `1` |
| `page_size` | query | integer |  | ≥1，≤100，默认 `20` |
| `region` | query | string |  | 默认 `CN` |
| `status` | query | string |  | published\|draft\|disabled；空=全部（不含 archived） |
| `has_level` | query | string |  |  |
| `occupation_id` | query | string |  |  |

**响应**：`SkillBundleListOut`

---

### `POST` /v1/admin/skills

**新建逻辑技能（一次多档，进待审）**

提交 skill_key + levels.L1..L5 对象 + occupation_links（含 weight）。审核通过后拆成 N 条 skill_level，requires 边写入岗位侧权重。不直接写库。

**请求体**：`SkillBundleBody`

**响应**：`ChangeOut`

---

### `POST` /v1/admin/skills/preview

**预览多档拆分结果（不写库、不进审）**

**请求体**：`SkillBundleBody`

**响应**：`SkillBundlePreviewOut`

---

### `PATCH` /v1/admin/skills/{skill_key}

**更新逻辑技能（多档/岗链，进待审）**

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `skill_key` | path | string | 是 |  |

**请求体**：`SkillBundleBody`

**响应**：`ChangeOut`

---

### `GET` /v1/admin/skills/{skill_key}

**查询逻辑技能详情（聚合读）**

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `skill_key` | path | string | 是 |  |
| `region` | query | string |  | 默认 `CN` |

**响应**：`SkillOut`

---

### `DELETE` /v1/admin/skills/{skill_key}

**删除逻辑技能（软删，L1–L5 一起）**

把该 `skill_key` 的**全部档位节点与关联边**标为 `archived`。是**软删**：`archived` 是逻辑删除，任何读接口都不返回，但记录还在。物理删会连岗位的 `requires` 边一起带走，误删找不回来。 边一定跟着节点一起归档 —— 只归档节点的话，`edge_published()` 过滤不掉那些边，图查询会画出指向不可见节点的断头箭头，管理台按边计数也会比详情页多。 **不阻止删除还在被岗位引用的技能**：响应里给 `occupations_affected`，由前端提示影响面，删不删是运营的判断。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `skill_key` | path | string | 是 |  |
| `region` | query | string |  | 地区（默认 `CN`） |

**响应**：`SkillDeletedOut`

---

### `GET` /v1/admin/skills/{skill_key}/prerequisites

**列出先修技能**

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `skill_key` | path | string | 是 |  |
| `region` | query | string |  | 默认 `CN` |

**响应**：`PrereqOut` 数组

---

### `POST` /v1/admin/skills/{skill_key}/prerequisites

**添加先修（无环校验）**

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `skill_key` | path | string | 是 |  |

**请求体**：`PrereqBody`

**响应**：`PrereqOut`

---

### `PUT` /v1/admin/skills/{skill_key}/prerequisites

**整体替换先修列表（无环）**

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `skill_key` | path | string | 是 |  |

**请求体**：`PrereqSetBody`

**响应**：`PrereqOut` 数组

---

### `DELETE` /v1/admin/skills/{skill_key}/prerequisites/{prereq_key}

**删除一条先修**

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `skill_key` | path | string | 是 |  |
| `prereq_key` | path | string | 是 |  |
| `region` | query | string |  | 默认 `CN` |

**响应**：`PrereqDeletedOut`

---

## 管理台 · 数据列表

### `GET` /v1/admin/composition

**技能构成（专业直连技能 / 岗位技能）**

同时服务**专业**与**岗位**的技能构成页，字段对齐管理端原型。

| 节点类型 | 关系 | 权重 |
| --- | --- | --- |
| `occupation` | `requires` | 有，可归一化 |
| `major` | `covers`（E4） | **无**，专业技能不做归一化 |

`node` 段为页面头部：所属行业 / 关联专业 / 职级 / 薪资 / 状态 / 版本 / 编码。
每项技能返回 `available_levels`（该技能全部档）与 `selected_level`（当前选中档），前端即可渲染 L1–L5 档位按钮并高亮选中项。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `node_id` | query | string | 是 | 专业或岗位的节点 id（含冒号，用 query 传） |

**响应**：`SkillCompositionAdminOut`

---

### `PUT` /v1/admin/composition

**技能构成 · 添加或改档（幂等）**

从已有技能中选一项加入构成，或改已有项的等级/权重。

「选中等级」由**边指向哪个等级节点**表达，因此改档实现为先删该 skill_key 的旧边再建新边——对同一 skill_key 重复调用是幂等的。

**一个技能只保留一个要求档**（高级天然含低级；同时挂多档会让管理台按边求和的权重与前台按 skill_key 聚合的权重对不上）。因此：
- `mode=add`（添加入口）：该技能已存在则返回 **409**，附 `current_level`
- `mode=set`（默认，改档入口）：直接替换档位/权重

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `node_id` | query | string | 是 | 专业或岗位的节点 id |
| `mode` | query | string |  | set=改档（默认，覆盖）；add=新增（已存在则 409）（默认 `set`） |

**请求体**：`CompositionSkillBody`

**响应**：`SkillCompositionAdminOut`

---

### `DELETE` /v1/admin/composition

**技能构成 · 移除一项技能**

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `node_id` | query | string | 是 | 专业或岗位的节点 id |
| `skill_key` | query | string | 是 | 要移除的逻辑技能名 |

**响应**：`SkillCompositionAdminOut`

---

### `POST` /v1/admin/composition/normalize

**技能构成 · 权重归一化（仅岗位）**

把该岗位所有技能权重**等比缩放**到和为 1.00 —— 等比而非均分，保留运营已设定的相对重要性；末位吸收舍入误差以保证精确为 1.00。

原权重全为空/0 时退化为均分（此时无从推断相对重要性）。
**专业技能不带权重，调用会返回 400。**

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `node_id` | query | string | 是 | 岗位节点 id |

**响应**：`SkillCompositionAdminOut`

---

### `GET` /v1/admin/composition/options

**技能构成 · 可选技能（支持按名称搜索）**

技能构成抽屉底部下拉的数据源：按 `skill_key` 聚合的已有技能。

- `q` 按**技能名模糊搜索**（技能上千条，下拉必须能搜）
- 每项附 `available_levels`（该技能已配齐的档位）与 `level_completeness`，前端据此决定 L1–L5 哪些档可选

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `q` | query | string |  | 技能名关键字，模糊匹配 |
| `region` | query | string |  | 区域，默认 CN（默认 `CN`） |
| `limit` | query | integer |  | ≥1，≤200，默认 `50` |

**响应**：`SkillOptionOut` 数组

---

### `GET` /v1/kg/edges

**边列表（分页）**

管理端边管理：按关系类型 / 端点节点 id / 端点名称检索。删节点后可按 `node_id` 查询，确认关联边是否已清空（total=0）。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `rel_type` | query | string |  | 关系类型：prepares_for\|requires\|belongs_to\|parent_of\|related_to\|… |
| `node_id` | query | string |  | 匹配 src_id 或 dst_id（核对某节点关联边） |
| `src_id` | query | string |  | 仅起点 id |
| `dst_id` | query | string |  | 仅终点 id |
| `q` | query | string |  | 端点名称或边 id 关键字 |
| `status` | query | string |  | 状态过滤：published（默认只返回这个）\| draft \| archived \| disabled。归档边只留在库里、默认不返回；要核对或恢复时显式传 archived |
| `scope` | query | string |  | manage=不限状态（管理端核对用）；缺省仅 published |
| `page` | query | integer |  | ≥1，默认 `1` |
| `page_size` | query | integer |  | ≥1，≤200，默认 `20` |

**响应**：`EdgeListResponse`

---

### `GET` /v1/kg/node-detail

**四维详情（行业 / 专业 / 岗位 / 技能）**

管理台详情面板一站式取数，按节点 `type` 返回对应结构。字段对齐 `backend.html` 管理端原型，逐项依据见 `docs/管理台详情接口-原型对照.md`。

| type | 返回段 |
| --- | --- |
| `industry` | `majors[]`（含各专业的岗位数）、`occupations[]`（直连岗位）、`counts` |
| `major` | `industries[]`、`occupations[]`（含 level / skill_count / weight_sum）、`aggregated_skills[]`（**按被引用岗位数倒序**，含 required_level 与 used_by[]） |
| `occupation` | `industries[]`、`majors[]`、`skills[]`（含 required_level / weight_pct / prereqs 先修 / levels 五档格）、`weight_sum` |
| `skill_level` | `levels[]`、`level_completeness`、`occupations[]`（引用它的岗位）、`prereqs[]` 先修、`unlocks[]` 后继 |

**可见状态**：published + draft + disabled；archived 不返回。

> 聚合技能的 `used_by` 按岗位去重——同一技能在一个岗位下有 L1–L5 多个节点，只保留该岗位的最高要求档，避免同名岗位重复出现。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | query | string | 是 | 节点全局 id（含冒号，故用 query 传） |

**响应**：`NodeDetailOut`

---

### `GET` /v1/kg/nodes

**四维管理列表 · 分页列出节点**

【对齐参考后台 backend.html / 本仓 Admin Table】

按**维度 type** 分页列出节点，供管理表格使用（非图探索）。

| 四维 | type 参数 |
| --- | --- |
| 行业 | `industry` |
| 专业 | `major` |
| 岗位 | `occupation` |
| 技能 | `skill_level` |

示例：
- 专业第 1 页：`GET /v1/kg/nodes?type=major&page=1&page_size=20`
- 岗位搜「软件」：`GET /v1/kg/nodes?type=occupation&q=软件&page=1`
- 行业：`type=industry`；技能：`type=skill_level`

返回 `items` + `total` + `page` + `page_size` + `total_pages`，前端可直接做分页器。

说明：旧 Admin 曾用 `GET /v1/graph/explore?q=*&type=major&depth=0` 凑列表且**无服务端分页**（最多 500 条客户端翻页）；**新对接请用本接口**。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `type` | query | string |  | 维度/节点类型（管理 Table 必传其一）。四维：industry\|major\|occupation\|skill_level；也可 course\|credential。不传则混列所有类型（一般不推荐） |
| `region` | query | string |  | 区域，默认 CN；all=不限 |
| `q` | query | string |  | 名称关键字模糊搜索（可选）；不传或空=该维度全量分页 |
| `status` | query | string |  | 按状态过滤：published\|disabled\|…；默认仅 published |
| `scope` | query | string |  | 默认仅已发布（图谱 Table/探索/学员同源）。scope=manage 时管理控制台可看全状态（含停用） |
| `page` | query | integer |  | 页码，从 1 开始（≥1，默认 `1`） |
| `page_size` | query | integer |  | 每页条数，默认 20（≥1，≤200，默认 `20`） |
| `include_counts` | query | boolean |  | True 时联读附加 counts（行业/专业/岗位）；岗位另带 industries（默认 `False`） |
| `order_by` | query | string |  | 排序：`created_desc` 按创建时间倒序（**新建的排最前**，scope=manage 时的默认） \| `sort_order` 人工序（前台/图谱默认）\| `name` 按名称。注意：新建节点 sort_order 为空，用人工序会被排到最后一页 |

**响应**：`NodeListResponse`

---

## 系统

### `GET` /v1/config

**前端配置（UC SDK 等，无需登录）**

对齐 bcs-ai-agent /api/v1/config；UC 项来自仓库根 .env。

**响应**：`FrontendConfigOut`

---

## 前台 · 图谱检索

### `GET` /v1/capability

**能力全景（专业 → 行业/岗位(层级) → 技能，渐进式）**

面向「能力体系结构化全景」的聚合读接口，一次返回（仅 published）：

| 层 | 关系 | 内容 |
| --- | --- | --- |
| 归属（上） | major -belongs_to-> industry | 该专业所属行业 |
| 岗位（中） | major -prepares_for-> occupation | 对口岗位，含 `level` 岗位层级(1..N)、`skill_count` |
| 技能（下） | occupation -requires-> skill_level | 每岗位技能明细，**默认不返回**，见 `include_skills` |
| 晋升链 | occupation -advances_to-> occupation | `progressions[]`，同岗位族内按 level 递进派生 |
| 共享技能 | — | `shared_skills[]`，被多个岗位共同要求，供侧栏展示（不画线） |

**渐进式展示约定**：默认视图只回答「这个专业有哪些岗位、怎么晋升」，技能是二级信息。
因此 `include_skills` 默认为 `false`——每个岗位只给 `skill_count` 角标，用户 hover 单个岗位时再调 `GET /v1/occupations/skills?occupation_id=...` 按需拉取，
避免一次把上百条技能边全画出来导致图不可读。

参数 `major` 与 `major_id` 二选一（`major_id` 优先）。`region` 枚举：`CN`|`EU`|`US`|`all`。

> 注意：`progressions` 依赖 `occupation.level`。当前库内岗位主要来自「职业分类大典」，该源不含职级维度，故多数专业返回空数组；待企业职级/招聘级别数据接入后自动生效。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `major` | query | string |  | 专业名称关键字（与 major_id 二选一） |
| `major_id` | query | string |  | 专业节点全局 id（优先于 major） |
| `region` | query | string |  | 区域枚举：CN \| EU \| US \| all(不限)；默认 CN |
| `limit_occupations` | query | integer |  | 岗位数上限（≥1，≤1000，默认 `200`） |
| `limit_skills_per_occ` | query | integer |  | 每个岗位返回技能数上限（仅 include_skills=true 时生效）（≥1，≤500，默认 `80`） |
| `include_skills` | query | boolean |  | 是否下发每个岗位的技能明细。默认 false（渐进式折叠）：`occupations[].skills` 为空数组，只给 `skill_count`；true 时恢复全量返回，适合导出/离线分析等一次取全的场景（默认 `False`） |
| `include_progression` | query | boolean |  | 是否返回岗位晋升链 progressions[]（advances_to，派生）（默认 `True`） |
| `shared_skill_min_occ` | query | integer |  | 共享技能阈值：被 >= N 个岗位共同要求的技能收进 shared_skills[]（按 occ_count 倒序）；0 = 不计算（可省一次技能明细扫描）（≥0，≤50，默认 `2`） |

**响应**：`CapabilityOut`

---

### `GET` /v1/industries/search

**行业模糊搜索（平铺，供选择行业的下拉框）**

行业**平铺**返回，不区分大类/子行业——交互上用户直接搜一个行业选中即可。

每项附 `major_count` / `occupation_count`，便于下拉里提示规模、避免用户选到空行业。
排序：名称前缀命中优先 → 专业数倒序 → 名称。`q` 为空时按专业数倒序列出。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `q` | query | string |  | 行业名关键字，模糊匹配；空=按规模列出 |
| `region` | query | string |  | 区域：CN \| EU \| US \| all；默认 CN |
| `limit` | query | integer |  | 返回条数上限（≥1，≤200，默认 `30`） |

**响应**：object[]

---

### `GET` /v1/industries/tree

**行业树**

industry 节点 + parent_of 边（父→子）。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `region` | query | string |  | 区域，默认 CN |
| `limit` | query | integer |  | 行业节点上限（≥1，≤2000，默认 `500`） |

**响应**：`IndustryTreeResponse`

---

### `GET` /v1/industry-graph

**行业关联图（行业 → 专业 → 岗位，只到岗位层）**

面向「选定一个行业 → 看三层关联图」的主视图接口。**只画到岗位层**，技能是二级信息，点岗位后另调 `GET /v1/occupation-skills-graph`。

与通用 `explore/expand` 的区别：按层组织（`layers.majors` / `layers.occupations`），前端不必自己判层；且由服务端按层截断并在 `meta.truncated` 告知，避免超大行业撑爆画布（专业数中位数 12，但最大 292）。

`layout=matrix` 时额外返回 `matrix`（热力图：行=专业、列=岗位）。
强度 `metric=skill_affinity`：该岗位的技能与**同专业其他对口岗位**的重合总量，即「这个岗位在多大程度上代表该专业的主流技能」。
（未采用「专业与岗位的共有技能数」，因为 `covers`（专业→技能）边当前为 0 条。）

`progressions` 为岗位晋升链，只含两端都在当前画布内的 `advances_to` 真边。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `industry_id` | query | string |  | 行业节点全局 id（优先于 industry） |
| `industry` | query | string |  | 行业名关键字（与 industry_id 二选一） |
| `region` | query | string |  | 区域：CN \| EU \| US \| all；默认 CN |
| `limit_majors` | query | integer |  | 专业层上限，按对口岗位数倒序截断（≥1，≤300，默认 `20`） |
| `limit_occupations_per_major` | query | integer |  | 每个专业取几个岗位，按技能数倒序（≥1，≤50，默认 `8`） |
| `layout` | query | string |  | 展示形态：layered=垂直分层图（默认）；matrix=热力图（附加 matrix 字段）（默认 `layered`） |

**响应**：`IndustryGraphOut`

---

### `GET` /v1/node

**节点详情（query id，推荐）**

`GET /v1/node?id=CN:major:…`，避免 path 中冒号被编码导致 UC MAC 校验失败。默认仅 published；管理端编辑请加 `scope=manage`。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | query | string | 是 | 节点全局 id |
| `include_counts` | query | boolean |  | 附加关联 counts（联读）（默认 `False`） |
| `include_links` | query | boolean |  | 附加 industry_ids/major_ids/occupation_ids（默认 `False`） |
| `scope` | query | string |  | scope=manage 时允许 draft/disabled，并默认带 link_ids |

**响应**：`KgNode`

---

### `GET` /v1/nodes/{node_id}

**节点详情**

按全局 id 取单节点档案。id 含冒号时建议用下方 query 接口。默认仅 published（BR-07）；`scope=manage` 可读草稿/停用（管理端编辑）。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `node_id` | path | string | 是 |  |
| `include_counts` | query | boolean |  | 附加关联 counts（联读）（默认 `False`） |
| `include_links` | query | boolean |  | 附加 industry_ids/major_ids/occupation_ids（编辑用）（默认 `False`） |
| `scope` | query | string |  | scope=manage 时允许 draft/disabled，并默认带 link_ids |

**响应**：`KgNode`

---

### `GET` /v1/occupation-skills-graph

**岗位技能图谱（按分类分区 + 前置关系）**

点击岗位后的二级视图：技能按 `category` 分区，区内给出前置关系箭头。

`categories[].key` 取自 `kg_node.category`，按国家职业技能标准的「职业功能」维度划分（安全与环保 / 作业准备 / 操作与加工 / 设备维护与检修 / 质量与检验 / 数据与信息 / 服务与业务 / 技术管理与创新 / 运营与管理 / 培训与指导）；未分类技能归入 `未分类` 分区并排在最后。

`prereqs` 来自 `kg_skill_prereq`，**只含两端都在本岗位技能集内的边**，避免画出指向岗位外技能的孤立箭头。方向为 `from`(先修) → `to`(后继)。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `occupation_id` | query | string | 是 | 岗位节点全局 id |
| `region` | query | string |  | 区域：CN \| EU \| US；默认 CN |
| `limit` | query | integer |  | 技能条数上限（≥1，≤500，默认 `200`） |

**响应**：`OccupationSkillsGraphOut`

---

### `GET` /v1/occupations/skills

**岗位 → 技能列表**

关系 requires。默认 `aggregate=true`：按 skill_key 聚合为逻辑技能 （含 levels / required_level / level_descriptions）；`aggregate=false` 返回 skill_level 扁平行（KgNodeWithEdge）。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `occupation_id` | query | string |  | 岗位节点全局 id |
| `q` | query | string |  | 岗位名称关键字 |
| `region` | query | string |  | 区域，默认 CN |
| `limit` | query | integer |  | 最多返回条数（≥1，≤200，默认 `50`） |
| `aggregate` | query | boolean |  | True=按 skill_key 聚合逻辑技能；False=skill_level 扁平行（默认 `True`） |

**响应**：any[]

---

## 管理台 · 数据维护

### `POST` /v1/kg/edges

**新建边**

请求体见 Schema `EdgeCreate`。默认 status=draft；src_id/dst_id 须已存在。

**请求体**：`EdgeCreate`

**响应**：`KgEdge`

---

### `DELETE` /v1/kg/edges/{edge_id}

**归档边（软删）**

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `edge_id` | path | string | 是 |  |

**响应**：`ArchiveEdgeResponse`

---

### `POST` /v1/kg/nodes

**新建节点**

请求体见 Schema `NodeCreate`。默认 status=draft。【鉴权】业务接口需 `Authorization: MAC ...`（UC 登录后由 SDK 生成）。开发可用 AUTH_BYPASS=1 + 头 `X-Test-Uid` / `X-Test-Uname`。可选 `X-User-Name`（encodeURIComponent）补充展示名。

**请求体**：`NodeCreate`

**响应**：`KgNode`

---

### `PATCH` /v1/kg/nodes/{node_id}

**编辑节点**

请求体见 Schema `NodePatch`；只传需要改的字段。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `node_id` | path | string | 是 |  |

**请求体**：`NodePatch`

**响应**：`KgNode`

---

### `DELETE` /v1/kg/nodes/{node_id}

**归档节点（软删）**

将 status 置为 archived，不物理删除。

**请求参数**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `node_id` | path | string | 是 |  |

**响应**：`KgNode`

---

## 数据模型

共 152 个模型，按名称排序。端点处的类型链接都指向这里。

### AdminDashboardOut

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `kg_nodes` | integer |  | 图节点总数 |
| `kg_edges` | integer |  | 图边总数 |
| `nodes_by_type` | map<string, integer> |  | 各类型节点数，键为节点类型 |
| `users_with_goal` | integer |  | 已锁定学习目标的用户数（默认 `0`） |
| `diagnosis_sessions` | integer |  | 诊断会话数（默认 `0`） |
| `learning_plans_pushed` | integer |  | 已成功推送到学习空间的学习计划数（路径本体不在本服务）（默认 `0`） |
| `pending_proposals` | integer |  | 待审提案数（默认 `0`） |

### AiGatewayOut

> AI 网关连通状态。内测环境网关常为空，`enabled=false` 时所有 AI 功能走规则兜底。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `enabled` | boolean | 是 | 网关是否就绪（地址、token、模型三者齐备） |
| `base_url` | string |  | 网关地址；未配置为 null |
| `model` | string |  | 模型名；未配置为 null |
| `has_token` | boolean | 是 | 是否已配置访问 token（不回显 token 本身） |

### AnswerAcceptedOut

> 答题回执。选择题当场出档位，开放题只回执不给分。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `accepted` | boolean | 是 | 是否已记录；校验失败走 400 而不是 false |
| `graded` | boolean | 是 | 是否已判分。选择题 true（纯查表）；开放题 false，判分在后台线程 |
| `index` | integer | 是 | 题号，与请求一致（≥0.0） |
| `level` | integer |  | 实测档位 1–5，仅 graded=true 时有值 |
| `progress` | `ProgressOut` | 是 | 提交后的最新进度 |

### AnswerBody

> 提交一题。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `index` | integer | 是 | 题号（question.index）（≥0.0） |
| `answer` | integer \| string | 是 | 选择题传选项 value（int）；开放题传作答文本（str） |

### AnswerRecordOut

> 已作答记录（状态恢复用）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `index` | integer | 是 | 题号（≥0.0） |
| `raw_answer` | string |  | 学员原始作答（选项 value 或文本） |
| `level` | integer |  | 判定档位 1–5 |
| `score` | integer |  | 判分得分 |
| `grade_status` | `pending` \| `graded` \| `failed` | 是 | pending=后台判分中；graded=已判；failed=判分失败（按未测处理） |
| `source` | string |  | 判分来源：choice=查表；llm=模型；rule=规则兜底 |
| `evidence_score` | number |  | 证据充分度 0–1，开放题判分置信度 |
| `capped` | boolean |  | 是否因证据不足被压档：作答说得很大但举证不足时不给高档 |
| `reason` | string |  | 判分理由（模型给出） |

### AppliedResult

> 变更实际落库的结果。
>
> 与 `ChangePayload` 同理——建普通节点、建技能 bundle、改边三条路径返回的键
> 各不相同，这里列出全部可能键（都是可选）并允许额外字段。
>
> `status=draft` + `gate` 一起出现时，表示写入成功但**发布门禁没过**，
> 节点停在草稿态——这不是失败，前台看不到而已，要看 `gate.failed` 补数据。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `node` | `KgNode` |  | 落库后的节点对象 |
| `nodes` | `KgNode`[] |  | 技能 bundle 一次建出的多个档位节点 |
| `linked_edges` | `KgEdge`[] |  | 自动建出的边 |
| `skill_bundle` | boolean |  | true=走的是技能 bundle 写入路径 |
| `skill_key` | string |  | 技能聚合主键 |
| `levels` | string[] \| object[] |  | 本次建出的档位码，如 `["L1","L3"]`。**曾误声明成 `list[dict]`** —— 写入其实成功了，但响应按模型校验时失败，接口回 400，管理台显示「新建失败」而库里已经有了，人就会重复提交。各档明细在 `nodes` / `bundle` 里。留成联合类型是有意的：这一处窄声明已经踩过一次，多一个分支不会让任何生产方失败，而窄一格就可能重演「写进去了却回 400」 |
| `bundle` | `SkillBundleBrief` |  | 聚合后的技能 bundle |
| `status` | string |  | 落库后的状态；draft 表示门禁未过，停在草稿 |
| `gate` | object |  | 发布门禁结果（同 PublishValidateOut）；仅未通过时出现 |
| `deleted` | boolean |  | 删除类变更是否已执行。**恒为布尔**——删除的条数看 `delete_result`。  这里曾经在物理删节点时被塞进一个 dict（`{node_id, nodes_deleted, edges_deleted}`），而驳回那条路给的是 `true`，同一字段两种形状，响应模型校验直接把整个删除接口打成 500。 |
| `delete_result` | `DeleteResult` |  | 删除实际影响的条数；仅物理删除时出现 |
| `note` | string |  | 人话补充说明。内容类动作（新建/编辑/技能构成）会说明「已存为草稿，发布请走 POST /v1/admin/publish/node」；停用会说明连带停用了几条关联边 |

### ArchiveEdgeResponse

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 边 ID |
| `status` | `archived` |  | 归档后状态（默认 `archived`） |

### AssessmentReportOut

> 综合能力报告。
>
> 两个匹配度，分母不同，**不要混用也不要相互换算**：
>
> - `match_score`：分母是岗位**可评分**技能的全部权重，未测到的按 0 分计入。答「这个
>   岗位你整体准备好了多少」，与岗位探索列表（`match_with_profile`）同源，可跨岗位横向
>   比较、可用于列表排序。覆盖率 `coverage` 是它的置信度说明。
> - `tested_match_score`：分母只有**实测**技能权重。答「你考过的部分掌握得怎样」，
>   只适合在报告详情页单点看；不同覆盖率之间不可比。
>
> 「可评分」= 岗位给了 1–5 的要求档。要求档缺失或越界的技能没有基准、算不出达标率，
> **分子分母都不计**（`items[].scorable=false`，汇总在 `no_baseline`）。一项都没有
> 可评分技能时 `match_score` 为 **null**、`score_status="no_baseline"` ——
> 前端必须显示「该岗位尚未配置能力要求，无法评分」，**不要显示 0%**：
> 0% 会被读成「完全不匹配」，而真相是数据缺口。库内 80% 的岗位目前是这个状态。
>
> **前端接入须知（2026-08 破坏性变更）**
> **`match_score` 已由必填 number 改为可空**（`float | None`）。以前可以假定一定有值，
> 现在不行了，`?? 0` / `|| 0` 这类兜底会把「没有评分依据」渲染成「完全不匹配」。
>
> - `null` ≠ `0`。`0` 是**算出来的结论**：有基准、有权重、也测过，只是一项都没达标。
>   `null` 是**算不出来**：岗位没配能力要求（`score_status="no_baseline"`）。
>   学员看到 0% 会理解成「我完全不行」，看到 null 该理解成「这里还没有依据」——
>   两种情绪完全不同，混淆一次就是产品事故。
> - 建议展示：`null` 时保留分数的位置与字号，改成虚线框 + 「? %」占位
>   （见 `frontend/student.html` 的 `.score.unknown`），旁边给
>   `score_status` 对应的说明文案；不要塌成一行普通文字，否则看不出这里本来有分数。
> - `score_status="partial_baseline"` 时**有分数但只能当参考**：数字照显示，
>   必须同时展示「仅供参考」与 `no_baseline_weight`，也不要拿它跨岗位排序。
> - 一句话结论直接用 `summary`：它已经按 `score_status` 分支写好了措辞，
>   不会出现「匹配度 None%」。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `session_id` | integer |  | 产生这份报告的诊断会话 id。**生成学习计划要用它**（`POST /v1/student/goal/learning-plan` 的必填入参）。  数据层一直有这个字段，但响应模型此前没声明，被 Pydantic 丢掉了，导致前端拿不到 session_id、生成按钮永久禁用（2026-08-18 修）。 |
| `channel` | string |  | 产生渠道：assessment / resume / chat / profile |
| `target_occupation_id` | string |  | 目标岗位节点 id |
| `target_occupation_name` | string |  | 目标岗位名 |
| `match_score` | number |  | 综合能力匹配度 0–100。分母是岗位**可评分**技能的全部权重（未测项按 0 分计入、缺要求档的项完全不计入），与岗位探索列表口径一致，可横向比较与排序。  **可空字段（2026-08 起）**：为 null 表示算不出来（岗位一项要求档都没配），原因见 score_status。**为 null 时不要显示 0%，也不要 `?? 0` 兜底** —— 0% 是「测过但一项都没达标」的结论，null 是「没有评分依据」，学员会把前者读成「我完全不行」。建议改用虚线框 +「? %」占位并给出说明文案。 |
| `score_status` | `ok` \| `partial_baseline` \| `no_skills` \| `no_baseline` \| `no_weight` \| `no_evidence` |  | match_score 为什么是这个值，决定前端显示数字、显示数字+提示、还是只显示文案。取值顺序与服务端词表 `config.SCORE_STATUSES` 同源：  \| 取值 \| 含义 \| match_score \| 建议展示 \| \| --- \| --- \| --- \| --- \| \| `ok` \| 有基准、有权重、也测到了，分数可用 \| 数字 \| 正常显示分数，可横向比较与排序 \| \| `partial_baseline` \| 配了一部分：缺要求档的权重**超过 30%**，分数只依据已配置的那部分算出 \| 数字 \| 分数照显示，**同时**标「仅供参考 · 该岗位 {no_baseline_weight}% 的能力要求待完善」；不要用于跨岗位排序 \| \| `no_skills` \| 该岗位尚未配置技能构成，无从算起 \| 0（无意义） \| 「该岗位技能构成待完善」，不显示 0% \| \| `no_baseline` \| 有技能构成但**一项要求档都没配** \| **null** \| 「? %」占位 + 「该岗位能力标准待完善，暂时无法评分」；**别引导学员去做诊断**，做了也算不出来 \| \| `no_weight` \| 可评分技能的权重全为 0（脏数据），算不出加权分 \| 0（无意义） \| 同 no_skills，按「暂无法评分」处理 \| \| `no_evidence` \| 有基准也有权重，但本次一项都没测到 \| 0（无意义） \| 「未评估」+ 引导做测评；不要显示 0% \|  口径与 `PositionMatchOut.source` 的 no_overlap / none 一致：不用 0% 冒充结论。阈值 30% 是服务端常量（`config.PARTIAL_BASELINE_PCT`），前端不要自己再定一份。历史报告（新增此字段之前生成）缺该键，按 ok 处理（默认 `ok`） |
| `tested_match_score` | number |  | 仅按**实测**技能权重为分母的匹配度 0–100，即「考过的部分掌握得怎样」。与 match_score 不同刻度，不可跨岗位比较；本次未测到任何技能时为 0，一项可评分技能都没有时为 null（理由同 match_score）。历史报告（新增此字段之前生成）为 null |
| `coverage` | number | 是 | 本次测评覆盖的**可评分**技能权重百分比 0–100（分母不含缺要求档的项），是 match_score 的置信度。它只表达「缺证据」，「缺基准」看 no_baseline_weight |
| `no_baseline_weight` | number |  | 因缺要求档而**无法评分**的技能权重，占岗位**全部**技能权重的百分比 0–100。100 表示整个岗位没配能力要求（此时 match_score 为 null）。**超过 30% 时服务端会把 score_status 降级成 partial_baseline**，前端不必自己比阈值，但展示「仅供参考」时可以把这个数字带出来（如「该岗位 82.4% 的能力要求待完善」）。权重全为 0 的脏数据岗位退回按项数占比计算。与 coverage 是两种不同的缺失：coverage 缺的是证据，这里缺的是基准（默认 `0.0`） |
| `radar` | `RadarOut` | 是 | 双系列雷达图数据 |
| `strengths` | `ReportItem`[] |  | 优势项（已测到且已达标），按超出幅度降序 |
| `gaps` | `ReportItem`[] |  | 短板项（已测到但未达标），按 urgency 降序 |
| `untested` | `ReportItem`[] |  | 本次未覆盖的技能，按权重降序；不下能力结论 |
| `no_baseline` | `ReportItem`[] |  | **无法评分**的技能（岗位要求档缺失或越界），按权重降序。既不在 strengths 也不在 gaps —— 没有基准谈不上达标或差距。与 `untested` 是**两个正交视角**、会重叠：一项技能可以既没测到（untested）又没有基准（no_baseline）。该数据缺口需要运营在管理台补要求档，不是学员的问题 |
| `items` | `ReportItem`[] |  | 全部技能明细（含未测） |
| `counts` | `ReportCounts` | 是 | 分项计数 |
| `summary` | string |  | 一句话结论 |
| `created_at` | string |  | 生成时间 ISO8601 |

### AssessmentStateOut

> 测评当前状态，用于刷新恢复。
>
> 全部来自业务表两条普通查询，不依赖工作流存档——进程重启也能恢复。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `session_id` | integer | 是 | 会话 id |
| `exists` | boolean | 是 | 是否已出过题；false 表示会话刚建还没开始 |
| `occupation_id` | string |  | 本场测评对标的岗位 id |
| `stages` | `StageOut`[] | 是 | 三阶段状态，顺序固定 parse/assess/report |
| `current_stage` | `parse` \| `assess` \| `report` | 是 | 当前所处阶段 |
| `questions` | `QuestionOut`[] |  | 本场全部题目 |
| `answers` | `AnswerRecordOut`[] |  | 已作答记录 |
| `question` | `QuestionOut` |  | 下一道未答题；全部答完为 null |
| `progress` | `ProgressOut` | 是 | 答题进度 |
| `question_end` | boolean | 是 | 题目是否已出完。库里有题即为 true——出题是一次性长连接，不会再追加 |
| `report` | `AssessmentReportOut` |  | 综合能力报告；未结算为 null |

### BadgeDefOut

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `code` | string | 是 | 编码 |
| `name` | string | 是 | 名称 |
| `description` | string |  | 描述 |
| `points` | integer | 是 | 成长值/积分 |
| `category` | string |  | 分类 |

### CapabilityMeta

> 能力全景的查询口径。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `matched` | integer |  | 是否命中专业：0=没找到，其余字段为空（默认 `0`） |
| `occupation_count` | integer |  | 对口岗位数（≥0.0，默认 `0`） |
| `skill_total` | integer |  | 技能总数（≥0.0，默认 `0`） |
| `skills_included` | boolean |  | 是否下钻返回了技能明细；默认 false，数据量大（默认 `False`） |
| `progression_count` | integer |  | 晋升链条数（≥0.0，默认 `0`） |
| `shared_skill_count` | integer |  | 共性技能数（≥0.0，默认 `0`） |
| `region` | string |  | 地区 |

### CapabilityOut

> 专业能力全景：以一个专业为根，一次给出它的行业、对口岗位、晋升链与共性技能。
>
> 前半段（node_types / rel_types / regions / endpoints）是服务能力自描述，
> 与查的是哪个专业无关；后半段才是这次查询的结果。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `node_types` | string[] |  | 支持的节点类型 |
| `rel_types` | string[] |  | 支持的关系类型 |
| `regions` | string[] |  | 已有数据的地区 |
| `endpoints` | string[] |  | 主要查询入口 |
| `counts` | map<string, integer> |  | 各类型节点数 |
| `root` | `KgNode` |  | 作为根的专业节点；没匹配到为 null |
| `industries` | `KgNode`[] |  | 该专业所属行业（major -belongs_to→ industry） |
| `occupations` | `KgNode`[] |  | 对口岗位（major -prepares_for→ occupation） |
| `progressions` | `ProgressionLink`[] |  | 岗位间的晋升链 |
| `shared_skills` | `SharedSkill`[] |  | 多个对口岗位共同要求的技能，按命中岗位数降序 |
| `meta` | `CapabilityMeta` |  | 本次查询的口径与计数 |

### ChangeApprovedOut

> 变更通过。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `approved` | `True` | 是 | 固定 true |
| `id` | integer | 是 | 变更单 id |
| `applied` | `AppliedResult` |  | 实际落库的内容；结构随变更类型而异（建节点 / 改边 / 改属性） |

### ChangeOut

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | integer | 是 | 节点 id |
| `entity_kind` | string | 是 | 实体种类：node / edge |
| `action` | string | 是 | 变更动作：create / update / delete |
| `dim_type` | string |  | 维度类型：industry / major / occupation / skill |
| `target_id` | string |  | 目标节点 id |
| `title` | string |  | 标题 |
| `payload` | `ChangePayload` |  | 变更内容；结构随 action 与 dim_type 而异（建节点的字段集与改边的完全不同） |
| `status` | string |  | 状态（默认 `pending`） |
| `created_by` | string |  | 提交人 id |
| `created_by_name` | string |  | 提交人姓名 |
| `created_at` | string |  | 创建时间 ISO8601 |
| `applied` | `AppliedResult` |  | 实际落库的内容；结构随变更类型而异，未通过时为 null |
| `direct` | boolean |  | true=直写生效；false=进待审队列 |
| `review_required` | boolean |  | 是否开启审核（0=直写，1=进待审队列） |

### ChangePayload

> 变更内容。
>
> 真实形状随 `entity_kind` + `action` + `dim_type` 而变，无法收敛成单一结构，
> 所以这里把**各分支可能出现的键**都列出来（都是可选），并允许额外字段。
> 这样 Swagger 上看到的是真实键名，而不是一个 `any`。
>
> 建节点时传节点字段；关联关系用 `*_ids` 列表表达，服务端自动建边，
> 客户端不需要自己构造 edge：
>
> - 专业 major → `industry_ids`（major -belongs_to→ industry）
> - 岗位 occupation → `major_ids`（major -prepares_for→ occupation）
> - 技能 skill_level → `occupation_ids`（occupation -requires→ skill）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `type` | string |  | 节点类型：industry / major / occupation / skill_level |
| `id` | string |  | 节点 id（编辑/删除时必填） |
| `name` | string |  | 名称 |
| `region` | string |  | 地区，默认 CN |
| `status` | string |  | 状态；新建一律先落 draft，门禁通过才升 published |
| `code` | string |  | 编码 |
| `description` | string |  | 描述 |
| `attrs` | object |  | 自由属性（无数据库约束的 JSON 列） |
| `industry_ids` | string[] |  | 关联行业（专业用：major -belongs_to→ industry）。**整体替换**：多的自动移除、少的自动新建。不传=这类关联一个不动；传 `[]`=清空这一类 |
| `major_ids` | string[] |  | 关联专业（岗位用：major -prepares_for→ occupation）。语义同 `industry_ids` |
| `occupation_ids` | string[] |  | 关联岗位。技能用（occupation -requires→ skill）；专业也可用（major -prepares_for→ occupation）。语义同 `industry_ids` |
| `skill_key` | string |  | 技能聚合主键（技能 bundle 用） |
| `levels` | object |  | L1–L5 各档内容（技能 bundle 用），键为档位号 |
| `src_id` | string |  | 边的**起点** id（entity_kind=edge）。方向固定：`belongs_to` 专业→行业 \| `prepares_for` 专业→岗位 \| `requires` 岗位→技能 |
| `dst_id` | string |  | 边的**终点** id，方向见 `src_id` |
| `rel_type` | string |  | 边的关系类型：belongs_to / prepares_for / requires / advances_to |
| `weight` | number |  | 边权重 |

### ChangeRejectedOut

> 变更驳回。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `rejected` | `True` | 是 | 固定 true |
| `id` | integer | 是 | 变更单 id |
| `deleted` | boolean | 是 | 变更单是否已删除 |

### ChangeSubmitBody

> 提交待审变更。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `entity_kind` | `node` \| `edge` | 是 | node\|edge |
| `action` | `create` \| `update` \| `delete` \| `disable` \| `enable` | 是 | create 新建 / update 编辑 / delete 物理删除 / disable 停用 / enable 发布 |
| `dim_type` | string |  | 节点四维 type：industry\|major\|occupation\|skill_level |
| `target_id` | string |  | 编辑/删除/停用/发布时的目标 id |
| `title` | string |  | 列表展示标题 |
| `payload` | `ChangePayload` |  | 节点字段 + 可选关联 id 列表（多选，服务端自动建边，客户端无需构造 edge）： - **专业 major**：`industry_ids: string[]` → 自动 major -belongs_to→ industry - **岗位 occupation**：`major_ids: string[]` → 自动 major -prepares_for→ occupation - **技能 skill_level**：`occupation_ids: string[]` → 自动 occupation -requires→ skill 边默认 weight=0.8、confidence=manual_seed、status=published。 |

### ChatMessageBody

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `content` | string | 是 | 学员回复内容（长度≥1） |

### ChatMessageOut

> 一轮对话的回复。轮次够了会同时给出报告。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `session_id` | integer | 是 | 会话 id |
| `reply` | string |  | AI 追问或结语 |
| `done` | boolean |  | 对话是否结束；true 时 report 有值（默认 `False`） |
| `turn` | integer |  | 当前轮次 |
| `report` | `AssessmentReportOut` |  | 结束时产出的诊断报告 |

### ChatSessionBody

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `target_occupation_id` | string |  | 对话诊断目标岗位 |

### ChatSessionOut

> 对话诊断会话。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `session_id` | integer | 是 | 会话 id |
| `channel` | `chat` | 是 | 固定 chat |
| `status` | string | 是 | 会话状态：active / done |
| `target_occupation_id` | string |  | 目标岗位 id |
| `first_question` | string |  | 开场提问 |

### ChoiceOption

> SJT（情景判断题）选项。每项对应一个能力档位，选谁即判谁。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `value` | integer | 是 | 选项序号，作答时回传这个值（≥1.0） |
| `level` | integer | 是 | 该选项体现的能力档位 1–5，同题内互不相同（≥1.0，≤5.0） |
| `text` | string | 是 | 选项文案 |

### ClearGoalOut

> 清除目标的回执。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `status` | `cleared` | 是 | 固定 cleared |

### CompositionCounts

> 构成计数。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `skill` | integer |  | 技能项数（≥0.0，默认 `0`） |

### CompositionItem

> 技能构成里的一项。
>
> 一个技能只出现一次——高级档天然包含低级档，同一技能挂多档是数据错误，
> 写入侧已加判重（`mode=add` 时重复会返回 409）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `edge_id` | string | 是 | requires / covers 边的 id |
| `skill_key` | string | 是 | 技能聚合主键（ASCII code） |
| `skill_name` | string |  | 展示名 —— **页面上要显示这个**。`skill_key` 从 2026-08-19 起是 ASCII code（形如 SK0123456789），拿它渲染就是一串哈希 |
| `category` | string |  | 技能大类 |
| `prereqs` | string[] |  | 先修技能的 skill_key 列表（来自 `kg_skill_prereq`）；空数组表示无先修。**不限本节点的技能集**——前置技能可能不被本岗位/专业要求，但学员仍需先具备 |
| `skill_level_id` | string | 是 | 边指向的那个 skill_level 节点 id |
| `available_levels` | integer[] |  | 该技能已配齐的档位 1–5 |
| `levels` | `CompositionLevelDetail`[] |  | 各档明细（含档位文案与要求描述） |
| `selected_level` | integer |  | 当前选中的要求档。改档 = 删旧边建新边，边指向哪档就是要求哪档 |
| `weight` | number |  | 权重；专业 covers 无权重，此时为 null |
| `weight_pct` | integer |  | 权重百分比，便于直接展示 |
| `dangling` | boolean |  | **异常边标记**：这条边指向的技能节点不是 published（停用 / 归档 / 仅草稿）。前台按节点状态过滤看不到这条技能，管理台口径看得到 —— 于是同一个岗位「前台 5 项 Σ0.81 / 管理台 6 项 Σ1.00」。读侧**不会**偷偷把它滤掉（那样运营永远发现不了），而是在这里标出来。存量数据用 `scripts/check_dangling_status_edges.py` 扫（默认 `False`） |
| `endpoint_status` | string |  | 该边指向的技能节点的实际 status（dangling 时用它解释原因） |

### CompositionLevelDetail

> 技能某一档的明细。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `level` | integer |  | 产品档 1–5 |
| `level_label` | string |  | 档位文案，取自 skill_level_meta |
| `node_id` | string |  | 该档的 skill_level 节点 id |
| `requirement` | string |  | 该档的能力要求描述 |
| `status` | string |  | 该档节点的状态 |

### CompositionNodeHeader

> 构成页头部：原型「岗位模型 · 内容运营专员」上方那几项。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 节点 id |
| `type` | string | 是 | 节点类型：occupation / major |
| `name` | string |  | 名称 |
| `status` | string |  | 图状态：published / draft / disabled |
| `level` | integer |  | 职级 |
| `level_label` | string |  | 职级文案，如 L3 |
| `version_label` | string | 是 | 版本号文案，如 V1 |
| `owner_name` | string |  | 负责人 |
| `description` | string |  | 描述 |
| `industries` | `IdName`[] |  | 所属行业（可多个） |
| `industry_name` | string |  | 行业名拼接串，便于直接展示；无为 null |
| `majors` | `IdName`[] |  | 关联专业（岗位才有） |
| `major_name` | string |  | 专业名拼接串 |
| `salary` | string |  | 薪资区间（来自 attrs） |
| `demand` | string |  | 需求热度（来自 attrs） |
| `code` | string |  | 编码（来自 attrs） |

### CompositionSkillBody

> 技能构成：添加/更新一项技能。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `skill_key` | string | 是 | 逻辑技能名（从 /composition/options 里选） |
| `level` | integer |  | 要求等级 1–5（对应 L1–L5）。留空取该技能最高档；该技能未配齐此档时返回 400 并列出可用档位 |
| `weight` | number |  | 权重 0–1，**仅岗位生效**；专业技能不带权重（不参与归一化） |

### CourseResourceOut

> 一门课程资源。`kind` 决定前端怎么展示，别把两类混在一起计数。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 课程节点 id |
| `name` | string | 是 | 课程名 |
| `url` | string |  | 课程/检索页地址，可直接点开 |
| `search_url` | string |  | 仅 `kind=catalog` 有值：按课程名生成的慕课检索地址。课标条目自身的 `url` 是教育部专业教学标准的**大类目录页**（与技能无关），真要展示时请用这个而不是 `url` |
| `platform` | string |  | 来源系统，如 ICOURSE163 / XUETANGX |
| `platform_label` | string |  | 平台中文名，直接展示用 |
| `kind` | `real` \| `enroll` \| `catalog` \| `landing` | 是 | 资源性质，决定前端怎么展示，**只有 real 是点开当场能学的**：  - `real`：免登录、免报名、无开课周期，点开就能学 - `enroll`：是真课程，但**要登录报名**或按学期开课，往期只剩介绍页（中国大学MOOC / 学堂在线） - `catalog`：教育部课标里的课目条目（`role=curriculum_catalog`），点开是专业培养方案目录，**没有课程内容** - `landing`：检索入口，点开是搜索结果页  判定以课程节点的 `attrs.role` 为准，不是按 source_system —— 曾把 MOE_CN 当成真课，学员点开是课标目录页；也曾把 MOOC 归进 real，学员点开是报名墙。 |
| `learner_count` | integer |  | 学习/选课人数，质量信号；landing 类无此值 |
| `school` | string |  | 开课院校/机构 |
| `img_url` | string |  | 封面图 |

### DeleteResult

> 物理删除实际影响的条数。
>
> 节点删除会连带删掉它两端的边（不级联删其它节点），所以两个计数都要给：
> 运营点一次「删除岗位」，需要知道顺带带走了多少条关联边。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `node_id` | string |  | 被删的节点 id；删边时为 null |
| `edge_id` | string |  | 被删的边 id；删节点时为 null |
| `nodes_deleted` | integer |  | 删掉的节点数（0 表示目标本就不存在）（≥0.0，默认 `0`） |
| `edges_deleted` | integer |  | 连带删掉的关联边数（≥0.0，默认 `0`） |

### DiagnosedBrief

> 某岗位的历史诊断摘要。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `match_score` | number | 是 | 诊断得出的匹配度 0–100 |
| `session_id` | integer |  | 诊断会话 id |
| `channel` | string |  | 诊断渠道：assessment / resume / chat |
| `diagnosed_at` | string |  | 诊断时间 ISO8601 |

### DiagnosedOccupationItem

> 已诊断过的岗位一行。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `occupation_id` | string |  | 岗位节点 id |
| `occupation_name` | string |  | 岗位名 |
| `match_score` | number |  | 最近一次匹配度 0–100 |
| `channel` | string |  | 诊断渠道：assessment / resume / chat |
| `last_session_id` | integer |  | 最近一次诊断会话 id |
| `session_count` | integer |  | 累计诊断次数（≥0.0，默认 `0`） |
| `diagnosed_at` | string |  | 最近诊断时间 ISO8601 |
| `goal_status` | string |  | 该岗位的目标状态：active / archived；从未设为目标则为 null |
| `is_active_goal` | boolean |  | 是否为当前活跃目标（默认 `False`） |
| `major_name` | string |  | 关联专业名 |
| `goal_created_at` | string |  | 设为目标的时间 ISO8601 |
| `plan_id` | string |  | 学习计划 id；未生成为空串（默认 ``） |
| `plan_created_at` | string |  | 学习计划生成时间 ISO8601 |

### DiagnosedOccupationListOut

> 已诊断岗位分页列表。分页下沉到 SQL，不是取回内存再切。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `items` | `DiagnosedOccupationItem`[] | 是 | 当前页数据 |
| `total` | integer | 是 | 总条数（≥0.0） |
| `page` | integer | 是 | 页码，从 1 起（≥1.0） |
| `page_size` | integer | 是 | 每页条数（≥1.0） |
| `pages` | integer | 是 | 总页数（≥0.0） |

### DiagnosisReportOut

> 诊断报告（简历 / 对话渠道）。
>
> 与测评报告 `AssessmentReportOut` 的区别在证据强度：那边每项技能都有实测档位
> 与要求档位的对照，这边只是从文本里解析出的技能名与岗位要求做匹配。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` | string |  | UC 用户 id |
| `channel` | string |  | 诊断渠道：resume / chat / profile |
| `target_occupation_id` | string |  | 目标岗位节点 id |
| `target_occupation_name` | string |  | 目标岗位名 |
| `match_score` | number |  | 匹配度 0–100 |
| `user_skills` | `ParsedUserSkill`[] |  | 从简历/对话解析出的学员技能 |
| `required_skills` | `RequiredSkillRef`[] |  | 岗位要求的技能 |
| `gaps` | `RequiredSkillRef`[] |  | 缺口技能（岗位要求但学员未体现） |
| `radar` | `SimpleRadar` |  | 雷达图数据 |
| `summary` | string |  | 一句话结论 |

### EdgeCreate

> 新建边请求体。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `src_id` | string | 是 | 起点节点全局 id（必填，须已存在于库中） |
| `dst_id` | string | 是 | 终点节点全局 id（必填，须已存在于库中） |
| `rel_type` | string | 是 | 关系类型小写（必填）。常用：prepares_for=专业培养对口岗位 \| requires=岗位要求技能 \| belongs_to=岗位归属行业 \| parent_of=行业父子 \| related_to=相关 \| taught_by=技能由课程教授 |
| `region` | string |  | 边所属区域，默认 CN（默认 `CN`） |
| `id` | string |  | 边 ID（可选）。不传则生成 `edge:{src}\|{rel}\|{dst}` |
| `weight` | number |  | 关系强度 0～1（可选），越大表示越相关/越重要 |
| `evidence` | string |  | 证据摘要或原文摘录（可选），便于审核溯源 |
| `attrs` | object |  | 边扩展属性 JSON（可选） |
| `confidence` | string |  | 置信度（可选），默认 manual_seed；官方边用 official（默认 `manual_seed`） |
| `status` | `draft` \| `published` \| `archived` \| `disabled` |  | draft=草稿默认 \| published=已发布 \| archived=归档（默认 `draft`） |
| `source_url` | string |  | 溯源 URL（可选） |

### EdgeListItem

> 边列表项（含端点名称，便于核对删节点后是否连带删边）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string |  | 边 ID |
| `src_id` | string | 是 | 起点节点 id |
| `dst_id` | string | 是 | 终点节点 id |
| `rel_type` | string | 是 | 关系类型 |
| `neo4j_type` | string |  | Neo4j 侧的关系类型（历史兼容字段） |
| `region` | string |  | 地区，如 CN |
| `weight` | number |  | 权重 |
| `confidence` | string |  | 置信度 |
| `evidence` | string |  | 判定依据 |
| `source_url` | string |  | 来源链接 |
| `status` | string |  | published\|disabled\|… |
| `src_name` | string |  | 起点名称 |
| `src_type` | string |  | 起点类型 |
| `dst_name` | string |  | 终点名称 |
| `dst_type` | string |  | 终点类型 |

### EdgeListResponse

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `items` | `EdgeListItem`[] |  | 当前页数据 |
| `page` | integer | 是 | 页码，从 1 起 |
| `page_size` | integer | 是 | 每页条数 |
| `total` | integer | 是 | 总条数 |
| `total_pages` | integer | 是 | 总页数 |
| `rel_type` | string |  | 关系类型 |
| `node_id` | string |  | 按节点过滤时传入的 node_id |
| `q` | string |  | 本次查询用的关键词（回显） |

### EdgeReviewFilter

> 本次查询用到的筛选条件（回显）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `confidence` | string |  | 置信度筛选值 |
| `rel_type` | string |  | 关系类型筛选值 |
| `region` | string |  | 地区 |

### EdgeReviewItem

> 一条待审的边。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 边 id |
| `rel_type` | string | 是 | 关系类型，如 requires / covers / belongs_to |
| `src_id` | string | 是 | 源节点 id |
| `src_name` | string |  | 源节点名 |
| `src_type` | string |  | 源节点类型 |
| `dst_id` | string | 是 | 目标节点 id |
| `dst_name` | string |  | 目标节点名 |
| `dst_type` | string |  | 目标节点类型 |
| `confidence` | string |  | 置信度标记，如 ai_inferred=模型推断（默认筛选项） |
| `weight` | number |  | 边权重（requires 才有） |
| `status` | string |  | 边状态：published / draft / disabled |
| `evidence` | string |  | 建边依据 |
| `source_url` | string |  | 来源链接 |

### EdgeReviewListOut

> 待审边分页列表。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `items` | `EdgeReviewItem`[] | 是 | 当前页数据 |
| `page` | integer | 是 | 页码，从 1 起（≥1.0） |
| `page_size` | integer | 是 | 每页条数（≥1.0） |
| `total` | integer | 是 | 总条数（≥0.0） |
| `total_pages` | integer | 是 | 总页数（≥0.0） |
| `filter` | `EdgeReviewFilter` | 是 | 筛选条件回显 |

### FrontendConfigOut

> 前端启动配置。
>
> 只暴露前端确实需要的开关，**不含任何密钥**；`auth_bypass` 在生产必须是 0。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `api_version` | string |  | 接口版本号 |
| `uc_sdk_url` | string |  | 用户中心 SDK 地址 |
| `uc_env` | string |  | 用户中心环境 |
| `uc_component_host` | string |  | 用户中心组件域名 |
| `uc_api_host` | string |  | 用户中心接口域名 |
| `auth_bypass` | boolean \| integer |  | 是否跳过审核门禁；**生产必须为 0** |
| `review_required` | boolean \| integer |  | 0=修改直写生效；1=进待审队列 |
| `llm_enabled` | boolean |  | AI 网关是否可用；false 时 AI 功能走规则兜底 |
| `llm_model` | string |  | 模型名；网关不可用时为 null |

### GoalItem

> 一个学习目标（对应 biz_user_goal 一行）。一人可有多个，其一为活跃。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` | string | 是 | UC 用户 id |
| `user_name` | string |  | 用户名（冗余字段，用户中心不在本服务） |
| `occupation_id` | string |  | 目标岗位节点 id |
| `occupation_name` | string |  | 目标岗位名 |
| `major_id` | string |  | 关联专业 id |
| `major_name` | string |  | 关联专业名 |
| `industry_id` | string |  | 所属行业 id |
| `industry_name` | string |  | 所属行业名 |
| `status` | string |  | active=当前活跃目标；archived=历史目标 |
| `created_at` | string |  | 设定时间 ISO8601 |
| `updated_at` | string |  | 更新时间 ISO8601；也是链路的绑定时间 |
| `progression` | `GoalProgressionOut` |  | 绑定的晋升链路。锁定目标时可传 `progression_path` 指定，不传则自动绑第一条（置信度最高、职级最近的方向一路走到头）；该岗位没有 `advances_to` 出边时为 null |

### GoalOccupationOut

> 总览里的目标岗位详情。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string |  | 岗位节点 id |
| `name` | string |  | 岗位名 |
| `level` | integer |  | 职级 |
| `level_label` | string |  | 职级文案，如 L3 |
| `description` | string |  | 岗位职责描述 |
| `salary` | string |  | 薪资区间（来自 attrs） |
| `skill_count` | integer |  | 该岗位技能数（≥0.0，默认 `0`） |

### GoalOut

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` | string | 是 | UC 用户 id |
| `user_name` | string |  | 用户名（冗余字段，用户中心不在本服务） |
| `occupation_id` | string |  | 目标岗位 id |
| `occupation_name` | string |  | 目标岗位名 |
| `major_id` | string |  | 关联专业 id |
| `major_name` | string |  | 关联专业名 |
| `industry_id` | string |  | 所属行业 id |
| `industry_name` | string |  | 所属行业名 |
| `updated_at` | string |  | 更新时间 ISO8601；也是链路的绑定时间 |
| `progression` | `GoalProgressionOut` |  | 绑定的晋升链路。锁定时传了 `progression_path` 就是那条，否则是自动绑的第一条；该岗位无 `advances_to` 出边时为 null |

### GoalOverviewOut

> 学习目标总览：当前目标 + 岗位详情 + 测评结果 + 晋升路径 + 学习计划 id。
>
> 原型上那张卡片一次就要这些数据，拆成多个接口会让首屏串行等待。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `has_goal` | boolean | 是 | 是否已锁定目标；false 时其余字段多为 null |
| `goal` | `GoalItem` |  | 当前活跃目标 |
| `goals` | `GoalItem`[] |  | 该用户全部目标（含历史） |
| `progression` | `GoalProgressionOut` |  | 绑定的晋升链路，与 `goal.progression` 同一份，提到顶层方便前端直接画「当前 → 下一级 → …」。`next_target` 即默认的下一目标，`next_levels` 已把它排到首位 |
| `progression_stale` | boolean |  | 绑定的下一跳在当前图里已无对应 `advances_to` 边（边被归档或重跑采集改了方向）。链路仍按原样返回——用户选过的不该被悄悄改掉——但前端应提示「该晋升方向已变更，请重新选择」（默认 `False`） |
| `occupation` | `GoalOccupationOut` |  | 目标岗位详情 |
| `major` | `RefOut` |  | 关联专业 |
| `industry` | `RefOut` |  | 所属行业 |
| `match_score` | number |  | 该岗位最近一次诊断的匹配度 0–100，取自 `assessment.match_score`。**null 有两种成因，引导文案不同**：没测过 → 引导「去测评」；测过但岗位没配能力要求档（`assessment.score_status="no_baseline"`）→ 只能提示「该岗位标准待完善」，再引导测评也算不出分。两种都不要显示 0% |
| `assessment` | `AssessmentReportOut` |  | 最近一次的完整测评报告；没测过为 null |
| `next_level` | `NextLevelOut` |  | **兼容字段**：`next_levels` 的第一条（置信度最高、职级最近）。无 advances_to 边时为 null。新前端请读 `next_levels` |
| `next_levels` | `NextLevelOut`[] |  | 全部向上方向。`advances_to` 是 **1:N** —— 一个岗位可以有多条晋升路径（本方向纵深 / 管理路线 / 跨方向转型）。要看多跳完整链路用 `GET /v1/student/positions/progressions` |
| `learning_plan_id` | string |  | 学习计划 id（学习空间服务的主键）；尚未生成时为空串（默认 ``） |
| `learning_plan_created_at` | string |  | 学习计划生成时间 ISO8601 |

### GoalProgressionOut

> 锁定目标时绑定的晋升链路。
>
> 库里只存**岗位 id 序列**，岗位名与职级是读时按 id 查最新的：`advances_to` 由
> LLM 推断、重跑采集会变，存 id 让用户选定的路径不随图漂移，而展示信息保持最新。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `direction` | string |  | `user_selected`=用户显式选的；`default_first`=锁定时未选、自动绑的第一条 |
| `chain` | `ProgressionNodeOut`[] |  | 链路上的岗位，从当前目标开始，顺序即晋升顺序 |
| `hops` | integer |  | 跳数 = chain 长度 - 1（默认 `0`） |
| `next_target` | `ProgressionNodeOut` |  | 下一目标 = 链路里当前目标之后那一跳；链路只有起点时为 null |
| `target` | `ProgressionNodeOut` |  | 链路终点岗位 |

### GoalPutBody

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `occupation_id` | string | 是 | 目标岗位节点 id（必填） |
| `major_id` | string |  | 可选：关联专业 id |
| `progression_path` | string[] |  | 可选：要绑定的晋升链路，岗位 id 序列，**第一个必须是 `occupation_id` 本身**。从 `GET /v1/student/positions/progressions?id=` 的某条 path 里取（`hops[].from` + 最后一跳的 `to`）。  服务端会校验相邻两跳真的存在 published 的 `advances_to` 边，并拒绝成环与超长——否则可以拼出任意两个岗位的假晋升关系。  不传则自动绑第一条（置信度最高、职级最近的方向）；重锁同一岗位而不传时，保留原来绑的那条，不被默认值覆盖。 |

### GraphCounts

> 图查询的计数口径。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `node_count` | integer |  | 返回的节点数（≥0.0，默认 `0`） |
| `edge_count` | integer |  | 返回的边数（≥0.0，默认 `0`） |
| `root_count` | integer |  | 根节点数（无父行业的顶层）（≥0.0，默认 `0`） |
| `region` | string |  | 地区，如 CN |

### GraphIndustryBrief

> 图上的行业根节点。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 行业节点 id |
| `name` | string |  | 行业名 |
| `region` | string |  | 地区 |

### GraphLayers

> 行业图的分层节点。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `majors` | `GraphMajorNode`[] |  | 专业层 |
| `occupations` | `GraphOccupationNode`[] |  | 岗位层 |

### GraphLink

> 行业图的层间连边（专业 → 岗位）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `from` | string | 是 | 源节点 id |
| `to` | string | 是 | 目标节点 id |
| `rel` | string | 是 | 关系类型，如 prepares_for |

### GraphMajorNode

> 行业图里的专业层节点。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 专业节点 id |
| `name` | string |  | 专业名 |
| `occupation_count` | integer |  | 对口岗位数；截断按它倒序，先丢弱关联的（≥0.0，默认 `0`） |

### GraphOccupationNode

> 行业图里的岗位层节点。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 岗位节点 id |
| `name` | string |  | 岗位名 |
| `level` | integer |  | 岗位层级 |
| `major_ids` | string[] |  | 对口专业 id 列表 |

### IdName

> id + name 的轻引用。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 节点 id |
| `name` | string |  | 名称 |

### IndustryGraphMeta

> 行业图的口径与截断说明。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `matched` | integer |  | 是否命中行业：0=没找到，此时各层为空（默认 `0`） |
| `major_total` | integer |  | 专业总数（≥0.0，默认 `0`） |
| `major_shown` | integer |  | 本次返回的专业数（≥0.0，默认 `0`） |
| `occupation_total` | integer |  | 岗位总数（≥0.0，默认 `0`） |
| `occupation_shown` | integer |  | 本次返回的岗位数（≥0.0，默认 `0`） |
| `truncated` | boolean |  | 是否发生截断；true 表示你看到的不是全量（默认 `False`） |
| `layout` | string |  | 布局：layered / matrix |
| `region` | string |  | 地区 |

### IndustryGraphOut

> 行业图谱：行业 → 专业 → 岗位 三层 + 晋升链。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `industry` | `GraphIndustryBrief` |  | 行业根节点；没命中为 null |
| `layers` | `GraphLayers` |  | 分层节点 |
| `links` | `GraphLink`[] |  | 层间连边 |
| `progressions` | `ProgressionLink`[] |  | 同岗位族内按 level 递进的晋升链 |
| `matrix` | `MajorOccupationMatrix` |  | 专业×岗位矩阵；仅 layout=matrix 时返回 |
| `meta` | `Industry`GraphMeta`` |  | 口径与截断说明 |

### IndustryItem

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 节点 id |
| `name` | string |  | 名称 |
| `code` | string |  | 编码 |
| `level` | integer \| string |  | 等级 |
| `parent_code` | string |  | 父级编码 |
| `desc` | string |  | 简介 |
| `counts` | `RelationCounts` |  | major / occupation 直连计数 |

### IndustryLink

> 岗位挂到的一个行业。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 行业节点 id |
| `name` | string |  | 行业名 |

### IndustryListOut

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `page` | integer | 是 | 页码，从 1 起 |
| `page_size` | integer | 是 | 每页条数 |
| `total` | integer | 是 | 总条数 |
| `total_pages` | integer | 是 | 总页数 |
| `items` | `IndustryItem`[] | 是 | 当前页数据 |

### IndustryRef

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 行业节点 id |
| `name` | string |  | 行业名 |

### IndustryTreeResponse

> 行业树。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `nodes` | `KgNode`[] |  | 行业节点 |
| `edges` | `KgEdge`[] |  | parent_of 边（父→子） |
| `roots` | `KgNode`[] |  | 无父节点的根行业 |
| `meta` | `GraphCounts` |  | 本次查询的计数与地区口径 |

### KgEdge

> 知识图谱关系边。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string |  | 边 ID |
| `rel_type` | string | 是 | 关系类型小写：prepares_for(专→岗)\|requires(岗→技)\|belongs_to(岗→行业)\|parent_of(行业父子)\|related_to\|taught_by\|… |
| `neo4j_type` | string |  | 关系类型大写（兼容旧前端字段名，等同 rel_type.upper()） |
| `src_id` | string | 是 | 起点节点 id |
| `dst_id` | string | 是 | 终点节点 id |
| `weight` | number |  | 权重 0～1，越大越强 |
| `confidence` | string |  | 边置信度 |
| `source_url` | string |  | 溯源 URL |
| `evidence` | string |  | 证据摘要/原文片段 |
| `status` | `draft` \| `published` \| `archived` \| `disabled` \| string |  | published\|draft\|archived |

### KgNode

> 知识图谱节点（专业/岗位/技能/课程/认证/行业等）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 全局唯一 ID，如 CN:major:MOE_CN:… |
| `labels` | string[] |  | 类型标签列表（兼容旧 Neo 形态，通常等于 [type]） |
| `region` | string |  | 区域：CN \| EU \| US |
| `type` | string | 是 | 节点类型：industry\|major\|occupation\|skill_level\|course\|credential |
| `name` | string | 是 | 官方/原始名称（查询按此字段匹配） |
| `display_name` | string |  | 展示名；专业会消歧为「层次 · 名称 · 代码」 |
| `name_en` | string |  | 英文名 |
| `name_zh` | string |  | 中文名 |
| `description` | string |  | 简介/摘要 |
| `source_system` | string |  | 来源系统，如 MOE_CN、MOHRSS、BOSS_ZHIPIN、MANUAL |
| `source_id` | string |  | 来源侧主键/编码 |
| `source_url` | string |  | 溯源 URL |
| `confidence` | string |  | 置信度：official\|derived\|ai_inferred\|manual_seed\|… |
| `attrs` | object \| any[] \| string |  | 扩展属性 JSON（专业 code/level、技能等级等，随 type 变化） |
| `status` | `draft` \| `published` \| `archived` \| `disabled` \| string |  | 库内状态 published\|disabled\|…；缺省视为 published |
| `order` | integer |  | 同层稳定排序（库列 sort_order；按 type 内 name 生成，从 1 起） |
| `sort_order` | integer |  | 同 order；兼容字段 |
| `child_count` | integer |  | 向下可展开子节点数：industry→major、major→occupation、occupation→skill_level；懒加载「可展开 N 项」用 |
| `updated_by` | string |  | 最近修改人 user-id |
| `updated_by_name` | string |  | 最近修改人姓名 |
| `created_at` | string |  | 创建时间 ISO8601。管理台列表默认按它倒序（`order_by=created_desc`），新建的数据排最前；历史数据用采集时间 fetched_at 回填 |
| `version` | integer |  | 发布版本号，从 1 起，每次成功发布（status→published）+1 |
| `version_label` | string |  | 版本展示文案，如 `V3`（原型「版本」列） |
| `owner` | string |  | 业务负责人 user-id |
| `owner_name` | string |  | 业务负责人姓名（原型「负责人」列）；新建时默认取创建人 |
| `code` | string |  | 业务编码（**同 region + 同 type 内唯一**，可编辑，写入前校验，冲突返回 409）。与 `id` 解耦：改 code 不影响 id 与已建的边。各维度编码体系独立——行业为语义 slug（`internet-ecom`）、专业为教育部专业码（`580506K`）、岗位为大典职业码（`6-18-01-10`）。同时保留在 `attrs.code` 以兼容既有前端 |
| `level` | integer |  | occupation 岗位层级 1..N；skill_level 可复用为 L 序 |
| `category` | string |  | skill_level 技能大类的 **code**（TECH / OPERATE / SAFETY …），字典见 `GET /v1/kg/skill-categories`。展示请用 `category_name` |
| `category_name` | string |  | 技能大类展示名，由 code 连 `kg_skill_category` 表取得，**不入库**。前端不要按 code 自己映射中文 —— 改名只动那张表 |
| `is_draft` | boolean |  | 这一行是不是草稿行。读路径「同一 id 只取一行、草稿优先」 |
| `has_draft` | boolean |  | 这条记录有没有未发布的草稿。等价于 `is_draft` —— 草稿优先取行之后，「拿到的是草稿行」就等于「有草稿」，不是另一个落库字段 |
| `record_status` | string |  | **记录状态 = 最新版本的状态**：有草稿就是 `draft`，否则等于线上行的 status。列表上给运营看的就是这个；`status` 仍是本行自己的库内状态。判定按**发布单元**——只改技能构成（草稿边、没有草稿节点行）也算 draft |
| `draft_change` | string |  | 草稿改的是什么：`node`=节点字段 \| `edges`=仅关联/技能构成 \| `both`=两者。`edges` 那种没有草稿节点行，但同样要发布才生效 |
| `target_status` | string |  | 仅草稿行有：发布后线上行会变成什么（published\|disabled\|archived），null=只更新内容不改状态。**不要把它写进 status**，那样草稿会泄漏到前台 |
| `base_version` | integer |  | 仅草稿行有：基于哪个已发布版本改的；发布时与线上 version 不等则 409 |
| `pending_change_id` | integer |  | 待审变更 id；有值表示有未审完的操作，但生效状态仍以 status 为准 |
| `pending_action` | string |  | 待审动作 create\|update\|delete\|disable\|enable（通过后才改库） |
| `pending_title` | string |  | 待审变更的标题 |
| `counts` | map<string, integer \| number> |  | 关联计数（include_counts=1 时联读填充）。键：major/occupation/skill/industry/course/level；skill 为逻辑技能 DISTINCT skill_key；skill_aggregated 为专业经岗位两跳汇总的技能数。另有 weight_sum（岗位 requires 权重和）是**小数**，故值类型为 int \| float |
| `industries` | `IndustryLink`[] |  | 岗位所属行业列表（occupation + include_counts）：直连 belongs_to + 经专业 prepares_for→belongs_to 两跳，按 name, id 稳定排序 |
| `industry_id` | string |  | industries[0].id |
| `industry_name` | string |  | industries[0].name |
| `industry_ids` | string[] |  | 专业→行业 关联 id 列表（编辑表单预选） |
| `major_ids` | string[] |  | 岗位←专业 关联 id 列表（编辑表单预选） |
| `occupation_ids` | string[] |  | 技能←岗位 关联 id 列表（编辑表单预选） |
| `link_ids` | map<string, string[]> |  | 结构化关联 id：industry_ids/major_ids/occupation_ids |

### LadderStep

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `tier` | integer | 是 | 阶梯层级 1..n |
| `position_id` | string | 是 | 岗位节点 id |
| `position_name` | string |  | 岗位名 |
| `position` | `PositionOut` |  | 岗位详情 |

### LearningPlanBody

> 生成学习计划。
>
> 只需要一个 `session_id`：岗位从该会话取，短板由服务端从诊断报告读——
> 不信客户端传的短板列表，那会让「学员看到的计划」和「诊断结论」对不上。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `session_id` | integer | 是 | 据以生成的诊断会话 id（必填）。学习计划必须基于一次真实诊断：没有诊断结果就没有短板数据，生成出来是一条空路径。  取值来自测评/简历/对话诊断完成后返回的 `session_id`。（≥1.0） |
| `recommend_resources` | boolean |  | 是否为每个技能挂载可学课程（图谱里有 taught_by/related_to 边时）（默认 `True`） |

### LearningPlanCreatedOut

> 生成学习计划的回执。
>
> **没有本地兜底**：推不上去就返回错误码，不再像旧版那样发个假 plan_id。
> 学习计划是业务主数据，给假 id 会让学员点进去看到空白页。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `plan_id` | string | 是 | 学习空间返回的计划 id |
| `created` | boolean | 是 | true=本次新建；false=幂等命中（同一次诊断重复调用，对方返回已存在的计划，无副作用） |
| `superseded_plan_id` | string |  | 本次换代时被归档的旧计划 id；首次生成为 null |
| `occupation_id` | string | 是 | 目标岗位 id（取自该诊断会话） |
| `session_id` | integer | 是 | 据以生成的诊断会话 id |
| `phases_count` | integer | 是 | 阶段数（≥1.0） |
| `tasks_count` | integer | 是 | 任务总数（≥1.0） |
| `pushed_at` | string |  | 推送时间 ISO8601 |

### LearningPlanItem

> 一条学习计划推送记录（对应 `biz_user_learning_plan` 一行）。
>
> 计划内容不在本服务——本地只留关联与推送状态，进度真源在学习空间服务。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | integer | 是 | 本地记录 id |
| `user_id` | string | 是 | UC 用户 id |
| `occupation_id` | string | 是 | 目标岗位 id |
| `plan_id` | string | 是 | 学习空间返回的计划 id；推送失败时为空串 |
| `session_id` | integer |  | 据以生成的诊断会话 id |
| `push_status` | `ok` \| `failed` |  | ok=已成功推送；failed=推送失败，`last_error` 里是原因，可重推 |
| `last_error` | string |  | 最近一次推送失败的原因；成功时为 null |
| `pushed_at` | string |  | 最近一次推送时间 ISO8601 |
| `created_at` | string |  | 创建时间 ISO8601 |

### MajorOccupationMatrix

> 专业 × 岗位热力图矩阵（layout=matrix 时才有）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `rows` | string[] |  | 行：专业节点 id，按顺序 |
| `cols` | string[] |  | 列：岗位节点 id，按顺序 |
| `cells` | `MatrixCell`[] |  | 非零格；未出现的格视为 0 |
| `max` | integer |  | 全矩阵最大格值，配色归一化用（默认 `0`） |
| `metric` | string |  | 格值口径，如 skill_affinity=专业与岗位的共同技能数 |

### MatchItem

> 匹配度明细的一项技能：岗位要求 vs 学员已有。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `skill_key` | string | 是 | 技能聚合主键 |
| `skill_name` | string |  | 技能展示名 |
| `category` | string |  | 技能大类 code，见 /v1/kg/skill-categories |
| `category_name` | string |  | 技能大类展示名（由 code 派生，不入库） |
| `required_level` | integer |  | 岗位要求档 1–5；**0 表示该技能没有指定要求档**（边没指向带产品档的等级节点，或档位是越界脏值），此时 `scorable=false`、该项不计入匹配度 |
| `user_level` | integer |  | 学员已有档 1–5；**0 表示该技能无任何证据**（不是「水平为零」），null 同义 |
| `weight` | number | 是 | 该技能在岗位中的权重，Σ≈1 |
| `is_core` | boolean |  | 是否核心技能 |
| `ratio` | number | 是 | 达成比 = 已有/要求，封顶 1.0。`scorable=false` 时这里是「有证据即 1.0」的展示值，**不参与匹配度计算** |
| `ok` | boolean | 是 | 是否达标（已有 ≥ 要求）；无要求档时恒为 false |
| `scorable` | boolean |  | 该项能否评分。false = 岗位要求档缺失或越界（无基准），分子分母都不计入 match_score，也不进 strengths / gaps；汇总在 `no_baseline` 里（默认 `True`） |
| `matched_by` | string |  | 命中方式：exact=技能名精确匹配；fuzzy=模糊匹配；none=无证据 |

### MatchRadarOut

> 匹配度的雷达图。
>
> 与测评报告的 `RadarOut` 不同：那边是「学员实测 vs 岗位要求」双系列，
> 这里只有一条达成率曲线——匹配度算的是差距比例，没有独立的「要求」系列。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `categories` | string[] |  | 各轴名称（技能大类） |
| `scores` | integer[] |  | 各轴达成率 0–100，顺序与 categories 一致 |

### MatrixCell

> 矩阵里的一个非零格。用行列下标而非二维数组——矩阵很稀疏，摊平会浪费大量 0。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `r` | integer | 是 | 行下标，对应 rows[r]（≥0.0） |
| `c` | integer | 是 | 列下标，对应 cols[c]（≥0.0） |
| `v` | integer | 是 | 格值，含义见 metric |

### MeOut

> 学员个人首页数据。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` | string | 是 | UC 用户 id |
| `user_name` | string |  | 用户名 |
| `goal` | `UserGoalBrief` |  | 当前学习目标；未锁定为 null |
| `points` | integer |  | 成长值/积分（默认 `0`） |
| `badges` | `UserBadgeOut`[] |  | 已解锁成就 |
| `skills` | `User`SkillOut``[] |  | 技能画像 |

### NextLevelOut

> 晋升路径上的下一档岗位。
>
> ⚠ `unlock_skills` 与 `description` 曾漏在这里没声明，后端明明算好了，
> Pydantic 按模型序列化时**静默丢掉**，前端拿不到就一直显示
> 「下一级岗位暂未配置技能构成」—— 数据在库里、逻辑也对，只是没进契约。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 岗位节点 id |
| `name` | string |  | 岗位名 |
| `level` | integer |  | 职级 |
| `level_label` | string |  | 职级文案，如 L3 |
| `description` | string |  | 岗位职责描述 |
| `confidence` | string |  | 晋升边置信度：official / derived / ai_inferred |
| `unlock_skills` | `SkillGapOut`[] |  | 进阶要补的关键技能（最多 6 项，按目标岗位权重倒序） |

### NodeCreate

> 新建节点请求体。字段含义与返回的 `KgNode` 对齐。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `type` | string | 是 | 节点类型（必填）。取值：industry=行业 \| major=专业 \| occupation=岗位 \| skill_level=技能等级 \| course=课程 \| credential=认证 |
| `name` | string | 是 | 节点官方名称（必填）。搜索/匹配主字段，前端列表优先展示 display_name（长度≥1） |
| `region` | string |  | 区域代码。本期默认/推荐 CN；可选 EU、US（默认 `CN`） |
| `id` | string |  | 全局唯一 ID（可选）。不传则服务端生成，形如 `CN:manual:{type}:{12位hex}`。若传入须保证全局唯一 |
| `name_en` | string |  | 英文名称（可选） |
| `name_zh` | string |  | 中文名称（可选；可与 name 相同） |
| `description` | string |  | 简介/备注（可选） |
| `aliases` | string[] \| map<string, string> |  | 别名列表或别名结构（可选），用于扩展检索 |
| `attrs` | object |  | 扩展属性 JSON（可选）。随 type 变化，例如专业：`code` 专业代码、`level`/`level_zh` 办学层次；技能：`skill_name`、`level`（产品档 1–5，1 了解 → 5 专家） |
| `source_system` | string |  | 来源系统标识。手工录入默认 MANUAL；官方数据如 MOE_CN、MOHRSS（默认 `MANUAL`） |
| `source_url` | string |  | 溯源链接（可选）。无则服务端可填 manual://admin |
| `confidence` | string |  | 置信度。取值建议：official=官方 \| derived=规则派生 \| ai_inferred=模型推断 \| manual_seed=人工录入（默认）（默认 `manual_seed`） |
| `status` | `draft` \| `published` \| `archived` \| `disabled` |  | 发布状态。draft=草稿（默认，可进审核）\| published=已发布可见 \| archived=归档不可用（默认 `draft`） |
| `industry_ids` | string[] |  | 关联行业（专业用：major -belongs_to→ industry）。**整体替换**：多的自动移除、少的自动新建。不传=不动；传 `[]`=清空 |
| `major_ids` | string[] |  | 关联专业（岗位用：major -prepares_for→ occupation）。语义同 `industry_ids` |
| `occupation_ids` | string[] |  | 关联岗位。技能用（occupation -requires→ skill）；专业也可用（major -prepares_for→ occupation）。语义同 `industry_ids` |

### NodeDetailOut

> 节点详情。
>
> 返回键随节点类型而变（技能有 levels / prereqs，岗位有 skills / majors），
> 这里列出全部可能键；`extra=allow` 兜住未列出的。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string |  | 节点 id |
| `type` | string |  | 节点类型 |
| `name` | string |  | 名称 |
| `skill_key` | string |  | 技能聚合主键（技能详情） |
| `category` | string |  | 技能大类 code，见 /v1/kg/skill-categories |
| `category_name` | string |  | 技能大类展示名（由 code 派生，不入库） |
| `levels` | object[] |  | L1–L5 档位栅格，缺档的位置为空 |
| `level_completeness` | string |  | 档位完整度，形如「3/5」 |
| `level_descriptions` | map<string, string> |  | 各档能力描述，键为档位号 |
| `occupations` | `NodeRef`[] |  | 引用该技能的岗位 |
| `prereqs` | `NodeRef`[] |  | 前置技能 |
| `unlocks` | `NodeRef`[] |  | 学完可解锁的技能 |
| `counts` | map<string, integer> |  | 关联计数 |

### NodeListResponse

> 四维管理 Table 分页列表响应。
>
> 对齐参考后台 backend.html：行业 / 专业 / 岗位 / 技能 各自可像表格一样列出并翻页。
> 请求：`GET /v1/kg/nodes?type=major&page=1&page_size=20`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `items` | `KgNode`[] |  | 当前页节点列表（KgNode） |
| `page` | integer | 是 | 当前页码，从 1 开始（≥1.0） |
| `page_size` | integer | 是 | 每页条数（≥1.0，≤200.0） |
| `total` | integer | 是 | 符合条件的总条数（用于分页控件） |
| `total_pages` | integer | 是 | 总页数 = ceil(total / page_size) |
| `type` | string |  | 本次过滤的节点类型；四维：industry\|major\|occupation\|skill_level |
| `region` | string |  | 实际生效区域，如 CN |
| `q` | string |  | 名称关键字（模糊）；空表示不限 |
| `status` | string |  | 状态过滤；空表示排除 archived 的全部 |

### NodePatch

> 编辑节点请求体。全部可选，只传需要修改的字段。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `industry_ids` | string[] |  | 覆盖关联行业（专业用）。**整体替换**：多的自动移除、少的自动新建。不传=不动；传 `[]`=清空 |
| `major_ids` | string[] |  | 覆盖关联专业（岗位用）。语义同 `industry_ids` |
| `occupation_ids` | string[] |  | 覆盖关联岗位（技能 / 专业用）。语义同 `industry_ids` |
| `name` | string |  | 新名称（可选） |
| `name_en` | string |  | 新英文名（可选） |
| `name_zh` | string |  | 新中文名（可选） |
| `description` | string |  | 新简介（可选） |
| `aliases` | string[] \| map<string, string> |  | 覆盖别名（可选） |
| `attrs` | object |  | 覆盖扩展属性（可选；整对象替换，非深合并） |
| `source_url` | string |  | 新溯源 URL（可选） |
| `confidence` | string |  | 新置信度（可选）：official\|derived\|ai_inferred\|manual_seed |
| `status` | `draft` \| `published` \| `archived` \| `disabled` |  | 新状态（可选）：draft\|published\|archived |
| `region` | string |  | 新区域（可选），如 CN |

### NodeRef

> 节点详情里的关联引用。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string |  | 节点 id |
| `name` | string |  | 名称 |
| `skill_key` | string |  | 技能聚合主键（技能类引用） |
| `level` | integer |  | 等级 / 要求档 |
| `weight` | number |  | 权重 |

### OccupationBrief

> 岗位摘要（匹配度、总览等处的公共引用）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string |  | 岗位节点 id |
| `name` | string |  | 岗位名 |
| `level` | integer |  | 岗位职级/层级 |

### OccupationLink

> 技能被哪个岗位引用。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `occupation_id` | string | 是 | 岗位节点 id |
| `occupation_name` | string |  | 岗位名 |
| `level` | integer |  | 该岗位要求的档位 1–5 |
| `weight` | number |  | 权重 |

### OccupationLinkIn

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `occupation_id` | string | 是 | 岗位节点 id |
| `weight` | number |  | 该技能在本岗位技能构成中的权重 0–1（>1 视为百分制会 /100）；写在 requires 边上 |
| `required_level` | integer |  | 岗位要求档 1–5；权重写在该档对应边上 |

### OccupationSkillsGraphOut

> 岗位技能图谱：技能按大类分区 + 区内前置关系。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `occupation` | `GraphOccupationNode` |  | 岗位摘要 |
| `categories` | ``SkillCategory`Group`[] |  | 技能大类分区，按学习顺序排；每区 skills[].depth 为前置层深（0=可直接学） |
| `prereqs` | `SkillPrereqLink`[] |  | 前置关系边 |
| `meta` | `Skills`GraphMeta`` |  | 口径说明 |

### ParsedSkill

> 简历解析推断出的一项技能档位。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `skill_key` | string | 是 | 技能聚合主键（ASCII code） |
| `skill_name` | string |  | 展示名 —— **题干与列表都显示这个**。`skill_key` 是 ASCII code（SK0123456789），拿它渲染就是一串哈希 |
| `level` | integer | 是 | 推断档位 1–5（≥1.0，≤5.0） |

### ParsedUserSkill

> 从简历/对话文本里解析出的一项学员技能。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `skill_name` | string | 是 | 技能名 |
| `level` | integer |  | 推断档位 1–5；关键词命中时默认 2（≥1.0，≤5.0，默认 `2`） |
| `score` | integer |  | 推断得分（默认 `0`） |
| `source` | string |  | 来源：resume / chat / llm / rule |

### PositionCourseSkillGroup

> 按技能分组的课程。技能顺序按岗位 requires 权重倒序。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `skill_key` | string | 是 | 逻辑技能名 |
| `required_level` | integer |  | 该岗位要求的档位 1–5，来自 attrs.level |
| `weight` | number |  | 该技能在岗位能力结构中的权重，**小数**（同岗位 Σ≈1） |
| `category` | string |  | 技能大类 code，见 /v1/kg/skill-categories |
| `category_name` | string |  | 技能大类展示名（由 code 派生，不入库） |
| `courses` | `CourseResourceOut`[] |  | 课程列表 |

### PositionCoursesOut

> 岗位相关课程（GET /v1/student/positions/courses）。
>
> 独立于岗位详情与技能构成接口：详情接口不含课程，避免为了加卡片改动既有契约。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `occupation` | `OccupationBrief` | 是 | 岗位摘要 |
| `by_skill` | `PositionCourseSkillGroup`[] | 是 | 按技能分组的课程 |
| `skill_count` | integer | 是 | 有课程的技能数（≥0.0） |
| `course_count` | integer | 是 | 课程总数（real + enroll + catalog + landing）（≥0.0） |
| `real_course_count` | integer | 是 | 点开当场能学的资源数，**看这个判断资源是否可用**（≥0.0） |
| `enroll_count` | integer |  | 需报名/有开课周期的课程数（MOOC 类），不计入 real（≥0.0，默认 `0`） |
| `catalog_count` | integer |  | 课标目录条目数（点开是培养方案，不是课程）（≥0.0，默认 `0`） |
| `catalog_hidden` | boolean |  | 课标条目是否被隐藏（默认 true 且 catalog_count>0 时）。用于提示「这里还有数据缺口」（默认 `False`） |
| `landing_count` | integer | 是 | 检索入口数（≥0.0） |
| `note` | string |  | 口径说明 |

### PositionDetailOut

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `position` | `PositionOut` | 是 | 岗位详情 |
| `skills` | `SkillOut`[] |  | 岗位技能要求 |

### PositionListOut

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `page` | integer | 是 | 页码，从 1 起 |
| `page_size` | integer | 是 | 每页条数 |
| `total` | integer | 是 | 总条数 |
| `total_pages` | integer | 是 | 总页数 |
| `items` | `PositionOut`[] | 是 | 当前页数据 |

### PositionMatchOut

> 岗位匹配度。
>
> `source` 回答的是「展示这个分数时最该先提醒学员什么」，前端必须据此区分展示。
> 前三档是证据强度（级联命中即返回），后四档是「这个数不能当结论」的原因。
> 每档的建议展示文案与 `frontend/student.html` 的 `MATCH_SOURCE_LABEL` 保持一致：
>
> - `diagnosis` —— 该岗位做过完整测评，分数直接取自报告，最准。有分数；
>   配文「实测 · 你诊断过这个岗位」
> - `assessment` —— 用**其他场**测评实测到的档位现算（技能有重叠），`estimated=true`。
>   有分数；配文「推算 · 用你在其他岗位测出的技能比对」
> - `memory` —— 用五维记忆画像推断，证据最弱，`estimated=true`。有分数；
>   配文「预估 · 由你的能力画像推断」
> - `partial_baseline` —— 该岗位**缺要求档的技能权重超过 30%**，分数只依据已配置的
>   那部分算出。**分数照给**（那部分是真实依据，丢掉更亏）；配文「参考 · 该岗位
>   {no_baseline_weight}% 的能力要求待完善，分数仅供参考」，且不要拿它跨岗位排序
> - `no_overlap` —— 岗位技能与已有证据零交集。`match_score` 为 **null**；
>   配文「该岗位要求的技能你还没测过」+ 引导做一次 AI 诊断
> - `no_baseline` —— **该岗位一项技能都没配要求档**，没有基准就算不出达标率。
>   `match_score` 为 **null**；配文「该岗位能力标准待完善，暂时无法评分」。
>   这是数据缺口（库内 80% 的岗位目前如此）、不是学员的问题：文案别说
>   「你的画像没覆盖」，**也别引导去做诊断**——学员做了照样算不出来，
>   缺的是运营配置，等岗位标准补齐即可
> - `none` —— 该岗位尚未配置技能构成，或该学员无任何证据。`match_score` 为 **null**；
>   配文「尚无评估依据」+ 引导做一次 AI 诊断
>
> **前端接入须知**
> - **`match_score` 可空**。`null` ≠ `0`：0 是「测过但一项都没达标」的结论，
>   null 是「没有评分依据」。`?? 0` 会把后者渲染成前者，学员读成「完全不匹配」。
>   建议 null 时保留分数的位置与字号，用虚线框 +「? %」占位（`.score.unknown`），
>   旁边给上面列出的对应说明文案。
> - `partial_baseline` / `no_baseline` 的阈值与判定**全在服务端**
>   （`config.PARTIAL_BASELINE_PCT` = 30%）：前端不要自己拿 `no_baseline_weight`
>   比阈值，每个页面各定一次迟早不一致。
> - `partial_baseline` 时证据强度并没有丢：`estimated`（实测 or 推断）、
>   `diagnosis`（测过没测过）、`profile` 都还在，需要时可以叠加显示。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `occupation` | `OccupationBrief` | 是 | 岗位摘要 |
| `match_score` | number |  | 匹配度 0–100。**可空**：无证据（no_overlap / none）或无基准（no_baseline）时为 null。**为 null 时不要显示 0%、也不要 `?? 0` 兜底** —— 0% 是「测过但一项都没达标」的结论，null 是「没有评分依据」，学员会把前者读成「我完全不行」。建议用虚线框 +「? %」占位，并展示 source 对应的说明文案 |
| `source` | `diagnosis` \| `assessment` \| `memory` \| `partial_baseline` \| `no_overlap` \| `no_baseline` \| `none` | 是 | 展示这个分数时最该先提醒什么；每个取值的含义与建议文案见模型说明 |
| `score_status` | string |  | 算分结果的机器可读原因，取值与 `AssessmentReportOut.score_status` 同一套（含含义与建议文案的表格见那边）：ok / partial_baseline / no_skills / no_baseline / no_weight / no_evidence。`source` 描述的是「这个数最该配什么提示」，这里描述的是「为什么算得出/算不出」；两者共用同一个 30% 阈值，不会一个说 partial 一个说 ok。**刻意不声明成枚举**：历史数据与降级分支可能带来词表外的值，响应模型不该为此整页 500 |
| `estimated` | boolean |  | 是否为推断值：`assessment` / `memory` 来源为 true（拿已有画像现算），`diagnosis` 为 false（该岗位实测）。UI 上应与实测分区分；`source=partial_baseline` 盖住了证据强度时，它是「实测还是推断」的唯一依据（默认 `False`） |
| `reason` | string |  | 无法计算、或分数只能当参考时的原因说明（可直接展示给学员）。`source=partial_baseline` 时这里会写明缺了多少权重的要求档 |
| `skill_total` | integer |  | 岗位技能总数 |
| `matched_count` | integer |  | 已达标技能数 |
| `covered_count` | integer |  | 有证据覆盖的技能数；为 0 时 match_score 无意义 |
| `coverage` | number |  | 证据覆盖的权重百分比 0–100；分母是**可评分**技能权重（不含缺要求档的项） |
| `no_baseline_weight` | number |  | 因缺要求档而无法评分的技能权重占岗位**全部**技能权重的百分比 0–100；100 表示整个岗位没配能力要求。**超过 30% 时服务端已把 source 与 score_status 降级成 partial_baseline**，前端不必自己比阈值，但展示「仅供参考」时可以把这个数字带出来。与 coverage 是两种不同的缺失 |
| `items` | `MatchItem`[] |  | 全部技能明细 |
| `strengths` | `MatchItem`[] |  | 已达标项（仅可评分项） |
| `gaps` | `MatchItem`[] |  | 未达标项（仅可评分项），按权重降序 |
| `no_baseline` | `MatchItem`[] |  | 无法评分的技能（岗位要求档缺失或越界），按权重降序；既不在 strengths 也不在 gaps。需要运营补要求档 |
| `radar` | `Match`RadarOut`` |  | 单系列雷达图（按技能大类聚合的达成率）；无数据时为空对象 |
| `diagnosis` | `DiagnosedBrief` |  | 该岗位的历史诊断摘要；没测过为 null |

### PositionOut

> 岗位（产品 position，图侧 occupation）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 节点 id |
| `name` | string |  | 名称 |
| `raw_name` | string |  | 原始名称（未做展示名替换） |
| `type` | string |  | 类型（默认 `position`） |
| `kg_type` | string |  | 图侧节点类型 |
| `region` | string |  | 地区，如 CN |
| `status` | integer |  | 状态 |
| `desc` | string |  | 简介 |
| `tier` | string \| integer |  | 职级/推荐档 |
| `demand` | string \| integer \| number \| boolean |  | 需求热度（取自 attrs，可能是数字） |
| `salary` | string \| integer \| number \| boolean |  | 薪资区间（取自 attrs，可能是数字） |
| `source_url` | string |  | 来源链接 |
| `attrs` | object |  | 自由属性（无数据库约束的 JSON 列，键随数据来源而异） |
| `edge` | `backend__api__schemas_biz__EdgeBrief` |  | 与专业/行业的连边摘要 |
| `counts` | `RelationCounts` |  | skill=逻辑技能数；major=对口专业数；industry=归属行业数 |
| `industries` | `IndustryRef`[] |  | 归属行业（多选）；原型可取首项 |
| `industry_id` | string |  | industries[0].id，便于单行业展示 |
| `industry_name` | string |  | industries[0].name |
| `progressions` | `ProgressionBrief`[] |  | 成长通道（最多 3 条，按方向去重）。完整多跳链路仍看 `/v1/student/positions/progressions` |
| `top_skills` | `TopSkillBrief`[] |  | 核心胜任力要求（按权重降序前 4 项）。完整技能构成看 `/v1/student/positions/skill-composition` |

### PositionProgressionsOut

> 岗位晋升链路（GET /v1/student/positions/progressions）。
>
> 独立于岗位详情与技能构成接口，加卡片不动既有契约。
>
> `advances_to` 是 **1:N**：一个岗位可以有多条向上路径（本方向纵深 / 管理路线 /
> 跨方向转型）。早期本体误定为 1:1，读路径 `LIMIT 1`，于是 Java 有三条向上路径
> 却只显示「全栈工程师」一条。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `occupation` | `ProgressionOccBrief` | 是 | 查询的岗位 |
| `paths` | `ProgressionPath`[] | 是 | 全部晋升路径，长链在前 |
| `path_count` | integer | 是 | 路径条数（≥0.0） |
| `truncated` | boolean |  | 是否因超出上限被截断（枢纽岗位可展开出上百条）（默认 `False`） |
| `note` | string |  | 口径说明 |

### PrereqBody

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `prereq_skill_key` | string | 是 | 先修逻辑技能 key |
| `evidence` | string |  | 判定依据 |
| `region` | string |  | 地区，如 CN（默认 `CN`） |

### PrereqDeletedOut

> 删除先修关系的回执。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `deleted` | `True` | 是 | 固定 true |
| `skill_key` | string | 是 | 技能聚合主键 |
| `prereq_skill_key` | string | 是 | 被移除的先修技能 |

### PrereqOut

> 一条先修关系：学 `skill_key` 之前应先具备 `prereq_skill_key`。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `skill_key` | string | 是 | 技能聚合主键 |
| `prereq_skill_key` | string | 是 | 先修技能的聚合主键 |
| `skill_name` | string |  | 技能展示名 |
| `prereq_skill_name` | string |  | 先修技能展示名 |
| `region` | string | 是 | 地区，如 CN |
| `evidence` | string |  | 判定依据 |
| `confidence` | string |  | 来源等级：manual_seed / official / derived / ai_inferred |
| `created_at` | string |  | 创建时间 ISO8601 |

### PrereqSetBody

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `prereq_skill_keys` | string[] |  | 先修技能的聚合主键列表 |
| `region` | string |  | 地区，如 CN（默认 `CN`） |

### ProfessionDetailOut

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `profession` | `ProfessionOut` | 是 |  |
| `positions` | `PositionOut`[] |  | 对口岗位 |
| `ladder` | `LadderStep`[] |  | 成长阶梯 |

### ProfessionListOut

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `page` | integer | 是 | 页码，从 1 起 |
| `page_size` | integer | 是 | 每页条数 |
| `total` | integer | 是 | 总条数 |
| `total_pages` | integer | 是 | 总页数 |
| `items` | `ProfessionOut`[] | 是 | 当前页数据 |

### ProfessionOut

> 专业（产品口径 profession，图侧 type=major）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 节点 id |
| `name` | string |  | 展示名 |
| `raw_name` | string |  | 原始 name |
| `type` | string |  | 固定 profession（默认 `profession`） |
| `kg_type` | string |  | 图节点类型 major |
| `region` | string |  | 地区，如 CN |
| `status` | integer |  | 1=已发布 0=草稿 2=停用（映射） |
| `desc` | string |  | 简介 |
| `industry` | string \| integer \| number \| boolean |  | 行业/门类提示（取自 attrs，类型不受约束） |
| `code` | string \| integer \| number \| boolean |  | 专业代码（取自 attrs，可能是数字） |
| `level` | string \| integer \| number \| boolean |  | 层次：专业是 `voc_associate` 这类字符串，但同一个键在别的节点类型上是 int，所以声明成标量联合 |
| `level_zh` | string \| integer \| number \| boolean |  | 学历层次中文名 |
| `source_url` | string |  | 来源链接 |
| `attrs` | object |  | 自由属性（无数据库约束的 JSON 列，键随数据来源而异） |
| `counts` | `RelationCounts` |  | occupation=对口岗数；skill=关联逻辑技能数 |

### ProgressOut

> 答题进度。三个计数都来自业务表实时查询，不是内存态。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `asked` | integer | 是 | 已出题数（≥0.0） |
| `answered` | integer | 是 | 已作答题数（≥0.0） |
| `grading` | integer | 是 | 仍在后台判分的开放题数；结算接口会等它归零（≥0.0） |
| `target_total` | integer |  | 本场目标题数；进程重启后可能为 null，不要用它判断是否出完 |

### ProgressionBrief

> 岗位卡片上的一条「成长通道」：往哪个方向、最终到哪个岗位。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `direction` | string | 是 | 方向名：本方向纵深 / 管理路线 / 技术纵深 / 跨方向转型 / 向上发展 |
| `target` | `ProgressionTarget` | 是 | 这条通道的终点岗位 |
| `depth` | integer | 是 | 要走几跳（≥1.0） |

### ProgressionHop

> 路径上的一跳。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `from` | `ProgressionOccBrief` | 是 | 起点岗位 |
| `to` | `ProgressionOccBrief` | 是 | 终点岗位 |
| `direction` | string | 是 | 方向：本方向纵深 / 技术纵深 / 管理路线 / 跨方向转型 / 向上发展 |
| `confidence` | string |  | 置信度，晋升边目前均为 ai_inferred |
| `evidence` | string |  | 判定依据（LLM 给出） |
| `unlock_skills` | `SkillGapOut`[] |  | 进阶要补的技能（最多 6 项，按权重倒序） |

### ProgressionLink

> 晋升链的一段。只保留两端都在当前画布内的边，否则会画出断头箭头。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `from` | string | 是 | 低阶岗位 id |
| `to` | string | 是 | 高阶岗位 id |
| `from_name` | string |  | 低阶岗位名 |
| `to_name` | string |  | 高阶岗位名 |
| `from_level` | integer |  | 低阶岗位职级 1–5，来自 attrs.level |
| `to_level` | integer |  | 高阶岗位职级 1–5，来自 attrs.level |
| `from_level_code` | string |  | 低阶职级码（L1–L5），由 level 派生**不入库**；前端直接用，不要自己拼 'L'+level |
| `to_level_code` | string |  | 高阶职级码（L1–L5），同上 |
| `from_level_name` | string |  | 低阶职级名（入门/专员/资深/经理/总监） |
| `to_level_name` | string |  | 高阶职级名，同上 |
| `rel_type` | string |  | 固定 advances_to（默认 `advances_to`） |
| `confidence` | string |  | 置信度 |
| `evidence` | string |  | 建边依据 |

### ProgressionNodeOut

> 绑定晋升链路上的一个岗位。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 岗位节点 id |
| `name` | string |  | 岗位名；节点已归档时为 null |
| `level` | integer |  | 职级 1–5；未标注为 null |
| `level_code` | string |  | 职级代码 L1–L5，由 level 派生 |
| `level_name` | string |  | 职级名（入门/专员/资深/经理/总监） |
| `is_current` | boolean |  | 是否为链路起点，即当前目标岗位（默认 `False`） |
| `missing` | boolean |  | 该岗位在图里已找不到（被归档）。**仍占位保留而不是跳过**——悄悄少一跳会让「下一目标」凭空前移一级（默认 `False`） |

### ProgressionOccBrief

> 晋升链路上的一个岗位节点。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 岗位节点 id |
| `name` | string |  | 岗位名 |
| `level` | integer |  | 职级，来自 `attrs.level`（唯一真源） |
| `level_code` | string |  | 职级代号，由 level **派生**不入库（双写会不一致） |
| `level_name` | string |  | 职级中文名，同样派生 |
| `description` | string |  | 岗位描述 |

### ProgressionPath

> 一条完整晋升路径，可能多跳。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `target` | `ProgressionOccBrief` | 是 | 路径终点岗位 |
| `direction` | string | 是 | 首跳的方向，用作 tab 标题的分类 |
| `depth` | integer | 是 | 跳数（≥1.0） |
| `hops` | `ProgressionHop`[] | 是 | 逐跳明细 |

### ProgressionTarget

> 成长通道的终点岗位（卡片只需要这三样）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string |  | 岗位节点 id |
| `name` | string |  | 岗位名 —— 卡片上显示这个 |
| `level` | integer |  | 职级 1–5；缺配时为 null，前端别拼成 Lundefined |

### QuestionOut

> 一道题。choice 与 open 共用此模型，按 `type` 区分有效字段。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `index` | integer | 是 | 题号，作答时回传；也是本场测评内的稳定序号（≥0.0） |
| `type` | `choice` \| `open` | 是 | choice=情景判断选择题（当场判分）；open=开放问答题（后台判分） |
| `variant` | string |  | 题目来源变体：sjt=模型生成的情景判断题；self_report=网关不可用时的自评降级题（考核力弱，报告里据此标注）；generic=通用开放题 |
| `skill_key` | string |  | 考查的技能（聚合主键，非某一档节点 id） |
| `skill_name` | string |  | 展示名 —— **题干与列表都显示这个**。`skill_key` 是 ASCII code（SK0123456789），拿它渲染就是一串哈希 |
| `category` | string |  | 技能大类，雷达图按它聚合 |
| `required_level` | integer |  | 岗位对该技能要求的档位 1–5，用于判定是否达标 |
| `weight` | number |  | 该技能在岗位 requires 中的权重，Σ≈1 |
| `prompt` | string | 是 | 题干 |
| `options` | `ChoiceOption`[] |  | 选择题的选项；开放题为空数组 |
| `rubric` | string[] |  | 开放题评分要点，前端可作答题提示展示；选择题为空 |
| `min_chars` | integer |  | 开放题建议最少字数；选择题为 null |
| `planned_total` | integer |  | 本场预计总题数；分批出题时是预估值，以 question_end 为准 |

### RadarOut

> 双系列雷达图（学员实测 vs 岗位标准）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `axis_type` | `skill` \| `category` | 是 | 轴的口径：skill=按技能名（默认，与原型一致）；实测技能不足 3 项时回落为 category=按技能大类聚合 |
| `categories` | string[] | 是 | 各轴名称 |
| `series` | `RadarSeries`[] | 是 | 两条系列：实测与要求 |
| `scores` | integer[] | 是 | 兼容字段，等同 series[key=user].scores；新接入请用 series |

### RadarSeries

> 雷达图的一条系列。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `key` | `user` \| `required` | 是 | user=学员实测；required=岗位要求 |
| `name` | string | 是 | 系列展示名 |
| `scores` | integer[] | 是 | 各轴得分 0–100，顺序与 categories 一致 |

### RefOut

> id + name 的轻引用。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string |  | 节点 id |
| `name` | string |  | 展示名 |

### RelationCounts

> 关联节点数量（联读聚合；按 type 有意义键非 0）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `major` | integer |  | 关联专业数（默认 `0`） |
| `occupation` | integer |  | 关联岗位数（默认 `0`） |
| `skill` | integer |  | 逻辑技能数（DISTINCT skill_key），非 L 扁平行数（默认 `0`） |
| `industry` | integer |  | 关联行业数（默认 `0`） |
| `course` | integer |  | 关联课程数（默认 `0`） |
| `level` | integer |  | 技能 bundle 下已有 L 档数（默认 `0`） |
| `skill_aggregated` | integer |  | 专业经岗位两跳汇总的技能数（skill 为直连数）（默认 `0`） |
| `weight_sum` | number |  | 岗位 requires 权重和，**小数**；归一化后应为 1.0（默认 `0.0`） |

### ReportCounts

> 报告的分项计数。
>
> `tested + untested == skill_total` 恒成立；但 `strength + gap` 只数**测到且有基准**
> 的项，缺要求档的项两边都不进（数量见 `no_baseline` 列表长度），所以
> `strength + gap + untested` 可能小于 `skill_total`。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `skill_total` | integer | 是 | 岗位技能总数（≥0.0） |
| `tested` | integer | 是 | 本次实测到的技能数（≥0.0） |
| `untested` | integer | 是 | 未覆盖的技能数（≥0.0） |
| `strength` | integer | 是 | 达标项数（仅可评分项）（≥0.0） |
| `gap` | integer | 是 | 未达标项数（仅可评分项）（≥0.0） |

### ReportItem

> 报告里的一项技能：岗位要求 vs 学员实测。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `skill_key` | string | 是 | 技能聚合主键（ASCII code） |
| `skill_name` | string |  | 展示名 —— **页面上要显示这个**。`skill_key` 从 2026-08-19 起是 ASCII code（形如 SK0123456789），拿它渲染就是一串哈希 |
| `category` | string | 是 | 技能大类，缺失时为「未分类」 |
| `required_level` | integer |  | 岗位要求档 1–5 |
| `required_label` | string |  | 要求档文案，如「熟练」 |
| `measured_level` | integer |  | 实测档 1–5；未测为 null |
| `measured_label` | string |  | 实测档文案 |
| `weight` | number | 是 | 该技能在岗位中的权重，Σ≈1 |
| `weight_pct` | integer |  | 权重百分比（四舍五入），便于直接展示 |
| `ratio` | number | 是 | 达成比 = 实测/要求，封顶 1.0。`scorable=false`（岗位没给要求档）时这里是「有实测即 1.0」的展示值，**不参与匹配度计算**，不要拿它当达标结论 |
| `ok` | boolean | 是 | 是否达标（实测 ≥ 要求）；无要求档时恒为 false |
| `scorable` | boolean |  | 该项能否评分。false = 岗位对这个技能的要求档缺失或越界（无基准），**分子分母都不计入 match_score**，也不进 strengths / gaps —— 没有基准既谈不上达标、也谈不上差距。这类项汇总在 `no_baseline` 里。历史报告（新增此字段之前生成）缺该键，按 true 处理（默认 `True`） |
| `tested` | boolean | 是 | 本次是否实际考到。false 时 measured_* 为 null、ratio 为 0，仍按 0 分计入 match_score（只有 tested_match_score 把它排除在分母外） |
| `source` | string |  | 档位来源：choice / llm / rule |
| `evidence_score` | number |  | 证据充分度 0–1 |
| `capped` | boolean |  | 是否因证据不足被压档 |
| `urgency` | number |  | 补强紧迫度 = 权重 × 差距档数；短板排序用，越大越该先补（默认 `0.0`） |

### RequiredSkillRef

> 岗位要求的一项技能。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string |  | 技能节点或 bundle id |
| `skill_name` | string |  | 技能名 |
| `skill_key` | string |  | 技能聚合主键 |
| `required_level` | integer |  | 要求档 1–5 |
| `weight` | number |  | 权重 |
| `category` | string |  | 技能大类 |

### ResourceItem

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 节点 id |
| `title` | string |  | 标题 |
| `type` | string |  | video\|practice\|article\|course… |
| `status` | integer |  | 状态 |
| `provider` | string |  | 提供方 |
| `url` | string |  | 链接 |
| `skill_hint` | string |  | 技能提示 |
| `desc` | string |  | 简介 |

### ResourceListOut

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `page` | integer | 是 | 页码，从 1 起 |
| `page_size` | integer | 是 | 每页条数 |
| `total` | integer | 是 | 总条数 |
| `total_pages` | integer | 是 | 总页数 |
| `items` | `ResourceItem`[] | 是 | 当前页数据 |

### ResumeDiagBody

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `content_text` | string | 是 | 简历正文（粘贴文本）。文件上传后续可扩展 multipart（长度≥1） |
| `target_occupation_id` | string |  | 目标岗位 id；不传则用已设 goal 或不对标岗位 |

### ResumeExtractOut

> 简历文件 → 文本。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `content_text` | string | 是 | 抽取出的简历正文 |
| `filename` | string |  | 原始文件名 |
| `chars` | integer |  | 正文字数（≥0.0，默认 `0`） |
| `engine` | string |  | 抽取引擎：docx / pdf / plain |

### ResumeSampleOut

> 示例简历，供前端一键填充。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `content_text` | string | 是 | 示例简历正文 |
| `note` | string | 是 | 用法说明 |

### SharedSkill

> 多个对口岗位共同要求的技能。命中岗位越多，越是这个专业的「专业基本功」。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `skill_key` | string |  | 技能聚合主键（ASCII code，形如 SK0123456789） |
| `skill_name` | string |  | 技能展示名 —— **页面上要显示这个**。`skill_key` 从 2026-08-19 起是 code，拿它渲染就是一串哈希（能力全景页的共享技能 chip 踩过） |
| `category` | string |  | 技能大类 code，见 /v1/kg/skill-categories |
| `category_name` | string |  | 技能大类展示名（由 code 派生，不入库） |
| `occ_count` | integer |  | 有多少个对口岗位共同要求它；命中越多越是「专业基本功」 |
| `levels` | string[] |  | 这些岗位要求到的档位码（如 L2 / L3） |
| `occupation_ids` | string[] |  | 要求它的岗位 id，供前端点开定位 |

### SimpleRadar

> 单系列雷达图：按技能大类聚合的达成率。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `categories` | string[] |  | 各轴名称（技能大类） |
| `scores` | integer[] |  | 各轴达成率 0–100，顺序与 categories 一致 |

### SkillBundleBody

> 一次录入逻辑技能 + L1–L5 对象 + 岗位构成权重。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `skill_key` | string |  | 聚合主键；缺省用 name |
| `name` | string |  | 展示名；缺省用 skill_key |
| `region` | string |  | 地区，如 CN（默认 `CN`） |
| `scale` | string |  | 等级尺度（默认 `l1_l5`） |
| `levels` | map<string, `SkillLevelObjIn` \| string \| object> |  | 键 L1–L5；值为对象（推荐）或字符串。创建时至少一档；更新可省略以保留原档 |
| `occupation_links` | ``OccupationLink`In`[] |  | 岗位技能构成：权重在 requires 边上 |
| `occupation_ids` | string[] |  | 兼容：无权重时的岗位 id 列表 |
| `category` | string |  | 技能大类 **code**（TECH / OPERATE …），候选见 `GET /v1/kg/skill-categories`。也接受中文名或别名，服务端会归一；认不出的落兜底 `UNSORTED`（待归类），不会硬塞进某一类。留空同样落兜底 |
| `description` | string |  | 描述 |
| `source_url` | string |  | 来源链接 |
| `confidence` | string |  | 置信度（默认 `manual_seed`） |
| `attrs` | object |  | 自由属性（无数据库约束的 JSON 列，键随数据来源而异） |

### SkillBundleBrief

> 聚合后的技能 bundle 摘要（一个 skill_key 下的 L1–L5 汇总）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string |  | bundle:{region}:{skill_key} |
| `skill_key` | string |  | 技能聚合主键 |
| `skill_name` | string |  | 技能名（去等级后缀） |
| `region` | string |  | 地区，如 CN |
| `category` | string |  | 技能大类 code，见 /v1/kg/skill-categories |
| `category_name` | string |  | 技能大类展示名（由 code 派生，不入库） |
| `available_levels` | integer[] |  | 已配齐的档位 1–5 |
| `missing_levels` | integer[] |  | 尚缺的档位 1–5 |

### SkillBundleListOut

> 技能库分页列表（逻辑技能 bundle，多档已聚合）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `items` | `SkillOut`[] | 是 | 当前页技能 |
| `page` | integer | 是 | 页码，从 1 起（≥1.0） |
| `page_size` | integer | 是 | 每页条数（≥1.0） |
| `total` | integer | 是 | 总条数（≥0.0） |
| `total_pages` | integer | 是 | 总页数（≥0.0） |

### SkillBundlePreviewOut

> 技能 bundle 写入前的影响面预览：会建几个节点、几条边。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `skill_key` | string | 是 | 技能聚合主键 |
| `level_codes` | string[] | 是 | 将要写入的档位编码 |
| `level_count` | integer | 是 | 档位数（≥0.0） |
| `occupation_count` | integer | 是 | 关联岗位数（≥0.0） |
| `occupation_links` | `OccupationLink`[] | 是 | 关联岗位明细 |
| `will_create_nodes` | integer | 是 | 预计新建节点数（≥0.0） |
| `will_create_edges` | integer | 是 | 预计新建边数（≥0.0） |

### SkillCategory

> 技能分类字典项。**`code` 是真源**，`kg_node.category` 存的就是它。
>
> 展示一律用 `name`（从 `kg_skill_category` 表取），前端不要按 code 硬编码文案 ——
> 改名只动那张表，不动 12062 条技能数据。
>
> ⚠ 这个模型曾只声明 id/name，`skill_count` 等字段被 Pydantic **静默丢弃**，
> 管理台拿不到数量。加字段时记得同步这里。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `code` | string | 是 | 分类 code，如 TECH / OPERATE；写入 kg_node.category |
| `id` | string | 是 | 等同 code，保留给旧前端 |
| `name` | string | 是 | 展示名 |
| `description` | string |  | 这一类涵盖什么，管理台分类时的判断依据（默认 ``） |
| `sort_order` | integer |  | 展示顺序，也是学习推进顺序（默认 `999`） |
| `is_fallback` | boolean |  | 是否兜底类（待归类）。新技能认不出时落这里，代表数据缺口（默认 `False`） |
| `skill_count` | integer |  | 该类下的**逻辑技能**数（按 skill_key 去重，不是节点数）（≥0.0，默认 `0`） |

### SkillCategoryGroup

> 技能大类分区。按职业功能推进顺序排，不按技能数量——否则展示顺序会和箭头方向矛盾。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `key` | string | 是 | 技能大类 **code**（TECH / OPERATE …），字典见 `GET /v1/kg/skill-categories` |
| `name` | string |  | 技能大类展示名。分区标题用这个，别拿 key 显示给人看（默认 ``） |
| `rank` | integer | 是 | 推进顺序序号，越小越靠前 |
| `skills` | `SkillNodeBrief`[] |  | 该区技能 |

### SkillCompositionAdminOut

> 岗位/专业的技能构成（管理台视图）。
>
> 岗位 `requires` 技能带权重且需归一化（Σ≈1）；专业 `covers` 技能无权重、不归一。
> `weighted` 就是这两种情况的区分标志。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `node` | `CompositionNodeHeader` | 是 | 节点头部信息 |
| `relation` | `requires` \| `covers` | 是 | requires=岗位要求技能（带权重）；covers=专业覆盖技能（无权重） |
| `weighted` | boolean | 是 | 是否带权重；covers 为 false |
| `items` | `CompositionItem`[] | 是 | 技能构成明细 |
| `weight_sum` | number |  | 权重之和，**小数**；weighted=false 时为 null |
| `normalized` | boolean | 是 | 权重是否已归一（0.995–1.005） |
| `can_normalize` | boolean | 是 | 是否支持一键归一化（等同 weighted） |
| `counts` | `CompositionCounts` | 是 | 计数 |
| `normalized_from` | number |  | 归一化前的权重和；仅归一化接口返回 |

### SkillCompositionOut

> 岗位技能构成（逻辑技能 + 边权重）。
>
> 权重只认 `requires` 边上的 weight，节点 `attrs.weight_pct` 仅历史兼容。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `occupation` | `OccupationBrief` | 是 | 岗位摘要 |
| `skills` | `SkillOut`[] | 是 | 逻辑技能列表（多档已聚合成 bundle） |
| `skill_count` | integer | 是 | 技能数（≥0.0） |
| `weight_sum` | number | 是 | 权重之和，**小数**；归一化后应≈1.0，不要声明成 int |
| `weighted_skill_count` | integer | 是 | 带权重的技能数（≥0.0） |
| `weight_sum_ok` | boolean | 是 | 权重和是否在容差内（0.85–1.15）；false 表示该岗位权重待归一化 |
| `note` | string |  | 口径说明 |

### SkillDeletedOut

> 删除逻辑技能的结果。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `deleted` | boolean | 是 | 恒为 true；失败走 4xx |
| `skill_key` | string | 是 | 被删的逻辑技能名 |
| `archived_nodes` | integer |  | 归档的档位节点数（L1–L5 里实际存在的）（≥0.0，默认 `0`） |
| `archived_edges` | integer |  | 一并归档的关联边数（≥0.0，默认 `0`） |
| `occupations_affected` | integer |  | 删除前还挂着这个技能的岗位数，供前端提示影响面（≥0.0，默认 `0`） |
| `discarded_drafts` | integer |  | 一并丢弃的未发布草稿行数。留着的话这条已删技能会继续挂在「待发布」页，点发布等于复活它 —— 删除立即生效，不留能撤销它的草稿（≥0.0，默认 `0`） |

### SkillGapOut

> 进阶要补的一项技能：目标岗位要求里，当前岗位没有的 / 要求更高的。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `skill_key` | string | 是 | 技能聚合主键（ASCII code） |
| `skill_name` | string |  | 展示名 —— **页面上要显示这个**。`skill_key` 从 2026-08-19 起是 ASCII code（形如 SK0123456789），拿它渲染就是一串哈希 |
| `category` | string |  | 技能大类 code，见 /v1/kg/skill-categories |
| `category_name` | string |  | 技能大类展示名（由 code 派生，不入库） |
| `required_level` | integer |  | 目标岗位要求的档位 1–5 |
| `required_label` | string |  | 档位名称，从 skill_level_meta 读，前端不要硬编码 |
| `current_required_level` | integer |  | 当前岗位对该技能的要求档；null 表示当前岗位根本不要求 |
| `weight` | number |  | 该技能在目标岗位能力结构中的权重，**小数** |

### SkillLevelItem

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `level` | integer |  | 产品等级 1–5（1 了解 → 5 专家），唯一判定依据 |
| `level_label` | string |  | 档位文案，取自 skill_level_meta |
| `node_id` | string |  | 对应的图节点 id |
| `description` | string |  | 描述 |
| `status` | string |  | 状态 |
| `weight` | number |  | 权重 |

### SkillLevelMeta

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `level` | integer | 是 | 1–5 |
| `name` | string | 是 | 了解/掌握/熟练/精通/专家 |
| `base_score` | integer | 是 | 基准分 |

### SkillLevelObjIn

> 单档对象；可扩展 criteria / evidence 等。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `label` | string |  | 了解/掌握/… |
| `description` | string |  | 能力等级描述 |
| `criteria` | string[] |  | 该档的考核要点，一条一句；不要塞对象 |
| `evidence` | string |  | 判定依据 |

### SkillListOut

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `page` | integer | 是 | 页码，从 1 起 |
| `page_size` | integer | 是 | 每页条数 |
| `total` | integer | 是 | 总条数 |
| `total_pages` | integer | 是 | 总页数 |
| `items` | `SkillOut`[] | 是 | 当前页数据 |
| `view` | string |  | bundle \| level |

### SkillNodeBrief

> 技能图里的一个技能节点。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `skill_key` | string |  | 技能聚合主键（ASCII code，形如 SK0123456789） |
| `skill_name` | string |  | 技能展示名 —— **图上的节点标签用这个**。`skill_key` 从 2026-08-19 起是code，拿它当标签就是一串哈希。这里**没有** `name` 字段：它与 skill_name 装同一个值，留着只会让前端每次判「用哪个」，判错了不报错、只是显示不对 |
| `depth` | integer |  | 前置层深；0 表示无前置，可直接学 |
| `required_level` | integer |  | 岗位要求档 1–5 |
| `weight` | number |  | 权重 |

### SkillOptionLevel

> 备选技能的一个档位。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `level` | integer |  | 产品档 1–5 |
| `level_label` | string |  | 档位文案 |
| `requirement` | string |  | 该档能力要求描述 |

### SkillOptionOut

> 备选技能（下拉用），按 skill_key 聚合。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `skill_key` | string | 是 | 技能聚合主键（ASCII code，形如 SK0123456789） |
| `skill_name` | string |  | 技能展示名 —— **下拉框里该显示这个**，skill_key 是给接口用的 code |
| `category` | string |  | 技能大类 |
| `available_levels` | integer[] |  | 已配齐的档位 1–5 |
| `levels` | `SkillOptionLevel`[] |  | 各档明细 |
| `level_completeness` | string | 是 | 档位完整度，形如「3/5」——五档没配齐的技能选了会缺档 |

### SkillOut

> 技能：默认逻辑技能 bundle（多 L 已聚合）；view=level 时为单档扁平行。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | bundle:{region}:{skill_key} 或 skill_level 节点 id |
| `name` | string |  | 名称 |
| `skill_name` | string |  | 技能名（去等级后缀） |
| `skill_key` | string |  | 聚合主键 |
| `level_label` | string \| integer \| number \| boolean |  | 等级文案或要求档（取自 attrs） |
| `status` | string |  | 逻辑技能的聚合状态：各档一致时取该值，不一致为 `mixed`（如 L1–L3 已发布、L4 还是草稿）。取值 published / draft / disabled / mixed |
| `record_status` | string |  | **记录状态 = 最新版本的状态**：任一档有草稿 → `draft`，否则等于 `status`。管理台列表的状态列显示这个（仅管理台口径返回） |
| `has_draft` | boolean |  | 这个技能有没有未发布的改动（任一档有草稿行即为 true） |
| `draft_change` | string |  | 草稿改的是什么：node=档位节点字段 \| edges=关联 \| both |
| `version` | integer |  | 版本号。bundle 没有自己的行，取 L1–L5 线上行的**最大** version。仅管理台口径返回；拿不到时前端应显示「—」，**不要兜底成 V1** —— 那是个看起来像真值的假版本号 |
| `version_label` | string |  | 版本展示文案，如 `V3` |
| `owner` | string |  | 业务负责人 user-id（各档第一个非空） |
| `owner_name` | string |  | 业务负责人姓名 |
| `updated_by_name` | string |  | 最近修改人姓名 |
| `type` | string |  | 类型（默认 `skill`） |
| `kg_type` | string |  | 图侧节点类型 |
| `region` | string |  | 地区，如 CN |
| `desc` | string |  | 简介 |
| `required_level` | integer |  | 岗位要求档 L1–L5（int） |
| `weight` | number |  | 岗位要求权重（0–1 小数） |
| `weight_pct` | integer |  | 权重百分比，直接展示用（学员端岗位详情的「权重」列读的就是这个）。上游一直在算，只是这里没声明 —— 学员判断不出哪个技能更重要 |
| `is_core` | boolean |  | 核心技能标记（权重 ≥ 30%）；同样是上游已算、这里补声明 |
| `category` | string |  | 技能大类 **code**（TECH / OPERATE …），字典见 `GET /v1/kg/skill-categories` |
| `category_name` | string |  | 技能大类展示名，由 code 连字典表取得，**不入库**；前端展示用这个 |
| `source_url` | string |  | 来源链接 |
| `attrs` | object |  | 自由属性（无数据库约束的 JSON 列，键随数据来源而异） |
| `edge` | `backend__api__schemas_biz__EdgeBrief` |  | 与岗位/专业的连边摘要——岗位要求的权重就在这里 |
| `levels` | `SkillLevelItem`[] |  | 已有等级节点（聚合视图） |
| `level_descriptions` | map<string, string> |  | L1–L5 能力描述文案 |
| `available_levels` | integer[] |  | 已配齐的档位 1–5 |
| `missing_levels` | integer[] |  | 尚缺的档位 1–5 |
| `counts` | `RelationCounts` |  | 关联计数 |
| `prerequisites` | `SkillPrereqBrief`[] |  | 先修技能（学这个之前应先具备）。`skill_key` 是**先修技能的** key |
| `created_at` | string |  | 创建时间 ISO8601，取各档 `created_at` 的最大值；列表默认按它倒序 |

### SkillPrereqBrief

> 技能详情里带的一条先修关系（精简形态，完整形态见 `PrereqOut`）。
>
> **`skill_key` 指的是先修技能自己的 key**，不是被查询的那个技能 ——
> 这是 `get_skill_bundle` 已有的形状（`skill_key = p["prereq_skill_key"]`），
> 前端按它渲染，这里如实声明，不改名以免动到前端。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `skill_key` | string |  | 先修技能的聚合主键 |
| `name` | string |  | 先修技能展示名（当前等于 skill_key） |
| `evidence` | string |  | 判定依据 |

### SkillPrereqLink

> 技能前置关系：学 `to` 之前应先具备 `from`。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `from` | string | 是 | 先修技能的 skill_key |
| `to` | string | 是 | 后继技能的 skill_key |
| `confidence` | string |  | 来源等级：manual_seed / official / derived / ai_inferred |
| `evidence` | string |  | 判定依据 |

### SkillsGraphMeta

> 岗位技能图的口径。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `matched` | integer |  | 是否命中岗位（默认 `0`） |
| `skill_total` | integer |  | 技能总数（≥0.0，默认 `0`） |
| `category_count` | integer |  | 技能大类数（≥0.0，默认 `0`） |
| `uncategorized` | integer |  | 未分类的技能数（≥0.0，默认 `0`） |
| `prereq_total` | integer |  | 前置关系数（≥0.0，默认 `0`） |
| `max_depth` | integer |  | 前置层最大深度（≥0.0，默认 `0`） |
| `order` | string |  | 排序口径说明 |
| `region` | string |  | 地区 |

### StageAssessOutput

> 阶段二「对话问答测评」的产出。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `asked` | integer |  | 已出题数（≥0.0，默认 `0`） |
| `answered` | integer |  | 已作答题数（≥0.0，默认 `0`） |
| `grading` | integer |  | 后台判分中的题数（≥0.0，默认 `0`） |

### StageOut

> 前端步骤条的一个节点。三节点固定，不随后端图结构变化。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `key` | `parse` \| `assess` \| `report` | 是 | 阶段标识 |
| `name` | string | 是 | 阶段中文名：简历解析推断 / 对话问答测评 / 综合能力报告 |
| `status` | `pending` \| `active` \| `done` | 是 | pending=灰；active=高亮；done=打勾 |
| `output` | `StageParseOutput` \| `StageAssessOutput` \| `AssessmentReportOut` \| object |  | 该阶段产出，按 key 取对应结构；report 阶段未完成时为空对象 {} |

### StageParseOutput

> 阶段一「简历解析推断」的产出。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `engine` | string |  | 解析引擎：llm=模型抽取；rule=规则兜底；skip=未提供简历 |
| `skill_count` | integer |  | 解析出的技能数（≥0.0，默认 `0`） |
| `skills` | `ParsedSkill`[] |  | 逐项技能与推断档位 |
| `note` | string |  | 降级或失败说明；正常为 null |

### StartBody

> 开始一场测评。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `occupation_id` | string |  | 目标岗位节点 id；不传则取当前锁定的学习目标 |
| `resume_text` | string |  | 简历原文；留空则跳过解析阶段，直接按岗位标准出题 |

### TopSkillBrief

> 岗位卡片上的一项「核心胜任力要求」。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `skill_key` | string | 是 | 技能聚合主键（ASCII code） |
| `skill_name` | string |  | 技能展示名 —— 卡片上显示这个 |
| `required_level` | integer |  | 要求档 1–5 |
| `weight` | number |  | 在该岗位技能构成里的权重（0–1） |

### UserBadgeOut

> 已解锁的成就（成就定义 + 解锁时间）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `code` | string | 是 | 成就编码 |
| `name` | string | 是 | 成就名 |
| `description` | string |  | 成就说明 |
| `points` | integer |  | 该成就的成长值（默认 `0`） |
| `category` | string |  | 成就分类 |
| `unlocked_at` | string |  | 解锁时间 ISO8601 |

### UserGoalBrief

> 当前学习目标摘要。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` | string |  | UC 用户 id |
| `user_name` | string |  | 用户名 |
| `occupation_id` | string |  | 目标岗位 id |
| `occupation_name` | string |  | 目标岗位名 |
| `major_id` | string |  | 关联专业 id |
| `major_name` | string |  | 关联专业名 |
| `industry_id` | string |  | 所属行业 id |
| `industry_name` | string |  | 所属行业名 |
| `status` | string |  | active=活跃；archived=历史 |
| `created_at` | string |  | 设定时间 ISO8601 |
| `updated_at` | string |  | 更新时间 ISO8601 |

### UserSkillItem

> 学员技能画像的一项（对应 biz_user_skill 一行）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` | string |  | UC 用户 id |
| `skill_id` | string | 是 | 技能 id 或 skill_key |
| `skill_name` | string |  | 技能名 |
| `level` | integer |  | 档位 1–5（≥1.0，≤5.0，默认 `1`） |
| `score` | integer |  | 得分（默认 `0`） |
| `source` | string |  | 来源：self=自评；assessment=测评；resume=简历解析（默认 `self`） |
| `updated_at` | string |  | 更新时间 ISO8601 |

### UserSkillOut

> 学员技能画像的一项（对应 biz_user_skill 一行）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` | string |  | UC 用户 id |
| `skill_id` | string |  | 技能 id 或 skill_key |
| `skill_name` | string |  | 技能名 |
| `level` | integer |  | 档位 1–5（≥1.0，≤5.0，默认 `1`） |
| `score` | integer |  | 得分（默认 `0`） |
| `source` | string |  | 来源：self=自评；assessment=测评；resume=简历解析（默认 `self`） |
| `updated_at` | string |  | 更新时间 ISO8601 |

### backend__api__schemas_biz__EdgeBrief

> 连边摘要：这条记录是**经哪条边**关联进来的。
>
> 同一个节点可能因不同的边被带进列表（岗位既 belongs_to 行业、又被专业
> prepares_for），`rel_type` 与 `weight` 说明的是这一次的关联口径。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string |  | 边 id |
| `rel_type` | string |  | 关系类型：requires / covers / belongs_to / prepares_for |
| `weight` | number |  | 边权重；无权重的关系为 null |
| `confidence` | string |  | 置信度标记，如 ai_inferred |
