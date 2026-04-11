"""SQLite schema for the FTS5 email search index."""

import os
import re
import sqlite3
from pathlib import Path

from .. import ASSETS_DIR

SCHEMA_VERSION = 4

DB_PATH = ASSETS_DIR / "index.db"
PROGRESS_PATH = ASSETS_DIR / "index-progress.json"
LOCK_PATH = ASSETS_DIR / "index-progress.lock"
LOGS_DIR = ASSETS_DIR / "logs"

FTS5_SPECIAL_CHARS = re.compile(r'(["\'\-\*\(\)\:\^])')

INSERT_EMAIL_SQL = """INSERT OR REPLACE INTO emails
    (message_id, account, mailbox, subject, sender, content, date_received, emlx_path, rfc_message_id)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"""

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);

CREATE TABLE IF NOT EXISTS emails (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    account TEXT NOT NULL,
    mailbox TEXT NOT NULL,
    subject TEXT,
    sender TEXT,
    content TEXT,
    date_received TEXT,
    emlx_path TEXT,
    rfc_message_id TEXT,
    indexed_at TEXT DEFAULT (datetime('now')),
    UNIQUE(account, mailbox, message_id)
);

CREATE INDEX IF NOT EXISTS idx_emails_account_mailbox ON emails(account, mailbox);
CREATE INDEX IF NOT EXISTS idx_emails_date ON emails(date_received DESC);
CREATE INDEX IF NOT EXISTS idx_emails_subject_date ON emails(subject, date_received);
CREATE INDEX IF NOT EXISTS idx_emails_rfc_mid ON emails(rfc_message_id);

CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts USING fts5(
    subject, sender, content,
    content='emails', content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS emails_ai AFTER INSERT ON emails BEGIN
    INSERT INTO emails_fts(rowid, subject, sender, content)
    VALUES (new.rowid, new.subject, new.sender, new.content);
END;
CREATE TRIGGER IF NOT EXISTS emails_ad AFTER DELETE ON emails BEGIN
    INSERT INTO emails_fts(emails_fts, rowid, subject, sender, content)
    VALUES('delete', old.rowid, old.subject, old.sender, old.content);
END;
CREATE TRIGGER IF NOT EXISTS emails_au AFTER UPDATE ON emails BEGIN
    INSERT INTO emails_fts(emails_fts, rowid, subject, sender, content)
    VALUES('delete', old.rowid, old.subject, old.sender, old.content);
    INSERT INTO emails_fts(rowid, subject, sender, content)
    VALUES (new.rowid, new.subject, new.sender, new.content);
END;

CREATE TABLE IF NOT EXISTS sync_state (
    account TEXT NOT NULL,
    mailbox TEXT NOT NULL,
    last_sync TEXT,
    message_count INTEGER DEFAULT 0,
    PRIMARY KEY(account, mailbox)
);
"""


def init_database(db_path: Path | None = None) -> sqlite3.Connection:
    """Create and initialize the index database, returns open connection."""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()

    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")

    if is_new:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    )
    if cursor.fetchone() is None:
        conn.executescript(_SCHEMA_SQL)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
    else:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row and row[0] < SCHEMA_VERSION:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_emails_subject_date ON emails(subject, date_received)"
            )
            if row[0] < 4:
                try:
                    conn.execute("ALTER TABLE emails ADD COLUMN rfc_message_id TEXT")
                except sqlite3.OperationalError:
                    pass  # column already exists
                conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_rfc_mid ON emails(rfc_message_id)")
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            conn.commit()

    return conn


def sanitize_fts_query(query: str) -> str:
    """Escape special FTS5 characters for safe use."""
    if not query or not query.strip():
        return ""
    return FTS5_SPECIAL_CHARS.sub(r"\\\1", query.strip())
