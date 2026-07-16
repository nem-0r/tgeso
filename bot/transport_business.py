"""Live Telegram Business transport (aiogram).

Imported lazily by app.py so the simulator/tests run without aiogram installed.
Replies are sent ON BEHALF of the business account via business_connection_id, so
the client sees the reader's own account (no bot label).
"""
from . import config
from .transport import Transport


class BusinessTransport(Transport):
    def __init__(self, bot):
        self.bot = bot

    async def send_text(self, chat_id, text, business_connection_id=None) -> int:
        m = await self.bot.send_message(
            chat_id, text, business_connection_id=business_connection_id)
        return m.message_id

    async def send_photo(self, chat_id, image_path, caption=None,
                         business_connection_id=None) -> int:
        from aiogram.types import FSInputFile
        m = await self.bot.send_photo(
            chat_id, FSInputFile(image_path), caption=caption,
            business_connection_id=business_connection_id)
        return m.message_id

    async def send_chat_action(self, chat_id, action="typing",
                              business_connection_id=None):
        try:
            await self.bot.send_chat_action(
                chat_id, action, business_connection_id=business_connection_id)
        except Exception:
            pass  # cosmetic; never block a send on a failed typing action

    async def mark_read(self, chat_id, message_id, business_connection_id=None):
        try:
            await self.bot.read_business_message(
                business_connection_id=business_connection_id,
                chat_id=chat_id, message_id=message_id)
        except Exception:
            pass

    async def notify_operator(self, text, html=False):
        if config.OPERATOR_CHAT_ID:
            try:
                await self.bot.send_message(
                    int(config.OPERATOR_CHAT_ID), text,
                    parse_mode="HTML" if html else None)
            except Exception:
                pass
