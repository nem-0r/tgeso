"""Durable step scheduler (outbox pattern).

Per step: CLAIM (pending->sending, atomic) -> guards -> SEND (outside any txn)
-> CONFIRM (mark sent + sent_log + insert NEXT step + update state, one txn).

Durability:
  * greeting/ask/working/intro/image/diagnosis = at-least-once (a 'sending' row
    left by a crash is reset to 'pending' and re-sent by startup_sweep).
  * cta = at-most-once (sent_log row written BEFORE the API call; on ambiguous
    crash it is NOT re-sent).
Chain: the NEXT step is inserted in the SAME transaction that marks the current
one sent, so a crash can never silently strand a client mid-funnel.
"""
import os
import random
import sqlite3

from . import config, content, variants
from .db import transaction, log_event as dbm_log_event

TERMINAL = config.TERMINAL_STATES


def _template(conn, name):
    row = conn.execute("SELECT text FROM templates WHERE step_name=?", (name,)).fetchone()
    return row["text"] if row else ""


def _variant(conn, variant_id):
    return conn.execute("SELECT * FROM variants WHERE variant_id=?", (variant_id,)).fetchone()


def _media_path(conn, variant_id):
    row = conn.execute(
        "SELECT m.file_name FROM variants v JOIN media m ON v.media_key=m.media_key "
        "WHERE v.variant_id=?", (variant_id,)).fetchone()
    return os.path.join(config.MEDIA_DIR, row["file_name"])


def _next_step(step_name):
    i = config.STEP_ORDER.index(step_name)
    return config.STEP_ORDER[i + 1] if i + 1 < len(config.STEP_ORDER) else None


def _ensure_variant(conn, client, now, rng=None):
    """Card-time fallback: if no topic was ever detected in the client's messages,
    assign a random topic's variant now so the client always gets a reading
    (mirrors the pre-topic fully-random behaviour). variant_id is the lock."""
    if client["variant_id"] is not None:
        return client
    topic = client["topic"] or (rng or random).choice(variants.topics(conn))
    vid = variants.draw_variant(conn, topic, rng)   # outside the txn (rebuild opens its own)
    with transaction(conn):
        cur = conn.execute(
            "UPDATE clients SET topic=COALESCE(topic, ?), variant_id=?, version=version+1 "
            "WHERE client_id=? AND variant_id IS NULL",
            (topic, vid, client["client_id"]))
        if cur.rowcount == 1:   # observability: topic was NOT understood -> random
            dbm_log_event(conn, "topic_fallback", client["client_id"], client["run_id"], now)
    return conn.execute("SELECT * FROM clients WHERE client_id=?",
                        (client["client_id"],)).fetchone()


def _delay(step_name, rng=None):
    d = config.STEP_DELAY[step_name]
    if config.JITTER_ENABLED and d >= 60:
        d = int(d * (rng or random).uniform(0.85, 1.2))
    return d


async def deliver(conn, transport, client, step_name):
    chat = client["client_id"]
    bcid = client["bcid"]
    if step_name == "image":
        await transport.send_chat_action(chat, "upload_photo", bcid)
        path = _media_path(conn, client["variant_id"])
        return await transport.send_photo(chat, path, None, bcid)
    if step_name == "diagnosis":
        text = _variant(conn, client["variant_id"])["diagnosis"]
    elif step_name == "intro":
        text = content.render_intro(_template(conn, "intro"), client["name"])
    else:
        text = _template(conn, step_name)
    await transport.send_chat_action(chat, "typing", bcid)
    return await transport.send_text(chat, text, bcid)


