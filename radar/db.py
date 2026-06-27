"""Async SQLite storage for Inference Radar.

Conventions match ~/wsl_git/workday_connector/common/src/job_scraper_common/storage.py:
WAL, busy_timeout, row_factory=Row, schema in module-level _SCHEMA, migrations
applied via PRAGMA table_info inspection.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

# GitHub's auto-close keywords. A PR body that says any of these followed by
# `#N` will auto-close issue N on merge. We use this to suppress alerts on
# issues that already have a PR sitting in review.
PR_CLOSES_ISSUE_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)[\s:]+#(\d+)\b",
    re.IGNORECASE,
)

_SCHEMA = """
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

CREATE TRIGGER IF NOT EXISTS issues_ai AFTER INSERT ON issues BEGIN
    INSERT INTO issues_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS issues_ad AFTER DELETE ON issues BEGIN
    INSERT INTO issues_fts(issues_fts, rowid, title, body)
        VALUES('delete', old.id, old.title, old.body);
END;
CREATE TRIGGER IF NOT EXISTS issues_au AFTER UPDATE ON issues BEGIN
    INSERT INTO issues_fts(issues_fts, rowid, title, body)
        VALUES('delete', old.id, old.title, old.body);
    INSERT INTO issues_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS prs_fts USING fts5(
    title, body, content='prs', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS prs_ai AFTER INSERT ON prs BEGIN
    INSERT INTO prs_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS prs_ad AFTER DELETE ON prs BEGIN
    INSERT INTO prs_fts(prs_fts, rowid, title, body)
        VALUES('delete', old.id, old.title, old.body);
END;
CREATE TRIGGER IF NOT EXISTS prs_au AFTER UPDATE ON prs BEGIN
    INSERT INTO prs_fts(prs_fts, rowid, title, body)
        VALUES('delete', old.id, old.title, old.body);
    INSERT INTO prs_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

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

CREATE TABLE IF NOT EXISTS issue_alert_evaluations (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id             INTEGER NOT NULL REFERENCES issues(id),
    track                TEXT NOT NULL,
    in_scope             INTEGER NOT NULL,
    relevance            INTEGER,
    evidence_quotes_json TEXT,
    why                  TEXT,
    model                TEXT NOT NULL,
    prompt_version       TEXT NOT NULL,
    evaluated_at         TEXT NOT NULL,
    UNIQUE(issue_id, track, prompt_version, model)
);
CREATE INDEX IF NOT EXISTS idx_issue_alert_eval_lookup
    ON issue_alert_evaluations(issue_id, track, prompt_version);

CREATE TABLE IF NOT EXISTS issue_alert_notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id        INTEGER NOT NULL REFERENCES issues(id),
    track           TEXT NOT NULL,
    sent_at         TEXT NOT NULL,
    ntfy_response   TEXT,
    UNIQUE(issue_id, track)
);
CREATE INDEX IF NOT EXISTS idx_issue_alert_notif_track
    ON issue_alert_notifications(track, sent_at DESC);

CREATE TABLE IF NOT EXISTS pr_alert_evaluations (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_id                INTEGER NOT NULL REFERENCES prs(id),
    track                TEXT NOT NULL,
    in_scope             INTEGER NOT NULL,
    relevance            INTEGER,
    evidence_quotes_json TEXT,
    why                  TEXT,
    model                TEXT NOT NULL,
    prompt_version       TEXT NOT NULL,
    evaluated_at         TEXT NOT NULL,
    UNIQUE(pr_id, track, prompt_version, model)
);
CREATE INDEX IF NOT EXISTS idx_pr_alert_eval_lookup
    ON pr_alert_evaluations(pr_id, track, prompt_version);

