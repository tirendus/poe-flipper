"""Assisted selling at Faustus's currency exchange.

One call handles one item: put it into the "I Have" slot through the
picker's search, read the order book exactly the way flipper.py does, and
hand back flipper's own data so its pricing decides.  Nothing in here ever
presses Place Order.

Screen positions live in flipper's 1920x1080 reference space and go through
its Geometry, so they follow the panel wherever the game puts it (centred,
or pushed left when only the inventory is open).
"""
import ctypes
import difflib
import re
import time

from PIL import Image

import flipper

TARGET = "Chaos Orb"

# --- exchange panel, reference space -------------------------------------
SLOT_POS = {"want": (690, 240), "have": (1190, 240)}    # click = open picker
SLOT_BOX = {"want": (628, 214, 826, 266), "have": (1132, 214, 1332, 266)}
TABS_BOX = (600, 176, 1320, 218)        # the "I Want" / "I Have" tab labels
MARKET_POS = (960, 196)                 # the ratio under "Market Ratio"
MARKET_BOX = (868, 182, 1052, 212)      # ... and the box that holds it
PLACE_ORDER_POS = (958, 321)
PLACE_ORDER_BOX = (880, 304, 1036, 338)
COUNTER_BOX = (1268, 316, 1348, 348)    # "2/10": order slots in use

# --- item picker, reference space of the centred layout ------------------
# the picker is anchored on its own title ("I HAVE" / "I WANT"), found by
# OCR each time, so it does not matter where the game opens it
PICKER_TITLE = (959, 109)
PICKER_TITLE_BOX = (860, 88, 1060, 130)
PICKER_ALL = (543, 164)                 # the "All" category tab
PICKER_SEARCH = (1030, 947)
PICKER_CLEAR = (1168, 947)              # the x that empties the search box
PICKER_AREA = (745, 140, 1445, 968)     # result grid + search box below it
PICKER_SEARCH_Y = 928                   # cells below this are the search box
PICKER_OCR_SCALE = 0.75                 # item names are big: read them faster

VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN, VK_V = (
    0x10, 0x11, 0x12, 0x5B, 0x5C, 0x56)


class SellError(Exception):
    """A step failed; the message is meant for the user."""


# ------------------------------------------------------------------ text ---

_CONFUSABLE = str.maketrans({"l": "i", "1": "i", "0": "o"})


def norm(text):
    """Comparison form of a name as OCR may deliver it: case, spacing and
    punctuation dropped, and the glyphs OCR swaps (l/I/1, 0/O) folded."""
    return re.sub(r"[^a-z0-9]", "", text.lower()).translate(_CONFUSABLE)


def similarity(a, b):
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def same_name(seen, wanted):
    """Is the OCR'd `seen` the item `wanted`?  Exact after normalising, or
    off by a stray glyph on a name long enough for that to be meaningful."""
    a, b = norm(seen), norm(wanted)
    if not a or not b:
        return False
    return a == b or (len(b) >= 8 and similarity(seen, wanted) >= 0.93)


# ------------------------------------------------------------------- OCR ---

MIN_SIDE = 736      # RapidOCR's detector enlarges every image until its
                    # SHORT side is this long: a 300x70 crop became a
                    # 3400x736 job (~1 s while the game is running).  On a
                    # padded canvas the same crop costs ~0.2 s and reads
                    # cleaner too.
PAD_COLOUR = (20, 16, 12)


def _base_scale(img):
    """Small screens get the same 2x upscale flipper.calibrate applies."""
    return max(1.0, 2.0 * flipper.REF_H / img.height)


def _ocr(img):
    """PIL image -> [(text, x, cy, width)] in that image's pixels."""
    import numpy as np
    w, h = max(MIN_SIDE, img.width), max(MIN_SIDE, img.height)
    if (w, h) != img.size:
        canvas = Image.new("RGB", (w, h), PAD_COLOUR)
        canvas.paste(img, (0, 0))
        img = canvas
    # the angle classifier is skipped: game text is never upside down
    result, _ = flipper.get_ocr()(np.array(img), use_cls=False)
    cells = []
    for box, text, _score in result or []:
        xs = [p[0] for p in box]
        cells.append((text, min(xs), sum(p[1] for p in box) / 4,
                      max(xs) - min(xs)))
    return cells


