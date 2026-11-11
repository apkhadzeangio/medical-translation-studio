"""Regression tests for page designs that broke the parser on a real "at a glance" handbook.

All PDFs are synthetic (tests/pdf_factory.py); no page of the book is in the repository.
"""
import hashlib

import pymupdf
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingest.pdf_layout import Chunk, PageInfo, ParsedDocument, analyse_bytes, review_structure
from app.ingest.store import store_parsed
from app.models import Asset, Block, Chapter, Document, Project, Segment
from app.storage import LocalStorage
from tests import pdf_factory as f


def codes(parsed, severity=None):
    return [i["code"] for i in parsed.issues if severity in (None, i["severity"])]


# ----------------------------------------------------------------------------- columns, lists, footers


def test_footer_across_the_gutter_does_not_mix_the_columns():
    parsed = analyse_bytes(f.build_two_column_steps())
    page = [c for c in parsed.chunks if c.page == 2]
    assert [c.type for c in page] == ["paragraph", "list", "paragraph", "heading", "paragraph", "list", "heading",
                                      "list", "paragraph"]
    assert page[0].text.endswith("It is:") and page[2].text.startswith("The premise of the index")
    assert page[3].text == "Sound patterns" and page[6].text == "Procedure to perform the test"
    assert parsed.pages[1].columns == 2 and codes(parsed) == []


def test_running_foot_with_a_changing_title_is_dropped():
    parsed = analyse_bytes(f.build_two_column_steps())
    assert parsed.dropped == 4
    assert not any("Invented principles" in c.text for c in parsed.chunks)
    assert any(c.text.startswith("Note: if the patient") for c in parsed.chunks)      # body text near it stays


def test_list_runs_on_into_the_next_column_and_steps_stay_in_order():
    parsed = analyse_bytes(f.build_two_column_steps())
    lists = [c for c in parsed.chunks if c.page == 2 and c.type == "list"]
    assert [item.split(":")[0] for item in lists[1].items] == ["· Triphasic", "· Biphasic", "· Monophasic"]
    assert lists[1].items[1].endswith("one more whoosh per heartbeat.")               # wrapped line kept with its item
    assert [item.split()[0] for item in lists[2].items] == ["1", "2", "3", "4", "5", "6"]
    assert lists[2].items[5] == "6 Repeat with the other leg."                        # the note is not part of step 6


# ----------------------------------------------------------------------------- fraction formula


def test_fraction_formula_is_one_block_with_its_parts():
    parsed = analyse_bytes(f.build_fraction())
    assert [c.type for c in parsed.chunks] == ["paragraph", "equation", "paragraph"]
    formula = parsed.chunks[1]
    assert formula.detection == "fraction"
    assert formula.extra == {"left": "", "numerator": "Systolic pedal (DP) pressure",
                             "denominator": "Systolic brachial pressure", "right": "= ABPI"}
    assert formula.text == "(Systolic pedal (DP) pressure) / (Systolic brachial pressure) = ABPI"
    assert codes(parsed) == ["formula_image"] and codes(parsed, "error") == []        # flagged for a check
    assert not any(c.text.strip() in ("(", ")", "( )", "DP") for c in parsed.chunks)  # no loose pieces


def test_formula_is_stored_as_an_image_and_never_sent_to_the_model(migrated_db, tmp_path):
    data = f.build_fraction()
    with Session(migrated_db) as s, pymupdf.open(stream=data, filetype="pdf") as pdf:
        doc = _document(s, data, "fraction")
        store_parsed(s, doc, analyse_bytes(data), pdf=pdf, storage=LocalStorage(tmp_path))
        s.commit()
        block = s.scalars(select(Block).where(Block.document_id == doc.id, Block.type == "equation")).one()
        assert block.data["formula"]["denominator"] == "Systolic brachial pressure" and block.data["needs_review"]
        assert s.get(Asset, block.asset_id).kind == "figure"
        segment = s.scalars(select(Segment).where(Segment.block_id == block.id)).one()
        assert not segment.translatable
        assert doc.meta["parse"]["blocking"] == 0 and doc.meta["parse"]["issues"][0]["code"] == "formula_image"


# ----------------------------------------------------------------------------- framed diagram


def test_framed_diagram_is_a_figure_not_a_table():
    parsed = analyse_bytes(f.build_framed_diagram())
    assert [c.type for c in parsed.chunks] == ["heading", "caption", "figure", "footnote"]
    caption, figure, key = parsed.chunks[1:]
    assert caption.text == "Figure 15.1 Invented scars and approaches."               # label joined to its sentence
    assert figure.caption_id == caption.id and figure.label == "15.1"
    assert figure.labels == ["Carotid approach (eg: carotid endarterectomy)", "Axillary artery approach",
                             "Median sternotomy for cardiac access", "Radial-cephalic fistula", "Combinations", "(a)"]
    assert len(figure.label_boxes) == len(figure.labels)
    assert figure.label_boxes[0][0] < 100 < figure.label_boxes[2][0]                  # where each label is printed
    assert key.text.startswith("Abbreviations:")                                      # the key is kept
    assert parsed.pages[0].tables == 0 and codes(parsed, "error") == []


