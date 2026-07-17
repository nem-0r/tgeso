#!/usr/bin/env python3
"""Self-checks: import fidelity, exact funnel sequence, image+text delivery,
verbatim diagnosis, name substitution + fallback, even-random distribution,
idempotency. No pytest needed:  python run_tests.py
"""
import asyncio
import random

from bot import db as dbm
from bot import config, simulate, scheduler, variants, content, funnel
from bot.clock import VirtualClock
from bot.transport import SimulatedTransport

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


async def test_sequence_and_media():
    print("\n== funnel sequence + image + verbatim diagnosis ==")
    cid = 111111
    transport, _ = await simulate.main(client_id=cid, seed=3, verbose=False)
    sent = transport.sent_steps_for(cid)
    kinds = [e["kind"] for e in sent]
    check("7 messages sent", len(sent) == 7, f"got {len(sent)}")
    check("order text,text,text,text,PHOTO,text,text",
          kinds == ["text", "text", "text", "text", "photo", "text", "text"], str(kinds))
    # intro (4th) personalised with the captured name 'Маша'
    check("intro has captured name 'Маша'", "Маша" in str(sent[3]["content"]))
    # photo (5th) references an existing file
    check("photo file exists", isinstance(sent[4]["content"], dict))
    # diagnosis (6th) must equal the assigned variant's diagnosis verbatim
    conn = dbm.connect()
    c = conn.execute("SELECT variant_id FROM clients WHERE client_id=?", (cid,)).fetchone()
    v = conn.execute("SELECT diagnosis, card_name FROM variants WHERE variant_id=?",
                     (c["variant_id"],)).fetchone()
    check("diagnosis verbatim", str(sent[5]["content"]) == v["diagnosis"])
    conn.close()


async def test_timings():
    print("\n== strict timings (relative to previous step) ==")
    cid = 222222
    transport, timeline = await simulate.main(client_id=cid, seed=1, verbose=False,
                                              messages=[(0, "ТАРО"), (30, "Игорь, про работу")])
    sent = transport.sent_steps_for(cid)
    codeword_t = min(t for (t, d, _) in timeline if d == "IN")  # real anchor: code-word arrival
    offs = [round(e["t"] - codeword_t) for e in sent]
    check("greeting at +7:00", offs[0] == 420, str(offs))
    check("working at +12:00", offs[2] == 720, str(offs))
    check("intro at +27:00", offs[3] == 1620, str(offs))
    check("photo+diagnosis at +27:00", offs[4] == 1620 and offs[5] == 1620, str(offs))
    check("cta at +27:30", offs[6] == 1650, str(offs))


async def test_name_fallback():
    print("\n== no-name fallback (intro without vocative, no leftover placeholder) ==")
    cid = 333333
    transport, _ = await simulate.main(client_id=cid, seed=5, verbose=False,
                                       messages=[(0, "ТАРО")])  # never sends a name
    sent = transport.sent_steps_for(cid)
    intro = str(sent[3]["content"])
    check("no '{name}' left in intro", "{name}" not in intro)
    check("intro starts capitalised", intro[:1].isupper(), intro[:20])


def test_distribution():
    print("\n== even-random distribution WITHIN each topic (per-topic bags) ==")
    conn = dbm.connect()
    tops = variants.topics(conn)
    check("3 topics in DB", len(tops) == 3, str(tops))
    for t in tops:
        variants.rebuild_bag(conn, t, random.Random(9))
        n = conn.execute("SELECT COUNT(*) AS c FROM variants WHERE topic=?", (t,)).fetchone()["c"]
        counts = {}
        for _ in range(n * 2):
            vid = variants.draw_variant(conn, t, random.Random(0))
            counts[vid] = counts.get(vid, 0) + 1
        own = {r["variant_id"] for r in conn.execute(
            "SELECT variant_id FROM variants WHERE topic=?", (t,))}
        check(f"{t}: all {n} used exactly twice over 2 bags",
              len(counts) == n and set(counts.values()) == {2}, str(sorted(set(counts.values()))))
        check(f"{t}: every draw stays inside the topic", set(counts) <= own)
    conn.close()