def _clamp(img, box):
    x0, y0, x1, y1 = (int(v) for v in box)
    return (max(0, x0), max(0, y0), min(img.width, x1), min(img.height, y1))


def ocr_box(img, box, scale=1.0):
    """OCR one pixel box of a screenshot -> cells in screen pixels."""
    x0, y0, x1, y1 = _clamp(img, box)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return []
    f = _base_scale(img) * scale
    crop = img.crop((x0, y0, x1, y1))
    if f != 1.0:
        crop = crop.resize((max(1, int(crop.width * f)),
                            max(1, int(crop.height * f))), Image.LANCZOS)
    return [(text, x0 + x / f, y0 + cy / f, w / f)
            for text, x, cy, w in _ocr(crop)]


def ocr_boxes(img, boxes):
    """OCR several small boxes of one screenshot in a single pass by
    stacking them on one canvas.  {key: box} -> {key: cells in screen px}."""
    f = _base_scale(img)
    gap, y, parts = 28, 0, []
    for key, box in boxes.items():
        x0, y0, x1, y1 = _clamp(img, box)
        if x1 - x0 < 8 or y1 - y0 < 8:
            continue
        crop = img.crop((x0, y0, x1, y1))
        if f != 1.0:
            crop = crop.resize((int(crop.width * f), int(crop.height * f)),
                               Image.LANCZOS)
        parts.append((key, crop, x0, y0, y))
        y += crop.height + gap
    out = {key: [] for key in boxes}
    if not parts:
        return out
    canvas = Image.new("RGB", (max(p[1].width for p in parts), y), PAD_COLOUR)
    for _key, crop, _x0, _y0, top in parts:
        canvas.paste(crop, (0, top))
    for text, x, cy, w in _ocr(canvas):
        for key, crop, x0, y0, top in parts:
            if top <= cy < top + crop.height:
                out[key].append((text, x0 + x / f, y0 + (cy - top) / f, w / f))
                break
    return out


def _lines(cells):
    """OCR cells -> one string, top line first, left to right in a line."""
    out, last_y = [], None
    for c in sorted(cells, key=lambda c: c[2]):
        if last_y is None or c[2] - last_y > 8:
            out.append([])
        out[-1].append(c)
        last_y = c[2]
    return " ".join(c[0] for line in out
                    for c in sorted(line, key=lambda c: c[1])).strip()


def find_text(cells, wanted, scale=1.0):
    """Locate `wanted` among OCR cells even when the OCR cut it into pieces
    ("PLACE" + "ORDER").  -> (centre x, centre y) or None."""
    target = norm(wanted)
    lines, last_y = [], None
    for c in sorted(cells, key=lambda c: c[2]):
        if last_y is None or c[2] - last_y > 10 * scale:
            lines.append([])
        lines[-1].append(c)
        last_y = c[2]
    for line in lines:
        line.sort(key=lambda c: c[1])
        for i in range(len(line)):
            text = ""
            for j in range(i, min(i + 4, len(line))):
                if j > i and line[j][1] - (line[j - 1][1] + line[j - 1][3]) \
                        > 30 * scale:
                    break
                text += line[j][0]
                if norm(text) == target:
                    return ((line[i][1] + line[j][1] + line[j][3]) / 2,
                            sum(c[2] for c in line[i:j + 1]) / (j - i + 1))
    return None


def _slot_text(cells):
    text = _lines(cells)
    return "" if norm(text) == norm("Click to Select") else text


