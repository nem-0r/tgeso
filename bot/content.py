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
        if " " in w:                                  # phrase: contiguous, word-bounded,
            if re.search(_phrase_re(wl), low) and \
               not re.search(r"(?<!\w)не\s+" + re.escape(wl) + r"(?!\w)", low):
                return True                           # ...and not negated («не готова оплатить»)
        else:                                         # single token, not negated by 'не'
            for i, t in enumerate(ntoks):             # («не куплю», «не готова оплатить»)
                if t == wl and not any(ntoks[j] == "не" for j in range(max(0, i - 2), i)):
                    return True
    return False


# ---------------------------------------------------------------------------
# Topic detection (Любовь/отношения | Финансы | Будущее) for per-topic variants.
# Canonical strings MUST match variants.topic in the DB byte-exactly.
TOPIC_LOVE = "Любовь/отношения"
TOPIC_MONEY = "Финансы"
TOPIC_FUTURE = "Будущее"
TOPICS = (TOPIC_LOVE, TOPIC_MONEY, TOPIC_FUTURE)

# Exact word forms only (no bare prefixes: «любой»/«Люба»/«любопытно» must NOT match).
_TOPIC_WORDS = {
    TOPIC_LOVE: {
        "любовь", "любви", "любовью", "влюблена", "влюблен", "влюбилась", "влюбился",
        "отношения", "отношений", "отношениях", "отношениям", "отношение",
        "половинка", "половинку", "половинки", "суженый", "суженого", "суженая",
        "жених", "жениха", "невеста", "невесту", "парень", "парня", "парнем",
        "девушка", "девушку", "девушки", "девушкой",
        "муж", "мужа", "мужем", "мужу", "жена", "жену", "женой", "жене",
        "бывший", "бывшего", "бывшая", "бывшую", "бывшим",
        "расставание", "расстались", "разошлись", "развод", "развода", "разводимся",
        "свадьба", "свадьбы", "брак", "браке", "замуж", "замужество",
        "жениться", "женюсь", "семья", "семье", "семью", "чувства", "чувств",
        "любимый", "любимая", "любимого", "любимую", "любимым",
        "избранник", "избранника", "избранница",
        # NB: «роман» deliberately excluded — Роман is a common first name
        "измена", "измены", "изменяет", "ревность", "ревнует",
        "одиночество", "одинока", "одинок",
        # situations
        "влюбленность", "страсть", "свидание", "свидания", "помолвка", "венчание",
        "примирение", "помиримся", "сойдемся", "расстался", "рассталась", "разлука",
        "бросил", "бросила", "разлюбил", "разлюбила", "нравлюсь",
        "замужем", "женат", "холост", "разведена", "разведен",
        "взаимность", "взаимности", "безответная", "безответной", "любит",
    },
    TOPIC_MONEY: {
        "деньги", "денег", "деньгам", "деньгами", "деньгах",
        "финансы", "финансов", "финансовый", "финансовое", "финансовая",
        "зарплата", "зарплату", "зарплаты", "зарплате", "зп",
        "доход", "дохода", "доходы", "заработок", "заработать", "зарабатываю",
        "работа", "работу", "работы", "работе", "работой",
        "карьера", "карьеру", "карьере", "бизнес", "бизнеса", "бизнесе",
        "долг", "долги", "долгов", "кредит", "кредиты", "ипотека", "ипотеку",
        "богатство", "прибыль", "прибыли", "инвестиции", "бабло",
        "премия", "премию", "повышение", "увольнение", "уволили", "уволят",
        "сокращение", "сократили",
        # situations
        "богат", "богатая", "разбогатею", "разбогатеть", "накопления", "сбережения",
        "зарплатой", "оклад", "оклада", "подработка", "подработку", "фриланс",
        "торговля", "продажи", "стартап", "инвестировать", "вложения", "вложить",
        "крипта", "криптовалюта", "биткоин", "выигрыш", "выиграю", "лотерея",
        "наследство", "наследства", "алименты", "безденежье", "нищета", "бедность",
        "задолженность", "займ", "займы", "микрозайм", "банкротство", "банкрот",
        "расходы", "траты", "бюджет", "начальник", "начальство", "увольняюсь",
        "уволиться", "сокращают", "собеседование", "собеседования", "вакансия",
        "вакансию", "трудоустройство", "работодатель",
    },
    TOPIC_FUTURE: {
        "будущее", "будущего", "будущем", "будущему", "будущий", "будущая",
        "судьба", "судьбы", "судьбе", "судьбу", "предназначение",
        "перспективы", "перспектива", "грядущее", "впереди",
        # situations
        "предсказание", "предсказания", "прогноз", "пророчество", "грядет",
        "дальнейшее", "дальнейшая", "дальнейшей", "перемены", "перемен",
        "изменения", "предначертано", "сложится", "ожидает", "предстоит",
        "сбудется", "исполнится",
    },
}
_TOPIC_PHRASES = {
    TOPIC_LOVE: ("вторая половинка", "личная жизнь", "личной жизни", "личную жизнь",
                 "любит ли", "вернется ли он", "вернется ли она", "вернется ли муж",
                 "вернется ли жена", "вернется ли парень", "вернется ли бывший",
                 "вернется ли бывшая", "будем ли вместе", "мы расстались",
                 "меня бросил", "меня бросила", "сойдемся ли", "помиримся ли",
                 "выйду ли замуж", "женится ли", "есть ли чувства",
                 "про него", "про нее"),   # production: «про него, что он действительно…»
    TOPIC_MONEY: ("денежный вопрос", "найду ли работу", "сменю ли работу",
                  "повысят ли", "вернут ли долг", "отдадут ли долг", "вернут ли деньги"),
    TOPIC_FUTURE: ("что ждет", "что меня ждет", "что нас ждет",
                   "что будет", "что будет дальше", "что дальше",
                   "что ожидает", "что предстоит", "как сложится",
                   "что произойдет", "что случится", "ближайшее время",
                   "что происходит"),   # production: «Жанна 20.11.1966.что происходит»
}