def test_topic_detection():
    print("\n== topic detection: synonyms, name traps, priorities ==")
    T = content
    cases = [
        # (text, exclude_name, expected)
        ("Меня зовут Саша, расскажи про мою половинку", None, T.TOPIC_LOVE),
        ("что с любовью?", None, T.TOPIC_LOVE),
        ("посмотри отношения с мужем", None, T.TOPIC_LOVE),
        ("вернется ли бывший", None, T.TOPIC_LOVE),
        ("когда выйду замуж", None, T.TOPIC_LOVE),
        ("про личную жизнь", None, T.TOPIC_LOVE),
        ("что с деньгами", None, T.TOPIC_MONEY),
        ("Игорь, про работу", None, T.TOPIC_MONEY),
        ("посмотри финансы и зп", None, T.TOPIC_MONEY),
        ("бизнес пойдет ли в гору", None, T.TOPIC_MONEY),
        ("долги задушили, что делать", None, T.TOPIC_MONEY),
        ("что меня ждет", None, T.TOPIC_FUTURE),
        ("расскажи про будущее", None, T.TOPIC_FUTURE),
        ("какая у меня судьба", None, T.TOPIC_FUTURE),
        ("что будет дальше", None, T.TOPIC_FUTURE),
        # priorities: specific beats the generic Будущее
        ("будущий муж — кто он?", None, T.TOPIC_LOVE),
        ("финансовое будущее", None, T.TOPIC_MONEY),
        ("что ждет в отношениях", None, T.TOPIC_LOVE),
        # both specific -> earliest mention wins
        ("про любовь и деньги", None, T.TOPIC_LOVE),
        ("сначала деньги, потом любовь", None, T.TOPIC_MONEY),
        # name traps: names must never read as topics
        ("Меня зовут Люба", "Люба", None),
        ("Меня зовут Любовь", "Любовь", None),
        ("Я Денис", "Денис", None),
        ("любой вопрос", None, None),
        ("мне любопытно", None, None),
        # no topic at all
        ("Меня зовут Аня", None, None),
        ("привет как дела", None, None),
        ("", None, None),
        # ё normalisation + case
        ("что ждЁт меня", None, T.TOPIC_FUTURE),
        ("ЛЮБОВЬ", None, T.TOPIC_LOVE),
    ]
    for text, excl, exp in cases:
        got = content.detect_topic(text, exclude_name=excl)
        check(f"detect({text[:36]!r})=={exp and exp[:12]!r}", got == exp, f"got {got!r}")


