"""诊断结果 → 五维记忆（Force 提交）。

    POST /api/v1/user-profile/agent/memory-signals/force

闭环里的第 ② 步：测评产出的能力档位是最硬的结构化证据，同步进画像后，
**其他没测过的岗位**才能拿它当推断依据（同一岗位直接用报告分，用不上记忆）。

为什么走 force
--------------
普通 `memory-signals` 可能返回 `needs_confirmation`，要用户再确认一遍。
而这是学员刚亲手做完的客观测评，不该再弹一次「你确认这条记忆吗」。
force 保证结果不含 needs_confirmation（但不保证一定写入——仍受
policy_blocked / memory_disabled / 证据约束）。

text 怎么写
-----------
按文档《text 内容规范》：**纯自然语言、语义自包含、保留具体名称与时间**，
不嵌 JSON、不指定维度/标题/标签——五维归类、拆分事实、生成标签全由平台负责。
所以这里只把档位翻译成人话，不做任何结构化。

异步与幂等
----------
在后台线程提交，失败不影响报告返回；`Idempotency-Key` 由 session_id 派生，
同一次诊断重复触发不会写重复记忆。
"""
from __future__ import annotations

import threading
import uuid
from typing import Any

from backend import settings

FORCE_PATH = "/api/v1/user-profile/agent/memory-signals/force"
SIGNAL_PATH = "/api/v1/user-profile/agent/memory-signals"

# 与 skill_level_meta 一致的档位语义，写进文本让平台能理解"5 级"是什么
_LEVEL_WORD = {1: "了解", 2: "掌握", 3: "熟练", 4: "精通", 5: "专家"}

# 同一会话固定的命名空间，保证 Idempotency-Key 可复现
_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def available() -> bool:
    from backend.bts import bts_client

    return bool(settings.OPENQ_AI_MANAGER) and bts_client().available()


def build_text(report: dict[str, Any], *, occupation_name: str, when: str | None = None) -> str:
    """诊断报告 → 一段语义自包含的自然语言。

    只写**实测到的**技能（tested），未覆盖的不写——没测过就不是证据，
    写进去等于让平台把「没测」记成「不会」。
    """
    items = [i for i in (report.get("items") or []) if i.get("tested") and i.get("measured_level")]
    if not items:
        return ""
    items.sort(key=lambda x: -(x.get("measured_level") or 0))

    parts = [
        f"用户完成了「{occupation_name}」岗位的 AI 能力测评"
        + (f"（测评时间 {when[:10]}）" if when else "")
        + f"，综合能力匹配度 {report.get('match_score')}%。"
    ]
    lv_desc = "、".join(f"{n} 级表示{w}" for n, w in _LEVEL_WORD.items())
    parts.append(f"能力档位按 1 到 5 级划分，{lv_desc}。")
    parts.append(
        "本次实测结果："
        + "；".join(
            f"{i['skill_key']} 达到 {i['measured_level']} 级"
            f"（{_LEVEL_WORD.get(i['measured_level'], '')}），"
            f"该岗位要求 {i.get('required_level') or '未定义'} 级"
            for i in items
        )
        + "。"
    )
    ok = [i["skill_key"] for i in items if i.get("ok")]
    gap = [i["skill_key"] for i in items if not i.get("ok")]
    if ok:
        parts.append(f"其中 {'、'.join(ok)} 已达到该岗位的能力要求。")
    if gap:
        parts.append(f"{'、'.join(gap)} 低于岗位要求，是用户当前的主要能力短板。")
    return "".join(parts)[:4000]


def push_diagnosis(
    uc_user_id: str,
    session_id: int,
    report: dict[str, Any],
    *,
    occupation_name: str,
    when: str | None = None,
    force: bool = True,
    timeout: int | None = None,
) -> dict[str, Any]:
    """同步提交（阻塞）。返回 {ok, submission_id, text, status, error}。"""
    from backend.bts import BtsClient, BtsError

    text = build_text(report, occupation_name=occupation_name, when=when)
    out: dict[str, Any] = {"ok": False, "text": text, "session_id": session_id}
    if not text:
        out["error"] = "本次测评没有实测到的技能，无可提交内容"
        return out
    if not available():
        out["error"] = "用户画像服务未配置（OPENQ_AI_MANAGER / BTS）"
        return out

    client = BtsClient(endpoint=settings.OPENQ_AI_MANAGER, timeout=timeout)
    path = FORCE_PATH if force else SIGNAL_PATH
    # 同一次诊断重复触发要复用同一个 key，避免写重复记忆
    idem = str(uuid.uuid5(_NS, f"assessment:{session_id}"))
    try:
        resp = client.post(
            path,
            json={"referenceId": f"assessment_session_{session_id}", "text": text},
            uc_user_id=str(uc_user_id),
            extra_headers={"Idempotency-Key": idem},
        )
        body = resp if isinstance(resp, dict) else {}
        out.update({
            "ok": True,
            "submission_id": body.get("submissionId") or body.get("submission_id"),
            "status": body.get("status"),
            "response": body,
            "idempotency_key": idem,
            "path": path,
        })
    except BtsError as e:
        out["error"] = f"{e}（status={e.status}）"
        out["response"] = e.body
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:300]
    return out


def push_diagnosis_async(
    uc_user_id: str, session_id: int, report: dict[str, Any], *, occupation_name: str,
    when: str | None = None,
) -> None:
    """后台提交：画像服务慢或不可用都不该拖住报告返回。"""

    def _run() -> None:
        res = push_diagnosis(
            uc_user_id, session_id, report, occupation_name=occupation_name, when=when
        )
        # 结果记进事件表，便于事后排查「为什么这次没同步上」
        try:
            from backend.kg.pg_store.client import connect
            import json as _json

            with connect() as conn:
                conn.execute(
                    "INSERT INTO biz_event (user_id, event_type, payload) VALUES (%s,%s,%s::jsonb)",
                    (
                        str(uc_user_id),
                        "memory_sync",
                        _json.dumps(
                            {k: v for k, v in res.items() if k != "text"} | {"chars": len(res.get("text") or "")},
                            ensure_ascii=False,
                        ),
                    ),
                )
                conn.commit()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(
        target=_run, name=f"memory-sync-{session_id}", daemon=True
    ).start()


def get_submission(uc_user_id: str, submission_id: str) -> dict[str, Any]:
    """轮询处理结果（GET /agent/memory-signals/{submissionId}）。"""
    from backend.bts import BtsClient

    client = BtsClient(endpoint=settings.OPENQ_AI_MANAGER)
    return client.get(f"{SIGNAL_PATH}/{submission_id}", uc_user_id=str(uc_user_id))
