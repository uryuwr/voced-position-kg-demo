"""基准库**只读**查看端口（默认 8090）。

用途：随时打开页面看基准数据长什么样，而不必担心手一抖改坏它。

只读是**数据库层面**保证的，不是靠约定
--------------------------------------
连的是 PG 只读角色（`voced_kg_ro`，只有 SELECT 权限）。任何写操作会被 PG 直接
拒绝，即便应用层有 bug、即便有人误点了管理台的保存按钮。

这个项目里「靠自觉」被证明过不管用：E2E 的 C2 用例本身就是写入测试（验证非法
`attrs.level` 被拒 400），它必须写库；测试 fixture 也就这么混进了基准数据，
还挂到了「混凝土工」的技能构成上。所以只读要落到权限上。

角色建好一次就行（用管理员执行）：

    CREATE ROLE voced_kg_ro LOGIN PASSWORD '<pw>';
    GRANT CONNECT ON DATABASE voced_kg TO voced_kg_ro;
    GRANT USAGE ON SCHEMA public TO voced_kg_ro;
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO voced_kg_ro;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO voced_kg_ro;

然后把连接串写进 `backend/.env` 的 `BASELINE_RO_DATABASE_URL`。
没配的话本脚本**拒绝启动** —— 不会悄悄退回可写连接串，那就白挂个「只读」的名。

为什么不复用 8088
----------------
8088 连的是可写连接串，且是联调用的实例。要的是「一个确定不会改数据的入口」，
换端口 + 换角色才有意义，只换端口等于没换。

用法::

    python -X utf8 scripts/serve_baseline_readonly.py            # 8090
    python -X utf8 scripts/serve_baseline_readonly.py 8091
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
# 绑 127.0.0.1：只读也不该往局域网暴露，库里有学员画像与诊断报告（PII）
HOST = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"

import backend.settings as settings  # noqa: E402  触发 dotenv 加载

RO_URL = (os.getenv("BASELINE_RO_DATABASE_URL") or "").strip()
if not RO_URL:
    print(
        "缺 BASELINE_RO_DATABASE_URL。\n"
        "这个脚本的全部意义是「连只读角色」，没有它就只能退回可写连接串 ——\n"
        "那样挂个「只读」的名字反而更危险。请先按本文件顶部的 SQL 建 voced_kg_ro，\n"
        "再把连接串写进 backend/.env。",
        file=sys.stderr,
    )
    raise SystemExit(2)

# 在 dotenv 加载**之后**改内存值：settings.py 用 override=True 载入 .env，
# 命令行传 DATABASE_URL 会被它盖掉（CLAUDE.md 的「配置」一节）
settings.DATABASE_URL = RO_URL
os.environ["DATABASE_URL"] = RO_URL

# 自测页要能打开，否则只有 /docs 可看
settings.SERVE_DEV_UI = True
os.environ["SERVE_DEV_UI"] = "1"

import backend.kg.pg_store.client as pg_client  # noqa: E402

pg_client.DATABASE_URL = RO_URL


def _skip_ddl(*a, **kw):  # noqa: ANN002, ANN003
    return None


# 启动时的幂等 DDL 在只读角色下必然失败（`permission denied for schema public`），
# 而那是**预期**的，不是故障 —— 基准库的结构由可写实例（8088）维护，这里只看。
#
# 三个都要拦，而且必须在 import backend.api.main **之前**：main 用的是
# from-import，模块加载时就把函数绑进自己的命名空间了，之后再替换源模块没用。
import backend.kg.pg_store.biz_store as _biz  # noqa: E402
import backend.kg.pg_store.review as _review  # noqa: E402

pg_client.ensure_schema = _skip_ddl
_biz.ensure_biz_schema = _skip_ddl
_review.ensure_review_schema = _skip_ddl

from backend.api.main import app  # noqa: E402


def _guard_writes() -> None:
    """把写方法从路由表上摘掉，省得页面上点了保存才收到 PG 的报错。

    真正的防线是 PG 权限；这一层只是让「点了没反应」变成「明确的 405」。
    """
    keep = []
    dropped = 0
    for r in app.router.routes:
        methods = getattr(r, "methods", None) or set()
        if methods & {"POST", "PUT", "PATCH", "DELETE"}:
            dropped += 1
            continue
        keep.append(r)
    app.router.routes = keep
    print("已摘除 %d 个写路由（只读实例）" % dropped)


if __name__ == "__main__":
    import uvicorn

    _guard_writes()
    masked = RO_URL
    if "@" in masked:
        masked = masked.split("://")[0] + "://***@" + masked.split("@", 1)[1]
    print("基准库只读实例：http://%s:%d   库=%s" % (HOST, PORT, masked))
    print("  页面 /student /admin-console   契约 /docs")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
