"""Regression tests for page designs that broke the parser on a real "at a glance" handbook.

All PDFs are synthetic (tests/pdf_factory.py); no page of the book is in the repository.
"""
import hashlib

import pymupdf
import pytest
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


@pytest.mark.parametrize("grid", [False, True])
def test_unshaded_ruled_table_inside_a_captioned_frame(grid):
    parsed = analyse_bytes(f.build_ruled_panel_table(grid))
    tables = [c for c in parsed.chunks if c.type == "table"]
    assert len(tables) == 1
    assert tables[0].rows == [["Diameter", "Area"], ["30", "50"], ["50", "75"], ["70", "90"]]
    assert tables[0].caption_id is not None
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


def test_merged_table_cells_are_complete_when_the_span_covers_them():
    table = _chunk("table", rows=[["Category", "Description"], ["A", "One"], ["", "Two"],
                                 ["", "Three"], ["", "Four"]], caption_id="cap",
                   spans=[{"row_start": 1, "row_end": 4, "col_start": 0, "col_end": 0}])
    parsed = _parsed([table])
    review_structure(parsed)
    assert codes(parsed, "error") == []
    table.spans = []
    damaged = _parsed([table])
    review_structure(damaged)
    assert codes(damaged, "error") == ["table_structure"]


@pytest.mark.parametrize("outcome", ["accepted", "declined", "failed"])
def test_only_accepted_model_reordering_resolves_fallback_uncertainty(monkeypatch, outcome):
    from app.ingest import pdf_layout

    original = pdf_layout.reading_order

    def uncertain(chunks, notes=None):
        result = original(chunks)
        if notes is not None:
            notes.append("uncertain")
        return result

    def reorder(page, info, chunks):
        if outcome == "failed":
            raise RuntimeError("unavailable")
        return list(chunks) if outcome == "accepted" else None

    monkeypatch.setattr(pdf_layout, "reading_order", uncertain)
    parsed = analyse_bytes(f.build_framed_diagram(), reorder)
    assert ("reading_order" in codes(parsed, "error")) == (outcome != "accepted")


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


# ----------------------------------------------------------------------------- the model decides structure


def _model(engine, answer):
    """A gateway whose provider answers with `answer(request)`; returns it and the requests seen."""
    import json

    import httpx

    from app.config import Settings
    from app.llm.gateway import LLMGateway
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer(seen[-1]))}}],
                                         "usage": {"prompt_tokens": 900, "completion_tokens": 60}})

    settings = Settings(llm_api_keys="sk-test", llm_base_url="https://llm.test/v1", llm_max_attempts=1)
    return LLMGateway(engine, settings, transport=httpx.MockTransport(handler), sleep=lambda s: None), seen


def test_model_makes_a_formula_translatable_only_if_its_words_match(migrated_db):
    from app.ingest.pdf_structure import make_reorder
    baseline = analyse_bytes(f.build_fraction())
    ids = [c.id for c in baseline.chunks]
    formula = baseline.chunks[1].id
    good = "ABPI = (Systolic pedal (DP) pressure) / (Systolic brachial pressure)"

    gw, seen = _model(migrated_db, lambda _: {"order": ids, "formulas": [{"ids": [formula], "text": good}]})
    parsed = analyse_bytes(f.build_fraction(), make_reorder(gw))
    assert len(seen) == 1                                           # a page with a formula goes to the model
    assert parsed.chunks[1].text == good and parsed.chunks[1].extra["verified"]
    assert codes(parsed) == ["formula_text"] and parsed.pages[0].read_by_model

    wrong = good.replace("brachial", "radial")                       # a word that is not on the page
    gw, _ = _model(migrated_db, lambda _: {"order": ids, "formulas": [{"ids": [formula], "text": wrong}]})
    parsed = analyse_bytes(f.build_fraction(), make_reorder(gw))
    assert parsed.chunks[1].text == baseline.chunks[1].text and "verified" not in parsed.chunks[1].extra
    assert codes(parsed) == ["formula_image"]
    assert any("does not match its printed words" in w for w in parsed.warnings)


