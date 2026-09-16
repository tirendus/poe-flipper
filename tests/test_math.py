"""Regression suite for the pricing/parsing logic (no OCR, fast).

Every landmark book here comes from a real bug fixed during development —
run this before any release: it is the memory of everything that has ever
gone wrong with the math.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import flipper
from flipper import (build_buy_qty_suggestions, build_buy_suggestions,
                     build_suggestions, classify_book, parse_hotkey,
                     parse_ratio_num, parse_tables, simplest_between,
                     snap_ratio)


def L(a, b, stock, approx=False):
    return {"price": a / b, "a": a, "b": b, "stock": stock, "approx": approx}


ANCIENT = {     # first book ever: chunky x:1 ratios, human-friendly picks
    "market": 13.0, "unparsed": 0,
    "available": [L(13, 1, 1378), L(12.1, 1, 12100)],
    "competing": [L(22.48, 1, 2100), L(25, 1, 400), L(30, 1, 300)],
    "want_name": "Chaos", "have_name": "Ancient",
}

DECKS_HEALTHY = {   # v1.0.4: deep top level, queue-ladder profits
    "market": 1.0, "unparsed": 0,
    "available": [L(106, 100, 18), L(105, 100, 23), L(103, 100, 30),
                  L(101, 100, 71), L(1, 1, 1564), L(1, 1, 744, True)],
    "competing": [L(110, 100, 361), L(120, 100, 320), L(125, 100, 432),
                  L(130, 100, 60), L(133, 100, 81), L(133, 100, 407, True)],
    "want_name": "Deck", "have_name": "Chaos",
}

DIVINE_WRAPPED = {  # v1.0.9: values recovered from two-line wrapped ratios
    "market": 1 / 178.8, "unparsed": 0,
    "available": [L(1, 178, 11), L(1, 178.5, 22), L(1, 178.8, 170),
                  L(1, 178.99, 100), L(1, 178.99, 4847, True)],
    "competing": [L(1, 177, 313290), L(1, 176.81, 12200), L(1, 176.67, 1590),
                  L(1, 176.5, 14473), L(1, 176.4, 882),
                  L(1, 176.4, 1424695, True)],
    "want_name": "Divine", "have_name": "Chaos",
}

CRYSTAL = {     # v1.0.12: integer levels must be outbid by exactly one
    "market": 1 / 470, "unparsed": 0,
    "available": [L(1, 470, 3), L(1, 485, 4), L(1, 490, 11), L(1, 493, 2),
                  L(1, 494, 2), L(1, 494, 154, True)],
    "competing": [L(1, 410, 1640), L(1, 406, 4060), L(1, 401, 401),
                  L(1, 400, 3200), L(1, 399, 3591), L(1, 399, 112845, True)],
    "want_name": "Crystal", "have_name": "Chaos",
}

ALTERATION = {  # v1.0.13: micro-bands (8.50 vs 8.47) ate whole rungs
    "market": 1 / 8.9, "unparsed": 0,
    "available": [L(1, 8.89, 993), L(1, 8.90, 1141), L(1, 8.98, 9750),
                  L(1, 8.99, 4400), L(1, 8.99, 1100), L(1, 8.99, 16822, True)],
    "competing": [L(1, 8.50, 17), L(1, 8.50, 3408), L(1, 8.47, 8000),
                  L(1, 8, 21040), L(1, 7.99, 8769), L(1, 7.99, 326517, True)],
    "want_name": "Chaos", "have_name": "Alteration",
}

SCARAB_SELL_INT = {  # v1.0.14: integer a:1 sells undercut by one chaos
    "market": 320.0, "unparsed": 0,
    "available": [L(320, 1, 31040), L(317, 1, 1268), L(316, 1, 4424),
                  L(315, 1, 2835), L(314, 1, 3140), L(314, 1, 7282, True)],
    "competing": [L(325, 1, 2), L(330, 1, 3), L(332, 1, 1), L(335, 1, 4),
                  L(337, 1, 1), L(337, 1, 594, True)],
    "want_name": "Chaos", "have_name": "Scarab",
}

BLOODLINES = {  # v1.0.9: qty=1 on a 1-chaos-spaced book -> Match fallbacks
    "market": 1 / 200, "unparsed": 0,
    "available": [L(1, 199, 5), L(1, 200, 10), L(1, 201, 2), L(1, 202, 15),
                  L(1, 203, 29), L(1, 203, 124, True)],
    "competing": [L(1, 195, 975), L(1, 194, 7566), L(1, 193, 12159),
                  L(1, 192, 7680), L(1, 190, 7980), L(1, 190, 92802, True)],
    "want_name": "Scarab", "have_name": "Chaos",
}

TRADITION = {   # v1.0.11: scarce item, small fill blocks over exact totals
    "market": 1 / 44, "unparsed": 0,
    "available": [L(1, 37, 300), L(1, 40, 800), L(1, 44, 3000),
                  L(1, 44, 9000, True)],
    "competing": [L(1, 36, 144), L(1, 35, 315), L(1, 34, 7208), L(1, 33, 330),
                  L(1, 32, 32), L(1, 32, 90000, True)],
    "want_name": "Scarab", "have_name": "Chaos",
}

DECKS_QTY = {   # v1.0.11: wide-band block scoring (5:13 over 1:3)
    "market": 1 / 2.5, "unparsed": 0,
    "available": [L(1, 2.55, 3), L(1, 3.65, 2000), L(1, 3.65, 5000, True)],
    "competing": [L(1, 2.5, 750), L(1, 2.4, 480), L(1, 2.2, 220),
                  L(1, 2.2, 9000, True)],
    "want_name": "Deck", "have_name": "Chaos",
}

DEAD_SELL = {   # v1.0.15: no rival sellers; must not price AT the buyers
    "market": 1.5, "unparsed": 0,
    "available": [L(1.50, 1, 6525), L(1.40, 1, 112), L(1.33, 1, 812),
                  L(1.33, 1, 117), L(1.32, 1, 29), L(1.32, 1, 16929, True)],
    "competing": [],
    "want_name": "Chaos", "have_name": "Item",
}

TATTOO = {      # v1.0.7: buy queue counts in fulfillable trades, not chaos
    "market": 1 / 89, "unparsed": 0,
    "available": [L(1, 89, 100), L(1, 90, 500), L(1, 90, 300, True)],
    "competing": [L(1, 76, 76), L(1, 75, 150), L(1, 74, 74),
                  L(1, 72, 144), L(1, 72, 500, True)],
    "want_name": "Tattoo", "have_name": "Chaos",
}


def coarse(rows):
    return [(s["w"], s["d"]) for _l, s in rows if not s.get("fine")]


class TestParsing(unittest.TestCase):
    def test_hotkey(self):
        self.assertEqual(parse_hotkey("alt+q"), (1, 81))
        self.assertEqual(parse_hotkey("ctrl+shift+x"), (6, 88))
        self.assertEqual(parse_hotkey("f8"), (0, 119))
        for bad in ("alt", "ctrl+foo", ""):
            with self.assertRaises(ValueError):
                parse_hotkey(bad)

    def test_ratio_num(self):
        self.assertEqual(parse_ratio_num("12.10"), 12.10)
        self.assertEqual(parse_ratio_num("1,378"), 1378)
        self.assertEqual(parse_ratio_num("178.99"), 178.99)

    def test_sections_and_market(self):
        rows = [("market", "13 : 1"), ("available", "13 : 1 1,378"),
                ("available", "12.10 : 1 12,100"),
                ("competing", "22.48 : 1 21"), ("competing", "> 46.67 : 1 342")]
        d = parse_tables(rows)
        self.assertEqual(d["market"], 13.0)
        self.assertEqual(len(d["available"]), 2)
        self.assertEqual(len(d["competing"]), 2)
        self.assertTrue(d["competing"][-1]["approx"])

    def test_outlier_filter(self):
        # v1.0.4: "1:1.20" OCR'd as "1:120" must be dropped
        rows = [("available", "1 : 1.10 20"), ("available", "1 : 120 315"),
                ("available", "1 : 1.25 424"), ("available", "1 : 1.30 60"),
                ("available", "1 : 1.33 66")]
        d = parse_tables(rows)
        self.assertTrue(all(e["price"] > 0.5 for e in d["available"]))
        self.assertEqual(d["unparsed"], 1)

    def test_aggregate_flags(self):
        # v1.0.9: markers bleeding mid-section cleared; lost final marker
        # restored when the last row repeats the previous price
        rows = [("competing", "<1 : 100 5"), ("competing", "1 : 99 7"),
                ("competing", "1 : 99 900")]
        d = parse_tables(rows)
        flags = [e["approx"] for e in d["competing"]]
        self.assertEqual(flags, [False, False, True])


class TestPricing(unittest.TestCase):
    def test_simplest_between(self):
        self.assertEqual(simplest_between(21.8, 22.48), (22, 1))
        self.assertEqual(simplest_between(0.11412, 0.117647), (3, 26))
        self.assertEqual(simplest_between(1.5, 1.545), (20, 13))

    def test_snap_divine_scale(self):
        # v1.0.6: 1:1000-scale ratios must not lose to absurd 1:500
        s = snap_ratio(1 / 1010 * 1.005, 5000, lo=1 / 1010 * 0.9999,
                       denom_weight=0.1, dmax_cap=150)
        self.assertGreaterEqual(1 / s["price"], 900)

    def test_classify(self):
        cls = classify_book(BLOODLINES["competing"])
        self.assertEqual(cls["wall"]["b"], 194)      # 25% of deepest
        self.assertEqual(len(cls["cluster"]), 1)     # the 195 dud
        self.assertIsNotNone(cls["abyss"])
        # duplicate displayed prices merge (v1.0.5)
        cls = classify_book(ALTERATION["competing"])
        self.assertEqual(cls["real"][0]["stock"], 17 + 3408)


class TestLadders(unittest.TestCase):
    def test_ancient_sell(self):
        rows = build_suggestions(ANCIENT, 352)["rows"]
        self.assertEqual(coarse(rows)[:3], [(22, 1), (24, 1), (29, 1)])

    def test_crystal_buy_integer_steps(self):
        rows = build_buy_suggestions(CRYSTAL, 3900)["rows"]
        self.assertEqual([d for _w, d in coarse(rows)][:3], [411, 407, 402])

    def test_scarab_sell_integer_steps(self):
        rows = build_suggestions(SCARAB_SELL_INT, 50)["rows"]
        self.assertEqual(coarse(rows),
                         [(324, 1), (329, 1), (331, 1), (334, 1), (336, 1),
                          (338, 1)])

    def test_alteration_complete_ladder(self):
        rows = build_suggestions(ALTERATION, 12515)["rows"]
        self.assertGreaterEqual(len(coarse(rows)), 5)
        first = rows[0][1]
        self.assertEqual((first["w"], first["d"]), (3, 26))

    def test_healthy_deck_flip(self):
        sugg = build_buy_suggestions(DECKS_HEALTHY, 193)
        # losing rows hidden, profitable queue rungs present
        self.assertTrue(all(s["profit"] > 0 for _l, s in sugg["rows"]))
        pays = [round(s["pay_per_unit"], 3) for _l, s in sugg["rows"]
                if not s["fine"]]
        self.assertIn(round(6 / 7, 3), pays)          # 2nd in line 7:6

    def test_divine_wall_resale(self):
        sugg = build_buy_suggestions(DIVINE_WRAPPED, 5000)
        self.assertAlmostEqual(sugg["resale"], 178.8 * 0.995, places=3)

    def test_greedy_floor(self):
        # the guarantee: some option always clears the greedy margin
        sugg = build_buy_suggestions(CRYSTAL, 3900)
        self.assertTrue(any(s["profit_pct"] >= 15.0
                            for _lb, s in sugg["rows"]))
        # on a book whose ladder tops out below 15% the labeled Greedy row
        # must dig past the abyss to deliver it (v1.0.6 deck book)
        decks = {
            "market": 1 / 1.11, "unparsed": 0,
            "available": [L(1, 1.11, 45), L(1, 1.12, 25), L(1, 1.12, 8),
                          L(1, 1.20, 4750), L(1, 1.20, 627),
                          L(1, 1.20, 6525, True)],
            "competing": [L(1, 1.11, 6439), L(1, 1.10, 1551), L(1, 1.09, 12),
                          L(1, 1.09, 467), L(1, 1.02, 102),
                          L(1, 1.02, 13793, True)],
            "want_name": "Deck", "have_name": "Chaos",
        }
        rows = build_buy_suggestions(decks, 1000)["rows"]
        greedy = [s for lb, s in rows if "Greedy" in lb]
        self.assertTrue(greedy)
        self.assertGreaterEqual(greedy[0]["profit_pct"], 15.0)

    def test_queue_in_fulfillable_trades(self):
        rows = build_buy_suggestions(TATTOO, 1000)["rows"]
        labels = " | ".join(lb for lb, _s in rows)
        self.assertIn("(1 ahead)", labels)
        self.assertIn("(3 ahead)", labels)


class TestQuantityBuy(unittest.TestCase):
    def test_qty1_match_fallback(self):
        rows = build_buy_qty_suggestions(BLOODLINES, 1)["rows"]
        useds = [s["used"] for _l, s in rows if not s["fine"]]
        self.assertEqual(useds[:5], [196, 195, 194, 193, 191])
        self.assertTrue(any("Match" in lb for lb, _s in rows))

    def test_qty_small_blocks(self):
        rows = build_buy_qty_suggestions(TRADITION, 40)["rows"]
        lead = [s for _l, s in rows if not s["fine"]]
        self.assertTrue(all(s["w"] <= 10 for s in lead))
        self.assertIn((2, 71), [(s["w"], s["d"]) for s in lead])

    def test_qty_block_scoring(self):
        rows = build_buy_qty_suggestions(DECKS_QTY, 100)["rows"]
        first = rows[0][1]
        self.assertEqual((first["w"], first["d"]), (5, 13))

    def test_qty_flex_valuable_currency(self):
        # buying 50 chaos with divines: exact-50 prices step 8.33 -> 10.0,
        # missing every band; flexing the quantity restores fine pricing,
        # and the match fallback must not emit a 10:1 row labeled 9.3
        book = {
            "market": 9.21, "unparsed": 0,
            "available": [L(9.22, 1, 461), L(9.21, 1, 2763),
                          L(9.20, 1, 24886), L(9.19, 1, 708),
                          L(9.19, 1, 386), L(9.19, 1, 272686, True)],
            "competing": [L(9.30, 1, 630), L(9.33, 1, 7677), L(9.34, 1, 200),
                          L(9.35, 1, 200), L(9.38, 1, 353),
                          L(9.38, 1, 39304, True)],
            "want_name": "Chaos", "have_name": "Divine",
        }
        sugg = build_buy_qty_suggestions(book, 50)
        rows = sugg["rows"]
        first = rows[0][1]
        # Outbid all flexes to 46:5 (9.2/ea) instead of overpaying at 8.33
        self.assertEqual((first["want_total"], first["used"]), (46, 5))
        prices = [s["price"] for _l, s in rows]
        self.assertNotIn(10.0, prices)          # the old bogus "Match 9.3"
        self.assertFalse(any("Match" in lb and abs(s["price"] - 9.3) > 0.05
                             for lb, s in rows))
        # flexed quantities stay within 15% of the request
        self.assertTrue(all(abs(s["want_total"] - 50) <= 7.5
                            for _l, s in rows))

    def test_qty_flex_tie_does_not_crash(self):
        # v1.0.19 regression: exact and flexed candidates with equal error
        # made the tuple sort compare None with int -> TypeError, leaving
        # the popup silently inert on Enter
        book = {
            "market": 0.1, "unparsed": 0,
            "available": [L(1, 14.5, 10), L(1, 14.66, 3), L(1, 14.75, 678),
                          L(1, 15, 188), L(1, 15.5, 2), L(1, 15.5, 936, True)],
            "competing": [L(1, 10, 270), L(1, 8, 7992), L(1, 7, 119),
                          L(1, 6.2, 31), L(1, 6.11, 110), L(1, 6.11, 9258, True)],
            "want_name": "Item", "have_name": "Chaos",
        }
        rows = build_buy_qty_suggestions(book, 8)["rows"]
        self.assertTrue(rows)
        self.assertTrue(all(s["used"] >= 1 and s["want_total"] >= 1
                            for _l, s in rows))

    def test_qty_no_resale_no_greedy(self):
        rows = build_buy_qty_suggestions(TRADITION, 40)["rows"]
        self.assertFalse(any("Greedy" in lb for lb, _s in rows))
        self.assertFalse(any("profit" in s for _l, s in rows))


class TestDeadMarket(unittest.TestCase):
    def test_sell_never_crosses(self):
        sugg = build_suggestions(DEAD_SELL, 186)
        self.assertTrue(sugg["dead"])
        self.assertTrue(all(s["price"] > 1.50 for _l, s in sugg["rows"]))
        first = sugg["rows"][0][1]
        self.assertEqual((first["w"], first["d"]), (20, 13))

    def test_sell_integer_wall(self):
        book = dict(DEAD_SELL)
        book["available"] = [L(320, 1, 31040), L(317, 1, 1268),
                             L(314, 1, 7282, True)]
        book["market"] = 320.0
        first = build_suggestions(book, 50)["rows"][0][1]
        self.assertEqual((first["w"], first["d"]), (321, 1))

    def test_buy_never_crosses(self):
        book = {"market": 1 / 470, "unparsed": 0,
                "available": [L(1, 470, 300), L(1, 485, 40),
                              L(1, 485, 154, True)],
                "competing": [], "want_name": "X", "have_name": "Chaos"}
        rows = build_buy_suggestions(book, 3000)["rows"]
        self.assertTrue(all(s["price"] > 1 / 470 and s["profit"] > 0
                            for _l, s in rows))
        qty = build_buy_qty_suggestions(book, 3)["rows"]
        self.assertEqual(qty[0][1]["used"], 1409)


class TestWrappedAssembly(unittest.TestCase):
    """v1.0.9: two-line wrapped ratios + overprint mashes + noise cells."""

    CELLS = [
        (' MARKET RATIO', 94, 48, 150), ('1: 178.80', 164, 94, 90),
        ('AVAILABLE TRADES', 76, 158, 200), ('Ratio', 92, 209, 40),
        ('Stock', 268, 209, 40),
        ('1:178', 81, 264, 60), ('11', 298, 252, 20),
        ('22', 293, 298, 20), ('178.50', 73, 320, 55),
        ('170', 287, 392, 28), ('173:80', 73, 412, 55),
        ('100', 282, 436, 28), ('<73:99', 75, 459, 60),
        ('4,847', 269, 484, 45), ('178.99', 74, 505, 55),
        ('COMPETING TRADES', 62, 554, 210), ('Ratio', 92, 605, 40),
        ('Stock', 265, 604, 40),
        ('1: 177', 95, 648, 55), ('313,290', 255, 648, 62),
        ('1 : 176.81', 99, 693, 85), ('12,200', 261, 694, 55),
        ('1 : 176.67', 87, 748, 85), ('1,590', 269, 741, 45),
        ('14,473', 265, 788, 55), ('1715:50', 73, 808, 62),
        ('882', 283, 832, 28), ('71:40', 75, 856, 48),
        ('1,424,695', 240, 880, 80), ('176.40', 74, 902, 55),
        # background noise right of the parchment (v1.0.8 x-band filter)
        ('X', 416, 141, 12), ('375', 450, 534, 30), ('44', 452, 766, 20),
        ('3.7', 460, 826, 25), ('40', 0, 187, 20),
    ]

    def test_full_recovery(self):
        from PIL import Image
        orig = flipper.ocr_cells
        flipper.ocr_cells = lambda img, scale=2: list(self.CELLS)
        try:
            tagged = flipper.read_tables(Image.new("RGB", (250, 550)))
        finally:
            flipper.ocr_cells = orig
        d = parse_tables(tagged)
        self.assertEqual([(e["b"], e["stock"]) for e in d["available"]],
                         [(178.0, 11), (178.5, 22), (178.8, 170),
                          (178.99, 100), (178.99, 4847)])
        self.assertEqual([(e["b"], e["stock"]) for e in d["competing"]],
                         [(177.0, 313290), (176.81, 12200), (176.67, 1590),
                          (176.5, 14473), (176.4, 882), (176.4, 1424695)])
        self.assertEqual(d["unparsed"], 0)
        self.assertTrue(d["available"][-1]["approx"])
        self.assertTrue(d["competing"][-1]["approx"])
        self.assertFalse(d["available"][-2]["approx"])


if __name__ == "__main__":
    unittest.main()