async def test_topic_flow():
    print("\n== topic flow: lock from trigger/answer, lock-once, fallback, retrigger reset ==")
    conn = dbm.connect()

    def topic_of(cid):
        c = conn.execute("SELECT topic, variant_id FROM clients WHERE client_id=?", (cid,)).fetchone()
        vt = conn.execute("SELECT topic FROM variants WHERE variant_id=?", (c["variant_id"],)).fetchone()
        return c["topic"], vt["topic"] if vt else None

    # (a) topic inside the TRIGGER message itself
    transport, _ = await simulate.main(client_id=910001, seed=11, verbose=False,
                                       messages=[(0, "ТАРО что скажешь про любовь"),
                                                 (40, "Меня Маша зовут")])
    ct, vt = topic_of(910001)
    check("trigger-message topic locks Любовь", ct == content.TOPIC_LOVE, f"{ct!r}")
    check("variant drawn FROM Любовь bag", vt == content.TOPIC_LOVE, f"{vt!r}")
    check("full chain still 7 messages", len(transport.sent_steps_for(910001)) == 7)

    # (b) topic in the ANSWER (name+topic in one message)
    transport, _ = await simulate.main(client_id=910002, seed=12, verbose=False,
                                       messages=[(0, "ТАРО"),
                                                 (40, "Меня зовут Игорь, что с деньгами?")])
    ct, vt = topic_of(910002)
    check("answer topic locks Финансы", ct == content.TOPIC_MONEY and vt == content.TOPIC_MONEY,
          f"{ct!r}/{vt!r}")

    # (c) lock-once: second topic mention does NOT switch
    transport, _ = await simulate.main(client_id=910003, seed=13, verbose=False,
                                       messages=[(0, "ТАРО"), (30, "про деньги"),
                                                 (60, "нет, лучше про любовь")])
    ct, vt = topic_of(910003)
    check("first detected topic (Финансы) is locked", ct == content.TOPIC_MONEY and vt == content.TOPIC_MONEY,
          f"{ct!r}/{vt!r}")

    # (d) NO topic ever -> card-time fallback assigns a random topic, funnel completes
    transport, _ = await simulate.main(client_id=910004, seed=14, verbose=False,
                                       messages=[(0, "ТАРО"), (40, "Аня")])
    ct, vt = topic_of(910004)
    check("fallback assigned some topic", ct in content.TOPICS, f"{ct!r}")
    check("fallback variant matches its topic", vt == ct, f"{vt!r} vs {ct!r}")
    check("fallback funnel still 7 messages", len(transport.sent_steps_for(910004)) == 7)

    # (d2) «хочу расклад про любовь» as the ANSWER: funnel continues (no false handoff),
    # topic locks Любовь — this is why «хочу расклад» was removed from INTENT_WORDS
    transport, _ = await simulate.main(client_id=910006, seed=16, verbose=False,
                                       messages=[(0, "ТАРО"),
                                                 (40, "Меня зовут Вера, хочу расклад про любовь")])
    ct, vt = topic_of(910006)
    c = conn.execute("SELECT state, name FROM clients WHERE client_id=?", (910006,)).fetchone()
    check("«хочу расклад про любовь» does NOT handoff (funnel completed)",
          c["state"] == "CTA_SENT", c["state"])
    check("…and locks Любовь", ct == content.TOPIC_LOVE and vt == content.TOPIC_LOVE, f"{ct!r}/{vt!r}")
    check("…name Вера captured", c["name"] == "Вера", f"{c['name']!r}")
    check("…all 7 delivered", len(transport.sent_steps_for(910006)) == 7)

    # (d3) «Да, про деньги» — «Да» is NOT the name, topic still locks Финансы,
    # intro degrades gracefully (no vocative), full chain delivered
    transport, _ = await simulate.main(client_id=910007, seed=17, verbose=False,
                                       messages=[(0, "ТАРО"), (40, "Да, про деньги")])
    ct, vt = topic_of(910007)
    c = conn.execute("SELECT name FROM clients WHERE client_id=?", (910007,)).fetchone()
    check("«Да» not captured as a name", c["name"] is None, repr(c["name"]))
    check("…but topic Финансы locked", ct == content.TOPIC_MONEY and vt == content.TOPIC_MONEY,
          f"{ct!r}/{vt!r}")
    intro = str(transport.sent_steps_for(910007)[3]["content"])
    check("…intro has no «Да,» vocative", not intro.startswith("Да"), intro[:30])
    check("…all 7 delivered", len(transport.sent_steps_for(910007)) == 7)

    # (e) name trap in flow: «Меня зовут Люба» must NOT lock Любовь early
    transport, _ = await simulate.main(client_id=910005, seed=15, verbose=False,
                                       messages=[(0, "ТАРО"), (40, "Меня зовут Люба")])
    c = conn.execute("SELECT name, topic FROM clients WHERE client_id=?", (910005,)).fetchone()
    check("name Люба captured", c["name"] == "Люба", f"{c['name']!r}")
    # topic comes only from fallback => it is whatever the fallback drew; the point is
    # the LOCK happened at card time, not from the name. Verify intro used the name:
    sent = transport.sent_steps_for(910005)
    check("intro personalised with 'Люба'", "Люба" in str(sent[3]["content"]))

    # (f) re-trigger resets topic and redraws in the NEW topic
    # NB: each simulate.main() wipes runtime tables, so re-use the LAST client (910005).
    cfull = conn.execute("SELECT updated_at, run_id FROM clients WHERE client_id=?", (910005,)).fetchone()
    later = cfull["updated_at"] + config.RETRIGGER_COOLDOWN + 5
    res = funnel.handle_incoming(conn, 910005, "ТАРО теперь про будущее", later,
                                 bcid="SIM", rng=random.Random(1))
    check("re-triggered after cooldown", res["action"] == "re-triggered", str(res))
    ct, vt = topic_of(910005)
    check("new run locked Будущее", ct == content.TOPIC_FUTURE and vt == content.TOPIC_FUTURE,
          f"{ct!r}/{vt!r}")
    c2 = conn.execute("SELECT name FROM clients WHERE client_id=?", (910005,)).fetchone()
    check("name reset on re-trigger", c2["name"] is None, f"{c2['name']!r}")
    conn.close()


