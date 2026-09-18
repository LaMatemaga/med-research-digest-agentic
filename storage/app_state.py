"""Small key/value store for run-to-run state that isn't a seen item (e.g. the Telegraph
access token). Lives in the same SQLite file as seen_items, so it shares the gitignored
data/ directory and the GitHub Actions cache."""

import sqlite3
from contextlib import closing
from pathlib import Path

from storage import seen_store

_SCHEMA = "CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"


def _connect(db_path: str | Path | None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else Path(seen_store.DEFAULT_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(_SCHEMA)
    return conn


def get_value(key: str, db_path: str | Path | None = None) -> str | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_value(key: str, value: str, db_path: str | Path | None = None) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
