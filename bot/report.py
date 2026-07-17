"""Operator digest — analytics over the events log + sent_log.

Production pattern: the SCHEDULED digest reports a CLOSED period (the full previous
local day), so numbers are final and no time is ever dropped. The on-demand button
shows "today so far" (the in-progress day). Moscow is UTC+3 with NO DST, so a fixed
offset is correct year-round (no timezone library needed).
"""
import html
import time

from . import config


def _offset_seconds():
    return config.REPORT_TZ_OFFSET_HOURS * 3600


def local_parts(now):
    """(date_str 'YYYY-MM-DD', hour) at the operator's local time for epoch `now`."""
    lt = time.gmtime(now + _offset_seconds())
    return time.strftime("%Y-%m-%d", lt), lt.tm_hour


def day_window(now):
    """[start, end) epoch UTC of the LOCAL day containing `now` (today)."""
    off = _offset_seconds()
    shifted = now + off
    local_midnight = shifted - (shifted % 86400)   # 00:00 local, in the shifted frame
    start = local_midnight - off                    # back to a real UTC epoch
    return start, start + 86400


def prev_day_window(now):
    """[start, end) epoch UTC of the FULL previous local day (yesterday)."""
    today_start, _ = day_window(now)
    return today_start - 86400, today_start


def should_send(now, last_date):
    """(bool, today_str): fire once when it's >= REPORT_HOUR local AND not sent today."""
    date_str, hour = local_parts(now)
    return (hour >= config.REPORT_HOUR and date_str != last_date), date_str


def _date_label(epoch):
    """DD.MM.YYYY for the local day containing `epoch`."""
    return time.strftime("%d.%m.%Y", time.gmtime(epoch + _offset_seconds()))


def collect(conn, start, end):
    """Metrics for the [start, end) epoch window."""
    triggered = conn.execute(
        "SELECT COUNT(*) AS c FROM events WHERE event='triggered' AND ts>=? AND ts<?",
        (start, end)).fetchone()["c"]
    readings = conn.execute(
        "SELECT COUNT(*) AS c FROM sent_log WHERE step_name='diagnosis' AND sent_at>=? AND sent_at<?",
        (start, end)).fetchone()["c"]
    hot_rows = conn.execute(
        "SELECT e.client_id AS cid, c.name AS name FROM events e "
        "LEFT JOIN clients c ON c.client_id = e.client_id "
        "WHERE e.event='hot_lead' AND e.ts>=? AND e.ts<? ORDER BY e.ts",
        (start, end)).fetchall()
    detected = conn.execute(
        "SELECT COUNT(*) AS c FROM events WHERE event='topic_detected' AND ts>=? AND ts<?",
        (start, end)).fetchone()["c"]
    fallback = conn.execute(
        "SELECT COUNT(*) AS c FROM events WHERE event='topic_fallback' AND ts>=? AND ts<?",
        (start, end)).fetchone()["c"]
    return {"triggered": triggered, "readings": readings,
            "topic_detected": detected, "topic_assigned": detected + fallback,
            "hot": [(r["cid"], r["name"]) for r in hot_rows]}


def _lead_link(cid, name):
    label = html.escape(name) if name else "клиент"
    return f'• <a href="tg://user?id={int(cid)}">{label}</a>'


def render(metrics, title):
    """HTML digest message from a metrics dict + a title line."""
    t, r, hot = metrics["triggered"], metrics["readings"], metrics["hot"]
    conv = round(len(hot) / t * 100) if t else 0
    # NB: topic detected/fallback stats stay INTERNAL (events table + collect());
    # the operator's digest deliberately does not include them.
    lines = [
        f"📊 <b>{title}</b> (МСК)",
        "",
        f"Написали ТАРО:      <b>{t}</b>",
        f"Дошли до разбора:   <b>{r}</b>",
        f"🔥 Горячих лидов:    <b>{len(hot)}</b>   (конверсия {conv}%)",
    ]
    if hot:
        lines += ["", "Горячие лиды (тап — открыть чат):"]
        lines += [_lead_link(cid, name) for cid, name in hot]
    else:
        lines += ["", "Горячих лидов не было."]
    return "\n".join(lines)


def build_report(conn, now, scope="today"):
    """scope='yesterday' -> closed previous day (scheduled digest);
       scope='today'     -> in-progress current day (on-demand button)."""
    if scope == "yesterday":
        start, end = prev_day_window(now)
        title = f"Итоги за {_date_label(start)}"
    else:
        start, end = day_window(now)
        title = f"Итоги за сегодня ({_date_label(start)}, день идёт)"
    return render(collect(conn, start, end), title)
