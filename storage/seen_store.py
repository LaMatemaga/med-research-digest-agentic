"""Persistent memory across runs: a SQLite `seen_items` table so re-runs only
surface genuinely new papers instead of re-showing everything every time."""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "seen_items.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_items (
    pmid TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_score INTEGER,
    alerted_at TEXT
);
"""


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


def init_db(db_path: str | Path | None = None) -> None:
    """Creates the seen_items table if it doesn't already exist."""
    with closing(_connect(db_path)):
        pass


def reset(db_path: str | Path | None = None) -> None:
    """Clears all remembered items. Used by --reset-seen for demo control."""
    with closing(_connect(db_path)) as conn:
        conn.execute("DELETE FROM seen_items")
        conn.commit()


def filter_new(papers: list[dict], db_path: str | Path | None = None) -> list[dict]:
    """Returns only the papers whose pmid is not already recorded in seen_items."""
    if not papers:
        return []
    with closing(_connect(db_path)) as conn:
        pmids = [p.get("pmid", "") for p in papers]
        placeholders = ",".join("?" for _ in pmids)
        rows = conn.execute(
            f"SELECT pmid FROM seen_items WHERE pmid IN ({placeholders})", pmids
        ).fetchall()
        already_seen = {row[0] for row in rows}
    return [p for p in papers if p.get("pmid", "") not in already_seen]


def record_seen(papers: list[dict], db_path: str | Path | None = None) -> None:
    """Upserts each paper into seen_items. first_seen is set once; last_score updates
    on every run so the memory reflects the most recent grading."""
    now = datetime.now(timezone.utc).isoformat()
    with closing(_connect(db_path)) as conn:
        for paper in papers:
            pmid = paper.get("pmid", "")
            if not pmid:
                continue
            conn.execute(
                """
                INSERT INTO seen_items (pmid, first_seen, last_score, alerted_at)
                VALUES (?, ?, ?, NULL)
                ON CONFLICT(pmid) DO UPDATE SET last_score = excluded.last_score
                """,
                (pmid, now, paper.get("relevance_score")),
            )
        conn.commit()


def mark_alerted(pmid: str, db_path: str | Path | None = None) -> None:
    """Records that a Telegram alert was sent for this PMID."""
    now = datetime.now(timezone.utc).isoformat()
    with closing(_connect(db_path)) as conn:
        conn.execute("UPDATE seen_items SET alerted_at = ? WHERE pmid = ?", (now, pmid))
        conn.commit()


def was_alerted(pmid: str, db_path: str | Path | None = None) -> bool:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT alerted_at FROM seen_items WHERE pmid = ?", (pmid,)
        ).fetchone()
    return bool(row and row[0])
