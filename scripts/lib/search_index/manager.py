"""Search index manager — build, sync, search, and content-cache the FTS5 email index.

All DB access is centralized through this class.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from .schema import DB_PATH, INSERT_EMAIL_SQL, init_database, sanitize_fts_query
from .disk import (
    find_mail_directory,
    scan_all_emails,
    get_disk_inventory,
    parse_emlx,
    infer_account_mailbox,
)
from .. import relative_time


class SearchIndexManager:
    """Manages the FTS5 search index for email content retrieval and search."""

    def __init__(self, db_path: Path | None = None):
        self._db_path = db_path or DB_PATH
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = init_database(self._db_path)
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def has_index(self) -> bool:
        return self._db_path.exists()

    # ------------------------------------------------------------------
    # Full build
    # ------------------------------------------------------------------

    def build_from_disk(self) -> dict:
        """Full index build from .emlx files on disk.
        Requires Full Disk Access for Terminal.
        """
        t0 = datetime.now()
        mail_dir = find_mail_directory()
        conn = self._get_conn()

        conn.execute("DELETE FROM emails")
        conn.execute("DELETE FROM sync_state")

        conn.execute("DROP TRIGGER IF EXISTS emails_ai")
        conn.execute("DROP TRIGGER IF EXISTS emails_ad")
        conn.execute("DROP TRIGGER IF EXISTS emails_au")

        total = 0
        mailbox_counts: dict[tuple[str, str], int] = {}
        batch: list[tuple] = []
        batch_size = 500

        try:
            for em in scan_all_emails(mail_dir):
                key = (em["account"], em["mailbox"])
                mailbox_counts[key] = mailbox_counts.get(key, 0) + 1

                batch.append((
                    em["id"],
                    em["account"],
                    em["mailbox"],
                    em.get("subject", ""),
                    em.get("sender", ""),
                    em.get("content", ""),
                    em.get("date_received", ""),
                    em.get("emlx_path", ""),
                    None,
                ))

                if len(batch) >= batch_size:
                    conn.executemany(INSERT_EMAIL_SQL, batch)
                    conn.commit()
                    total += len(batch)
                    batch = []

            if batch:
                conn.executemany(INSERT_EMAIL_SQL, batch)
                total += len(batch)

            now = datetime.now().isoformat()
            for (account, mailbox), count in mailbox_counts.items():
                conn.execute(
                    "INSERT OR REPLACE INTO sync_state (account, mailbox, last_sync, message_count) "
                    "VALUES (?, ?, ?, ?)",
                    (account, mailbox, now, count),
                )
            conn.commit()

            conn.execute("INSERT INTO emails_fts(emails_fts) VALUES('rebuild')")
            conn.execute("INSERT INTO emails_fts(emails_fts) VALUES('optimize')")
            conn.commit()
        finally:
            conn.executescript("""
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
            """)

        elapsed = (datetime.now() - t0).total_seconds()
        return {
            "indexed": total,
            "mailboxes": len(mailbox_counts),
            "elapsed_seconds": round(elapsed, 1),
        }

    # ------------------------------------------------------------------
    # Incremental sync
    # ------------------------------------------------------------------

    def sync_updates(self) -> dict:
        """Incremental sync: compare disk inventory vs DB, add new, remove deleted."""
        mail_dir = find_mail_directory()
        conn = self._get_conn()

        disk_inv = get_disk_inventory(mail_dir)
        cursor = conn.execute("SELECT account, mailbox, message_id FROM emails")
        db_inv = {(r[0], r[1], r[2]) for r in cursor}

        to_add = disk_inv - db_inv
        to_remove = db_inv - disk_inv

        removed = 0
        for account, mailbox, msg_id in to_remove:
            conn.execute(
                "DELETE FROM emails WHERE account=? AND mailbox=? AND message_id=?",
                (account, mailbox, msg_id),
            )
            removed += 1

        if removed:
            conn.commit()

        added = 0
        batch: list[tuple] = []
        for account, mailbox, msg_id in to_add:
            for emlx_path in mail_dir.rglob(f"{msg_id}.emlx"):
                inf_acc, inf_mb = infer_account_mailbox(emlx_path, mail_dir)
                if inf_acc == account and inf_mb == mailbox:
                    parsed = parse_emlx(emlx_path)
                    if parsed:
                        batch.append((
                            parsed["id"],
                            account,
                            mailbox,
                            parsed.get("subject", ""),
                            parsed.get("sender", ""),
                            parsed.get("content", ""),
                            parsed.get("date_received", ""),
                            str(emlx_path),
                            None,
                        ))
                    break

            if len(batch) >= 200:
                conn.executemany(INSERT_EMAIL_SQL, batch)
                conn.commit()
                added += len(batch)
                batch = []

        if batch:
            conn.executemany(INSERT_EMAIL_SQL, batch)
            conn.commit()
            added += len(batch)

        now = datetime.now().isoformat()
        conn.execute("UPDATE sync_state SET last_sync=? WHERE 1=1", (now,))
        conn.commit()

        return {"added": added, "removed": removed}

    # ------------------------------------------------------------------
    # message-id resolution hints
    # ------------------------------------------------------------------

    def upsert_hints(self, hints: list[tuple[str, int, str, str]]):
        """batch upsert (rfc_message_id, int_id, account, mailbox) location hints."""
        if not hints:
            return
        conn = self._get_conn()
        for rfc, mid, acc, mbox in hints:
            existing = conn.execute(
                "SELECT rowid FROM emails WHERE rfc_message_id = ?", (rfc,)
            ).fetchone()
            if existing:
                collision = conn.execute(
                    "SELECT 1 FROM emails WHERE account = ? AND mailbox = ? AND message_id = ? AND rowid != ?",
                    (acc, mbox, mid, existing[0]),
                ).fetchone()
                if not collision:
                    conn.execute(
                        "UPDATE emails SET message_id = ?, account = ?, mailbox = ? WHERE rowid = ?",
                        (mid, acc, mbox, existing[0]),
                    )
            else:
                backfilled = conn.execute(
                    "UPDATE emails SET rfc_message_id = ? WHERE message_id = ? AND account = ? AND mailbox = ? AND rfc_message_id IS NULL",
                    (rfc, mid, acc, mbox),
                ).rowcount
                if not backfilled:
                    conn.execute(
                        "INSERT OR IGNORE INTO emails (message_id, account, mailbox, rfc_message_id) VALUES (?,?,?,?)",
                        (mid, acc, mbox, rfc),
                    )
        conn.commit()

    def get_hint(self, rfc_message_id: str) -> dict | None:
        """lookup cached location for an rfc message-id."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT message_id, account, mailbox FROM emails WHERE rfc_message_id = ? LIMIT 1",
            (rfc_message_id,),
        ).fetchone()
        if row:
            return {"int_id": row[0], "account": row[1], "mailbox": row[2]}
        return None

    # ------------------------------------------------------------------
    # Content retrieval with ID-shift self-healing
    # ------------------------------------------------------------------

    def batch_content(self, msg_ids: list[int], messages: list[dict] | None = None) -> dict[int, str]:
        """Batch-query index for email content with ID-shift self-healing.

        Primary: lookup by message_id (fast, covers stable IDs).
        Secondary (for misses): match by (subject, date_received prefix) from messages list.
        Self-heal: UPDATE stored message_id so subsequent calls hit the primary path.
        """
        if not msg_ids:
            return {}

        conn = self._get_conn()
        result: dict[int, str] = {}

        ph = ",".join("?" * len(msg_ids))
        rows = conn.execute(
            f"SELECT message_id, content FROM emails WHERE message_id IN ({ph})",
            msg_ids,
        ).fetchall()

        for row in rows:
            if row[1]:
                result[row[0]] = row[1]

        if not messages:
            return result

        missed_ids = set(msg_ids) - set(result.keys())
        if not missed_ids:
            return result

        msg_lookup = {int(m["id"]): m for m in messages}
        healed = False

        for mid in missed_ids:
            msg = msg_lookup.get(mid)
            if not msg:
                continue
            subject = msg.get("subject", "")
            date = msg.get("date_received", "")
            if not subject or not date:
                continue

            date_prefix = date[:16] if len(date) >= 16 else date
            row = conn.execute(
                "SELECT rowid, message_id, content FROM emails "
                "WHERE subject = ? AND date_received LIKE ? || '%' AND message_id != ? "
                "LIMIT 1",
                (subject, date_prefix, mid),
            ).fetchone()

            if row and row[2]:
                result[mid] = row[2]
                collision = conn.execute(
                    "SELECT 1 FROM emails WHERE message_id = ? AND account = (SELECT account FROM emails WHERE rowid = ?) "
                    "AND mailbox = (SELECT mailbox FROM emails WHERE rowid = ?) AND rowid != ?",
                    (mid, row[0], row[0], row[0]),
                ).fetchone()
                if not collision:
                    conn.execute(
                        "UPDATE emails SET message_id = ? WHERE rowid = ?",
                        (mid, row[0]),
                    )
                    healed = True

        if healed:
            conn.commit()

        return result

    def targeted_index(self, msg_ids: set[int]) -> dict[int, str]:
        """Find specific .emlx files on disk by message ID, parse and insert content."""
        if not msg_ids:
            return {}

        try:
            mail_dir = find_mail_directory()
        except (FileNotFoundError, PermissionError):
            return {}

        conn = self._get_conn()
        found: dict[int, str] = {}

        for msg_id in msg_ids:
            for emlx_path in mail_dir.rglob(f"{msg_id}.emlx"):
                if ".partial.emlx" in emlx_path.name:
                    continue
                parsed = parse_emlx(emlx_path)
                if parsed and parsed.get("content"):
                    account, mailbox = infer_account_mailbox(emlx_path, mail_dir)
                    conn.execute(
                        INSERT_EMAIL_SQL,
                        (
                            parsed["id"],
                            account,
                            mailbox,
                            parsed.get("subject", ""),
                            parsed.get("sender", ""),
                            parsed.get("content", ""),
                            parsed.get("date_received", ""),
                            str(emlx_path),
                            None,
                        ),
                    )
                    found[msg_id] = parsed["content"]
                    break

        if found:
            conn.commit()

        return found

    def cache_content(
        self,
        message_id: int,
        subject: str,
        sender: str,
        content: str,
        date_received: str,
        account: str = "",
        mailbox: str = "",
        rfc_message_id: str = None,
    ):
        """Cache content for a single email with deduplication.

        Checks for existing entry with same (subject, date_received prefix) but
        different message_id. If found, updates the existing row (handles ID shift).
        Otherwise, INSERT OR REPLACE as normal.
        """
        if not content:
            return

        conn = self._get_conn()

        date_prefix = date_received[:16] if len(date_received) >= 16 else date_received
        existing = conn.execute(
            "SELECT rowid, message_id FROM emails "
            "WHERE subject = ? AND date_received LIKE ? || '%' AND message_id != ? "
            "LIMIT 1",
            (subject, date_prefix, message_id),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE emails SET message_id = ?, content = ?, sender = ?, rfc_message_id = COALESCE(?, rfc_message_id), "
                "indexed_at = datetime('now') WHERE rowid = ?",
                (message_id, content, sender, rfc_message_id, existing[0]),
            )
        else:
            conn.execute(
                INSERT_EMAIL_SQL,
                (message_id, account, mailbox, subject, sender, content, date_received, "", rfc_message_id),
            )

        conn.commit()

    # ------------------------------------------------------------------
    # Index metadata
    # ------------------------------------------------------------------

    def get_index_age(self) -> dict:
        """Return index age as dict with iso and relative timestamps."""
        conn = self._get_conn()
        row = conn.execute("SELECT MAX(indexed_at) FROM emails").fetchone()
        if not row or not row[0]:
            return {"iso": None, "relative": "no index"}

        iso = row[0]
        return {"iso": iso, "relative": relative_time(iso)}

    def maybe_prune(self, max_size_mb: float = 256):
        """Delete oldest 30% of entries if DB exceeds size threshold."""
        if not self._db_path.exists():
            return

        size_mb = self._db_path.stat().st_size / (1024 * 1024)
        if size_mb <= max_size_mb:
            return

        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
        if total == 0:
            return

        delete_count = int(total * 0.3)
        conn.execute(
            "DELETE FROM emails WHERE rowid IN "
            "(SELECT rowid FROM emails ORDER BY indexed_at ASC LIMIT ?)",
            (delete_count,),
        )
        conn.commit()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        account: str | None = None,
        mailbox: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """FTS5 search with BM25 ranking, returns list of result dicts."""
        safe_query = sanitize_fts_query(query)
        if not safe_query:
            return []

        conn = self._get_conn()

        sql = """
            SELECT e.message_id, e.account, e.mailbox, e.subject, e.sender,
                   e.content, e.date_received,
                   -bm25(emails_fts, 1.0, 0.5, 2.0) as score
            FROM emails_fts
            JOIN emails e ON emails_fts.rowid = e.rowid
            WHERE emails_fts MATCH ?
        """
        params: list = [safe_query]

        if account:
            sql += " AND e.account = ?"
            params.append(account)
        if mailbox:
            sql += " AND e.mailbox = ?"
            params.append(mailbox)

        sql += " ORDER BY score DESC LIMIT ?"
        params.append(limit)

        try:
            cursor = conn.execute(sql, params)
        except sqlite3.OperationalError as e:
            if "fts5: syntax error" in str(e).lower():
                escaped = '"' + query.replace('"', '""') + '"'
                return self.search(escaped, account, mailbox, limit)
            raise

        results = []
        for row in cursor:
            content = row[5] or ""
            snippet = " ".join(content.split())[:200] + ("..." if len(content) > 200 else "")
            results.append({
                "id": row[0],
                "account": row[1],
                "mailbox": row[2],
                "subject": row[3] or "",
                "sender": row[4] or "",
                "snippet": snippet,
                "date_received": row[6] or "",
                "score": round(row[7], 3),
            })
        return results

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        """Return index statistics."""
        conn = self._get_conn()
        email_count = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
        mailbox_count = conn.execute(
            "SELECT COUNT(DISTINCT account || '/' || mailbox) FROM emails"
        ).fetchone()[0]
        row = conn.execute("SELECT MAX(last_sync) FROM sync_state").fetchone()
        last_sync = row[0] if row else None

        db_size_mb = 0.0
        if self._db_path.exists():
            db_size_mb = round(self._db_path.stat().st_size / (1024 * 1024), 2)

        return {
            "email_count": email_count,
            "mailbox_count": mailbox_count,
            "last_sync": last_sync,
            "db_size_mb": db_size_mb,
        }
