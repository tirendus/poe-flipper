"""Selling helper: screen reading on real 4K captures of the Faustus window
and the whole one-item flow driven through a scripted stand-in for the game
(no input is sent, no game needed).  Slower than test_stash: real OCR."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

import flipper
import seller

FIX = Path(__file__).resolve().parent / "fixtures" / "faustus"
SCREEN = (3840, 2160)
_cache = {}


def shot(label):
    """A fixture pasted back onto a black 4K screen at its own position."""
    if label not in _cache:
        path = next(FIX.glob(label + "@*.jpg"))
        x, y = (int(v) for v in path.stem.split("@")[1].split(","))
        canvas = Image.new("RGB", SCREEN, (0, 0, 0))
        canvas.paste(Image.open(path).convert("RGB"), (x, y))
        _cache[label] = canvas
    return _cache[label]


class FakeGame:
    """Plays back screenshots and records what the helper would have done."""

    def __init__(self, frames, focused=True):
        self.frames = list(frames)
        self.events = []
        self._focused = focused

    def focused(self):
        return self._focused

    def wait_released(self, extra=(), timeout=3.0):
        return True

    def capture(self):
        self.events.append(("capture", self.frames[0]))
        return shot(self.frames.pop(0))

    def sleep(self, seconds):
        pass

    def move(self, x, y):
        self.events.append(("move", round(x), round(y)))

    def park(self):
        self.events.append(("park",))

    def click(self, x, y):
        self.events.append(("click", round(x), round(y)))

    def key_down(self, vk):
        self.events.append(("down", vk))

    def key_up(self, vk):
        self.events.append(("up", vk))

    def clipboard(self):
        return "user's own clipboard"

    def restore_clipboard(self, text):
        self.events.append(("restore", text))

    def paste(self, text):
        self.events.append(("paste", text))

    def of(self, kind):
        return [e for e in self.events if e[0] == kind]


class SellerBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        flipper.DEBUG_SAVE = False
        cls._tmp = tempfile.TemporaryDirectory()
        cls._calib = flipper.CALIB_FILE
        # no cached panel positions: the live calibration file stays untouched
        flipper.CALIB_FILE = Path(cls._tmp.name) / "calib.json"

    @classmethod
    def tearDownClass(cls):
        flipper.CALIB_FILE = cls._calib
        cls._tmp.cleanup()


class TestNames(unittest.TestCase):
    def test_norm_folds_what_ocr_confuses(self):
        self.assertEqual(seller.norm("Lesser Eldritch lchor"),
                         seller.norm("Lesser Eldritch Ichor"))
        self.assertEqual(seller.norm("PLACEORDER"), seller.norm("Place Order"))
        self.assertEqual(seller.norm("Rogue’s Marker"),
                         seller.norm("Rogue's Marker"))

    def test_same_name_keeps_neighbours_apart(self):
        ok = seller.same_name
        self.assertTrue(ok("Greater E Eldritch Ichor", "Greater Eldritch Ichor"))
        self.assertTrue(ok("stacked deck", "Stacked Deck"))
        for seen, wanted in (
                ("Greater Eldritch Ember", "Greater Eldritch Ichor"),
                ("Lesser Eldritch Ichor", "Greater Eldritch Ichor"),
                ("Grand Eldritch Ichor", "Greater Eldritch Ichor"),
                ("Eldritch Chaos Orb", "Chaos Orb"),
                ("Veiled Chaos Orb", "Chaos Orb"),
                ("Lucky Deck", "Stacked Deck"),
                ("Essence of Fear", "Essence of Zeal"),
                ("Shrieking Essence of Scorn", "Screaming Essence of Scorn"),
                ("stacked", "Stacked Deck"), ("", "Chaos Orb")):
            self.assertFalse(ok(seen, wanted), (seen, wanted))

    def test_find_result_refuses_ambiguity(self):
        entries = [("CURRENCY", 10, 10), ("Chaos Orb", 100, 50),
                   ("Veiled Chaos Orb", 300, 50), ("Eldritch Chaos Orb", 500, 50)]
        self.assertEqual(seller.find_result(entries, "Chaos Orb")[1:], (100, 50))
        self.assertIsNone(seller.find_result(entries, "Tainted Chaos Orb"))
        # listed twice = the same item in two sections: take the upper one
        twice = [("Chaos Orb", 100, 200)] + entries
        self.assertEqual(seller.find_result(twice, "Chaos Orb")[1:], (100, 50))
        # one garbled hit for a full-name search is still that item ...
        lone = [("CURRENCY", 10, 10), ("Stackcd Dcck", 100, 50)]
        self.assertEqual(seller.find_result(lone, "Stacked Deck")[1:], (100, 50))
        # ... but only while the search box is known to hold that name
        self.assertIsNone(seller.find_result(lone, "Stacked Deck",
                                             lone_ok=False))
        self.assertIsNone(seller.find_result(
            [("Lucky Deck", 100, 50)], "Stacked Deck"))

    def test_search_box_showing_only_the_tail_of_a_long_name(self):
        # live case: the box scrolled ("Tat" out of view) and the OCR glued
        # the clear button on as "X" — the paste had worked all along
        took = seller.search_took
        self.assertTrue(took("too of the Ramako Sniper X",
                             "Tattoo of the Ramako Sniper"))
        self.assertTrue(took("Stacked Deck", "Stacked Deck"))
        self.assertTrue(took("Chaos Orb X", "Chaos Orb"))
        for typed in ("Type keywords here...", "", "Lucky Deck"):
            self.assertFalse(took(typed, "Stacked Deck"), typed)
        # the section header sits right above the lone hit and starts at
        # almost the same x: it once got glued to the name's first line
        cells = [("DEXTERITY TATTOOS", 1317, 322, 600),
                 ("Tattoo of the", 1338, 378, 204), ("1", 1260, 372, 10),
                 ("Ramako Sniper", 1340, 418, 232)]
        entries = seller.result_entries(cells, 1.97)
        self.assertEqual([e[0] for e in entries],
                         ["DEXTERITY TATTOOS", "Tattoo of the Ramako Sniper"])
        hit = seller.find_result(entries, "Tattoo of the Ramako Sniper",
                                 lone_ok=False)
        self.assertEqual(hit[0], "Tattoo of the Ramako Sniper")
        self.assertAlmostEqual(hit[2], 398, delta=1)    # between both lines

    def test_name_cut_in_two_on_one_line_is_joined(self):
        cells = [("ELDRITCH CURRENCY", 1301, 323, 340),
                 ("Lesser Eldritch", 1337, 397, 232), ("Ember", 1582, 398, 87),
                 ("59", 1262, 372, 22),
                 # neighbouring grid cells on one row stay three items
                 ("Orb of Alteration", 1556, 586, 267),
                 ("Chromatic Orb", 2004, 586, 231),
                 ("Jeweller's Orb", 2450, 586, 217)]
        entries = seller.result_entries(cells, 1.97)
        self.assertEqual([e[0] for e in entries],
                         ["ELDRITCH CURRENCY", "Lesser Eldritch Ember",
                          "Orb of Alteration", "Chromatic Orb",
                          "Jeweller's Orb"])
        hit = seller.find_result(entries, "Lesser Eldritch Ember",
                                 lone_ok=False)
        self.assertAlmostEqual(hit[1], (1337 + 1582 + 87) / 2, delta=1)

    def test_failed_step_keeps_what_it_saw(self):
        with tempfile.TemporaryDirectory() as td:
            old = flipper.APP_DIR, flipper.DEBUG_SAVE, seller.DUMP_KEEP
            flipper.APP_DIR, flipper.DEBUG_SAVE, seller.DUMP_KEEP = \
                Path(td), True, 2
            orig_time = seller.time.strftime
            stamps = iter(["20261119_100001", "20261119_100002",
                           "20261119_100003"])
            seller.time.strftime = lambda fmt: next(stamps)
            try:
                frame = Image.new("RGB", (64, 64), (10, 10, 10))
                for reason in ("no_picker", "not_found", "no_tables"):
                    seller.dump(frame, reason, "what was read")
                flipper.DEBUG_SAVE = False
                seller.dump(frame, "ignored")       # switched off: no file
                seller.dump(None, "nothing")        # no frame: no crash
            finally:
                seller.time.strftime = orig_time
                flipper.APP_DIR, flipper.DEBUG_SAVE, seller.DUMP_KEEP = old
            names = sorted(p.name for p in (Path(td) / "debug").iterdir())
            self.assertEqual(names, [
                "sell_20261119_100002_not_found.jpg",
                "sell_20261119_100002_not_found.txt",
                "sell_20261119_100003_no_tables.jpg",
                "sell_20261119_100003_no_tables.txt"])      # newest two kept

    def test_wrapped_names_are_glued(self):
        cells = [("Orb of", 1553, 470, 111), ("Transmutation", 1557, 512, 230),
                 ("Orb of Augmentation", 2004, 493, 330), ("133", 1484, 472, 24),
                 ("Orb of Alteration", 1556, 586, 267)]
        got = [e[0] for e in seller.result_entries(cells, 1.98)]
        self.assertEqual(got, ["Orb of Transmutation", "Orb of Augmentation",
                               "Orb of Alteration"])


class TestMarketRatio(unittest.TestCase):
    def test_parse(self):
        p = seller.parse_market_ratio
        self.assertEqual(p("3.21 : 1"), 3.21)
        self.assertEqual(p("3.21 :1"), 3.21)
        self.assertAlmostEqual(p("1: 1.20"), 1 / 1.2)
        self.assertEqual(p("5 388 : 1"), 5388.0)         # thousands gap
        self.assertEqual(p("5 388:1"), 5388.0)
        self.assertEqual(p("1 : 12 500"), 1 / 12500)
        self.assertEqual(p("5,388 : 1"), 5388.0)
        for junk in ("Select currency...", "", "3.21", "a : b", "0 : 1"):
            self.assertIsNone(p(junk), junk)

    def test_plausible(self):
        ok = seller.plausible
        self.assertTrue(ok(3.2, 3.21))
        self.assertTrue(ok(5200, 5388))
        self.assertFalse(ok(388, 5388))         # a digit lost to the OCR
        self.assertFalse(ok(53880, 5388))
        self.assertTrue(ok(7.0, 3.21))          # a wide but real spread
        self.assertFalse(ok(9.0, 3.21))
        self.assertTrue(ok(3.2, None))          # nothing to compare against


class TestPanelPlacement(unittest.TestCase):
    """The session panel once sat on the picker's search box: the helper
    read its own text there and reported that the paste had failed."""

    def test_bar_keeps_clear_of_everything_read_or_clicked(self):
        import sell_ui
        # panel geometries measured on the captures: centred, and pushed
        # left by the inventory; the picker follows the panel in both
        for dx in (28.3, -640.3):
            picker = flipper.Geometry(3840, 2160, 1.9713, dx, 7.0)
            lowest = max(picker.box(seller.PICKER_AREA)[3],
                         picker.pt(seller.PICKER_SEARCH)[1],
                         picker.pt(seller.PICKER_CLEAR)[1])
            for wanted in (60, 150, 400, 2000):     # however tall it wants
                x, y, w, h = sell_ui.bar_rect(3840, 2160, wanted)
                self.assertGreater(y, lowest + 10, (dx, wanted))
                self.assertLessEqual(y + h, 2160)
                self.assertTrue(0.29 * 3840 <= x and x + w <= 0.71 * 3840)

    def test_same_on_a_1080p_screen(self):
        import sell_ui
        picker = flipper.Geometry(1920, 1080, 1.0, 0.0, 0.0)
        x, y, w, h = sell_ui.bar_rect(1920, 1080, 300)
        self.assertGreater(y, picker.box(seller.PICKER_AREA)[3])

    def test_bar_widens_for_large_text_but_not_without_limit(self):
        import sell_ui
        # 100% text: 40% of the width is plenty
        self.assertEqual(sell_ui.bar_rect(1920, 1080, 70, 760)[2], 768)
        # 150% text needs more than that; the bar grows, stays centred ...
        x, y, w, h = sell_ui.bar_rect(1920, 1080, 100, 1180)
        self.assertEqual((w, x), (1180, 370))
        # ... and stops at 70% of the screen
        self.assertEqual(sell_ui.bar_rect(1920, 1080, 100, 5000)[2], 1344)
        # the height limit does not depend on the width
        self.assertGreaterEqual(sell_ui.bar_rect(1920, 1080, 999, 5000)[1], 972)


class TestQueue(unittest.TestCase):
    ROWS = [
        {"name": "Chaos Orb", "qty": 105, "total": 105.0, "sell": False, "fixed": True},
        {"name": "Divine Orb", "qty": 108, "total": 23166.0, "sell": False, "fixed": False},
        {"name": "Ritual Thread", "qty": 90, "total": 116.1, "sell": True, "fixed": False},
        {"name": "Ultimatum Thread", "qty": 70, "total": 172.2, "sell": True, "fixed": False},
        {"name": "Portal Scroll", "qty": 381, "total": 0.8, "sell": True, "fixed": False},
    ]

    def test_ticked_rows_most_valuable_first(self):
        q = seller.SellQueue(self.ROWS, min_total=1.0)
        self.assertEqual([i["name"] for i in q.items],
                         ["Ultimatum Thread", "Ritual Thread"])
        self.assertEqual(q.current()["qty"], 70)
        self.assertEqual(q.position(), (1, 2))
        q.mark("done")
        self.assertEqual(q.current()["name"], "Ritual Thread")
        self.assertEqual(q.position(), (2, 2))
        q.mark("skipped")
        self.assertIsNone(q.current())
        self.assertEqual((q.count("done"), q.count("skipped")), (1, 1))

    def test_choose_row_takes_the_plain_undercut(self):
        sugg = {"rows": [("Undercut all", {"w": 16, "d": 5, "fine": False}),
                         ("2nd in line (129 ahead)", {"w": 33, "d": 10, "fine": False}),
                         ("Undercut all", {"w": 323, "d": 100, "fine": True})]}
        self.assertEqual(seller.choose_row(sugg)["w"], 16)
        self.assertEqual(seller.choose_row(sugg, "2nd in line")["w"], 33)
        dead = {"rows": [("Step past offers", {"w": 3, "d": 2, "fine": False})]}
        self.assertIsNone(seller.choose_row(dead))


class TestPanel(SellerBase):
    def test_empty_panel(self):
        p = seller.read_panel(shot("panel_empty"))
        self.assertEqual((p["want"], p["have"], p["orders"]), ("", "", (0, 10)))
        self.assertAlmostEqual(p["geo"].s, 1.975, delta=0.02)

    def test_panel_pushed_left_by_the_inventory(self):
        p = seller.read_panel(shot("panel_empty_left"))
        self.assertEqual((p["want"], p["have"], p["orders"]), ("", "", (0, 10)))
        centred = seller.read_panel(shot("panel_empty"))["geo"]
        self.assertAlmostEqual(centred.dx - p["geo"].dx, 668, delta=12)
        # same reference point, ~668 px further left on screen
        self.assertAlmostEqual(
            centred.pt(seller.PLACE_ORDER_POS)[0]
            - p["geo"].pt(seller.PLACE_ORDER_POS)[0], 666, delta=12)

    def test_selected_pair_and_two_line_name(self):
        p = seller.read_panel(shot("panel_chaos_stacked_deck"))
        self.assertEqual((p["want"], p["have"]), ("Chaos Orb", "Stacked Deck"))
        p = seller.read_panel(shot("panel_chaos_ichor"))
        self.assertTrue(seller.same_name(p["want"], "Chaos Orb"))
        self.assertTrue(seller.same_name(p["have"], "Greater Eldritch Ichor"))

    def test_order_counter(self):
        self.assertEqual(seller.read_panel(shot("panel_one_order"))["orders"],
                         (1, 10))
        self.assertEqual(seller.read_panel(shot("panel_two_orders"))["orders"],
                         (2, 10))

    def test_no_panel_when_the_picker_covers_it(self):
        self.assertIsNone(seller.read_panel(shot("picker_have_all")))


class TestPicker(SellerBase):
    def setUp(self):
        self.geo = seller.read_panel(shot("panel_chaos_ichor"))["geo"]

    def test_picker_found_only_when_open(self):
        self.assertIsNotNone(
            seller.locate_picker(shot("picker_have_all"), self.geo, "have"))
        self.assertIsNotNone(
            seller.locate_picker(shot("picker_want_all"), self.geo, "want"))
        self.assertIsNone(
            seller.locate_picker(shot("picker_have_all"), self.geo, "want"))
        self.assertIsNone(
            seller.locate_picker(shot("panel_empty"), self.geo, "have"))

    def test_click_points_land_on_the_controls(self):
        picker = seller.locate_picker(shot("picker_have_all"), self.geo, "have")
        # measured on the capture: the hint text is at y=1881, x 1815..2147,
        # the clear button around x=2334, the "All" tab label at (1062..1132, 330)
        x, y = picker.pt(seller.PICKER_SEARCH)
        self.assertTrue(1830 <= x <= 2290 and abs(y - 1881) <= 14, (x, y))
        x, y = picker.pt(seller.PICKER_CLEAR)
        self.assertTrue(abs(x - 2334) <= 14 and abs(y - 1881) <= 14, (x, y))
        x, y = picker.pt(seller.PICKER_ALL)
        self.assertTrue(1040 <= x <= 1180 and abs(y - 330) <= 14, (x, y))

    def test_full_list_entries(self):
        picker = seller.locate_picker(shot("picker_have_all"), self.geo, "have")
        typed, entries = seller.read_picker(shot("picker_have_all"), picker)
        self.assertFalse(seller.same_name(typed, "Chaos Orb"))  # just the hint
        for name, (x0, x1, y) in {
                "Chaos Orb": (2440, 2620, 680),
                "Stacked Deck": (2440, 2660, 1058),
                "Divine Orb": (1995, 2180, 1152),
                "Orb of Transmutation": (1545, 1790, 491),
                "Voidborn Reliquary Key": (1545, 1860, 1249)}.items():
            hit = seller.find_result(entries, name)
            self.assertIsNotNone(hit, name)
            self.assertTrue(x0 <= hit[1] <= x1 and abs(hit[2] - y) <= 14,
                            (name, hit))
        # not on screen / cut off by the bottom edge -> never guessed
        self.assertIsNone(seller.find_result(entries, "Greater Eldritch Ichor"))
        self.assertIsNone(seller.find_result(entries, "Greater Eldritch Ember"))

    def test_want_picker_lists_chaos_twice(self):
        # "Popular" repeats Chaos Orb above the full list: either is fine,
        # the upper one (y=396 on the capture, middle column) is clicked
        picker = seller.locate_picker(shot("picker_want_all"), self.geo, "want")
        _typed, entries = seller.read_picker(shot("picker_want_all"), picker)
        exact = [e for e in entries if seller.norm(e[0]) == seller.norm("Chaos Orb")]
        self.assertEqual(len(exact), 2)
        hit = seller.find_result(entries, "Chaos Orb")
        self.assertTrue(1995 <= hit[1] <= 2180 and abs(hit[2] - 396) <= 14, hit)

    def test_flipper_still_offers_what_the_helper_builds_on(self):
        # everything below is flipper.py's; an upstream update that renames
        # any of it must fail here, not in the middle of a selling session
        for name in ("capture_full", "read_screen", "build_suggestions",
                     "auto_fill", "load_geometries", "calibrate", "Geometry",
                     "get_ocr", "apply_config", "parse_hotkey", "warmup_ocr",
                     "REF_W", "REF_H", "ANCHOR_WANT", "ANCHOR_HAVE", "Popup"):
            self.assertTrue(hasattr(flipper, name), name)
        for name in ("_build_input_stage", "_entry_key", "_on_submit",
                     "_build_result_stage", "_fill_btn", "_place",
                     "_force_focus", "_refresh_status", "close"):
            self.assertTrue(hasattr(flipper.Popup, name), name)


_fhd_cache = {}


def fhd(label):
    """The same capture the way a 1920x1080 screen shows it."""
    if label not in _fhd_cache:
        _fhd_cache[label] = shot(label).resize((1920, 1080), Image.LANCZOS)
    return _fhd_cache[label]


class FakeGameFullHD(FakeGame):
    def capture(self):
        self.events.append(("capture", self.frames[0]))
        return fhd(self.frames.pop(0))


class TestFullHD(SellerBase):
    """Every fixture is a 4K capture; the helper has to work just the same
    on a Full HD screen, where all text is half the size."""

    def test_panel_in_both_positions(self):
        p = seller.read_panel(fhd("panel_two_orders"))
        self.assertEqual((p["want"], p["orders"]), ("Chaos Orb", (2, 10)))
        self.assertTrue(seller.same_name(p["have"], "Greater Eldritch Ichor"))
        self.assertAlmostEqual(p["geo"].s, 0.988, delta=0.012)
        left = seller.read_panel(fhd("panel_empty_left"))
        self.assertEqual((left["want"], left["have"], left["orders"]),
                         ("", "", (0, 10)))
        self.assertAlmostEqual(p["geo"].dx - left["geo"].dx, 334, delta=8)

    def test_whole_item_at_half_the_resolution(self):
        game = FakeGameFullHD(TestFlow.FRAMES)
        got = seller.prepare_item(game, "Stacked Deck")
        self.assertEqual(game.frames, [])
        slot, tab_all, clear, search, hit = game.of("click")
        # the 4K click points, halved
        self.assertTrue(1135 <= slot[1] <= 1250 and abs(slot[2] - 240) <= 6)
        self.assertTrue(915 <= search[1] <= 1145 and abs(search[2] - 940) <= 8)
        self.assertTrue(abs(clear[1] - 1167) <= 8 and abs(clear[2] - 940) <= 8)
        self.assertTrue(1000 <= hit[1] <= 1108 and abs(hit[2] - 198) <= 8, hit)
        data = got["data"]
        self.assertEqual(data["market_ref"], 3.21)
        self.assertEqual([(e["a"], e["stock"]) for e in data["competing"]],
                         [(3.24, 129), (3.32, 34), (3.33, 40), (3.33, 201),
                          (3.33, 76), (3.33, 30503)])
        self.assertEqual(data["unparsed"], 0)
        row = seller.choose_row(flipper.build_suggestions(data, 100))
        self.assertEqual((row["used"], row["want_total"]), (100, 320))


class TestFlow(SellerBase):
    """prepare_item on a scripted game: Chaos Orb / Greater Eldritch Ichor is
    on the panel, Stacked Deck is next in the queue."""

    FRAMES = ["panel_chaos_ichor", "picker_have_all",
              "picker_have_search_stacked_deck", "tooltip_stacked_deck"]

    def test_selects_reads_and_prices(self):
        game = FakeGame(self.FRAMES)
        got = seller.prepare_item(game, "Stacked Deck")
        self.assertEqual(game.frames, [])                   # every frame used
        self.assertEqual(got["orders"], (0, 10))

        clicks = game.of("click")
        self.assertEqual(len(clicks), 5)
        slot, tab_all, clear, search, hit = clicks
        self.assertTrue(2270 <= slot[1] <= 2500 and abs(slot[2] - 480) <= 12)
        self.assertTrue(abs(search[2] - 1881) <= 14 and abs(clear[2] - 1881) <= 14)
        # the lone "Stacked Deck" hit, not "Lucky Deck" below it
        self.assertTrue(2000 <= hit[1] <= 2215 and abs(hit[2] - 397) <= 14, hit)
        self.assertEqual(game.of("paste"), [("paste", "Stacked Deck")])
        self.assertEqual(game.of("restore"),
                         [("restore", "user's own clipboard")])

        kinds = [e[0] if e[0] != "capture" else e[1] for e in game.events]
        # right after the slot click the cursor leaves the item grid (an
        # item tooltip there hides the picker's title) for the search box
        first_move = game.of("move")[0]
        self.assertTrue(1830 <= first_move[1] <= 2290
                        and abs(first_move[2] - 1881) <= 14, first_move)
        self.assertTrue(kinds.index("click") < kinds.index("move")
                        < kinds.index("picker_have_all"))

        # Alt goes down before the tooltip capture and always comes back up
        down, up = kinds.index("down"), kinds.index("up")
        self.assertTrue(down < kinds.index("tooltip_stacked_deck") < up)
        self.assertEqual(game.of("down"), [("down", seller.VK_MENU)])
        self.assertEqual(game.of("up"), [("up", seller.VK_MENU)])

        data = got["data"]
        self.assertEqual(data["market"], 3.21)
        self.assertEqual(data["market_ref"], 3.21)
        # our own reading of the same line, used when flipper has none
        own = seller.parse_market_ratio(seller._lines(seller.ocr_box(
            shot("tooltip_stacked_deck"), got["geo"].box(seller.MARKET_BOX))))
        self.assertEqual(own, 3.21)
        own = seller.parse_market_ratio(seller._lines(seller.ocr_box(
            shot("panel_chaos_ichor"), got["geo"].box(seller.MARKET_BOX))))
        self.assertAlmostEqual(own, 1 / 1.2)
        self.assertEqual([(e["a"], e["stock"]) for e in data["competing"]],
                         [(3.24, 129), (3.32, 34), (3.33, 40), (3.33, 201),
                          (3.33, 76), (3.33, 30503)])
        self.assertEqual(len(data["available"]), 6)
        self.assertEqual(data["unparsed"], 0)

        # flipper's own pricing decides: just under the 3.24 seller
        row = seller.choose_row(flipper.build_suggestions(data, 100))
        self.assertEqual((row["used"], row["want_total"], row["left"]),
                         (100, 320, 0))
        row = seller.choose_row(flipper.build_suggestions(data, 37))
        self.assertEqual((row["used"], row["want_total"], row["left"]),
                         (35, 112, 2))

    def test_already_selected_item_is_not_picked_again(self):
        game = FakeGame(["panel_chaos_stacked_deck", "tooltip_stacked_deck"])
        seller.prepare_item(game, "Stacked Deck")
        self.assertEqual(game.of("click"), [])
        self.assertEqual(game.of("paste"), [])

    def test_waits_for_a_slow_tooltip_without_recalibrating(self):
        # live on a 1080p screen the tooltip was not up yet on the first
        # frame; flipper got that frame, found no tables and spent ~3 s
        # recalibrating before the retry.  Early frames are now skipped.
        game = FakeGame(["panel_chaos_stacked_deck", "panel_chaos_stacked_deck",
                         "panel_chaos_stacked_deck", "tooltip_stacked_deck"])
        seen, orig = [], flipper.read_screen

        def spy(img, *a, **k):
            seen.append(img)
            return orig(img, *a, **k)
        flipper.read_screen = spy
        try:
            got = seller.prepare_item(game, "Stacked Deck")
        finally:
            flipper.read_screen = orig
        self.assertEqual(game.frames, [])
        self.assertEqual(len(seen), 1)          # only the frame with tables
        self.assertTrue(got["data"]["competing"])
        # one Alt hold covered all the waiting
        self.assertEqual(len(game.of("down")), 1)
        self.assertEqual(len(game.of("up")), 1)

    def test_wrong_item_in_the_slot_stops_before_pricing(self):
        # the picker step "succeeded" but the slot shows another item
        game = FakeGame(["panel_chaos_stacked_deck", "tooltip_stacked_deck"])
        orig = seller.select_item
        seller.select_item = lambda *a, **k: None
        try:
            with self.assertRaises(seller.SellError) as cm:
                seller.prepare_item(game, "Greater Eldritch Ichor")
        finally:
            seller.select_item = orig
        self.assertIn("Stacked Deck", str(cm.exception))
        self.assertEqual(game.of("up"), [("up", seller.VK_MENU)])   # Alt freed

    def test_full_order_slots_stop_immediately(self):
        game = FakeGame(["panel_two_orders"])
        orig = seller._counter
        seller._counter = lambda cells: (10, 10)
        try:
            with self.assertRaises(seller.SellError) as cm:
                seller.prepare_item(game, "Stacked Deck")
        finally:
            seller._counter = orig
        self.assertIn("10", str(cm.exception))
        self.assertEqual(game.of("click"), [])

    def test_nothing_happens_without_the_game_in_front(self):
        game = FakeGame(self.FRAMES, focused=False)
        with self.assertRaises(seller.SellError):
            seller.prepare_item(game, "Stacked Deck")
        self.assertEqual(game.events, [])

    def test_no_exchange_window(self):
        game = FakeGame(["picker_have_all", "picker_have_all"])
        with self.assertRaises(seller.SellError):
            seller.prepare_item(game, "Stacked Deck")
        self.assertEqual(game.of("click"), [])
        self.assertEqual(game.of("park"), [("park",)])  # looked twice

    def test_panel_hidden_at_first_is_read_again(self):
        # something (a tooltip under the cursor) covers the panel on the
        # first look; with the cursor parked it is there
        game = FakeGame(["picker_have_all", "panel_chaos_stacked_deck",
                         "tooltip_stacked_deck"])
        got = seller.prepare_item(game, "Stacked Deck")
        self.assertEqual(got["orders"], (0, 10))
        kinds = [e[0] if e[0] != "capture" else e[1] for e in game.events]
        self.assertTrue(kinds.index("picker_have_all") < kinds.index("park")
                        < kinds.index("panel_chaos_stacked_deck"))

    def test_paste_that_did_not_land_still_finds_the_exact_item(self):
        # the search box still shows its hint and the whole list is up:
        # the item is in it under its exact name, so it is the right click
        game = FakeGame(["panel_chaos_ichor", "picker_have_all",
                         "picker_have_all", "tooltip_stacked_deck"])
        seller.prepare_item(game, "Stacked Deck")
        hit = game.of("click")[-1]
        self.assertTrue(2440 <= hit[1] <= 2660 and abs(hit[2] - 1058) <= 14,
                        hit)

    def test_list_that_filters_a_moment_late_is_looked_at_again(self):
        # live case: 13 items went through, the 14th "was not in the list"
        # although a screenshot taken a moment later showed it there
        game = FakeGame(["panel_chaos_ichor", "picker_have_all",
                         "panel_empty",                       # not there yet
                         "picker_have_search_stacked_deck",   # now it is
                         "tooltip_stacked_deck"])
        seller.prepare_item(game, "Stacked Deck")
        self.assertEqual(game.frames, [])
        hit = game.of("click")[-1]
        self.assertTrue(2000 <= hit[1] <= 2215 and abs(hit[2] - 397) <= 14, hit)
        self.assertEqual(game.of("paste"), [("paste", "Stacked Deck")])

    def test_item_that_is_not_in_the_list(self):
        game = FakeGame(["panel_chaos_ichor", "picker_have_all"]
                        + ["picker_have_all"] * 3)      # looked at 3 times
        with self.assertRaises(seller.SellError) as cm:
            seller.prepare_item(game, "Mirror Shard")
        self.assertEqual(game.frames, [])
        self.assertIn("is not in the exchange list", str(cm.exception))
        self.assertEqual(len(game.of("click")), 4)      # no result was clicked
        self.assertEqual(game.of("restore"),
                         [("restore", "user's own clipboard")])


if __name__ == "__main__":
    unittest.main()
