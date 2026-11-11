"""Synthetic PDFs with known layout. All text is invented filler."""
from __future__ import annotations

import pymupdf

W, H = 595, 842
LEFT = (60, 290)
RIGHT = (310, 540)
BODY, SMALL = 10, 7

FILLER = ("Sentence {n} of this synthetic paragraph exists only to fill a column with wrapped "
          "lines so that the layout analysis has something realistic to measure. ")


def para(n: int, sentences: int = 2) -> str:
    return "".join(FILLER.format(n=f"{n}.{i}") for i in range(1, sentences + 1)).strip()


def box(page, x, y, text, *, size=BODY, bold=False, height=400) -> float:
    """Place wrapped text; returns the y just below it."""
    rect = pymupdf.Rect(x[0], y, x[1], y + height)
    left = page.insert_textbox(rect, text, fontsize=size, fontname="hebo" if bold else "helv")
    assert left >= 0, "test text does not fit its box"
    return y + height - left


def furniture(page, number: int, title="Synthetic Handbook of Layout Testing") -> None:
    page.insert_text((60, 40), title, fontsize=8)
    page.insert_text((290, 815), str(number), fontsize=8)


def two_column_page(doc, number: int, heading: str | None = None, start: int = 1):
    """Full-width heading, then two columns whose paragraph gaps line up horizontally."""
    page = doc.new_page(width=W, height=H)
    furniture(page, number)
    y = 90
    if heading:
        y = box(page, (60, 540), y, heading, size=18, bold=True, height=40) + 14
    for col_index, col in enumerate((LEFT, RIGHT)):
        cy = y
        for row in range(3):
            cy = box(page, col, cy, para(start + col_index * 3 + row), height=160) + 12
    return page


def build_book() -> bytes:
    doc = pymupdf.open()
    two_column_page(doc, 1, "CHAPTER 1 Arterial Layout Basics", start=1)
    two_column_page(doc, 2, None, start=7)
    two_column_page(doc, 3, "CHAPTER 2 Venous Layout Basics", start=13)
    two_column_page(doc, 4, None, start=19)
    return doc.tobytes()


def build_continuation() -> bytes:
    """A paragraph that runs from the bottom of the left column to the top of the right one."""
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    y = box(page, LEFT, 90, para(1), height=160) + 12
    box(page, LEFT, y, "This paragraph starts in the left column and deliberately stops in the mid-", height=60)
    y = box(page, RIGHT, 90, "dle of a word so that it must be joined with its continuation here.", height=60) + 12
    box(page, RIGHT, y, para(2), height=160)
    return doc.tobytes()


def build_mixed() -> bytes:
    """Single column with a section heading, a figure caption, a footnote and references."""
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    full = (60, 540)
    y = box(page, full, 90, "Imaging Follow-up", size=14, bold=True, height=30) + 8
    y = box(page, full, y, para(1), height=80) + 10
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 80, 50), False)
    pix.set_rect(pix.irect, (120, 140, 200))
    page.insert_image(pymupdf.Rect(60, y, 300, y + 150), pixmap=pix)
    y += 158
    y = box(page, full, y, "Figure 1.1 Invented caption describing a blue rectangle.", size=8, height=30) + 10
    y = box(page, full, y, "- First invented bullet point with 5 mg of nothing.", height=30) + 10
    y = box(page, full, y, "References", size=14, bold=True, height=30) + 8
    box(page, full, y, "1. Sample AB, Writer CD. An invented reference title. J Synth Test. 2020;1:1-9.", height=40)
    box(page, full, 760, "* Invented footnote in a small font near the bottom.", size=SMALL, height=30)
    return doc.tobytes()


TABLE = [["Diameter", "Interval", "Note"],
         ["3.0-3.9 cm", "36 months", "invented"],
         ["4.0-4.9 cm", "12 months", "invented"],
         ["5.0-5.4 cm", "6 months", "invented"]]


def _cells(page, x0, y0, col_w, row_h, rows, *, grid: bool):
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            page.insert_text((x0 + c * col_w + 4, y0 + r * row_h + 14), cell, fontsize=9,
                             fontname="hebo" if r == 0 else "helv")
    width, height = col_w * len(rows[0]), row_h * len(rows)
    if grid:
        for r in range(len(rows) + 1):
            page.draw_line((x0, y0 + r * row_h), (x0 + width, y0 + r * row_h))
        for c in range(len(rows[0]) + 1):
            page.draw_line((x0 + c * col_w, y0), (x0 + c * col_w, y0 + height))
    else:   # "booktabs": top rule, rule under the header, bottom rule
        for y in (y0, y0 + row_h, y0 + height):
            page.draw_line((x0, y), (x0 + width, y))
    return y0 + height


