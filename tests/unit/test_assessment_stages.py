"""backend/agent/assessment/stages.py —— 图状态 → 前端三阶段的投影（纯函数）。

前端步骤条写死三节点，图内部有 7 个节点。这层投影是**契约面**：图改结构时前端
不该跟着改，所以 status 取值与 key 都要锁住。`project_from_store` 要连库，不在这测。
"""
from __future__ import annotations

import pytest

from backend.agent.assessment.stages import (
    STAGE_ASSESS,
    STAGE_NAMES,
    STAGE_PARSE,
    STAGE_REPORT,
    pending_question,
    project,
    with_progress,
)

Q1 = {"skill_key": "A", "prompt": "q1"}
Q2 = {"skill_key": "B", "prompt": "q2"}


def stages_of(state, **kw):
    return {s["key"]: s for s in project(state, **kw)["stages"]}


class TestStageSkeleton:
    def test_永远是固定的三阶段且顺序不变(self):
        out = project({})
        assert [s["key"] for s in out["stages"]] == [STAGE_PARSE, STAGE_ASSESS, STAGE_REPORT]

    def test_阶段名与前端原型一致(self):
        assert STAGE_NAMES == {
            "parse": "简历解析推断",
            "assess": "对话问答测评",
            "report": "综合能力报告",
        }
        assert [s["name"] for s in project({})["stages"]] == list(STAGE_NAMES.values())

    @pytest.mark.parametrize("state", [{}, {"paper": [Q1]}, {"report": {"match_score": 1}}])
    def test_状态取值只在三种之内(self, state):
        assert all(s["status"] in ("pending", "active", "done") for s in project(state)["stages"])

    def test_每个阶段都带输出对象(self):
        assert all(isinstance(s["output"], dict) for s in project({})["stages"])


class TestParseStage:
    def test_一开始高亮在解析(self):
        out = project({})
        assert stages_of({})[STAGE_PARSE]["status"] == "active"
        assert out["current_stage"] == STAGE_PARSE

    def test_跳过解析时仍算未开始(self):
        """profile_meta.engine == 'skip' 表示学员没传简历，不算解析完成。"""
        s = stages_of({"profile_meta": {"engine": "skip"}})
        assert s[STAGE_PARSE]["status"] == "active"

    def test_解析出结果后转完成并带技能清单(self):
        state = {
            "profile_meta": {"engine": "llm_rate"},
            "profile_levels": {"低": 1, "高": 4, "中": 3},
        }
        s1 = stages_of(state)[STAGE_PARSE]
        assert s1["status"] == "done"
        assert s1["output"]["engine"] == "llm_rate"
        assert s1["output"]["skill_count"] == 3
        assert [x["skill_key"] for x in s1["output"]["skills"]] == ["高", "中", "低"]

    def test_已经开始出题时解析阶段一律算完成(self):
        s1 = stages_of({"paper": [Q1]})[STAGE_PARSE]
        assert s1["status"] == "done"

    def test_解析报错时把原因放进备注(self):
        s1 = stages_of({"profile_meta": {"engine": "rule_fallback", "error": "网关超时"}})[STAGE_PARSE]
        assert s1["output"]["note"] == "网关超时"


class TestAssessStage:
    def test_没出题时待办(self):
        assert stages_of({})[STAGE_ASSESS]["status"] == "pending"

    def test_出题后高亮并回显游标(self):
        s2 = stages_of({"paper": [Q1, Q2], "cursor": 1, "batches": 2})[STAGE_ASSESS]
        assert s2["status"] == "active"
        assert s2["output"] == {"asked": 2, "answered": 0, "cursor": 1, "batches": 2}

    def test_无效作答不计入已答(self):
        state = {
            "paper": [Q1, Q2],
            "graded": [{"level": 3}, {"invalid": True, "level": None}],
        }
        assert stages_of(state)[STAGE_ASSESS]["output"]["answered"] == 1

    def test_出报告后测评阶段转完成且不再带游标(self):
        state = {"paper": [Q1], "graded": [{"level": 3}], "report": {"match_score": 60}, "batches": 1}
        s2 = stages_of(state)[STAGE_ASSESS]
        assert s2["status"] == "done"
        assert "cursor" not in s2["output"]


class TestReportStage:
    def test_没报告时待办且输出为空(self):
        s3 = stages_of({"paper": [Q1]})[STAGE_REPORT]
        assert s3["status"] == "pending" and s3["output"] == {}

    def test_有报告时整份塞进输出(self):
        rep = {"match_score": 88.8, "items": []}
        s3 = stages_of({"report": rep})[STAGE_REPORT]
        assert s3["status"] == "done" and s3["output"] == rep

    def test_当前阶段随进度推进(self):
        assert project({})["current_stage"] == STAGE_PARSE
        assert project({"paper": [Q1]})["current_stage"] == STAGE_ASSESS
        assert project({"paper": [Q1], "report": {"x": 1}})["current_stage"] == STAGE_REPORT


class TestQuestionEndAndProgress:
    def test_出题未收敛时前端应继续等新题(self):
        assert project({"paper": [Q1]})["question_end"] is False

    def test_收敛标记或已出报告都算出完(self):
        assert project({"paper": [Q1], "exhausted": True})["question_end"] is True
        assert project({"report": {"x": 1}})["question_end"] is True

    def test_进度里的预估总题数为零时给None(self):
        """懒加载下真实总题数事先未知；给 0 会让前端显示「1 / 0」。"""
        assert project({})["progress"]["planned_total"] is None
        assert project({"planned_total": 8})["progress"]["planned_total"] == 8

    def test_收敛原因透传(self):
        assert project({"stop_reason": "已出满预估题数 6"})["progress"]["stop_reason"] == "已出满预估题数 6"
        assert project({})["progress"]["stop_reason"] == ""

    def test_中断标记表示正在等作答(self):
        assert project({}, interrupted=True)["awaiting_answer"] is True
        assert project({})["awaiting_answer"] is False

    def test_岗位原样带出(self):
        occ = {"id": "o1", "name": "混凝土工"}
        assert project({"occupation": occ})["occupation"] == occ

    @pytest.mark.parametrize("cursor", [None, "", "3", 3])
    def test_游标脏值不炸(self, cursor):
        assert isinstance(project({"cursor": cursor})["progress"]["cursor"], int)


class TestPendingQuestion:
    def test_优先取中断携带的题(self):
        assert pending_question({"paper": [Q1], "cursor": 0}, {"question": Q2}) is Q2

    def test_否则按游标从卷子里取(self):
        assert pending_question({"paper": [Q1, Q2], "cursor": 1}) is Q2

    def test_游标越界时没有待答题(self):
        assert pending_question({"paper": [Q1], "cursor": 5}) is None
        assert pending_question({"paper": [], "cursor": 0}) is None

    def test_中断值不是题时回落到游标(self):
        assert pending_question({"paper": [Q1], "cursor": 0}, "随便") is Q1
        assert pending_question({"paper": [Q1], "cursor": 0}, {"noise": 1}) is Q1


class TestWithProgress:
    def test_补上预估总题数(self):
        out = with_progress(dict(Q1), {"planned_total": 6})
        assert out["planned_total"] == 6

    def test_丢掉懒加载下没有意义的旧total字段(self):
        out = with_progress({**Q1, "total": 1}, {"planned_total": 6})
        assert "total" not in out, "旧字段会让前端显示 1/?"

    def test_没有题时原样返回(self):
        assert with_progress(None, {"planned_total": 6}) is None

    def test_不就地改原题(self):
        q = {**Q1, "total": 1}
        with_progress(q, {})
        assert q["total"] == 1
