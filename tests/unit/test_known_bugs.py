"""写单测过程中发现、**尚未修好**的 backend 缺陷 —— 立此存照。

带 `xfail(strict=True)` 的用例断言的是**修好之后**应有的行为：现在 XFAIL（套件仍绿），
修好后变 XPASS 让套件变红，提醒把标记删掉、把用例挪进对应模块的正式测试文件。

不带标记的是「当前行为的存档」（characterization test）：它们现在是绿的，只是把
一个可疑但尚未定论的现状钉住，重构时若无意改动会立刻发现。

已定案的缺陷，测试已迁出本文件（都改成了**正向断言**，不再是 xfail）：

| 缺陷 | 定案 | 现在锁在哪 |
| --- | --- | --- |
| BUG-1 越界档位让 `base_score` 抛 KeyError、打死整份报告 | 越界值按「无效档位」处理（记 0 分） | `test_assessment_report.py::TestDirtyRadarLevel` / `TestDirtyLevelGuard` |
| BUG-2 两个匹配度口径不同源 | 统一为「岗位整体准备度」= 可评分权重分母 | `test_match_score_parity.py`（同源锁）+ `test_assessment_report.py::TestBuildReportMatchScore` |
| BUG-3 `MAX_QUESTIONS=10` 在出题路径上失效 | 实际排出的题数必须 ≤ 10 | `test_assessment_pipeline.py::TestHardCap` / `TestCapKeepsSemantics` |
| BUG-5 脏 weight 一边抛错一边判 0，`"0.5"` 两边解析不一致 | 抽 `config.as_weight` 两边共用；数字字符串照解析、真脏值取 0 | `test_pg_guards.py::TestAsWeight` + `test_assessment_report.py::TestDirtyWeightInReport` + `test_match_score_parity.py::TestParityOnDirtyInput` |
| BUG-6 越界/缺失要求档被当成「无要求」反而白送满分 | 无基准的项整项排除出分子分母；全无基准时 `match_score=None` | `test_assessment_report.py::TestNoBaselineScoring` + `test_match_profile.py::TestNoBaseline` |
| BUG-7 画像侧 `user_levels` 没过 `as_level`：越界实测档在列表页拿满分、负数算出负分、字符串抛 TypeError | 画像侧也走 `config.as_level`（`ulv = as_level(raw) or 0`），与报告侧同一个函数 | `test_match_profile.py::TestDirtyMeasuredLevel`（画像侧正向断言）+ `test_match_score_parity.py::TestParityOnDirtyInput` 的脏实测档那几条（两侧同源）；两侧对「有证据」定义不同这个**残留**差异存档在 `TestEvidenceAsymmetry` |

留在本文件的只有 BUG-4：产品口径未定，不是「已知该怎么修但还没修」。

**BUG-8 例外，不在本文件**：`config.as_level` 把 `3.0` / `Decimal("3.0")` 判成
「没有档位」（`int("3.0")` 抛 ValueError），整个岗位会静默变成 `no_baseline`、
分数变 null。它的 strict xfail 与 `as_level` 的其余口径断言放在一起，
见 `test_pg_guards.py::TestAsLevel::test_整数值的浮点档位该收成整数` ——
一个函数的口径拆两个文件写，改的人只会看到一半。
"""
from __future__ import annotations

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
