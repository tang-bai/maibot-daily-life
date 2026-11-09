"""SQLite-backed claims, durable deduplication, and generation attempt accounting.

All timestamps are caller-supplied Unix seconds. Every operation opens its own
connection; a Store may safely be used by multiple threads or processes.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterator
from uuid import uuid4


STATES = frozenset({"generating", "draft", "submitting", "success", "failed", "skipped", "uncertain"})
ACTIVE_STATES = frozenset({"generating", "submitting"})
UPDATE_FIELDS = frozenset({"state", "caption", "image_path", "tid", "error"})


class ClaimError(Exception):
    """A rejected reservation, with a short, credential-free reason code."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection(write=True) as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    activity_key TEXT NOT NULL UNIQUE,
                    snapshot TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN
                        ('generating','draft','submitting','success','failed','skipped','uncertain')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    day TEXT NOT NULL,
                    caption TEXT,
                    image_path TEXT,
                    tid TEXT,
                    error TEXT,
                    lease_until REAL,
                    submission_started INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0
                )
            """)
            # Existing databases predate explicit submission/retry tracking.
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
            for name in ("submission_started", "retry_count"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS seen (
                    activity_key TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS attempt_ledger (
                    task_id TEXT PRIMARY KEY,
                    day TEXT NOT NULL,
                    attempted_at REAL NOT NULL
                )
            """)
            connection.execute("CREATE TABLE IF NOT EXISTS auto_seen (activity_key TEXT PRIMARY KEY)")
            connection.execute("CREATE INDEX IF NOT EXISTS ledger_day ON attempt_ledger(day)")
            connection.execute("CREATE INDEX IF NOT EXISTS ledger_time ON attempt_ledger(attempted_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS tasks_created ON tasks(created_at)")
            # This also protects draft -> submitting transitions, not just reserve.
            connection.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_task
                ON tasks ((1)) WHERE state IN ('generating', 'submitting')
            """)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result["snapshot"] = json.loads(result["snapshot"])
        return result

    @staticmethod
    def _recover(connection: sqlite3.Connection, now: float) -> None:
        connection.execute("""
            UPDATE tasks SET
                state = CASE state WHEN 'generating' THEN 'failed' ELSE 'uncertain' END,
                error = CASE state WHEN 'generating' THEN 'lease_expired'
                                   ELSE 'submission_uncertain' END,
                updated_at = ?, lease_until = NULL
            WHERE state IN ('generating', 'submitting') AND lease_until <= ?
        """, (now, now))

    def reserve(
        self, activity_key: str, snapshot: dict, now: float, day: str,
        daily_limit: int, cooldown_seconds: float, mode: str = "auto",
        lease_seconds: float = 900, *, ignore_limits: bool = False,
    ) -> dict:
        if lease_seconds <= 0 or cooldown_seconds < 0 or daily_limit < 0 or mode == "skip":
            raise ValueError("invalid_reservation")
        encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
        reason = None
        result = None
        with self._connection(write=True) as connection:
            self._recover(connection, now)
            seen = connection.execute("SELECT task_id FROM seen WHERE activity_key = ?", (activity_key,)).fetchone()
            skipped = None
            if seen and ignore_limits:
                row = connection.execute("SELECT * FROM tasks WHERE id = ?", (seen["task_id"],)).fetchone()
                if (row is not None and row["state"] == "skipped" and row["mode"] == "skip"
                        and row["submission_started"] == 0
                        and all(row[field] in (None, "") for field in ("caption", "image_path", "tid"))
                        and not connection.execute("SELECT 1 FROM attempt_ledger WHERE task_id = ?", (row["id"],)).fetchone()):
                    # A rule-based automatic skip never generated anything. An
                    # explicit manual command may claim it without weakening dedupe
                    # for actual attempts, drafts, or potentially submitted posts.
                    skipped = row
            if seen and skipped is None:
                reason = "duplicate"
            elif not ignore_limits and connection.execute("SELECT COUNT(*) FROM attempt_ledger WHERE day = ?", (day,)).fetchone()[0] >= daily_limit:
                reason = "daily_limit"
            else:
                previous = connection.execute("SELECT MAX(attempted_at) FROM attempt_ledger").fetchone()[0]
                if not ignore_limits and previous is not None and now - previous < cooldown_seconds:
                    reason = "cooldown"
                elif connection.execute("SELECT 1 FROM tasks WHERE state IN ('generating', 'submitting') LIMIT 1").fetchone():
                    reason = "busy"
            if reason is None:
                task_id = skipped["id"] if skipped is not None else uuid4().hex
                if skipped is not None:
                    connection.execute("""
                        UPDATE tasks SET snapshot = ?, mode = ?, state = 'generating',
                            created_at = ?, updated_at = ?, day = ?, error = NULL, lease_until = ?
                        WHERE id = ?
                    """, (encoded, mode, now, now, day, now + lease_seconds, task_id))
                else:
                    connection.execute("""
                        INSERT INTO tasks
                            (id, activity_key, snapshot, mode, state, created_at,
                             updated_at, day, lease_until)
                        VALUES (?, ?, ?, ?, 'generating', ?, ?, ?, ?)
                    """, (task_id, activity_key, encoded, mode, now, now, day, now + lease_seconds))
                    connection.execute("INSERT INTO seen VALUES (?, ?)", (activity_key, task_id))
                connection.execute("INSERT INTO attempt_ledger VALUES (?, ?, ?)", (task_id, day, now))
                result = self._decode(connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
        # Commit expired-lease recovery even when this particular claim is denied.
        if reason is not None:
            raise ClaimError(reason)
        assert result is not None
        return result

    def retry(
        self, job_id: str, activity_key: str, snapshot: dict, now: float, day: str,
        daily_limit: int, cooldown_seconds: float, lease_seconds: float = 900, *,
        ignore_limits: bool = False,
    ) -> dict:
        """Explicitly claim a new generation attempt for an unsubmitted failure.

        This is never called by reserve: ordinary/manual duplicate posts remain
        deduplicated. Each retry records its own durable attempt entry. Explicit
        manual commands may ignore_limits; this never bypasses safety/state checks.
        """
        if lease_seconds <= 0 or cooldown_seconds < 0 or daily_limit < 0:
            raise ValueError("invalid_reservation")
        encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
        reason = None
        result = None
        with self._connection(write=True) as connection:
            self._recover(connection, now)
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                reason = "not_found"
            elif row["activity_key"] != activity_key:
                reason = "activity_changed"
            elif (row["state"] != "failed" or row["submission_started"] != 0
                  or any(row[field] not in (None, "") for field in ("caption", "image_path", "tid"))):
                # Old databases lack a trustworthy flag: saved posting artifacts
                # are independently disqualifying, even when the new flag is 0.
                reason = "not_retryable"
            elif not ignore_limits and connection.execute("SELECT COUNT(*) FROM attempt_ledger WHERE day = ?", (day,)).fetchone()[0] >= daily_limit:
                reason = "daily_limit"
            else:
                previous = connection.execute("SELECT MAX(attempted_at) FROM attempt_ledger").fetchone()[0]
                if not ignore_limits and previous is not None and now - previous < cooldown_seconds:
                    reason = "cooldown"
                elif connection.execute("SELECT 1 FROM tasks WHERE state IN ('generating', 'submitting') LIMIT 1").fetchone():
                    reason = "busy"
            if reason is None:
                connection.execute("""
                    UPDATE tasks SET mode = 'retry', state = 'generating', snapshot = ?,
                        created_at = ?, updated_at = ?, day = ?, error = NULL,
                        caption = NULL, image_path = NULL, tid = NULL, lease_until = ?,
                        retry_count = retry_count + 1
                    WHERE id = ?
                """, (encoded, now, now, day, now + lease_seconds, job_id))
                # task_id is the historical column name, but retry entries must
                # have independent IDs so no previous attempt can be replaced.
                connection.execute("INSERT INTO attempt_ledger VALUES (?, ?, ?)", (uuid4().hex, day, now))
                result = self._decode(connection.execute("SELECT * FROM tasks WHERE id = ?", (job_id,)).fetchone())
        # A denied retry must still commit expired-lease recovery.
        if reason is not None:
            raise ClaimError(reason)
        assert result is not None
        return result

    def for_activity(self, activity_key: str) -> dict | None:
        with self._connection() as connection:
            return self._decode(connection.execute(
                "SELECT * FROM tasks WHERE activity_key = ?", (activity_key,)
            ).fetchone())

    def get(self, id: str) -> dict | None:
        with self._connection() as connection:
            return self._decode(connection.execute("SELECT * FROM tasks WHERE id = ?", (id,)).fetchone())

    def latest(self, limit: int = 5) -> list[dict]:
        if limit < 0:
            raise ValueError("invalid_limit")
        with self._connection() as connection:
            return [self._decode(row) for row in connection.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            )]

    def auto_seen(self, activity_key: str) -> bool:
        """Automatic-only deduplication; a startup baseline may still be previewed manually."""
        with self._connection() as connection:
            return connection.execute("SELECT 1 FROM auto_seen WHERE activity_key = ?", (activity_key,)).fetchone() is not None

    def remember_auto(self, activity_key: str) -> None:
        with self._connection(write=True) as connection:
            connection.execute("INSERT OR IGNORE INTO auto_seen VALUES (?)", (activity_key,))

    def block_reason(self, now: float, day: str, daily_limit: int, cooldown_seconds: float) -> str | None:
        """Read-only gate at first observation, not an execution claim.

        reserve() still authoritatively checks these conditions in a transaction.
        This early gate prevents a change during cooldown from becoming queued
        merely because its stability window crosses the cooldown/day boundary.
        """
        with self._connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM attempt_ledger WHERE day = ?", (day,)).fetchone()[0]
            if count >= daily_limit:
                return "daily_limit"
            previous = connection.execute("SELECT MAX(attempted_at) FROM attempt_ledger").fetchone()[0]
            if previous is not None and now - previous < cooldown_seconds:
                return "cooldown"
            if connection.execute(
                "SELECT 1 FROM tasks WHERE state IN ('generating', 'submitting') AND lease_until > ? LIMIT 1",
                (now,),
            ).fetchone():
                return "busy"
        return None

    def attempts(self, day: str) -> int:
        with self._connection() as connection:
            return connection.execute("SELECT COUNT(*) FROM attempt_ledger WHERE day = ?", (day,)).fetchone()[0]

    def update(
        self, id: str, expected_state: str, now: float, *,
        expected_retry_count: int | None = None, **fields,
    ) -> bool:
        """CAS task state, optionally fencing a runner from an older attempt."""
        if not fields.keys() <= UPDATE_FIELDS:
            raise ValueError("invalid_update_field")
        if "state" in fields and fields["state"] not in STATES:
            raise ValueError("invalid_state")
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (id,)).fetchone()
            if (row is None or row["state"] != expected_state
                    or (expected_retry_count is not None and row["retry_count"] != expected_retry_count)):
                return False
            if row["state"] in ACTIVE_STATES and row["lease_until"] <= now:
                return False
            state = fields.get("state", row["state"])
            lease = row["lease_until"]
            if state not in ACTIVE_STATES:
                lease = None
            elif row["state"] != state:
                lease = now + 900
            values = dict(fields)
            if state == "submitting":
                values["submission_started"] = 1
            values.update(updated_at=now, lease_until=lease)
            assignments = ", ".join(f"{key} = ?" for key in values)
            try:
                cursor = connection.execute(
                    f"UPDATE tasks SET {assignments} WHERE id = ? AND state = ? "
                    "AND (? IS NULL OR retry_count = ?)",
                    (*values.values(), id, expected_state, expected_retry_count, expected_retry_count),
                )
            except sqlite3.IntegrityError:
                # Another active task owns the global lease.
                return False
            return cursor.rowcount == 1

    def confirm_submission(self, job_id: str, tid: str, now: float) -> bool:
        """Record a verified upstream receipt even if the waiting lease expired.

        Expiry means uncertainty, not proof of failure; a late genuine receipt
        resolves that uncertainty without issuing another external submission.
        """
        if not isinstance(tid, str) or not tid.strip():
            raise ValueError("invalid_tid")
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                "UPDATE tasks SET state = 'success', tid = ?, error = '', updated_at = ?, lease_until = NULL "
                "WHERE id = ? AND state IN ('submitting', 'uncertain')",
                (tid, now, job_id),
            )
            return cursor.rowcount == 1

    def begin_submission(self, job_id: str, now: float, lease_seconds: float = 900) -> bool:
        """Atomically claim the global active lease for an existing draft.

        Publishing a draft is not another generation attempt: the durable quota
        and cooldown ledger is deliberately untouched.
        """
        if lease_seconds <= 0:
            raise ValueError("invalid_lease")
        with self._connection(write=True) as connection:
            self._recover(connection, now)
            cursor = connection.execute("""
                UPDATE tasks SET state = 'submitting', submission_started = 1, updated_at = ?, lease_until = ?
                WHERE id = ? AND state = 'draft' AND NOT EXISTS (
                    SELECT 1 FROM tasks WHERE state IN ('generating', 'submitting')
                )
            """, (now, now + lease_seconds, job_id))
            return cursor.rowcount == 1

    def skip(self, activity_key: str, snapshot: dict, now: float, reason: str) -> None:
        encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        with self._connection(write=True) as connection:
            if connection.execute("SELECT 1 FROM seen WHERE activity_key = ?", (activity_key,)).fetchone():
                return
            task_id = uuid4().hex
            connection.execute("""
                INSERT INTO tasks
                    (id, activity_key, snapshot, mode, state, created_at, updated_at, day, error)
                VALUES (?, ?, ?, 'skip', 'skipped', ?, ?, ?, ?)
            """, (task_id, activity_key, encoded, now, now, day, reason))
            connection.execute("INSERT INTO seen VALUES (?, ?)", (activity_key, task_id))

    def recover(self, now: float, stale_seconds: float) -> None:
        """Recover expired leases only; age alone never invalidates a live lease.

        stale_seconds is retained for the public API; all records have explicit
        leases, so updated_at age must not be used as a second expiry condition.
        """
        with self._connection(write=True) as connection:
            self._recover(connection, now)

    def renew(
        self, id: str, now: float, lease_seconds: float = 900, *,
        expected_retry_count: int | None = None,
    ) -> bool:
        """Extend a live lease only for the expected generation attempt, if given."""
        if lease_seconds <= 0:
            raise ValueError("invalid_lease")
        with self._connection(write=True) as connection:
            cursor = connection.execute("""
                UPDATE tasks SET lease_until = ?, updated_at = ?
                WHERE id = ? AND state IN ('generating', 'submitting') AND lease_until > ?
                    AND (? IS NULL OR retry_count = ?)
            """, (now + lease_seconds, now, id, now, expected_retry_count, expected_retry_count))
            return cursor.rowcount == 1

    def expire_drafts(self, now: float, ttl_seconds: float) -> list[dict]:
        if ttl_seconds < 0:
            raise ValueError("invalid_ttl")
        with self._connection(write=True) as connection:
            rows = connection.execute("""
                SELECT * FROM tasks WHERE state = 'draft' AND updated_at <= ?
                ORDER BY created_at, rowid
            """, (now - ttl_seconds,)).fetchall()
            expired = []
            for row in rows:
                connection.execute("""
                    UPDATE tasks SET state = 'skipped', error = 'draft_expired',
                        updated_at = ?, lease_until = NULL WHERE id = ? AND state = 'draft'
                """, (now, row["id"]))
                expired.append(self._decode(connection.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()))
            return expired

    def prune(self, now: float, retention_seconds: float) -> None:
        if retention_seconds < 0:
            raise ValueError("invalid_retention")
        with self._connection(write=True) as connection:
            connection.execute("""
                DELETE FROM tasks WHERE state IN ('success', 'failed', 'skipped')
                    AND updated_at < ?
            """, (now - retention_seconds,))

    def close(self) -> None:
        """No persistent connection is held."""
