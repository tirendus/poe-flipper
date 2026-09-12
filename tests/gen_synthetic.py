"""Synthetic exchange-panel screenshots for OCR pipeline tests.

Draws the Market Ratio tooltip the way the game renders it (center-anchored,
height-scaled), so the full capture->geometry->OCR->parse pipeline can be
exercised at any resolution without the game running.
"""
from PIL import Image, ImageDraw, ImageFont

AVAIL = [("13 : 1", "1,378"), ("12.10 : 1", "12,100"), ("11 : 1", "7,898"),
         ("8 : 1", "2,568"), ("7 : 1", "763"), ("< 7 : 1", "49,638")]
COMP = [("22.48 : 1", "21"), ("25 : 1", "4"), ("30 : 1", "3"),
        ("40 : 1", "1"), ("46.67 : 1", "3"), ("> 46.67 : 1", "342")]


def make(path, width=1920, height=1080, shift=0,
         table_dx=0, table_dy=0, hide_market=False, chrome=False):
    """shift moves the whole panel; table_dx/table_dy additionally move the
    trade tables relative to the tabs (PoE2's tooltip sits at a different
    offset than PoE1's); hide_market omits the Market Ratio title (PoE2's
    tooltip covers it); chrome draws close/pin buttons right of the
    Available Trades title (a pinned tooltip — OCR merges them into the
    title, which must not skew the table anchoring)."""
    s = height / 1080.0

    def X(x):
        return int(width / 2 + (x - 960) * s) + shift

    def Y(y):
        return int(y * s)

    def font(sz, bold=False):
        name = "seguisb.ttf" if bold else "segoeui.ttf"
        return ImageFont.truetype(rf"C:\Windows\Fonts\{name}",
                                  max(9, int(sz * s)))

    PARCH, DARK, WHITE = (205, 200, 185), (40, 35, 30), (230, 225, 210)
    img = Image.new("RGB", (width, height), (25, 20, 16))
    dr = ImageDraw.Draw(img)

    if not hide_market:
        dr.text((X(960), Y(174)), "Market Ratio", font=font(16, True),
                fill=WHITE, anchor="mm")
    dr.text((X(707), Y(197)), "I Want", font=font(14, True), fill=WHITE,
            anchor="mm")
    dr.text((X(1213), Y(197)), "I Have", font=font(14, True), fill=WHITE,
            anchor="mm")
    dr.text((X(960), Y(196)), "13 : 1", font=font(15), fill=WHITE,
            anchor="mm")
    dr.text((X(714), Y(240)), "Chaos Orb", font=font(15), fill=WHITE,
            anchor="mm")
    dr.text((X(1217), Y(240)), "Ancient Orb", font=font(15), fill=WHITE,
            anchor="mm")

    def TX(x):
        return X(x) + int(table_dx * s)

    def TY(y):
        return Y(y) + int(table_dy * s)

    for title, rows, y_top, y_head in (("Available Trades", AVAIL, 215, 220),
                                       ("Competing Trades", COMP, 412, 416)):
        dr.rectangle((TX(862), TY(y_top), TX(1062), TY(y_top + 185)),
                     fill=PARCH)
        dr.text((TX(870), TY(y_head)), title, font=font(15, True), fill=DARK)
        if chrome and title.startswith("Available"):
            dr.text((TX(1066), TY(y_head)), "X POO", font=font(13, True),
                    fill=WHITE)
        # sub-header word centers match the real game's measured positions
        # (they are calibration anchors)
        sub_y = 254 if title.startswith("Available") else 452
        dr.text((TX(896), TY(sub_y)), "Ratio", font=font(13, True),
                fill=DARK, anchor="mm")
        dr.text((TX(984), TY(sub_y)), "Stock", font=font(13, True),
                fill=DARK, anchor="mm")
        y = y_head + 48
        for ratio, stock in rows:
            dr.text((TX(950), TY(y)), ratio, font=font(13), fill=DARK,
                    anchor="ra")
            dr.text((TX(1040), TY(y)), stock, font=font(13), fill=DARK,
                    anchor="ra")
            y += 22

    img.save(path)
    return path
