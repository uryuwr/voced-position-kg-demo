"""业务库表（学员端/管理端运行时），与图 kg_node/kg_edge 分离。"""
from __future__ import annotations

BIZ_SCHEMA_SQL = """
-- 学习目标（对齐 frontend goal）
CREATE TABLE IF NOT EXISTS biz_user_goal (
  user_id TEXT PRIMARY KEY,
  user_name TEXT NOT NULL DEFAULT '',
  occupation_id TEXT,
  occupation_name TEXT,
  major_id TEXT,
  major_name TEXT,
  industry_id TEXT,
  industry_name TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 目标从「每人一个」升级为「每人多个、其一为活跃」：原型卡片是「当前活跃目标」，
-- 而换目标后旧目标的测评结果、晋升进度仍要能查回来，所以按 (user_id, occupation_id)
-- 存一行。老表主键 user_id 会把用户锁死在单个目标上，必须换成业务唯一键。
ALTER TABLE biz_user_goal ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE biz_user_goal ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE biz_user_goal DROP CONSTRAINT IF EXISTS biz_user_goal_pkey;
CREATE UNIQUE INDEX IF NOT EXISTS uq_biz_user_goal_user_occ
  ON biz_user_goal(user_id, occupation_id);
-- 每人至多一个活跃目标
CREATE UNIQUE INDEX IF NOT EXISTS uq_biz_user_goal_active
  ON biz_user_goal(user_id) WHERE status = 'active';

-- 学员 × 岗位 × 学习计划 的关联。
-- 学习计划由外部服务生成并返回 plan_id，本库只存关联关系（不存计划内容），
-- 这样「岗位学习与自适应路径」列表能显示某岗位已生成过哪些计划，
-- 综合能力报告也能回指到它是基于哪次诊断的哪些短板生成的。
CREATE TABLE IF NOT EXISTS biz_user_learning_plan (
  id            bigserial PRIMARY KEY,
  user_id       TEXT NOT NULL,
  occupation_id TEXT NOT NULL,
  plan_id       TEXT NOT NULL,
  session_id    BIGINT,
  gap_skills    JSONB,
  source        TEXT NOT NULL DEFAULT 'api',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (user_id, occupation_id, plan_id)
);
CREATE INDEX IF NOT EXISTS idx_biz_ulp_user_occ
  ON biz_user_learning_plan(user_id, occupation_id, created_at DESC);

-- 测评题目与作答。
-- 之前这些存在 LangGraph checkpointer 的序列化 blob 里，运营连「哪道题所有人都选
-- 最低档、是不是出得有问题」这种 SQL 都写不出来。题目与答案本就是要长期保存、
-- 可统计可复盘的业务数据，checkpointer 只适合存「图跑到哪了」。
CREATE TABLE IF NOT EXISTS biz_assessment_question (
  id             BIGSERIAL PRIMARY KEY,
  session_id     BIGINT NOT NULL REFERENCES biz_diagnosis_session(id) ON DELETE CASCADE,
  idx            INT NOT NULL,
  type           TEXT NOT NULL,                 -- choice | open
  variant        TEXT,                          -- sjt | self_report | generic
  skill_key      TEXT,
  category       TEXT,
  required_level INT,
  weight         DOUBLE PRECISION,
  payload        JSONB NOT NULL,                -- prompt / options / rubric
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (session_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_biz_aq_skill ON biz_assessment_question(skill_key);

CREATE TABLE IF NOT EXISTS biz_assessment_answer (
  id           BIGSERIAL PRIMARY KEY,
  session_id   BIGINT NOT NULL REFERENCES biz_diagnosis_session(id) ON DELETE CASCADE,
  idx          INT NOT NULL,
  raw_answer   TEXT,
  level        INT,
  score        INT,
  grade_status TEXT NOT NULL DEFAULT 'pending', -- pending | graded | failed
  grade_json   JSONB,
  answered_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  graded_at    TIMESTAMPTZ,
  UNIQUE (session_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_biz_aa_pending
  ON biz_assessment_answer(session_id) WHERE grade_status = 'pending';

-- 用户技能画像
CREATE TABLE IF NOT EXISTS biz_user_skill (
  user_id TEXT NOT NULL,
  skill_id TEXT NOT NULL,
  skill_name TEXT,
  level INT NOT NULL DEFAULT 1,
  score INT NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'self',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (user_id, skill_id)
);

-- 诊断会话
CREATE TABLE IF NOT EXISTS biz_diagnosis_session (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  user_name TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL,
  target_occupation_id TEXT,
  target_occupation_name TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_biz_diag_user ON biz_diagnosis_session(user_id);

CREATE TABLE IF NOT EXISTS biz_diagnosis_result (
  session_id BIGINT PRIMARY KEY REFERENCES biz_diagnosis_session(id) ON DELETE CASCADE,
  match_score DOUBLE PRECISION,
  gap_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  radar_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  evidence_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  report_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS biz_resume_asset (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  content_text TEXT,
  file_name TEXT,
  parse_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'parsed',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS biz_chat_message (
  id BIGSERIAL PRIMARY KEY,
  session_id BIGINT NOT NULL REFERENCES biz_diagnosis_session(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  meta_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 学习路径两张表（biz_learning_path / biz_learning_step）已于 2026-08-18 下线：
-- 路径改由 e-ai-spaces 承载，本服务不再存副本（见 docs/实现方案-接入真实学习计划服务.md）。
-- **DROP 不写在这里**：这份 DDL 每次启动都跑，且预生产 PG 与 bcs-ai-agent 共用，
-- 把 DROP TABLE 放进幂等启动路径风险过高。代码引用已清空，两张表现在是无人读写的
-- 孤表，观察一个周期后人工执行：DROP TABLE biz_learning_step, biz_learning_path;

-- 成就
CREATE TABLE IF NOT EXISTS biz_achievement_def (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT,
  points INT NOT NULL DEFAULT 10,
  category TEXT
);

CREATE TABLE IF NOT EXISTS biz_user_achievement (
  user_id TEXT NOT NULL,
  achievement_code TEXT NOT NULL REFERENCES biz_achievement_def(code),
  unlocked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (user_id, achievement_code)
);

CREATE TABLE IF NOT EXISTS biz_user_points (
  user_id TEXT PRIMARY KEY,
  user_name TEXT NOT NULL DEFAULT '',
  total INT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 技能分类字典。kg_node.category 存 code，展示时连这张表取 name。
--
-- 种子在 kg/pg_store/skill_taxonomy.py，启动时幂等 upsert 进来：代码里留真源便于
-- review 与版本控制，库里有表便于连表和管理台增删。upsert **不删**库里多出来的行，
-- 管理台自行新增的分类不会被下次启动抹掉。
CREATE TABLE IF NOT EXISTS kg_skill_category (
  code        TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  sort_order  INT  NOT NULL DEFAULT 999,   -- 同时是前端分区顺序与学习推进顺序
  is_fallback BOOLEAN NOT NULL DEFAULT FALSE,
  status      TEXT NOT NULL DEFAULT 'published',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_kg_skill_category_sort
  ON kg_skill_category(sort_order, code);

-- 逻辑技能先修（BR-05，skill_key 维度，无环由写入校验）
CREATE TABLE IF NOT EXISTS kg_skill_prereq (
  skill_key TEXT NOT NULL,
  prereq_skill_key TEXT NOT NULL,
  region TEXT NOT NULL DEFAULT 'CN',
  evidence TEXT,
  confidence TEXT NOT NULL DEFAULT 'manual_seed',
  created_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (region, skill_key, prereq_skill_key),
  CHECK (skill_key <> prereq_skill_key)
);
CREATE INDEX IF NOT EXISTS idx_kg_skill_prereq_prereq
  ON kg_skill_prereq(region, prereq_skill_key);


-- 运营事件（薄）
CREATE TABLE IF NOT EXISTS biz_event (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT,
  event_type TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 学习计划推送记录扩展。
-- CREATE TABLE IF NOT EXISTS 不会给既有表补列，故显式 ALTER。
ALTER TABLE biz_user_learning_plan ADD COLUMN IF NOT EXISTS external_path_id TEXT;    -- 幂等键，见 learningplan/builder.py
ALTER TABLE biz_user_learning_plan ADD COLUMN IF NOT EXISTS payload_sha256 TEXT;      -- 推送快照指纹，409 排查用
ALTER TABLE biz_user_learning_plan ADD COLUMN IF NOT EXISTS push_status TEXT;         -- pending | ok | failed
ALTER TABLE biz_user_learning_plan ADD COLUMN IF NOT EXISTS superseded_plan_id TEXT;  -- 对方归档的旧计划
ALTER TABLE biz_user_learning_plan ADD COLUMN IF NOT EXISTS last_error TEXT;          -- 失败原因，支持重推
ALTER TABLE biz_user_learning_plan ADD COLUMN IF NOT EXISTS pushed_at TIMESTAMPTZ;
-- 幂等键唯一。推失败的记录 plan_id 为空串，靠这个索引仍能定位到同一条重推。
CREATE UNIQUE INDEX IF NOT EXISTS uq_biz_ulp_user_extpath
  ON biz_user_learning_plan(user_id, external_path_id)
  WHERE external_path_id IS NOT NULL;
"""

# 这是 upsert 种子：删掉列表项**不会**清掉库里已有的定义行与用户已解锁记录。
# `first_step` / `streak_3` 依赖「学员完成了某个步骤」这个事件，而进度真源已移到
# e-ai-spaces，本服务收不到完成事件，两个成就再也不会被颁发。留着就是学员成就墙上
# 永不点亮的孤儿条目，所以从种子里摘掉，并在下线两张表时一并人工清理：
#   DELETE FROM biz_user_achievement WHERE achievement_code IN ('first_step','streak_3');
#   DELETE FROM biz_achievement_def  WHERE code IN ('first_step','streak_3');
ACHIEVEMENT_SEEDS = [
    ("first_goal", "锁定目标", "首次设定学习目标岗位", 20, "goal"),
    ("first_diag", "初诊完成", "完成一次能力诊断", 30, "diag"),
    ("first_path", "路径启程", "生成专属学习路径", 20, "learn"),
]
