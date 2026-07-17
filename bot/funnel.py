"""Funnel state machine (pure DB; no network).

handle_incoming() classifies an inbound message and mutates state:
  code word  -> start (or re-trigger after cooldown) the timed drip
  stop word  -> STOPPED + cancel pending
  buy intent -> HANDOFF + cancel pending (caller notifies the operator)
  otherwise  -> capture the client's name+question (once), for the r6 intro,
                and detect their TOPIC (любовь/финансы/будущее) to pick the card
                from the right category (per-topic bag, even-random within it).

The variant is NOT drawn at trigger time any more: it locks when a topic is
detected in any client message before the card, or (if none) the scheduler's
card-time fallback assigns a random topic. variant_id doubles as the lock.
The timed chain itself is driven by the scheduler; this module only schedules
the FIRST step (greeting at +7 min) on trigger.
"""
from . import config, content
from .db import transaction, log_event
from .variants import draw_variant


def get_client(conn, client_id):
    return conn.execute("SELECT * FROM clients WHERE client_id = ?", (client_id,)).fetchone()


def should_mark_read(conn, client_id):
    """Mark an inbound message read ONLY while the client is in an active funnel.
    After a terminal state (HANDOFF/STOPPED/...) the bot stops touching the chat, so
    messages stay UNREAD and the human operator sees the unread badge and reads herself."""
    c = get_client(conn, client_id)
    return c is not None and c["state"] not in config.TERMINAL_STATES


def touch_incoming(conn, client_id, bcid, now):
    # NB: must NOT touch updated_at — the re-trigger cooldown reads it.
    # A fresh bcid (Telegram rotates it on reconnect) wins over the stored one.
    with transaction(conn):
        conn.execute(
            "UPDATE clients SET last_incoming_at = ?, bcid = COALESCE(?, bcid) "
            "WHERE client_id = ?",
            (now, bcid, client_id))


def _schedule_first(conn, client_id, run_id, now):
    conn.execute(
        "INSERT OR IGNORE INTO steps(client_id, run_id, step_name, run_at, status, created_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?)",
        (client_id, run_id, config.FIRST_STEP, now + config.STEP_DELAY[config.FIRST_STEP], now))


def start_or_reset(conn, client_id, bcid, now, rng=None):
    c = get_client(conn, client_id)
    if c is None:
        with transaction(conn):
            conn.execute(
                "INSERT INTO clients(client_id, bcid, state, variant_id, topic, run_id, "
                "triggered_at, last_incoming_at, created_at, updated_at) "
                "VALUES (?, ?, 'TRIGGERED', NULL, NULL, 1, ?, ?, ?, ?)",
                (client_id, bcid, now, now, now, now))
            _schedule_first(conn, client_id, 1, now)
            log_event(conn, "triggered", client_id, 1, now)
        return "triggered"

    if (c["state"] in config.TERMINAL_STATES or c["state"] == "CTA_SENT") \
            and (now - c["updated_at"]) >= config.RETRIGGER_COOLDOWN:
        new_run = c["run_id"] + 1
        with transaction(conn):
            conn.execute("UPDATE steps SET status='cancelled' "
                         "WHERE client_id=? AND run_id=? AND status='pending'",
                         (client_id, c["run_id"]))
            conn.execute(
                "UPDATE clients SET state='TRIGGERED', variant_id=NULL, topic=NULL, "
                "run_id=?, name=NULL, question=NULL, triggered_at=?, last_incoming_at=?, "
                "version=version+1, updated_at=? WHERE client_id=?",
                (new_run, now, now, now, client_id))
            _schedule_first(conn, client_id, new_run, now)
            log_event(conn, "triggered", client_id, new_run, now)
        return "re-triggered"

    return "ignored"  # already in an active funnel, or cooldown not elapsed


def maybe_set_topic(conn, client_id, text, now, rng=None):
    """Detect the client's topic from any pre-card message; on first detection draw
    the variant from that topic's bag and lock both (variant_id IS the lock — set
    either here or by the scheduler's card-time fallback, never changed after)."""
    c = get_client(conn, client_id)
    if c is None or c["state"] in config.TERMINAL_STATES or c["variant_id"] is not None:
        return None
    exclude = c["name"] or content.extract_name(text)
    topic = content.detect_topic(text, exclude_name=exclude)
    if topic is None:
        return None
    vid = draw_variant(conn, topic, rng)   # outside the txn: rebuild_bag opens its own
    with transaction(conn):
        cur = conn.execute(
            "UPDATE clients SET topic=?, variant_id=?, version=version+1, updated_at=? "
            "WHERE client_id=? AND variant_id IS NULL",
            (topic, vid, now, client_id))
        if cur.rowcount == 1:   # observability: detected from the client's own words
            log_event(conn, "topic_detected", client_id, c["run_id"], now)
    return topic if cur.rowcount == 1 else None


