#!/usr/bin/env python3
"""Parse «Воронка таро.xlsx» into the DB + media files.

Default (safe — use for CONTENT UPDATES on a live bot):
    python3 run_import.py          # rebuilds content only; clients/history preserved

Full reset (fresh install; wipes EVERYTHING incl. live clients — dangerous!):
    python3 run_import.py --fresh
"""
import sys

from bot.importer import run

if __name__ == "__main__":
    fresh = "--fresh" in sys.argv
    if fresh:
        print("!!! --fresh: будут СТЁРТЫ клиенты, история и события (полный сброс) !!!")
    run(wipe_runtime=fresh)
