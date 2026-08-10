# 图探索 API：search + expand

> 对齐业界常规（AWS Graph Explorer、Neo4j Browser、create-context-graph）  
> 日期：2026-08-07

## 形态

```text
1) GET  /v1/search?q=…&type=&limit=
   → 仅种子节点列表（不扩邻域）

2) POST /v1/graph/expand
   body: { "node_id": "…", "limit": 25, "direction": "both", "rel_types": null }
   → 该节点 1 跳邻居 + 边（可重复点，前端合并去重）

3) GET  /v1/graph/explore?q=…&depth=0   （默认）
   → 与 search 类似的种子子图响应包装
   depth≥1 仍保留旧 BFS，仅兼容，不推荐作默认探索
```

## 前端（admin-cytoscape）

- **查询**：`explore` 固定 `depth=0`，画布只放匹配种子  
- **双击节点**：`POST /v1/graph/expand`，结果 **merge** 进当前图  
- **Table 下钻**：子节点用 expand  

## 为何改

旧 `depth=2` 一次无向 BFS 会把岗位相关专业下的大量课目标叶子全部拉上画布。  
常规产品是 **search 定种子 → 用户按需 expand**。

## OpenAPI

改 `backend/api/schemas.py` 的 `ExpandRequest` / `GraphMeta` 后，`/docs` 自动更新。