def _confirm(conn, step_row, client, step_name, mid, now, rng=None):
    """The message is already delivered (irreversible), so ALWAYS mark it sent and
    record sent_log. But only chain the next step + advance state if the client is
    still alive on the same run — a STOP/HANDOFF/re-trigger may have landed during
    the send (TOCTOU); otherwise we'd resurrect a cancelled funnel."""
    nxt = _next_step(step_name)
    cid = client["client_id"]
    with transaction(conn):
        conn.execute("UPDATE steps SET status='sent' WHERE id=?", (step_row["id"],))
        conn.execute(
            "INSERT INTO sent_log(client_id, run_id, step_name, tg_message_id, sent_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(client_id, run_id, step_name) DO UPDATE SET "
            "tg_message_id=COALESCE(excluded.tg_message_id, sent_log.tg_message_id)",
            (cid, step_row["run_id"], step_name, mid, now))
        cur = conn.execute("SELECT state, run_id FROM clients WHERE client_id=?", (cid,)).fetchone()
        alive = (cur is not None and cur["run_id"] == step_row["run_id"]
                 and cur["state"] not in TERMINAL)
        if alive:
            if nxt:
                conn.execute(
                    "INSERT OR IGNORE INTO steps(client_id, run_id, step_name, run_at, status, created_at) "
                    "VALUES (?, ?, ?, ?, 'pending', ?)",
                    (cid, step_row["run_id"], nxt, now + _delay(nxt, rng), now))
            conn.execute("UPDATE clients SET state=?, version=version+1, updated_at=? WHERE client_id=?",
                         (config.STATE_AFTER[step_name], now, cid))


def _skip(conn, step_id, reason):
    conn.execute("UPDATE steps SET status='skipped', last_error=? WHERE id=?", (reason, step_id))


# errors that mean the client is permanently unreachable (not worth retrying)
_PERMANENT = ("forbidden", "blocked", "deactivated", "peer_id_invalid",
              "user_is_blocked", "bot can't initiate", "chat not found")


def _is_permanent(err) -> bool:
    m = str(err).lower()
    return any(k in m for k in _PERMANENT)


def _maybe_abandon(conn, s, now):
    """If a skip leaves the run with no pending steps, park the client in a recoverable
    terminal state so the code word can restart it after the cooldown (no dead-ends)."""
    pend = conn.execute("SELECT COUNT(*) AS c FROM steps WHERE client_id=? AND run_id=? AND status='pending'",
                        (s["client_id"], s["run_id"])).fetchone()["c"]
    if pend:
        return
    c = conn.execute("SELECT state, run_id FROM clients WHERE client_id=?", (s["client_id"],)).fetchone()
    if c and c["run_id"] == s["run_id"] and c["state"] not in TERMINAL:
        conn.execute("UPDATE clients SET state='ABANDONED', version=version+1, updated_at=? WHERE client_id=?",
                     (now, s["client_id"]))


def _halt(conn, s, now, reason):
    with transaction(conn):
        conn.execute("UPDATE steps SET status='skipped', last_error=? WHERE id=?", (reason, s["id"]))
        _maybe_abandon(conn, s, now)


def _block(conn, s, now, reason):
    with transaction(conn):
        conn.execute("UPDATE steps SET status='skipped', last_error=? WHERE id=?", (reason, s["id"]))
        conn.execute("UPDATE steps SET status='cancelled' WHERE client_id=? AND run_id=? AND status='pending'",
                     (s["client_id"], s["run_id"]))
        c = conn.execute("SELECT state, run_id FROM clients WHERE client_id=?", (s["client_id"],)).fetchone()
        if c and c["run_id"] == s["run_id"] and c["state"] not in TERMINAL:
            conn.execute("UPDATE clients SET state='BLOCKED', version=version+1, updated_at=? WHERE client_id=?",
                         (now, s["client_id"]))


