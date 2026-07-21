#!/usr/bin/env python3
"""Extended QA battery: drives the real engine through many scenarios and edge
cases with precise assertions. Uses an isolated temp DB and a controllable
transport (can inject send failures). No pytest / no network.
    python3 run_qa.py
"""
import asyncio
import os
import random
from collections import defaultdict

from bot import db as dbm, config, funnel, scheduler, variants, content, importer
from bot.clock import VirtualClock
from bot.transport import SimulatedTransport

QA_DB = "/tmp/qa_tarot.sqlite"
FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class Ctrl(SimulatedTransport):
    """SimulatedTransport that can fail sends on demand: fail(kind, chat, attempt)->bool."""
    def __init__(self, clock, fail=None):
        super().__init__(clock, verbose=False)
        self.fail = fail
        self.attempts = defaultdict(int)
        self.alerts = []

    async def send_text(self, chat_id, text, business_connection_id=None):
        self.attempts[(chat_id, "text")] += 1
        if self.fail and self.fail("text", chat_id, self.attempts[(chat_id, "text")]):
            raise RuntimeError("simulated text failure")
        return await super().send_text(chat_id, text, business_connection_id)

    async def send_photo(self, chat_id, image_path, caption=None, business_connection_id=None):
        self.attempts[(chat_id, "photo")] += 1
        if self.fail and self.fail("photo", chat_id, self.attempts[(chat_id, "photo")]):
            raise RuntimeError("simulated photo failure")
        return await super().send_photo(chat_id, image_path, caption, business_connection_id)

    async def notify_operator(self, text):
        self.alerts.append(text)


class H:
    """Scenario harness over an isolated DB + virtual clock."""
    def __init__(self, seed=0, fail=None):
        self.conn = dbm.connect(QA_DB)
        with dbm.transaction(self.conn):
            dbm.wipe(self.conn, dbm.RUNTIME_TABLES)
        for _t in variants.topics(self.conn):
            variants.rebuild_bag(self.conn, _t, random.Random(seed))
        self.clock = VirtualClock(1_700_000_000)
        self.t0 = self.clock.now()
        self.rng = random.Random(seed)
        self.tr = Ctrl(self.clock, fail)

    def at(self, off):
        self.clock.set(self.t0 + off)

    def send(self, cid, text, off=None):
        if off is not None:
            self.at(off)
        return funnel.handle_incoming(self.conn, cid, text, self.clock.now(), bcid="SIM", rng=self.rng)

    async def drain(self, max_ticks=3000):
        for _ in range(max_ticks):
            nxt = scheduler.next_pending_run_at(self.conn)
            if nxt is None:
                return
            if nxt > self.clock.now():
                self.clock.set(nxt)
            await scheduler.tick(self.conn, self.tr, self.clock.now(), self.rng)
        raise RuntimeError("drain did not terminate")

    def state(self, cid):
        r = funnel.get_client(self.conn, cid)
        return r["state"] if r else None

    def sent(self, cid):
        return self.tr.sent_steps_for(cid)

    def pending(self, cid):
        return self.conn.execute(
            "SELECT COUNT(*) AS c FROM steps WHERE client_id=? AND status='pending'",
            (cid,)).fetchone()["c"]

    def step_status(self, cid, name):
        r = self.conn.execute(
            "SELECT status FROM steps WHERE client_id=? AND step_name=? ORDER BY id DESC LIMIT 1",
            (cid, name)).fetchone()
        return r["status"] if r else None

    def close(self):
        self.conn.close()


ORDER = ["greeting", "ask", "working", "intro", "image", "diagnosis", "cta"]