CREATE TABLE IF NOT EXISTS pr_notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_id           INTEGER NOT NULL REFERENCES prs(id),
    track           TEXT NOT NULL,
    sent_at         TEXT NOT NULL,
    ntfy_response   TEXT,
    UNIQUE(pr_id, track)
);
CREATE INDEX IF NOT EXISTS idx_pr_notif_track
    ON pr_notifications(track, sent_at DESC);

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
"""

# Analytics layer — populated by comments.py, enrich.py, papers.py.
# Kept separate from _SCHEMA so existing tables are never touched.
_ANALYTICS_SCHEMA = """
CREATE TABLE IF NOT EXISTS pr_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_id       INTEGER NOT NULL REFERENCES prs(id),
    comment_id  INTEGER NOT NULL,
    author_login TEXT,
    body        TEXT,
    created_at  TEXT,
    source      TEXT NOT NULL,   -- 'issue_comment' | 'review'
    UNIQUE(pr_id, comment_id, source)
);
CREATE INDEX IF NOT EXISTS idx_prc_pr ON pr_comments(pr_id);
CREATE INDEX IF NOT EXISTS idx_prc_author ON pr_comments(author_login);

CREATE TABLE IF NOT EXISTS pr_mentions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_id           INTEGER NOT NULL REFERENCES prs(id),
    mentioned_login TEXT NOT NULL,
    source          TEXT NOT NULL,   -- 'body' | 'comment'
    UNIQUE(pr_id, mentioned_login, source)
);
CREATE INDEX IF NOT EXISTS idx_prm_login ON pr_mentions(mentioned_login);
CREATE INDEX IF NOT EXISTS idx_prm_pr    ON pr_mentions(pr_id);

