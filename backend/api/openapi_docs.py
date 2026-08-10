"""
OpenAPI / Swagger / ReDoc 元数据。

访问:
  /docs  /redoc  /openapi.json  /api-guide
"""
from __future__ import annotations

import os
import socket
from typing import Any

from backend.api.auth_temp import SECURITY_REQUIREMENT, SECURITY_SCHEMES, USER_HEADER_NOTE

API_TITLE = "职业教育知识图谱 API"
API_VERSION = "0.6.3"

API_DESCRIPTION = f"""
## 对接说明

- **图存储**：PostgreSQL；默认区域 **CN**。
- **契约**：[Swagger `/docs`](/docs) · [ReDoc](/redoc) · [OpenAPI JSON](/openapi.json)
- **鉴权**：UC **MAC Token**（对齐 bcs-ai-agent）
- **学员产品**：Open-Q `frontend.html` → `/v1/student/**`（**仅已发布**数据）
- **管理产品**：`/admin` 控制台 + `/v1/admin/**`

## 鉴权

{USER_HEADER_NOTE}

| Header | 必填 | 说明 |
| --- | --- | --- |
| `Authorization` | 是（生产） | `MAC id="...",nonce="...",mac="..."` |
| `X-User-Name` | 否 | 展示名，中文 encodeURIComponent |
| `X-Test-Uid` / `X-Test-Uname` | 开发 | `AUTH_BYPASS=1` 时旁路 |

前端配置：`GET /v1/config`（无需登录）。

## 审核与直写

- 配置 **`REVIEW_REQUIRED`**（`.env`，默认 **0**）：
  - **0**：`POST /v1/admin/changes` **立即写主库**（`status=applied`），不进待审——本期默认
  - **1**：进待审队列；**通过**写库删记录；**驳回**删记录
- 前端可读 `GET /v1/config` → `review_required`
- **图检索 / 图谱 / 学员端**：只 **published**（BR-07）
- **发布门禁 BR-02~08**：enable / `status=published` 服务端硬拦截；预检 `POST /v1/admin/publish/validate`
- **管理控制台四维列表**：`scope=manage` 可看全状态

### 新建节点顺带关联（客户端只传 id 列表）

| 新建类型 | payload 字段 | 服务端自动边 |
| --- | --- | --- |
| 专业 `major` | `industry_ids: string[]` | major →belongs_to→ industry |
| 岗位 `occupation` | `major_ids: string[]` | major →prepares_for→ occupation |
| 技能 `skill_level` | `occupation_ids: string[]` | occupation →requires→ skill |

支持多选；边字段由系统初始化，无需客户端拼 edge 结构。

## 模块

| 模块 | 说明 |
| --- | --- |
| 系统 | health / config / me |
| 图检索 | 通用 KG 查询（保留） |
| 管理端 · 数据列表 | 四维分页 |
| 管理端 · 数据维护 | 直接写 API（联调）；产品流请走审核） |
| 管理端 · 审核 | `/v1/admin/changes` |
| 管理端 · 运营看板 | dashboard |
| 学生端 · * | 探索/诊断/学习/我的 |
"""

OPENAPI_TAGS = [
    {"name": "系统", "description": "健康检查、UC 配置、当前用户（config 无需登录）"},
    {
        "name": "图检索",
        "description": "通用 KG 检索（保留，与业务审核流独立）",
    },
    {
        "name": "管理端 · 数据列表",
        "description": "四维节点分页 + 边列表（`GET /v1/kg/edges`，可按 node_id 核对删边）",
    },
    {
        "name": "管理端 · 数据维护",
        "description": "底层写接口；产品操作请优先 /v1/admin/changes",
    },
    {
        "name": "管理端 · 审核",
        "description": "待审变更：提交/通过生效/驳回删除",
    },
    {
        "name": "管理端 · 运营看板",
        "description": "运营摘要",
    },
    {"name": "学生端 · 探索", "description": "仅已发布数据"},
    {"name": "学生端 · 诊断", "description": "简历/对话/报告"},
    {"name": "学生端 · 学习", "description": "路径与资源"},
    {"name": "学生端 · 我的", "description": "画像与徽章"},
]


def _lan_ipv4s() -> list[str]:
    ips: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127.") and ip not in ips:
            ips.insert(0, ip)
    except Exception:
        pass
    preferred = [i for i in ips if i.startswith("192.168.") or i.startswith("10.")]
    others = [i for i in ips if i not in preferred]
    return preferred + others


