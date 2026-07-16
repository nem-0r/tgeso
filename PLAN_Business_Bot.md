r

# Вариант A — Telegram Business: полный план реализации автоответчика

> Отдельный подробный документ по **Варианту A** (Telegram Business + подключённый бот).
> Автоответчик отвечает **от лица гадалки, как живой человек** — клиент не видит, что это бот.
> Все технические факты ниже **проверены** по официальным докам Telegram/aiogram (deep-research, фактчек 2026, цитаты в §14).
> Дата: 2026-07-01. Дополняет общий `PLAN.md`. Статус: на согласование → затем реализация.

---

## 1. Что это за документ и главный вывод

Здесь — «от начала до конца», как сделать автоответчик на **Telegram Business**: как он технически пишет от имени гадалки, **как шлёт фото**, как выглядит «по-живому», **как устроена база данных**, **какой VPS брать** и как задеплоить.

**Главный проверенный вывод:** Bot API полностью покрывает вашу воронку.

- Бот шлёт **текст и фото «от лица» аккаунта**, указывая параметр `business_connection_id` в обычных методах `sendMessage` / `sendPhoto` / `sendMediaGroup` / `sendChatAction` (появился в Bot API 7.2, 31.03.2024).
- Клиент **не видит никакого «бота»** — сообщение приходит под именем и аватаркой гадалки. Флаг `via_business_bot_id` — это внутренние метаданные протокола, официальные клиенты его получателю **не показывают**. Маленький индикатор «вручную/бот» видит только сама гадалка.
- **Отложенные** сообщения (+7 / +5 / +15 мин, +30 сек) **разрешены**: подключённый бот может писать в течение **скользящего окна 24 часа** с момента последнего входящего от клиента (право `can_reply`). Вся воронка укладывается в минуты — ограничение «только мгновенный ответ» отсутствует.
- **«Печатает…»** работает через `sendChatAction` (`typing` для текста, `upload_photo` для фото). Бот может **помечать входящие прочитанными** (право `can_read_messages` → `readBusinessMessage`). Прочтения клиента бот видеть не может (не критично).

Итог: технических блокеров нет. Ниже — как это собрать правильно.

---

## 2. Как клиент видит переписку (человекоподобность) — самое важное

Ваше требование: клиент не должен догадаться, что пишет бот. Что мы для этого делаем:

1. **Нет метки «бот».** Проверено на уровне протокола: сообщения бизнес-соединения приходят под именем/аватаркой гадалки, без «bot», без «via @bot», без @username. (§14, источник 1–2.)
2. **«Печатает…» перед каждым сообщением.** Показываем `typing` на 2–6 сек (пропорционально длине текста) и `upload_photo` перед отправкой карты — как живой человек, который набирает.
3. **Живые задержки (джиттер).** Не ровно 7:00, а 6–9 мин; +5 мин → ±60 сек; +15 мин → ±3 мин. Ровные интервалы — единственное, что выдаёт робота.
4. **Помечаем входящие прочитанными** (`readBusinessMessage`) — у клиента видно, что «гадалка прочитала», и только потом печатает ответ.
5. **Персонализация именем** + **60 разных связок** → нет двух одинаковых переписок.
6. **Разбивка на короткие сообщения.** Иногда длинный ответ шлём в 2 коротких — так пишут люди (опционально, настраивается).
7. **Тихие часы** (опционально): не отправлять диагностику/CTA ночью по её часовому поясу.
8. **Ручной перехват.** Если гадалка/оператор сама пишет в чат — бот встаёт на паузу в этом чате (встроено в Telegram Business + наша логика).

> Единственный теоретический «след»: модифицированный неофициальный клиент мог бы прочитать флаг в данных сообщения. В обычных Telegram (iOS/Android/Desktop/Web) клиент не видит ничего. Это не наш кейс.

---

## 3. Как бот технически пишет «от лица гадалки»

Механика (Bot API поверх MTProto):

- Гадалка подключает нашего бота в Telegram Business. Telegram создаёт **бизнес-соединение** с уникальным `business_connection_id`.
- Наш бот получает апдейты о входящих в её личку (`business_message`) и об изменении соединения (`business_connection`).
- Чтобы ответить **от её имени**, бот вызывает обычный метод отправки, **добавляя** `business_connection_id`:

```python
# aiogram 3.x — отправка текста ОТ ИМЕНИ гадалки
await bot.send_message(
    chat_id=client_id,                     # кому (клиент)
    text="ПРИВЕТ, рада, что ты здесь 🃏",
    business_connection_id=bcid,           # ← вот это делает сообщение "от гадалки"
)
```

Под капотом Telegram оборачивает запрос в `invokeWithBusinessConnection` — т.е. отправляет так, будто это сделала сама гадалка. (§14, источник 1.)

**Права.** При подключении бот получает объект прав `BusinessBotRights`. Нам нужны минимум:

- `can_reply` — отвечать и редактировать в чатах с входящими за последние 24 ч (это ядро воронки);
- `can_read_messages` — помечать входящие прочитанными (для «по-живому»).

(В Bot API 9.0 от 11.04.2025 простой булев `can_reply` заменён на объект `BusinessBotRights` — читаем права оттуда.)

---

## 4. Как отправляются ФОТО (карты Таро) — подробно

Фото отправляются тем же механизмом — `sendPhoto` с `business_connection_id`.

### 4.1. Хранение и кэш `file_id`

- **Оригиналы карт лежат на диске** в папке `media/` — это источник истины (~24 файла).
- **Первая отправка** каждой карты: заливаем файл с диска, Telegram возвращает `file_id`.
- **Дальше переиспользуем `file_id`** (быстро, без повторной загрузки) — кэшируем в таблице `media`.
- **Подстраховка:** если `file_id` когда-то перестанет приниматься — просто перезаливаем с диска (диск всегда истина). Поэтому оригиналы не удаляем.

```python
# Первая отправка — заливаем файл, запоминаем file_id
from aiogram.types import FSInputFile

msg = await bot.send_photo(
    chat_id=client_id,
    photo=FSInputFile("media/tower.jpg"),
    caption=diagnosis_short,               # подпись под фото (лимит 1024 символа)
    business_connection_id=bcid,
)
file_id = msg.photo[-1].file_id            # ← кэшируем в БД (media.file_id)

# Последующие отправки той же карты — по file_id (мгновенно)
await bot.send_photo(chat_id=other_client, photo=file_id,
                     caption=..., business_connection_id=bcid)
```

### 4.2. Фото + длинный текст диагностики

Подпись к фото ограничена **1024 символами**, а диагностика в воронке длиннее. Стратегия:

- Показываем `upload_photo` («отправляет фото…»), **отправляем карту** (с короткой подписью или без);
- затем `typing` и **отправляем полный текст** диагностики отдельным сообщением;
- потом (+30 сек) — CTA.

Это и обходит лимит подписи, и выглядит **естественнее** (сначала «прислала карту», потом «расписала»).

```python
await bot.send_chat_action(client_id, "upload_photo", business_connection_id=bcid)
await bot.send_photo(client_id, photo=file_id, business_connection_id=bcid)   # карта
await human_typing(bot, client_id, bcid, text=diagnosis_full)                 # "печатает…"
await bot.send_message(client_id, diagnosis_full, business_connection_id=bcid)# разбор
```

---

## 5. Отложенные сообщения (+7 / +5 / +15 / +30 сек)

- Разрешены: право `can_reply` даёт отправку в течение **24 ч** от последнего входящего клиента. Наши шаги — минуты, всё внутри окна. (§14, источник 3.)
- Планирование делаем **на нашем сервере** (см. §7 «Планировщик»): для каждого клиента в таблицу `steps` кладём строки с `run_at` (время отправки в UTC), а фоновый цикл раз в ~10 сек отправляет «созревшие» шаги.
- Если клиент замолчал и 24 ч прошли — отправить уже нельзя; для нашей воронки это неактуально (весь цикл ~30 мин), но «догоняющие» follow-upّы, если захотим, должны быть внутри 24 ч.

---

## 6. Настройка BotFather и получение апдейтов

Что делаем мы в @BotFather (один раз):

1. Создаём бота → получаем **токен**.
2. **Включаем Business Mode:** @BotFather → `/mybots` → наш бот → **Bot Settings → Business Mode → Turn On**. Без этого бота нельзя подключить к бизнес-аккаунту.
3. Выдаём/подтверждаем нужные права (`can_reply`, `can_read_messages`) — фактически права выдаёт владелец при подключении, мы просто их используем.