def _lev1(a: str, b: str) -> bool:
    """True if edit distance(a, b) <= 1 (one substitution, insertion or deletion)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(1 for x, y in zip(a, b) if x != y) == 1
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = j = diff = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
        else:
            j += 1
            diff += 1
            if diff > 1:
                return False
    return True


def _fuzzy_topic(tok: str):
    """Typo-tolerant topic for one token: distance<=1 against the dictionaries, but
    ONLY for tokens >=5 chars with the same first letter — so «денги/зарплта/любов»
    match while real words like «забота» (vs «работа») or «брат» (vs «брак») never do.
    Ambiguity across topics -> None (better an honest fallback than a wrong guess)."""
    if len(tok) < 5:
        return None
    found = None
    for topic in TOPICS:
        for w in _TOPIC_WORDS[topic]:
            if w[0] != tok[0] or abs(len(w) - len(tok)) > 1:
                continue
            if _lev1(tok, w):
                if found is not None and found != topic:
                    return None
                found = topic
                break
    return found


def detect_topic(text: str, exclude_name=None):
    """Return the canonical topic detected in `text`, or None.

    exclude_name: the client's (extracted) first name — its token never counts as a
    topic word, so «Меня зовут Люба/Любовь» does not read as the Love topic.
    Priority: the specific topics (Любовь, Финансы) beat the generic Будущее
    («будущий муж» -> Любовь, «финансовое будущее» -> Финансы); between the two
    specific ones the earliest mention in the text wins. If nothing matches exactly,
    a conservative distance-1 typo pass runs («денги» -> Финансы)."""
    if not text:
        return None
    low = norm(text)
    excl = norm(exclude_name) if exclude_name else None
    ordered = [norm(t) for t in tokens(text)]
    present = set(ordered)
    if excl:
        present.discard(excl)
    hits = {}   # topic -> earliest char position in the normalised text
    for topic in TOPICS:
        for w in (present & _TOPIC_WORDS[topic]):
            m = re.search(_phrase_re(w), low)
            if m:
                hits[topic] = min(hits.get(topic, 1 << 30), m.start())
        for p in _TOPIC_PHRASES.get(topic, ()):
            m = re.search(_phrase_re(norm(p)), low)
            if m:
                hits[topic] = min(hits.get(topic, 1 << 30), m.start())
    if not hits:  # typo pass, exact matches always win
        for pos, t in enumerate(ordered):
            if t == excl:
                continue
            ft = _fuzzy_topic(t)
            if ft:
                hits[ft] = min(hits.get(ft, 1 << 30), pos)
    if not hits:
        return None
    specific = {t: pos for t, pos in hits.items() if t != TOPIC_FUTURE}
    if specific:
        return min(specific, key=specific.get)
    return TOPIC_FUTURE


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
    # affirmations / fillers — «Да, про деньги» must never yield the name «Да»
    "да", "нет", "не", "ага", "угу", "ок", "окей", "ладно", "давай", "хорошо", "конечно", "ну",
    # adverbs/particles seen in production («Очень большая девушка…», «Только если бесплатно»)
    "очень", "только", "просто", "уже", "еще", "ещё", "вот", "тоже", "пока", "может",
}
_MARKERS = ("зовут", "звать", "я", "это")
_NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\-]{1,19}$")
# every topic word, to keep «Деньги»/«Любовь и деньги» from being read as a name
_ALL_TOPIC_TOKENS = frozenset().union(*_TOPIC_WORDS.values())


def _valid_name(tok: str, allow_topic_word=False) -> bool:
    """allow_topic_word: only the explicit «меня зовут X» marker may accept a name that
    doubles as a topic word (a real person called Любовь); everywhere else a topic word
    is a topic answer («Деньги», «любовь и деньги»), never a name."""
    if not _NAME_RE.match(tok) or tok.lower() in _NON_NAME \
            or tok.lower() == config.CODE_WORD.lower():
        return False
    if not allow_topic_word and norm(tok) in _ALL_TOPIC_TOKENS:
        return False
    return True


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
            for j in (i + 1, i - 1):       # explicit marker -> even Любовь/Роман are names
                if 0 <= j < len(toks) and _valid_name(toks[j], allow_topic_word=True):
                    return _cap(toks[j])
        elif t in ("я", "это") and i + 1 < len(toks) and _valid_name(toks[i + 1]):
            return _cap(toks[i + 1])
    if toks and _valid_name(toks[0]):      # otherwise the first token, if name-like
        return _cap(toks[0])
    return None