async def test_idempotency():
    print("\n== idempotency (no double-send after completion) ==")
    cid = 444444
    await simulate.main(client_id=cid, seed=2, verbose=False)
    conn = dbm.connect()
    clock = VirtualClock(2_000_000_000)  # far future
    transport = SimulatedTransport(clock, verbose=False)
    scheduler.startup_sweep(conn)
    await scheduler.tick(conn, transport, clock.now())
    check("no extra sends on re-tick", len(transport.sent_steps_for(cid)) == 0)
    conn.close()


def test_matchers():
    print("\n== stop / intent matching (token-based) ==")
    check("stop 'Стоп!'", content.is_stop("Стоп!"))
    check("stop 'стоп пожалуйста'", content.is_stop("стоп пожалуйста"))
    check("stop 'не пиши мне больше'", content.is_stop("не пиши мне больше"))
    check("normal text not stop", not content.is_stop("привет как дела"))
    check("intent 'сколько цена?'", content.has_intent("сколько цена?"))
    check("intent 'хочу купить'", content.has_intent("хочу купить"))
    check("intent 'готова оплатить'", content.has_intent("готова оплатить"))
    check("no false intent 'я заберу телефон'", not content.has_intent("я заберу телефон"))
    check("no false intent 'красивая сцена'", not content.has_intent("красивая сцена"))
    check("'хочу расклад' is NOT intent (answer to ask, not buying)",
          not content.has_intent("хочу расклад про любовь"))
    check("'не готова оплатить' NOT intent (phrase negation)",
          not content.has_intent("я не готова оплатить"))
    print("\n== name guards: affirmations/topic words are never names ==")
    check("extract('Да, про деньги') is None", content.extract_name("Да, про деньги") is None,
          repr(content.extract_name("Да, про деньги")))
    check("extract('не скажу') is None", content.extract_name("не скажу") is None,
          repr(content.extract_name("не скажу")))
    check("extract('Деньги') is None", content.extract_name("Деньги") is None)
    check("extract('любовь и деньги') is None", content.extract_name("любовь и деньги") is None)
    check("extract('Меня зовут Любовь')=='Любовь'", content.extract_name("Меня зовут Любовь") == "Любовь")
    check("extract('Я Роман')=='Роман'", content.extract_name("Я Роман") == "Роман")
    check("detect('Я Роман, про деньги')==Финансы",
          content.detect_topic("Я Роман, про деньги", exclude_name="Роман") == content.TOPIC_MONEY)


