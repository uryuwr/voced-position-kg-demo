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

技能必须写**展示名**，不能写 `skill_key`
----------------------------------------
2026-08-19 `skill_key` 改成 `SK`+md5 之后，这里漏改了一天：灌进五维记忆的是
「`SKa1fa1d005d` 达到 3 级」。三重危害，且全程 HTTP 200 无异常：

1. 平台把这段文本当**语义证据长期保存**，供别的岗位推断用，而一串哈希对任何
   语义推断都是零信息量 —— 等于这次测评的证据白扔。
2. 读回来的那一端（`agent/assessment/profile.recall_skills`）是按**技能名**在
   记忆原文里做子串召回，写哈希进去永远召不回，闭环两头都不通。
3. `Idempotency-Key` 按 session 派生，重推覆盖不了 —— 脏文本进去就留在那儿了。

所以取名字失败时**宁可少写一项**（`_writable` 丢弃），少写只是证据不全，
写错会让平台记住一条永久的脏事实。丢了几项记进 `biz_event`，不静默。

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


def _display_name(item: dict[str, Any]) -> str:
    """技能展示名；拿不到就返回空串，调用方据此跳过这一项。

    `skill_name` 为空、或它本身就是个生成式 code（上游漏挑字段时会回落成
    `skill_key`，见 `report.py` 的 `it.get("skill_name") or key`）都算拿不到。
    判据与 `skill_display.normalize_stored_report_skills._name_missing` 一致：
    用 `is_generated` 而不是 `is_valid_key` —— 后者对 `Python`/`SQL`/`Excel`
    这类纯 ASCII 技能名也为真，会把正常名字当 code 丢掉。
    """
    from backend.kg.skill_key import is_generated

    nm = str(item.get("skill_name") or "").strip()
    return "" if not nm or is_generated(nm) else nm


def _writable(report: dict[str, Any]) -> tuple[list[tuple[dict[str, Any], str]], int]:
    """→ (`[(项, 展示名)]` 按实测档降序, 因取不到展示名而丢弃的条数)。

    两道过滤：
    - **只留实测到的**（`tested` 且有 `measured_level`）—— 没测过不是证据，
      写进去等于让平台把「没测」记成「不会」。
    - **只留有展示名的** —— 理由见模块说明。
    """
    tested = [i for i in (report.get("items") or []) if i.get("tested") and i.get("measured_level")]
    rows = [(i, _display_name(i)) for i in tested]
    rows = [(i, nm) for i, nm in rows if nm]
    rows.sort(key=lambda x: -(x[0].get("measured_level") or 0))
    return rows, len(tested) - len(rows)


def build_text(report: dict[str, Any], *, occupation_name: str, when: str | None = None) -> str:
    """诊断报告 → 一段语义自包含的自然语言。取不到内容时返回空串。"""
    rows, _ = _writable(report)
    if not rows:
        return ""

    # 匹配度可能算不出来（岗位一项要求档都没配，见 report.build_report）：
    # 那时写「匹配度 None%」会把脏文本灌进五维记忆，宁可不写这一句
    score = report.get("match_score")
    parts = [
        f"用户完成了「{occupation_name}」岗位的 AI 能力测评"
        + (f"（测评时间 {when[:10]}）" if when else "")
        + (f"，综合能力匹配度 {score}%。" if score is not None
           else "（该岗位未配置能力要求档，未计算匹配度）。")
    ]
    lv_desc = "、".join(f"{n} 级表示{w}" for n, w in _LEVEL_WORD.items())
    parts.append(f"能力档位按 1 到 5 级划分，{lv_desc}。")
    parts.append(
        "本次实测结果："
        + "；".join(
            f"{nm} 达到 {i['measured_level']} 级"
            f"（{_LEVEL_WORD.get(i['measured_level'], '')}），"
            f"该岗位要求 {i.get('required_level') or '未定义'} 级"
            for i, nm in rows
        )
        + "。"
    )
    ok = [nm for i, nm in rows if i.get("ok")]
    gap = [nm for i, nm in rows if not i.get("ok")]
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
    _, dropped = _writable(report)
    out: dict[str, Any] = {"ok": False, "text": text, "session_id": session_id}
    # 「本该写 n 项、只写了 m 项」必须可查：丢弃是正确行为，但整批被丢是上游 bug
    if dropped:
        out["dropped_no_name"] = dropped
    if not text:
        out["error"] = (
            f"实测到的 {dropped} 项技能都取不到展示名（skill_name 缺失或仍是 code），"
            "不提交以免把 code 写进五维记忆"
            if dropped
            else "本次测评没有实测到的技能，无可提交内容"
        )
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
