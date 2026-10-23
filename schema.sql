-- SkillBridge schema for SQLite 3.35+

PRAGMA foreign_keys = ON;

CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
INSERT INTO schema_version (version) VALUES (1);

CREATE TABLE profiles (
  id            TEXT PRIMARY KEY,
  role          TEXT NOT NULL DEFAULT 'student' CHECK (role IN ('student', 'tutor', 'admin')),
  full_name     TEXT NOT NULL CHECK (length(full_name) BETWEEN 2 AND 120),
  email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  avatar        TEXT,
  bio           TEXT,
  subjects      TEXT,
  is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
  is_verified   INTEGER NOT NULL DEFAULT 0 CHECK (is_verified IN (0, 1)),
  verification_token TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  last_seen_at  TEXT
);

CREATE TABLE modules (
  id          INTEGER PRIMARY KEY,
  title       TEXT NOT NULL,
  sort_order  INTEGER NOT NULL
);

CREATE TABLE subtopics (
  id         INTEGER PRIMARY KEY,
  module_id  INTEGER NOT NULL REFERENCES modules(id),
  title      TEXT NOT NULL
);

CREATE TABLE skills (
  id           INTEGER PRIMARY KEY,
  module_id    INTEGER NOT NULL REFERENCES modules(id),
  subtopic_id  INTEGER NOT NULL REFERENCES subtopics(id),
  name         TEXT NOT NULL,
  UNIQUE (module_id, name)
);

CREATE TABLE skill_prerequisites (
  skill_id          INTEGER NOT NULL REFERENCES skills(id),
  requires_skill_id INTEGER NOT NULL REFERENCES skills(id),
  PRIMARY KEY (skill_id, requires_skill_id),
  CHECK (skill_id <> requires_skill_id)
);

CREATE TABLE scoring_weights (
  version   INTEGER NOT NULL,
  category  TEXT NOT NULL CHECK (category IN ('F', 'P', 'C')),
  weight    REAL NOT NULL CHECK (weight > 0),
  PRIMARY KEY (version, category)
);
INSERT INTO scoring_weights (version, category, weight)
VALUES (1, 'F', 0.30), (1, 'P', 0.40), (1, 'C', 0.30);

-- Set A = pretest. Set B = retest. Set C = long test. Same skill positions.
CREATE TABLE items (
  id                TEXT PRIMARY KEY,
  module_id         INTEGER NOT NULL REFERENCES modules(id),
  position          INTEGER NOT NULL CHECK (position BETWEEN 1 AND 10),
  set_code          TEXT NOT NULL DEFAULT 'A' CHECK (set_code IN ('A', 'B', 'C')),
  purpose           TEXT NOT NULL DEFAULT 'assessment' CHECK (purpose IN ('assessment', 'practice')),
  primary_skill_id  INTEGER NOT NULL REFERENCES skills(id),
  category          TEXT NOT NULL CHECK (category IN ('F', 'P', 'C')),
  stem              TEXT NOT NULL,
  correct_option    TEXT NOT NULL CHECK (correct_option IN ('A', 'B', 'C', 'D')),
  key_verified      INTEGER NOT NULL DEFAULT 0 CHECK (key_verified IN (0, 1)),
  UNIQUE (set_code, module_id, position)
);

CREATE TABLE item_options (
  item_id        TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  option_letter  TEXT NOT NULL CHECK (option_letter IN ('A', 'B', 'C', 'D')),
  body           TEXT NOT NULL,
  error_source   TEXT CHECK (error_source IN ('F', 'P', 'C')),
  PRIMARY KEY (item_id, option_letter)
);

CREATE TABLE resources (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL CHECK (kind IN ('lesson', 'video', 'practice_set')),
  title         TEXT NOT NULL,
  body_md       TEXT,
  video_url     TEXT,
  est_minutes   INTEGER CHECK (est_minutes > 0),
  is_published  INTEGER NOT NULL DEFAULT 0 CHECK (is_published IN (0, 1)),
  author_id     TEXT REFERENCES profiles(id)
);

CREATE TABLE resource_skills (
  resource_id  TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
  skill_id     INTEGER NOT NULL REFERENCES skills(id),
  coverage     REAL NOT NULL CHECK (coverage BETWEEN 0 AND 1),
  PRIMARY KEY (resource_id, skill_id)
);

