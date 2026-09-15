"""PoE Currency Exchange flip helper.

Sits in the tray. Press Alt+Q while the Currency Exchange "Market Ratio"
panel is open (hover + hold Alt in game) to capture the screen, OCR the
Available/Competing trade tables, and get divisible ratio suggestions for
your stock.

Usage:
    pythonw flipper.py            # normal tray mode
    python  flipper.py --test IMG [--qty N]   # run OCR+math on a saved screenshot
    python  flipper.py --dump     # capture crops now and save them for inspection
"""

import argparse
import ctypes
import math
import os
import queue
import re
import sys
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from PIL import Image, ImageDraw

APP_DIR = Path(__file__).resolve().parent
LOG_FILE = APP_DIR / "flipper.log"
DEBUG_SAVE = True  # save last capture + OCR rows for troubleshooting

# --- reference geometry: all coordinates live in 1920x1080 space and are
# mapped to the real screen by a Geometry transform (uniform scale + offset).
# Default: PoE's exchange panel is centered and scales with screen height.
# If that guess fails to parse, an OCR pass over the screenshot finds the
# panel headers and calibrates the transform automatically (persisted).
REF_W, REF_H = 1920, 1080
TABLES_BOX = (840, 150, 1090, 700)   # market ratio + available + competing tables
WANT_BOX = (598, 215, 830, 266)      # "I Want" item name
HAVE_BOX = (1090, 215, 1345, 266)    # "I Have" item name
WANT_FIELD_POS = (864, 243)          # in-game amount input boxes (for auto-fill)
HAVE_FIELD_POS = (1045, 243)
ANCHOR_MARKET = (960, 174)           # center of the "Market Ratio" title
ANCHOR_WANT = (707, 197)             # center of the "I Want" tab text
ANCHOR_HAVE = (1213, 197)            # center of the "I Have" tab text
ANCHOR_AVAIL_LEFT = (878, 229)       # left edge of the "Available Trades"
                                     # title (tooltip chrome merges into the
                                     # right side, so centers are unreliable)
ANCHOR_SUBHDR = (940, 254)           # midpoint of the first "Ratio | Stock"
                                     # sub-header row
ANCHOR_SUBHDR_DY = 198               # vertical gap between the two
                                     # "Ratio | Stock" rows

OCR_SCALE = 2                        # upscale factor before OCR
ROW_PITCH = 22 * OCR_SCALE           # vertical distance between table rows (scaled px)
TABLE_X_BAND = (45, 395)             # parchment content span inside the tables
                                     # crop (scaled px) — cells outside are
                                     # background noise, not table data

__version__ = "1.0.19"
GITHUB_REPO = "tirendus/poe-flipper"

HOTKEY_DEFAULT = "alt+q"
CALIB_FILE = APP_DIR / "flipper_calib.json"
CONFIG_FILE = APP_DIR / "flipper_config.json"
UPDATE_MARKER = APP_DIR / ".updated"


def load_config():
    import json
    try:
        # utf-8-sig: tolerate the BOM that Notepad/PowerShell like to write
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
    except OSError:
        return {}
    except ValueError:
        log("flipper_config.json is not valid JSON — using defaults "
            "(file left untouched)")
        return {}


def load_hotkey():
    """Read the hotkey from flipper_config.json (created with the default on
    first run).  Returns (label, modifier_mask, vk)."""
    import json
    cfg = load_config()
    spec = cfg.get("hotkey", HOTKEY_DEFAULT)
    try:
        mods, vk = parse_hotkey(spec)
    except ValueError as e:
        log(f"invalid hotkey {spec!r} in flipper_config.json ({e}) — "
            f"falling back to {HOTKEY_DEFAULT}")
        spec = HOTKEY_DEFAULT
        mods, vk = parse_hotkey(spec)
    if not CONFIG_FILE.exists():
        try:
            CONFIG_FILE.write_text(
                json.dumps({"hotkey": spec, "auto_update": True,
                            "wall_fraction": 0.25, "greedy_min_pct": 15,
                            "fill_verify": True}, indent=2),
                encoding="utf-8")
        except OSError:
            pass
    return spec, mods, vk