def _refined(img, geo, tab_cells):
    """Exact scale and offset from the two tab labels, the same anchors
    flipper.calibrate uses.  flipper's cached profiles are good enough for
    its own clicks next to the tabs, but the picker's search box sits ~840
    reference px below its title: there a 1% scale error is a missed click."""
    want = find_text(tab_cells, "I WANT", geo.s)
    have = find_text(tab_cells, "I HAVE", geo.s)
    if want is None or have is None:
        return geo
    s = (have[0] - want[0]) / (flipper.ANCHOR_HAVE[0] - flipper.ANCHOR_WANT[0])
    if not 0.8 < s / geo.s < 1.25:
        return geo
    dx = (have[0] + want[0]) / 2 - (flipper.REF_W / 2) * s
    dy = (want[1] + have[1]) / 2 - flipper.ANCHOR_WANT[1] * s
    return flipper.Geometry(img.width, img.height, s, dx, dy)


def _panel_boxes(geo):
    return {"tabs": geo.box(TABS_BOX), "want": geo.box(SLOT_BOX["want"]),
            "have": geo.box(SLOT_BOX["have"]),
            "place": geo.box(PLACE_ORDER_BOX),
            "counter": geo.box(COUNTER_BOX)}


def _counter(cells):
    for text, _x, _cy, _w in cells:
        m = re.fullmatch(r"\s*(\d{1,2})\s*/\s*(\d{1,2})\s*", text)
        if m and int(m.group(2)) > 0:
            return int(m.group(1)), int(m.group(2))
    return None


def read_panel(img):
    """Find the exchange panel and read its state in one OCR pass.
    -> {"geo", "want", "have", "orders"} or None when it is not on screen.
    `want` / `have` are the slot names ('' = nothing chosen), `orders` is
    (slots in use, slots) or None."""
    w, h = img.size
    profiles, _cached = flipper.load_geometries(w, h)
    for attempt in range(len(profiles) + 1):
        if attempt < len(profiles):
            geo = profiles[attempt]
        else:               # no cached position fits: look for the panel
            geo = flipper.calibrate(img)
            if geo is None:
                return None
        got = ocr_boxes(img, _panel_boxes(geo))
        if find_text(got["place"], "PLACE ORDER", geo.s) is None:
            continue
        return {"geo": _refined(img, geo, got["tabs"]),
                "want": _slot_text(got["want"]),
                "have": _slot_text(got["have"]),
                "orders": _counter(got["counter"])}
    return None


def read_slot(img, geo, side):
    """Name shown in the I Want / I Have slot ('' when nothing is chosen)."""
    return _slot_text(ocr_box(img, geo.box(SLOT_BOX[side])))


def locate_picker(img, geo, side):
    """Geometry of the open item picker, or None when it is not open.
    Looks where the panel's own geometry predicts the title first, then
    across the whole top of the screen."""
    title = "I WANT" if side == "want" else "I HAVE"
    f = geo.s
    x0, y0, x1, y1 = geo.box(PICKER_TITLE_BOX)
    for box in ((x0, y0, x1, y1), (0, y0, img.width, y1)):
        at = find_text(ocr_box(img, box), title, f)
        if at is not None:
            return flipper.Geometry(
                img.width, img.height, f,
                at[0] - PICKER_TITLE[0] * f, at[1] - PICKER_TITLE[1] * f)
    return None


def read_picker(img, picker):
    """One OCR pass over the open picker -> (text in the search box, result
    entries)."""
    cells = ocr_box(img, picker.box(PICKER_AREA), scale=PICKER_OCR_SCALE)
    split = picker.pt((0, PICKER_SEARCH_Y))[1]
    typed = _lines([c for c in cells if c[2] >= split])
    return typed, result_entries([c for c in cells if c[2] < split], picker.s)


