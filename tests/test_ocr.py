"""OCR-level regression suite: full pipeline on synthetic screenshots and
real captured fixtures.  Slower than test_math (RapidOCR inference)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image

import flipper
from flipper import parse_tables, read_screen, read_tables
import gen_synthetic

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class OcrBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # don't pollute the live app's debug artifacts from test runs
        flipper.DEBUG_SAVE = False
        cls.tmp = tempfile.mkdtemp(prefix="flipper-tests-")


class TestSynthetic(OcrBase):
    def test_1080p_full_pipeline(self):
        path = gen_synthetic.make(str(Path(self.tmp) / "s1080.png"))
        data = read_screen(Image.open(path).convert("RGB"), persist=False)
        self.assertEqual(data["market"], 13.0)
        self.assertEqual(len(data["available"]), 6)
        self.assertEqual(len(data["competing"]), 6)
        self.assertEqual(data["available"][0]["price"], 13.0)
        self.assertAlmostEqual(data["competing"][0]["price"], 22.48)
        self.assertTrue(data["available"][-1]["approx"])
        self.assertTrue(data["competing"][-1]["approx"])

    def test_1440p_height_scaled(self):
        path = gen_synthetic.make(str(Path(self.tmp) / "s1440.png"),
                                  2560, 1440)
        data = read_screen(Image.open(path).convert("RGB"), persist=False)
        self.assertEqual(len(data["available"]), 6)
        self.assertEqual(len(data["competing"]), 6)

    def test_ultrawide_shifted_autocalibrates(self):
        path = gen_synthetic.make(str(Path(self.tmp) / "uw.png"),
                                  3440, 1440, shift=200)
        data = read_screen(Image.open(path).convert("RGB"), persist=False)
        self.assertEqual(len(data["available"]), 6)
        self.assertEqual(len(data["competing"]), 6)
        self.assertAlmostEqual(flipper._GEO.dx, 640, delta=8)


class TestPoE2Layout(OcrBase):
    """PoE2 opens the exchange left of center, positions the tooltip at a
    different offset from the tabs, and covers the Market Ratio title."""

    def _poe2(self, name):
        # left-shifted panel, independently shifted tables, covered Market
        # Ratio title, AND pinned-tooltip chrome merging into the Available
        # Trades title (the real-world case that broke title anchoring)
        return gen_synthetic.make(str(Path(self.tmp) / name),
                                  shift=-330, table_dx=-40, table_dy=-40,
                                  hide_market=True, chrome=True)

    def test_default_left_layout_calibrates(self):
        path = self._poe2("poe2.png")
        with tempfile.TemporaryDirectory() as td:
            old = flipper.CALIB_FILE
            flipper.CALIB_FILE = Path(td) / "calib.json"
            try:
                data = read_screen(Image.open(path).convert("RGB"))
            finally:
                flipper.CALIB_FILE = old
        self.assertEqual(len(data["available"]), 6)
        self.assertEqual(len(data["competing"]), 6)
        self.assertEqual(data["available"][0]["price"], 13.0)
        # tables anchored independently of the tabs
        self.assertAlmostEqual(flipper._GEO.tdx - flipper._GEO.dx, -40,
                               delta=10)

    def test_profiles_survive_game_switching(self):
        poe1 = gen_synthetic.make(str(Path(self.tmp) / "p1.png"))
        poe2 = self._poe2("p2.png")
        with tempfile.TemporaryDirectory() as td:
            old = flipper.CALIB_FILE
            flipper.CALIB_FILE = Path(td) / "calib.json"
            try:
                for path in (poe1, poe2, poe1, poe2):
                    data = read_screen(Image.open(path).convert("RGB"))
                    self.assertEqual(len(data["available"]), 6, path)
                    self.assertEqual(len(data["competing"]), 6, path)
                profiles, cached = flipper.load_geometries(1920, 1080)
                self.assertTrue(cached)
                self.assertEqual(len(profiles), 2)   # one per game layout
            finally:
                flipper.CALIB_FILE = old


class TestFixtures(OcrBase):
    """Real captures that once broke the parser."""

    def parse(self, name):
        img = Image.open(FIXTURES / name)
        return parse_tables(read_tables(img))

    def test_own_order_box(self):
        # player's Selling/Buying summary below the tables must not leak in,
        # and the missing 'Available Trades' header uses the sub-header
        d = self.parse("book_own_order_box.png")
        self.assertEqual([(round(e["price"], 3), e["stock"])
                          for e in d["available"]],
                         [(1.75, 28), (1.7, 27217), (1.6, 1512), (1.06, 845),
                          (1.0, 501), (1.0, 6405)])
        self.assertEqual(d["competing"], [])
        self.assertEqual(d["unparsed"], 0)

    def test_background_noise(self):
        # stash/UI numbers beside the tooltip must not corrupt rows
        d = self.parse("book_background_noise.png")
        self.assertEqual(len(d["competing"]), 6)
        for e in d["available"] + d["competing"]:
            self.assertTrue(0.8 <= e["price"] <= 1.35,
                            f"implausible level {e}")

    def test_dead_market(self):
        d = self.parse("book_dead_market.png")
        self.assertEqual(d["competing"], [])
        self.assertEqual(len(d["available"]), 6)
        self.assertEqual(d["available"][0]["price"], 1.5)


if __name__ == "__main__":
    unittest.main()