async def test_retrigger():
    print("\n== re-trigger only after cooldown ==")
    cid = 555555
    await simulate.main(client_id=cid, seed=4, verbose=False)
    conn = dbm.connect()
    c = funnel.get_client(conn, cid)
    check("funnel finished (CTA_SENT)", c["state"] == "CTA_SENT", c["state"])
    now = c["updated_at"] + config.RETRIGGER_COOLDOWN + 10
    res = funnel.handle_incoming(conn, cid, "ТАРО", now, bcid="SIM")
    check("re-triggers after cooldown", res["action"] == "re-triggered", str(res))
    c2 = funnel.get_client(conn, cid)
    check("run_id incremented", c2["run_id"] == c["run_id"] + 1)
    pend = conn.execute("SELECT COUNT(*) AS c FROM steps WHERE client_id=? AND status='pending'",
                        (cid,)).fetchone()["c"]
    check("new greeting scheduled", pend >= 1)
    res2 = funnel.handle_incoming(conn, cid, "ТАРО", now + 5, bcid="SIM")
    check("code word ignored while active", res2["action"] == "ignored", str(res2))
    conn.close()


async def test_early_answer_refine():
    print("\n== name refine across messages (greeting-then-name) ==")
    cid = 666666
    transport, _ = await simulate.main(
        client_id=cid, seed=6, verbose=False,
        messages=[(0, "ТАРО"), (20, "привет"), (50, "Меня зовут Оля, про деньги")])
    sent = transport.sent_steps_for(cid)
    check("intro personalised with 'Оля'", "Оля" in str(sent[3]["content"]),
          str(sent[3]["content"])[:40])


async def test_post_cta_handoff():
    print("\n== post-CTA reply -> operator handoff (hot lead not dropped) ==")
    cid = 888888
    await simulate.main(client_id=cid, seed=8, verbose=False)
    conn = dbm.connect()
    c = funnel.get_client(conn, cid)
    check("state CTA_SENT after funnel", c["state"] == "CTA_SENT", c["state"])
    res = funnel.handle_incoming(conn, cid, "да, интересно", c["updated_at"] + 60, bcid="SIM")
    check("any reply after CTA -> handoff", res["action"] == "handoff", str(res))
    check("state HANDOFF", funnel.get_client(conn, cid)["state"] == "HANDOFF")
    conn.close()


def test_confirm_toctou():
    print("\n== _confirm honours a STOP that lands during the send (TOCTOU) ==")
    cid = 777777
    conn = dbm.connect()
    with dbm.transaction(conn):
        dbm.wipe(conn, dbm.RUNTIME_TABLES)
    funnel.start_or_reset(conn, cid, "SIM", now=1000)
    s = conn.execute("SELECT * FROM steps WHERE client_id=? AND status='pending'", (cid,)).fetchone()
    conn.execute("UPDATE steps SET status='sending' WHERE id=?", (s["id"],))  # claimed, mid-send
    funnel._terminate(conn, cid, "STOPPED", 1001)                             # STOP arrives
    client = funnel.get_client(conn, cid)                                     # stale snapshot
    scheduler._confirm(conn, s, client, "greeting", 12345, 1002)             # send finished
    pend = conn.execute("SELECT COUNT(*) AS c FROM steps WHERE client_id=? AND status='pending'",
                        (cid,)).fetchone()["c"]
    check("no next step scheduled after STOP", pend == 0, f"pending={pend}")
    check("state stays STOPPED (not resurrected)", funnel.get_client(conn, cid)["state"] == "STOPPED")
    check("greeting still recorded sent", conn.execute(
        "SELECT status FROM steps WHERE id=?", (s["id"],)).fetchone()["status"] == "sent")
    conn.close()