def result_entries(cells, scale):
    """Group the OCR cells of the picker's result grid into entries
    [(text, cx, cy)]: pieces of one line the OCR cut apart are joined, the
    two lines of a wrapped name are glued together, and the owned-count
    numbers printed over the icons are dropped."""
    lines = []
    for c in sorted((c for c in cells if re.search(r"[A-Za-z]{2}", c[0])),
                    key=lambda c: c[2]):
        if lines and abs(c[2] - lines[-1][-1][2]) <= 6 * scale:
            lines[-1].append(c)
        else:
            lines.append([c])
    # same line, a word gap apart: one name ("Lesser Eldritch" + "Ember").
    # Neighbouring grid cells are 100+ px apart, far beyond a word gap.
    words = []
    for line in lines:
        joined = None
        for text, x, cy, w in sorted(line, key=lambda c: c[1]):
            if joined and 0 <= x - (joined[1] + joined[3]) <= 14 * scale:
                joined = (f"{joined[0]} {text}", joined[1], joined[2],
                          x + w - joined[1])
            else:
                if joined:
                    words.append(joined)
                joined = (text, x, cy, w)
        if joined:
            words.append(joined)
    words.sort(key=lambda c: (c[2], c[1]))
    used, out = set(), []
    for i, (text, x, cy, w) in enumerate(words):
        if i in used:
            continue
        right, bottom = x + w, cy
        for j in range(i + 1, len(words)):
            t2, x2, cy2, w2 = words[j]
            # the second line of a name sits ~21 reference px under the
            # first.  A section header (ALL CAPS) is ~28 above the first row
            # and, over a lone centred hit, starts at nearly the same x: it
            # must never be taken for the first line of that hit's name.
            if j not in used and not text.isupper() and not t2.isupper() \
                    and abs(x2 - x) <= 16 * scale and \
                    14 * scale <= cy2 - cy <= 26 * scale:
                used.add(j)
                text = f"{text} {t2}"
                right, bottom = max(right, x2 + w2), cy2
                break
        out.append((text, (x + right) / 2, (cy + bottom) / 2))
    return out


def search_took(typed, name):
    """Does the search box show (part of) `name`?  A long name scrolls the
    box, which then shows only its tail ("too of the Ramako Sniper"), and
    the OCR tends to glue the clear button on as a stray "X" — so this asks
    for a long shared run of characters, not for equality."""
    a, b = norm(typed), norm(name)
    if not a or not b:
        return False
    run = difflib.SequenceMatcher(None, a, b).find_longest_match(
        0, len(a), 0, len(b)).size
    return run >= min(len(b), 12)


def find_result(entries, name, lone_ok=True):
    """The entry to click for `name`, or None.  An exact match wins (the
    same name listed twice is the same item shown in two sections, e.g.
    "Popular" and "Currency": the upper one is taken).  With `lone_ok` — the
    search box is known to hold this name — a single hit is accepted when it
    is recognisably the same name, garbled by the OCR.  Anything ambiguous
    is refused; the slot is re-read after the click in any case."""
    exact = [e for e in entries if norm(e[0]) == norm(name)]
    if exact:
        return min(exact, key=lambda e: (e[2], e[1]))
    items = [e for e in entries if not e[0].isupper()]    # headers are CAPS
    close = [e for e in items if same_name(e[0], name)]
    if len(close) == 1:
        return close[0]
    if lone_ok and len(items) == 1 and similarity(items[0][0], name) >= 0.75:
        return items[0]
    return None


# ----------------------------------------------------------------- input ---

def _clipboard_api():
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    k.GlobalAlloc.restype = ctypes.c_void_p
    k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u.OpenClipboard.argtypes = [ctypes.c_void_p]
    u.GetClipboardData.restype = ctypes.c_void_p
    u.GetClipboardData.argtypes = [ctypes.c_uint]
    u.SetClipboardData.restype = ctypes.c_void_p
    u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    return u, k


def _open_clipboard(u):
    for _ in range(20):
        if u.OpenClipboard(None):
            return True
        time.sleep(0.03)
    return False


def get_clipboard():
    """Current clipboard text, or None (empty / not text / busy)."""
    u, k = _clipboard_api()
    if not _open_clipboard(u):
        return None
    try:
        handle = u.GetClipboardData(13)             # CF_UNICODETEXT
        if not handle:
            return None
        ptr = k.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            k.GlobalUnlock(handle)
    finally:
        u.CloseClipboard()


def set_clipboard(text):
    u, k = _clipboard_api()
    data = text.encode("utf-16-le") + b"\x00\x00"
    if not _open_clipboard(u):
        raise SellError("The clipboard is held by another program.")
    try:
        u.EmptyClipboard()
        handle = k.GlobalAlloc(0x0002, len(data))   # GMEM_MOVEABLE
        ptr = k.GlobalLock(handle)
        ctypes.memmove(ptr, data, len(data))
        k.GlobalUnlock(handle)
        u.SetClipboardData(13, handle)
    finally:
        u.CloseClipboard()