def capture_answer(conn, client_id, text, now):
    """Capture name+question from the client's reply. Keeps refining the name across
    messages (early answers, greeting-then-name) until a name is locked in or the
    personalised intro has already been sent."""
    c = get_client(conn, client_id)
    if c is None or c["state"] in config.TERMINAL_STATES:
        return "ignored"
    if c["name"] is not None:
        return "noted"  # name already locked in
    intro_sent = conn.execute(
        "SELECT 1 FROM sent_log WHERE client_id=? AND run_id=? AND step_name='intro'",
        (client_id, c["run_id"])).fetchone()
    if intro_sent:
        return "noted"  # too late to personalise
    name = content.extract_name(text)
    with transaction(conn):
        conn.execute(
            "UPDATE clients SET name=COALESCE(?, name), question=?, "
            "version=version+1, updated_at=? WHERE client_id=? AND name IS NULL",
            (name, text, now, client_id))
    return "captured"


def cancel_pending(conn, client_id, run_id):
    conn.execute("UPDATE steps SET status='cancelled' "
                 "WHERE client_id=? AND run_id=? AND status='pending'",
                 (client_id, run_id))


def _terminate(conn, client_id, state, now, event=None):
    c = get_client(conn, client_id)
    if c is None:
        return
    with transaction(conn):
        cancel_pending(conn, client_id, c["run_id"])
        conn.execute("UPDATE clients SET state=?, version=version+1, updated_at=? WHERE client_id=?",
                     (state, now, client_id))
        if event:
            log_event(conn, event, client_id, c["run_id"], now)


def owner_reply_is_own_send(conn, client_id, msg_id):
    """True if msg_id is a message THIS bot already sent on the owner's behalf.
    Lets the caller tell the bot's own on-behalf send (which Telegram may echo back
    as a business_message from the owner) apart from the owner genuinely typing herself,
    so auto-pause never fires on the bot's own messages."""
    if msg_id is None:
        return False
    return conn.execute(
        "SELECT 1 FROM sent_log WHERE client_id=? AND tg_message_id=?",
        (client_id, msg_id)).fetchone() is not None


def owner_took_over(conn, client_id, now):
    """The account owner wrote in this chat herself -> a human took the conversation.
    Cancel the pending drip and mark HANDOFF so the bot stops auto-sending here.
    No-op if the chat is not an active funnel client (e.g. her ordinary contacts)."""
    c = get_client(conn, client_id)
    if c is None or c["state"] in config.TERMINAL_STATES:
        return False
    with transaction(conn):
        cancel_pending(conn, client_id, c["run_id"])
        conn.execute("UPDATE clients SET state='HANDOFF', version=version+1, updated_at=? "
                     "WHERE client_id=?", (now, client_id))
    return True


def handle_incoming(conn, client_id, text, now, bcid=None, msg_id=None, rng=None):
    """Classify + mutate. Returns {'action': ...}. Network side effects (operator
    alert, read receipt) are left to the async caller based on the returned action."""
    if get_client(conn, client_id) is not None:
        touch_incoming(conn, client_id, bcid, now)

    if content.is_stop(text):
        _terminate(conn, client_id, "STOPPED", now)
        return {"action": "stopped"}

    # code word: start / re-trigger. If it can't (active client / cooldown not elapsed),
    # fall through so a CTA_SENT/engaged reply still reaches the hand-off branch.
    is_cw = content.is_code_word(text)
    if is_cw:
        action = start_or_reset(conn, client_id, bcid, now, rng)
        if action != "ignored":
            # the trigger message itself may already carry the topic («ТАРО про любовь»)
            maybe_set_topic(conn, client_id, text, now, rng)
            return {"action": action}

    c = get_client(conn, client_id)
    if c and c["state"] not in config.TERMINAL_STATES and c["state"] != "NEW":
        # after the CTA, ANY reply is a hot lead; buy-intent at any active step -> handoff
        if c["state"] == "CTA_SENT" or content.has_intent(text):
            _terminate(conn, client_id, "HANDOFF", now, event="hot_lead")
            return {"action": "handoff", "client_id": client_id, "name": c["name"]}
        # topic can arrive in any message until the card locks it (incl. repeated code word)
        maybe_set_topic(conn, client_id, text, now, rng)
        if is_cw:
            return {"action": "ignored"}   # active client just repeated the code word
        return {"action": capture_answer(conn, client_id, text, now)}

    return {"action": "ignored"}
