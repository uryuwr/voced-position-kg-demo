"""写单测过程中发现、**尚未定案**的 backend 缺陷 —— 立此存照。

带 `xfail(strict=True)` 的用例断言的是**修好之后**应有的行为：现在 XFAIL（套件仍绿），
修好后变 XPASS 让套件变红，提醒把标记删掉、把用例挪进对应模块的正式测试文件。

不带标记的是「当前行为的存档」（characterization test）：它们现在是绿的，只是把
一个可疑但尚未定论的现状钉住，重构时若无意改动会立刻发现。

已定案的三个缺陷，测试已迁出本文件（都改成了**正向断言**，不再是 xfail）：

| 缺陷 | 定案 | 现在锁在哪 |
| --- | --- | --- |
| BUG-1 越界档位让 `base_score` 抛 KeyError、打死整份报告 | 越界值按「无效档位」处理（记 0 分） | `test_assessment_report.py::TestDirtyRadarLevel` / `TestDirtyLevelGuard` |
| BUG-2 两个匹配度口径不同源 | 统一为「岗位整体准备度」= 全权重分母 | `test_match_score_parity.py`（同源锁）+ `test_assessment_report.py::TestBuildReportMatchScore` |
| BUG-3 `MAX_QUESTIONS=10` 在出题路径上失效 | 实际排出的题数必须 ≤ 10 | `test_assessment_pipeline.py::TestHardCap` / `TestCapKeepsSemantics` |
"""
from __future__ import annotations

import pytest

from backend.agent.assessment.report import build_report


def it(key, *, weight=0.25, level=3, category="生产准备"):
    return {"skill_key": key, "weight": weight, "required_level": level, "category": category}


# ══════════════════════════════════════════════════════════════
# BUG-4（轻）：雷达回落到大类后仍可能不足 3 根轴，却不带「画不出来」的标记
# ══════════════════════════════════════════════════════════════
#
# `_build_radar` 的 docstring 写「仍不足才判定画不出来」，但代码里没有这个判定：
# 两项技能同属一个大类时，回落后只剩 1 根轴，照样返回 axis_type='category'。
# 前端拿到 1 根轴画不成多边形，只能自己再判一次。
#
# 未定案：是后端补一个 `drawable: false`（前端少一处判断，但多一个契约字段），
# 还是把 docstring 里那句承诺删掉、明确「够不够画由前端判」。等产品定。


class TestRadarTooFewAxes:
    def test_当前行为存档_回落后一根轴也照样返回(self):
        required = [it("A", weight=0.5, category="同一类"), it("B", weight=0.5, category="同一类")]
        rep = build_report(
            occupation=None, required_items=required,
            measured={"A": {"level": 3}, "B": {"level": 2}},
        )
        radar = rep["radar"]
        assert radar["axis_type"] == "category"
        assert len(radar["categories"]) == 1
        assert "drawable" not in radar and "insufficient" not in radar


# ══════════════════════════════════════════════════════════════
# BUG-5（写 BUG-2 的同源测试时发现）：脏 weight 一边抛错、一边按 0 处理
# ══════════════════════════════════════════════════════════════
#
# `weight` 来自 `kg_edge.weight`，采集脚本与直连改库都绕得过应用层校验：
#   - `match_with_profile`：`float(w) if isinstance(w, (int, float)) else 0.0` —— 脏值取 0，站得住
#   - `build_report`      ：`float(it.get("weight") or 0)` —— `"abc"` 直接 ValueError，整份报告 500
# 形状就是项目栽过四次的「一条脏数据打死一整页」，只是这次落在报告接口上。
#
# 顺带一个口径分家：`"0.5"` 这种**数字字符串**在报告侧被当成 0.5、在画像侧被当成 0.0，
# 于是 BUG-2 刚统一好的匹配度又能在脏数据上分家（见下面第二条存档）。
#
# 未定案：是把 report 改成跟画像侧一样的 `isinstance` 判定（脏值一律取 0），
# 还是抽一个共用的 `_as_weight()` 两边都用。建议随 BUG-2 一并定。


class TestDirtyWeight:
    @pytest.mark.known_bug
    @pytest.mark.xfail(
        strict=True, raises=ValueError,
        reason="BUG-5（未定案）：非数值 weight 让 build_report 抛 ValueError，整份报告 500。",
    )
    def test_非数值权重不该打死报告(self):
        rep = build_report(
            occupation=None,
            required_items=[{"skill_key": "A", "category": "c", "required_level": 3, "weight": "abc"}],
            measured={"A": {"level": 3}},
        )
        assert isinstance(rep["match_score"], float)

    def test_当前行为存档_数字字符串权重两边解析不一致(self):
        """report 认 `"0.5"`、match_with_profile 认成 0.0 —— 统一 weight 解析后改这条。"""
        from backend.kg.pg_store.biz_store import match_with_profile

        required = [{"skill_key": "A", "category": "c", "required_level": 3, "weight": "0.5"}]
        rep = build_report(occupation=None, required_items=required, measured={"A": {"level": 3}})
        prof = match_with_profile({"id": "o"}, required, {"A": 3})
        assert rep["items"][0]["weight"] == 0.5
        assert prof["items"][0]["weight"] == 0.0


# ══════════════════════════════════════════════════════════════
# BUG-6（BUG-1 修复的副作用）：越界要求档被当成「岗位没有要求」，反而白送满分
# ══════════════════════════════════════════════════════════════
#
# BUG-1 的定案是「越界档位按无效处理」，`report._level()` 把 9 取成 None，
# `build_report` 里再 `or 0` 落成 req=0。而 `_ratio` 对 req=0 的口径是
# 「有实测即满分」（本意是「岗位没给要求档就别为此扣分」）。两条规则叠起来：
# 一条 `attrs.level='9'` 的脏边 → 该技能按**满分**计入匹配度。
#
# 不再 500 了（BUG-1 的目标达到了），但脏数据现在是往上抬分而不是往下压分，
# 方向上比原来更糟：学员看到虚高的「整体准备度」。
#
# 未定案的三条路：
#   a) 越界要求档 ⇒ ratio 0（保守：没有可信标准就不给分）
#   b) 越界要求档 ⇒ 整项**排除**出分母（既不给分也不扣分，用 coverage 表达）
#   c) 维持现状，把「无效要求档」的项数在报告里显式暴露给运营去修数据
# 注意 a/b 要和 `match_with_profile` 一起改，否则 BUG-2 刚统一的口径又分家。


class TestDirtyRequiredLevelInflatesScore:
    def test_当前行为存档_越界要求档让该项按满分计入(self):
        required = [
            {"skill_key": "脏边", "category": "c", "required_level": 9, "weight": 0.5},
            {"skill_key": "正常", "category": "c", "required_level": 5, "weight": 0.5},
        ]
        rep = build_report(
            occupation=None, required_items=required,
            measured={"脏边": {"level": 1}, "正常": {"level": 1}},
        )
        脏, 正常 = rep["items"]
        assert (脏["ratio"], 脏["ok"]) == (1.0, False), "算满分，却又不算达标"
        assert 正常["ratio"] == 0.2
        # 只因为一条脏边，L1 的学员在这个岗位上拿到 60%
        assert rep["match_score"] == 60.0
        assert [g["skill_key"] for g in rep["gaps"]] == ["正常", "脏边"]