# ---------------------------------------------------------------- scenarios
async def t_happy_multitopic():
    print("\n== happy path across topics; image card == diagnosis card per client ==")
    h = H(seed=1)
    for cid in [10, 11, 12, 13, 14]:
        h.send(cid, "ТАРО", off=0)          # all start at the same time (no TTL staleness)
    for cid in [10, 11, 12, 13, 14]:
        h.send(cid, "Оля, вопрос", off=20)
    await h.drain()
    ok = True
    for cid in [10, 11, 12, 13, 14]:
        s = h.sent(cid)
        kinds = [e["kind"] for e in s]
        if kinds != ["text", "text", "text", "text", "photo", "text", "text"]:
            ok = False
        # image file must belong to the same card as the diagnosis text
        c = funnel.get_client(h.conn, cid)
        v = h.conn.execute("SELECT card_name, diagnosis FROM variants WHERE variant_id=?",
                           (c["variant_id"],)).fetchone()
        _, card_from_text = content.parse_card(v["diagnosis"])
        diag_msg = str(s[5]["content"])
        if content.parse_card(diag_msg)[1] != card_from_text:
            ok = False
    check("5 clients each get 7 msgs in order + text/card consistent", ok)
    check("intro personalised (Оля)", "Оля" in str(h.sent(10)[3]["content"]))
    h.close()


async def t_no_name():
    print("\n== no-name -> intro fallback ==")
    h = H(seed=2)
    h.send(99, "ТАРО")
    await h.drain()
    intro = str(h.sent(99)[3]["content"])
    check("no {name} placeholder leaks", "{name}" not in intro)
    check("7 messages still sent", len(h.sent(99)) == 7)
    h.close()


async def t_stop_variants():
    print("\n== stop word variants terminate ==")
    for i, word in enumerate(["Стоп!", "стоп пожалуйста", "не пиши мне больше"]):
        h = H(seed=3 + i)
        cid = 200 + i
        h.send(cid, "ТАРО")
        h.send(cid, word, off=5)
        check(f"stop via '{word}'", h.state(cid) == "STOPPED")
        check(f"no pending after stop '{word}'", h.pending(cid) == 0)
        await h.drain()
        check(f"nothing sent after stop '{word}'", len(h.sent(cid)) == 0)
        h.close()


async def t_stop_midfunnel():
    print("\n== stop AFTER greeting cancels the rest ==")
    h = H(seed=9)
    cid = 300
    h.send(cid, "ТАРО")
    # let greeting+ask fire (advance to first due, tick once)
    h.at(8 * 60)
    await scheduler.tick(h.conn, h.tr, h.clock.now(), h.rng)
    n_after_greet = len(h.sent(cid))
    h.send(cid, "стоп", off=8 * 60 + 5)
    await h.drain()
    check("stopped mid-funnel", h.state(cid) == "STOPPED")
    check("no further messages after stop", len(h.sent(cid)) == n_after_greet)
    h.close()


async def t_intent_handoff():
    print("\n== buy-intent BEFORE the reading -> early_lead ping, funnel CONTINUES ==")
    h = H(seed=10)
    cid = 400
    h.send(cid, "ТАРО")
    r = h.send(cid, "а сколько стоит расклад? хочу купить", off=30)
    check("early intent -> early_lead action (not handoff)", r["action"] == "early_lead", str(r))
    check("state NOT terminal (funnel alive)", h.state(cid) not in
          ("HANDOFF", "STOPPED", "COMPLETED", "BLOCKED", "ABANDONED"), h.state(cid))
    check("pending steps NOT cancelled", h.pending(cid) >= 1, str(h.pending(cid)))
    await h.drain()
    check("client still receives ALL 7 messages", len(h.sent(cid)) == 7, str(len(h.sent(cid))))
    check("finishes CTA_SENT", h.state(cid) == "CTA_SENT", h.state(cid))
    # after the reading, the reply -> full handoff as before
    r2 = h.send(cid, "беру", off=40 * 60)
    check("post-reading reply -> handoff", r2["action"] == "handoff", str(r2))
    h.close()


