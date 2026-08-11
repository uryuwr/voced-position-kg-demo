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

-- 学习路径
CREATE TABLE IF NOT EXISTS biz_learning_path (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  user_name TEXT NOT NULL DEFAULT '',
  occupation_id TEXT,
  occupation_name TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  source TEXT NOT NULL DEFAULT 'diagnosis',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_biz_path_user ON biz_learning_path(user_id);

CREATE TABLE IF NOT EXISTS biz_learning_step (
  id BIGSERIAL PRIMARY KEY,
  path_id BIGINT NOT NULL REFERENCES biz_learning_path(id) ON DELETE CASCADE,
  seq INT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'skill',
  skill_id TEXT,
  skill_name TEXT,
  resource_id TEXT,
  resource_title TEXT,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  completed_at TIMESTAMPTZ
);

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

-- 学习路径「阶段任务树」扩展（前台原型 4.8/4.9/4.10）
-- CREATE TABLE IF NOT EXISTS 不会给既有表补列，故显式 ALTER。
ALTER TABLE biz_learning_step ADD COLUMN IF NOT EXISTS stage INT;              -- 阶段序号 1..N
ALTER TABLE biz_learning_step ADD COLUMN IF NOT EXISTS stage_title TEXT;       -- 阶段名（取技能大类）
ALTER TABLE biz_learning_step ADD COLUMN IF NOT EXISTS category TEXT;          -- 技能大类
ALTER TABLE biz_learning_step ADD COLUMN IF NOT EXISTS weight DOUBLE PRECISION;-- 任务权重（国标技能权重）
ALTER TABLE biz_learning_step ADD COLUMN IF NOT EXISTS duration_min INT;       -- 建议耗时（分钟，按目标等级估算）
ALTER TABLE biz_learning_step ADD COLUMN IF NOT EXISTS required_level INT;     -- 目标等级
"""

ACHIEVEMENT_SEEDS = [
    ("first_goal", "锁定目标", "首次设定学习目标岗位", 20, "goal"),
    ("first_diag", "初诊完成", "完成一次能力诊断", 30, "diag"),
    ("first_path", "路径启程", "生成专属学习路径", 20, "learn"),
    ("first_step", "学完一步", "完成学习路径中的一个步骤", 15, "learn"),
    ("streak_3", "连续学习", "累计完成 3 个学习步骤", 40, "learn"),
]