def build_table(grid: bool = True) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    full = (60, 540)
    y = box(page, full, 90, para(1), height=80) + 12
    y = box(page, full, y, "Table 2.1 Invented surveillance intervals.", size=8, height=20) + 6
    y = _cells(page, 60, y, 160, 20, TABLE, grid=grid) + 16
    box(page, full, y, para(2), height=80)
    return doc.tobytes()


def build_vector_figure() -> bytes:
    """A small bar chart drawn with vector shapes, with labels inside and a caption below."""
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    full = (60, 540)
    y = box(page, full, 90, para(1), height=80) + 12
    page.draw_line((80, y), (80, y + 150))
    page.draw_line((80, y + 150), (330, y + 150))
    for i, h in enumerate((40, 90, 120, 70)):
        page.draw_rect(pymupdf.Rect(100 + i * 55, y + 150 - h, 140 + i * 55, y + 150), fill=(0.3, 0.5, 0.8))
        page.insert_text((104 + i * 55, y + 144 - h), f"{h}%", fontsize=7)
    y += 160
    y = box(page, full, y, "Figure 3.2 Invented bar chart of nothing in particular.", size=8, height=20) + 12
    box(page, full, y, para(2), height=80)
    return doc.tobytes()


# ----------------------------------------------------------------------------- "at a glance" layouts
# Invented stand-ins for the page designs that broke the parser on a real handbook: a text page
# whose footer bridges the column gutter, a fraction formula, a framed diagram with leader
# labels, and a framed panel that holds a table next to a formula and a chart.

BAR = (0.97, 0.82, 0.76)
HEAD = (0.94, 0.66, 0.56)
SKIN = (1.0, 0.9, 0.83)


def lines(page, x: float, y: float, rows: list[str], *, size: float = BODY, bold: bool = False,
          pitch: float = 11.5) -> float:
    """Consecutive lines written one by one; returns the y below the last line."""
    for row in rows:
        page.insert_text((x, y + size), row, fontsize=size, fontname="hebo" if bold else "helv")
        y += pitch
    return y


def build_two_column_steps() -> bytes:
    """Four text pages; page 2 has bullets and numbered steps that run on across the columns.

    Every page ends with a running foot that spans the gutter and changes from page to page
    ("<chapter title>   Invented principles   35"), as right-hand pages of a handbook do.
    """
    doc = pymupdf.open()
    for number in range(1, 5):
        page = doc.new_page(width=W, height=H)
        if number != 2:
            for col in (LEFT, RIGHT):
                y = 90
                for row in range(3):
                    y = box(page, col, y, para(number * 10 + row), height=160) + 12
        else:
            y = lines(page, LEFT[0], 90, ["The invented index is worth a chapter of its own.", "It is:",
                                          "· highly sensitive and specific for invented",
                                          "disease", "· non-invasive", "· reproducible.",
                                          "The premise of the index lies in measuring a",
                                          "difference between the arm and the foot."], size=9)
            y = box(page, LEFT, y + 14, "Sound patterns", size=13, bold=True, height=24) + 4
            lines(page, LEFT[0], y, ["These are the sounds, from normal to abnormal:",
                                     "· Triphasic: a loud whoosh and two smaller ones."], size=9)
            y = lines(page, RIGHT[0], 90, ["· Biphasic: a loud whoosh followed by one more",
                                           "whoosh per heartbeat.",
                                           "· Monophasic: only one whoosh per heartbeat."], size=9)
            y = box(page, RIGHT, y + 14, "Procedure to perform the test", size=13, bold=True, height=24) + 4
            y = lines(page, RIGHT[0], y, ["1 The patient must be lying supine and be rested.",
                                          "2 The legs and arms should be fully exposed.",
                                          "3 Apply a cuff snugly around the upper arm."], size=9)
            y = lines(page, RIGHT[0], y, ["4 Inflate the cuff until the signal disappears.",
                                          "5 Inflate another 20-30 mmHg.",
                                          "6 Repeat with the other leg.",
                                          "Note: if the patient has an invented ulcer, it is",
                                          "protected with film for the duration of the test."], size=9)
        page.insert_text((300, 812), f"Invented chapter number {number}   Invented principles   {33 + number}",
                         fontsize=8)
    return doc.tobytes()


