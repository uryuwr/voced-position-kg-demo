"""backend/agent/assessment/grading.py —— 判分（选择题定档、问答题收敛、画像合并）。

`grade_open` 的规则降级路径这里强制走到（monkeypatch `llm_ready` → False），
不受本机 .env 里有没有网关影响。
"""
from __future__ import annotations

import pytest

from backend.agent.assessment import grading
from backend.agent.assessment.grading import (
    _level_ceiling,
    _rule_evidence_score,
    grade_choice,
    grade_open,
    merge_measured,
)


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """判分的降级分支必须可测：内测环境网关常为空，测试不能依赖它到底空不空。"""
    monkeypatch.setattr(grading, "llm_ready", lambda: False)


SJT = {
    "skill_key": "配料准备",
    "category": "生产准备",
    "variant": "sjt",
    "required_level": 3,
    "weight": 0.4,
    "options": [
        {"value": 1, "level": 3, "text": "复现工况后用示波器比对波形定位"},
        {"value": 2, "level": 1, "text": "只换常规件"},
        {"value": 3, "level": 5, "text": "溯源批次缺陷并形成 SOP"},
    ],
}


# ── grade_choice ──────────────────────────────────────────────


class TestGradeChoice:
    def test_选中即定档而不是按选项顺序(self):
        """SJT 的选项顺序不按档位排列，答案是 value，档位来自选项自带的 level。"""
        g = grade_choice(SJT, 2)
        assert g["level"] == 1
        assert g["picked_value"] == 2
        assert g["picked_text"] == "只换常规件"

    def test_答案为数字字符串也接受(self):
        assert grade_choice(SJT, "3")["level"] == 5

    @pytest.mark.parametrize("answer", [None, 0, 99, -1, "x", "", [1], {"a": 1}, -0.5])
    def test_无效选项判无效而不是抛错(self, answer):
        g = grade_choice(SJT, answer)
        assert g["invalid"] is True
        assert g["level"] is None
        assert "可选" in g["message"]

    @pytest.mark.parametrize("answer,level", [(1.5, 3), (2.9, 1)])
    def test_小数答案被截断成整数(self, answer, level):
        """当前行为存档：int(1.5)==1，落到 1 号选项。接口层已用 Pydantic 收成 int，
        这里只是记录纯函数自身的口径，别在重构时无意改成四舍五入或拒收。"""
        assert grade_choice(SJT, answer)["level"] == level

    def test_情景判断题标记为强证据(self):
        assert grade_choice(SJT, 1)["source"] == "sjt"

    def test_自评题标记为弱证据(self):
        q = {**SJT, "variant": "self_report"}
        assert grade_choice(q, 1)["source"] == "self_report"
        q2 = {k: v for k, v in SJT.items() if k != "variant"}
        assert grade_choice(q2, 1)["source"] == "self_report"

    def test_带出要求档与权重供报告加权(self):
        g = grade_choice(SJT, 1)
        assert (g["required_level"], g["weight"]) == (3, 0.4)
        assert g["category"] == "生产准备"

    def test_档位为零视为未定档(self):
        q = {"skill_key": "A", "options": [{"value": 1, "level": 0, "text": "t"}]}
        assert grade_choice(q, 1)["level"] is None

    def test_没有选项时判无效(self):
        assert grade_choice({"skill_key": "A"}, 1)["invalid"] is True

    def test_选中文本截断到两百字(self):
        q = {"skill_key": "A", "options": [{"value": 1, "level": 3, "text": "长" * 500}]}
        assert len(grade_choice(q, 1)["picked_text"]) == 200


# ── 证据分与档位上限 ──────────────────────────────────────────


class TestEvidenceScore:
    def test_空回答零分(self):
        assert _rule_evidence_score("") == (0, [])
        assert _rule_evidence_score(None) == (0, [])

    def test_短且无信号词零分(self):
        assert _rule_evidence_score("我会一点") == (0, [])

    def test_四类证据齐全时满分区间(self):
        text = (
            "我负责生产线的排查与优化，方法是逐项比对波形定位异常；"
            "最终故障率下降 30%，交付按时完成。"
        )
        score, hits = _rule_evidence_score(text)
        assert set(hits) == {"篇幅", "方法", "结果", "量化"}
        assert score == 85

    def test_只有篇幅时二十分(self):
        score, hits = _rule_evidence_score("啊" * 45)
        assert (score, hits) == (20, ["篇幅"])

    def test_八十字加十五分且不进命中列表(self):
        score, hits = _rule_evidence_score("啊" * 85)
        assert (score, hits) == (35, ["篇幅"])

    def test_封顶一百(self):
        text = "方法流程步骤工具方案排查分析优化调试，结果提升下降完成解决，数量 100 次。" * 5
        assert _rule_evidence_score(text)[0] == 100

    @pytest.mark.parametrize(
        "score,ceiling",
        [(0, 1), (29, 1), (30, 2), (49, 2), (50, 3), (69, 3), (70, 4), (84, 4), (85, 5), (100, 5)],
    )
    def test_证据分到档位上限的阶梯(self, score, ceiling):
        assert _level_ceiling(score) == ceiling


