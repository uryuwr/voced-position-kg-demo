"""一次性脚本：给 schemas*.py 里没写 description 的字段补上注释。

只补**通用字段**（id / name / region / status 这类各模型含义一致的），
业务含义特殊的字段留给人工——机械套一句"名称"比没注释更糟，
那会让人误以为已经写过了。

跑完自查：python -X utf8 scripts/check_openapi_shapes.py
"""
from __future__ import annotations

import io
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DESC = {
    "id": "节点 id",
    "name": "名称",
    "raw_name": "原始名称（未做展示名替换）",
    "type": "类型",
    "kg_type": "图侧节点类型",
    "region": "地区，如 CN",
    "status": "状态",
    "desc": "简介",
    "description": "描述",
    "source_url": "来源链接",
    "evidence": "判定依据",
    "confidence": "置信度",
    "weight": "权重",
    "level": "等级",
    "code": "编码",
    "title": "标题",
    "note": "备注说明",
    "items": "当前页数据",
    "total": "总条数",
    "total_pages": "总页数",
    "page": "页码，从 1 起",
    "page_size": "每页条数",
    "user_id": "UC 用户 id",
    "user_name": "用户名（冗余字段，用户中心不在本服务）",
    "updated_at": "更新时间 ISO8601",
    "created_at": "创建时间 ISO8601",
    "skill_id": "技能 id",
    "skill_name": "技能名",
    "skill_key": "技能聚合主键",
    "skill_hint": "技能提示",
    "salary": "薪资区间",
    "seq": "序号",
    "url": "链接",
    "version": "版本号",
    "attrs": "自由属性（无数据库约束的 JSON 列，键随数据来源而异）",
    "target_id": "目标节点 id",
    "reviewed_by": "审核人 id",
    "reviewed_by_name": "审核人姓名",
    "review_required": "是否开启审核（0=直写，1=进待审队列）",
    "users_with_goal": "已锁定学习目标的用户数",
    "servers": "服务地址列表",
    "swagger": "Swagger 文档地址",
    "service": "服务名",
    "store": "存储后端",
    "pending_title": "待审变更的标题",
    "path_id": "学习路径 id",
    "kind": "任务类型",
    "resource_id": "资源 id",
    "resource_title": "资源标题",
    "completed_at": "完成时间 ISO8601",
    "provider": "提供方",
    "parent_code": "父级编码",
    "tier": "层级",
    "demand": "需求热度",
    "points": "成长值/积分",
    "category": "分类",
    "base_score": "基准分",
    "loc": "出错字段路径",
    "msg": "错误说明",
    "kg_nodes": "图节点总数",
    "kg_edges": "图边总数",
    "diagnosis_sessions": "诊断会话数",
    "learning_paths": "学习路径数",
    "pending_proposals": "待审提案数",
    "major": "关联专业数",
    "occupation": "关联岗位数",
    "skill": "关联技能数",
    "industry": "关联行业数",
    "course": "关联课程数",
    "action": "变更动作：create / update / delete",
    "created_by": "提交人 id",
    "created_by_name": "提交人姓名",
    "dim_type": "维度类型：industry / major / occupation / skill",
    "direct": "true=直写生效；false=进待审队列",
    "entity_kind": "实体种类：node / edge",
    "payload": "变更内容；结构随 action 与 dim_type 而异（建节点的字段集与改边的完全不同）",
    "applied": "实际落库的内容；结构随变更类型而异，未通过时为 null",
    "detail": "该规则的取证细节，结构随 rule 而异（缺哪些档、少哪些边等）",
    "industry_id": "所属行业 id",
    "industry_name": "所属行业名",
    "major_name": "关联专业名",
    "occupation_name": "目标岗位名",
    "include_direct_occupations": "是否包含行业直挂的岗位（不经专业）",
    "include_skills": "是否下钻到技能层；默认不返回，数据量大",
    "industries": "归属行业（可多个）",
    "position": "岗位详情",
    "position_id": "岗位节点 id",
    "position_name": "岗位名",
    "neo4j_type": "Neo4j 侧的关系类型（历史兼容字段）",
    "q": "本次查询用的关键词（回显）",
    "rel_type": "关系类型",
    "prereq_skill_keys": "先修技能的聚合主键列表",
    "level_zh": "学历层次中文名",
    "api_prefix": "接口路径前缀",
    "default_region": "默认地区",
    "dev_ui": "自测页地址；未开启为 null",
    "guide": "对接说明页地址",
    "openapi_json": "OpenAPI 规格地址",
    "redoc": "ReDoc 文档地址",
    "required_headers": "调用必须携带的请求头",
    "nodes_by_type": "各类型节点数，键为节点类型",
    "aliases": "别名；历史数据里可能是数组也可能是对象，故不收紧",
    "meta": "本次查询的元信息（口径、计数、参数回显）",
    "docs": "Swagger 文档地址",
    "postgresql": "数据库连通性探测结果",
    "ai_gateway": "AI 网关就绪态",
    "value": "值",
    "text": "文案",
    "reason": "原因说明",
    "summary": "摘要",
    "details": "详情",
    "ok": "是否通过",
    "checked": "扫描数",
    "demoted": "降级数",
}

# 形如：    field: TYPE                （无默认值）
BARE = re.compile(r"^(\s+)([a-z_][a-z0-9_]*)\s*:\s*([^=\n]+?)\s*$")
# 形如：    field: TYPE = <默认值>      （默认值不是 Field(...)）
DEFAULTED = re.compile(r"^(\s+)([a-z_][a-z0-9_]*)\s*:\s*([^=]+?)\s*=\s*(?!Field\()(.+?)\s*$")
# 形如：    field: TYPE = Field(<单行、无 description>)
FIELD1 = re.compile(
    r"^(\s+)([a-z_][a-z0-9_]*)\s*:\s*([^=]+?)\s*=\s*Field\(([^)]*)\)\s*$"
)


def patch(path: Path) -> int:
    src = io.open(path, encoding="utf-8").read()
    out, n = [], 0
    for line in src.split("\n"):
        m = FIELD1.match(line)
        if m and "description" not in m.group(4):
            ind, fname, typ, args = m.groups()
            d = DESC.get(fname)
            if d:
                args = args.strip()
                inner = f'{args}, description="{d}"' if args else f'..., description="{d}"'
                out.append(f"{ind}{fname}: {typ} = Field({inner})")
                n += 1
                continue
        m = DEFAULTED.match(line)
        if m and "Field(" not in line:
            ind, fname, typ, dflt = m.groups()
            d = DESC.get(fname)
            if d and not dflt.startswith("#"):
                out.append(f'{ind}{fname}: {typ} = Field({dflt}, description="{d}")')
                n += 1
                continue
        m = BARE.match(line)
        if m and "Field(" not in line and not line.strip().startswith("#"):
            ind, fname, typ = m.groups()
            # 排除 class 体外的模块级注解与 model_config
            if fname in DESC and len(ind) >= 4 and not typ.endswith(","):
                out.append(f'{ind}{fname}: {typ} = Field(..., description="{DESC[fname]}")')
                n += 1
                continue
        out.append(line)
    if n:
        io.open(path, "w", encoding="utf-8", newline="\n").write("\n".join(out))
    return n


if __name__ == "__main__":
    total = 0
    for f in sorted((ROOT / "backend" / "api").glob("schemas*.py")):
        c = patch(f)
        total += c
        print(f"{c:4d}  {f.name}")
    print(f"合计补 {total} 处")