async def t_post_cta_handoff():
    print("\n== any reply after CTA -> handoff ==")
    h = H(seed=11)
    cid = 500
    h.send(cid, "ТАРО")
    h.send(cid, "Аня, любовь", off=20)
    await h.drain()
    check("finished CTA_SENT", h.state(cid) == "CTA_SENT")
    r = h.send(cid, "ну и что дальше?", off=40 * 60)
    check("post-CTA reply -> handoff", r["action"] == "handoff", str(r))
    check("state HANDOFF", h.state(cid) == "HANDOFF")
    h.close()


async def t_codeword_edges():
    print("\n== code-word matching + no name pollution ==")
    h = H(seed=12)
    check("'таро' triggers", funnel.get_client(h.conn, 601) is None)
    for cid, txt, exp in [(601, "таро", "triggered"), (602, "хочу ТАРО", "triggered"),
                          (603, "ТАРО!", "triggered")]:
        r = h.send(cid, txt)
        check(f"'{txt}' -> {exp}", r["action"] == exp, str(r))
    check("'тароплан' does NOT trigger", not content.is_code_word("тароплан"))
    check("'гадать' does NOT trigger", not content.is_code_word("гадать"))
    # code word twice while active -> ignored, name not polluted
    h.send(604, "ТАРО")
    r2 = h.send(604, "ТАРО", off=5)
    check("2nd code word ignored", r2["action"] == "ignored", str(r2))
    check("name not set to 'Таро'", funnel.get_client(h.conn, 604)["name"] is None)
    h.close()


async def t_retrigger():
    print("\n== re-trigger only after cooldown ==")
    h = H(seed=13)
    cid = 700
    h.send(cid, "ТАРО")
    h.send(cid, "Ира, деньги", off=20)
    await h.drain()
    c = funnel.get_client(h.conn, cid)
    now = c["updated_at"] + config.RETRIGGER_COOLDOWN + 10
    r = funnel.handle_incoming(h.conn, cid, "ТАРО", now, bcid="SIM")
    check("re-triggered after cooldown", r["action"] == "re-triggered", str(r))
    check("run_id incremented", funnel.get_client(h.conn, cid)["run_id"] == c["run_id"] + 1)
    # too soon
    h2 = H(seed=14)
    h2.send(cid + 1, "ТАРО")
    h2.send(cid + 1, "Лена, любовь", off=20)
    await h2.drain()
    c2 = funnel.get_client(h2.conn, cid + 1)
    r2 = funnel.handle_incoming(h2.conn, cid + 1, "ТАРО", c2["updated_at"] + 60, bcid="SIM")
    check("within cooldown at CTA_SENT: reply-branch not re-trigger", r2["action"] in ("ignored", "handoff"))
    h.close(); h2.close()


async def t_garbage_input():
    print("\n== garbage / emoji / long input doesn't crash ==")
    h = H(seed=15)
    h.send(800, "ТАРО")
    h.send(800, "😀😀😀", off=10)          # emoji only -> no name
    h.send(801, "ТАРО")
    h.send(801, "П", off=10)               # 1 char -> not a valid name
    h.send(802, "ТАРО")
    h.send(802, "Даша " + "очень " * 2000, off=10)  # very long
    await h.drain()
    check("emoji-only -> no vocative", "{name}" not in str(h.sent(800)[3]["content"]))
    check("long msg still yields a name (Даша)", "Даша" in str(h.sent(802)[3]["content"]))
    check("all three completed 7 msgs", all(len(h.sent(c)) == 7 for c in (800, 801, 802)))
    h.close()


async def t_rapid_messages():
    print("\n== rapid multi-message answer -> one funnel, name captured ==")
    h = H(seed=16)
    cid = 900
    h.send(cid, "ТАРО")
    h.send(cid, "привет", off=2)
    h.send(cid, "Марина, про работу", off=4)
    h.send(cid, "ну расскажи", off=6)
    await h.drain()
    check("exactly one funnel (7 msgs)", len(h.sent(cid)) == 7, str(len(h.sent(cid))))
    check("name captured = Марина", "Марина" in str(h.sent(cid)[3]["content"]))
    h.close()


