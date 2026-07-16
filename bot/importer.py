"""One-time importer: «Воронка таро.xlsx» -> SQLite + media files.

Applies every fix confirmed by the verification report:
  * cards joined by CANONICAL NAME (numbers are Arabic 0 + Roman I..XXI) — C1
  * image->variant mapping by DRAWING ANCHOR COLUMN, blobs de-duped by md5 — C2
  * name substituted ONLY in the r6 intro ('{name}'); r4/r8/r9 stored verbatim — H1
  * openpyxl loaded in FULL mode; text read from openpyxl, images from the zip
  * steps keyed by ROW POSITION (r3..r9), not by the timing string
Fails LOUD on any structural surprise so we never ship wrong content.
"""
import hashlib
import os
import re
import xml.etree.ElementTree as ET
import zipfile

import openpyxl

from . import config, content
from .db import connect, init, wipe, transaction, CONTENT_TABLES, RUNTIME_TABLES
from .variants import rebuild_bag

NS = {
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

# fixed row layout of every variant block
ROW = {"greeting": 3, "ask": 4, "working": 5, "intro": 6, "diagnosis": 8, "cta": 9}
SHARED_STEPS = ["greeting", "ask", "working", "intro", "cta"]


def _cell(rows, r, c):
    if 1 <= r <= len(rows):
        row = rows[r - 1]
        if 0 <= c < len(row):
            v = row[c]
            return "" if v is None else str(v)
    return ""


def extract_images_by_anchor(xlsx_path):
    """Return {variant_idx: (media_filename, bytes)} using drawing anchors.
    variant_idx = (from_col - 2) // 4 ; asserts a clean 66-way bijection."""
    out = {}
    with zipfile.ZipFile(xlsx_path) as z:
        names = set(z.namelist())
        drawings = sorted(n for n in names if re.match(r"xl/drawings/drawing\d+\.xml$", n))
        for d in drawings:
            rels_path = f"xl/drawings/_rels/{os.path.basename(d)}.rels"
            rels = {}
            if rels_path in names:
                relroot = ET.fromstring(z.read(rels_path))
                for rel in relroot:
                    rels[rel.get("Id")] = rel.get("Target")
            root = ET.fromstring(z.read(d))
            for anchor in root:
                tag = anchor.tag.split("}")[-1]
                if tag not in ("oneCellAnchor", "twoCellAnchor"):
                    continue
                frm = anchor.find("xdr:from", NS)
                if frm is None:
                    continue
                col = int(frm.find("xdr:col", NS).text)
                row = int(frm.find("xdr:row", NS).text)
                blip = anchor.find(".//a:blip", NS)
                if blip is None:
                    continue
                embed = blip.get(f"{{{NS['r']}}}embed")
                target = rels.get(embed)
                if not target:
                    continue
                media_path = "xl/" + target.replace("../", "")
                data = z.read(media_path)
                if row != 6:
                    raise ValueError(f"image anchor at unexpected row {row} (expected 6)")
                if (col - 2) % 4 != 0:
                    raise ValueError(f"image anchor at unexpected col {col}")
                vidx = (col - 2) // 4
                out[vidx] = (os.path.basename(media_path), data)
    return out


def parse_workbook(xlsx_path):
    wb = openpyxl.load_workbook(xlsx_path)  # FULL mode (H2) — do NOT use read_only
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    ncols = max((len(r) for r in rows), default=0)

    timing_cols = sorted(c for c in range(ncols)
                         if _cell(rows, 2, c).strip() == "Тайминг")
    if len(timing_cols) != 66:
        raise ValueError(f"expected 66 variant blocks, found {len(timing_cols)}")

    variants = []
    for idx, tc in enumerate(timing_cols):
        tx = tc + 1
        topic = _cell(rows, 1, tc - 1).strip() or _cell(rows, 1, tc).strip()
        block = {name: _cell(rows, ROW[name], tx) for name in ROW}
        variants.append({"idx": idx, "topic": topic, "texts": block})
    return variants


def _assert_shared_identical(variants):
    shared = {}
    for step in SHARED_STEPS:
        vals = {v["texts"][step].strip() for v in variants}
        if len(vals) != 1:
            raise ValueError(
                f"shared step '{step}' is NOT identical across variants "
                f"({len(vals)} distinct) — cannot treat as a global template")
        shared[step] = variants[0]["texts"][step]
    return shared


def run(xlsx_path=None, db_path=None, media_dir=None, verbose=True, wipe_runtime=True):
    xlsx_path = xlsx_path or config.XLSX_PATH
    media_dir = media_dir or config.MEDIA_DIR
    os.makedirs(media_dir, exist_ok=True)

    variants = parse_workbook(xlsx_path)
    images = extract_images_by_anchor(xlsx_path)
    if set(images) != set(range(66)):
        missing = set(range(66)) - set(images)
        raise ValueError(f"image anchors incomplete; missing variant idx {sorted(missing)}")

    shared = _assert_shared_identical(variants)
    intro_tmpl = content.make_intro_template(shared["intro"])  # H1

    # parse + validate every diagnosis card (C1: by canonical name)
    topic_cards = {}
    for v in variants:
        number, card = content.parse_card(v["texts"]["diagnosis"])
        v["card_number"], v["card_name"] = number, card
        topic_cards.setdefault(v["topic"], set()).add(card)
    for topic, cards in topic_cards.items():
        if len(cards) != 22:
            raise ValueError(f"topic {topic!r} has {len(cards)} distinct cards (expected 22)")

    conn = connect(db_path)
    init(conn)
    # single atomic transaction: wipe + repopulate (a crash never leaves content half-gone)
    md5_to_key = {}
    with transaction(conn):
        if wipe_runtime:
            wipe(conn, RUNTIME_TABLES)
        wipe(conn, CONTENT_TABLES)
        for step in SHARED_STEPS:
            text = intro_tmpl if step == "intro" else shared[step]
            conn.execute("INSERT INTO templates(step_name, text) VALUES (?, ?)", (step, text))
        for v in variants:
            fname, data = images[v["idx"]]
            h = hashlib.md5(data).hexdigest()
            if h not in md5_to_key:
                ext = os.path.splitext(fname)[1] or ".jpg"
                out_name = f"{h}{ext}"
                with open(os.path.join(media_dir, out_name), "wb") as f:
                    f.write(data)
                conn.execute("INSERT INTO media(media_key, file_name, file_id) VALUES (?, ?, NULL)",
                             (h, out_name))
                md5_to_key[h] = out_name
            conn.execute(
                "INSERT INTO variants(variant_id, topic, card_number, card_name, diagnosis, media_key) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (v["idx"], v["topic"], v["card_number"], v["card_name"],
                 v["texts"]["diagnosis"], h))
    rebuild_bag(conn)

    report = {
        "variants": len(variants),
        "unique_images_md5": len(md5_to_key),
        "topics": {t: len(c) for t, c in topic_cards.items()},
        "intro_has_placeholder": "{name}" in intro_tmpl,
    }
    if verbose:
        print("IMPORT OK:")
        print(f"  variants:        {report['variants']}")
        print(f"  unique images:   {report['unique_images_md5']} (md5-deduped)")
        print(f"  topics:          {report['topics']}")
        print(f"  intro template:  {intro_tmpl[:60]!r}")
        # QA contact sheet
        sheet = conn.execute(
            "SELECT variant_id, topic, card_number, card_name, media_key FROM variants "
            "ORDER BY variant_id").fetchall()
        with open(os.path.join(config.BASE_DIR, "content", "import_report.txt"), "w",
                  encoding="utf-8") as f:
            for r in sheet:
                f.write(f"{r['variant_id']:>2} | {r['topic']:<18} | {r['card_number']:>4}. "
                        f"{r['card_name']:<16} | {r['media_key'][:10]}\n")
        print("  QA sheet -> content/import_report.txt")
    conn.close()
    return report


if __name__ == "__main__":
    run()