Как бот получает события:

- Подписываемся на апдейты `business_connection`, `business_message`, `edited_business_message`, `deleted_business_messages`. **Важно:** они **не приходят по умолчанию** — их нужно явно указать в `allowed_updates`.
- Доставка — **long-polling** (`getUpdates`) или **webhook**. Для нас — **long-polling** (проще, не нужен домен/HTTPS; §9).

```python
# aiogram 3.x — подписка на бизнес-апдейты и хендлеры
dp = Dispatcher()

@dp.business_connection()
async def on_connect(evt: BusinessConnection):
    # сохранить business_connection_id, user_chat_id, права (can_reply/can_read)
    ...

@dp.business_message()
async def on_message(msg: Message):
    # входящее клиента в личку гадалки → в движок воронки
    ...

await dp.start_polling(
    bot,
    allowed_updates=["business_connection", "business_message",
                     "edited_business_message", "deleted_business_messages"],
)
```

---

## 7. Архитектура системы (Вариант A)

```
Клиент ──пишет в личку гадалки──▶ Telegram ──business_message──▶ НАШ БОТ (aiogram, long-polling)
                                                                      │
                                                                      ▼
                                   ┌──────────────────────────────────────────────┐
                                   │ ДВИЖОК ВОРОНКИ                                │
                                   │  • роутер входящих: кодовое слово / имя / стоп│
                                   │  • state machine клиента                     │
                                   │  • выбор связки (shuffled-bag, поровну из 60) │
                                   │  • человекоподобность (typing, джиттер, read) │
                                   │  • хенд-офф оператору                        │
                                   └───────┬───────────────────────┬──────────────┘
                                           │ ставит шаги (run_at)   │ читает/пишет состояние
                                           ▼                        ▼
                                   ┌────────────────┐      ┌───────────────────────────┐
                                   │ ПЛАНИРОВЩИК    │◀────▶│ БАЗА ДАННЫХ (SQLite WAL)  │
                                   │ DB-поллер ~10с │      │ §8: clients, steps,       │
                                   │ + джиттер      │      │ variants, variant_bag,    │
                                   └───────┬────────┘      │ media, sent_log, ...      │
                                           │ отправка через business_connection_id
                                           ▼
                                   НАШ БОТ ──sendMessage/sendPhoto/sendChatAction──▶ Клиент
                                   (приходит ОТ ИМЕНИ гадалки)
```

Принцип: «мозг» (воронка/БД) отделён от «транспорта» (bot API). Если когда-нибудь понадобится userbot (Вариант B) — меняем только транспорт, БД и логику не трогаем.

---

## 8. База данных — как реализуем (подробно)

**СУБД: SQLite в режиме WAL**, доступ через **SQLAlchemy 2.x** (+ `aiosqlite`).
Почему SQLite: один всегда-включённый процесс, ~30 диалогов/день, крошечная конкурентность → SQLite идеален (в процессе, ~0 RAM, бэкап = копия одного файла). Единственное ограничение SQLite (один писатель) для нас неактуально — мы однопроцессные. Через SQLAlchemy переход на PostgreSQL = смена одной строки-URL, если вырастем.
Настройки: `PRAGMA journal_mode=WAL; busy_timeout=5000; synchronous=NORMAL`.

> Примечание честности: выбор SQLite здесь — инженерное суждение под этот масштаб (в источниках отдельно не «доказан»), но это стандартная практика для однопроцессной нагрузки такого объёма.

### 8.1. Схема (SQLite DDL)