def test_owner_takeover():
    print("\n== owner manual reply -> auto-pause drip (#1) + own-send echo guard ==")
    conn = dbm.connect()
    with dbm.transaction(conn):
        dbm.wipe(conn, dbm.RUNTIME_TABLES)
    cid = 121212
    funnel.start_or_reset(conn, cid, "SIM", now=1000)   # active drip: greeting pending
    pend_before = conn.execute("SELECT COUNT(*) AS c FROM steps WHERE client_id=? AND status='pending'",
                               (cid,)).fetchone()["c"]
    check("drip scheduled before takeover", pend_before >= 1, f"pending={pend_before}")
    took = funnel.owner_took_over(conn, cid, now=1005)   # owner types in this chat herself
    check("owner_took_over reports handled", took is True)
    check("state HANDOFF after takeover", funnel.get_client(conn, cid)["state"] == "HANDOFF")
    pend_after = conn.execute("SELECT COUNT(*) AS c FROM steps WHERE client_id=? AND status='pending'",
                              (cid,)).fetchone()["c"]
    check("pending drip cancelled", pend_after == 0, f"pending={pend_after}")
    check("no-op for her ordinary contact", funnel.owner_took_over(conn, 999999, now=1006) is False)
    check("no-op when already terminal", funnel.owner_took_over(conn, cid, now=1007) is False)
    # echo guard: a message id the bot itself sent must NOT be treated as a manual reply
    conn.execute("INSERT INTO sent_log(client_id, run_id, step_name, tg_message_id, sent_at) "
                 "VALUES (?,?,?,?,?)", (cid, 1, "greeting", 55501, 1000))
    check("own send recognised (id in sent_log)", funnel.owner_reply_is_own_send(conn, cid, 55501) is True)
    check("foreign id -> genuine manual reply", funnel.owner_reply_is_own_send(conn, cid, 999) is False)
    check("None id -> not own send", funnel.owner_reply_is_own_send(conn, cid, None) is False)
    conn.close()


def test_mark_read_gating():
    print("\n== mark_read gating (#2b): active funnel yes; stranger / after handoff no ==")
    conn = dbm.connect()
    with dbm.transaction(conn):
        dbm.wipe(conn, dbm.RUNTIME_TABLES)
    check("stranger (no client row) -> NOT read", funnel.should_mark_read(conn, 12321) is False)
    funnel.start_or_reset(conn, 555, "SIM", now=1000)          # active funnel (TRIGGERED)
    check("active funnel client -> read (looks responsive)", funnel.should_mark_read(conn, 555) is True)
    funnel._terminate(conn, 555, "HANDOFF", 1001)              # hot lead handed to human
    check("after handoff -> NOT read (gadalka reads herself)", funnel.should_mark_read(conn, 555) is False)
    funnel._terminate(conn, 555, "STOPPED", 1002)
    check("after stop -> NOT read", funnel.should_mark_read(conn, 555) is False)
    conn.close()


