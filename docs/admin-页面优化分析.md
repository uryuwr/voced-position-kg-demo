# `/admin` 图谱后台 · 优化分析

> 对象：`frontend/admin.html`（图谱后台 Graph/Table/Raw）
> 定位（据 `docs/图谱前端方案.md` §11、`docs/展示形态与用户路径.md`）：**面向运维/图谱专家**的 Neo4j Browser 中文平替；业务用户走 `/`。
> 因此下列建议以「专家工具的正确性与易用性」为准，不建议把 admin 改成分层卡片。
> 引用位置为分析时的行号，改动后可能漂移。

## 一、逻辑 Bug（优先修）

| # | 现象 | 位置 | 结论 |
| --- | --- | --- | --- |
| P0-1 | **全屏时点节点，详情抽屉看不见**。真原生全屏只渲染全屏元素及其后代，而 `#drawer` 是 `<body>` 直接子节点、不在 `#view-graph` 内；fallback 模式下 `.fs-fallback` 是 `z-index:9999`，抽屉只有 `z-index:20` 被盖住。但全屏点节点仍会 `pinGraphFocus → showDrawer`，即「功能触发了、面板不可见」。 | `.drawer` `admin.html:192-197`；`.fs-fallback` `:104-108`；`pinGraphFocus` `:545-561` | 真实 bug。把 drawer 移入 `#view-graph` 内，或全屏时提升 drawer 层级 / 改用图内浮层。 |
| P0-2 | **力导向「冻结定时器」会吞掉用户的锁定高亮**。`renderGraph` 设了 1.8~5s 后 `replaceMerge` 整段 series 冻结布局；若用户在这段时间内点节点锁定（`applyPinVisual` 设 `layout:none`+fixed+透明度），定时器到点后用**未带 pin 透明度**的 `gNodes/gLinks` 覆盖，锁定态被重置。`applyPinVisual` 未清除该定时器。 | `forceFreezeTimer` `:990-1043`；`applyPinVisual` `:459-529` | 真实竞争。锁定/拖拽时 `clearTimeout(forceFreezeTimer)`，或冻结分支里带上 `pinnedFocusId` 的可见态。 |
| P1-3 | **「已自动改为岗位」提示被立即覆盖**。US/EU 下若 type=专业，`explore` 会提示已自动改（`:1150`），但成功后 `:1209` 无条件重写 `info`，提示几乎闪现即失。 | `:1147-1154` vs `:1209-1214` | 把该提示并入最终 `mode` 文案，或用独立 toast。 |
| P1-4 | **API 状态点不回弹**。`probe()` 只在启动跑一次；后续仅在 explore 失败降级时 `setApi(false)`，成功时不 `setApi(true)`。后端从离线恢复后，状态灯长期停在「离线/降级」。 | `probe` `:1333-1340`，仅 `:1382` 调用一次 | explore 成功时 `setApi(true,…)`，或定时 probe。 |
| P2-5 | **`skillLabel(name, true)` 第二参数是死参数**。函数只声明 `skillLabel(name)`，传的 `true` 被忽略；注释「技能始终带等级」其实靠函数本身实现，参数误导。 | 调用 `:492`，定义 `:665` | 清理死参数，避免后人误解。 |
| P2-6 | **`esc()` 不转义引号**，却用于属性上下文（`title="…"`、`href="…"`）。当前数据受控风险低，但节点名/URL 若含 `"` 会破坏属性甚至注入。 | `esc` `:367-369`；用于 `:1059,1065,1091` | 属性场景增加 `"`/`'` 转义。 |

## 二、功能易用性

| 优先 | 点 | 说明 / 位置 |
| --- | --- | --- |
| P1 | **连改筛选触发多次全量请求+重跑力导向** | region/type/depth 的 `change` 都立即 `explore()`（`:1352-1370`）。想连调 3 个条件=3 次请求+3 次 `chart.clear()` 重绘闪烁。建议加去抖，或改回「改条件→点查询」显式触发（保留深度即时预览可选）。 |
| P1 | **缺「重置视图/复位」** | 力导向拖乱或缩放后无法复位，只能重查。加一个 fitView/复位按钮（echarts 可重设 zoom/center）。 |
| P1 | **无导出** | 方案 Phase C 明确要 CSV 导出；专家后台还应有图片导出。可开启 echarts `toolbox.saveAsImage` + Table 导 CSV。 |
| P2 | **搜索无补全/历史/防抖** | `#q` 仅 Enter 触发（`:1351`）。接 `/v1/search` 做下拉补全会显著提速。 |
| P2 | **深度下拉文案假定固定五维顺序** | 「2跳…→技能 / 3跳…→课程」（`:230-233`）对美欧（无 major、以岗位/技能为主）语义对不上，易误导。文案按地区动态或改中性描述。 |
| P2 | **截断后无「加载更多」** | 命中 `max_nodes` 只提示「已截断」（`:1211`），无法继续看。可给「放宽上限/换更具体词」的明确出口。 |