```sql
-- Подключённые бизнес-аккаунты (обычно один — гадалка)
CREATE TABLE business_connections (
    business_connection_id TEXT PRIMARY KEY,
    owner_user_id          INTEGER NOT NULL,     -- id аккаунта гадалки
    can_reply              INTEGER NOT NULL DEFAULT 1,
    can_read               INTEGER NOT NULL DEFAULT 1,
    is_enabled             INTEGER NOT NULL DEFAULT 1,
    tz                     TEXT DEFAULT 'Europe/Moscow',
    connected_at           TEXT NOT NULL
);

-- Клиенты / диалоги (одна строка = один клиент = одна воронка)
CREATE TABLE clients (
    client_id       INTEGER PRIMARY KEY,          -- Telegram user id клиента
    business_connection_id TEXT NOT NULL REFERENCES business_connections,
    state           TEXT NOT NULL DEFAULT 'NEW',   -- NEW/TRIGGERED/AWAITING_ANSWER/
                                                   -- PROCESSING/DIAGNOSING/CTA_SENT/
                                                   -- COMPLETED/HANDOFF/STOPPED
    variant_id      INTEGER REFERENCES variants,   -- назначенная связка (1 из 60)
    name            TEXT,                          -- имя из ответа клиента
    question        TEXT,                          -- запрос клиента
    triggered_at    TEXT,                          -- когда пришло кодовое слово
    answered_at     TEXT,                          -- когда клиент назвал имя/запрос
    last_incoming_at TEXT,                         -- для контроля окна 24 ч
    version         INTEGER NOT NULL DEFAULT 0,    -- optimistic-lock (защита от гонок)
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Запланированные шаги (двигают таймеры воронки)
CREATE TABLE steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   INTEGER NOT NULL REFERENCES clients,
    step_name   TEXT NOT NULL,                     -- greeting/working/diagnosis/cta/nudge/fallback
    run_at      TEXT NOT NULL,                     -- когда отправить (UTC ISO8601)
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending/sent/skipped/cancelled
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_steps_due ON steps(status, run_at);

-- 60 связок-диагностик (контент из вашей таблицы)
CREATE TABLE variants (
    variant_id       INTEGER PRIMARY KEY,
    topic            TEXT,                          -- love/finance/... (для будущего)
    diagnosis_text   TEXT NOT NULL,                 -- шаблон с {name}
    cta_text         TEXT,                          -- если своё CTA; иначе общее
    card_media_key   TEXT REFERENCES media(media_key),
    is_active        INTEGER NOT NULL DEFAULT 1,
    weight           INTEGER NOT NULL DEFAULT 1     -- на будущее (веса)
);
-- Общие шаги (greeting/ask/working/cta) храним как настройки, если они одинаковы для всех.

-- Картинки карт: диск = истина, file_id = кэш
CREATE TABLE media (
    media_key   TEXT PRIMARY KEY,                  -- напр. 'tower', 'lovers'
    file_path   TEXT NOT NULL,                     -- media/tower.jpg
    file_id     TEXT,                              -- кэш Telegram после 1-й отправки
    uploaded_at TEXT
);

-- "Перемешанный мешок" для равномерно-случайного выбора 1 из 60
CREATE TABLE variant_bag (
    position    INTEGER PRIMARY KEY,               -- порядок в текущем цикле
    variant_id  INTEGER NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE bag_cursor (id INTEGER PRIMARY KEY CHECK (id=1), pos INTEGER NOT NULL);

-- Идемпотентность: гарантия "один шаг отправлен один раз"
CREATE TABLE sent_log (
    client_id   INTEGER NOT NULL,
    step_name   TEXT NOT NULL,
    tg_message_id INTEGER,
    sent_at     TEXT NOT NULL,
    PRIMARY KEY (client_id, step_name)
);

-- Лог событий (аудит/отладка, опционально)
CREATE TABLE events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id  INTEGER,
    kind       TEXT,                               -- incoming/outgoing/handoff/error
    payload    TEXT,
    created_at TEXT NOT NULL
);
```

### 8.2. Как таблицы работают вместе

- Пришло входящее → смотрим `clients.state`:
  - нет клиента + текст == кодовое слово → создаём `clients` (`state=TRIGGERED`), достаём связку из `variant_bag`, кладём шаг `greeting` в `steps` на `now+7мин±джиттер`.
  - `state=AWAITING_ANSWER` → это имя/запрос: пишем `name/question`, `state=PROCESSING`, ставим `working` на `+5мин±`.
  - ключевые слова «купить/цена/оплата» или ручное сообщение оператора → `state=HANDOFF`, шлём уведомление, отменяем `pending`-шаги.