def test_daily_report():
    print("\n== daily digest: closed-day window (gap-free), metrics, conversion, events ==")
    import calendar
    from bot import report
    conn = dbm.connect()
    dbm.init(conn)                                   # ensure new events/meta tables exist
    with dbm.transaction(conn):
        dbm.wipe(conn, dbm.RUNTIME_TABLES)
    now = calendar.timegm((2026, 7, 16, 7, 0, 0, 0, 0, 0))    # 07:00 UTC = 10:00 MSK
    today_s, today_e = report.day_window(now)
    y_s, y_e = report.prev_day_window(now)
    check("today window is 24h", today_e - today_s == 86400)
    check("yesterday window is 24h", y_e - y_s == 86400)
    check("windows contiguous — no gap, no overlap", y_e == today_s, f"{y_e} vs {today_s}")
    check("now inside today window", today_s <= now < today_e)
    date_str, hour = report.local_parts(now)
    check("local time = 2026-07-16 10:00 MSK", date_str == "2026-07-16" and hour == 10, f"{date_str} {hour}")
    check("fires at 10:00 (>=REPORT_HOUR) when unsent", report.should_send(now, None)[0])
    check("does NOT fire again same day", not report.should_send(now, "2026-07-16")[0])
    check("does NOT fire at 09:00 MSK", not report.should_send(now - 3600, None)[0])

    def ev(e, cid, ts):
        conn.execute("INSERT INTO events(ts,event,client_id,run_id) VALUES (?,?,?,1)", (ts, e, cid))
    def diag(cid, ts):
        conn.execute("INSERT INTO sent_log(client_id,run_id,step_name,tg_message_id,sent_at) "
                     "VALUES (?,1,'diagnosis',?,?)", (cid, cid, ts))
    def cli(cid, name):
        conn.execute("INSERT OR REPLACE INTO clients(client_id,state,run_id,name,created_at,updated_at) "
                     "VALUES (?,'HANDOFF',1,?,0,0)", (cid, name))
    cli(501, "Аня"); cli(502, "Боря"); cli(601, "Витя")
    # YESTERDAY (incl. an evening lead near end-of-day -> must NOT dissolve)
    ev("triggered", 1, y_s + 100); ev("triggered", 2, y_s + 200); ev("triggered", 3, y_e - 60)
    diag(1, y_s + 300); diag(2, y_e - 30)
    ev("hot_lead", 501, y_s + 400); ev("hot_lead", 502, y_e - 20)   # 23:59-ish lead
    # TODAY (in-progress) -> must NOT appear in yesterday's report
    ev("triggered", 4, now - 60); ev("hot_lead", 601, now - 30)

    ym = report.collect(conn, y_s, y_e)
    check("yesterday: 3 triggered (evening incl.)", ym["triggered"] == 3, str(ym["triggered"]))
    check("yesterday: 2 readings", ym["readings"] == 2, str(ym["readings"]))
    check("yesterday: 2 hot (evening lead NOT dissolved)", len(ym["hot"]) == 2, str(ym["hot"]))
    out = report.build_report(conn, now, scope="yesterday")
    check("digest titled by CLOSED prev day 15.07.2026", "15.07.2026" in out, out.splitlines()[0])
    check("yesterday conversion 67%", "67%" in out)
    check("lead clickable", 'tg://user?id=501' in out and "Аня" in out)
    check("today's lead NOT in yesterday digest", "Витя" not in out)

    out2 = report.build_report(conn, now, scope="today")
    check("today button says 'день идёт'", "день идёт" in out2)
    check("today shows today's lead, not yesterday's", "Витя" in out2 and "Аня" not in out2)

    # event logging integration: start -> 'triggered', handoff -> 'hot_lead'
    with dbm.transaction(conn):
        dbm.wipe(conn, dbm.RUNTIME_TABLES)
    funnel.start_or_reset(conn, 4242, "SIM", now=now)
    got = [r["event"] for r in conn.execute("SELECT event FROM events WHERE client_id=4242")]
    check("start_or_reset logs 'triggered'", got == ["triggered"], str(got))
    conn.execute("UPDATE clients SET state='CTA_SENT' WHERE client_id=4242")
    r = funnel.handle_incoming(conn, 4242, "да хочу расклад", now + 1, bcid="SIM")
    check("post-CTA reply -> handoff", r["action"] == "handoff")
    hl = conn.execute("SELECT COUNT(*) c FROM events WHERE client_id=4242 AND event='hot_lead'").fetchone()["c"]
    check("handoff logs 'hot_lead' once", hl == 1, str(hl))
    conn.close()


async def main():
    await test_sequence_and_media()
    await test_timings()
    await test_name_fallback()
    test_matchers()
    await test_retrigger()
    await test_early_answer_refine()
    await test_post_cta_handoff()
    test_confirm_toctou()
    test_owner_takeover()
    test_mark_read_gating()
    test_daily_report()
    test_distribution()
    test_topic_detection()
    await test_topic_flow()
    await test_idempotency()
    print("\n" + "=" * 50)
    if FAILS:
        print(f"FAILED: {len(FAILS)} -> {FAILS}")
        raise SystemExit(1)
    print("ALL TESTS PASSED ✅")


if __name__ == "__main__":
    asyncio.run(main())
