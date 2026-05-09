"""Async SQLite storage for Inference Radar.

Conventions match ~/wsl_git/workday_connector/common/src/job_scraper_common/storage.py:
WAL, busy_timeout, row_factory=Row, schema in module-level _SCHEMA, migrations
applied via PRAGMA table_info inspection.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

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
        await self._db.commit()

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
            "ON CONFLICT(repo_id, kind) DO UPDATE SET last_seen_updated_at=excluded.last_seen_updated_at",
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
