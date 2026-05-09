-- 0001 — initial schema. Mirrors radar/db.py:_SCHEMA exactly.
-- Run on fresh DBs implicitly via RadarDB.initialize().
-- This file is the canonical source for future migrations to diff against.

CREATE TABLE IF NOT EXISTS repos (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id      INTEGER NOT NULL REFERENCES repos(id),
    number       INTEGER NOT NULL,
    title        TEXT,
    body         TEXT,
    labels_json  TEXT,
    assignee     TEXT,
    state        TEXT,
    html_url     TEXT,
    created_at   TEXT,
    updated_at   TEXT,
    ingested_at  TEXT NOT NULL,
    raw_json     TEXT,
    UNIQUE(repo_id, number)
);
CREATE INDEX IF NOT EXISTS idx_issues_state ON issues(state, repo_id);
CREATE INDEX IF NOT EXISTS idx_issues_updated ON issues(updated_at DESC);

CREATE TABLE IF NOT EXISTS prs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id      INTEGER NOT NULL REFERENCES repos(id),
    number       INTEGER NOT NULL,
    title        TEXT,
    body         TEXT,
    labels_json  TEXT,
    state        TEXT,
    merged_at    TEXT,
    html_url     TEXT,
    created_at   TEXT,
    updated_at   TEXT,
    ingested_at  TEXT NOT NULL,
    raw_json     TEXT,
    UNIQUE(repo_id, number)
);
CREATE INDEX IF NOT EXISTS idx_prs_updated ON prs(updated_at DESC);

CREATE TABLE IF NOT EXISTS issue_evaluations (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id                 INTEGER NOT NULL REFERENCES issues(id),
    in_scope                 INTEGER NOT NULL,
    scope_bucket             TEXT,
    label_confirmed          INTEGER NOT NULL,
    evidence_quotes_json     TEXT,
    blackwell_intent_signal  TEXT,
    difficulty               INTEGER,
    why                      TEXT,
    model                    TEXT NOT NULL,
    prompt_version           TEXT NOT NULL,
    evaluated_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_issue ON issue_evaluations(issue_id, evaluated_at DESC);

CREATE TABLE IF NOT EXISTS notifications (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id            INTEGER NOT NULL REFERENCES issues(id),
    evaluation_id       INTEGER NOT NULL REFERENCES issue_evaluations(id),
    track               TEXT NOT NULL CHECK(track IN ('confirmed','speculative')),
    sent_at             TEXT NOT NULL,
    ntfy_response       TEXT,
    dismissed_correct   INTEGER,
    dismissed_reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_notif_issue_track ON notifications(issue_id, track);

CREATE TABLE IF NOT EXISTS cursors (
    repo_id              INTEGER NOT NULL REFERENCES repos(id),
    kind                 TEXT NOT NULL CHECK(kind IN ('issues','prs')),
    last_seen_updated_at TEXT,
    PRIMARY KEY (repo_id, kind)
);

CREATE VIRTUAL TABLE IF NOT EXISTS issues_fts USING fts5(
    title, body, content='issues', content_rowid='id'
);
CREATE VIRTUAL TABLE IF NOT EXISTS prs_fts USING fts5(
    title, body, content='prs', content_rowid='id'
);