CREATE TABLE practice_responses (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id  TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  item_id     TEXT NOT NULL REFERENCES items(id),
  chosen      TEXT NOT NULL CHECK (chosen IN ('A', 'B', 'C', 'D')),
  is_correct  INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE attempts (
  id                 TEXT PRIMARY KEY,
  student_id         TEXT NOT NULL REFERENCES profiles(id),
  kind               TEXT NOT NULL CHECK (kind IN ('pretest', 'retest')),
  status             TEXT NOT NULL DEFAULT 'in_progress'
                       CHECK (status IN ('in_progress', 'submitted', 'void')),
  scoring_version    INTEGER NOT NULL DEFAULT 1,
  module_id          INTEGER REFERENCES modules(id),
  parent_attempt_id  TEXT REFERENCES attempts(id),
  tutor_id           TEXT REFERENCES profiles(id),
  started_at         TEXT NOT NULL DEFAULT (datetime('now')),
  submitted_at       TEXT,
  CHECK (kind = 'pretest' OR parent_attempt_id IS NOT NULL)
);

CREATE UNIQUE INDEX one_pretest_per_module ON attempts (student_id, module_id) WHERE kind = 'pretest';

CREATE TABLE responses (
  attempt_id   TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
  item_id      TEXT NOT NULL REFERENCES items(id),
  chosen       TEXT CHECK (chosen IN ('A', 'B', 'C', 'D')),
  answered_at  TEXT,
  PRIMARY KEY (attempt_id, item_id)
);

CREATE TABLE skill_scores (
  attempt_id        TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
  skill_id          INTEGER NOT NULL REFERENCES skills(id),
  proficiency       REAL NOT NULL CHECK (proficiency BETWEEN 0 AND 100),
  items_count       INTEGER NOT NULL,
  err_arith         INTEGER NOT NULL DEFAULT 0,
  err_proc          INTEGER NOT NULL DEFAULT 0,
  err_concept       INTEGER NOT NULL DEFAULT 0,
  err_unclassified  INTEGER NOT NULL DEFAULT 0,
  no_response       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (attempt_id, skill_id)
);

CREATE TABLE recommendations (
  attempt_id   TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
  rank         INTEGER NOT NULL CHECK (rank >= 1),
  resource_id  TEXT NOT NULL REFERENCES resources(id),
  cosine       REAL NOT NULL,
  PRIMARY KEY (attempt_id, rank)
);

CREATE TABLE tutor_assignments (
  id          TEXT PRIMARY KEY,
  student_id  TEXT NOT NULL REFERENCES profiles(id),
  tutor_id    TEXT NOT NULL REFERENCES profiles(id),
  started_at  TEXT NOT NULL DEFAULT (datetime('now')),
  ended_at    TEXT,
  CHECK (ended_at IS NULL OR ended_at >= started_at)
);

CREATE UNIQUE INDEX one_active_tutor ON tutor_assignments (student_id) WHERE ended_at IS NULL;

CREATE TABLE tutor_ratings (
  id          TEXT PRIMARY KEY,
  student_id  TEXT NOT NULL REFERENCES profiles(id),
  tutor_id    TEXT NOT NULL REFERENCES profiles(id),
  retest_id   TEXT NOT NULL UNIQUE REFERENCES attempts(id),
  stars       INTEGER NOT NULL CHECK (stars BETWEEN 1 AND 5),
  comment     TEXT CHECK (length(comment) <= 500),
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE audit_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_id    TEXT REFERENCES profiles(id),
  action      TEXT NOT NULL,
  entity      TEXT NOT NULL,
  entity_id   TEXT,
  detail      TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE lesson_progress (
  student_id       TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  module_id        INTEGER NOT NULL REFERENCES modules(id),
  lesson_done      INTEGER NOT NULL DEFAULT 0 CHECK (lesson_done IN (0,1)),
  simulation_done  INTEGER NOT NULL DEFAULT 0 CHECK (simulation_done IN (0,1)),
  practice_done    INTEGER NOT NULL DEFAULT 0 CHECK (practice_done IN (0,1)),
  scenario_done    INTEGER NOT NULL DEFAULT 0 CHECK (scenario_done IN (0,1)),
  updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (student_id, module_id)
);

CREATE TABLE long_test_attempts (
  id                 TEXT PRIMARY KEY,
  student_id         TEXT NOT NULL REFERENCES profiles(id),
  status             TEXT NOT NULL DEFAULT 'in_progress'
                       CHECK (status IN ('in_progress','submitted')),
  scope              TEXT NOT NULL DEFAULT 'full'
                       CHECK (scope IN ('full','weak')),
  parent_attempt_id  TEXT REFERENCES long_test_attempts(id),
  skills_tested      TEXT,
  correct_count      INTEGER,
  administered_count INTEGER,
  started_at         TEXT NOT NULL DEFAULT (datetime('now')),
  submitted_at       TEXT
);

CREATE TABLE long_test_responses (
  attempt_id   TEXT NOT NULL REFERENCES long_test_attempts(id) ON DELETE CASCADE,
  item_id      TEXT NOT NULL REFERENCES items(id),
  chosen       TEXT CHECK (chosen IN ('A','B','C','D')),
  answered_at  TEXT,
  PRIMARY KEY (attempt_id, item_id)
);

CREATE TABLE session_notes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id  TEXT NOT NULL REFERENCES profiles(id),
  tutor_id    TEXT NOT NULL REFERENCES profiles(id),
  note        TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE assigned_retests (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id  TEXT NOT NULL REFERENCES profiles(id),
  tutor_id    TEXT NOT NULL REFERENCES profiles(id),
  module_id   INTEGER NOT NULL REFERENCES modules(id),
  status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done')),
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS expert_labels (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id    TEXT NOT NULL,
  item_id       TEXT NOT NULL,
  chosen        TEXT NOT NULL,
  expert_id     TEXT NOT NULL REFERENCES profiles(id),
  expert_label  TEXT NOT NULL CHECK (expert_label IN ('F', 'P', 'C', 'U')),
  system_label  TEXT NOT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(attempt_id, item_id)
);