## 三、UI / 样式

| 优先 | 点 | 说明 / 位置 |
| --- | --- | --- |
| P1 | **顶部工具栏一行塞 8 个控件**，窄屏 `flex-wrap` 后堆成多行较乱 | `.topbar` `:27-52`、`:210-242`。建议分组（搜索区 / 过滤区 / 动作区）或收进「高级」，主行只留搜索+类型+查询。 |
| P1 | **两份图例冗余且措辞不一** | graph 图例 `:265-281` 与 table 图例 `:283-301` 内容重叠；graph 写「课程覆盖 技能↔课程」，而 `REL_ZH` 里 `covers=专业覆盖`、`taught_by=课程覆盖`（`:336-340`），table 图例又多「通向认证」。统一成一份可复用图例、并与 `REL_ZH` 对齐。 |
| P2 | **密图标签恒显示、相互重叠** | `buildGraphNodes` 里 `showLabel` 恒 `true`（`:804`，注释称「以前被关掉」）。节点多时全量标签糊成一片，建议按缩放级别/节点数分级显示。 |
| P2 | **`meta` 行信息过载** | apiSt + 很长的 info + hint 挤一行（`:252-256`、动态 info `:1209`）。信息分层：状态灯独立、操作提示弱化。 |
| P2 | **Raw 视图**大 JSON 无折叠/复制按钮 | `:306-308`、`renderRaw :1073-1075`。加「复制」按钮即可。 |

## 四、UX / 交互

| 优先 | 点 | 说明 / 位置 |
| --- | --- | --- |
| P1 | **无 loading 反馈**，大数据量查询时只有文字「查询中…」+禁用按钮，图区停在旧图 | `explore :1162-1166`。加遮罩/骨架/进度。 |
| P1 | **失败用 `alert` 打断** | `:1236` 双重失败弹 alert，体验割裂。改为页内错误条 + 重试按钮。 |
| P2 | **切到 Table/Raw 时抽屉仍浮在右下遮挡内容** | `.drawer` 固定定位（`:192`），切 tab 不隐藏。切非 graph 视图时收起 drawer。 |
| P2 | **切地区清空已输入搜索词无提示** | `:1352-1358` 重置 `q`/`type`，用户输入丢失。可保留词或提示。 |

## 五、可访问性（P2）

- 搜索框仅有 placeholder、无 `<label>`/`aria-label`（`:213`），屏幕阅读器不友好。
- 图谱交互纯鼠标（滚轮缩放、拖拽），无键盘替代路径。
- 节点类型仅以颜色区分（有文字标签兜底，尚可），可补形状/图案以助色觉障碍。
- button/select 无自定义 focus 环，仅 input 有（`:44`），键盘焦点可见性偏弱。

## 建议修复顺序

1. **P0**：全屏抽屉不可见（P0-1）、冻结定时器吞锁定态（P0-2）—— 直接影响核心「全屏看图 + 点选锁定」体验。
2. **P1**：筛选去抖/显式触发、复位按钮、loading 与错误条、工具栏分组、图例合一、状态灯回弹。
3. **P2**：导出、搜索补全、标签分级、可访问性、死参数与转义清理。

## 落地状态（本轮已改 `frontend/admin.html`）

