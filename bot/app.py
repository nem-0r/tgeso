"""Live entrypoint (Telegram Business).

STUB: without BOT_TOKEN this refuses to run and points you at the simulator.
When BOT_TOKEN + the connected business account are ready, `python -m bot.app`
runs the bot 24/7 (long-polling) — no domain/webhook needed.
"""
import asyncio
import html
import time

from . import config, importer, scheduler, funnel, report
from . import db as dbm


async def _run_live():
    from aiogram import Bot, Dispatcher
    from aiogram.types import (Message, BusinessConnection,
                               ReplyKeyboardMarkup, KeyboardButton)
    from .transport_business import BusinessTransport

    conn = dbm.connect()
    dbm.init(conn)
    if conn.execute("SELECT COUNT(*) AS c FROM variants").fetchone()["c"] != 66:
        conn.close()
        importer.run(wipe_runtime=False)  # never destroy live clients/steps
        conn = dbm.connect()
    scheduler.startup_sweep(conn)

    bot = Bot(config.BOT_TOKEN)
    transport = BusinessTransport(bot)
    dp = Dispatcher()

    # operator-only report keyboard (shown only in the operator's private chat with the bot)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=config.REPORT_BUTTON)]],
                             resize_keyboard=True, is_persistent=True)

    def _is_operator(uid):
        return bool(config.OPERATOR_CHAT_ID) and str(uid) == str(config.OPERATOR_CHAT_ID)

    async def _send_report(chat_id):   # on-demand button -> today so far
        await bot.send_message(chat_id, report.build_report(conn, int(time.time()), scope="today"),
                               parse_mode="HTML", reply_markup=kb)

    def _remember_owner(bcid, owner_id, can_reply=True, enabled=True):
        if owner_id is None:
            return
        with dbm.transaction(conn):
            conn.execute(
                "INSERT INTO business_connections(business_connection_id, owner_user_id, "
                "can_reply, can_read, is_enabled, connected_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(business_connection_id) DO UPDATE SET owner_user_id=excluded.owner_user_id, "
                "can_reply=excluded.can_reply, is_enabled=excluded.is_enabled",
                (bcid, owner_id, 1 if can_reply else 0, 1, 1 if enabled else 0, int(time.time())))

    async def _owner_of(bcid):
        r = conn.execute("SELECT owner_user_id FROM business_connections WHERE business_connection_id=?",
                         (bcid,)).fetchone()
        if r and r["owner_user_id"] is not None:
            return r["owner_user_id"]
        try:  # cold cache (e.g. after a restart) -> fetch once from Telegram
            bc = await bot.get_business_connection(bcid)
            _remember_owner(bcid, bc.user.id)
            return bc.user.id
        except Exception:
            return None

    @dp.business_connection()
    async def on_connection(evt: BusinessConnection):
        rights = getattr(evt, "rights", None)
        can_reply = getattr(rights, "can_reply", None) if rights else getattr(evt, "can_reply", None)
        owner_id = evt.user.id if getattr(evt, "user", None) else None
        _remember_owner(evt.id, owner_id, bool(can_reply), bool(evt.is_enabled))
        print(f"[business_connection] id={evt.id} owner={owner_id} enabled={evt.is_enabled} can_reply={can_reply}")
        if evt.is_enabled and not can_reply:
            print("  WARNING: bot lacks can_reply — enable 'reply to messages' in the connection")

    @dp.business_message()
    async def on_message(msg: Message):
        if msg.from_user is None:
            return  # #3: a message with no sender is never a funnel client
        now = int(time.time())
        owner_id = await _owner_of(msg.business_connection_id)
        if owner_id is not None and msg.from_user.id == owner_id:
            # #1: the owner typed in a client chat herself -> a human took over; pause our
            # drip there. Skip the bot's OWN on-behalf sends (echoed back) via sent_log id.
            if not funnel.owner_reply_is_own_send(conn, msg.chat.id, msg.message_id):
                funnel.owner_took_over(conn, msg.chat.id, now)
            return
        text = msg.text or msg.caption or ""
        res = funnel.handle_incoming(
            conn, msg.from_user.id, text, now,
            bcid=msg.business_connection_id, msg_id=msg.message_id)
        # #2: mark read ONLY during an active funnel — never her ordinary contacts, and
        # never after handoff/stop (leave those UNREAD so she notices and reads them herself)
        if funnel.should_mark_read(conn, msg.from_user.id):
            await transport.mark_read(msg.chat.id, msg.message_id, msg.business_connection_id)
        if res["action"] == "handoff":
            nm = html.escape(res.get("name") or "клиент")
            body = html.escape(text[:80])
            await transport.notify_operator(
                f'🔥 Горячий лид: <a href="tg://user?id={msg.from_user.id}">{nm}</a>\n'
                f'написал: {body}', html=True)
        elif res["action"] == "early_lead":
            nm = html.escape(res.get("name") or "клиент")
            body = html.escape(text[:80])
            await transport.notify_operator(
                f'💡 Ранний интерес: <a href="tg://user?id={msg.from_user.id}">{nm}</a> '
                f'спрашивает про покупку до разбора.\nНаписал: {body}\n'
                f'Воронка ПРОДОЛЖАЕТСЯ — разбор придёт по расписанию.', html=True)

    @dp.message()
    async def on_direct(msg: Message):
        # direct messages to the bot's own chat: OPERATOR ONLY. Everyone else is ignored,
        # so a stranger opening the bot never sees any report or data.
        if msg.from_user is None or not _is_operator(msg.from_user.id):
            return
        txt = (msg.text or "").strip()
        if txt == config.REPORT_BUTTON or txt.lower() in ("/report", "отчет", "отчёт"):
            await _send_report(msg.chat.id)
        elif txt.startswith("/start"):
            await bot.send_message(
                msg.chat.id,
                "Привет! Я твой ассистент по воронке. Жми кнопку, чтобы посмотреть сводку за день.",
                reply_markup=kb)
        else:
            await bot.send_message(msg.chat.id, "Жми кнопку 👇", reply_markup=kb)

    async def _maybe_daily_report(now):
        last = dbm.meta_get(conn, "last_report_date")
        fire, today = report.should_send(now, last)
        if not fire:
            return
        with dbm.transaction(conn):
            dbm.meta_set(conn, "last_report_date", today)   # mark first -> never double-send
        if config.OPERATOR_CHAT_ID:
            await bot.send_message(int(config.OPERATOR_CHAT_ID),
                                   report.build_report(conn, now, scope="yesterday"),
                                   parse_mode="HTML", reply_markup=kb)
            print(f"[daily report] sent on {today} (covers previous day)")

    async def poller():
        while True:
            now = int(time.time())
            try:
                await scheduler.tick(conn, transport, now)
            except Exception as e:  # never let the poller die
                print("poller error:", e)
            try:
                await _maybe_daily_report(now)
            except Exception as e:  # report must never break the funnel
                print("daily report error:", e)
            await asyncio.sleep(config.POLL_INTERVAL)

    poller_task = asyncio.create_task(poller())  # keep a strong ref (GC safety)
    _ = poller_task
    print("Bot live (long-polling). Waiting for business messages…")
    await dp.start_polling(
        bot, allowed_updates=["business_connection", "business_message",
                              "edited_business_message", "deleted_business_messages",
                              "message"])   # "message" = operator's direct commands/button


def main():
    if not config.BOT_TOKEN:
        print("BOT_TOKEN не задан — живой режим отключён (заглушка VPS/API).")
        print("Проверить работу без токена:  python run_simulate.py")
        print("Запустить импорт контента:     python run_import.py")
        return
    asyncio.run(_run_live())


if __name__ == "__main__":
    main()