# ── grade_open：只收敛不抬升 ──────────────────────────────────


class TestGradeOpen:
    STRONG = (
        "我负责生产线的排查与优化，方法是逐项比对波形定位异常；"
        "最终故障率下降 30%，交付按时完成。"
    )

    def test_证据不足时下调自评档并标记(self):
        g = grade_open({"skill_key": "A", "required_level": 4}, "我很擅长", self_level=5)
        assert g["level"] == 1
        assert g["self_level"] == 5
        assert g["capped"] is True

    def test_证据充分也不抬升自评档(self):
        """问答题职责是校验虚高，不是重新定级 —— 漂亮的自述不足以证明更高水平。"""
        g = grade_open({"skill_key": "A"}, self.STRONG, self_level=2)
        assert g["evidence_score"] == 85
        assert g["level"] == 2, "上限 5 也只能维持自评的 2"
        assert g["capped"] is False

    def test_没有自评档时直接取上限(self):
        g = grade_open({"skill_key": "A"}, self.STRONG, self_level=None)
        assert g["level"] == 5
        assert g["capped"] is False

    def test_降级路径标注引擎为规则(self):
        g = grade_open({"skill_key": "A"}, "短", self_level=None)
        assert g["meta"]["engine"] == "rule"
        assert g["meta"]["gateway"]["enabled"] is False
        assert g["source"] == "verified"

    def test_模型判分抛错时退回规则并留下痕迹(self, monkeypatch):
        monkeypatch.setattr(grading, "llm_ready", lambda: True)

        def _boom(*a, **k):
            raise RuntimeError("网关 502")

        monkeypatch.setattr(grading, "_llm_score", _boom)
        g = grade_open({"skill_key": "A"}, self.STRONG, self_level=None)
        assert g["meta"]["engine"] == "rule_fallback"
        assert "网关 502" in g["meta"]["error"]
        assert g["level"] == 5, "判分失败不能中断测评"

    def test_模型判分成功时采用其分数(self, monkeypatch):
        monkeypatch.setattr(grading, "llm_ready", lambda: True)
        monkeypatch.setattr(grading, "_llm_score", lambda q, t: (55, "还行", ["r1"]))
        g = grade_open({"skill_key": "A", "rubric": ["r1", "r2"]}, "任意", self_level=5)
        assert (g["evidence_score"], g["level"], g["capped"]) == (55, 3, True)
        assert g["meta"]["engine"] == "llm"
        assert g["rubric_met"] == ["r1"]

    def test_空回答不炸(self):
        assert grade_open({"skill_key": "A"}, "", self_level=3)["level"] == 1
        assert grade_open({"skill_key": "A"}, None, self_level=None)["level"] == 1


# ── merge_measured：一技能一档 ────────────────────────────────


class TestMergeMeasured:
    def test_问答题结果覆盖选择题自评(self):
        out = merge_measured([
            {"skill_key": "A", "type": "choice", "level": 4},
            {"skill_key": "A", "type": "open", "level": 2},
        ])
        assert out["A"]["level"] == 2 and out["A"]["type"] == "open"

    def test_问答题在前时选择题不再覆盖(self):
        out = merge_measured([
            {"skill_key": "A", "type": "open", "level": 2},
            {"skill_key": "A", "type": "choice", "level": 4},
        ])
        assert out["A"]["level"] == 2

    def test_同类型取先出现的(self):
        out = merge_measured([
            {"skill_key": "A", "type": "choice", "level": 4},
            {"skill_key": "A", "type": "choice", "level": 1},
        ])
        assert out["A"]["level"] == 4

    @pytest.mark.parametrize(
        "row",
        [
            {"skill_key": None, "type": "choice", "level": 3},
            {"skill_key": "A", "type": "choice", "level": None},
            {"skill_key": "A", "type": "choice", "level": 3, "invalid": True},
            {"type": "choice", "level": 3},
        ],
    )
    def test_无效或未定档的作答不进结果(self, row):
        assert merge_measured([row]) == {}

    def test_简历画像只补没测到的技能(self):
        out = merge_measured(
            [{"skill_key": "A", "type": "choice", "level": 4}],
            {"A": 1, "B": 3},
        )
        assert out["A"]["level"] == 4, "实测优先于简历推断"
        assert out["B"] == {"skill_key": "B", "level": 3, "type": "profile", "source": "resume"}

    def test_画像里档位为零的不补(self):
        assert merge_measured([], {"B": 0}) == {}

    def test_返回的是副本不改原作答(self):
        g = {"skill_key": "A", "type": "choice", "level": 4}
        out = merge_measured([g])
        out["A"]["level"] = 1
        assert g["level"] == 4
