from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from config import get_settings


SCHEMA_FILE = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


def _sqlite_path() -> Path:
    settings = get_settings()
    root = Path(__file__).resolve().parents[2]
    db_path = Path(settings.sqlite_path)
    if not db_path.is_absolute():
        db_path = root / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(_sqlite_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        conn.commit()


def fetch_one(query: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    with get_connection() as conn:
        cur = conn.execute(query, tuple(params))
        return cur.fetchone()


def fetch_all(query: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with get_connection() as conn:
        cur = conn.execute(query, tuple(params))
        return cur.fetchall()


def execute(query: str, params: Iterable[Any] = ()) -> int:
    with get_connection() as conn:
        cur = conn.execute(query, tuple(params))
        conn.commit()
        return int(cur.lastrowid)
