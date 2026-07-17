#!/usr/bin/env python3
"""Local chat tester — talk to the bot's LOGIC without Telegram, network or prod.

Interactive:  python3 run_chat.py
    <text>          send it as the client (shows how the bot understood it)
    /ff             fast-forward virtual time to the next scheduled step
    /run            fast-forward until the funnel finishes
    /state          show the client card (name, topic, chosen card, state)
    /probe <text>   analyse a message without sending it
    /reset          wipe and start a fresh client
    /quit           exit

One-shot:     python3 run_chat.py --probe "Очень большая девушка"
    prints how the bot parses the message (code word / stop / intent / name / topic).

Uses the LOCAL dev DB (same wipe-runtime pattern as bot/simulate.py). Never touches prod.
"""
import asyncio
import sys

from bot import config, content, funnel, scheduler, simulate
from bot import db as dbm
from bot.clock import VirtualClock
from bot.transport import SimulatedTransport

CID = 990001


def probe(text):
    name = content.extract_name(text)
    print(f"  кодовое слово : {'ДА' if content.is_code_word(text) else 'нет'}")
    print(f"  стоп-слово    : {'ДА' if content.is_stop(text) else 'нет'}")
    print(f"  интент покупки: {'ДА' if content.has_intent(text) else 'нет'}")
    print(f"  имя           : {name!r}")
    print(f"  тема          : {content.detect_topic(text, exclude_name=name)!r}")


def fmt_out(ev, t0):
    d = ev["t"] - t0
    body = ev["content"]
    if isinstance(body, dict):
        body = f"🖼 PHOTO {body['image_path'].split('/')[-1]}"
    else:
        body = str(body).replace("\n", " ⏎ ")[:100]
    return f"  [+{d // 60:02d}:{d % 60:02d}] гадалка ← {body}"


class Chat:
    def __init__(self):
        simulate._ensure_content()
        self.conn = dbm.connect()
        dbm.init(self.conn)
        self.reset()

    def reset(self):
        with dbm.transaction(self.conn):
            dbm.wipe(self.conn, dbm.RUNTIME_TABLES)
        self.clock = VirtualClock(1_800_000_000)
        self.tr = SimulatedTransport(self.clock, verbose=False)
        self.t0 = self.clock.now()
        self.seen = 0
        print(f"— новый клиент {CID}; напишите ему что-нибудь (кодовое слово: {config.CODE_WORD})")

    def state(self):
        c = funnel.get_client(self.conn, CID)
        if c is None:
            print("  (клиента ещё нет — напишите сообщение)")
            return
        v = self.conn.execute("SELECT topic, card_number, card_name FROM variants WHERE variant_id=?",
                              (c["variant_id"],)).fetchone() if c["variant_id"] is not None else None
        card = f"{v['card_number']}. {v['card_name']} [{v['topic']}]" if v else "(не выбрана)"
        pend = self.conn.execute("SELECT step_name, run_at FROM steps WHERE client_id=? AND status='pending' "
                                 "ORDER BY run_at LIMIT 1", (CID,)).fetchone()
        nxt = f"{pend['step_name']} через {pend['run_at'] - self.clock.now()}с" if pend else "нет"
        print(f"  state={c['state']}  имя={c['name']!r}  тема={c['topic']!r}")
        print(f"  карта: {card}   следующий шаг: {nxt}")

    def show_new_sends(self):
        evs = [e for e in self.tr.events if e["kind"] in ("text", "photo")]
        for ev in evs[self.seen:]:
            print(fmt_out(ev, self.t0))
        self.seen = len(evs)

    async def incoming(self, text):
        res = funnel.handle_incoming(self.conn, CID, text, self.clock.now(), bcid="SIM")
        print(f"  → действие бота: {res['action']}")
        probe(text)
        await scheduler.tick(self.conn, self.tr, self.clock.now())
        self.show_new_sends()

    async def ff(self, to_end=False):
        moved = False
        while True:
            nxt = scheduler.next_pending_run_at(self.conn)
            if nxt is None:
                if not moved:
                    print("  (запланированных шагов нет)")
                return
            self.clock.set(max(nxt, self.clock.now()))
            await scheduler.tick(self.conn, self.tr, self.clock.now())
            self.show_new_sends()
            moved = True
            if not to_end:
                return


async def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--probe":
        print(f"АНАЛИЗ: {sys.argv[2]!r}")
        probe(sys.argv[2])
        return
    chat = Chat()
    while True:
        try:
            line = input("клиент> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line == "/quit":
            break
        elif line == "/reset":
            chat.reset()
        elif line == "/state":
            chat.state()
        elif line == "/ff":
            await chat.ff()
        elif line == "/run":
            await chat.ff(to_end=True)
        elif line.startswith("/probe "):
            probe(line[7:])
        else:
            await chat.incoming(line)
    chat.conn.close()


if __name__ == "__main__":
    asyncio.run(main())
