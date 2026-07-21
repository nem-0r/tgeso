"""Central configuration.

VPS / bot token / business account are STUBBED for now: without BOT_TOKEN the bot
runs in SIMULATION mode (no network). Set BOT_TOKEN + OPERATOR_CHAT_ID to go live.
"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _p(*a):
    return os.path.join(BASE_DIR, *a)


# --- data / content ---
XLSX_PATH = os.environ.get("TAROT_XLSX", _p("Воронка таро.xlsx"))
DB_PATH = os.environ.get("TAROT_DB", _p("data", "tarot.sqlite"))
MEDIA_DIR = os.environ.get("TAROT_MEDIA", _p("media"))

# --- trigger ---
CODE_WORD = os.environ.get("TAROT_CODE_WORD", "ТАРО")
# ANY first message from an unknown person (text/sticker/gif -> empty text) also
# starts the funnel. NB: "unknown" = not in our DB, i.e. first contact SINCE the bot
# was connected — Telegram cannot tell us about older history. Chats the owner
# started herself are protected (see funnel.owner_took_over). Kill switch:
# TAROT_FIRST_CONTACT=0 reverts to code-word-only triggering (no redeploy needed).
FIRST_CONTACT_TRIGGER = os.environ.get("TAROT_FIRST_CONTACT", "1") == "1"

# --- Telegram (STUB until provided) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")               # None => simulation only
OPERATOR_CHAT_ID = os.environ.get("OPERATOR_CHAT_ID")  # operator id: hot-lead alerts + daily report + report button

# --- daily digest to the operator ---
REPORT_HOUR = int(os.environ.get("REPORT_HOUR", "10"))               # morning hour: digest of the CLOSED previous day
REPORT_TZ_OFFSET_HOURS = int(os.environ.get("REPORT_TZ_OFFSET", "3"))  # Moscow = UTC+3, no DST
REPORT_BUTTON = "📊 Отчёт за сегодня"                                  # on-demand report button label

# --- funnel timing (seconds) — STRICTLY per воронка, each relative to previous step ---
# (step_name, delay_from_previous_step_seconds, kind)
STEP_CHAIN = [
    ("greeting",  7 * 60, "text"),   # +7 мин после кодового слова
    ("ask",            0, "text"),   # сразу после greeting
    ("working",   5 * 60, "text"),   # +5 мин
    ("intro",    15 * 60, "text"),   # +15 мин
    ("image",          0, "photo"),  # сразу: карта
    ("diagnosis",      0, "text"),   # сразу: разбор (текст ПОД картинкой)
    ("cta",           30, "text"),   # +30 сек
]
STEP_ORDER = [s[0] for s in STEP_CHAIN]
STEP_DELAY = {s[0]: s[1] for s in STEP_CHAIN}
STEP_KIND = {s[0]: s[2] for s in STEP_CHAIN}
FIRST_STEP = STEP_ORDER[0]

# state assigned to client AFTER a given step is sent
STATE_AFTER = {
    "greeting": "GREETED",
    "ask": "ASKED",
    "working": "WORKING",
    "intro": "DIAGNOSING",
    "image": "DIAGNOSING",
    "diagnosis": "DIAGNOSED",
    "cta": "CTA_SENT",   # automation finished; a client reply now = hot lead -> operator
}

# staleness TTL (seconds): a due step older than this is skipped instead of sent late
STEP_TTL = {"greeting": 15 * 60, "ask": 10 * 60, "working": 20 * 60,
            "intro": 30 * 60, "image": 30 * 60, "diagnosis": 30 * 60, "cta": 5 * 60}

# strict mode: exact timings (as requested). Enable jitter later for anti-ban.
JITTER_ENABLED = os.environ.get("TAROT_JITTER", "0") == "1"

BUSINESS_WINDOW = 24 * 60 * 60   # Telegram business reply window
WINDOW_SAFETY = 23 * 60 * 60     # send only if last_incoming within this

RETRIGGER_COOLDOWN = 24 * 60 * 60  # a completed/stopped client may re-enter after this

# bounded retry for failed sends (avoid hammering a blocked user; give up cleanly)
MAX_SEND_ATTEMPTS = 5
RETRY_BACKOFF_BASE = 30    # seconds
RETRY_BACKOFF_CAP = 300

STOP_WORDS = {"стоп", "stop", "отписка", "отписаться", "хватит", "не пиши", "unsubscribe"}
# NB: «хочу расклад» deliberately NOT here — it is a natural ANSWER to the ask step
# («хочу расклад про любовь»), not buy intent. Post-CTA any reply is a hot lead anyway.
INTENT_WORDS = {"куплю", "купить", "цена", "цену", "стоимость", "оплата", "оплатить",
                "записаться", "запиши", "беру", "оплачу", "готова оплатить",
                "сколько стоит"}   # phrase: safe now that early intent only pings

TERMINAL_STATES = {"STOPPED", "HANDOFF", "COMPLETED", "BLOCKED", "ABANDONED"}
# after these states a client reply = engaged lead -> operator handoff
ENGAGED_STATES = {"WORKING", "DIAGNOSING", "DIAGNOSED", "CTA_SENT", "COMPLETED"}

POLL_INTERVAL = 2  # seconds (production poller tick)