class Game:
    """The real game: Win32 input and screen capture.  Tests swap in a
    scripted stand-in with the same methods."""

    def __init__(self):
        self.u = ctypes.windll.user32

    def focused(self):
        """Is Path of Exile the foreground window?  Clicks must never land
        on whatever else the user happens to have in front."""
        hwnd = self.u.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(256)
        self.u.GetWindowTextW(hwnd, buf, 256)
        return "path of exile" in buf.value.lower()

    def capture(self):
        return flipper.capture_full()

    def sleep(self, seconds):
        time.sleep(seconds)

    def move(self, x, y):
        self.u.SetCursorPos(int(x), int(y))
        self.u.mouse_event(0x0001, 1, 0, 0, 0)      # nudge: wakes hover
        self.u.mouse_event(0x0001, -1, 0, 0, 0)

    def park(self):
        """Put the cursor where the game shows no tooltip: the bottom
        centre of the screen, where the helper's own bar sits."""
        self.move(self.u.GetSystemMetrics(0) // 2,
                  int(self.u.GetSystemMetrics(1) * 0.97))

    def click(self, x, y):
        self.u.SetCursorPos(int(x), int(y))
        time.sleep(0.06)
        self.u.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.02)
        self.u.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(0.10)

    def key_down(self, vk):
        self.u.keybd_event(vk, 0, 0, 0)

    def key_up(self, vk):
        self.u.keybd_event(vk, 0, 2, 0)

    def clipboard(self):
        return get_clipboard()

    def restore_clipboard(self, text):
        """Put the user's own clipboard text back (best effort)."""
        if text is None:
            return
        try:
            set_clipboard(text)
        except SellError:
            pass

    def paste(self, text):
        """Type `text` into the focused field through the clipboard: the
        only way that does not depend on the keyboard layout.  The caller
        restores the clipboard once the game has had time to read it."""
        set_clipboard(text)
        time.sleep(0.03)
        self.key_down(VK_CONTROL)
        time.sleep(0.02)
        self.key_down(VK_V)
        time.sleep(0.02)
        self.key_up(VK_V)
        time.sleep(0.02)
        self.key_up(VK_CONTROL)

    def wait_released(self, extra=(), timeout=3.0):
        """Wait until the user lets go of the hotkey: a held Alt would turn
        our Ctrl+V into Ctrl+Alt+V."""
        keys = (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN) + tuple(extra)
        end = time.time() + timeout
        while time.time() < end:
            if not any(self.u.GetAsyncKeyState(vk) & 0x8000 for vk in keys):
                return True
            time.sleep(0.02)
        return False


# ------------------------------------------------------------------ steps ---

