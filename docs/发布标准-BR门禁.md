# 发布标准 BR-01～BR-08 与服务端门禁

## 规则摘要

| 编号 | 内容 | 落点 |
| --- | --- | --- |
| BR-01 | `skill_level_meta` 全局单源（L1–L5 名 + base_score） | `backend/kg/pg_store/skill_level_meta.py`、`GET /v1/student/meta/skill-levels` |
| BR-02 | 专业发布：≥1 已发布岗位（`prepares_for`） | `publish_rules.check_br02_major` |
| BR-03 | 岗位 `requires.weight` 按 skill_key 聚合 Σ≈1（±0.01） | `check_br03_occupation` |
| BR-04 | 技能 L1–L5 行为描述齐全 | `check_br04_skill` |
| BR-05 | 先修无环 | `check_br05` + `skill_prereq.add` |
| BR-06 | 删技能前 requires/课程边/被先修引用须空 | `check_br06` + 审核 delete |
| BR-07 | `status=draft` 不进前台/图检索 | search / explore / expand / list(默认) / 学员列表 / 节点详情 |
| BR-08 | 草稿→发布必须过 BR-02～06 | 审核 enable、PATCH status=published、新建通过后升权 |

## 服务端接口

| 接口 | 作用 |
| --- | --- |
| `POST/GET /v1/admin/publish/validate` | 只读校验，不写库 |
| `POST /v1/admin/publish/demote` | 扫描不达标 published → draft（默认 `dry_run=true`） |
| `POST /v1/admin/changes/{id}/approve`（action=enable） | **硬门禁**：失败 400，不发布 |
| 审核 create 通过 | 先 draft，门禁过则升 published，否则保持草稿 |
| `PATCH /v1/kg/nodes` `status=published` | 硬门禁 |
| 图/学员读路径 | 仅 `COALESCE(status,'published')='published'` |

## 存量治理

```bash
# 预览
python scripts/demote_noncompliant_publish.py --dry-run

# 落库
python scripts/demote_noncompliant_publish.py --apply
```

报告：`reports/demote_noncompliant.json`。

## 状态语义

- **published**：前台 + 图可见，须满足对应 BR
- **draft**：管理端 `scope=manage` 可见；前台/图/学员不可见
- **disabled**：停用，不可见
- **archived**：软删