async def t_transient_failure():
    print("\n== transient send failure -> retried, funnel completes ==")
    h = H(seed=17, fail=lambda kind, chat, att: kind == "text" and att == 1)  # 1st text fails
    cid = 1000
    h.send(cid, "ТАРО")
    h.send(cid, "Ника, любовь", off=20)
    await h.drain()
    check("greeting eventually sent (retry)", h.step_status(cid, "greeting") == "sent")
    check("all 7 delivered despite 1 failure", len(h.sent(cid)) == 7, str(len(h.sent(cid))))
    h.close()


async def t_transient_forever():
    print("\n== transient-forever send -> bounded retries, ABANDONED (re-triggerable) ==")
    h = H(seed=18, fail=lambda kind, chat, att: True)  # every send raises (transient)
    cid = 1100
    h.send(cid, "ТАРО")
    await h.drain()
    check("greeting skipped after max attempts", h.step_status(cid, "greeting") == "skipped")
    check("funnel halted: no 'ask' scheduled", h.step_status(cid, "ask") is None)
    check("nothing delivered", len(h.sent(cid)) == 0)
    check("client ABANDONED (recoverable, not stuck)", h.state(cid) == "ABANDONED")
    h.close()


async def t_permanent_block():
    print("\n== blocked/deactivated user -> BLOCKED immediately, no hammering ==")
    h = H(seed=32)

    async def boom(*a, **k):
        raise RuntimeError("Forbidden: bot was blocked by the user")
    h.tr.send_text = boom
    h.tr.send_photo = boom
    cid = 1800
    h.send(cid, "ТАРО")
    await h.drain()
    check("client BLOCKED", h.state(cid) == "BLOCKED")
    att = h.conn.execute("SELECT attempts FROM steps WHERE client_id=? AND step_name='greeting'",
                        (cid,)).fetchone()["attempts"]
    check("no wasteful retries (attempts<=1)", att <= 1, f"attempts={att}")
    h.close()


async def t_name_extraction():
    print("\n== name extraction (fillers rejected, markers work, hyphen) ==")
    cases = [("Маша, что с деньгами", "Маша"), ("Добрый день, помогите с любовью", None),
             ("Что меня ждет?", None), ("Спасибо большое", None),
             ("Здравствуйте, меня зовут Маша", "Маша"), ("Привет, я Оля", "Оля"),
             ("Анна-Мария", "Анна-Мария"), ("😀😀😀", None), ("П", None),
             # affirmations are never names
             ("Да, про деньги", None), ("Нет", None), ("не скажу", None), ("Ага, понял", None),
             # topic answers are never names
             ("Деньги", None), ("любовь и деньги", None), ("Работа", None),
             # ...but an explicit «меня зовут X» keeps a real name that doubles as a word
             ("Меня зовут Любовь", "Любовь"), ("Я Роман", "Роман")]
    for text, exp in cases:
        got = content.extract_name(text)
        check(f"extract_name({text!r})=={exp!r}", got == exp, f"got {got!r}")


async def t_classifier_boundaries():
    print("\n== stop/intent word-boundary + negation ==")
    check("'пожалуйста не пишите' NOT stop", not content.is_stop("пожалуйста не пишите так формально"))
    check("'не пиши мне' IS stop", content.is_stop("не пиши мне"))
    check("'не хочу расклад' NOT intent", not content.has_intent("не хочу никакой расклад"))
    # «хочу расклад» is a natural ANSWER to the ask step, deliberately NOT intent
    check("'хочу расклад' NOT intent (it's an answer, not buying)",
          not content.has_intent("хочу расклад"))
    check("'хочу расклад про любовь' NOT intent", not content.has_intent("хочу расклад про любовь"))
    check("'готова оплатить' IS intent", content.has_intent("готова оплатить хоть сейчас"))
    check("'нет не куплю' NOT intent", not content.has_intent("нет не куплю"))
    check("'красивая сцена' NOT intent", not content.has_intent("красивая сцена из фильма"))


