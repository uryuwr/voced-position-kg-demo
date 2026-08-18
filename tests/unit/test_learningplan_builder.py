"""学习计划 payload 构造（`backend/learningplan/`）。

这些用例锁的是**推送前就必须成立**的性质。对方的 422 报错以 `loc` 路径呈现
（`body.phases.0.tasks.3.resources.1.url`），要反推是哪个技能哪门课，排查很贵；
所以契约约束在本地全部复刻一遍，错误落在这里。

契约以 2026-08-18 对预生产实测反推的为准（有效签名 + 故意非法值 → 读 422 detail），
不以对接文档为准 —— 两者有出入，最典型的是 `resources` 用 `title`/`url`
而文档写的是 `name`/`source_url`。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.learningplan.builder import (
    MAX_SKILLS_IN_PATH,
    _gap_keys,
    _met_keys,
    _phase_weights,
    build_payload,
    external_path_id_for,
)
from backend.learningplan.schema import ImportPayload, Phase, Resource, Task, sanitize_id


def skill(key, *, level=3, weight=0.2, category=None, sid=None):
    return {
        "id": sid or f"bundle:CN:{key}", "skill_key": key, "skill_name": key,
        "name": key, "required_level": level, "weight": weight, "category": category,
    }


def report(items):
    """items: [(skill_key, ok, measured_level, scorable)]"""
    return {
        "items": [
            {"skill_key": k, "ok": ok, "measured_level": m, "scorable": sc}
            for k, ok, m, sc in items
        ]
    }


def build(skills, rep=None, **kw):
    kw.setdefault("session_id", 77)
    kw.setdefault("region", "CN")
    kw.setdefault("occupation_id", "CN:occupation:X")
    kw.setdefault("occupation_name", "测试岗位")
    return build_payload(skills=skills, report=rep or {}, **kw)


class Test幂等键:
    def test_一次诊断一条路径(self):
        assert external_path_id_for("CN", 77) == "vockg-CN-s77"

    def test_换会话就换_id(self):
        """契约要求「重新测评必须换新 ID」；session_id 自增天然满足。"""
        assert external_path_id_for("CN", 77) != external_path_id_for("CN", 78)

    def test_同输入产出同_payload(self):
        """幂等的前提：纯函数。输出变了，超时重推就会撞 409。"""
        sk = [skill("A", weight=0.6), skill("B", weight=0.4)]
        a = build(sk).wire_dict()
        b = build(sk).wire_dict()
        assert a == b


class Test权重归一:
    def test_和精确等于_100(self):
        assert sum(_phase_weights([0.3, 0.3, 0.4])) == 100.0

    @pytest.mark.parametrize("sums", [[1.0], [0.1, 0.9], [1, 1, 1], [0.7, 0.2, 0.05, 0.05]])
    def test_各种分布都落在契约区间(self, sums):
        s = sum(_phase_weights(sums))
        assert 99.0 <= s <= 101.0, f"{sums} → Σ{s}"

    def test_权重全为零时均分(self):
        """脏数据：整列 weight 都是 0。均分而不是推出 0 被 422。"""
        w = _phase_weights([0.0, 0.0, 0.0])
        assert sum(w) == 100.0
        assert all(x > 0 for x in w)

    def test_单个阶段权重为零时被抬到大于零(self):
        """契约 phase_weight > 0。某阶段技能权重全 0、别的不为 0 时，
        归一会给它 0.0 —— 必须抬起来，否则整条路径被拒。"""
        w = _phase_weights([1.0, 0.0, 1.0])
        assert all(x > 0 for x in w), w
        assert 99.0 <= sum(w) <= 101.0

    def test_单阶段直接给满(self):
        assert _phase_weights([0.0]) == [100.0]


class Test达标判定:
    def test_达标项来自_items_而不是_gaps(self):
        """`gaps` 只装未达标项，从里面找 ok=True 永远是空集。"""
        rep = report([("A", True, 3, True), ("B", False, 1, True)])
        assert _met_keys(rep) == {"A"}
        assert _gap_keys(rep) == {"B"}

    def test_历史报告里_scorable_为_None_仍算达标(self):
        """`scorable` 是无基准改造时才加的字段，老报告是 None。
        按真值判会把所有老报告的达标项误判成未达标。"""
        assert _met_keys(report([("A", True, 3, None)])) == {"A"}

    def test_明确不可评分的不算达标(self):
        """scorable=False 是「岗位没配要求档」，不是「学员会了」。"""
        assert _met_keys(report([("A", True, 3, False)])) == set()

    def test_没测过的不算达标(self):
        """未测技能的 ok 也可能为真（要求档为 0），标成已完成 = 假进度。"""
        assert _met_keys(report([("A", True, None, True)])) == set()

    def test_老报告没有_items_时退回子集并集(self):
        rep = {"gaps": [{"skill_key": "B", "ok": False, "measured_level": 1}],
               "strengths": [{"skill_key": "A", "ok": True, "measured_level": 3}]}
        assert _met_keys(rep) == {"A"}
        assert _gap_keys(rep) == {"B"}


class Test编排:
    def test_短板优先于高权重(self):
        """移植时修掉的既有 bug：原实现按 gaps[].skill_name 判短板，
        而 gaps 项只有 skill_key，于是「短板优先」一直没生效。"""
        sk = [skill("强项", weight=0.9), skill("短板", weight=0.1)]
        rep = report([("强项", True, 5, True), ("短板", False, 1, True)])
        p = build(sk, rep)
        names = [t.name for ph in p.phases for t in ph.tasks]
        assert "短板" in names[0], names

    def test_最多取八个技能(self):
        p = build([skill(f"S{i}", weight=0.1) for i in range(20)])
        assert sum(len(ph.tasks) for ph in p.phases) == MAX_SKILLS_IN_PATH

    def test_按大类分阶段且空阶段被剔除(self):
        sk = [skill("a", category="作业准备"), skill("b", category="操作与加工")]
        p = build(sk)
        assert [ph.phase_name for ph in p.phases] == ["作业准备", "操作与加工"]
        assert all(ph.tasks for ph in p.phases)   # 空阶段会被对方 422

    def test_任务_id_在全路径内唯一(self):
        p = build([skill(f"S{i}", category=f"C{i % 3}") for i in range(8)])
        ids = [t.external_task_id for ph in p.phases for t in ph.tasks]
        assert len(set(ids)) == len(ids)

    def test_档位名取自_skill_level_meta(self):
        """CLAUDE.md：档位名只能从 skill_level_meta 读，禁止硬编码。"""
        p = build([skill("A", level=5)])
        assert "专家" in p.phases[0].tasks[0].name

    def test_没有技能时明确报错(self):
        with pytest.raises(ValueError, match="技能构成"):
            build([])


class Test资源:
    def test_只收绝对_http_链接(self):
        sk = [skill("A")]
        cbk = {"A": [
            {"id": "c1", "name": "好课", "source_url": "https://x.com/c1"},
            {"id": "c2", "name": "相对路径", "source_url": "/course/2"},
            {"id": "c3", "name": "空链接", "source_url": ""},
        ]}
        p = build(sk, courses_by_key=cbk)
        res = p.phases[0].tasks[0].resources
        assert [r.title for r in res] == ["好课"]

    def test_字段名是_title_和_url(self):
        """实测契约：`name`/`source_url` 会被对方按 extra=forbid 拒掉。"""
        d = Resource(title="课", url="https://x").model_dump()
        assert set(d) == {"title", "url"}

    def test_非_http_链接被拒(self):
        with pytest.raises(ValidationError, match="绝对 http"):
            Resource(title="课", url="ftp://x/y")


class Test契约校验:
    def test_id_字符集(self):
        """技能名带中文和引号（`“互联网+”增材制造`），直接当 id 会被 422。"""
        assert sanitize_id("“互联网+”增材制造", fallback="fb") == "fb"
        assert sanitize_id("vockg-CN-s77", fallback="fb") == "vockg-CN-s77"

    def test_权重和越界被拦在本地(self):
        ph = lambda i, w: Phase(
            external_phase_id=f"stage-{i}", phase_name=f"P{i}", phase_weight=w,
            tasks=[Task(external_task_id=f"t{i}", name="N", estimated_minutes=10)],
        )
        with pytest.raises(ValidationError, match="权重之和"):
            ImportPayload(external_path_id="p1", external_job_id="J", job_name="岗",
                          title="T", phases=[ph(1, 30.0), ph(2, 30.0)])

    def test_任务_id_重复被拦在本地(self):
        t = lambda: Task(external_task_id="dup", name="N", estimated_minutes=1)
        with pytest.raises(ValidationError, match="重复"):
            ImportPayload(
                external_path_id="p1", external_job_id="J", job_name="岗", title="T",
                phases=[Phase(external_phase_id="stage-1", phase_name="P",
                              phase_weight=100.0, tasks=[t(), t()])],
            )

    def test_不发_task_weight(self):
        """契约要 task_weight > 0，而课程任务本就不该计权。整条路径都不传。"""
        p = build([skill("A")])
        assert "task_weight" not in p.wire_dict()["phases"][0]["tasks"][0]

    def test_可选字段不发_null(self):
        d = build([skill("A")]).wire_dict()
        assert "revision_of_external_path_id" not in d
        assert all("description" not in t
                   for ph in d["phases"] for t in ph["tasks"])
