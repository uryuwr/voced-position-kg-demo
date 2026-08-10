# admin-cytoscape E2E · 2026-08-05

**结果：通过**

## 检查项
- [x] default_table_tab: 
- [x] graph_search_filled: q='软件技术'
- [x] graph_info_has_counts: 进入 Graph · 维度 专业+岗位+技能+课程+认证 · 节点 90 · 边 114 · 该数据无「认证」 · 布局：力导向 cose（几何聚类，非语义距离） · 国内：教育部专业目录（MOE_CN）
- [x] graph_has_nodes: nodes=90
- [x] graph_has_edges: edges=114
- [x] graph_not_huge_catalog: nodes=90 (circle dump would be 300-500)
- [x] search_nursing_nodes: 维度 专业+岗位+技能+课程+认证 · 节点 100 · 边 105 · 该数据无「技能/认证」 · 布局：力导向 cose（几何聚类，非语义距离） · 国内：教育部专业目录（MOE_CN）
- [x] catalog_switches_table: 
- [x] table_has_majors: ▶
本 · 半导体工艺与装备 · 080723TK
普通本科
080723TK
0807 电子信息类
普通高等学校本科专业目录（202
MOE_CN
official
CN
专业
▶
本 · 包装工程 · 081702
普通本科
08170
- [x] table_jump_graph: title='本 · 半导体工艺与装备 · 080723TK' info=已锁定：「半导体工艺与装备」高亮相邻 2 层 · 再点同一节点或空白处恢复

## 截图
- `reports/e2e_shots/`

```json
{
  "ok": true,
  "checks": [
    {
      "name": "default_table_tab",
      "pass": true,
      "detail": ""
    },
    {
      "name": "graph_search_filled",
      "pass": true,
      "detail": "q='软件技术'"
    },
    {
      "name": "graph_info_has_counts",
      "pass": true,
      "detail": "进入 Graph · 维度 专业+岗位+技能+课程+认证 · 节点 90 · 边 114 · 该数据无「认证」 · 布局：力导向 cose（几何聚类，非语义距离） · 国内：教育部专业目录（MOE_CN）"
    },
    {
      "name": "graph_has_nodes",
      "pass": true,
      "detail": "nodes=90"
    },
    {
      "name": "graph_has_edges",
      "pass": true,
      "detail": "edges=114"
    },
    {
      "name": "graph_not_huge_catalog",
      "pass": true,
      "detail": "nodes=90 (circle dump would be 300-500)"
    },
    {
      "name": "search_nursing_nodes",
      "pass": true,
      "detail": "维度 专业+岗位+技能+课程+认证 · 节点 100 · 边 105 · 该数据无「技能/认证」 · 布局：力导向 cose（几何聚类，非语义距离） · 国内：教育部专业目录（MOE_CN）"
    },
    {
      "name": "catalog_switches_table",
      "pass": true,
      "detail": ""
    },
    {
      "name": "table_has_majors",
      "pass": true,
      "detail": "▶\n本 · 半导体工艺与装备 · 080723TK\n普通本科\n080723TK\n0807 电子信息类\n普通高等学校本科专业目录（202\nMOE_CN\nofficial\nCN\n专业\n▶\n本 · 包装工程 · 081702\n普通本科\n08170"
    },
    {
      "name": "table_jump_graph",
      "pass": true,
      "detail": "title='本 · 半导体工艺与装备 · 080723TK' info=已锁定：「半导体工艺与装备」高亮相邻 2 层 · 再点同一节点或空白处恢复"
    }
  ],
  "base": "http://127.0.0.1:8088",
  "graph_stats": {
    "nodes": 90,
    "edges": 114,
    "labeled": 90,
    "info": "进入 Graph · 维度 专业+岗位+技能+课程+认证 · 节点 90 · 边 114 · 该数据无「认证」 · 布局：力导向 cose（几何聚类，非语义距离） · 国内：教育部专业目录（MOE_CN）",
    "q": "软件技术"
  }
}
```