- **Планировщик** раз в ~10 сек: `SELECT ... FROM steps WHERE status='pending' AND run_at<=now` → отправляет → в той же транзакции `status='sent'` + запись в `sent_log`. Пережил перезапуск — «просроченные» шаги просто отправятся на следующем тике.
- **Гонки** между входящим и планировщиком по одному клиенту исключаем через `version` (optimistic-lock) или `SELECT ... FOR UPDATE`-семантику.

### 8.3. Выбор 1 из 60 — случайно и поровну (shuffled-bag)

- Генерируем случайную перестановку всех 60 `variant_id` в `variant_bag`, курсор в `bag_cursor`.
- Каждому новому клиенту атомарно берём следующий id и двигаем курсор.
- Мешок кончился → новая перестановка. Итог: за каждые 60 клиентов каждая связка — ровно 1 раз, порядок случайный, переживает перезапуск.

---

## 9. VPS и деплой

### 9.1. Сервер

- **Hetzner Cloud CAX11** (ARM Ampere): **2 vCPU, 4 GB RAM, 40 GB NVMe SSD** — с большим запасом для одного Python-бота на ~30 диалогов/день. (§14, источник 6.)
- Цена: ориентировочно **€4–5/мес** (точную цену 2026 подтвердим при заказе — в источниках проверены характеристики, не цена).
- ОС: **Ubuntu 24.04 LTS (ARM)**.
- Альтернативы: Hetzner CX22 (x86, если нужен x86), Netcup, Oracle Cloud Free ARM (бесплатно, но менее надёжно для 24/7). Рекомендую **CAX11**.

### 9.2. Доставка апдейтов: long-polling (рекомендую)

- **Long-polling** (`getUpdates`): бот сам опрашивает Telegram. **Не нужен домен, HTTPS, вебхук** — проще и надёжнее для нашего объёма.
- Webhook нужен только под высокие нагрузки/мгновенность — нам не требуется.

### 9.3. Запуск и надёжность

- **systemd-сервис** `Restart=always` (один процесс) или **Docker Compose** `restart: unless-stopped`.
- Никогда не запускать **два экземпляра** с одним токеном одновременно (конфликт `getUpdates`).
- **Логи**: journald + алерты оператору в Telegram (падение, ошибка отправки, 429).
- **Бэкапы**: ежедневно, зашифрованные (restic/borg): файл SQLite + `media/` + `.env`. Hetzner Storage Box (~€4/мес) или S3.
- **Секрет**: токен бота в `.env` (chmod 600, не в git). Компрометация несравнимо менее опасна, чем session userbot'а: доступа к перепискам нет, отзывается в 1 клик.

### 9.4. Пример `systemd`-юнита

```ini
[Unit]
Description=Tarot Business Bot
After=network-online.target

[Service]
WorkingDirectory=/opt/tarot-bot
ExecStart=/opt/tarot-bot/.venv/bin/python -m bot.main
EnvironmentFile=/opt/tarot-bot/.env
Restart=always
RestartSec=5
User=tarot

[Install]
WantedBy=multi-user.target
```

---

## 10. Технологический стек и структура кода

| Слой               | Выбор                                       | Обоснование                                                                                                                                                          |
| ---------------------- | ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Язык               | Python 3.12 (asyncio)                            | Экосистема Telegram                                                                                                                                                   |
| Библиотека   | **aiogram 3.29.0** (Bot API 10.1)          | Актив­ная, нативно поддерживает бизнес-соединения (§14, источник 5). Альтернатива — python-telegram-bot v22.8 |
| БД                   | SQLite (WAL) + SQLAlchemy 2.x + aiosqlite        | §8                                                                                                                                                                             |
| Планировщик | Собственный DB-поллер (asyncio) | Переживает перезапуск, таймеры+состояние в одной транзакции                                                                 |
| Доставка       | long-polling                                     | Без домена/HTTPS                                                                                                                                                       |
| Процесс         | systemd`Restart=always` / Docker               | Автоперезапуск                                                                                                                                                    |

Структура проекта:

```
tarot-bot/
  bot/
    main.py         # запуск, Dispatcher, long-polling, allowed_updates
    config.py       # .env, настройки, тайминги, кодовое слово
    db.py           # модели SQLAlchemy + сессия + PRAGMA WAL
    handlers.py     # @business_connection, @business_message
    funnel.py       # state machine + роутер входящих
    scheduler.py    # DB-поллер: отправка "созревших" шагов
    sender.py       # обёртка: send_text/send_photo/typing/read (с business_connection_id)
    variants.py     # shuffled-bag выбор 1 из 60
    media.py        # отправка фото + кэш file_id
    humanize.py     # джиттер задержек, длительность "печатает", тихие часы
    operator.py     # уведомления/хенд-офф
  content/          # 60 связок (импорт из вашей Google-таблицы) → variants
  media/            # ~24 картинки карт (источник истины)
  data/             # tarot.sqlite + бэкапы
  .env              # BOT_TOKEN, OPERATOR_CHAT_ID, ... (chmod 600)
  requirements.txt
  systemd/tarot-bot.service
```

---

## 11. Логика воронки (Вариант A, кратко)

Соответствует вашим ответам: триггер = **кодовое слово**; **ждём имя**, потом продолжаем; 1 аккаунт, ~30/день; выбор связки — **случайно поровну**.

```
NEW ──(входящее == кодовое слово)──▶ TRIGGERED ──greeting @ +7мин(±)──▶ шлём приветствие+вопрос
TRIGGERED ─────────────────────────▶ AWAITING_ANSWER
AWAITING_ANSWER ──(клиент прислал имя/запрос)──▶ PROCESSING ──working @ +5мин(±)──▶ DIAGNOSING
   └─(молчит N мин)─▶ nudge; (ещё M мин молчит)─▶ обобщённый вариант/парковка
DIAGNOSING ──карта+диагностика @ +15мин(±)──▶ cta @ +30сек(±)──▶ CTA_SENT
CTA_SENT ──(ответ/«купить/цена»)──▶ HANDOFF (пауза + уведомить оператора) │ (молчит)─▶ COMPLETED
```

Все времена в UTC; конверсия в часовой пояс гадалки только для «тихих часов»/показа.

---

## 12. Ограничения Business-бота (gotchas — что важно знать)

1. **Окно 24 часа.** Слать/редактировать можно только в чатах с входящим за последние 24 ч. Для нашей воронки (минуты) — не проблема; но «догоняющие» через сутки — нельзя.
2. **Права нужно выдать при подключении** (`can_reply`, `can_read_messages`). Проверяем, что они есть, при `business_connection`. (Миф «проверять `can_reply` перед каждым ответом» — **опровергнут** в исследовании; достаточно наличия права.)
3. **Rich-media** бот может слать, только если сам пользователь (гадалка) вправе слать такие сообщения — для обычного аккаунта это норма.
4. **Бизнес-апдейты не приходят по умолчанию** — обязательно добавить в `allowed_updates`.
5. **Флаг `via_business_connection`** есть в данных сообщения (виден владельцу), но **не показывается клиенту** официальными клиентами.
6. **Прочтения клиента** бот видеть не может (только помечать свои входящие прочитанными).

---

## 13. Риски, открытые вопросы и стоимость

### Открытые вопросы (проверим на Шаге 0 / до запуска)

- **Нужен ли гадалке Telegram Premium** для подключения бизнес-бота. Business исторически — функция Premium, поэтому с очень высокой вероятностью **да**; но в источниках этого раза отдельно не подтверждено — проверим при настройке. (Бюджет закладываем с Premium.)
- **Специфические анти-спам лимиты бизнес-соединений** и риск ограничения именно аккаунта при авто-ответах — в докладах не квантифицированы. Общий лимит — **~1 сообщение/сек в чат** (у нас несопоставимо меньше). Риск для Варианта A несравнимо ниже, чем у userbot, но держим человекоподобность и вменяемые объёмы.
- Точная цена CAX11 2026 — подтвердим при заказе.

### Комплаенс (юридический момент, не техника)

- В некоторых юрисдикциях (ЕС, **AI Act, ст. 50, с 02.08.2026**) есть требование уведомлять человека, что он общается с ИИ, «если это не очевидно». Технически Telegram метку не показывает — это ваше бизнес-решение и зона ответственности; отметьте для своей юрисдикции/аудитории.

### Стоимость (в месяц)