def test_figure_labels_become_segments_that_keep_their_place(migrated_db, tmp_path):
    data = f.build_framed_diagram()
    with Session(migrated_db) as s, pymupdf.open(stream=data, filetype="pdf") as pdf:
        doc = _document(s, data, "diagram")
        store_parsed(s, doc, analyse_bytes(data), pdf=pdf, storage=LocalStorage(tmp_path))
        s.commit()
        block = s.scalars(select(Block).where(Block.document_id == doc.id, Block.type == "figure")).one()
        labels = {seg.idx: seg.source_text for seg in s.scalars(select(Segment).where(Segment.block_id == block.id))}
        assert labels[0] == "Carotid approach (eg: carotid endarterectomy)" and len(labels) == 5
        assert 5 not in labels                                                         # "(a)" needs no translation
        assert len(block.data["label_boxes"]) == 6 and block.data["caption_block_idx"] == 1
        assert s.get(Asset, block.asset_id).kind == "figure"


# ----------------------------------------------------------------------------- table beside a formula and a chart


def test_table_in_a_panel_keeps_its_rows_and_leaves_the_chart_alone():
    parsed = analyse_bytes(f.build_panel_table_with_charts())
    assert [c.type for c in parsed.chunks] == ["heading", "caption", "table", "heading", "equation", "caption",
                                               "figure"]
    table = parsed.chunks[2]
    assert table.rows == f.STENOSIS                                # 30 -> 50, 50 -> 75, 70 -> 90, row by row
    assert table.header_rows == 1 and table.label == "17.1" and table.caption_id == parsed.chunks[1].id
    assert parsed.chunks[1].text.startswith("Table 17.1 percentage (%) reduction")    # a caption despite lower case
    formula = parsed.chunks[4]
    assert formula.extra["numerator"] == "Diameter of patent lumen" and formula.extra["right"] == "x 100"
    figure = parsed.chunks[6]
    assert figure.caption_id == parsed.chunks[5].id
    assert figure.labels == ["Normal iliac (triphasic femoral waveform)", "20", "10", "cm/s", "0"]
    cells = {cell for row in table.rows for cell in row}
    assert not cells & {"20", "10", "0", "cm/s", "Total vessel diameter"}             # nothing leaked into the table
    assert codes(parsed, "error") == []


# ----------------------------------------------------------------------------- warnings and the translation gate


def _parsed(chunks):
    return ParsedDocument(pages=[PageInfo(1, 595, 842)], chunks=chunks, chapters=[], body_size=10)


def _chunk(kind, text="", **more):
    return Chunk(id=f"c{len(text)}{kind}", page=1, bbox=(0, 0, 10, 10), text=text, size=10, bold=False, lines=1,
                 type=kind, **more)


def test_damaged_structure_is_reported_as_an_error():
    ragged = _chunk("table", rows=[["Aorto", ""], ["", "bifemoral"], ["pp (eg", ""], ["p", "varix"]], caption_id="x")
    parsed = _parsed([ragged, _chunk("paragraph", "( )"), _chunk("caption", "Table 3.1 Invented title")])
    review_structure(parsed)
    assert sorted(codes(parsed, "error")) == ["stray_symbols", "table_not_found", "table_structure"]
    assert all(w.startswith("p1: ") for w in parsed.warnings)


def test_sound_structure_reports_nothing():
    table = _chunk("table", rows=[["a", "b"], ["1", "2"]], caption_id="cap")
    caption = _chunk("caption", "Table 1.1 Invented")
    caption.id = "cap"
    parsed = _parsed([_chunk("paragraph", "Plain text."), caption, table])
    review_structure(parsed)
    assert parsed.issues == []


def _document(session, data, name, meta=None):
    project = Project(name=name)
    session.add(project)
    session.flush()
    doc = Document(project_id=project.id, filename=f"{name}.pdf", format="pdf", storage_key="k", status="parsed",
                   sha256=hashlib.sha256(data + name.encode() + str(project.id).encode()).hexdigest(), meta=meta)
    session.add(doc)
    session.flush()
    return doc


def test_translation_is_held_back_while_the_structure_is_damaged(client, migrated_db):
    issue = {"page": 2, "code": "reading_order", "severity": "error", "message": "columns could not be separated"}
    note = {"page": 5, "code": "formula_image", "severity": "warning", "message": "formula kept as image"}
    with Session(migrated_db) as s:
        doc = _document(s, b"gate", "gate", {"parse": {"issues": [issue, note], "blocking": 1}})
        damaged = Chapter(document_id=doc.id, idx=0, title="one", page_start=1, page_end=2)
        sound = Chapter(document_id=doc.id, idx=1, title="two", page_start=3, page_end=6)
        s.add_all([damaged, sound])
        s.commit()
        doc_id, damaged_id, sound_id = doc.id, damaged.id, sound.id

    refused = client.post(f"/api/chapters/{damaged_id}/translate")
    assert refused.status_code == 409
    assert refused.json()["detail"]["issues"] == [issue]                   # only what makes it unsafe
    assert client.post(f"/api/documents/{doc_id}/translate").status_code == 409
    assert client.post(f"/api/chapters/{sound_id}/translate").status_code == 200       # a warning does not block
    assert client.post(f"/api/chapters/{damaged_id}/translate?force=true").status_code == 200
    assert client.post(f"/api/documents/{doc_id}/translate?force=true").status_code == 200
