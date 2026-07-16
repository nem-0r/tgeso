"""Network-free end-to-end demo with a VIRTUAL clock.

Drives a fake client through the whole funnel (code word -> +7m greeting -> ask
-> name reply -> +5m working -> +15m intro -> card PHOTO -> diagnosis -> +30s CTA)
and prints the exact transcript with relative timings. Proves image+text delivery
without any Telegram token.
"""
import asyncio
import random

from . import config, importer, scheduler, funnel
from . import db as dbm
from .clock import VirtualClock
from .transport import SimulatedTransport


def _ensure_content():
    conn = dbm.connect()
    dbm.init(conn)
    n = conn.execute("SELECT COUNT(*) AS c FROM variants").fetchone()["c"]
    conn.close()
    if n != 66:
        print("(content not imported yet — running importer)\n")
        importer.run()


def _fmt(t, t0):
    d = t - t0
    sign = "+" if d >= 0 else "-"
    d = abs(d)
    return f"{sign}{d // 60:02d}:{d % 60:02d}"


def _preview(content_val):
    if isinstance(content_val, dict):  # photo
        kb = content_val["bytes"] / 1024
        return f"🖼  PHOTO {content_val['image_path'].split('/')[-1]} ({kb:.0f} KB)"
    first = str(content_val).replace("\n", " ⏎ ")
    return (first[:110] + " …") if len(first) > 110 else first


async def main(client_id=900001, seed=7, verbose=True, messages=None):
    _ensure_content()
    conn = dbm.connect()
    with dbm.transaction(conn):
        dbm.wipe(conn, dbm.RUNTIME_TABLES)
    rng = random.Random(seed)
    clock = VirtualClock(1_700_000_000)
    transport = SimulatedTransport(clock, verbose=False)
    t0 = clock.now()

    if messages is None:
        messages = [(0, config.CODE_WORD), (40, "Меня Маша зовут, что с любовью?")]
    timeline = []  # (t, direction, desc)
    inbox = [(t0 + off, client_id, text) for off, text in messages]

    def deliver_incoming():
        while inbox and inbox[0][0] <= clock.now():
            t, cid, text = inbox.pop(0)
            timeline.append((t, "IN", f"клиент: {text}"))
            res = funnel.handle_incoming(conn, cid, text, clock.now(), bcid="SIM", rng=rng)
            if res["action"] == "handoff":
                asyncio.ensure_future(transport.notify_operator(
                    f"🔥 Горячий лид {cid} ({res.get('name')}) — подключись"))

    guard = 0
    while True:
        guard += 1
        if guard > 100000:
            raise RuntimeError("simulation did not terminate")
        deliver_incoming()
        await scheduler.tick(conn, transport, clock.now(), rng)
        nxt_step = scheduler.next_pending_run_at(conn)
        nxt_msg = inbox[0][0] if inbox else None
        cands = [x for x in (nxt_step, nxt_msg) if x is not None]
        if not cands:
            break
        target = min(cands)
        if target > clock.now():
            clock.set(target)

    # merge outgoing events into the timeline
    for ev in transport.events:
        if ev["kind"] in ("text", "photo"):
            timeline.append((ev["t"], "OUT", _preview(ev["content"])))
        elif ev["kind"] == "alert":
            timeline.append((ev["t"], "OP", ev["content"]))
    timeline.sort(key=lambda x: (x[0], 0 if x[1] == "IN" else 1))

    if verbose:
        c = funnel.get_client(conn, client_id)
        v = conn.execute("SELECT * FROM variants WHERE variant_id=?", (c["variant_id"],)).fetchone()
        print("=" * 72)
        print(f"ДЕМО ВОРОНКИ (строго по скрипту)  клиент={client_id}")
        print(f"назначенная связка: #{v['variant_id']}  тема={v['topic']}  "
              f"карта={v['card_number']}. {v['card_name']}  имя={c['name']!r}")
        print("=" * 72)
        for t, d, desc in timeline:
            arrow = {"IN": "→", "OUT": "←", "OP": "⚑"}[d]
            print(f"  {_fmt(t, t0)}  {arrow}  {desc}")
        print("=" * 72)
        sent = [e for e in transport.events if e["kind"] in ("text", "photo")]
        print(f"Итого отправлено ботом: {len(sent)} сообщений "
              f"(ожидается 7: greeting, ask, working, intro, PHOTO, diagnosis, cta)")
    conn.close()
    return transport, timeline


if __name__ == "__main__":
    asyncio.run(main())
