"""Even-but-random variant selection via a persisted shuffled bag.

Guarantees each of the N variants is used exactly once per full cycle (max-min
usage difference = 0 within a cycle), in random order, and survives restarts.
"""
import random

from .db import transaction


def _all_variant_ids(conn):
    return [r["variant_id"] for r in conn.execute(
        "SELECT variant_id FROM variants ORDER BY variant_id")]


def rebuild_bag(conn, rng=None):
    ids = _all_variant_ids(conn)
    if not ids:
        raise RuntimeError("no variants imported; run the importer first")
    (rng or random).shuffle(ids)
    with transaction(conn):
        conn.execute("DELETE FROM bag")
        conn.executemany("INSERT INTO bag(position, variant_id) VALUES (?, ?)",
                         list(enumerate(ids)))
        conn.execute("INSERT OR REPLACE INTO bag_cursor(id, pos) VALUES (1, 0)")


def draw_variant(conn, rng=None) -> int:
    """Atomically pop the next variant id; regenerate the bag when exhausted."""
    for _ in range(2):
        row = conn.execute("SELECT pos FROM bag_cursor WHERE id = 1").fetchone()
        size = conn.execute("SELECT COUNT(*) AS c FROM bag").fetchone()["c"]
        if row is None or size == 0 or row["pos"] >= size:
            rebuild_bag(conn, rng)
            continue
        pos = row["pos"]
        vid = conn.execute("SELECT variant_id FROM bag WHERE position = ?",
                           (pos,)).fetchone()["variant_id"]
        # advance cursor only if still at pos (guards against a double-draw)
        cur = conn.execute(
            "UPDATE bag_cursor SET pos = pos + 1 WHERE id = 1 AND pos = ?", (pos,))
        if cur.rowcount == 1:
            return vid
    raise RuntimeError("failed to draw a variant")
