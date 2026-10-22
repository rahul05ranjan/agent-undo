"""SQLite-backed operation journal for agent-undo."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    op_type TEXT NOT NULL,          -- file-write, shell, git, api, checkpoint
    "timestamp" REAL NOT NULL,
    command TEXT,
    path TEXT,
    content_before TEXT,
    content_after TEXT,
    exit_code INTEGER,
    metadata TEXT,                  -- JSON blob for extra fields
    parent_id INTEGER REFERENCES operations(id)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id INTEGER NOT NULL REFERENCES operations(id),
    label TEXT NOT NULL,
    "timestamp" REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ops_session ON operations(session_id);
CREATE INDEX IF NOT EXISTS idx_ops_timestamp ON operations("timestamp");
CREATE INDEX IF NOT EXISTS idx_ops_type ON operations(op_type);
"""


class Journal:
    """Persistent journal of agent operations."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def transaction(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def record(
        self,
        session_id: str,
        op_type: str,
        *,
        command: str | None = None,
        path: str | None = None,
        content_before: str | None = None,
        content_after: str | None = None,
        exit_code: int | None = None,
        metadata: dict[str, Any] | None = None,
        parent_id: int | None = None,
        timestamp: float | None = None,
    ) -> int:
        """Record an operation. Returns the row id."""
        ts = time.time() if timestamp is None else timestamp
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO operations
                   (session_id, op_type, "timestamp", command, path,
                    content_before, content_after, exit_code, metadata, parent_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    op_type,
                    ts,
                    command,
                    path,
                    content_before,
                    content_after,
                    exit_code,
                    json.dumps(metadata) if metadata else None,
                    parent_id,
                ),
            )
            return cur.lastrowid

    def checkpoint(self, session_id: str, label: str, op_id: int | None = None) -> int:
        """Tag the current moment (or a specific operation) as a checkpoint."""
        ts = time.time()
        with self.transaction() as conn:
            if op_id is None:
                # Create a checkpoint-type operation
                cur = conn.execute(
                    'INSERT INTO operations (session_id, op_type, "timestamp", command)'
                    " VALUES (?, 'checkpoint', ?, ?)",
                    (session_id, ts, label),
                )
                op_id = cur.lastrowid
            conn.execute(
                'INSERT INTO checkpoints (operation_id, label, "timestamp") VALUES (?, ?, ?)',
                (op_id, label, ts),
            )
            return op_id

    def get_session_ops(self, session_id: str, op_type: str | None = None) -> list[dict]:
        """Get all operations for a session, optionally filtered by type."""
        with self.transaction() as conn:
            if op_type:
                rows = conn.execute(
                    "SELECT * FROM operations"
                    ' WHERE session_id = ? AND op_type = ? ORDER BY "timestamp"',
                    (session_id, op_type),
                ).fetchall()
            else:
                rows = conn.execute(
                    'SELECT * FROM operations WHERE session_id = ? ORDER BY "timestamp"',
                    (session_id,),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_timeline(self, session_id: str) -> list[dict]:
        """Get the full timeline of operations with checkpoint labels."""
        with self.transaction() as conn:
            rows = conn.execute(
                """SELECT o.*, c.label as checkpoint_label
                   FROM operations o
                   LEFT JOIN checkpoints c ON c.operation_id = o.id
                   WHERE o.session_id = ?
                   ORDER BY o."timestamp" """,
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def find_checkpoint(self, session_id: str, label: str) -> int | None:
        """Find the operation id for a named checkpoint."""
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT c.operation_id FROM checkpoints c
                   JOIN operations o ON o.id = c.operation_id
                   WHERE o.session_id = ? AND c.label = ?
                   ORDER BY c."timestamp" DESC LIMIT 1""",
                (session_id, label),
            ).fetchone()
            return row["operation_id"] if row else None

    def get_ops_since(self, session_id: str, since_op_id: int) -> list[dict]:
        """Get all operations after a given operation id (exclusive)."""
        with self.transaction() as conn:
            row = conn.execute(
                'SELECT "timestamp" FROM operations WHERE id = ?', (since_op_id,)
            ).fetchone()
            if not row:
                return []
            ts = row["timestamp"]
            rows = conn.execute(
                """SELECT o.*, c.label as checkpoint_label
                   FROM operations o
                   LEFT JOIN checkpoints c ON c.operation_id = o.id
                   WHERE o.session_id = ? AND o."timestamp" >= ? AND o.id != ?
                   ORDER BY o."timestamp" DESC""",
                (session_id, ts, since_op_id),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_statistics(self) -> dict:
        """Get global journal statistics."""
        with self.transaction() as conn:
            total = conn.execute("SELECT COUNT(*) as cnt FROM operations").fetchone()["cnt"]
            sessions = conn.execute(
                "SELECT COUNT(DISTINCT session_id) as cnt FROM operations"
            ).fetchone()["cnt"]
            types = conn.execute(
                "SELECT op_type, COUNT(*) as cnt FROM operations GROUP BY op_type"
            ).fetchall()
            return {
                "total_operations": total,
                "total_sessions": sessions,
                "by_type": {r["op_type"]: r["cnt"] for r in types},
            }

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
