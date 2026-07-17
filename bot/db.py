"""SQLite (WAL) data layer — stdlib only, single-process.

Times are stored as INTEGER epoch UTC and compared numerically.
Transactions are explicit (BEGIN IMMEDIATE / COMMIT) via the `transaction()` ctx.
"""
import os
import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS media (
    media_key TEXT PRIMARY KEY,   -- md5 of the image bytes
    file_name TEXT NOT NULL,      -- stored under MEDIA_DIR
    file_id   TEXT                -- Telegram file_id cache (nullable)
);

CREATE TABLE IF NOT EXISTS templates (
    step_name TEXT PRIMARY KEY,   -- greeting/ask/working/intro/cta
    text      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS variants (
    variant_id  INTEGER PRIMARY KEY,   -- 0..65 (block order)
    topic       TEXT NOT NULL,
    card_number TEXT NOT NULL,         -- display only ('0','I','II'..)
    card_name   TEXT NOT NULL,
    diagnosis   TEXT NOT NULL,         -- r8, verbatim
    media_key   TEXT NOT NULL REFERENCES media(media_key)
);

CREATE TABLE IF NOT EXISTS bag (
    topic      TEXT NOT NULL,            -- per-topic shuffled bag (even-random WITHIN a topic)
    position   INTEGER NOT NULL,
    variant_id INTEGER NOT NULL,
    PRIMARY KEY (topic, position)
);
CREATE TABLE IF NOT EXISTS bag_cursor (
    topic TEXT PRIMARY KEY,
    pos   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS clients (
    client_id        INTEGER PRIMARY KEY,   -- telegram user id of the client
    bcid             TEXT,                  -- business_connection_id (nullable in sim)
    state            TEXT NOT NULL DEFAULT 'NEW',
    variant_id       INTEGER,               -- NULL until the topic locks (or card-time fallback)
    topic            TEXT,                  -- detected client topic; locked together with variant_id
    name             TEXT,
    question         TEXT,
    run_id           INTEGER NOT NULL DEFAULT 1,
    triggered_at     INTEGER,
    last_incoming_at INTEGER,
    version          INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS steps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id  INTEGER NOT NULL,
    run_id     INTEGER NOT NULL,
    step_name  TEXT NOT NULL,
    run_at     INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending/sending/sent/skipped/cancelled
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE (client_id, run_id, step_name)
);
CREATE INDEX IF NOT EXISTS idx_steps_due ON steps(status, run_at);
CREATE INDEX IF NOT EXISTS idx_steps_client ON steps(client_id, run_id);

CREATE TABLE IF NOT EXISTS sent_log (
    client_id     INTEGER NOT NULL,
    run_id        INTEGER NOT NULL,
    step_name     TEXT NOT NULL,
    tg_message_id INTEGER,
    sent_at       INTEGER NOT NULL,
    PRIMARY KEY (client_id, run_id, step_name)
);

CREATE TABLE IF NOT EXISTS business_connections (
    business_connection_id TEXT PRIMARY KEY,   -- Telegram business connection id
    owner_user_id INTEGER,                     -- account owner (reader); used to ignore her own messages
    can_reply     INTEGER,
    can_read      INTEGER,
    is_enabled    INTEGER,
    connected_at  INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,          -- epoch UTC of the event
    event     TEXT NOT NULL,             -- 'triggered' | 'hot_lead' | 'topic_detected' | 'topic_fallback'
    client_id INTEGER,
    run_id    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts, event);
"""

CONTENT_TABLES = ["variants", "media", "templates", "bag", "bag_cursor"]
RUNTIME_TABLES = ["clients", "steps", "sent_log", "business_connections", "events"]


def log_event(conn, event, client_id, run_id, ts):
    """Append an analytics event. Call inside the caller's transaction so it is atomic
    with the state change it records (append-only, no constraints -> never conflicts)."""
    conn.execute("INSERT INTO events(ts, event, client_id, run_id) VALUES (?, ?, ?, ?)",
                 (ts, event, client_id, run_id))


def meta_get(conn, key, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else default


def meta_set(conn, key, value):
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def connect(path=None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit; we manage txns
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def init(conn):
    # Migration: bag/bag_cursor gained a topic dimension (per-topic even-random).
    # They are content tables (no client data) — drop old-schema ones; the importer
    # or the first draw rebuilds them from `variants`.
    bag_cols = _columns(conn, "bag")
    if bag_cols and "topic" not in bag_cols:
        conn.execute("DROP TABLE bag")
        conn.execute("DROP TABLE IF EXISTS bag_cursor")
    conn.executescript(SCHEMA)
    # Migration: clients.topic (additive, nullable — safe on a live DB).
    if "topic" not in _columns(conn, "clients"):
        conn.execute("ALTER TABLE clients ADD COLUMN topic TEXT")


def wipe(conn, tables):
    for t in tables:
        conn.execute(f"DELETE FROM {t}")


@contextmanager
def transaction(conn):
    """Immediate write transaction (atomic multi-statement)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
