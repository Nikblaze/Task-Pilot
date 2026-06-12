"""SQLite storage layer. Single file DB, no server needed.

Tables:
  items    - every captured thing (task / commitment / idea / note)
  settings - key/value (e.g. owner_chat_id)
  events   - activity log, used to build the weekly timelog
  pending  - short-lived "waiting for your next message" state (e.g. pick a time)
"""
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "data/bot.db")


@contextmanager
def _conn():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


# Columns added after the original schema shipped. SQLite has no
# "ADD COLUMN IF NOT EXISTS", so each is attempted and the duplicate error swallowed.
_MIGRATIONS = [
    "ALTER TABLE items ADD COLUMN remaining_effort_min INTEGER",
    "ALTER TABLE items ADD COLUMN actual_effort_min INTEGER",
    "ALTER TABLE items ADD COLUMN last_checkin_at TEXT",
    "ALTER TABLE items ADD COLUMN waiting_since TEXT",     # when a 'waiting' item was last nudged
]


def init_db():
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                type          TEXT NOT NULL DEFAULT 'note',
                title         TEXT NOT NULL,
                raw           TEXT,
                person        TEXT,
                due_at        TEXT,
                effort_min    INTEGER,
                start_by_at   TEXT,
                remind_at     TEXT,
                status        TEXT NOT NULL DEFAULT 'open',
                sent_startby  INTEGER NOT NULL DEFAULT 0,
                sent_daybefore INTEGER NOT NULL DEFAULT 0,
                sent_due      INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL,
                done_at       TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER,
                kind    TEXT,
                ts      TEXT
            );
            CREATE TABLE IF NOT EXISTS pending (
                chat_id INTEGER PRIMARY KEY,
                action  TEXT,
                item_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS checkins (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id              INTEGER,
                ts                   TEXT,
                remaining_before_min INTEGER,
                remaining_after_min  INTEGER
            );
            """
        )
        for stmt in _MIGRATIONS:
            try:
                c.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists


# ---------- settings ----------
def get_setting(key, default=None):
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with _conn() as c:
        c.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


# ---------- items ----------
def add_item(**f):
    cols = ["type", "title", "raw", "person", "due_at", "effort_min",
            "remaining_effort_min", "start_by_at", "remind_at", "status",
            "created_at", "waiting_since"]
    # remaining_effort defaults to the initial effort estimate
    if f.get("remaining_effort_min") is None:
        f["remaining_effort_min"] = f.get("effort_min")
    vals = [f.get(k) for k in cols]
    with _conn() as c:
        cur = c.execute(
            f"INSERT INTO items({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
            vals,
        )
        return cur.lastrowid


def update_item(item_id, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE items SET {sets} WHERE id=?", (*fields.values(), item_id))


def get_item(item_id):
    with _conn() as c:
        return c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()


def list_open():
    with _conn() as c:
        return c.execute(
            "SELECT * FROM items WHERE status='open' ORDER BY "
            "COALESCE(due_at, '9999') ASC, id ASC"
        ).fetchall()


def list_by_type(t):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM items WHERE type=? AND status='open' ORDER BY id DESC", (t,)
        ).fetchall()


def due_for_reminder(now_iso):
    """Open items that have any reminder condition possibly triggered."""
    with _conn() as c:
        return c.execute(
            "SELECT * FROM items WHERE status='open' AND "
            "(remind_at IS NOT NULL OR start_by_at IS NOT NULL OR due_at IS NOT NULL)"
        ).fetchall()


# ---------- events / timelog ----------
def add_event(item_id, kind, ts):
    with _conn() as c:
        c.execute("INSERT INTO events(item_id,kind,ts) VALUES(?,?,?)", (item_id, kind, ts))


def events_between(start_iso, end_iso):
    with _conn() as c:
        return c.execute(
            "SELECT e.*, i.title, i.type, i.person FROM events e "
            "LEFT JOIN items i ON i.id = e.item_id "
            "WHERE e.ts >= ? AND e.ts <= ? ORDER BY e.ts ASC",
            (start_iso, end_iso),
        ).fetchall()


# ---------- pending (multi-step replies) ----------
def set_pending(chat_id, action, item_id):
    with _conn() as c:
        c.execute(
            "INSERT INTO pending(chat_id,action,item_id) VALUES(?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET action=excluded.action, item_id=excluded.item_id",
            (chat_id, action, item_id),
        )


def get_pending(chat_id):
    with _conn() as c:
        return c.execute("SELECT * FROM pending WHERE chat_id=?", (chat_id,)).fetchone()


def clear_pending(chat_id):
    with _conn() as c:
        c.execute("DELETE FROM pending WHERE chat_id=?", (chat_id,))


# ---------- capacity ----------
def schedulable_open():
    """Open tasks/commitments with a deadline — the items that consume capacity."""
    with _conn() as c:
        return c.execute(
            "SELECT * FROM items WHERE status='open' "
            "AND type IN ('task','commitment') AND due_at IS NOT NULL"
        ).fetchall()


# ---------- waiting ----------
def list_waiting():
    with _conn() as c:
        return c.execute(
            "SELECT * FROM items WHERE type='waiting' AND status='open' ORDER BY id DESC"
        ).fetchall()


# ---------- missed / weekly review ----------
def missed_before(now_iso):
    """Open items whose deadline has passed (a missed commitment/task)."""
    with _conn() as c:
        return c.execute(
            "SELECT * FROM items WHERE status='open' "
            "AND type IN ('task','commitment') AND due_at IS NOT NULL AND due_at < ? "
            "ORDER BY due_at ASC",
            (now_iso,),
        ).fetchall()


def completed_between(start_iso, end_iso):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM items WHERE status='done' "
            "AND done_at IS NOT NULL AND done_at >= ? AND done_at <= ? "
            "ORDER BY done_at ASC",
            (start_iso, end_iso),
        ).fetchall()


# ---------- check-ins ----------
def add_checkin(item_id, ts, before_min, after_min):
    with _conn() as c:
        c.execute(
            "INSERT INTO checkins(item_id,ts,remaining_before_min,remaining_after_min) "
            "VALUES(?,?,?,?)",
            (item_id, ts, before_min, after_min),
        )