| 项 | 状态 |
| --- | --- |
| P0-1 全屏抽屉 | 已：drawer 移入 `#view-graph .graph-stage`，全屏 z-index 提高 |
| P0-2 冻结吞 pin | 已：**移除** force 冻结定时器；pin 时力导向不切 `layout:none`/`fixed` |
| P1 筛选连发 | 已：type/depth/region 空词浏览 **350ms 去抖**；有搜索词换地区不丢词，提示点查询 |
| P1 复位视图 | 已：顶栏「复位视图」 |
| P1 loading / 错误条 | 已：图区遮罩 + 页内 errBar（替代 alert） |
| P1 状态灯回弹 | 已：explore 成功 `setApi(true)` |
| P1 图例与 REL_ZH | 已：补「专业覆盖 / 通向认证」，graph/table 文案对齐 |
| P1-3 自动改岗位提示 | 已：并入最终 info 文案前缀 |
| P2 esc / skillLabel / 深度文案 / tab 收抽屉 / aria-label | 已做小清理 |
| P2 导出 / 搜索补全 / 标签分级 | **未做**（下一轮） |

## 六、图库选型：要不要换掉手写 ECharts

**背景**：admin 现用 ECharts `graph` 系列，手写了 `forceSeedPositions`/`layeredPositions`/`gridPositions` 布局、力导向冻结定时器、`applyPinVisual` 邻接高亮、`readNodePositions` 读坐标——这些正是「专门图库」内置的能力，也是上一轮 P0-2 等 bug 的来源。所以「不造轮子」的正解是换成专门的图可视化库。

| 库 | 定位 | 国内 | 国际 | 许可 | 专门图库? | 对本项目 |
| --- | --- | --- | --- | --- | --- | --- |
| **AntV G6** | 图可视化引擎（蚂蚁） | 事实标准，中文文档最好 | 中等、上升 | MIT | 是：布局(force/dagre分层/grid/circular，5.0 布局用 Rust 重写)+交互(邻接高亮/下钻展开/fisheye)+明暗主题+tooltip/legend/toolbar 插件全内置 | **首选**：能直接删掉手写布局/冻结/高亮；纯 JS 可用，不强制 React；当前 5.1.1，npm 472+ 项目依赖 |
| **Cytoscape.js** | 图/网络分析库 | 中等 | 事实标准（生信/学术/网络分析） | MIT | 是：布局插件(fcose/cola/dagre)+图算法(最短路/中心性)最全 | **次选**：MIT 无顾虑、算法强、适合路径分析；中文资料偏少 |
| **Neo4j NVL** | Neo4j 官方可视化（Bloom 同款引擎、GPU 加速） | 少 | Neo4j 生态内中等 | **专有**（SEE LICENSE IN LICENSE.txt；要求「应用连接 Neo4j」才可用，本项目满足，但商用前须读许可） | 是 | 后端正好是 Neo4j，最贴合；顾虑在专有许可 |
| Sigma.js + graphology | WebGL 大图渲染 | 少 | 较大 | MIT | 渲染器（交互/算法需自拼） | **不推荐**：为 10 万节点级设计；本项目前端按子图限流到几百节点，用不上还更费事 |
| ECharts `graph`（现状） | 通用图表库 | 大 | 大 | Apache-2.0 | 否（graph 只是一个系列） | 保留=继续手写图专用交互（现状痛点） |

**推荐**：
- 要最省事 + 中文生态 + 直接减负 → **AntV G6**（对本项目减负最大，正好覆盖 P0-2）。
- 要 MIT 无顾虑 + 国际最主流 + 图算法强 → **Cytoscape.js**。
- 「国内外都大」是硬指标 → 没有单库在两边都第一：G6（国内强/国际涨）与 Cytoscape（国际强/国内可用）二选一；ECharts 两边都大但**非专用**。

**迁移代价**：admin 是单文件，换库≈重写渲染层（约该文件 60%），但数据层（`explore`/`exploreSeed` 拉 `/v1/graph/explore` 与 seed JSON）可原样保留。换 G6/Cytoscape 后可删除 `forceSeed*`/`layered*`/`grid*`/冻结定时器/`applyPinVisual` 等最易出 bug 的手写逻辑。注：`docs/图谱前端方案.md` §4.1 已早有 G6/ECharts/NVL 的初步选型讨论，本节为落地版结论。

**参考**：
- AntV G6 <https://g6.antv.antgroup.com/> · npm <https://www.npmjs.com/package/@antv/g6>
- Cytoscape.js <https://js.cytoscape.org/>
- Neo4j NVL <https://neo4j.com/docs/nvl/current/> · <https://www.npmjs.com/package/@neo4j-nvl/base>
- Sigma.js <https://www.sigmajs.org/>