def select_item(game, geo, side, name, verify=True):
    """Open the picker of `side`, search for `name` and click the exact
    hit.  With `verify` the slot is re-read afterwards; the caller can skip
    that when its next screenshot shows the slot anyway.  Raises SellError
    whenever a step does not look right."""
    game.click(*geo.pt(SLOT_POS[side]))
    # The picker opens right under the cursor, which then rests on one of
    # its items — and the item's tooltip covers the picker's title.  Move
    # to where the search box is about to be (the picker opens on the
    # panel's own geometry): nothing there shows a tooltip.
    game.move(*geo.pt(PICKER_SEARCH))
    picker = img = None
    for wait in (0.35, 0.5):
        game.sleep(wait)
        img = game.capture()
        picker = locate_picker(img, geo, side)
        if picker is not None:
            break
    if picker is None:
        dump(img, "no_picker")
        raise SellError("The item picker did not open.")

    game.click(*picker.pt(PICKER_ALL))
    game.click(*picker.pt(PICKER_CLEAR))
    game.click(*picker.pt(PICKER_SEARCH))
    saved = game.clipboard()
    try:
        game.paste(name)
        # Whether the paste landed is not judged from the search box: a
        # long name scrolls it, so the box rarely shows the whole name.
        # What counts is that the list holds the item itself — an exact
        # name is right whatever the box says — and that the slot shows it
        # afterwards.  The list can lag behind the paste by a moment, so a
        # miss is looked at again before it is believed.
        typed, entries, hit = "", [], None
        for wait in (0.4, 0.5, 0.8):
            game.sleep(wait)
            img = game.capture()
            typed, entries = read_picker(img, picker)
            hit = find_result(entries, name,
                              lone_ok=search_took(typed, name))
            if hit is not None:
                break
    finally:
        game.restore_clipboard(saved)
    if hit is None:
        dump(img, "not_found",
             f"wanted: {name}\nsearch box: {typed}\nentries:\n"
             + "\n".join(f"  {e[0]!r} at ({e[1]:.0f}, {e[2]:.0f})"
                         for e in entries))
        raise SellError(f"[{name}] is not in the exchange list "
                        f"(search box: [{typed or 'empty'}]).")
    game.click(hit[1], hit[2])
    game.sleep(0.35)
    if verify:
        check_slot(game.capture(), geo, side, name)


_NUMBER = r"\d{1,3}(?:\s\d{3})+|[\d.,]+"


def parse_market_ratio(text):
    """The ratio printed under "Market Ratio" -> price in want-per-have, or
    None.  '3.21 : 1', '1: 1.20', '5 388 : 1'.  Unlike a table row this line
    holds nothing but the ratio, so a space inside a number can only be a
    thousands gap."""
    m = re.fullmatch(rf"\s*({_NUMBER})\s*[:;|：]\s*({_NUMBER})\s*", text)
    if not m:
        return None
    try:
        a = flipper.parse_ratio_num(re.sub(r"\s", "", m.group(1)))
        b = flipper.parse_ratio_num(re.sub(r"\s", "", m.group(2)))
    except ValueError:
        return None
    return a / b if a > 0 and b > 0 else None


def plausible(price, market, factor=2.5):
    """Is a listing price in the same league as the market ratio the game
    itself prints?  A table row that lost a digit to the OCR ("5 388" read
    as "388") is many times off; real books never are.  With no market
    ratio to compare against there is nothing to object to."""
    if not market or not price:
        return True
    return market / factor <= price <= market * factor


DUMP_KEEP = 20      # newest failed-step screenshots kept in debug\


def dump(img, reason, text=""):
    """Keep what the helper was looking at when a step failed, as
    debug\\sell_<time>_<reason>.jpg plus a .txt with what it read there.
    Without it a failure can only be guessed at from a later screenshot,
    which shows the screen after the moment has passed."""
    if img is None or not flipper.DEBUG_SAVE:
        return
    try:
        folder = flipper.APP_DIR / "debug"
        folder.mkdir(exist_ok=True)
        base = folder / f"sell_{time.strftime('%Y%m%d_%H%M%S')}_{reason}"
        img.save(base.with_suffix(".jpg"), quality=85)
        base.with_suffix(".txt").write_text(text, encoding="utf-8")
        for old in sorted(folder.glob("sell_*.jpg"))[:-DUMP_KEEP]:
            old.unlink()
            old.with_suffix(".txt").unlink(missing_ok=True)
    except OSError:
        pass


def check_slot(img, geo, side, name):
    shown = read_slot(img, geo, side)
    if not same_name(shown, name):
        dump(img, "wrong_slot", f"wanted: {name}\nslot shows: {shown}")
        raise SellError(f"The slot shows [{shown or 'nothing'}] instead of [{name}].")


TOOLTIP_POLLS = 6   # frames looked at per Alt hold; each costs one small
                    # OCR pass (~0.2-0.3 s), so ~2 s at the very most


def tooltip_up(img, geo):
    """Is the Market Ratio tooltip on this frame?  Its section titles
    ("Available Trades", "Competing Trades") and "Stock" column appear
    nowhere else in the tables box."""
    text = norm(" ".join(c[0] for c in ocr_box(img, geo.tbox(flipper.TABLES_BOX))))
    return norm("Trades") in text or norm("Stock") in text