def _fraction(page, x: float, y: float, numerator: list[tuple[str, float]], denominator: str,
              left: str = "", right: str = "", *, size: float = 9) -> float:
    """A printed fraction. `numerator` is (text, x) pieces written separately, the way a PDF
    stores brackets set in a different font. Returns the y of the rule."""
    for text, at in numerator:
        page.insert_text((at, y), text, fontsize=size)
    end = max(at + pymupdf.get_text_length(text, fontsize=size) for text, at in numerator)
    rule = y + 3
    page.draw_line((x, rule), (end, rule), width=0.5)
    width = pymupdf.get_text_length(denominator, fontsize=size)
    page.insert_text(((x + end - width) / 2, rule + size + 1), denominator, fontsize=size)
    if left:
        page.insert_text((x - pymupdf.get_text_length(left, fontsize=size) - 5, rule + 3), left, fontsize=size)
    if right:
        page.insert_text((end + 4, rule + 3), right, fontsize=size)
    return rule


def build_fraction() -> bytes:
    """A paragraph, a fraction formula whose brackets are separate text objects, a paragraph."""
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    full = (60, 540)
    y = box(page, full, 90, para(1), height=80) + 18
    size = 9
    pieces, x = [], 60.0
    for text in ("Systolic pedal", "(", "DP", ")", "pressure"):
        if text in ("(", ")", "DP"):
            x -= 0 if text == "(" else 2.5          # brackets hug the abbreviation
        pieces.append((text, x))
        x += pymupdf.get_text_length(text, fontsize=size) + 2.5
    rule = _fraction(page, 60, y, pieces, "Systolic brachial pressure", right="= ABPI", size=size)
    box(page, full, rule + 30, para(2), height=80)
    return doc.tobytes()


def _frame(page, rect, caption: str, bar_height: float = 13):
    """A framed panel with a shaded caption bar across its top; returns the y below the bar."""
    x0, y0, x1, y1 = rect
    page.draw_rect(pymupdf.Rect(x0, y0, x1, y0 + bar_height), color=None, fill=BAR)
    page.draw_rect(pymupdf.Rect(x0, y0, x1, y1), color=(0.1, 0.1, 0.1), width=0.5)
    label, _, rest = caption.partition("|")
    page.insert_text((x0 + 6, y0 + 9.5), label, fontsize=8, fontname="hebo")
    page.insert_text((x0 + 8 + pymupdf.get_text_length(label, fontname="hebo", fontsize=8), y0 + 9.5), rest,
                     fontsize=8)
    return y0 + bar_height


def _body_outline(page, cx: float, top: float) -> None:
    page.draw_oval(pymupdf.Rect(cx - 18, top, cx + 18, top + 44), color=(0.6, 0.3, 0.4), fill=SKIN)
    for side in (-1, 1):
        page.draw_bezier((cx + side * 18, top + 50), (cx + side * 70, top + 70), (cx + side * 60, top + 200),
                         (cx + side * 30, top + 330), color=(0.6, 0.3, 0.4))
    page.draw_bezier((cx - 30, top + 330), (cx - 10, top + 300), (cx + 10, top + 300), (cx + 30, top + 330),
                     color=(0.6, 0.3, 0.4))


def build_framed_diagram() -> bytes:
    """One framed figure: a body outline with leader labels, a highlighted label, a key below."""
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    box(page, (60, 540), 40, "Invented scars", size=18, bold=True, height=40)
    top = _frame(page, (47, 88, 548, 520), "Figure 15.1|Invented scars and approaches.")
    _body_outline(page, 300, top + 20)
    y = lines(page, 60, top + 30, ["Carotid approach", "(eg: carotid", "endarterectomy)"], size=8, pitch=9.6)
    page.draw_line((128, top + 36), (285, top + 60), width=0.3)
    y = lines(page, 60, y + 4, ["Axillary artery", "approach"], size=8, pitch=9.6)
    page.draw_line((120, y - 12), (270, top + 110), width=0.3)
    lines(page, 60, y + 60, ["Radial-cephalic", "fistula"], size=8, pitch=9.6)
    lines(page, 400, top + 120, ["Median sternotomy", "for cardiac access"], size=8, pitch=9.6)
    page.draw_line((398, top + 126), (310, top + 130), width=0.3)
    page.draw_rect(pymupdf.Rect(420, top + 240, 500, top + 262), color=None, fill=(0.85, 0.76, 0.87))
    lines(page, 426, top + 242, ["Combinations"], size=8)
    lines(page, 55, 500, ["(a)"], size=8)
    page.insert_text((47, 532), "Abbreviations: LSV, long invented vein", fontsize=8)
    return doc.tobytes()


