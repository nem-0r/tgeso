"""Content helpers: card canon, code-word/stop/intent matching, name extraction,
intro rendering, and per-step message building."""
import re

from . import config

# Canonical 22 Major Arcana in the exact order they appear in the funnel file.
CANON = [
    "Дурак", "Маг", "Верховная жрица", "Императрица", "Император", "Иерофант",
    "Влюблённые", "Колесница", "Правосудие", "Отшельник", "Колесо Фортуны", "Сила",
    "Повешенный", "Смерть", "Умеренность", "Дьявол", "Башня", "Звезда", "Луна",
    "Солнце", "Суд", "Мир",
]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("ё", "е").replace("Ё", "Е")).strip().lower()


CANON_NORM = {norm(c): c for c in CANON}

_WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+", re.UNICODE)


def tokens(text: str):
    return [t.lower() for t in _WORD_RE.findall(text or "")]


def _phrase_re(phrase_norm: str):
    return r"(?<!\w)" + re.escape(phrase_norm) + r"(?!\w)"


def is_code_word(text: str) -> bool:
    return config.CODE_WORD.lower() in tokens(text)


def is_stop(text: str) -> bool:
    toks = set(tokens(text))
    low = norm(text)
    for w in config.STOP_WORDS:
        wl = norm(w)
        if " " in w:                                  # phrase: match on word boundaries
            if re.search(_phrase_re(wl), low):        # 'не пиши' won't match 'не пишите'
                return True
        elif wl in toks:                              # single word: whole token
            return True
    return False


def has_intent(text: str) -> bool:
    low = norm(text)
    ntoks = [norm(t) for t in tokens(text)]
    for w in config.INTENT_WORDS:
        wl = norm(w)
        if " " in w:                                  # phrase: contiguous, word-bounded
            if re.search(_phrase_re(wl), low):
                return True
        else:                                         # single token, not negated by 'не'
            for i, t in enumerate(ntoks):
                if t == wl and not (i > 0 and ntoks[i - 1] == "не"):
                    return True
    return False


def parse_card(diagnosis: str):
    """Return (number_display, canonical_name) parsed from the first line of r8.
    Raises ValueError if the card name is not one of the 22 canonical arcana."""
    lines = (diagnosis or "").strip().splitlines()
    first = lines[0] if lines else ""
    m = re.match(r"^\s*([0-9IVXLC]+)\s*\.\s*(.+?)\.?\s*$", first)
    if not m:
        raise ValueError(f"cannot parse card heading: {first!r}")
    number, raw_name = m.group(1).strip(), m.group(2).strip()
    canon = CANON_NORM.get(norm(raw_name))
    if not canon:
        raise ValueError(f"unknown card name {raw_name!r} (norm={norm(raw_name)!r})")
    return number, canon


def make_intro_template(intro_raw: str) -> str:
    """Turn the leading 'ИМЯ ,' vocative in r6 into a '{name}' placeholder.
    Only the leading vocative is touched; the rest is left verbatim."""
    new, n = re.subn(r"^\s*ИМЯ\s*,\s*", "{name}, ", intro_raw, count=1)
    if n != 1:
        # r6 must start with the vocative; fail loud so we notice a format change.
        raise ValueError("intro (r6) does not start with 'ИМЯ ,' vocative")
    return new


def render_intro(intro_tmpl: str, name) -> str:
    if name:
        return intro_tmpl.replace("{name}", name)
    # no name captured -> drop the vocative, capitalise the next word
    s = re.sub(r"^\{name\}\s*,\s*", "", intro_tmpl)
    return (s[:1].upper() + s[1:]) if s else s


# words that are NOT a client name (greetings, question/filler/funnel words)
_NON_NAME = {
    "привет", "приветик", "здравствуй", "здравствуйте", "хай", "хеллоу", "hello", "hi",
    "доброго", "добрый", "доброе", "добрая", "день", "дня", "вечер", "вечера",
    "утро", "утра", "ночь", "ночи", "суток", "времени", "дратути",
    "меня", "зовут", "звать", "я", "это", "имя", "мое", "моё", "мне",
    "тебя", "тебе", "вас", "вам", "его", "её", "ее", "их", "нас", "нам", "он", "она", "они",
    "спасибо", "пожалуйста", "благодарю", "извините", "простите",
    "что", "чё", "чо", "как", "когда", "где", "почему", "зачем", "какой", "какая", "сколько",
    "хочу", "хочется", "надо", "нужно", "можно", "можете", "подскажите", "подскажи",
    "помогите", "помоги", "узнать", "вопрос", "вопросик", "расскажи", "расскажите", "скажите",
    "гадание", "расклад", "таро", "гадать", "дай", "дайте", "про",
}
_MARKERS = ("зовут", "звать", "я", "это")
_NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\-]{1,19}$")


def _valid_name(tok: str) -> bool:
    return bool(_NAME_RE.match(tok)) and tok.lower() not in _NON_NAME \
        and tok.lower() != config.CODE_WORD.lower()


def _cap(tok: str) -> str:  # capitalise each hyphen-separated part
    return "-".join(p[:1].upper() + p[1:].lower() for p in tok.split("-"))


def extract_name(text: str):
    """Best-effort first-name extraction. Prefers an explicit «(меня) зовут / я / это <Name>»
    marker anywhere; else the very first token IF it plausibly is a name. Greetings, question
    and filler words are never used; returns None -> intro drops the vocative."""
    if not text:
        return None
    toks = re.findall(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\-]*", text)
    low = [t.lower() for t in toks]
    for i, t in enumerate(low):
        if t in ("зовут", "звать"):        # name may follow OR precede ("меня Маша зовут")
            for j in (i + 1, i - 1):
                if 0 <= j < len(toks) and _valid_name(toks[j]):
                    return _cap(toks[j])
        elif t in ("я", "это") and i + 1 < len(toks) and _valid_name(toks[i + 1]):
            return _cap(toks[i + 1])
    if toks and _valid_name(toks[0]):      # otherwise the first token, if name-like
        return _cap(toks[0])
    return None
