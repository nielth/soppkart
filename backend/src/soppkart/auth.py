"""Users, passwords, login sessions and permissions, stored in SQLite.

Admins manage users and can see everyone's findings. What anyone can use,
admins included, is what's switched on for them (see PERMISSIONS); visitors
who aren't logged in get none of it.
"""

import hashlib
import hmac
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from soppkart import config

DB_PATH = config.DATA_DIR / "user" / "users.sqlite"
COOKIE_NAME = "soppkart_session"

# What an admin can grant, with the label shown in the admin page. "training":
# the user's findings are used in training and they may retrain, so people who
# register themselves can't skew the model until an admin trusts them.
PERMISSIONS = {
    "imagery": "Flyfoto",
    "strava": "Strava heatmap",
    **{s.key: s.name for s in config.SPECIES.values() if s.restricted},
    "training": "Trening",
}

# Self-registrations allowed per hour (all visitors together), against sign-up spam.
MAX_REGISTRATIONS_PER_HOUR = 20
_registrations: list[float] = []

# Failed logins per username, to slow down password guessing.
MAX_FAILED_LOGINS = 5
FAILED_LOGIN_WINDOW_S = 15 * 60
_failed_logins: dict[str, list[float]] = {}


@dataclass(frozen=True)
class User:
    id: int
    username: str
    is_admin: bool
    permissions: frozenset[str]
    created_at: str

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "is_admin": self.is_admin,
            "permissions": sorted(self.permissions),
        }


def can(user: User | None, permission: str) -> bool:
    return user is not None and user.can(permission)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                permissions TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at REAL NOT NULL
            )""")
        # Version 1: admins no longer get every permission automatically, so the
        # admins from before keep what they had by getting them all switched on once.
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            conn.execute(
                "UPDATE users SET permissions = ? WHERE is_admin = 1",
                (",".join(sorted(PERMISSIONS)),),
            )
            conn.execute("PRAGMA user_version = 1")
        yield conn
        conn.commit()
    finally:
        conn.close()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    _, salt, digest = stored.split("$")
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    return hmac.compare_digest(actual.hex(), digest)


def user_from_row(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        is_admin=bool(row["is_admin"]),
        permissions=frozenset(p for p in row["permissions"].split(",") if p in PERMISSIONS),
        created_at=row["created_at"],
    )


def count_users() -> int:
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def list_users() -> list[User]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC, id DESC").fetchall()
    return [user_from_row(r) for r in rows]


def get_user(user_id: int) -> User | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return user_from_row(row) if row else None


def get_user_by_name(username: str) -> User | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return user_from_row(row) if row else None


def usernames() -> dict[int, str]:
    return {u.id: u.username for u in list_users()}


def trusted_user_ids() -> set[int]:
    """Users whose findings are used in training."""
    return {u.id for u in list_users() if u.can("training")}


def registration_allowed() -> bool:
    """Counts a self-registration; False when there have been too many this hour."""
    now = time.time()
    _registrations[:] = [t for t in _registrations if now - t < 3600]
    if len(_registrations) >= MAX_REGISTRATIONS_PER_HOUR:
        return False
    _registrations.append(now)
    return True


def add_user(username: str, password: str, is_admin: bool, permissions: set[str]) -> User:
    """Raises ValueError if the username is taken."""
    try:
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, is_admin, permissions, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    username,
                    hash_password(password),
                    int(is_admin),
                    ",".join(sorted(permissions & PERMISSIONS.keys())),
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )
            user_id = cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError(f"Brukernavnet {username} er tatt") from None
    assert user_id is not None
    user = get_user(user_id)
    assert user is not None
    return user


def update_user(
    user_id: int,
    is_admin: bool | None = None,
    permissions: set[str] | None = None,
    password: str | None = None,
) -> User | None:
    with connect() as conn:
        if is_admin is not None:
            conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (int(is_admin), user_id))
        if permissions is not None:
            conn.execute(
                "UPDATE users SET permissions = ? WHERE id = ?",
                (",".join(sorted(permissions & PERMISSIONS.keys())), user_id),
            )
        if password is not None:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(password), user_id),
            )
            # A new password logs the user out everywhere.
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    return get_user(user_id)


def delete_user(user_id: int) -> bool:
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return cur.rowcount > 0


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def is_locked_out(username: str) -> bool:
    now = time.time()
    recent = [
        t for t in _failed_logins.get(username.lower(), []) if now - t < FAILED_LOGIN_WINDOW_S
    ]
    _failed_logins[username.lower()] = recent
    return len(recent) >= MAX_FAILED_LOGINS


def login(username: str, password: str) -> tuple[User, str] | None:
    """A new session token for the user, or None if the password is wrong."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if row is None or not check_password(password, row["password_hash"]):
        _failed_logins.setdefault(username.lower(), []).append(time.time())
        return None
    _failed_logins.pop(username.lower(), None)
    token = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(days=config.SESSION_DAYS)
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (token_hash(token), row["id"], expires.timestamp()),
        )
    return user_from_row(row), token


def logout(token: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))


def user_for_token(token: str | None) -> User | None:
    if not token:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id"
            " WHERE sessions.token_hash = ? AND sessions.expires_at > ?",
            (token_hash(token), time.time()),
        ).fetchone()
    return user_from_row(row) if row else None