STENOSIS = [["% Stenosis (diameter)", "% Area reduction"], ["30", "50"], ["50", "75"], ["70", "90"]]


def build_ruled_panel_table(grid: bool) -> bytes:
    """A captioned table inside a large frame, with no shaded table cells."""
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    _frame(page, (47, 88, 548, 520), "Table 17.1|Invented diameter and area reduction.")
    for y in ([110, 128, 146, 164, 182] if grid else [110, 128, 182]):
        page.draw_line((53, y), (300, y), width=0.5)
    if grid:
        for x in [53, 175, 300]:
            page.draw_line((x, 110), (x, 182), width=0.5)
    rows = [["Diameter", "Area"], ["30", "50"], ["50", "75"], ["70", "90"]]
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            page.insert_text((65 + c * 122, 122 + r * 18), text, fontsize=9,
                             fontname="hebo" if r == 0 else "helv")
    return doc.tobytes()


def build_panel_table_with_charts() -> bytes:
    """A framed panel: a table beside a formula box, then a captioned chart with axis numbers."""
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    box(page, (60, 540), 40, "Invented ultrasound", size=18, bold=True, height=40)
    page.draw_rect(pymupdf.Rect(47, 88, 548, 520), color=(0.1, 0.1, 0.1), width=0.5)
    # caption bar 1: a table caption that continues in lower case
    page.draw_rect(pymupdf.Rect(47, 88, 548, 101), color=None, fill=BAR)
    page.insert_text((53, 97.5), "Table 17.1", fontsize=8, fontname="hebo")
    page.insert_text((96, 97.5), "percentage (%) reduction in diameter and in invented area.", fontsize=8)
    # the table: header band, body band, two columns
    page.draw_rect(pymupdf.Rect(53, 110, 300, 128), color=None, fill=HEAD)
    page.draw_rect(pymupdf.Rect(53, 128, 300, 182), color=None, fill=BAR)
    for r, row in enumerate(STENOSIS):
        for c, cell in enumerate(row):
            page.insert_text((70 + c * 125, 122 + r * 17 + (2 if r else 0)), cell, fontsize=9,
                             fontname="hebo" if r == 0 else "helv")
    # beside it: a titled box with a fraction formula
    page.draw_rect(pymupdf.Rect(310, 110, 542, 128), color=None, fill=HEAD)
    page.draw_rect(pymupdf.Rect(310, 128, 542, 182), color=None, fill=BAR)
    page.insert_text((330, 122), "Invented diameter reduction calculation", fontsize=9, fontname="hebo")
    _fraction(page, 425, 150, [("Diameter of patent lumen", 425)], "Total vessel diameter",
              left="% reduction = 1 -", right="x 100", size=8)
    # caption bar 2 and its chart
    page.draw_rect(pymupdf.Rect(47, 200, 548, 213), color=None, fill=BAR)
    page.insert_text((53, 209.5), "Figure 17.1", fontsize=8, fontname="hebo")
    page.insert_text((100, 209.5), "Invented waveforms in the femoral artery.", fontsize=8)
    page.draw_rect(pymupdf.Rect(150, 240, 330, 380), color=None, fill=(0.73, 0.9, 0.98))
    page.draw_line((150, 240), (150, 380), width=0.5)
    page.draw_line((150, 330), (330, 330), width=0.5)
    page.draw_bezier((150, 330), (190, 180), (220, 420), (330, 325), color=(0.57, 0.15, 0.56))
    for n, value in enumerate(("20", "10", "0")):
        page.insert_text((136, 262 + n * 34), value, fontsize=6)
    page.insert_text((120, 300), "cm/s", fontsize=6)
    lines(page, 60, 250, ["Normal iliac", "(triphasic femoral", "waveform)"], size=6, pitch=7.2)
    return doc.tobytes()