def read_market(game, geo, have=None):
    """Hover the market ratio with Alt held and read the tables through
    flipper's own pipeline.  Returns flipper's data dict, plus
    "market_ref": the market ratio to sanity-check prices against —
    flipper's own reading of it, or ours when flipper could not parse it
    (it does not read numbers of 1000 and up).  With `have` the I Have slot
    is checked on the same screenshot first (the tooltip leaves both slots
    visible)."""
    img = None
    for _attempt in range(2):
        game.move(*geo.pt(MARKET_POS))
        game.key_down(VK_MENU)
        try:
            # the tooltip takes anything from 0.2 s to over half a second to
            # come up.  A frame taken too early has no tables, and flipper
            # answers that with a recalibration (~3 s) — so look for the
            # tooltip with one cheap OCR pass and only hand flipper a frame
            # that has it (or the last one, when it never showed)
            game.sleep(0.15)
            for poll in range(TOOLTIP_POLLS):
                img = game.capture()
                if poll == TOOLTIP_POLLS - 1 or tooltip_up(img, geo):
                    break
                game.sleep(0.08)
        finally:
            game.key_up(VK_MENU)
        if have is not None:
            check_slot(img, geo, "have", have)
        data = flipper.read_screen(img)
        if data["available"] or data["competing"]:
            data["market_ref"] = data.get("market") or parse_market_ratio(
                _lines(ocr_box(img, geo.box(MARKET_BOX))))
            return data
        game.sleep(0.2)
    dump(img, "no_tables")
    raise SellError("Could not read the Market Ratio tables (flipper does not "
                    "read ratios of 1000 and up yet).")


def prepare_item(game, name):
    """Everything up to the pricing decision for one item.
    -> {"data": flipper data, "geo": panel geometry, "orders": (used, total)
    or None}."""
    if not game.focused():
        raise SellError("The game is not in front - switch to Path of Exile.")
    game.wait_released()
    img = game.capture()
    panel = read_panel(img)
    if panel is None:
        # a tooltip under the cursor may be covering the panel: look again
        # with the cursor out of the way
        game.park()
        game.sleep(0.3)
        img = game.capture()
        panel = read_panel(img)
    if panel is None:
        dump(img, "no_panel")
        raise SellError("Cannot see the currency exchange - open it.")
    geo, orders = panel["geo"], panel["orders"]
    if orders and orders[0] >= orders[1]:
        raise SellError(f"All {orders[1]} order slots are taken - collect the "
                        "completed ones.")
    if not same_name(panel["want"], TARGET):
        select_item(game, geo, "want", TARGET)
    if not same_name(panel["have"], name):
        select_item(game, geo, "have", name, verify=False)
    return {"data": read_market(game, geo, have=name), "geo": geo,
            "orders": orders}


def choose_row(sugg, strategy="Undercut all"):
    """flipper's suggestion row for `strategy` — the plain one, not the
    "finer pricing" variant — or None when flipper offered no such row."""
    for label, row in sugg.get("rows", []):
        if not row.get("fine") and label.startswith(strategy):
            return row
    return None


# ------------------------------------------------------------------ queue ---

class SellQueue:
    """What to sell this session, most valuable first."""

    def __init__(self, rows, min_total=0.0):
        chosen = [r for r in rows
                  if r["sell"] and not r["fixed"] and r["total"] >= min_total]
        chosen.sort(key=lambda r: (-r["total"], r["name"]))
        self.items = [{"name": r["name"], "qty": r["qty"],
                       "total": r["total"], "status": "wait"} for r in chosen]

    def current(self):
        return next((i for i in self.items if i["status"] == "wait"), None)

    def mark(self, status):
        item = self.current()
        if item is not None:
            item["status"] = status
        return item

    def count(self, status):
        return sum(1 for i in self.items if i["status"] == status)

    def position(self):
        """(1-based number of the current item, total)."""
        done = sum(1 for i in self.items if i["status"] != "wait")
        return min(done + 1, len(self.items)), len(self.items)
