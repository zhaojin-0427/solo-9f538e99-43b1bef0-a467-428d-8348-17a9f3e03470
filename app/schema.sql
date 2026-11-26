-- 影展评委派单 / 回避补位系统 SQLite 结构
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS works (
    code     TEXT PRIMARY KEY,          -- 作品编号
    title    TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0 -- 保留 JSON 中的顺序
);

CREATE TABLE IF NOT EXISTS judges (
    code     TEXT PRIMARY KEY,          -- 评委编号
    name     TEXT NOT NULL DEFAULT '',
    capacity INTEGER NOT NULL,          -- 接单上限（有效派单数上限）
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS recusals (
    judge_code TEXT NOT NULL,
    work_code  TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'manual', -- json | manual
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (judge_code, work_code),
    FOREIGN KEY (judge_code) REFERENCES judges(code),
    FOREIGN KEY (work_code)  REFERENCES works(code)
);

CREATE TABLE IF NOT EXISTS assignments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT, -- 派单号（每单只收一次分）
    work_code    TEXT NOT NULL,
    judge_code   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'voided')),
    score        REAL,                              -- NULL = 待评；交分后不可改
    submitted_at TEXT,
    voided_at    TEXT,
    void_reason  TEXT,                               -- recusal | replacement
    created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (work_code)  REFERENCES works(code),
    FOREIGN KEY (judge_code) REFERENCES judges(code)
);

-- 同一作品与同一评委至多有一张有效派单；作废后允许保留历史行
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_pair
    ON assignments(work_code, judge_code) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS ix_assign_judge ON assignments(judge_code, status);
CREATE INDEX IF NOT EXISTS ix_assign_work  ON assignments(work_code, status);

CREATE TABLE IF NOT EXISTS previews (
    token           TEXT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'confirmed', 'stale')),
    plan_json       TEXT NOT NULL,
    conflict_reason TEXT NOT NULL DEFAULT '',
    result_json     TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    confirmed_at    TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