async def _process(conn, transport, s, now, rng=None):
    # 1) atomic claim
    if conn.execute("UPDATE steps SET status='sending', attempts=attempts+1 "
                    "WHERE id=? AND status='pending'", (s["id"],)).rowcount != 1:
        return
    step = s["step_name"]
    client = conn.execute("SELECT * FROM clients WHERE client_id=?", (s["client_id"],)).fetchone()

    # 2) guards (re-read fresh state: cancel-vs-send race)
    if client is None or client["run_id"] != s["run_id"]:
        return _skip(conn, s["id"], "stale-run")
    if client["state"] in TERMINAL:
        return _skip(conn, s["id"], "terminal-state")
    if now - s["run_at"] > config.STEP_TTL.get(step, 3600):
        return _halt(conn, s, now, "stale-ttl")
    if client["last_incoming_at"] and now - client["last_incoming_at"] > config.WINDOW_SAFETY:
        return _halt(conn, s, now, "24h-window")

    # 2b) the card steps need a variant: lock the card-time fallback if none detected
    if step in ("image", "diagnosis") and client["variant_id"] is None:
        client = _ensure_variant(conn, client, now, rng)

    # 3) CTA at-most-once: reserve sent_log BEFORE the irreversible send
    if step == "cta":
        try:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO sent_log(client_id, run_id, step_name, tg_message_id, sent_at) "
                    "VALUES (?, ?, ?, NULL, ?)",
                    (client["client_id"], s["run_id"], step, now))
        except sqlite3.IntegrityError:
            # already reserved/sent -> suppress the duplicate (at-most-once)
            conn.execute("UPDATE steps SET status='sent' WHERE id=?", (s["id"],))
            return
        except Exception as e:
            # reservation itself failed (row NOT written) -> retry later, never drop
            conn.execute("UPDATE steps SET status='pending', last_error=? WHERE id=?",
                         (str(e)[:200], s["id"]))
            return

    # 4) send (outside any DB transaction)
    try:
        mid = await deliver(conn, transport, client, step)
    except FileNotFoundError as e:
        return _halt(conn, s, now, f"media-missing:{e}")   # won't self-heal
    except Exception as e:
        if _is_permanent(e):                               # blocked/deactivated -> terminal
            return _block(conn, s, now, str(e)[:200])
        if step == "cta":
            # transient cta: sent_log already reserved (at-most-once). Advance state anyway
            # so a post-CTA hot-lead reply still triggers handoff. _confirm upserts sent_log.
            return _confirm(conn, s, client, "cta", None, now, rng)
        attempts = s["attempts"] + 1  # value after the claim's increment
        if attempts >= config.MAX_SEND_ATTEMPTS:
            return _halt(conn, s, now, f"max-attempts:{str(e)[:120]}")
        backoff = min(config.RETRY_BACKOFF_CAP,
                      config.RETRY_BACKOFF_BASE * (2 ** (attempts - 1)))
        conn.execute("UPDATE steps SET status='pending', run_at=?, last_error=? WHERE id=?",
                     (now + backoff, str(e)[:200], s["id"]))
        return

    # 5) confirm + schedule next (atomic DB)
    _confirm(conn, s, client, step, mid, now, rng)


async def tick(conn, transport, now, rng=None, limit=100):
    # reclaim any row left in 'sending' by a crashed _confirm (ticks never overlap,
    # so a 'sending' row seen here is genuinely stranded, not in-flight).
    startup_sweep(conn)
    due = conn.execute(
        "SELECT id, client_id, run_id, step_name, run_at, attempts FROM steps "
        "WHERE status='pending' AND run_at<=? ORDER BY run_at LIMIT ?", (now, limit)).fetchall()
    for s in due:
        await _process(conn, transport, s, now, rng)
    return len(due)


def startup_sweep(conn):
    """Recover 'sending' rows after a crash: cta stays sent if reserved, else re-queue."""
    for s in conn.execute("SELECT * FROM steps WHERE status='sending'").fetchall():
        reserved = conn.execute(
            "SELECT 1 FROM sent_log WHERE client_id=? AND run_id=? AND step_name=?",
            (s["client_id"], s["run_id"], s["step_name"])).fetchone()
        if s["step_name"] == "cta" and reserved:
            conn.execute("UPDATE steps SET status='sent' WHERE id=?", (s["id"],))
        else:
            conn.execute("UPDATE steps SET status='pending' WHERE id=?", (s["id"],))


def next_pending_run_at(conn):
    row = conn.execute("SELECT MIN(run_at) AS m FROM steps WHERE status='pending'").fetchone()
    return row["m"]
