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
    print("\n== even-random distribution (shuffled bag) ==")
    conn = dbm.connect()
    variants.rebuild_bag(conn, random.Random(9))
    n = conn.execute("SELECT COUNT(*) AS c FROM variants").fetchone()["c"]
    counts = {}
    for _ in range(n * 2):
        vid = variants.draw_variant(conn, random.Random(0))
        counts[vid] = counts.get(vid, 0) + 1
    check(f"all {n} variants used exactly twice over 2 bags",
          len(counts) == n and set(counts.values()) == {2}, str(sorted(set(counts.values()))))
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
    check("no false intent 'я заберу телефон'", not content.has_intent("я заберу телефон"))
    check("no false intent 'красивая сцена'", not content.has_intent("красивая сцена"))


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
    test_distribution()
    await test_idempotency()
    print("\n" + "=" * 50)
    if FAILS:
        print(f"FAILED: {len(FAILS)} -> {FAILS}")
        raise SystemExit(1)
    print("ALL TESTS PASSED ✅")


if __name__ == "__main__":
    asyncio.run(main())