def test_model_turns_a_false_table_into_a_figure_and_collects_labels(migrated_db):
    from app.ingest.pdf_structure import make_reorder
    baseline = analyse_bytes(f.build_table())
    table = next(c for c in baseline.chunks if c.type == "table")
    para = baseline.chunks[-1]
    order = [c.id for c in baseline.chunks if c is not para]
    gw, _ = _model(migrated_db, lambda _: {"order": order, "not_tables": [table.id],
                                           "figure_labels": {table.id: [para.id]}})
    parsed = analyse_bytes(f.build_table(), make_reorder(gw))
    figure = next(c for c in parsed.chunks if c.id == table.id)
    assert figure.type == "figure" and figure.rows is None and figure.detection == "model"
    assert figure.labels[:3] == ["Diameter", "Interval", "Note"] and figure.labels[-1] == para.text
    assert len(figure.label_boxes) == len(figure.labels)
    assert para.id not in [c.id for c in parsed.chunks] and parsed.pages[0].tables == 0
    assert {c.text for c in baseline.chunks if c.text} >= {c.text for c in parsed.chunks if c.text}   # no new text


def test_model_may_rearrange_table_cells_but_not_change_them(migrated_db):
    from app.ingest.pdf_structure import make_reorder
    baseline = analyse_bytes(f.build_table())
    ids = [c.id for c in baseline.chunks]
    table = next(c for c in baseline.chunks if c.type == "table")
    swapped = [f.TABLE[0], f.TABLE[2], f.TABLE[1], f.TABLE[3]]
    gw, _ = _model(migrated_db, lambda _: {"order": ids, "table_rows": {table.id: swapped}})
    assert next(c for c in analyse_bytes(f.build_table(), make_reorder(gw)).chunks if c.type == "table").rows == swapped

    edited = [row[:] for row in f.TABLE]
    edited[1][1] = "63 months"                                       # a number the page does not have
    gw, _ = _model(migrated_db, lambda _: {"order": ids, "table_rows": {table.id: edited}})
    parsed = analyse_bytes(f.build_table(), make_reorder(gw))
    assert next(c for c in parsed.chunks if c.type == "table").rows == f.TABLE
    assert any("do not hold the same text" in w for w in parsed.warnings)


def test_model_merges_split_pieces_and_every_page_mode_asks_about_plain_pages(migrated_db):
    import re

    from app.ingest.pdf_structure import make_reorder

    def answer(request):                     # merge the first two chunks the model was shown
        ids = re.findall(r"'id': '(\w+)'", request["messages"][1]["content"][0]["text"])
        return {"order": [ids[0]] + ids[2:], "merge": [ids[:2]]}

    plain = f.build_book()                   # text pages only: nothing the geometry calls complex
    baseline = analyse_bytes(plain)
    gw, seen = _model(migrated_db, answer)
    assert len(analyse_bytes(plain, make_reorder(gw)).chunks) == len(baseline.chunks) and seen == []

    parsed = analyse_bytes(plain, make_reorder(gw), every_page=True)
    assert len(seen) == 4 and all(p.read_by_model for p in parsed.pages)
    first, second = baseline.chunks[0], baseline.chunks[1]
    assert parsed.chunks[0].text == f"{first.text} {second.text}"                  # joined, not rewritten
    assert " ".join(c.text for c in parsed.chunks) == " ".join(c.text for c in baseline.chunks)


def test_an_answer_that_loses_or_misuses_an_id_is_refused():
    import pytest

    from app.ingest.pdf_structure import StructureError, validate
    kinds = {"a": "paragraph", "b": "paragraph", "t": "table", "g": "figure"}
    ids = list(kinds)
    plan = validate({"order": ["a", "t", "g"], "figure_labels": {"g": ["b"]}}, ids, kinds)
    assert plan.figure_labels == {"g": ["b"]}
    for bad in ({"order": ["a", "t", "g"]},                                             # b is lost
                {"order": ["a", "b", "t", "g"], "figure_labels": {"g": ["b"]}},        # b twice
                {"order": ["a", "b", "g"], "figure_labels": {"g": ["t"]}},             # a table as a label
                {"order": ["a", "b", "t", "g"], "not_tables": ["g"]},                  # not a table
                {"order": ["a", "t", "g"], "merge": [["b", "a"]]},                     # head not in the order
                {"order": ["a", "b", "t", "g"], "table_rows": {"g": [["x"]]}}):
        with pytest.raises(StructureError):
            validate(bad, ids, kinds)


def test_structure_mode_setting_accepts_the_old_switch():
    from app.config import Settings
    assert Settings().pdf_structure_model == "all"
    assert Settings(pdf_structure_model="true").pdf_structure_model == "all"
    assert Settings(pdf_structure_model="false").pdf_structure_model == "off"
    assert Settings(pdf_structure_model="complex").pdf_structure_model == "complex"
