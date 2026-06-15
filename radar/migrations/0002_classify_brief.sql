-- 0002 — classify + brief. Adds pr_classifications and briefings tables.
-- Mirrors radar/db.py:_SCHEMA additions in the same order.

CREATE TABLE IF NOT EXISTS pr_classifications (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_id                    INTEGER NOT NULL REFERENCES prs(id),
    primary_category         TEXT,
    secondary_categories_json TEXT,
    novel_category_proposed  TEXT,
    technical_summary        TEXT,
    perf_numbers_json        TEXT,
    cross_references_json    TEXT,
    reasoning                TEXT NOT NULL,
    one_line_summary         TEXT,
    bot_or_chore             INTEGER NOT NULL DEFAULT 0,
    model                    TEXT NOT NULL,
    prompt_version           TEXT NOT NULL,
    classified_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_class_pr ON pr_classifications(pr_id, classified_at DESC);
CREATE INDEX IF NOT EXISTS idx_class_cat ON pr_classifications(primary_category);

CREATE TABLE IF NOT EXISTS briefings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    briefing_date TEXT NOT NULL UNIQUE,
    repo_scope    TEXT NOT NULL,
    script_json   TEXT NOT NULL,
    video_path    TEXT,
    video_url     TEXT,
    duration_s    INTEGER,
    built_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_brief_date ON briefings(briefing_date DESC);