async def t_postcta_codeword():
    print("\n== post-CTA reply CONTAINING code word -> handoff (not ignored) ==")
    h = H(seed=30)
    cid = 1600
    h.send(cid, "ТАРО")
    h.send(cid, "Вера, любовь", off=20)
    await h.drain()
    check("CTA_SENT", h.state(cid) == "CTA_SENT")
    r = h.send(cid, "ТАРО, хочу оплатить!", off=3600)  # within cooldown
    check("code-word reply after CTA -> handoff", r["action"] == "handoff", str(r))
    check("state HANDOFF", h.state(cid) == "HANDOFF")
    h.close()


async def t_skip_then_retrigger():
    print("\n== abandoned funnel is re-triggerable after cooldown ==")
    h = H(seed=31)
    cid = 1700
    h.send(cid, "ТАРО")
    h.at(3 * 60 * 60)  # long outage: greeting exceeds TTL
    await scheduler.tick(h.conn, h.tr, h.clock.now(), h.rng)
    check("greeting skipped", h.step_status(cid, "greeting") == "skipped")
    check("client ABANDONED (not stuck)", h.state(cid) == "ABANDONED")
    now = funnel.get_client(h.conn, cid)["updated_at"] + config.RETRIGGER_COOLDOWN + 10
    r = funnel.handle_incoming(h.conn, cid, "ТАРО", now, bcid="SIM")
    check("re-triggerable after cooldown", r["action"] == "re-triggered", str(r))
    h.close()


async def t_cta_transient():
    print("\n== transient CTA failure still advances to CTA_SENT (handoff not disabled) ==")
    h = H(seed=33, fail=lambda kind, chat, att: False)
    cid = 1900
    h.send(cid, "ТАРО")
    h.send(cid, "Оля, любовь", off=20)
    # fail only the cta text send
    orig = h.tr.send_text

    async def maybe_fail(chat_id, text, business_connection_id=None):
        if text.startswith("Есть два пути"):
            raise RuntimeError("timeout")
        return await orig(chat_id, text, business_connection_id)
    h.tr.send_text = maybe_fail
    await h.drain()
    check("state advanced to CTA_SENT despite cta failure", h.state(cid) == "CTA_SENT", h.state(cid))
    r = h.send(cid, "да, хочу!", off=40 * 60)
    check("post-CTA reply still -> handoff", r["action"] == "handoff", str(r))
    h.close()


async def t_owner_table():
    print("\n== business_connections table + app.py owner SQL (live wiring) ==")
    conn = dbm.connect(QA_DB)
    sql = ("INSERT INTO business_connections(business_connection_id, owner_user_id, can_reply, "
           "can_read, is_enabled, connected_at) VALUES (?,?,?,?,?,?) "
           "ON CONFLICT(business_connection_id) DO UPDATE SET owner_user_id=excluded.owner_user_id, "
           "can_reply=excluded.can_reply, is_enabled=excluded.is_enabled")
    with dbm.transaction(conn):
        conn.execute(sql, ("bc1", 555, 1, 1, 1, 1_700_000_000))       # first connect
    with dbm.transaction(conn):
        conn.execute(sql, ("bc1", 777, 1, 1, 1, 1_700_000_001))       # reconnect (owner upsert)
    owner = conn.execute("SELECT owner_user_id FROM business_connections WHERE business_connection_id=?",
                        ("bc1",)).fetchone()["owner_user_id"]
    check("owner stored & upserted (no 'no such table')", owner == 777, str(owner))
    conn.close()