- VPS Hetzner CAX11: ~€5
- Telegram Premium (гадалке): ~$5
- Бэкапы (Storage Box): ~€4
- **Итого: ~$14–15/мес.** Разовое: ~0 (домен не нужен).

---

## 14. Источники (проверено, deep-research 2026)

1. Bot API — `business_connection_id` в методах отправки: [https://core.telegram.org/bots/api](https://core.telegram.org/bots/api)
2. Что видит клиент / `via_business_bot_id`: [https://core.telegram.org/api/bots/connected-business-bots](https://core.telegram.org/api/bots/connected-business-bots), [https://core.telegram.org/constructor/message](https://core.telegram.org/constructor/message), [https://grammy.dev/advanced/business](https://grammy.dev/advanced/business)
3. Окно 24 ч / `BusinessBotRights.can_reply`: [https://core.telegram.org/constructor/businessBotRights](https://core.telegram.org/constructor/businessBotRights), [https://core.telegram.org/bots/api](https://core.telegram.org/bots/api)
4. `sendChatAction` / чтение сообщений / changelog 7.2 и 9.0: [https://core.telegram.org/bots/api-changelog](https://core.telegram.org/bots/api-changelog), [https://core.telegram.org/bots/features](https://core.telegram.org/bots/features)
5. aiogram 3.29.0 (бизнес-методы): [https://docs.aiogram.dev/en/latest/changelog.html](https://docs.aiogram.dev/en/latest/changelog.html), [https://docs.aiogram.dev/en/latest/api/methods/send_photo.html](https://docs.aiogram.dev/en/latest/api/methods/send_photo.html)
6. VPS Hetzner CAX11: [https://www.hetzner.com/cloud/cost-optimized](https://www.hetzner.com/cloud/cost-optimized)
7. Лимиты (~1 msg/sec, 429): [https://core.telegram.org/bots/faq](https://core.telegram.org/bots/faq)

---

## 15. План реализации по шагам (Вариант A)

| Шаг                                                 | Что делаем                                                                                                                                                                                   | Оценка    |
| ------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------- |
| **0. Спайк**                                | Тест-бот + тест-Business-аккаунт: подтвердить отложенную отправку, фото, «печатает…», чтение, и**нужен ли Premium** | 0.5–1 день |
| **1. Скелет + БД**                       | Схема §8, SQLAlchemy, WAL, конфиг,`.env`                                                                                                                                                | 1 день      |
| **2. Транспорт + хендлеры**     | `sender.py` (send_text/photo/typing/read + business_connection_id), `@business_connection`, `@business_message`                                                                                 | 1–2 дня     |
| **3. Движок + планировщик**     | state machine, роутер (кодовое слово/имя/стоп), DB-поллер, джиттер                                                                                              | 2–3 дня     |
| **4. Контент + фото**                 | импорт 60 связок из таблицы, карты в`media/`, кэш `file_id`, персонализация                                                                           | 1–2 дня     |
| **5. Выбор связки + хенд-офф** | shuffled-bag, уведомления оператору, авто-пауза                                                                                                                          | 1 день      |
| **6. Надёжность**                      | идемпотентность, краевые случаи, обработка 429/retry_after, мониторинг                                                                                 | 1–2 дня     |
| **7. Деплой**                              | Hetzner CAX11, systemd/Docker, бэкапы, алерты                                                                                                                                             | 1 день      |
| **8. Пилот → запуск**                | 3–5 клиентов под присмотром → ramp до 30/день                                                                                                                            | 2–4 дня     |

**Итого: ~2–3 недели** до стабильного боевого режима.

---

## 16. Что нужно от вас, чтобы стартовать

1. Подтвердить **Вариант A**.
2. **Кодовое слово** (точная фраза-триггер).
3. **Доступ к картинкам** (открыть Google Doc «по ссылке» или прислать файлы карт).
4. **@username оператора** — кому слать «горячего» лида.
5. **Часовой пояс** гадалки + нужны ли «тихие часы».
6. Подтвердить у гадалки **наличие/готовность Premium** (для подключения Business).

Как только это будет — начинаю с Шага 0 и далее по плану.

---

*Конец документа. Технические факты проверены (deep-research, 24/25 утверждений подтверждены, цитаты в §14). Жду ваш «ОК» по §16.*
