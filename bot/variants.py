"""Even-but-random variant selection via persisted PER-TOPIC shuffled bags.

Each topic (Любовь/отношения, Финансы, Будущее) has its own bag: within a topic
every variant is used exactly once per full cycle (max-min usage difference = 0),
in random order, and the cursors survive restarts. Which topic a client gets is
decided by the funnel (detected from their message, or a card-time fallback).
"""
import random

from .db import transaction


def topics(conn):
    return [r["topic"] for r in conn.execute(
        "SELECT DISTINCT topic FROM variants ORDER BY topic")]


def _topic_variant_ids(conn, topic):
    return [r["variant_id"] for r in conn.execute(
        "SELECT variant_id FROM variants WHERE topic=? ORDER BY variant_id", (topic,))]


def rebuild_bag(conn, topic, rng=None):
    ids = _topic_variant_ids(conn, topic)
    if not ids:
        raise RuntimeError(f"no variants for topic {topic!r}; run the importer first")
    (rng or random).shuffle(ids)
    with transaction(conn):
        conn.execute("DELETE FROM bag WHERE topic=?", (topic,))
        conn.executemany("INSERT INTO bag(topic, position, variant_id) VALUES (?, ?, ?)",
                         [(topic, i, v) for i, v in enumerate(ids)])
        conn.execute("INSERT OR REPLACE INTO bag_cursor(topic, pos) VALUES (?, 0)", (topic,))


def draw_variant(conn, topic, rng=None) -> int:
    """Atomically pop the next variant id of `topic`; regenerate its bag when exhausted."""
    for _ in range(2):
        row = conn.execute("SELECT pos FROM bag_cursor WHERE topic=?", (topic,)).fetchone()
        size = conn.execute("SELECT COUNT(*) AS c FROM bag WHERE topic=?", (topic,)).fetchone()["c"]
        if row is None or size == 0 or row["pos"] >= size:
            rebuild_bag(conn, topic, rng)
            continue
        pos = row["pos"]
        vid = conn.execute("SELECT variant_id FROM bag WHERE topic=? AND position=?",
                           (topic, pos)).fetchone()["variant_id"]
        # advance cursor only if still at pos (guards against a double-draw)
        cur = conn.execute(
            "UPDATE bag_cursor SET pos = pos + 1 WHERE topic=? AND pos=?", (topic, pos))
        if cur.rowcount == 1:
            return vid
    raise RuntimeError("failed to draw a variant")