async def t_restart_recovery():
    print("\n== restart recovery of a 'sending' row (crash between send & confirm) ==")
    h = H(seed=19)
    cid = 1200
    h.send(cid, "ТАРО")
    # simulate a crash: a greeting row stuck in 'sending' with NO sent_log
    row = h.conn.execute("SELECT * FROM steps WHERE client_id=? AND status='pending'", (cid,)).fetchone()
    h.at(7 * 60)
    h.conn.execute("UPDATE steps SET status='sending' WHERE id=?", (row["id"],))
    await scheduler.tick(h.conn, h.tr, h.clock.now(), h.rng)  # tick's sweep should re-queue + send
    check("stranded greeting recovered & sent", h.step_status(cid, "greeting") == "sent")
    check("greeting delivered exactly once", len(h.sent(cid)) == 1)
    # cta reserved-but-'sending' must NOT resend
    h2 = H(seed=20)
    cid2 = 1201
    h2.conn.execute("INSERT INTO clients(client_id,state,variant_id,run_id,last_incoming_at,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?)", (cid2, "DIAGNOSED", 0, 1, h2.clock.now(), h2.clock.now(), h2.clock.now()))
    h2.conn.execute("INSERT INTO steps(client_id,run_id,step_name,run_at,status,created_at) "
                    "VALUES (?,?,?,?,?,?)", (cid2, 1, "cta", h2.clock.now(), "sending", h2.clock.now()))
    h2.conn.execute("INSERT INTO sent_log(client_id,run_id,step_name,tg_message_id,sent_at) "
                    "VALUES (?,?,?,?,?)", (cid2, 1, "cta", None, h2.clock.now()))
    scheduler.startup_sweep(h2.conn)
    check("reserved cta marked sent, not resent", h2.step_status(cid2, "cta") == "sent")
    check("no cta re-delivered", len(h2.sent(cid2)) == 0)
    h.close(); h2.close()


async def t_atomic_claim():
    print("\n== atomic claim + unique step ==")
    h = H(seed=21)
    cid = 1300
    h.send(cid, "ТАРО")
    row = h.conn.execute("SELECT id FROM steps WHERE client_id=? AND status='pending'", (cid,)).fetchone()
    r1 = h.conn.execute("UPDATE steps SET status='sending' WHERE id=? AND status='pending'", (row["id"],)).rowcount
    r2 = h.conn.execute("UPDATE steps SET status='sending' WHERE id=? AND status='pending'", (row["id"],)).rowcount
    check("claim succeeds once, fails second time", r1 == 1 and r2 == 0, f"{r1},{r2}")
    dup = h.conn.execute("INSERT OR IGNORE INTO steps(client_id,run_id,step_name,run_at,status,created_at) "
                         "VALUES (?,?,?,?,?,?)", (cid, 1, "greeting", 0, "pending", 0)).rowcount
    check("UNIQUE(client,run,step) blocks duplicate", dup == 0)
    h.close()


async def t_ttl_and_window():
    print("\n== stale TTL + 24h window guards ==")
    h = H(seed=22)
    cid = 1400
    h.send(cid, "ТАРО")
    row = h.conn.execute("SELECT * FROM steps WHERE client_id=? AND status='pending'", (cid,)).fetchone()
    # make greeting long overdue (beyond TTL); tick 'now' far ahead
    h.at(3 * 60 * 60)
    await scheduler.tick(h.conn, h.tr, h.clock.now(), h.rng)
    check("stale greeting skipped (not sent late)", h.step_status(cid, "greeting") == "skipped")
    check("no 'ask' scheduled after stale skip", h.step_status(cid, "ask") is None)
    # 24h window
    h2 = H(seed=23)
    cid2 = 1401
    h2.send(cid2, "ТАРО")
    h2.conn.execute("UPDATE clients SET last_incoming_at=? WHERE client_id=?",
                    (h2.clock.now() - 25 * 60 * 60, cid2))  # last inbound 25h ago
    h2.at(7 * 60)
    await scheduler.tick(h2.conn, h2.tr, h2.clock.now(), h2.rng)
    check("send skipped outside 24h window", h2.step_status(cid2, "greeting") == "skipped")
    h.close(); h2.close()


