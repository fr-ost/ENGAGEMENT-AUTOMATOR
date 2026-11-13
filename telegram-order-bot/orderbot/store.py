"""SQLite persistence, so scheduled orders survive restarts and redeploys."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    link TEXT NOT NULL,
    views INTEGER NOT NULL,
    likes INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    status TEXT NOT NULL,          -- active | done | cancelled
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    kind TEXT NOT NULL,            -- views | likes
    quantity INTEGER NOT NULL,
    service_id TEXT NOT NULL,
    due_at REAL NOT NULL,
    status TEXT NOT NULL,          -- pending | placing | placed | failed | unknown | cancelled
    attempts INTEGER NOT NULL DEFAULT 0,
    panel_order_id TEXT,
    error TEXT,
    placed_at REAL
);
CREATE INDEX IF NOT EXISTS orders_due ON orders(status, due_at);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

OPEN = ("pending", "placing")


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.executescript(SCHEMA)

    # -- settings ---------------------------------------------------------

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    # -- jobs -------------------------------------------------------------

    def create_job(
        self, link: str, views: int, likes: int, chat_id: int, now: float,
        orders: Iterable[tuple[str, int, str, float]],
    ) -> int:
        """Insert the job and all of its orders (kind, quantity, service_id, due_at) atomically."""
        with self._db:
            cur = self._db.execute(
                "INSERT INTO jobs(link, views, likes, chat_id, status, created_at) VALUES(?,?,?,?, 'active', ?)",
                (link, views, likes, chat_id, now),
            )
            job_id = cur.lastrowid
            self._db.executemany(
                "INSERT INTO orders(job_id, kind, quantity, service_id, due_at, status) VALUES(?,?,?,?,?, 'pending')",
                [(job_id, kind, qty, service, due) for kind, qty, service, due in orders],
            )
        return int(job_id)

    def job(self, job_id: int) -> Optional[sqlite3.Row]:
        return self._db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def active_job_for_link(self, link: str) -> Optional[sqlite3.Row]:
        return self._db.execute("SELECT * FROM jobs WHERE link=? AND status='active'", (link,)).fetchone()

    def recent_jobs(self, limit: int = 5) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def set_job_status(self, job_id: int, status: str) -> None:
        with self._db:
            self._db.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))

    # -- orders -----------------------------------------------------------

    def job_orders(self, job_id: int) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM orders WHERE job_id=? ORDER BY due_at, id", (job_id,)).fetchall()

    def due_orders(self, now: float) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM orders WHERE status='pending' AND due_at<=? ORDER BY due_at, id", (now,)
        ).fetchall()

    def next_due(self) -> Optional[float]:
        row = self._db.execute("SELECT MIN(due_at) AS due FROM orders WHERE status='pending'").fetchone()
        return row["due"]

    def orders_with_status(self, status: str) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM orders WHERE status=?", (status,)).fetchall()

    def update_order(self, order_id: int, **fields: Any) -> None:
        columns = ", ".join(f"{name}=?" for name in fields)
        with self._db:
            self._db.execute(f"UPDATE orders SET {columns} WHERE id=?", (*fields.values(), order_id))

    def cancel_pending(self, job_id: int) -> int:
        with self._db:
            cur = self._db.execute(
                "UPDATE orders SET status='cancelled' WHERE job_id=? AND status='pending'", (job_id,)
            )
        return cur.rowcount

    def has_open_orders(self, job_id: int) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM orders WHERE job_id=? AND status IN ('pending','placing') LIMIT 1", (job_id,)
        ).fetchone()
        return row is not None
