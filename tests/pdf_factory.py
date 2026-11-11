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
