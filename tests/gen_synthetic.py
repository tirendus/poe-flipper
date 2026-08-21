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


def make(path, width=1920, height=1080, shift=0):
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

    for title, rows, y_top, y_head in (("Available Trades", AVAIL, 215, 220),
                                       ("Competing Trades", COMP, 412, 416)):
        dr.rectangle((X(862), Y(y_top), X(1062), Y(y_top + 185)), fill=PARCH)
        dr.text((X(870), Y(y_head)), title, font=font(15, True), fill=DARK)
        dr.text((X(900), Y(y_head + 24)), "Ratio", font=font(13, True),
                fill=DARK)
        dr.text((X(980), Y(y_head + 24)), "Stock", font=font(13, True),
                fill=DARK)
        y = y_head + 48
        for ratio, stock in rows:
            dr.text((X(950), Y(y)), ratio, font=font(13), fill=DARK,
                    anchor="ra")
            dr.text((X(1040), Y(y)), stock, font=font(13), fill=DARK,
                    anchor="ra")
            y += 22

    img.save(path)
    return path
