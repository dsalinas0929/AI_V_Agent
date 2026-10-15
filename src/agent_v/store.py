"""SQLite persistence; only token hashes are stored."""

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class Store:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    name TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, workspace TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT 'New conversation',
                    history TEXT NOT NULL DEFAULT '[]',
                    updated TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_user(self, name: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("INSERT INTO users VALUES (?, ?)", (name, self.digest(token)))
        return token

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def authenticate(self, token: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT name FROM users WHERE token_hash = ?", (self.digest(token),)
            ).fetchone()
        return row[0] if row else None

    def revoke(self, name: str) -> bool:
        with self.connect() as db:
            return bool(db.execute("DELETE FROM users WHERE name = ?", (name,)).rowcount)

    def create_session(self, owner: str, workspace: str) -> str:
        session_id = uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO sessions (id, owner, workspace) VALUES (?, ?, ?)",
                (session_id, owner, workspace),
            )
        return session_id

    def session(self, session_id: str, owner: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE id = ? AND owner = ?", (session_id, owner)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["history"] = json.loads(result["history"])
        return result

    def sessions(self, owner: str) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id, title, workspace, updated FROM sessions "
                    "WHERE owner = ? ORDER BY updated DESC LIMIT 100",
                    (owner,),
                )
            ]

    def save(self, session_id: str, owner: str, history: list, title: str):
        with self.connect() as db:
            db.execute(
                "UPDATE sessions SET history = ?, title = ?, "
                "updated = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ? AND owner = ?",
                (json.dumps(history), title[:80], session_id, owner),
            )
