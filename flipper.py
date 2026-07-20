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

OCR_SCALE = 2                        # upscale factor before OCR
ROW_PITCH = 22 * OCR_SCALE           # vertical distance between table rows (scaled px)

HOTKEY_DEFAULT = "alt+q"
CALIB_FILE = APP_DIR / "flipper_calib.json"
CONFIG_FILE = APP_DIR / "flipper_config.json"


def load_hotkey():
    """Read the hotkey from flipper_config.json (created with the default on
    first run).  Returns (label, modifier_mask, vk)."""
    import json
    cfg = {}
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    spec = cfg.get("hotkey", HOTKEY_DEFAULT)
    try:
        mods, vk = parse_hotkey(spec)
    except ValueError as e:
        log(f"invalid hotkey {spec!r} in flipper_config.json ({e}) — "
            f"falling back to {HOTKEY_DEFAULT}")
        spec = HOTKEY_DEFAULT
        mods, vk = parse_hotkey(spec)
    if "hotkey" not in cfg:
        try:
            cfg["hotkey"] = spec
            CONFIG_FILE.write_text(json.dumps(cfg, indent=2),
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

MAX_DENOM = 60          # largest "have"-side of a suggested ratio
WASTE_WEIGHT = 60.0     # penalty for leftover stock (fraction of N)
ERR_WEIGHT = 100.0      # penalty for deviating from target price
DENOM_WEIGHT = 0.4      # preference for small denominators (0.4 ≈ trade 0.4%
                        # price accuracy for one step of ratio simplicity)


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


def read_tables(pil_img):
    """OCR the tables crop, return [(section, text), ...] with section in
    {'market', 'available', 'competing'}."""
    global _LAST_CELLS
    cells = ocr_cells(pil_img)
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
    tagged = []
    for cy, text in rows:
        if y_end is not None and cy >= y_end:
            continue
        if y_comp is not None and cy > y_comp:
            section = "competing"
        elif y_avail is not None and cy > y_avail:
            section = "available"
        elif y_market is not None and cy > y_market:
            section = "market"
        else:
            continue
        tagged.append((section, text))
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
    return data


# ------------------------------------------------------ suggestion math ---

def snap_ratio(target, n, lo=None, hi=None, denom_weight=DENOM_WEIGHT,
               dmax_cap=MAX_DENOM):
    """Best integer ratio w:d with d dividing a chunk of n, price near target.

    lo/hi are exclusive price bounds (e.g. hi=best_ask to strictly undercut).
    Returns dict or None.
    """
    if target <= 0 or n <= 0:
        return None
    dmax = min(max(dmax_cap, math.ceil(3 / target)), 1000, n)
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
            score = rel_err * ERR_WEIGHT + waste * WASTE_WEIGHT + d * denom_weight
            if best is None or score < best["score"]:
                g = math.gcd(w, d)
                rw, rd = w // g, d // g
                best = {
                    "w": rw, "d": rd, "price": price, "used": used,
                    "left": n - used, "score": score,
                    "want_total": rw * (used // rd),
                }
    return best


def build_suggestions(data, n):
    """Return {'rows': [(label, snap)], 'notes': [...], 'instant': ...}."""
    out = {"rows": [], "notes": [], "instant": None}
    avail, comp = data["available"], data["competing"]
    market = data["market"]
    best_bid = avail[0]["price"] if avail else None

    if comp:
        best_ask = comp[0]["price"]
        second = comp[1]["price"] if len(comp) > 1 else best_ask * 1.15
        # two sets: simple ratios (small dividers, coarser prices) and fine
        # ratios (bigger dividers, prices hug the targets more tightly)
        sets = [
            (False, DENOM_WEIGHT, MAX_DENOM, [
                ("Fast (undercut best)", best_ask * 0.97, None, best_ask),
                ("Fair (match best)", best_ask, None, None),
                ("Greedy (above best)",
                 min((best_ask + second) / 2, best_ask * 1.10), best_ask, None),
            ]),
            (True, 0.03, 150, [
                ("Fast (undercut best)", best_ask * 0.99, None, best_ask),
                ("Fair (match best)", best_ask, None, None),
                ("Greedy (above best)", best_ask * 1.03, best_ask, None),
            ]),
        ]
    else:
        base = market or best_bid
        if base is None:
            out["notes"].append("Could not read any prices from the panel.")
            return out
        out["notes"].append(
            "No competing offers parsed — market may be dead. "
            "Targets based on market/available ratio."
        )
        sets = [
            (False, DENOM_WEIGHT, MAX_DENOM, [
                ("Fast (near market)", base, None, None),
                ("Fair (+15%)", base * 1.15, None, None),
                ("Greedy (+30%)", base * 1.30, None, None),
            ]),
        ]

    seen = set()
    for fine, dweight, dcap, targets in sets:
        for label, t, lo, hi in targets:
            s = snap_ratio(t, n, lo, hi, denom_weight=dweight, dmax_cap=dcap)
            if not s:
                continue
            key = (s["w"], s["d"], s["used"])
            if key in seen:
                continue
            seen.add(key)
            s["fine"] = fine
            out["rows"].append((label, s))

    if best_bid is not None and out["rows"]:
        fast_price = out["rows"][0][1]["price"]
        if fast_price <= best_bid:
            out["notes"].append(
                f"Best instant price ({best_bid:g}) beats the fast listing — "
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
    out = {"rows": [], "notes": [], "instant": None, "resale": None}
    avail, comp = data["available"], data["competing"]
    market = data["market"]
    best_bid = avail[0]["price"] if avail else None   # want-per-have

    # sellers currently charge 1/best_bid have per want-unit; undercut by 1%
    if best_bid:
        resale = (1 / best_bid) * 0.99
    elif market:
        resale = 1 / market
    else:
        resale = None
    out["resale"] = resale

    if comp:
        best_ask = comp[0]["price"]
        # paying more have per want = a LOWER want-per-have ratio, so
        # outbidding rival buyers means going below their ratio
        sets = [
            (False, DENOM_WEIGHT, MAX_DENOM, [
                ("Outbid (queue front)", best_ask * 0.97, None, best_ask),
                ("Match best bid", best_ask, None, None),
            ]),
            (True, 0.03, 150, [
                ("Outbid (queue front)", best_ask * 0.99, None, best_ask),
            ]),
        ]
        queue = comp[0]["stock"] or "?"
        out["notes"].append(
            f"Matching the best bid queues you behind ~{queue} "
            "already offered at that price."
        )
    else:
        base = market or best_bid
        if base is None:
            out["notes"].append("Could not read any prices from the panel.")
            return out
        out["notes"].append(
            "No competing buyers — bid anchored to market ratio.")
        sets = [(False, DENOM_WEIGHT, MAX_DENOM,
                 [("Bid (near market)", base, None, None)])]

    seen = set()
    for fine, dweight, dcap, targets in sets:
        for label, t, lo, hi in targets:
            s = snap_ratio(t, m, lo=lo, hi=hi,
                           denom_weight=dweight, dmax_cap=dcap)
            if not s:
                continue
            key = (s["w"], s["d"], s["used"])
            if key in seen:
                continue
            seen.add(key)
            s["fine"] = fine
            s["pay_per_unit"] = s["d"] / s["w"]
            if resale:
                revenue = s["want_total"] * resale
                s["resell_revenue"] = int(revenue)
                s["profit"] = int(revenue - s["used"])
                s["profit_pct"] = (revenue - s["used"]) / s["used"] * 100
            out["rows"].append((label, s))

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


# ------------------------------------------------- geometry / capture ---

class Geometry:
    """Maps reference-space (1920x1080) coordinates onto the real screen:
    screen = ref * s + (dx, dy)."""

    def __init__(self, w, h, s=None, dx=None, dy=None):
        self.w, self.h = w, h
        self.s = s if s is not None else h / REF_H
        self.dx = dx if dx is not None else w / 2 - (REF_W / 2) * self.s
        self.dy = dy if dy is not None else 0.0

    def pt(self, p):
        return (int(round(p[0] * self.s + self.dx)),
                int(round(p[1] * self.s + self.dy)))

    def box(self, b):
        x1, y1 = self.pt(b[:2])
        x2, y2 = self.pt(b[2:])
        return (max(0, x1), max(0, y1), min(self.w, x2), min(self.h, y2))


_GEO = None     # geometry of the last capture, used by auto_fill


def load_geometry(w, h):
    """Returns (geometry, from_cache)."""
    try:
        import json
        cfg = json.loads(CALIB_FILE.read_text(encoding="utf-8"))
        if cfg.get("w") == w and cfg.get("h") == h:
            return Geometry(w, h, cfg["s"], cfg["dx"], cfg["dy"]), True
    except (OSError, ValueError, KeyError):
        pass
    return Geometry(w, h), False


def save_geometry(geo):
    try:
        import json
        CALIB_FILE.write_text(json.dumps(
            {"w": geo.w, "h": geo.h, "s": geo.s, "dx": geo.dx, "dy": geo.dy}),
            encoding="utf-8")
    except OSError:
        pass


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
    so the OCR pipeline sees identical input at every resolution."""
    crops = []
    for ref_box in (TABLES_BOX, WANT_BOX, HAVE_BOX):
        rw, rh = ref_box[2] - ref_box[0], ref_box[3] - ref_box[1]
        crops.append(full_img.crop(geo.box(ref_box)).resize((rw, rh),
                                                            Image.LANCZOS))
    return crops


def calibrate(full_img):
    """Locate the exchange panel headers on the full screenshot and derive
    the geometry transform.  Returns a Geometry or None."""
    w, h = full_img.size
    x0, y0 = int(w * 0.15), int(h * 0.05)
    band = full_img.crop((x0, y0, int(w * 0.85), int(h * 0.32)))
    f = max(1.0, 2.0 * REF_H / h)
    cells = ocr_cells(band, scale=f)

    def center(c):
        return (x0 + (c[1] + c[3] / 2) / f, y0 + c[2] / f)

    mk = wt = hv = None
    for c in cells:
        t = re.sub(r"[^a-z]", "", c[0].lower())
        if mk is None and "market" in t:
            mk = center(c)
        elif wt is None and t in ("iwant", "want"):
            wt = center(c)
        elif hv is None and t in ("ihave", "have"):
            hv = center(c)
    if not (mk and wt and hv):
        log(f"calibration: headers not found (market={bool(mk)} "
            f"want={bool(wt)} have={bool(hv)})")
        return None
    s = (hv[0] - wt[0]) / (ANCHOR_HAVE[0] - ANCHOR_WANT[0])
    if not 0.3 < s < 5:
        log(f"calibration: implausible scale {s:.3f}")
        return None
    dx = (hv[0] + wt[0]) / 2 - (REF_W / 2) * s
    dy = (wt[1] + hv[1]) / 2 - ANCHOR_WANT[1] * s
    exp_mx, exp_my = ANCHOR_MARKET[0] * s + dx, ANCHOR_MARKET[1] * s + dy
    if abs(exp_mx - mk[0]) > 60 * s or abs(exp_my - mk[1]) > 60 * s:
        log(f"calibration: market anchor off by "
            f"({mk[0] - exp_mx:.0f}, {mk[1] - exp_my:.0f}) — using anyway")
    return Geometry(w, h, s, dx, dy)


def read_screen(full_img, allow_calibrate=True, persist=True):
    """Full pipeline: geometry -> crops -> OCR -> parse, with automatic
    recalibration if the current geometry yields nothing."""
    global _GEO
    w, h = full_img.size
    geo, cached = load_geometry(w, h)
    _GEO = geo
    data = read_panel(*make_crops(full_img, geo))

    def rows_of(d):
        return len(d["available"]) + len(d["competing"])

    # calibrate when the crop looks misaligned (nothing parsed / many rows
    # unreadable), or on the first ever capture at this resolution — a parse
    # can succeed off a slightly shifted crop, but the auto-fill click
    # positions need exact anchoring
    suspicious = rows_of(data) == 0 or data["unparsed"] > rows_of(data)
    if allow_calibrate and (suspicious or not cached):
        if suspicious:
            log(f"parse looks off (rows={rows_of(data)}, "
                f"unparsed={data['unparsed']}) — auto-calibrating")
        else:
            log("first capture at this resolution — calibrating anchors")
        geo2 = calibrate(full_img)
        if geo2 is not None:
            data2 = read_panel(*make_crops(full_img, geo2))
            if rows_of(data2) > 0 and rows_of(data2) >= rows_of(data):
                log(f"calibration ok: scale={geo2.s:.3f} "
                    f"dx={geo2.dx:.0f} dy={geo2.dy:.0f}")
                _GEO = geo2
                if persist:
                    save_geometry(geo2)
                return data2
            log("calibration did not improve the parse — keeping defaults")
        if not suspicious and persist:
            # default geometry works here; remember it so we don't re-run
            # calibration on every capture
            save_geometry(geo)
    return data


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
        except OSError:
            pass
    try:
        data["want_name"], data["have_name"] = read_names(want_img, have_img)
    except Exception:
        data["want_name"], data["have_name"] = "I Want", "I Have"
    return data


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
        self.entry = self.entry_sell  # first focus target
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
        if n is None and m is None:
            self.entry_sell.configure(bg="#4a2b25")
            self.entry_buy.configure(bg="#4a2b25")
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
        for child in self.frame.winfo_children():
            child.destroy()
        del self.status
        self._build_result_stage(n, sell_sugg, m, buy_sugg)
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
            if "profit" in s:
                info += f"  resell ≈{s['resell_revenue']}" \
                        f" ({s['profit']:+} | {s['profit_pct']:+.0f}%)"
            tk.Label(grid, text=info, bg=BG, fg="#8a8272",
                     font=("Consolas", 9)).grid(row=row_i, column=3,
                                                sticky="w", padx=(0, 12))
            self._copy_btn(grid, f"⧉ {s['w']}:{s['d']}",
                           f"{s['w']}:{s['d']}").grid(row=row_i, column=4,
                                                      padx=(0, 4))
            self._fill_btn(grid, s).grid(row=row_i, column=5)
            row_i += 1

    def _build_result_stage(self, n, sell_sugg, m, buy_sugg):
        d = self.ref["data"]
        have = d.get("have_name", "I Have")
        want = d.get("want_name", "I Want")
        parts = []
        if n:
            parts.append(f"sell {n} {have}")
        if m:
            parts.append(f"buy {want} with {m} {have}")
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
                                  f"(1% under current sellers)",
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

        for note in notes:
            tk.Label(self.frame, text="⚠ " + note, bg=BG, fg="#d0a050",
                     font=("Segoe UI", 8), wraplength=560,
                     justify="left").pack(anchor="w", pady=(4, 0))
        tk.Label(self.frame,
                 text="⤷ = click game fields and type amounts | "
                      "Esc or click outside to close",
                 bg=BG, fg="#5a5448", font=("Segoe UI", 7)).pack(
            anchor="e", pady=(8, 0))
        for seq in ("<Return>", "<KP_Enter>", "<Alt-Return>", "<Alt-KP_Enter>"):
            self.win.bind(seq, lambda e: self.close())

    def _fill_btn(self, parent, s):
        def do_fill():
            have_amount, want_amount = s["used"], s["want_total"]
            self.close()
            threading.Thread(target=auto_fill,
                             args=(have_amount, want_amount),
                             daemon=True).start()
        return tk.Button(parent, text="⤷", command=do_fill, bg=BTN_BG,
                         fg="#7a9a6a", activebackground=ACCENT, relief="flat",
                         font=("Segoe UI", 9, "bold"), padx=7, cursor="hand2")

    def _copy_btn(self, parent, text, value):
        def do_copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(value)
            self.root.update()
        return tk.Button(parent, text=text, command=do_copy, bg=BTN_BG,
                         fg=FG, activebackground=ACCENT, relief="flat",
                         font=("Segoe UI", 8), padx=6, cursor="hand2")

    # -- window management --

    def _place(self):
        self.win.update_idletasks()
        w = self.win.winfo_reqwidth()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        self.win.geometry(f"+{(sw - w) // 2}+{int(sh * 0.30)}")

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
            if hasattr(self, "entry") and self.entry.winfo_exists():
                self.entry.focus_set()
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
        self.hotkey, self.hk_mods, self.hk_vk = load_hotkey()

    def run(self):
        threading.Thread(target=self._hotkey_listener, daemon=True).start()
        self._start_tray()
        threading.Thread(target=warmup_ocr, daemon=True).start()
        log(f"flipper running — press {self.hotkey} over the Market Ratio "
            f"panel (rebind in flipper_config.json)")
        self.root.after(50, self._poll)
        self.root.mainloop()
        if self.icon:
            self.icon.stop()

    def _hotkey_listener(self):
        """Native hotkey via RegisterHotKey.  Unlike a keyboard hook (the
        `keyboard` library), this consumes only the exact combo and never
        delays or re-injects modifier events, so holding Alt in game stays
        perfectly responsive."""
        from ctypes import wintypes
        u = ctypes.windll.user32
        MOD_NOREPEAT = 0x4000
        WM_HOTKEY = 0x0312
        if not u.RegisterHotKey(None, 1, self.hk_mods | MOD_NOREPEAT,
                                self.hk_vk):
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

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if self.popup:
                    self.popup.close()
                    self.popup = None
                if kind == "show":
                    self.popup = Popup(self.root, payload)
                elif kind == "exit":
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
    geo, _cached = load_geometry(*screen_size())
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
        geo, _cached = load_geometry(*full.size)
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
