"""Transport interface + a SimulatedTransport for local, network-free runs.

The funnel engine talks ONLY to this interface, so the same logic runs against
the simulator (today) and the real Telegram Business transport (once BOT_TOKEN
is set). See transport_business.py for the live implementation.
"""
import os


class Transport:
    async def send_text(self, chat_id, text, business_connection_id=None) -> int:
        raise NotImplementedError

    async def send_photo(self, chat_id, image_path, caption=None,
                         business_connection_id=None) -> int:
        raise NotImplementedError

    async def send_chat_action(self, chat_id, action="typing",
                              business_connection_id=None):
        raise NotImplementedError

    async def mark_read(self, chat_id, message_id, business_connection_id=None):
        return None

    async def notify_operator(self, text):
        return None


class SimulatedTransport(Transport):
    """Records everything it would send; prints a human-readable transcript.
    Verifies that image files actually exist on disk."""

    def __init__(self, clock, verbose=True):
        self.clock = clock
        self.verbose = verbose
        self.events = []          # list of dicts
        self._mid = 1000

    def _t(self):
        return self.clock.now()

    def _log(self, kind, chat_id, content):
        self._mid += 1
        ev = {"t": self._t(), "mid": self._mid, "chat_id": chat_id,
              "kind": kind, "content": content}
        self.events.append(ev)
        if self.verbose:
            self._print(ev)
        return self._mid

    def _print(self, ev):
        pass  # printing handled by the driver so it can show relative time

    async def send_text(self, chat_id, text, business_connection_id=None) -> int:
        return self._log("text", chat_id, text)

    async def send_photo(self, chat_id, image_path, caption=None,
                         business_connection_id=None) -> int:
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"image not found: {image_path}")
        size = os.path.getsize(image_path)
        return self._log("photo", chat_id,
                         {"image_path": image_path, "bytes": size, "caption": caption})

    async def send_chat_action(self, chat_id, action="typing",
                              business_connection_id=None):
        self.events.append({"t": self._t(), "chat_id": chat_id,
                            "kind": "action", "content": action})

    async def mark_read(self, chat_id, message_id, business_connection_id=None):
        self.events.append({"t": self._t(), "chat_id": chat_id,
                            "kind": "read", "content": message_id})

    async def notify_operator(self, text):
        self.events.append({"t": self._t(), "chat_id": "OPERATOR",
                            "kind": "alert", "content": text})

    # helpers for assertions in tests
    def sent_steps_for(self, chat_id):
        return [e for e in self.events
                if e.get("chat_id") == chat_id and e["kind"] in ("text", "photo")]
