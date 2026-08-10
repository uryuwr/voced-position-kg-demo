# frontend · 本地自测页（非正式产品前端）

需后端 `SERVE_DEV_UI=1` 后由 API 托管。

| 路径 | 页面 | 说明 |
| --- | --- | --- |
| `/admin` · `/admin-console` | **管理端控制台** | 看板 / 四维列表分页 / 手工建数 / 审核（对齐 backend.html 能力） |
| `/admin-cytoscape` | 图探索 | Cytoscape 邻域探索 |
| `/dev` | 规划工作台壳 | 旧学员壳 |
| `/cn-sources` | 知识来源目录 | 静态说明 |

## 临时身份请求头

所有 `/v1/**` 调用须带：

- `user-id`
- `user-name`

公共封装：`js/api-client.js`（`VocedApi.apiFetch`）。管理控制台顶栏可改操作人并写入 `localStorage`。

## 启动

```bash
$env:SERVE_DEV_UI="1"
$env:DATABASE_URL="postgresql://voced:<your-password>@localhost:5432/voced_kg"
python -m uvicorn backend.api.main:app --host 0.0.0.0 --port 8088
```

打开 http://127.0.0.1:8088/admin
