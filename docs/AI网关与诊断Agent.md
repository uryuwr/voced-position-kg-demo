# AI 网关与学员诊断 Agent

> 对齐 **bcs-ai-agent** 的 **AI 网关**（OpenAI 兼容），不是 UC 网关 / 业务网关。

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `LLM_BASE_URL` | AI 网关地址；可无尾 `/v1`，启动时会补全（SDK 会再拼 `/chat/completions`） |
| `GITHUB_TOKEN` | 网关 Authorization 凭证（与 bcs 一致） |
| `LLM_API_KEY` | 可选，等价于 GITHUB_TOKEN |
| `LLM_MODEL` | 模型名 |
| `LLM_TEMPERATURE` | 默认 0.2 |
| `LLM_MAX_OUTPUT_TOKENS` | 默认 8192 |
| `LLM_REQUEST_TIMEOUT` | 秒，默认 120 |

凭证未配齐时：`llm_enabled=false`，诊断走**规则关键词**，服务仍可启动。

示例见仓库根 `.env.example`。

## 代码位置

| 路径 | 作用 |
| --- | --- |
| `backend/settings.py` | 读 AI 网关 env，规范化 base_url |
| `backend/agent/llm.py` | `ChatOpenAI` → 网关 |
| `backend/agent/tools_kg.py` | 搜节点 / 岗位技能构成 / 节点档案 |
| `backend/agent/diagnose.py` | **`langgraph.prebuilt.create_react_agent`** |
| `backend/kg/pg_store/biz_store.py` | 简历/对话诊断调用 agent，失败降级 |

## 策略

1. **需要工具的 LLM 节点**：优先 `create_react_agent`（多轮 tool loop）。  
2. **逻辑过重**（强阶段、HITL、固定 finalize JSON）再拆自定义 `StateGraph`。  
3. 当前诊断产物：技能列表 JSON；`recursion_limit=12` 防死循环。

## 健康检查

`GET /health` 含：

```json
"ai_gateway": {
  "enabled": true,
  "base_url": "https://…/v1",
  "model": "…",
  "has_token": true
}
```

## 依赖

```bash
pip install -r backend/requirements.txt
# 含 langchain-openai langgraph langchain-core openai
```