async def t_distribution():
    print("\n== even-random distribution over 3 full cycles WITHIN each topic ==")
    h = H(seed=24)
    for t in variants.topics(h.conn):
        n = h.conn.execute("SELECT COUNT(*) AS c FROM variants WHERE topic=?", (t,)).fetchone()["c"]
        counts = defaultdict(int)
        for _ in range(n * 3):
            counts[variants.draw_variant(h.conn, t, h.rng)] += 1
        own = {r["variant_id"] for r in h.conn.execute(
            "SELECT variant_id FROM variants WHERE topic=?", (t,))}
        check(f"{t}: all {n} used exactly 3x", len(counts) == n and set(counts.values()) == {3},
              str(sorted(set(counts.values()))))
        check(f"{t}: draws never leave the topic", set(counts) <= own)
    h.close()


async def t_all_card_image_consistency():
    print("\n== all 66: diagnosis card == image's card (no cross-card image) ==")
    h = H(seed=25)
    rows = h.conn.execute(
        "SELECT variant_id, card_name, diagnosis, media_key FROM variants").fetchall()
    img2card = defaultdict(set)
    mism = 0
    for r in rows:
        if content.parse_card(r["diagnosis"])[1] != r["card_name"]:
            mism += 1
        img2card[r["media_key"]].add(r["card_name"])
    shared = [k for k, cs in img2card.items() if len(cs) != 1]
    check("0 text/card mismatches over all 66", mism == 0)
    check("no image shared across different cards", not shared, str(shared))
    h.close()


async def t_operator_alert():
    print("\n== operator alerts: early ping mid-funnel, hot-lead after the reading ==")
    h = H(seed=26)
    cid = 1500
    h.send(cid, "ТАРО")
    r = h.send(cid, "хочу оплатить", off=20)   # BEFORE the reading -> early ping, funnel alive
    if r["action"] in ("handoff", "early_lead"):
        await h.tr.notify_operator(f"lead {cid}")
    check("early intent -> early_lead (funnel alive)", r["action"] == "early_lead", str(r))
    check("early operator ping recorded", len(h.tr.alerts) == 1)
    await h.drain()
    check("client got the full funnel anyway", len(h.sent(cid)) == 7, str(len(h.sent(cid))))
    r2 = h.send(cid, "готова оплатить", off=60 * 60)   # AFTER the reading -> full handoff
    if r2["action"] == "handoff":
        await h.tr.notify_operator(f"hot {cid}")
    check("post-reading intent -> handoff", r2["action"] == "handoff", str(r2))
    check("hot-lead alert recorded", len(h.tr.alerts) == 2)
    h.close()


async def main():
    if os.path.exists(QA_DB):
        os.remove(QA_DB)
    conn = dbm.connect(QA_DB)
    dbm.init(conn)
    conn.close()
    importer.run(db_path=QA_DB, media_dir=config.MEDIA_DIR, verbose=False)

    for t in [t_happy_multitopic, t_no_name, t_stop_variants, t_stop_midfunnel,
              t_intent_handoff, t_post_cta_handoff, t_codeword_edges, t_retrigger,
              t_garbage_input, t_rapid_messages, t_transient_failure, t_transient_forever,
              t_permanent_block, t_owner_table, t_restart_recovery, t_atomic_claim, t_ttl_and_window,
              t_distribution, t_all_card_image_consistency, t_operator_alert,
              t_name_extraction, t_classifier_boundaries, t_postcta_codeword,
              t_skip_then_retrigger, t_cta_transient]:
        await t()

    print("\n" + "=" * 56)
    if FAILS:
        print(f"QA FAILED: {len(FAILS)} -> {FAILS}")
        raise SystemExit(1)
    print("ALL QA CASES PASSED ✅")


if __name__ == "__main__":
    asyncio.run(main())