CREATE TABLE IF NOT EXISTS contributor_orgs (
    login        TEXT PRIMARY KEY,
    org          TEXT,
    org_source   TEXT,   -- 'github_company' | 'bio_keyword' | 'manual'
    company_raw  TEXT,
    bio_snippet  TEXT,
    refreshed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pr_review_signal (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_id           INTEGER NOT NULL REFERENCES prs(id) UNIQUE,
    stall_reason    TEXT,
    reviewer_stance TEXT,
    newbie_viable   INTEGER NOT NULL DEFAULT 0,
    one_line_reason TEXT,
    model           TEXT,
    prompt_version  TEXT,
    classified_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prs_newbie ON pr_review_signal(newbie_viable, stall_reason);

CREATE TABLE IF NOT EXISTS paper_signals (
    paper_id          TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    published_date    TEXT,
    keyword_buckets   TEXT,   -- JSON array of matched bucket names
    abstract_snippet  TEXT,
    hf_url            TEXT,
    arxiv_id          TEXT,
    vllm_pr_appeared  TEXT,   -- date when a matching vLLM PR first appeared
    ingested_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ps_date ON paper_signals(published_date DESC);

CREATE TABLE IF NOT EXISTS keyword_first_seen (
    bucket      TEXT    NOT NULL,
    repo_id     INTEGER NOT NULL REFERENCES repos(id),
    first_pr_id INTEGER REFERENCES prs(id),
    first_seen  TEXT    NOT NULL,
    PRIMARY KEY (bucket, repo_id)
);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value)


def loads(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


class RadarDB:
    """Async SQLite storage for Inference Radar.

    Open with `async with RadarDB(path) as db: ...` or call `await db.initialize()`
    explicitly. Connection holds WAL mode and busy_timeout for the session.
    """

    def __init__(self, db_path: str | Path = "data/radar.db") -> None:
        self._db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None

    async def __aenter__(self) -> RadarDB:
        await self.initialize()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def initialize(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(_SCHEMA)
        await self._db.executescript(_ANALYTICS_SCHEMA)
        await self._analytics_migrate()
        await self._db.commit()

    async def _analytics_migrate(self) -> None:
        """Add analytics columns to prs; safe to call on existing DBs."""
        for sql in [
            "ALTER TABLE prs ADD COLUMN keyword_bucket TEXT",
            "ALTER TABLE prs ADD COLUMN keyword_secondary_json TEXT",
            "ALTER TABLE prs ADD COLUMN author_login TEXT",
            "ALTER TABLE prs ADD COLUMN comments_fetched_at TEXT",
        ]:
            try:
                await self.conn.execute(sql)
            except Exception:
                pass  # column already exists

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("RadarDB not initialized; call await db.initialize() first")
        return self._db

    # --- repos ---

    async def upsert_repo(self, slug: str, name: str) -> int:
        await self.conn.execute(
            "INSERT INTO repos (slug, name) VALUES (?, ?) "
            "ON CONFLICT(slug) DO UPDATE SET name=excluded.name",
            (slug, name),
        )
        await self.conn.commit()
        async with self.conn.execute("SELECT id FROM repos WHERE slug=?", (slug,)) as cur:
            row = await cur.fetchone()
            assert row is not None
            return int(row["id"])

    async def list_repos(self) -> list[aiosqlite.Row]:
        async with self.conn.execute("SELECT * FROM repos ORDER BY id") as cur:
            return list(await cur.fetchall())

    # --- cursors ---

    async def get_cursor(self, repo_id: int, kind: str) -> str | None:
        async with self.conn.execute(
            "SELECT last_seen_updated_at FROM cursors WHERE repo_id=? AND kind=?",
            (repo_id, kind),
        ) as cur:
            row = await cur.fetchone()
            return None if row is None else row["last_seen_updated_at"]

    async def set_cursor(self, repo_id: int, kind: str, ts: str) -> None:
        await self.conn.execute(
            "INSERT INTO cursors (repo_id, kind, last_seen_updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(repo_id, kind) DO UPDATE SET "
            "last_seen_updated_at=excluded.last_seen_updated_at",
            (repo_id, kind, ts),
        )
        await self.conn.commit()

    # --- issues / prs upsert ---

    async def upsert_issue(self, repo_id: int, payload: dict[str, Any]) -> int:
        labels = [lbl["name"] for lbl in payload.get("labels", [])]
        assignee = (payload.get("assignee") or {}).get("login")
        await self.conn.execute(
            """INSERT INTO issues (repo_id, number, title, body, labels_json, assignee, state,
                html_url, created_at, updated_at, ingested_at, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(repo_id, number) DO UPDATE SET
                   title=excluded.title, body=excluded.body, labels_json=excluded.labels_json,
                   assignee=excluded.assignee, state=excluded.state, html_url=excluded.html_url,
                   updated_at=excluded.updated_at, ingested_at=excluded.ingested_at,
                   raw_json=excluded.raw_json""",
            (
                repo_id, int(payload["number"]),
                payload.get("title"), payload.get("body"),
                dumps(labels), assignee, payload.get("state"),
                payload.get("html_url"),
                payload.get("created_at"), payload.get("updated_at"),
                now_iso(), dumps(payload),
            ),
        )
        await self.conn.commit()
        async with self.conn.execute(
            "SELECT id FROM issues WHERE repo_id=? AND number=?", (repo_id, int(payload["number"])),
        ) as cur:
            row = await cur.fetchone()
            assert row is not None
            return int(row["id"])

    async def upsert_pr(self, repo_id: int, payload: dict[str, Any]) -> int:
        labels = [lbl["name"] for lbl in payload.get("labels", [])]
        await self.conn.execute(
            """INSERT INTO prs (repo_id, number, title, body, labels_json, state, merged_at,
                html_url, created_at, updated_at, ingested_at, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(repo_id, number) DO UPDATE SET
                   title=excluded.title, body=excluded.body, labels_json=excluded.labels_json,
                   state=excluded.state, merged_at=excluded.merged_at, html_url=excluded.html_url,
                   updated_at=excluded.updated_at, ingested_at=excluded.ingested_at,
                   raw_json=excluded.raw_json""",
            (
                repo_id, int(payload["number"]),
                payload.get("title"), payload.get("body"),
                dumps(labels), payload.get("state"), payload.get("merged_at"),
                payload.get("html_url"),
                payload.get("created_at"), payload.get("updated_at"),
                now_iso(), dumps(payload),
            ),
        )
        await self.conn.commit()
        async with self.conn.execute(
            "SELECT id FROM prs WHERE repo_id=? AND number=?", (repo_id, int(payload["number"])),
        ) as cur:
            row = await cur.fetchone()
            assert row is not None
            return int(row["id"])

    # --- evaluations ---

    async def latest_evaluation(self, issue_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT * FROM issue_evaluations WHERE issue_id=? ORDER BY evaluated_at DESC LIMIT 1",
            (issue_id,),
        ) as cur:
            return await cur.fetchone()

    async def insert_evaluation(
        self,
        *,
        issue_id: int,
        in_scope: bool,
        scope_bucket: str | None,
        label_confirmed: bool,
        evidence_quotes: list[str] | None,
        blackwell_intent_signal: str | None,
        difficulty: int | None,
        why: str | None,
        model: str,
        prompt_version: str,
    ) -> int:
        cur = await self.conn.execute(
            """INSERT INTO issue_evaluations
               (issue_id, in_scope, scope_bucket, label_confirmed, evidence_quotes_json,
                blackwell_intent_signal, difficulty, why, model, prompt_version, evaluated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                issue_id, 1 if in_scope else 0, scope_bucket, 1 if label_confirmed else 0,
                dumps(evidence_quotes), blackwell_intent_signal, difficulty, why,
                model, prompt_version, now_iso(),
            ),
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    # --- notifications ---

    async def has_notification(self, issue_id: int, track: str) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM notifications WHERE issue_id=? AND track=? LIMIT 1",
            (issue_id, track),
        ) as cur:
            return await cur.fetchone() is not None

    async def insert_notification(
        self, *, issue_id: int, evaluation_id: int, track: str, ntfy_response: str | None,
    ) -> int:
        cur = await self.conn.execute(
            """INSERT INTO notifications
               (issue_id, evaluation_id, track, sent_at, ntfy_response)
               VALUES (?, ?, ?, ?, ?)""",
            (issue_id, evaluation_id, track, now_iso(), ntfy_response),
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    # --- classifications ---

    async def latest_classification(self, pr_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT * FROM pr_classifications WHERE pr_id=? ORDER BY classified_at DESC LIMIT 1",
            (pr_id,),
        ) as cur:
            return await cur.fetchone()

    async def insert_classification(
        self,
        *,
        pr_id: int,
        primary_category: str | None,
        secondary_categories: list[str] | None,
        novel_category_proposed: str | None,
        technical_summary: str | None,
        perf_numbers: list[dict[str, Any]] | None,
        cross_references: list[dict[str, Any]] | None,
        reasoning: str,
        one_line_summary: str | None,
        bot_or_chore: bool,
        model: str,
        prompt_version: str,
    ) -> int:
        cur = await self.conn.execute(
            """INSERT INTO pr_classifications
               (pr_id, primary_category, secondary_categories_json, novel_category_proposed,
                technical_summary, perf_numbers_json, cross_references_json,
                reasoning, one_line_summary, bot_or_chore, model, prompt_version, classified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pr_id, primary_category, dumps(secondary_categories), novel_category_proposed,
                technical_summary, dumps(perf_numbers), dumps(cross_references),
                reasoning, one_line_summary, 1 if bot_or_chore else 0,
                model, prompt_version, now_iso(),
            ),
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    # --- pr-link suppression (shared by firsts + issue_alerts) ---

    async def fetch_open_pr_linked_issues(
        self, repo_id: int,
    ) -> dict[int, list[int]]:
        """For one repo, scan every open PR's body for GitHub's auto-close
        keywords (`closes #N`, `fixes: #N`, `resolved #N`, …) and return a map
        `{issue_number: [pr_number, ...]}`. Caller uses the keys to suppress
        alerts on issues that already have a PR sitting in review.

        Empty dict if no open PR links any issue.
        """
        async with self.conn.execute(
            "SELECT number, body FROM prs "
            "WHERE repo_id=? AND state='open' AND body IS NOT NULL",
            (repo_id,),
        ) as cur:
            rows = await cur.fetchall()
        linked: dict[int, list[int]] = {}
        for r in rows:
            try:
                pr_num = int(r["number"])
            except (TypeError, ValueError):
                continue
            for m in PR_CLOSES_ISSUE_RE.findall(r["body"] or ""):
                try:
                    issue_num = int(m)
                except ValueError:
                    continue
                if issue_num == pr_num:
                    continue  # PR referring to itself; nonsensical, skip
                linked.setdefault(issue_num, []).append(pr_num)
        return linked

    # --- issue alerts (separate from `notifications` which has CHECK on track) ---

    async def fetch_open_unassigned_issues(self, repo_id: int) -> list[aiosqlite.Row]:
        """Open + unassigned issues for one repo, oldest-first."""
        async with self.conn.execute(
            """SELECT id, repo_id, number, title, body, labels_json,
                       assignee, state, html_url, created_at, updated_at
                  FROM issues
                 WHERE repo_id = ?
                   AND state = 'open'
                   AND (assignee IS NULL OR assignee = '')
              ORDER BY created_at ASC""",
            (repo_id,),
        ) as cur:
            return list(await cur.fetchall())

    async def get_issue_alert_eval(
        self, issue_id: int, track: str, prompt_version: str,
    ) -> aiosqlite.Row | None:
        async with self.conn.execute(
            """SELECT * FROM issue_alert_evaluations
                WHERE issue_id=? AND track=? AND prompt_version=?
                ORDER BY evaluated_at DESC LIMIT 1""",
            (issue_id, track, prompt_version),
        ) as cur:
            return await cur.fetchone()

    async def insert_issue_alert_eval(
        self,
        *,
        issue_id: int,
        track: str,
        in_scope: bool,
        relevance: int | None,
        evidence_quotes: list[str] | None,
        why: str | None,
        model: str,
        prompt_version: str,
    ) -> int:
        cur = await self.conn.execute(
            """INSERT OR IGNORE INTO issue_alert_evaluations
               (issue_id, track, in_scope, relevance, evidence_quotes_json,
                why, model, prompt_version, evaluated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                issue_id, track, 1 if in_scope else 0, relevance,
                dumps(evidence_quotes), why, model, prompt_version, now_iso(),
            ),
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    async def has_issue_alert_notification(self, issue_id: int, track: str) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM issue_alert_notifications WHERE issue_id=? AND track=? LIMIT 1",
            (issue_id, track),
        ) as cur:
            return await cur.fetchone() is not None

    async def insert_issue_alert_notification(
        self, *, issue_id: int, track: str, ntfy_response: str | None,
    ) -> int:
        cur = await self.conn.execute(
            """INSERT OR IGNORE INTO issue_alert_notifications
               (issue_id, track, sent_at, ntfy_response)
               VALUES (?, ?, ?, ?)""",
            (issue_id, track, now_iso(), ntfy_response),
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    # --- pr alerts ---

    async def fetch_recent_open_unassigned_prs(
        self, repo_id: int, since_days: int,
    ) -> list[aiosqlite.Row]:
        """Open PRs in this repo, no assignee, created within the past N days.

        Uses raw_json.assignee since the prs table doesn't have an assignee column.
        Sorted oldest-first so the alert queue drains FIFO.
        """
        async with self.conn.execute(
            f"""SELECT id, repo_id, number, title, body, labels_json, state,
                       html_url, created_at, updated_at, raw_json
                  FROM prs
                 WHERE repo_id = ?
                   AND state = 'open'
                   AND json_extract(raw_json, '$.assignee') IS NULL
                   AND created_at >= datetime('now', '-{int(since_days)} days')
              ORDER BY created_at ASC""",
            (repo_id,),
        ) as cur:
            return list(await cur.fetchall())

    async def get_pr_alert_eval(
        self, pr_id: int, track: str, prompt_version: str,
    ) -> aiosqlite.Row | None:
        async with self.conn.execute(
            """SELECT * FROM pr_alert_evaluations
                WHERE pr_id=? AND track=? AND prompt_version=?
                ORDER BY evaluated_at DESC LIMIT 1""",
            (pr_id, track, prompt_version),
        ) as cur:
            return await cur.fetchone()

    async def insert_pr_alert_eval(
        self,
        *,
        pr_id: int,
        track: str,
        in_scope: bool,
        relevance: int | None,
        evidence_quotes: list[str] | None,
        why: str | None,
        model: str,
        prompt_version: str,
    ) -> int:
        cur = await self.conn.execute(
            """INSERT OR IGNORE INTO pr_alert_evaluations
               (pr_id, track, in_scope, relevance, evidence_quotes_json,
                why, model, prompt_version, evaluated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pr_id, track, 1 if in_scope else 0, relevance,
                dumps(evidence_quotes), why, model, prompt_version, now_iso(),
            ),
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    async def has_pr_notification(self, pr_id: int, track: str) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM pr_notifications WHERE pr_id=? AND track=? LIMIT 1",
            (pr_id, track),
        ) as cur:
            return await cur.fetchone() is not None

    async def insert_pr_notification(
        self, *, pr_id: int, track: str, ntfy_response: str | None,
    ) -> int:
        cur = await self.conn.execute(
            """INSERT OR IGNORE INTO pr_notifications
               (pr_id, track, sent_at, ntfy_response)
               VALUES (?, ?, ?, ?)""",
            (pr_id, track, now_iso(), ntfy_response),
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    # --- analytics: pr_comments ---

    async def fetch_prs_needing_comments(self, max_prs: int = 100) -> list[aiosqlite.Row]:
        """PRs that have never had their comments fetched. Open first, then recent."""
        async with self.conn.execute(
            """SELECT p.id, p.number, p.state, r.slug AS repo_slug
               FROM prs p JOIN repos r ON r.id = p.repo_id
               WHERE p.comments_fetched_at IS NULL
                 AND p.created_at > date('now', '-180 days')
               ORDER BY CASE WHEN p.state='open' THEN 0 ELSE 1 END, p.updated_at DESC
               LIMIT ?""",
            (max_prs,),
        ) as cur:
            return list(await cur.fetchall())

    async def upsert_pr_comment(
        self, *, pr_id: int, comment_id: int, author_login: str | None,
        body: str | None, created_at: str | None, source: str,
    ) -> None:
        await self.conn.execute(
            """INSERT INTO pr_comments (pr_id, comment_id, author_login, body, created_at, source)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(pr_id, comment_id, source) DO UPDATE SET
                   author_login=excluded.author_login, body=excluded.body""",
            (pr_id, comment_id, author_login, body, created_at, source),
        )

    async def mark_comments_fetched(self, pr_id: int) -> None:
        await self.conn.execute(
            "UPDATE prs SET comments_fetched_at=? WHERE id=?", (now_iso(), pr_id),
        )
        await self.conn.commit()

    async def fetch_pr_comments(self, pr_id: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            """SELECT author_login, body, created_at, source
               FROM pr_comments WHERE pr_id=? ORDER BY created_at ASC""",
            (pr_id,),
        ) as cur:
            return list(await cur.fetchall())

    # --- analytics: pr_mentions ---

    async def upsert_pr_mention(
        self, *, pr_id: int, mentioned_login: str, source: str,
    ) -> None:
        await self.conn.execute(
            """INSERT OR IGNORE INTO pr_mentions (pr_id, mentioned_login, source)
               VALUES (?, ?, ?)""",
            (pr_id, mentioned_login, source),
        )

    # --- analytics: contributor_orgs ---

    async def get_contributor_org(self, login: str) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT * FROM contributor_orgs WHERE login=?", (login,),
        ) as cur:
            return await cur.fetchone()

    async def upsert_contributor_org(
        self, *, login: str, org: str | None, org_source: str,
        company_raw: str | None, bio_snippet: str | None,
    ) -> None:
        await self.conn.execute(
            """INSERT INTO contributor_orgs
               (login, org, org_source, company_raw, bio_snippet, refreshed_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(login) DO UPDATE SET
                   org=excluded.org, org_source=excluded.org_source,
                   company_raw=excluded.company_raw, bio_snippet=excluded.bio_snippet,
                   refreshed_at=excluded.refreshed_at""",
            (login, org, org_source, company_raw, bio_snippet, now_iso()),
        )

    async def fetch_logins_needing_org_lookup(self, max_logins: int = 300) -> list[str]:
        """Unique author logins from prs not yet in contributor_orgs (or stale >90d)."""
        async with self.conn.execute(
            """SELECT DISTINCT json_extract(p.raw_json, '$.user.login') AS login
               FROM prs p
               WHERE json_extract(p.raw_json, '$.user.login') IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM contributor_orgs co
                     WHERE co.login = json_extract(p.raw_json, '$.user.login')
                       AND co.refreshed_at > date('now', '-90 days')
                 )
               LIMIT ?""",
            (max_logins,),
        ) as cur:
            rows = await cur.fetchall()
        return [r["login"] for r in rows if r["login"]]

    # --- analytics: keyword_bucket on prs ---

    async def fetch_prs_needing_keyword_bucket(self, limit: int = 2000) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            """SELECT id, title, body FROM prs
               WHERE keyword_bucket IS NULL
               ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        ) as cur:
            return list(await cur.fetchall())

    async def update_pr_keyword_bucket(
        self, pr_id: int, primary: str | None, secondary: list[str],
        author_login: str | None,
    ) -> None:
        await self.conn.execute(
            """UPDATE prs
               SET keyword_bucket=?, keyword_secondary_json=?,
                   author_login=COALESCE(author_login, ?)
               WHERE id=?""",
            (primary, dumps(secondary) if secondary else None, author_login, pr_id),
        )

    # --- analytics: pr_review_signal ---

    async def get_pr_review_signal(self, pr_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT * FROM pr_review_signal WHERE pr_id=?", (pr_id,),
        ) as cur:
            return await cur.fetchone()

    async def upsert_pr_review_signal(
        self, *, pr_id: int, stall_reason: str, reviewer_stance: str,
        newbie_viable: bool, one_line_reason: str, model: str, prompt_version: str,
    ) -> None:
        await self.conn.execute(
            """INSERT INTO pr_review_signal
               (pr_id, stall_reason, reviewer_stance, newbie_viable,
                one_line_reason, model, prompt_version, classified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(pr_id) DO UPDATE SET
                   stall_reason=excluded.stall_reason,
                   reviewer_stance=excluded.reviewer_stance,
                   newbie_viable=excluded.newbie_viable,
                   one_line_reason=excluded.one_line_reason,
                   model=excluded.model, prompt_version=excluded.prompt_version,
                   classified_at=excluded.classified_at""",
            (pr_id, stall_reason, reviewer_stance, 1 if newbie_viable else 0,
             one_line_reason, model, prompt_version, now_iso()),
        )
        await self.conn.commit()

    async def fetch_prs_needing_review_signal(self, limit: int = 20) -> list[aiosqlite.Row]:
        """Open PRs that have comments but no review signal yet."""
        async with self.conn.execute(
            """SELECT p.id, p.title, p.state, r.slug AS repo_slug
               FROM prs p JOIN repos r ON r.id = p.repo_id
               WHERE p.state = 'open'
                 AND p.comments_fetched_at IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM pr_review_signal rs WHERE rs.pr_id = p.id
                 )
                 AND EXISTS (
                     SELECT 1 FROM pr_comments pc WHERE pc.pr_id = p.id LIMIT 1
                 )
               ORDER BY p.updated_at DESC LIMIT ?""",
            (limit,),
        ) as cur:
            return list(await cur.fetchall())

    # --- analytics: paper_signals ---

    async def upsert_paper_signal(
        self, *, paper_id: str, title: str, published_date: str | None,
        keyword_buckets: list[str], abstract_snippet: str | None,
        hf_url: str | None, arxiv_id: str | None,
    ) -> None:
        await self.conn.execute(
            """INSERT INTO paper_signals
               (paper_id, title, published_date, keyword_buckets,
                abstract_snippet, hf_url, arxiv_id, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(paper_id) DO UPDATE SET
                   keyword_buckets=excluded.keyword_buckets,
                   abstract_snippet=excluded.abstract_snippet,
                   ingested_at=excluded.ingested_at""",
            (paper_id, title, published_date, dumps(keyword_buckets),
             abstract_snippet, hf_url, arxiv_id, now_iso()),
        )
        await self.conn.commit()

    async def mark_paper_vllm_appeared(self, paper_id: str, appeared_date: str) -> None:
        await self.conn.execute(
            "UPDATE paper_signals SET vllm_pr_appeared=? WHERE paper_id=?",
            (appeared_date, paper_id),
        )
        await self.conn.commit()

    # --- analytics: keyword_first_seen ---

    async def upsert_keyword_first_seen(
        self, *, bucket: str, repo_id: int, first_pr_id: int, first_seen: str,
    ) -> None:
        await self.conn.execute(
            """INSERT OR IGNORE INTO keyword_first_seen
               (bucket, repo_id, first_pr_id, first_seen)
               VALUES (?, ?, ?, ?)""",
            (bucket, repo_id, first_pr_id, first_seen),
        )
        await self.conn.commit()

    async def fetch_bucket_first_seen(
        self, bucket: str,
    ) -> list[aiosqlite.Row]:
        """For a bucket, return one row per repo with first_seen date."""
        async with self.conn.execute(
            """SELECT kfs.bucket, kfs.first_seen, kfs.first_pr_id, r.slug AS repo_slug
               FROM keyword_first_seen kfs JOIN repos r ON r.id = kfs.repo_id
               WHERE kfs.bucket=?
               ORDER BY kfs.first_seen ASC""",
            (bucket,),
        ) as cur:
            return list(await cur.fetchall())

    # --- briefings ---

    async def get_briefing(self, briefing_date: str) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT * FROM briefings WHERE briefing_date=?", (briefing_date,),
        ) as cur:
            return await cur.fetchone()

    async def upsert_briefing(
        self,
        *,
        briefing_date: str,
        repo_scope: list[str],
        script: dict[str, Any],
        video_path: str | None,
        video_url: str | None,
        duration_s: int | None,
    ) -> int:
        await self.conn.execute(
            """INSERT INTO briefings
               (briefing_date, repo_scope, script_json, video_path, video_url, duration_s, built_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(briefing_date) DO UPDATE SET
                   repo_scope=excluded.repo_scope,
                   script_json=excluded.script_json,
                   video_path=COALESCE(excluded.video_path, briefings.video_path),
                   video_url=COALESCE(excluded.video_url, briefings.video_url),
                   duration_s=COALESCE(excluded.duration_s, briefings.duration_s),
                   built_at=excluded.built_at""",
            (
                briefing_date, dumps(repo_scope), dumps(script),
                video_path, video_url, duration_s, now_iso(),
            ),
        )
        await self.conn.commit()
        async with self.conn.execute(
            "SELECT id FROM briefings WHERE briefing_date=?", (briefing_date,),
        ) as cur:
            row = await cur.fetchone()
            assert row is not None
            return int(row["id"])
