"""E2E 专用启动器：在**进程内存里**开鉴权旁路，起一个独立端口的实例。

为什么需要它：backend/settings.py 用 `load_dotenv(backend/.env, override=True)`，
会覆盖命令行传进来的环境变量，所以 `AUTH_DEBUG=1 API_PORT=18099 python -m backend.api.main`
既不换端口也不开旁路。这里在 dotenv 加载完成后再改内存值。

**只影响这个测试子进程**：不写任何文件，不触碰 .env，8088 上的正常服务与生产配置不受影响。

用法：python -X utf8 tests/_e2e_server.py [port] [host]

⚠️ **host 默认且应当保持 `127.0.0.1`**。这个实例把鉴权旁路打开了——带一个
`X-Test-Uid` 头就能以任意用户身份访问，不需要登录。绑到 `0.0.0.0` 等于把这个
后门开放给整个局域网，学员画像、诊断报告、五维记忆（都是 PII）谁都能读。
要对外提供服务请用正常入口（`python -m uvicorn backend.api.main:app --host 0.0.0.0`），
那条路径的旁路是关的。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18099
HOST = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"

import backend.settings as settings  # noqa: E402  触发 dotenv 加载

# 让 GET /v1/config 回 auth_bypass=true，前端便跳过 UC 登录跳转
settings.AUTH_BYPASS = True
settings.AUTH_DEBUG = True

import backend.api.auth as auth  # noqa: E402

auth.AUTH_BYPASS = True
auth.AUTH_DEBUG = True

from backend.api.main import app  # noqa: E402

if __name__ == "__main__":
    import uvicorn

    if HOST not in ("127.0.0.1", "localhost"):
        print(
            f"⚠ 旁路实例绑到 {HOST}：任何人带 X-Test-Uid 即可冒充任意用户。"
            "仅限可信网段，且不要用于前端对接。",
            file=sys.stderr, flush=True,
        )
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