def parse_hotkey(spec):
    """'ctrl+shift+x' -> (RegisterHotKey modifier mask, virtual-key code).
    Supports alt/ctrl/shift/win + a letter, digit, F1-F24 or space."""
    MODS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
    mods, vk = 0, None
    for part in spec.lower().replace(" ", "").split("+"):
        if part in MODS:
            mods |= MODS[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", part):
            vk = 0x6F + int(part[1:])
        elif part == "space":
            vk = 0x20
        else:
            raise ValueError(f"unknown key {part!r}")
    if vk is None:
        raise ValueError("no non-modifier key")
    return mods, vk

MAX_DENOM = 60          # baseline cap for the "have"-side of a ratio
GREEDY_MIN_PCT = 15.0   # the Greedy buy row prices for at least this margin
                        # (override: "greedy_min_pct" in flipper_config.json)
WALL_FRACTION = 0.25    # a level is "the wall" when it holds at least this
                        # share of the deepest level's stock
                        # (override: "wall_fraction" in flipper_config.json)
FILL_VERIFY = True      # re-OCR the game fields after auto-fill and warn on
                        # mismatch ("fill_verify" in flipper_config.json)
DEBUG_KEEP = 30         # newest suspicious captures kept in debug/


def apply_config():
    """Apply optional tuning overrides from flipper_config.json."""
    global GREEDY_MIN_PCT, WALL_FRACTION, FILL_VERIFY
    cfg = load_config()
    try:
        GREEDY_MIN_PCT = float(cfg.get("greedy_min_pct", GREEDY_MIN_PCT))
        WALL_FRACTION = min(1.0, max(0.01, float(
            cfg.get("wall_fraction", WALL_FRACTION))))
        FILL_VERIFY = bool(cfg.get("fill_verify", FILL_VERIFY))
    except (TypeError, ValueError):
        log("invalid tuning values in flipper_config.json — using defaults")
WASTE_WEIGHT = 60.0     # penalty for leftover stock (fraction of N)
ERR_WEIGHT = 100.0      # penalty for deviating from target price
DENOM_WEIGHT = 1.5      # preference for simple denominators, normalized by
                        # the smallest denominator the price can be expressed
                        # with at all (so 1:1000 for divine-priced items isn't
                        # punished like 87:4 for chaos-priced ones)


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- OCR ---
# Backend: RapidOCR (PaddleOCR models via onnxruntime).  Windows' built-in
# OCR silently drops short isolated tokens like "8 : 1", so it can't be used.

_OCR = None
_OCR_LOCK = threading.Lock()


def get_ocr():
    global _OCR
    with _OCR_LOCK:
        if _OCR is None:
            from rapidocr_onnxruntime import RapidOCR
            _OCR = RapidOCR()
        return _OCR


def warmup_ocr():
    try:
        get_ocr()(__import__("numpy").zeros((32, 32, 3), dtype="uint8"))
    except Exception:
        log("OCR warmup failed:\n" + traceback.format_exc())


def ocr_cells(pil_img, scale=OCR_SCALE):
    """OCR an image, return [(text, x, cy, width), ...] in scaled coords."""
    import numpy as np
    img = pil_img.resize((int(pil_img.width * scale),
                          int(pil_img.height * scale)), Image.LANCZOS)
    result, _ = get_ocr()(np.array(img))
    cells = []
    for box, text, _score in result or []:
        xs = [p[0] for p in box]
        cy = sum(p[1] for p in box) / 4
        cells.append((text, min(xs), cy, max(xs) - min(xs)))
    return cells


def group_rows(cells, pitch=ROW_PITCH):
    """Cluster OCR cells into text rows by vertical center."""
    cells = sorted(cells, key=lambda c: c[2])
    rows, cur = [], []
    for c in cells:
        if cur and abs(c[2] - cur[-1][2]) > pitch * 0.5:
            rows.append(cur)
            cur = []
        cur.append(c)
    if cur:
        rows.append(cur)
    out = []
    for row in rows:
        row.sort(key=lambda c: c[1])
        cy = sum(c[2] for c in row) / len(row)
        out.append((cy, " ".join(c[0] for c in row)))
    return out


X_SPLIT = 232   # scaled px: ratio column is left of this, stock column right


def _repair_ratio_token(tok, stock_txt, above, below, market_big, one_first):
    """Reconstruct a wrapped/overprinted long ratio.

    The game wraps ratios with a long side ('1 : 178.50') onto a second
    line inside the row slot and sometimes overprints the short side onto
    the number, producing tokens like '178.50' (clean wrap), '173:80'
    (mash of '1 :' and 178.80) or '1715:50' (mash for 176.50).  The
    decimal tail survives the overlap, and trusted neighbours / the market
    ratio pin down the integer part.  one_first: section format is
    '1 : big' (True) or 'big : 1' (False)."""
    prefix = "<" if "<" in tok else (">" if ">" in tok else "")
    core = re.sub(r"[<>‹›\s]", "", tok)
    anchor = above if above is not None else (
        below if below is not None else market_big)
    if anchor is None or anchor < 10:
        return None
    v = None
    if re.fullmatch(r"\d+(?:\.\d+)?", core):
        v = float(core)                       # clean wrapped number
    else:
        m = re.search(r"[:.](\d{1,2})$", core)
        if not m:
            return None
        tail = m.group(1)                     # decimals survive the overlap
        best = None
        for base in (int(anchor), int(anchor) - 1, int(anchor) + 1):
            c = float(f"{base}.{tail}")
            if abs(c - anchor) / anchor <= 0.02 and \
                    (best is None or abs(c - anchor) < abs(best - anchor)):
                best = c
        v = best
    if v is None or not 0.9 <= v / anchor <= 1.1:
        return None
    ratio = f"1 : {v:g}" if one_first else f"{v:g} : 1"
    return f"{prefix}{ratio} {stock_txt}".strip()


def _assemble_zone(cells, y0, y1, market_form, market_big):
    """Rebuild one table section from raw cells by pairing each stock cell
    with its ratio cell — same line, or the wrapped line ~half a row
    below — then repairing broken ratio tokens."""
    zone = [c for c in cells if y0 < c[2] < y1]
    ratios, stocks = [], []
    for c in zone:
        low = c[0].lower()
        if not any(ch.isdigit() for ch in c[0]) or \
                "ratio" in low or "stock" in low or "trades" in low:
            continue
        (ratios if c[1] < X_SPLIT else stocks).append(c)

    pairs, used = [], set()
    for st in sorted(stocks, key=lambda c: c[2]):
        best_i, best_dy = None, None
        for i, rt in enumerate(ratios):
            if i in used:
                continue
            dy = rt[2] - st[2]
            if -16 <= dy <= 30 and (best_dy is None or abs(dy) < abs(best_dy)):
                best_i, best_dy = i, dy
        if best_i is None:
            pairs.append((None, st))
        else:
            used.add(best_i)
            pairs.append((ratios[best_i], st))
    for i, rt in enumerate(ratios):     # full rows OCR'd as a single cell
        if i not in used and RATIO_ROW.match(rt[0]):
            pairs.append((rt, None))
    pairs.sort(key=lambda p: (p[1] if p[1] is not None else p[0])[2])

    # first pass: the game always anchors one ratio side at 1, so a parsed
    # row in '1 : x' or 'x : 1' form is trusted; anything else (mash of
    # overprinted glyphs, or an unparsable fragment) is a repair candidate
    texts, bigs, forms = [], [], []
    for rt, st in pairs:
        stock_txt = st[0] if st is not None else ""
        if rt is None:
            texts.append(f"?ratio? {stock_txt}")
            bigs.append(None)
            forms.append(None)
            continue
        raw = f"{rt[0]} {stock_txt}".strip()
        m = RATIO_ROW.match(raw)
        conforming = False
        if m:
            try:
                a = parse_ratio_num(m.group(2))
                b = parse_ratio_num(m.group(3))
                conforming = (a == 1 or b == 1)
            except ValueError:
                pass
        if conforming:
            texts.append(raw)
            bigs.append(b if a == 1 else a)
            forms.append(a == 1)
        else:
            texts.append(("?", rt[0], stock_txt))
            bigs.append(None)
            forms.append(None)

    # section display form: from trusted rows, else the market ratio
    form_votes = [f for f in forms if f is not None]
    if form_votes:
        one_first = sum(form_votes) >= len(form_votes) / 2
    elif market_form is not None:
        one_first = market_form
    else:
        one_first = None

    # second pass: repair broken tokens using neighbouring trusted rows
    out = []
    for i, t in enumerate(texts):
        if isinstance(t, tuple):
            fixed = None
            if one_first is not None:
                above = next((bigs[j] for j in range(i - 1, -1, -1)
                              if bigs[j] is not None), None)
                below = next((bigs[j] for j in range(i + 1, len(bigs))
                              if bigs[j] is not None), None)
                fixed = _repair_ratio_token(t[1], t[2], above, below,
                                            market_big, one_first)
            if fixed:
                m = RATIO_ROW.match(fixed)
                if m:
                    bigs[i] = parse_ratio_num(
                        m.group(3) if one_first else m.group(2))
                out.append(fixed)
                log(f"repaired wrapped ratio {t[1]!r} -> {fixed!r}")
            else:
                out.append(f"{t[1]} {t[2]}".strip())
        else:
            out.append(t)
    return out


def read_tables(pil_img):
    """OCR the tables crop, return [(section, text), ...] with section in
    {'market', 'available', 'competing'}."""
    global _LAST_CELLS
    cells = [c for c in ocr_cells(pil_img)
             if TABLE_X_BAND[0] <= c[1] <= TABLE_X_BAND[1]]
    _LAST_CELLS = cells
    rows = group_rows(cells)
    y_market = y_avail = y_comp = y_end = None
    header_rows = []          # the two "Ratio | Stock" sub-header lines
    for cy, text in rows:
        low = text.lower()
        if "market" in low and y_market is None:
            y_market = cy
        elif "available" in low and y_avail is None:
            y_avail = cy
        elif "competing" in low and y_comp is None:
            y_comp = cy
        elif "ratio" in low and "stock" in low:
            header_rows.append(cy)
        elif ("selling" in low or "buying" in low) and y_end is None:
            # the player's own-order summary box sits below the tables —
            # everything from here down is not table data
            y_end = cy
    # fall back on the sub-headers when a section title wasn't recognized:
    # the first "Ratio Stock" line starts Available, the second Competing
    if y_avail is None and header_rows:
        y_avail = header_rows[0]
        log("'Available Trades' header not read — using sub-header position")
    if y_comp is None and len(header_rows) >= 2:
        y_comp = header_rows[1]
        log("'Competing Trades' header not read — using sub-header position")

    y_stop = y_end if y_end is not None else 1e9
    tagged = []
    market_form = market_big = None
    for cy, text in rows:
        if y_market is not None and y_avail is not None and \
                y_market < cy < y_avail:
            tagged.append(("market", text))
            m = RATIO_ROW.match(text)
            if m and market_big is None:
                try:
                    a = parse_ratio_num(m.group(2))
                    b = parse_ratio_num(m.group(3))
                    if a == 1:
                        market_form, market_big = True, b
                    elif b == 1:
                        market_form, market_big = False, a
                except ValueError:
                    pass
    if y_avail is None and y_comp is None:
        # no structure found — fall back to plain row tagging
        for cy, text in rows:
            if y_market is not None and cy > y_market and cy < y_stop:
                tagged.append(("market", text))
        return tagged
    if y_avail is not None:
        avail_end = y_comp if y_comp is not None else y_stop
        for text in _assemble_zone(cells, y_avail, min(avail_end, y_stop),
                                   market_form, market_big):
            tagged.append(("available", text))
    if y_comp is not None:
        for text in _assemble_zone(cells, y_comp, y_stop,
                                   market_form, market_big):
            tagged.append(("competing", text))
    return tagged


_LAST_CELLS = []


def read_names(want_img, have_img):
    """OCR both item-name crops in a single pass (stacked vertically)."""
    gap = 24
    w = max(want_img.width, have_img.width)
    canvas = Image.new("RGB", (w, want_img.height + gap + have_img.height),
                       (20, 16, 12))
    canvas.paste(want_img, (0, 0))
    canvas.paste(have_img, (0, want_img.height + gap))
    cells = ocr_cells(canvas)
    split = (want_img.height + gap / 2) * OCR_SCALE
    want = " ".join(c[0] for c in sorted(cells, key=lambda c: c[1])
                    if c[2] < split)
    have = " ".join(c[0] for c in sorted(cells, key=lambda c: c[1])
                    if c[2] >= split)
    return want or "I Want", have or "I Have"


# ------------------------------------------------------------- parsing ---

RATIO_ROW = re.compile(
    r"^\s*([<>‹›(]?)\s*([\d.,]+)\s*[:;|：]\s*([\d.,]+)(?:\s+([\d.,]+))?\s*$"
)


def parse_ratio_num(s):
    """Parse a ratio-side number: comma before 3 digits = thousands sep."""
    s = re.sub(r",(?=\d{3}\b)", "", s.strip())
    s = s.replace(",", ".")
    return float(s)


def parse_stock(s):
    digits = re.sub(r"\D", "", s)
    if not digits:
        raise ValueError(s)
    return int(digits)


def parse_tables(tagged_rows):
    """Turn (section, text) rows into {market, available[], competing[]}."""
    data = {"market": None, "available": [], "competing": [], "unparsed": 0}
    for section, text in tagged_rows:
        low = text.lower()
        if any(k in low for k in ("trades", "ratio", "stock")):
            continue  # header fragments
        m = RATIO_ROW.match(text)
        if not m:
            data["unparsed"] += 1
            log(f"unparsed {section} row: {text!r}")
            continue
        prefix, a_s, b_s, stock_s = m.groups()
        try:
            a, b = parse_ratio_num(a_s), parse_ratio_num(b_s)
        except ValueError:
            data["unparsed"] += 1
            continue
        if b <= 0 or a <= 0:
            continue
        price = a / b
        if section == "market":
            if data["market"] is None:
                data["market"] = price
            continue
        if stock_s is None:
            # OCR sometimes misses tiny stock numbers — keep the price
            # level anyway with stock 0 (unknown)
            stock = 0
        else:
            try:
                stock = parse_stock(stock_s)
            except ValueError:
                data["unparsed"] += 1
                continue
        data[section].append(
            {"price": price, "a": a, "b": b, "stock": stock,
             "approx": bool(prefix)}
        )

    # sanity filter: an OCR-mangled decimal ("1:1.20" read as "1:120")
    # produces a price wildly off from its section — drop such outliers
    for section in ("available", "competing"):
        levels = data[section]
        if len(levels) < 3:
            continue
        prices = sorted(e["price"] for e in levels)
        median = prices[len(prices) // 2]
        kept = []
        for e in levels:
            ratio = e["price"] / median if median else 1
            if ratio > 4 or ratio < 0.25:
                log(f"dropping implausible {section} level "
                    f"{_ratio_text(e)} (x{e['stock']}) — "
                    f"{ratio:.1f}x off the section median")
                data["unparsed"] += 1
            else:
                kept.append(e)
        data[section] = kept

    # the </> aggregate is always the LAST row of a section: markers that
    # bleed onto other rows (overlapping wrapped text) are noise, and a
    # final row repeating the previous price is the aggregate even when
    # its marker was lost
    for section in ("available", "competing"):
        levels = data[section]
        for e in levels[:-1]:
            e["approx"] = False
        if len(levels) >= 2 and not levels[-1]["approx"] and \
                abs(levels[-1]["price"] - levels[-2]["price"]) < 1e-9:
            levels[-1]["approx"] = True
    return data


# ------------------------------------------------------ suggestion math ---

def _denom_penalty(d, dmin, weight):
    """Simplicity cost of a denominator, relative to the smallest denominator
    (dmin) that can express the target price at all.  Large denominators get
    a discount when they are round numbers (1:1050 reads better than 1:1042).
    """
    p = weight * d / dmin
    if d > 20:
        if d % 100 == 0:
            mult = 1.0
        elif d % 50 == 0:
            mult = 1.15
        elif d % 10 == 0:
            mult = 1.3
        else:
            mult = 2.0
        p *= mult
    return p


def snap_ratio(target, n, lo=None, hi=None, denom_weight=DENOM_WEIGHT,
               dmax_cap=MAX_DENOM):
    """Best integer ratio w:d with d dividing a chunk of n, price near target.

    lo/hi are exclusive price bounds (e.g. hi=best_ask to strictly undercut).
    Returns dict or None.
    """
    if target <= 0 or n <= 0:
        return None
    dmin = max(1, math.ceil(1 / target)) if target < 1 else 1
    dmax = min(max(dmax_cap, math.ceil(3 / target)), 10000, n)
    best = None
    for d in range(1, dmax + 1):
        raw = target * d
        for w in {math.floor(raw), round(raw), math.ceil(raw)}:
            if w < 1:
                continue
            price = w / d
            if lo is not None and price <= lo:
                continue
            if hi is not None and price >= hi:
                continue
            used = (n // d) * d
            if used == 0:
                continue
            rel_err = abs(price - target) / target
            waste = (n - used) / n
            score = (rel_err * ERR_WEIGHT + waste * WASTE_WEIGHT
                     + _denom_penalty(d, dmin, denom_weight))
            if best is None or score < best["score"]:
                g = math.gcd(w, d)
                rw, rd = w // g, d // g
                best = {
                    "w": rw, "d": rd, "price": price, "used": used,
                    "left": n - used, "score": score,
                    "want_total": rw * (used // rd),
                }
    return best


def classify_book(levels, frac=None):
    """Split a price-level list (best-priced first, aggregates excluded) into
    the user's three zones: `top` (best level), `wall` (first level holding
    at least `frac` of the deepest level's stock — the real competition) and
    `cluster` (better-priced small levels sitting ahead of the wall).
    The </> aggregate row is "the abyss" and is never priced against."""
    real = []
    for e in levels:
        if e.get("approx"):
            continue
        # the game sometimes shows the same rounded ratio on several rows —
        # merge them so ladder bands aren't zero-width and queues add up
        if real and abs(e["price"] - real[-1]["price"]) < 1e-9:
            real[-1] = dict(real[-1], stock=real[-1]["stock"] + e["stock"])
        else:
            real.append(dict(e))
    if not real:
        return None
    abyss = next((e for e in levels if e.get("approx")), None)
    mx = max(e["stock"] for e in real)
    wall = real[0]
    if mx > 0:
        for e in real:
            if e["stock"] >= (frac if frac is not None
                              else WALL_FRACTION) * mx:
                wall = e
                break
    cluster = [e for e in real if e["price"] < wall["price"]]
    return {"top": real[0], "wall": wall, "cluster": cluster, "real": real,
            "abyss": abyss}


def _ratio_text(e):
    return f"{e['a']:g}:{e['b']:g}"


def _ladder_targets(levels, verb, queue_in_want=False):
    """Build the queue-ladder targets against a competing book (prices in
    want-per-have space, lower = more competitive).  Every level is a wall
    you can park in front of:
    - '<verb> all'      — smallest clean step past the best level
    - '2nd/3rd/4th in line' — step in front of the next level down, queued
      behind the levels above you (better price, slower fill — the flipper
      picks the tradeoff)
    Queue-ahead counts are fulfillable trades: when buying, a rival's Stock
    is the currency they committed, so it converts to items via that
    level's own price (queue_in_want=True); when selling, Stock already is
    the item count."""
    cls = classify_book(levels)
    if cls is None:
        return [], None
    real = cls["real"]

    def qty(e):
        return e["stock"] * e["price"] if queue_in_want else e["stock"]

    top_p = real[0]["price"]
    targets = [(f"{verb} all", top_p * 0.995, top_p * 0.80, top_p, real[0])]
    ordinal = {1: "2nd", 2: "3rd", 3: "4th", 4: "5th", 5: "6th"}
    cum = 0.0
    for k in range(1, min(len(real), 6)):
        cum += qty(real[k - 1])
        targets.append((f"{ordinal[k]} in line ({round(cum):,} ahead)",
                        real[k]["price"] * 0.995,
                        real[k - 1]["price"], real[k]["price"], real[k]))
    # the abyss rung: park just behind the last visible level but in front
    # of the entire </> aggregate — the deepest spot still worth holding
    if cls["abyss"] is not None:
        last_p = real[-1]["price"]
        cum_all = cum + qty(real[-1]) if len(real) > 1 else qty(real[0])
        targets.append((f"Front of abyss ({round(cum_all):,} ahead)",
                        last_p * 1.005, last_p, last_p * 1.03, None))
    return targets, cls


def simplest_between(lo, hi):
    """Simplest fraction (w, d) with lo < w/d < hi, via the Stern-Brocot /
    continued-fraction walk.  Guarantees a hit for any non-empty band, with
    the smallest denominator that can express a price inside it."""
    if not 0 < lo < hi:
        return None

    def rec(lo, hi):
        fl = math.floor(lo)
        if fl + 1 < hi:
            return (fl + 1, 1)
        lo_f = lo - fl
        hi_f = hi - fl
        if lo_f <= 0:
            k = math.floor(1 / hi_f) + 1
            return (fl * k + 1, k)
        a, b = rec(1 / hi_f, 1 / lo_f)
        return (fl * a + b, a)

    # shave the bounds so float noise can't produce a tie with a level
    w, d = rec(lo * (1 + 1e-9), hi * (1 - 1e-9))
    return (w, d)


def _simplest_row(n, lo, hi):
    """Coarse ladder row from the simplest fraction near the band's
    aggressive edge (within 3% of the level being beaten)."""
    if hi is None:
        return None
    band_lo = max(lo, hi * 0.97) if lo is not None else hi * 0.97
    fr = simplest_between(band_lo, hi)
    if fr is None:
        return None
    w, d = fr
    if d > min(2000, n):
        return None
    used = (n // d) * d
    if used <= 0:
        return None
    return {"w": w, "d": d, "price": w / d, "used": used,
            "left": n - used, "want_total": w * (used // d)}


def _forced_step(lvl, n, lo, hi):
    """For a level displayed with an integer long side the natural beat is
    exactly one currency unit past it: 1:411 over 1:410 when buying,
    324:1 under 325:1 when selling — no snapping heuristics.
    Decimal-priced levels return None and keep the generic logic."""
    if lvl is None:
        return None
    a, b = lvl.get("a"), lvl.get("b")
    if a == 1 and b >= 10 and float(b).is_integer():
        w, d = 1, int(b) + 1          # pay one more per item
    elif b == 1 and a >= 10 and float(a).is_integer():
        w, d = int(a) - 1, 1          # ask one less per item
    else:
        return None
    price = w / d
    if n < d or (lo is not None and price <= lo) or price >= hi:
        return None
    used = (n // d) * d
    return {"w": w, "d": d, "price": price, "used": used,
            "left": n - used, "want_total": w * (used // d)}


def _step_above(lvl, n, lo, hi):
    """Minimal one-unit step on the NON-CROSSING side of a level: ask just
    over the standing buyers / bid just under the standing sellers.
    Integer-priced levels only; mirrors _forced_step's direction."""
    if lvl is None:
        return None
    a, b = lvl.get("a"), lvl.get("b")
    if a == 1 and b >= 10 and float(b).is_integer():
        w, d = 1, int(b) - 1
    elif b == 1 and a >= 10 and float(a).is_integer():
        w, d = int(a) + 1, 1
    else:
        return None
    price = w / d
    if n < d or price <= lo or (hi is not None and price >= hi):
        return None
    used = (n // d) * d
    return {"w": w, "d": d, "price": price, "used": used,
            "left": n - used, "want_total": w * (used // d)}


def _dead_market_rows(avail, market, n):
    """No competing orders: price strictly past the standing available
    offers (never AT them — that just executes instantly), with patience
    tiers.  Returns (rows, note) or (None, None) if nothing to anchor on."""
    cls = classify_book(avail) if avail else None
    if cls is not None:
        lvl = cls["wall"]
        anchor = lvl["price"]
        note = ("no competing orders — pricing just past the standing "
                f"offers (wall {lvl['stock']:,} @ {_ratio_text(lvl)})")
    elif market:
        lvl, anchor = None, market
        note = "no competing orders — pricing anchored to the market ratio"
    else:
        return None, None
    rows, seen = [], set()
    targets = [("Step past offers", anchor * 1.005, anchor, anchor * 1.03),
               ("Patient (+10%)", anchor * 1.10, anchor, None),
               ("Very patient (+25%)", anchor * 1.25, anchor, None)]
    for i, (label, t, lo, hi) in enumerate(targets):
        s = None
        if i == 0:
            s = _step_above(lvl, n, lo, hi)
            if s is None:
                fr = simplest_between(lo, anchor * 1.03)
                if fr is not None:
                    w, d = fr
                    if d <= min(2000, n) and n // d > 0:
                        used = (n // d) * d
                        s = {"w": w, "d": d, "price": w / d, "used": used,
                             "left": n - used, "want_total": w * (used // d)}
        if s is None:
            s = snap_ratio(t, n, lo=lo, hi=hi)
        if not s:
            continue
        key = (s["w"], s["d"], s["used"])
        if key in seen:
            continue
        seen.add(key)
        s["fine"] = False
        rows.append((label, s))
    return rows, note


def strategy_rows(levels, n, verb, queue_in_want=False):
    """Ladder targets snapped to divisible ratios for quantity `n`, each in
    a simple-ratio and a finer-ratio variant, deduped."""
    targets, cls = _ladder_targets(levels, verb, queue_in_want)
    if cls is None:
        return [], None
    rows, seen = [], set()
    for fine, dweight, dcap in ((False, DENOM_WEIGHT, MAX_DENOM),
                                (True, 0.1, 150)):
        for label, t, lo, hi, lvl in targets:
            if not fine:
                s = _forced_step(lvl, n, lo, hi)
                if s is None:
                    s = _simplest_row(n, lo, hi)
                if s is None:
                    s = snap_ratio(t, n, lo=lo, hi=hi, denom_weight=dweight,
                                   dmax_cap=dcap)
            else:
                s = snap_ratio(t, n, lo=lo, hi=hi, denom_weight=dweight,
                               dmax_cap=dcap)
            if not s:
                continue
            key = (s["w"], s["d"], s["used"])
            if key in seen:
                continue
            seen.add(key)
            s["fine"] = fine
            rows.append((label, s))
    return rows, cls


def _wall_note(cls):
    if cls is None or cls["wall"] is cls["top"]:
        return None
    ahead = sum(e["stock"] for e in cls["cluster"])
    return (f"wall: {cls['wall']['stock']:,} @ {_ratio_text(cls['wall'])} — "
            f"smaller offers ahead of it: {ahead:,}")


def build_suggestions(data, n):
    """Return {'rows': [(label, snap)], 'notes': [...], 'instant': ...}."""
    out = {"rows": [], "notes": [], "instant": None, "dead": False}
    avail, comp = data["available"], data["competing"]
    market = data["market"]
    best_bid = avail[0]["price"] if avail else None

    if comp:
        out["rows"], cls = strategy_rows(comp, n, "Undercut")
        note = _wall_note(cls)
        if note:
            out["notes"].append(note)
    else:
        rows, note = _dead_market_rows(avail, market, n)
        if rows is None:
            out["notes"].append("Could not read any prices from the panel.")
            return out
        out["dead"] = True
        out["notes"].append(note)
        out["rows"] = rows

    if len([e for e in avail if not e.get("approx")]) <= 2 or \
            len([e for e in comp if not e.get("approx")]) <= 2:
        out["dead"] = True

    if best_bid is not None and out["rows"]:
        first_price = out["rows"][0][1]["price"]
        if first_price <= best_bid:
            out["notes"].append(
                f"Best instant price ({best_bid:g}) beats that listing — "
                "consider just taking available trades."
            )

    # rough instant-dump estimate: assumes 'Stock' in Available Trades is the
    # amount of the I-Want currency on offer at that tier
    if avail:
        remaining = float(n)
        got = 0.0
        for e in avail:
            if e["price"] <= 0:
                continue
            can_take_have = e["stock"] / e["price"]
            take = min(remaining, can_take_have)
            got += take * e["price"]
            remaining -= take
            if remaining <= 0:
                break
        sold = n - max(0, math.ceil(remaining))
        if sold > 0:
            out["instant"] = (int(sold), int(got))
    return out


def build_buy_suggestions(data, m):
    """Suggestions for buying the I-WANT currency with a budget of `m`
    I-HAVE currency — the panel is already in buy orientation, so orders go
    the same direction as selling (give have, get want); the competing
    trades are the rival buyers to outbid.

    Resale projection: the acquired want-currency would later be sold by
    undercutting the current best AVAILABLE offer (today's sellers) by 1%."""
    out = {"rows": [], "notes": [], "instant": None, "resale": None,
           "dead": False}
    avail, comp = data["available"], data["competing"]
    market = data["market"]
    best_bid = avail[0]["price"] if avail else None   # want-per-have

    # resale: undercut the sellers' WALL (their real depth), not a dud top
    # row with a handful of stock, by half a percent
    acls = classify_book(avail) if avail else None
    if acls:
        resale = (1 / acls["wall"]["price"]) * 0.995
    elif market:
        resale = 1 / market
    else:
        resale = None
    out["resale"] = resale

    if comp:
        out["rows"], cls = strategy_rows(comp, m, "Outbid",
                                         queue_in_want=True)
        note = _wall_note(cls)
        if note:
            out["notes"].append(note)
    else:
        rows, note = _dead_market_rows(avail, market, m)
        if rows is None:
            out["notes"].append("Could not read any prices from the panel.")
            return out
        out["dead"] = True
        out["notes"].append(note)
        out["rows"] = rows

    if len([e for e in avail if not e.get("approx")]) <= 2 or \
            len([e for e in comp if not e.get("approx")]) <= 2:
        out["dead"] = True

    # greedy: the most aggressive price that still nets GREEDY_MIN_PCT
    # against the resale estimate, wherever that lands in the book
    if resale and out["rows"]:
        wph_min = (1 + GREEDY_MIN_PCT / 100) / resale
        sg = snap_ratio(wph_min * 1.005, m, lo=wph_min * 0.9999,
                        denom_weight=0.1, dmax_cap=150)
        if sg:
            key = (sg["w"], sg["d"], sg["used"])
            if key not in {(s["w"], s["d"], s["used"])
                           for _l, s in out["rows"]}:
                sg["fine"] = False
                idx = next((i for i, (_l, s) in enumerate(out["rows"])
                            if s.get("fine")), len(out["rows"]))
                out["rows"].insert(
                    idx, (f"Greedy (≥+{GREEDY_MIN_PCT:.0f}%)", sg))

    for _label, s in out["rows"]:
        s["pay_per_unit"] = s["d"] / s["w"]
        if resale:
            revenue = s["want_total"] * resale
            s["resell_revenue"] = int(revenue)
            s["profit"] = int(revenue - s["used"])
            s["profit_pct"] = (revenue - s["used"]) / s["used"] * 100

    # a flip that projects at a loss is noise — hide those rows as long as
    # at least one profitable option exists
    if any(s.get("profit", 0) > 0 for _l, s in out["rows"]):
        out["rows"] = [(lb, s) for lb, s in out["rows"]
                       if s.get("profit", 1) > 0]

    # instant buy: take the available offers with the budget
    if avail:
        remaining = float(m)
        got = 0.0
        for e in avail:
            if e["price"] <= 0:
                continue
            take = min(remaining, e["stock"] / e["price"])
            got += take * e["price"]
            remaining -= take
            if remaining <= 0:
                break
        if got >= 1:
            out["instant"] = (int(got), int(round(m - max(0.0, remaining))))
    return out


def build_buy_qty_suggestions(data, n):
    """Buy an exact quantity `n` of the I-Want item: same queue ladder as
    the flip view, but the want side is locked to n and only the have
    (currency) total varies — so prices step in 1/n increments."""
    out = {"rows": [], "notes": [], "instant": None, "resale": None,
           "dead": False}
    avail, comp = data["available"], data["competing"]
    market = data["market"]

    acls = classify_book(avail) if avail else None
    if acls:
        resale = (1 / acls["wall"]["price"]) * 0.995
    elif market:
        resale = 1 / market
    else:
        resale = None
    out["resale"] = resale

    def qty_row(h, w=None):
        w = n if w is None else w
        g = math.gcd(w, h)
        return {"w": w // g, "d": h // g, "price": w / h, "used": h,
                "left": 0, "want_total": w, "fine": False}

    def find_h(t, lo, hi):
        """Integer currency total whose price n/h sits strictly inside the
        band, closest to the target."""
        best_h = None
        h0 = max(1, int(n / t))
        for h in range(max(1, h0 - 2), h0 + 4):
            p = n / h
            if lo is not None and p <= lo:
                continue
            if hi is not None and p >= hi:
                continue
            if best_h is None or abs(p - t) < abs(n / best_h - t):
                best_h = h
        return best_h

    def find_flex(t, lo, hi, flex=0.15):
        """Near-quantity pair (w, h): when the want currency is much
        cheaper than the have currency, exact-n prices step too coarsely
        (50 chaos for whole divines allows only 10.0 or 8.33 per) —
        flexing the want side a little restores fine pricing."""
        best = None
        h_mid = n / t
        for h in range(max(1, int(h_mid * 0.7)), int(h_mid * 1.4) + 2):
            for w in {math.floor(t * h), round(t * h), math.ceil(t * h)}:
                if w < 1 or not n * (1 - flex) <= w <= n * (1 + flex):
                    continue
                p = w / h
                if lo is not None and p <= lo:
                    continue
                if hi is not None and p >= hi:
                    continue
                score = abs(p - t) / t + 0.02 * abs(w - n) / n
                if best is None or score < best[0]:
                    best = (score, w, h)
        return (best[1], best[2]) if best else None

    seen_h = set()
    fine_rows = []

    def add(label, h, w=None, fine=False):
        if h is None or h < 1:
            return
        key = (w if w is not None else n, h)
        if key in seen_h:
            return
        seen_h.add(key)
        r = qty_row(h, w)
        r["fine"] = fine
        (fine_rows if fine else out["rows"]).append((label, r))

    # fill-block sizes to try, smallest first: an order fills in chunks of
    # its reduced ratio's want side, so small blocks matter for scarce
    # items; capping at n//4 guarantees at least four separate fills
    divisors = [d for d in range(1, max(1, n // 4) + 1) if n % d == 0]

    def block_price(lo, hi):
        """Currency total whose price sits strictly inside the wph band,
        balancing fill-block size against price premium over the level
        being outbid: 1:u beats 2:x beats 4:x unless the smaller block
        overpays too much on a wide band.  Returns h or None."""
        if hi is None:
            return None
        lo_u = 1 / hi                        # per-unit bounds, exclusive
        hi_u = (1 / lo) if lo is not None else None
        best = None                          # (score, h)
        for w in divisors:
            m = math.floor(lo_u * w) + 1     # smallest m with m/w > lo_u
            u = m / w
            if m < 1 or (hi_u is not None and u >= hi_u):
                continue
            premium_pct = (u / lo_u - 1) * 100
            score = premium_pct + w * 0.5
            if best is None or score < best[0]:
                best = (score, n * m // w)
        return best[1] if best else None

    def add_rung(label, t, lo, hi):
        hb = block_price(lo, hi)
        he = find_h(t, lo, hi)
        if hb is not None:
            add(label, hb)
            # the exact-total variant is finer-priced but fills in one
            # giant block — secondary when a small-block price exists
            if he is not None and he != hb:
                add(label, he, fine=True)
            return True
        # no small-block price: pick the candidate closest to the target
        # among exact-quantity and flexed-quantity pairs
        cands = []
        if he is not None:
            cands.append((abs(n / he - t) / t, he, None))
        fx = find_flex(t, lo, hi)
        if fx is not None:
            cands.append((abs(fx[0] / fx[1] - t) / t, fx[1], fx[0]))
        if not cands:
            return False
        cands.sort()
        add(label, cands[0][1], w=cands[0][2])
        for _e, h2, w2 in cands[1:]:
            add(label, h2, w=w2, fine=True)
        return True

    cls = classify_book(comp) if comp else None
    if cls is not None:
        real = cls["real"]

        def items(e):
            return e["stock"] * e["price"]

        note = _wall_note(cls)
        if note:
            out["notes"].append(note)
        top_p = real[0]["price"]
        add_rung("Outbid all", top_p * 0.995, top_p * 0.80, top_p)
        ordinal = {1: "2nd", 2: "3rd", 3: "4th", 4: "5th", 5: "6th"}
        cum = 0.0
        for k in range(1, min(len(real), 6)):
            cum += items(real[k - 1])
            if not add_rung(f"{ordinal[k]} in line ({round(cum):,} ahead)",
                            real[k]["price"] * 0.995,
                            real[k - 1]["price"], real[k]["price"]):
                # no price fits between adjacent levels — join the upper
                # level's queue, but only when whole-currency rounding
                # really lands ON that level's price (otherwise the row
                # would claim a match it doesn't make)
                lvl_p = real[k - 1]["price"]
                h_m = round(n / lvl_p)
                if h_m >= 1 and abs(n / h_m - lvl_p) <= 0.005 * lvl_p:
                    add(f"Match {_ratio_text(real[k - 1])} "
                        f"({round(cum):,} ahead)", h_m)
        if cls["abyss"] is not None:
            last_p = real[-1]["price"]
            cum_all = cum + items(real[-1]) if len(real) > 1 \
                else items(real[0])
            add_rung(f"Front of abyss ({round(cum_all):,} ahead)",
                     last_p * 1.005, last_p, last_p * 1.05)
    else:
        acls = classify_book(avail) if avail else None
        anchor = acls["wall"]["price"] if acls else market
        if not anchor:
            out["notes"].append("Could not read any prices from the panel.")
            return out
        out["dead"] = True
        out["notes"].append(
            "No competing buyers — bidding just under the standing sellers.")
        # largest currency total whose price stays strictly non-crossing
        h = int(n / anchor)
        while h >= 1 and n / h <= anchor:
            h -= 1
        if h >= 1:
            seen_h.add(h)
            out["rows"].append(("Step past offers", qty_row(h)))

    out["rows"] += fine_rows

    if len([e for e in avail if not e.get("approx")]) <= 2 or \
            len([e for e in comp if not e.get("approx")]) <= 2:
        out["dead"] = True

    # plain buying, not flipping — no resale projection on the rows
    for _label, s in out["rows"]:
        s["pay_per_unit"] = s["used"] / s["want_total"]

    # instant: cost of taking n items straight from the available offers
    if avail:
        remaining = float(n)
        cost = 0.0
        for e in avail:
            if e["price"] <= 0:
                continue
            take = min(remaining, e["stock"])
            cost += take / e["price"]
            remaining -= take
            if remaining <= 0:
                break
        got = n - max(0, math.ceil(remaining))
        if got >= 1:
            out["instant"] = (int(got), int(round(cost)))
    return out


# ---------------------------------------------------- in-game auto-fill ---

def auto_fill(have_amount, want_amount):
    """Click the in-game amount fields and type the values, I Have first.
    Assumes the exchange panel is open and the field positions above are
    correct for the current orientation."""
    u = ctypes.windll.user32
    LEFTDOWN, LEFTUP = 0x0002, 0x0004
    VK_BACK, VK_DELETE = 0x08, 0x2E
    CLEAR_TAPS = 12     # the game's fields don't support Ctrl+A — hammer
                        # Backspace and Delete to clear from any cursor spot

    def click(pos):
        u.SetCursorPos(int(pos[0]), int(pos[1]))
        time.sleep(0.06)
        u.mouse_event(LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.02)
        u.mouse_event(LEFTUP, 0, 0, 0, 0)
        time.sleep(0.10)

    def tap(vk, pause=0.02):
        u.keybd_event(vk, 0, 0, 0)
        time.sleep(0.012)
        u.keybd_event(vk, 0, 2, 0)
        time.sleep(pause)

    def fill(pos, value):
        click(pos)          # first click focuses the game window
        click(pos)          # second click reliably focuses the field
        for _ in range(CLEAR_TAPS):
            tap(VK_BACK, 0.012)
        for _ in range(CLEAR_TAPS):
            tap(VK_DELETE, 0.012)
        for ch in str(value):
            tap(0x30 + int(ch))

    geo = _GEO or Geometry(*screen_size())
    time.sleep(0.25)        # let the popup close and the game take focus
    fill(geo.pt(HAVE_FIELD_POS), have_amount)
    fill(geo.pt(WANT_FIELD_POS), want_amount)
    if FILL_VERIFY:
        time.sleep(0.35)    # let the game render the typed values
        _verify_fill(geo, have_amount, want_amount)


def _read_field(full_img, geo, ref_pos):
    """OCR one in-game amount field; returns int or None."""
    x, y = geo.pt(ref_pos)
    crop = full_img.crop((max(0, x - 46), max(0, y - 16), x + 46, y + 16))
    digits = re.sub(r"\D", "", "".join(
        c[0] for c in sorted(ocr_cells(crop, scale=4), key=lambda c: c[1])))
    return int(digits) if digits else None


def _verify_fill(geo, have_amount, want_amount):
    """Re-capture the amount fields after typing; warn if the game shows
    different numbers (lost focus mid-sequence, moved panel, missed key)."""
    try:
        full = capture_full()
        got_have = _read_field(full, geo, HAVE_FIELD_POS)
        got_want = _read_field(full, geo, WANT_FIELD_POS)
        if got_have == have_amount and got_want == want_amount:
            return
        msg = (f"FILL CHECK FAILED — game shows "
               f"{got_have if got_have is not None else '?'} / "
               f"{got_want if got_want is not None else '?'} but expected "
               f"{have_amount} / {want_amount}. Check before placing!")
        log(msg)
        if _APP is not None:
            _APP.q.put(("toast", msg))
    except Exception:
        log("fill verification failed:\n" + traceback.format_exc())


# ------------------------------------------------- geometry / capture ---

class Geometry:
    """Maps reference-space (1920x1080) coordinates onto the real screen:
    screen = ref * s + (dx, dy).

    The trade tables can carry their own transform (ts/tdx/tdy): PoE2's
    tooltip sits at a different offset from the panel tabs than PoE1's, so
    the tables crop is anchored on the Available/Competing headers
    themselves while names and the auto-fill fields keep the tab-derived
    transform.  When no table anchors are known both transforms match."""

    def __init__(self, w, h, s=None, dx=None, dy=None,
                 ts=None, tdx=None, tdy=None):
        self.w, self.h = w, h
        self.s = s if s is not None else h / REF_H
        self.dx = dx if dx is not None else w / 2 - (REF_W / 2) * self.s
        self.dy = dy if dy is not None else 0.0
        self.ts = ts if ts is not None else self.s
        self.tdx = tdx if tdx is not None else self.dx
        self.tdy = tdy if tdy is not None else self.dy

    def pt(self, p):
        return (int(round(p[0] * self.s + self.dx)),
                int(round(p[1] * self.s + self.dy)))

    def box(self, b):
        x1, y1 = self.pt(b[:2])
        x2, y2 = self.pt(b[2:])
        return (max(0, x1), max(0, y1), min(self.w, x2), min(self.h, y2))

    def tbox(self, b):
        x1 = int(round(b[0] * self.ts + self.tdx))
        y1 = int(round(b[1] * self.ts + self.tdy))
        x2 = int(round(b[2] * self.ts + self.tdx))
        y2 = int(round(b[3] * self.ts + self.tdy))
        return (max(0, x1), max(0, y1), min(self.w, x2), min(self.h, y2))

    def to_dict(self):
        return {"s": self.s, "dx": self.dx, "dy": self.dy,
                "ts": self.ts, "tdx": self.tdx, "tdy": self.tdy}

    def close_to(self, other, tol=12):
        return (abs(self.dx - other.dx) <= tol
                and abs(self.dy - other.dy) <= tol
                and abs(self.tdx - other.tdx) <= tol
                and abs(self.tdy - other.tdy) <= tol)


_GEO = None     # geometry of the last capture, used by auto_fill
_APP = None     # running App instance, for worker threads to post UI events

MAX_PROFILES = 3    # remembered panel positions per resolution (PoE1
                    # centered, PoE2 default-left, a dragged window, ...)


def load_geometries(w, h):
    """Cached calibration profiles for this resolution, most recent first.
    Returns (profiles, from_cache) — an empty cache yields the default
    center-anchored geometry."""
    try:
        import json
        cfg = json.loads(CALIB_FILE.read_text(encoding="utf-8"))
        if cfg.get("w") == w and cfg.get("h") == h:
            raw = cfg.get("profiles")
            if raw is None and "s" in cfg:      # migrate the old format
                raw = [{"s": cfg["s"], "dx": cfg["dx"], "dy": cfg["dy"]}]
            profiles = [Geometry(w, h, p["s"], p["dx"], p["dy"],
                                 p.get("ts"), p.get("tdx"), p.get("tdy"))
                        for p in raw or []]
            if profiles:
                return profiles, True
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return [Geometry(w, h)], False


def save_geometries(w, h, profiles):
    try:
        import json
        CALIB_FILE.write_text(json.dumps(
            {"w": w, "h": h,
             "profiles": [g.to_dict() for g in profiles[:MAX_PROFILES]]}),
            encoding="utf-8")
    except OSError:
        pass


def remember_geometry(geo, profiles=None):
    """Put `geo` at the front of the profile cache, replacing any profile
    at (nearly) the same position."""
    profiles = [g for g in (profiles or []) if not geo.close_to(g)]
    save_geometries(geo.w, geo.h, [geo] + profiles)


def screen_size():
    u = ctypes.windll.user32
    return u.GetSystemMetrics(0), u.GetSystemMetrics(1)


def capture_full():
    """Grab the primary monitor as a PIL image."""
    import mss
    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def make_crops(full_img, geo):
    """Cut out the three OCR regions and normalize them to reference size,
    so the OCR pipeline sees identical input at every resolution.  The
    tables use the table-anchored transform; the name boxes the tab one."""
    crops = []
    for ref_box, table in ((TABLES_BOX, True), (WANT_BOX, False),
                           (HAVE_BOX, False)):
        rw, rh = ref_box[2] - ref_box[0], ref_box[3] - ref_box[1]
        src = geo.tbox(ref_box) if table else geo.box(ref_box)
        crops.append(full_img.crop(src).resize((rw, rh), Image.LANCZOS))
    return crops


def calibrate(full_img):
    """Locate the exchange panel on the full screenshot and derive the
    geometry.  The I Want / I Have tabs anchor names and fill fields; the
    Available/Competing Trades titles anchor the tables crop exactly (PoE2
    positions its tooltip differently, and may cover the Market Ratio
    title entirely — that anchor is only a cross-check).  Returns a
    Geometry or None."""
    w, h = full_img.size
    x0, y0 = int(w * 0.10), int(h * 0.05)
    band = full_img.crop((x0, y0, int(w * 0.90), int(h * 0.60)))
    f = max(1.0, 2.0 * REF_H / h)
    cells = ocr_cells(band, scale=f)

    def center(c):
        return (x0 + (c[1] + c[3] / 2) / f, y0 + c[2] / f)

    def left(c):
        return (x0 + c[1] / f, y0 + c[2] / f)

    mk = wt = hv = av = None
    ratios, stocks, sub_pairs = [], [], []
    for c in cells:
        t = re.sub(r"[^a-z]", "", c[0].lower())
        if mk is None and "market" in t:
            mk = center(c)
        elif wt is None and t in ("iwant", "want"):
            wt = center(c)
        elif hv is None and t in ("ihave", "have"):
            hv = center(c)
        elif av is None and "available" in t:
            av = left(c)
        elif t == "ratio":
            ratios.append(center(c))
        elif t == "stock":
            stocks.append(center(c))
        elif t == "ratiostock":
            sub_pairs.append(center(c))     # OCR merged the pair — its
                                            # center IS the row midpoint

    # pair 'Ratio' with the 'Stock' on the same line: chrome-immune anchors
    # sitting inside the parchment itself
    used_stocks = set()
    for r in ratios:
        best = None
        for i, st in enumerate(stocks):
            if i in used_stocks or abs(st[1] - r[1]) > 12 * (h / REF_H):
                continue
            gap = st[0] - r[0]
            if 30 <= gap <= 260 and (best is None or gap < best[1]):
                best = (i, gap)
        if best is not None:
            used_stocks.add(best[0])
            st = stocks[best[0]]
            sub_pairs.append(((r[0] + st[0]) / 2, (r[1] + st[1]) / 2))
    sub_pairs.sort(key=lambda p: p[1])

    s = dx = dy = None
    if wt and hv:
        s = (hv[0] - wt[0]) / (ANCHOR_HAVE[0] - ANCHOR_WANT[0])
        if not 0.3 < s < 5:
            log(f"calibration: implausible tab scale {s:.3f}")
            s = None
        else:
            dx = (hv[0] + wt[0]) / 2 - (REF_W / 2) * s
            dy = (wt[1] + hv[1]) / 2 - ANCHOR_WANT[1] * s
            if mk:
                exp_mx = ANCHOR_MARKET[0] * s + dx
                exp_my = ANCHOR_MARKET[1] * s + dy
                if abs(exp_mx - mk[0]) > 60 * s or \
                        abs(exp_my - mk[1]) > 60 * s:
                    log(f"calibration: market anchor off by "
                        f"({mk[0] - exp_mx:.0f}, {mk[1] - exp_my:.0f}) — "
                        "using anyway")

    ts = tdx = tdy = None
    if sub_pairs:
        if len(sub_pairs) >= 2:
            ts = (sub_pairs[1][1] - sub_pairs[0][1]) / ANCHOR_SUBHDR_DY
            if not 0.3 < ts < 5:
                log(f"calibration: implausible table scale {ts:.3f}")
                ts = None
        if ts is None:
            ts = s if s is not None else h / REF_H
        tdx = sub_pairs[0][0] - ANCHOR_SUBHDR[0] * ts
        tdy = sub_pairs[0][1] - ANCHOR_SUBHDR[1] * ts
    elif av is not None:
        # fallback: the title's LEFT edge (chrome merges into its right)
        ts = s if s is not None else h / REF_H
        tdx = av[0] - ANCHOR_AVAIL_LEFT[0] * ts
        tdy = av[1] - ANCHOR_AVAIL_LEFT[1] * ts

    if s is None and ts is None:
        log(f"calibration: anchors not found (market={bool(mk)} "
            f"want={bool(wt)} have={bool(hv)} avail={bool(av)} "
            f"subheaders={len(sub_pairs)})")
        return None
    if s is None:
        # tabs unreadable — fall back to the table transform for everything
        s, dx, dy = ts, tdx, tdy
        log("calibration: tabs not found — names/fields use table anchors")
    return Geometry(w, h, s, dx, dy, ts, tdx, tdy)


def read_screen(full_img, allow_calibrate=True, persist=True):
    """Full pipeline: try each cached panel-position profile (most recent
    first), then fall back to a fresh calibration.  Keeps profiles for
    several window positions so switching games (PoE1 centered, PoE2
    default-left) never needs a recalibration round-trip twice."""
    global _GEO
    w, h = full_img.size
    profiles, cached = load_geometries(w, h)

    def rows_of(d):
        return len(d["available"]) + len(d["competing"])

    def suspicious(d):
        return rows_of(d) == 0 or d["unparsed"] > rows_of(d)

    best = None, None       # (data, geo) with the most rows so far
    for i, geo in enumerate(profiles):
        data = read_panel(*make_crops(full_img, geo))
        if not suspicious(data):
            _GEO = geo
            if i > 0:
                log(f"profile {i + 1} matched (dx={geo.dx:.0f}) — "
                    "promoting to front")
                if persist:
                    remember_geometry(geo, profiles)
            if cached or not allow_calibrate:
                return data
            # first ever capture at this resolution: the default guess
            # worked, but calibrate once anyway for exact fill anchoring
            log("first capture at this resolution — calibrating anchors")
            best = data, geo
            break
        if best[0] is None or rows_of(data) > rows_of(best[0]):
            best = data, geo
        if i == 0 and len(profiles) > 1:
            log("last profile missed — trying other cached positions")

    if allow_calibrate:
        if best[0] is None or suspicious(best[0]):
            log("no cached profile fits — auto-calibrating")
        geo2 = calibrate(full_img)
        if geo2 is not None:
            data2 = read_panel(*make_crops(full_img, geo2))
            if rows_of(data2) > 0 and rows_of(data2) >= rows_of(best[0] or
                                                               data2):
                log(f"calibration ok: scale={geo2.s:.3f} dx={geo2.dx:.0f} "
                    f"dy={geo2.dy:.0f} tables dx={geo2.tdx:.0f} "
                    f"dy={geo2.tdy:.0f}")
                _GEO = geo2
                if persist:
                    remember_geometry(geo2, profiles if cached else [])
                return data2
            log("calibration did not improve the parse")
        if best[1] is not None and not suspicious(best[0]) and persist:
            remember_geometry(best[1], profiles if cached else [])

    _GEO = best[1] if best[1] is not None else profiles[0]
    return best[0] if best[0] is not None else read_panel(
        *make_crops(full_img, profiles[0]))


def read_panel(tables_img, want_img, have_img):
    tagged = read_tables(tables_img)
    debug_text = "\n".join(f"{s}: {t}" for s, t in tagged) + "\n\ncells:\n" + \
        "\n".join(f"  x={x:6.0f} cy={cy:6.0f} {t!r}"
                  for t, x, cy, _w in _LAST_CELLS)
    if DEBUG_SAVE:
        try:
            tables_img.save(APP_DIR / "last_capture.png")
            (APP_DIR / "last_ocr.txt").write_text(debug_text, encoding="utf-8")
        except OSError:
            pass
    data = parse_tables(tagged)
    # a suspicious parse (nothing in a section, or unreadable rows) is worth
    # keeping for later diagnosis — save a timestamped copy
    if DEBUG_SAVE and (data["unparsed"] or
                       (tagged and (not data["available"]
                                    or not data["competing"]))):
        try:
            dbg = APP_DIR / "debug"
            dbg.mkdir(exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            tables_img.save(dbg / f"{stamp}.png")
            (dbg / f"{stamp}.txt").write_text(debug_text, encoding="utf-8")
            log(f"suspicious parse saved to debug/{stamp}.png "
                f"(avail={len(data['available'])}, "
                f"comp={len(data['competing'])}, "
                f"unparsed={data['unparsed']})")
            for old in sorted(dbg.glob("*.png"))[:-DEBUG_KEEP]:
                old.unlink()
                old.with_suffix(".txt").unlink(missing_ok=True)
        except OSError:
            pass
    try:
        data["want_name"], data["have_name"] = read_names(want_img, have_img)
    except Exception:
        data["want_name"], data["have_name"] = "I Want", "I Have"
    return data


# --------------------------------------------------------- auto-update ---

def _ver_tuple(v):
    nums = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in nums[:3]) if nums else (0,)


def check_update(app):
    """Silently update from the latest GitHub release: download the source
    zip, extract over the app dir, refresh deps/launcher if they changed,
    then ask the app to restart itself.  Failures only log."""
    import io
    import json
    import subprocess
    import urllib.request
    import zipfile
    CREATE_NO_WINDOW = 0x08000000
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "poe-flipper"})
        with urllib.request.urlopen(req, timeout=15) as r:
            rel = json.load(r)
        tag = rel.get("tag_name", "")
        if _ver_tuple(tag) <= _ver_tuple(__version__):
            return
        asset = next((a for a in rel.get("assets", [])
                      if a.get("name", "").endswith(".zip")), None)
        if asset is None:
            log(f"update {tag} available but has no zip asset — skipping")
            return
        log(f"updating {__version__} -> {tag} "
            f"({asset['name']}, {asset.get('size', 0) // 1024} KB)")
        with urllib.request.urlopen(asset["browser_download_url"],
                                    timeout=120) as r:
            data = r.read()

        def snapshot(name):
            try:
                return (APP_DIR / name).read_bytes()
            except OSError:
                return b""

        req_before = snapshot("requirements.txt")
        cs_before = snapshot("launcher.cs")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(APP_DIR)

        py = APP_DIR / ".venv" / "Scripts" / "python.exe"
        if snapshot("requirements.txt") != req_before and py.exists():
            log("requirements changed — updating venv")
            subprocess.run(
                [str(py), "-m", "pip", "install", "--quiet",
                 "--disable-pip-version-check", "-r",
                 str(APP_DIR / "requirements.txt")],
                timeout=900, creationflags=CREATE_NO_WINDOW)
        csc = Path(os.environ.get("WINDIR", r"C:\Windows")) / \
            "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
        if snapshot("launcher.cs") != cs_before and csc.exists():
            log("launcher changed — recompiling")
            subprocess.run(
                [str(csc), "/nologo", "/target:winexe",
                 f"/win32icon:{APP_DIR / 'flipper.ico'}",
                 f"/out:{APP_DIR / 'PoE Flipper.exe'}",
                 str(APP_DIR / "launcher.cs")],
                timeout=120, creationflags=CREATE_NO_WINDOW)

        try:
            UPDATE_MARKER.write_text(tag, encoding="utf-8")
        except OSError:
            pass
        log(f"updated to {tag} — restarting when idle")
        app.request_restart(tag)
    except Exception:
        log("auto-update failed:\n" + traceback.format_exc())


# ------------------------------------------------------------------ UI ---

BG = "#1e1a16"
FG = "#e8ddc8"
ACCENT = "#c8a24a"
BTN_BG = "#3a332a"


class Popup:
    """Two-stage popup.  Created immediately on hotkey with a shared `ref`
    dict; the OCR worker fills ref['data'] / ref['error'] in the background
    while the user types the quantity."""

    def __init__(self, root, ref):
        self.root = root
        self.ref = ref
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=BG, highlightthickness=2,
                           highlightbackground=ACCENT)
        self.frame = tk.Frame(self.win, bg=BG, padx=14, pady=12)
        self.frame.pack()
        self.win.bind("<Escape>", lambda e: self.close())
        self.win.bind("<FocusOut>", self._on_focus_out)
        self._had_focus = False
        self._focus_target = None
        self._first_fill = None
        self.win.bind("<FocusIn>", self._on_focus_in)
        self._build_input_stage()
        self._place()
        self._force_focus()
        self._refresh_status()

    # -- stages --

    def _build_input_stage(self):
        tk.Label(self.frame, text="Flip check", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.status = tk.Label(self.frame, text="reading screen…", bg=BG,
                               fg="#8a8272", font=("Segoe UI", 8))
        self.status.pack(anchor="w")

        def make_entry(caption):
            tk.Label(self.frame, text=caption, bg=BG, fg=FG,
                     font=("Segoe UI", 9)).pack(anchor="w", pady=(6, 0))
            e = tk.Entry(self.frame, font=("Segoe UI", 14), width=12,
                         bg="#2b251e", fg=FG, insertbackground=FG,
                         relief="flat")
            e.pack(anchor="w", ipady=3)
            # explicit key handling: numpad keys come in as KP_* keysyms (or
            # as navigation keys with NumLock off), and while Alt is held (the
            # user needs it for the in-game panel) Windows treats numpad
            # digits as alt-code entry — so decode by keycode/keysym ourselves
            e.bind("<KeyPress>", lambda ev: self._entry_key(ev, alt=False))
            e.bind("<Alt-KeyPress>", lambda ev: self._entry_key(ev, alt=True))
            return e

        self.entry_sell = make_entry("Sell — how many (I Have)?")
        self.entry_buy = make_entry(
            "Flip budget — I Have to spend buying I Want (optional)")
        self.entry_qty = make_entry(
            "Buy — how many I Want (optional)")
        self.entry = self.entry_sell  # first focus target
        self._focus_target = self.entry_sell
        self.entry_sell.focus_set()

    _NUMPAD_NAV = {"KP_Insert": "0", "KP_End": "1", "KP_Down": "2",
                   "KP_Next": "3", "KP_Left": "4", "KP_Begin": "5",
                   "KP_Right": "6", "KP_Home": "7", "KP_Up": "8",
                   "KP_Prior": "9"}

    def _entry_key(self, event, alt):
        ks = event.keysym
        w = event.widget
        if ks in ("Return", "KP_Enter"):
            self._on_submit(None)
            return "break"
        digit = None
        if 96 <= event.keycode <= 105:          # VK_NUMPAD0..9
            digit = str(event.keycode - 96)
        elif ks.startswith("KP_") and ks[3:].isdigit():
            digit = ks[3:]
        elif ks in self._NUMPAD_NAV:
            digit = self._NUMPAD_NAV[ks]
        elif alt and event.char.isdigit():
            digit = event.char
        if digit is not None:
            try:
                w.delete("sel.first", "sel.last")   # typing replaces selection
            except tk.TclError:
                pass
            w.insert("insert", digit)
            return "break"
        if alt and ks == "BackSpace":
            try:
                w.delete("sel.first", "sel.last")
            except tk.TclError:
                idx = w.index("insert")
                if idx:
                    w.delete(idx - 1)
            return "break"
        return None

    def _refresh_status(self):
        try:
            if not self.win.winfo_exists() or not hasattr(self, "status"):
                return
        except tk.TclError:
            return
        d = self.ref.get("data")
        err = self.ref.get("error")
        try:
            if err:
                self.status.configure(text="⚠ " + err, fg="#d06050")
            elif d is None:
                self.status.configure(text="reading screen…")
                self.win.after(100, self._refresh_status)
            else:
                info = (f"{d.get('have_name', '?')} → {d.get('want_name', '?')}"
                        f"  |  {len(d['available'])} avail, "
                        f"{len(d['competing'])} competing")
                if d.get("market"):
                    info += f", market {d['market']:g}"
                if d.get("unparsed"):
                    info += f"  (⚠ {d['unparsed']} rows unreadable)"
                self.status.configure(text=info, fg="#8a8272")
        except tk.TclError:
            pass

    @staticmethod
    def _parse_qty(entry):
        raw = entry.get().strip().replace(",", "").replace(" ", "")
        return int(raw) if raw.isdigit() and int(raw) > 0 else None

    def _on_submit(self, _event):
        n = self._parse_qty(self.entry_sell)
        m = self._parse_qty(self.entry_buy)
        q = self._parse_qty(self.entry_qty)
        if n is None and m is None and q is None:
            for e in (self.entry_sell, self.entry_buy, self.entry_qty):
                e.configure(bg="#4a2b25")
            return
        if self.ref.get("error"):
            return
        if self.ref.get("data") is None:
            self.status.configure(text="still reading screen… hold on")
            self.win.after(150, lambda: self._on_submit(None))
            return
        data = self.ref["data"]
        sell_sugg = build_suggestions(data, n) if n else None
        buy_sugg = build_buy_suggestions(data, m) if m else None
        qty_sugg = build_buy_qty_suggestions(data, q) if q else None
        for child in self.frame.winfo_children():
            child.destroy()
        del self.status
        self._first_fill = None
        self._build_result_stage(n, sell_sugg, m, buy_sugg, q, qty_sugg)
        # Enter fires the focused FILL button (the first row by default)
        self._focus_target = self._first_fill
        if self._first_fill is not None:
            self._first_fill.focus_set()
        self._place()
        self._force_focus()

    def _render_rows(self, rows, per_unit_of):
        """Render suggestion rows into a grid.  per_unit_of: 'price' for sell
        rows (want-per-have) or 'pay_per_unit' for buy rows."""
        grid = tk.Frame(self.frame, bg=BG)
        grid.pack(anchor="w")
        row_i = 0
        fine_started = False
        for label, s in rows:
            if s.get("fine") and not fine_started:
                fine_started = True
                tk.Label(grid, text="— finer pricing (bigger dividers) —",
                         bg=BG, fg="#5a5448", font=("Segoe UI", 8)).grid(
                    row=row_i, column=0, columnspan=6, sticky="w",
                    pady=(6, 2))
                row_i += 1
            tk.Label(grid, text=label, bg=BG, fg=FG, font=("Segoe UI", 9),
                     anchor="w").grid(row=row_i, column=0, sticky="w",
                                      padx=(0, 12), pady=2)
            amounts = f"give {s['used']} → get {s['want_total']}"
            tk.Label(grid, text=amounts, bg=BG, fg=ACCENT,
                     font=("Segoe UI", 11, "bold")).grid(row=row_i, column=1,
                                                         sticky="w",
                                                         padx=(0, 6))
            leftover = f"+{s['left']} left" if s["left"] else ""
            tk.Label(grid, text=leftover, bg=BG, fg="#8a8272",
                     font=("Segoe UI", 8)).grid(row=row_i, column=2,
                                                sticky="w", padx=(0, 12))
            per = s[per_unit_of] if per_unit_of in s else s["price"]
            info = f"{s['w']}:{s['d']}  ({per:.4g}/ea)"
            tk.Label(grid, text=info, bg=BG, fg="#8a8272",
                     font=("Consolas", 9)).grid(row=row_i, column=3,
                                                sticky="w", padx=(0, 12))
            if "profit" in s:
                pct = s["profit_pct"]
                color = ("#7aa86a" if pct >= 15
                         else "#c05a50" if pct <= 0 else "#8a8272")
                rs = (f"resell ≈{s['resell_revenue']} "
                      f"({s['profit']:+} | {pct:+.0f}%)")
                tk.Label(grid, text=rs, bg=BG, fg=color,
                         font=("Consolas", 9, "bold" if pct >= 15 else
                               "normal")).grid(row=row_i, column=4,
                                               sticky="w", padx=(0, 12))
            btn = self._fill_btn(grid, s)
            btn.grid(row=row_i, column=5, pady=1)
            if self._first_fill is None:
                self._first_fill = btn
            row_i += 1

    def _build_result_stage(self, n, sell_sugg, m, buy_sugg, q=None,
                            qty_sugg=None):
        d = self.ref["data"]
        have = d.get("have_name", "I Have")
        want = d.get("want_name", "I Want")
        parts = []
        if n:
            parts.append(f"sell {n} {have}")
        if m:
            parts.append(f"buy {want} with {m} {have}")
        if q:
            parts.append(f"buy {q} {want}")
        title = " / ".join(parts)
        title = title[0].upper() + title[1:]
        tk.Label(self.frame, text=title, bg=BG,
                 fg=ACCENT, font=("Segoe UI", 11, "bold")).pack(anchor="w")

        ctx = []
        if d["market"]:
            ctx.append(f"market {d['market']:g}")
        if d["available"]:
            ctx.append(f"best buy-offer {d['available'][0]['price']:g}")
        if d["competing"]:
            c = d["competing"][0]
            ctx.append(f"best competitor {c['price']:g} "
                       f"(x{c['stock'] or '?'})")
        if ctx:
            tk.Label(self.frame, text=" | ".join(ctx), bg=BG, fg="#8a8272",
                     font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 4))

        if (sell_sugg and sell_sugg.get("dead")) or \
                (buy_sugg and buy_sugg.get("dead")) or \
                (qty_sugg and qty_sugg.get("dead")):
            tk.Label(self.frame,
                     text="⚠ MARKET LOOKS DEAD/THIN — prices may be junk",
                     bg=BG, fg="#c05a50",
                     font=("Segoe UI", 9, "bold")).pack(anchor="w",
                                                        pady=(0, 4))

        notes = []
        if sell_sugg:
            tk.Label(self.frame, text=f"SELL {have}", bg=BG, fg="#b0a890",
                     font=("Segoe UI", 9, "bold")).pack(anchor="w",
                                                        pady=(6, 0))
            if sell_sugg["rows"]:
                self._render_rows(sell_sugg["rows"], "price")
            else:
                tk.Label(self.frame, text="No usable sell suggestion.",
                         bg=BG, fg="#d06050",
                         font=("Segoe UI", 9)).pack(anchor="w")
            if sell_sugg["instant"]:
                sold, got = sell_sugg["instant"]
                tk.Label(self.frame,
                         text=f"Instant: dump ~{sold} right now for ~{got}",
                         bg=BG, fg="#7a9a6a", font=("Segoe UI", 9)).pack(
                    anchor="w", pady=(2, 0))
            notes += sell_sugg["notes"]

        if buy_sugg:
            title = f"BUY {want} → RESELL"
            tk.Label(self.frame, text=title, bg=BG, fg="#b0a890",
                     font=("Segoe UI", 9, "bold")).pack(anchor="w",
                                                        pady=(8, 0))
            if buy_sugg["rows"]:
                self._render_rows(buy_sugg["rows"], "pay_per_unit")
                if buy_sugg["resale"]:
                    tk.Label(self.frame,
                             text=f"resale estimated at "
                                  f"{buy_sugg['resale']:.4g}/ea "
                                  f"(0.5% under the sellers' wall)",
                             bg=BG, fg="#5a5448",
                             font=("Segoe UI", 8)).pack(anchor="w")
            else:
                tk.Label(self.frame, text="No usable buy suggestion.",
                         bg=BG, fg="#d06050",
                         font=("Segoe UI", 9)).pack(anchor="w")
            if buy_sugg["instant"]:
                got, spent = buy_sugg["instant"]
                tk.Label(self.frame,
                         text=f"Instant: buy ~{got} right now for ~{spent}",
                         bg=BG, fg="#7a9a6a", font=("Segoe UI", 9)).pack(
                    anchor="w", pady=(2, 0))
            notes += buy_sugg["notes"]

        if qty_sugg:
            tk.Label(self.frame, text=f"BUY {q} {want}", bg=BG, fg="#b0a890",
                     font=("Segoe UI", 9, "bold")).pack(anchor="w",
                                                        pady=(8, 0))
            if qty_sugg["rows"]:
                self._render_rows(qty_sugg["rows"], "pay_per_unit")
            else:
                tk.Label(self.frame, text="No usable suggestion.",
                         bg=BG, fg="#d06050",
                         font=("Segoe UI", 9)).pack(anchor="w")
            if qty_sugg["instant"]:
                got, spent = qty_sugg["instant"]
                tk.Label(self.frame,
                         text=f"Instant: buy ~{got} right now for ~{spent}",
                         bg=BG, fg="#7a9a6a", font=("Segoe UI", 9)).pack(
                    anchor="w", pady=(2, 0))
            notes += [x for x in qty_sugg["notes"] if x not in notes]

        for note in notes:
            tk.Label(self.frame, text="⚠ " + note, bg=BG, fg="#d0a050",
                     font=("Segoe UI", 8), wraplength=560,
                     justify="left").pack(anchor="w", pady=(4, 0))
        tk.Label(self.frame,
                 text="FILL = click game fields and type amounts | "
                      "Esc or click outside to close",
                 bg=BG, fg="#5a5448", font=("Segoe UI", 7)).pack(
            anchor="e", pady=(8, 0))
        for seq in ("<Return>", "<KP_Enter>", "<Alt-Return>", "<Alt-KP_Enter>"):
            self.win.bind(seq, self._results_return)

    def _results_return(self, _event=None):
        focused = None
        try:
            focused = self.win.focus_get()
        except (tk.TclError, KeyError):
            pass
        if isinstance(focused, tk.Button):
            focused.invoke()
        else:
            self.close()
        return "break"

    def _fill_btn(self, parent, s):
        def do_fill():
            have_amount, want_amount = s["used"], s["want_total"]
            self.close()
            threading.Thread(target=auto_fill,
                             args=(have_amount, want_amount),
                             daemon=True).start()
        return tk.Button(parent, text="⤷ FILL", command=do_fill, bg=ACCENT,
                         fg="#1e1a16", activebackground="#e0be6a",
                         activeforeground="#1e1a16", relief="flat",
                         font=("Segoe UI", 9, "bold"), padx=10,
                         cursor="hand2", highlightthickness=2,
                         highlightbackground=BG, highlightcolor="#f5f0e0")


    # -- window management --

    def _place(self):
        self.win.update_idletasks()
        w = self.win.winfo_reqwidth()
        h = self.win.winfo_reqheight()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        y = min(int(sh * 0.30), max(10, sh - h - 30))
        self.win.geometry(f"+{(sw - w) // 2}+{y}")

    def _force_focus(self):
        try:
            self.win.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.win.winfo_id())
        except (tk.TclError, OSError):
            return
        self._focus_attempts = 0
        self._try_focus(hwnd)

    def _try_focus(self, hwnd):
        """Steal foreground from the game: attach to the foreground thread's
        input queue, tap Alt to satisfy the foreground-lock heuristic, then
        SetForegroundWindow.  Retry a few times — games re-grab focus."""
        try:
            if not self.win.winfo_exists():
                return
        except tk.TclError:
            return
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        try:
            fg = u.GetForegroundWindow()
            if fg != hwnd:
                fg_thread = u.GetWindowThreadProcessId(fg, None)
                cur_thread = k.GetCurrentThreadId()
                attached = False
                if fg_thread and fg_thread != cur_thread:
                    attached = bool(u.AttachThreadInput(fg_thread, cur_thread, True))
                VK_MENU = 0x12
                u.keybd_event(VK_MENU, 0, 0, 0)
                u.keybd_event(VK_MENU, 0, 2, 0)
                u.SetForegroundWindow(hwnd)
                u.BringWindowToTop(hwnd)
                if attached:
                    u.AttachThreadInput(fg_thread, cur_thread, False)
        except OSError:
            pass
        try:
            self.win.lift()
            self.win.focus_force()
            if self._focus_target is not None and \
                    self._focus_target.winfo_exists():
                self._focus_target.focus_set()
        except tk.TclError:
            pass
        self._focus_attempts += 1
        if u.GetForegroundWindow() != hwnd:
            if self._focus_attempts < 8:
                self.win.after(150, lambda: self._try_focus(hwnd))
            else:
                # unfocusable popups can't be closed by Esc/click-outside —
                # don't leave a zombie window over the game
                log("could not take keyboard focus from the foreground window")
                self.close()

    def _on_focus_in(self, _event):
        self._had_focus = True

    def _on_focus_out(self, _event):
        self.win.after(120, self._check_focus)

    def _check_focus(self):
        # only auto-close once we actually held focus — otherwise the retry
        # dance above would close the popup before the user ever typed
        if not self._had_focus:
            return
        try:
            if self.win.focus_get() is None:
                self.close()
        except (tk.TclError, KeyError):
            pass

    def close(self):
        try:
            self.win.destroy()
        except tk.TclError:
            pass


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.q = queue.Queue()
        self.popup = None
        self.busy = False
        self.icon = None
        self._restart = False
        self.hotkey, self.hk_mods, self.hk_vk = load_hotkey()
        global _APP
        _APP = self

    def run(self):
        threading.Thread(target=self._hotkey_listener, daemon=True).start()
        self._start_tray()
        threading.Thread(target=warmup_ocr, daemon=True).start()
        threading.Thread(target=self._update_worker, daemon=True).start()
        self._notify_if_updated()
        log(f"flipper {__version__} running — press {self.hotkey} over the "
            f"Market Ratio panel (rebind in flipper_config.json)")
        self.root.after(50, self._poll)
        self.root.mainloop()
        if self.icon:
            self.icon.stop()
        if self._restart:
            import subprocess
            subprocess.Popen([sys.executable, str(APP_DIR / "flipper.py")],
                             cwd=str(APP_DIR))

    def _update_worker(self):
        time.sleep(8)   # let startup settle first
        if load_config().get("auto_update", True):
            check_update(self)

    def request_restart(self, tag):
        self.q.put(("restart", tag))

    def _notify_if_updated(self):
        if not UPDATE_MARKER.exists():
            return
        try:
            tag = UPDATE_MARKER.read_text(encoding="utf-8").strip()
            UPDATE_MARKER.unlink()
        except OSError:
            return

        def notify():
            time.sleep(3)   # give the tray icon time to appear
            try:
                self.icon.notify(f"Updated to {tag}", "PoE Flipper")
            except Exception:
                pass
        threading.Thread(target=notify, daemon=True).start()

    def _hotkey_listener(self):
        """Native hotkey via RegisterHotKey.  Unlike a keyboard hook (the
        `keyboard` library), this consumes only the exact combo and never
        delays or re-injects modifier events, so holding Alt in game stays
        perfectly responsive."""
        from ctypes import wintypes
        u = ctypes.windll.user32
        MOD_NOREPEAT = 0x4000
        WM_HOTKEY = 0x0312
        # retry for a few seconds: after a self-update restart the old
        # process may still hold the hotkey for a moment
        registered = False
        for _ in range(6):
            if u.RegisterHotKey(None, 1, self.hk_mods | MOD_NOREPEAT,
                                self.hk_vk):
                registered = True
                break
            time.sleep(1)
        if not registered:
            log(f"RegisterHotKey {self.hotkey} failed — another instance is "
                "likely running (or the combo is taken); exiting this one")
            self.q.put(("exit", None))
            return
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self._on_hotkey()

    def _start_tray(self):
        import pystray
        img = Image.new("RGB", (64, 64), "#1e1a16")
        dr = ImageDraw.Draw(img)
        dr.polygon([(10, 22), (40, 22), (40, 12), (56, 27), (40, 42),
                    (40, 32), (10, 32)], fill="#c8a24a")
        dr.polygon([(54, 42), (24, 42), (24, 52), (8, 37)], fill="#8a8272")
        menu = pystray.Menu(
            pystray.MenuItem(f"PoE Flipper ({self.hotkey})", None,
                             enabled=False),
            pystray.MenuItem("Exit", self._on_exit),
        )
        self.icon = pystray.Icon("poe-flipper", img, "PoE Flipper", menu)
        self.icon.run_detached()

    def _on_exit(self, _icon=None, _item=None):
        self.root.after(0, self.root.quit)

    def _on_hotkey(self):
        if self.busy:
            return
        self.busy = True
        ref = {"data": None, "error": None}
        self.q.put(("show", ref))
        threading.Thread(target=self._capture_worker, args=(ref,),
                         daemon=True).start()

    def _capture_worker(self, ref):
        try:
            data = read_screen(capture_full())
            if not data["available"] and not data["competing"]:
                ref["error"] = ("No trade tables found. Hold Alt over the "
                                "Market Ratio panel, then press the hotkey.")
            else:
                ref["data"] = data
        except Exception:
            log("capture failed:\n" + traceback.format_exc())
            ref["error"] = "Capture/OCR failed — see flipper.log"
        finally:
            self.busy = False

    def _show_toast(self, msg):
        """Small self-closing warning that never takes keyboard focus."""
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BG, highlightthickness=2,
                      highlightbackground="#c05a50")
        tk.Label(win, text=msg, bg=BG, fg="#e8b0a8", padx=16, pady=10,
                 font=("Segoe UI", 10, "bold"), wraplength=480,
                 justify="left").pack()
        win.update_idletasks()
        sw = win.winfo_screenwidth()
        win.geometry(f"+{(sw - win.winfo_reqwidth()) // 2}+80")
        win.bind("<Button-1>", lambda e: win.destroy())
        win.after(6000, win.destroy)

    def _popup_active(self):
        try:
            return self.popup is not None and self.popup.win.winfo_exists()
        except tk.TclError:
            return False

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "show":
                    if self.popup:
                        self.popup.close()
                    self.popup = Popup(self.root, payload)
                elif kind == "toast":
                    self._show_toast(payload)
                elif kind == "exit":
                    self.root.quit()
                    return
                elif kind == "restart":
                    if self._popup_active():
                        # don't yank the app out from under an open popup
                        self.root.after(
                            5000, lambda p=payload: self.q.put(("restart", p)))
                    else:
                        self._restart = True
                        self.root.quit()
                        return
        except queue.Empty:
            pass
        self.root.after(50, self._poll)


# ------------------------------------------------------------- CLI/test ---

def run_test(image_path, qty):
    t0 = time.time()
    full = Image.open(image_path).convert("RGB")
    data = read_screen(full, persist=False)
    geo = _GEO
    print(f"(OCR took {time.time() - t0:.2f}s; geometry scale={geo.s:.3f} "
          f"dx={geo.dx:.0f} dy={geo.dy:.0f}; "
          f"fill points have={geo.pt(HAVE_FIELD_POS)} "
          f"want={geo.pt(WANT_FIELD_POS)})")
    print(f"want: {data.get('want_name')}   have: {data.get('have_name')}")
    print(f"market ratio: {data['market']}")
    if data["unparsed"]:
        print(f"unparsed rows: {data['unparsed']}")
    print("available:")
    for e in data["available"]:
        print(f"  {'~' if e['approx'] else ' '}{e['price']:>8g} : 1   "
              f"stock {e['stock']}")
    print("competing:")
    for e in data["competing"]:
        print(f"  {'~' if e['approx'] else ' '}{e['price']:>8g} : 1   "
              f"stock {e['stock']}")
    if qty:
        print(f"\nsell suggestions for qty={qty}:")
        sugg = build_suggestions(data, qty)
        for label, s in sugg["rows"]:
            extra = f", {s['left']} left over" if s["left"] else ""
            tag = "fine " if s.get("fine") else "     "
            print(f"  {tag}{label:<24} {s['w']}:{s['d']}  "
                  f"({s['price']:.4g}/ea)  "
                  f"give {s['used']} -> get {s['want_total']}{extra}")
        if sugg["instant"]:
            print(f"  instant: dump ~{sugg['instant'][0]} for "
                  f"~{sugg['instant'][1]}")
        for note in sugg["notes"]:
            print("  note:", note)


def run_buy_test(image_path, budget):
    data = read_screen(Image.open(image_path).convert("RGB"), persist=False)
    print(f"\nbuy {data.get('want_name')} with budget={budget} "
          f"{data.get('have_name')}:")
    sugg = build_buy_suggestions(data, budget)
    for label, s in sugg["rows"]:
        tag = "fine " if s.get("fine") else "     "
        profit = (f"  resell ~{s['resell_revenue']} ({s['profit']:+}, "
                  f"{s['profit_pct']:+.0f}%)" if "profit" in s else "")
        print(f"  {tag}{label:<24} {s['w']}:{s['d']}  "
              f"({s['pay_per_unit']:.4g}/ea)  give {s['used']} -> "
              f"get {s['want_total']}{profit}")
    if sugg["instant"]:
        print(f"  instant: buy ~{sugg['instant'][0]} for "
              f"~{sugg['instant'][1]}")
    for note in sugg["notes"]:
        print("  note:", note)


def run_pos():
    from ctypes import wintypes
    geo = load_geometries(*screen_size())[0][0]
    print(f"screen {geo.w}x{geo.h}  scale={geo.s:.3f} "
          f"dx={geo.dx:.0f} dy={geo.dy:.0f}")
    print(f"computed fill points: have={geo.pt(HAVE_FIELD_POS)} "
          f"want={geo.pt(WANT_FIELD_POS)}")
    pt = wintypes.POINT()
    print("Hover the in-game amount fields; printing cursor position for 20s.")
    for _ in range(40):
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        print(f"\r  ({pt.x}, {pt.y})      ", end="", flush=True)
        time.sleep(0.5)
    print()


def main():
    # per-monitor DPI awareness: makes screenshots, tk windows and
    # SetCursorPos all use the same physical-pixel coordinates even when
    # Windows display scaling is 125/150%
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (OSError, AttributeError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (OSError, AttributeError):
            pass

    apply_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", metavar="IMAGE",
                    help="run OCR + parsing on a saved 1920x1080 screenshot")
    ap.add_argument("--qty", type=int, default=None,
                    help="with --test: also print sell suggestions for this qty")
    ap.add_argument("--buy", type=int, default=None,
                    help="with --test: also print buy suggestions for this budget")
    ap.add_argument("--dump", action="store_true",
                    help="capture the screen now and save crop images")
    ap.add_argument("--pos", action="store_true",
                    help="print the mouse position for 20s (field calibration)")
    args = ap.parse_args()

    if args.pos:
        run_pos()
        return
    if args.dump:
        full = capture_full()
        geo = load_geometries(*full.size)[0][0]
        tables, want, have = make_crops(full, geo)
        tables.save(APP_DIR / "dump_tables.png")
        want.save(APP_DIR / "dump_want.png")
        have.save(APP_DIR / "dump_have.png")
        print(f"geometry: scale={geo.s:.3f} dx={geo.dx:.0f} dy={geo.dy:.0f}")
        print("saved dump_tables.png / dump_want.png / dump_have.png")
        return
    if args.test:
        run_test(args.test, args.qty)
        if args.buy:
            run_buy_test(args.test, args.buy)
        return
    App().run()


if __name__ == "__main__":
    main()