def openapi_servers(port: int = 8088) -> list[dict[str, str]]:
    servers = [
        {"url": f"http://127.0.0.1:{port}", "description": "本机 loopback"},
        {"url": f"http://localhost:{port}", "description": "本机 localhost"},
    ]
    for ip in _lan_ipv4s()[:5]:
        servers.append({"url": f"http://{ip}:{port}", "description": f"局域网 {ip}"})
    return servers


def apply_security_to_openapi(schema: dict[str, Any]) -> dict[str, Any]:
    components = schema.setdefault("components", {})
    schemes = components.setdefault("securitySchemes", {})
    schemes.update(SECURITY_SCHEMES)
    for path, methods in (schema.get("paths") or {}).items():
        if path in ("/", "/health", "/api-guide", "/openapi.json"):
            continue
        if path.startswith("/docs") or path.startswith("/redoc"):
            continue
        for method, op in list(methods.items()):
            if method.startswith("x-") or not isinstance(op, dict):
                continue
            tags = op.get("tags") or []
            if "系统" in tags:
                continue
            op["security"] = SECURITY_REQUIREMENT
    schema["servers"] = openapi_servers(int(os.getenv("API_PORT", "8088")))
    return schema


API_GUIDE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>API 对接指南</title>
  <style>
    body { font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      max-width: 800px; margin: 32px auto; padding: 0 16px; color: #0f172a; line-height: 1.55; }
    h1 { font-size: 1.35rem; } h2 { font-size: 1.05rem; margin-top: 1.5em; }
    code { background: #f1f5f9; padding: 0.1em 0.35em; border-radius: 4px; font-size: 0.92em; }
    table { border-collapse: collapse; width: 100%; font-size: 13px; }
    th, td { border: 1px solid #e2e8f0; padding: 8px 10px; text-align: left; vertical-align: top; }
    th { background: #f8fafc; }
    .muted { color: #64748b; font-size: 13px; }
    .warn { background: #fff7ed; border: 1px solid #fed7aa; padding: 12px; border-radius: 8px; }
  </style>
</head>
<body>
  <h1>职业教育知识图谱 · API 对接指南 v0.5</h1>
  <p class="muted">以 <a href="/docs">/docs</a> 为准。学员端对齐 Open-Q <code>frontend.html</code>。</p>

  <div class="warn">
    <strong>临时身份</strong>：业务请求头必填 <code>user-id</code>、<code>user-name</code>（未接 UC）。
  </div>

  <h2>学生端（优先对接）</h2>
  <table>
    <tr><th>原型模块</th><th>接口前缀 / 示例</th></tr>
    <tr><td>探索</td><td><code>/v1/student/professions</code> · <code>/positions</code> · <code>/goal</code> · <code>/industries</code></td></tr>
    <tr><td>AI 诊断</td><td><code>POST /v1/student/diagnosis/resume</code> · chat sessions · <code>GET .../report</code></td></tr>
    <tr><td>学习中心</td><td><code>/v1/student/learn/path</code> · generate · steps complete · resources</td></tr>
    <tr><td>我的</td><td><code>GET /v1/student/me</code> · badges · skills</td></tr>
  </table>

  <h2>管理端</h2>
  <p>自测页（需 <code>SERVE_DEV_UI=1</code>）：<a href="/admin">/admin</a> 控制台 ·
    <a href="/admin-cytoscape">/admin-cytoscape</a> 图探索</p>
  <table>
    <tr><th>能力</th><th>接口</th></tr>
    <tr><td>四维 Table</td><td><code>GET /v1/kg/nodes?type=major&amp;page=1</code></td></tr>
    <tr><td>建数 / 审核</td><td><code>/v1/kg/nodes|edges</code> · <code>/v1/review/proposals</code></td></tr>
    <tr><td>看板</td><td><code>GET /v1/admin/dashboard/summary</code></td></tr>
  </table>

  <h2>图检索（独立模块，保留）</h2>
  <p><code>/v1/search</code>（种子）· <code>POST /v1/graph/expand</code>（1 跳邻居）· <code>/v1/graph/explore?depth=0</code>（默认仅种子）· <code>/v1/nodes</code> · 关系列表。常规探索：search → expand，勿默认 depth=2 全邻域。</p>

  <p class="muted"><a href="/docs">打开 Swagger</a></p>
</body>
</html>
"""
