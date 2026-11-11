import hashlib
import json

import httpx
import pymupdf
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.ingest.pdf_layout import Chunk, _line_text, analyse_bytes, join_lines, reading_order
from app.ingest.pdf_structure import StructureError, make_reorder, validate
from app.ingest.store import store_parsed
from app.llm.gateway import LLMGateway
from app.models import Block, Chapter, Document, LLMCall, Project, Segment
from tests import pdf_factory as f


def numbers(parsed):
    """Paragraph numbers in reading order (filler sentences are tagged 'Sentence N.i')."""
    return [int(c.text.split()[1].split(".")[0]) for c in parsed.chunks if c.text.startswith("Sentence")]


def test_two_columns_read_down_then_across():
    parsed = analyse_bytes(f.build_book())
    assert numbers(parsed) == list(range(1, 25))
    assert all(p.columns == 2 for p in parsed.pages)


def test_running_heads_and_page_numbers_are_dropped():
    parsed = analyse_bytes(f.build_book())
    text = " ".join(c.text for c in parsed.chunks)
    assert "Synthetic Handbook" not in text
    assert parsed.dropped == 8
    assert not any(c.text.strip().isdigit() for c in parsed.chunks)


def test_headings_and_chapters():
    parsed = analyse_bytes(f.build_book())
    heads = [c for c in parsed.chunks if c.type == "heading"]
    assert [h.text for h in heads] == ["CHAPTER 1 Arterial Layout Basics", "CHAPTER 2 Venous Layout Basics"]
    assert all(h.level == 1 for h in heads)
    assert [(c.title, c.page_start, c.page_end) for c in parsed.chapters] == [
        ("CHAPTER 1 Arterial Layout Basics", 1, 2), ("CHAPTER 2 Venous Layout Basics", 3, 4)]


def test_pdf_outline_wins_for_chapters():
    doc = pymupdf.open(stream=f.build_book(), filetype="pdf")
    doc.set_toc([[1, "Outline One", 1], [2, "sub", 2], [1, "Outline Two", 3]])
    parsed = analyse_bytes(doc.tobytes())
    assert [c.title for c in parsed.chapters] == ["Outline One", "Outline Two"]
    assert parsed.chunks[parsed.chapters[1].first_chunk].page == 3


def test_paragraph_continues_across_columns_and_is_dehyphenated():
    parsed = analyse_bytes(f.build_continuation())
    joined = [c for c in parsed.chunks if "left column" in c.text]
    assert len(joined) == 1
    assert "in the middle of a word so that it must be joined" in joined[0].text
    assert len(joined[0].parts) == 1
    assert len(parsed.chunks) == 3


def test_block_types_on_mixed_page():
    parsed = analyse_bytes(f.build_mixed())
    kinds = {c.type: c.text for c in parsed.chunks}
    assert kinds["caption"].startswith("Figure 1.1")
    assert kinds["list_item"].startswith("- First")
    assert kinds["reference"].startswith("1. Sample AB")
    assert kinds["footnote"].startswith("* Invented footnote")
    assert [c.text for c in parsed.chunks if c.type == "heading"] == ["Imaging Follow-up", "References"]
    assert parsed.pages[0].complex and parsed.pages[0].image_area > 0.02


def test_source_text_is_exact():
    parsed = analyse_bytes(f.build_mixed())
    assert any(c.text == "- First invented bullet point with 5 mg of nothing." for c in parsed.chunks)


def test_scanned_page_is_reported():
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 50, 70), False)
    pix.set_rect(pix.irect, (200, 200, 200))
    page.insert_image(page.rect, pixmap=pix)
    parsed = analyse_bytes(doc.tobytes())
    assert parsed.pages[0].scanned and "OCR" in parsed.warnings[0]


def test_superscripts_and_ligatures_in_lines():
    span = {"size": 10, "flags": 0, "font": "Times"}
    line = {"spans": [{**span, "text": "arterial ﬂow in disease"},
                      {**span, "size": 6, "flags": 1, "text": "12,13"},
                      {**span, "text": " improves"}]}
    assert _line_text(line) == ("arterial flow in disease<sup>12,13</sup> improves", 10.0, False, "Times")
    assert join_lines(["a well-", "known case", "of follow-", "Up and 5-", "10 mm"]) == \
        "a wellknown case of follow- Up and 5- 10 mm"


def test_three_columns_and_spanning_block():
    def c(i, x0, y0, x1, y1):
        return Chunk(id=str(i), page=1, bbox=(x0, y0, x1, y1), text=str(i), size=10, bold=False, lines=3)
    chunks = [c(5, 400, 200, 540, 400), c(1, 60, 100, 540, 130), c(3, 60, 300, 200, 400),
              c(4, 230, 200, 370, 400), c(2, 60, 200, 200, 290), c(6, 60, 420, 540, 450),
              c(8, 310, 470, 540, 600), c(7, 60, 470, 290, 600)]
    assert [k.id for k in reading_order(chunks)] == list("12345678")


# ----------------------------------------------------------------------------- model step


def gateway(engine, answer):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        content = answer(seen[-1]) if callable(answer) else answer
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}],
                                         "usage": {"prompt_tokens": 900, "completion_tokens": 60}})

    settings = Settings(llm_api_keys="sk-test", llm_base_url="https://llm.test/v1", llm_max_attempts=1)
    return LLMGateway(engine, settings, transport=httpx.MockTransport(handler), sleep=lambda s: None), seen


def test_validate_rejects_lost_duplicated_or_invented_ids():
    ids = ["a", "b", "c"]
    assert validate({"order": ["c", "a"], "drop": ["b"], "types": {"a": "caption"}}, ids)[0] == ["c", "a"]
    for bad in ({"order": ["a", "b"]}, {"order": ["a", "b", "c", "c"]}, {"order": ["a", "b", "c", "zz"]},
                {"order": ["a", "b", "c"], "types": {"a": "poem"}}, ["a", "b", "c"]):
        with pytest.raises(StructureError):
            validate(bad, ids)


def test_model_reorders_complex_page_without_touching_text(migrated_db):
    baseline = analyse_bytes(f.build_mixed())
    ids = [c.id for c in baseline.chunks]
    caption = next(c.id for c in baseline.chunks if c.type == "caption")
    moved = [i for i in ids if i != caption] + [caption]
    gw, seen = gateway(migrated_db, {"order": moved, "types": {caption: "other"}, "drop": []})

    parsed = analyse_bytes(f.build_mixed(), make_reorder(gw))
    assert [c.id for c in parsed.chunks] == moved
    assert parsed.chunks[-1].type == "other"
    assert {c.id: c.text for c in parsed.chunks} == {c.id: c.text for c in baseline.chunks}
    content = seen[0]["messages"][1]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    with Session(migrated_db) as s:
        assert s.scalar(select(func.count()).select_from(LLMCall).where(LLMCall.stage == "pdf_structure")) >= 1


def test_bad_model_answer_falls_back_to_deterministic_order(migrated_db):
    baseline = analyse_bytes(f.build_mixed())
    gw, _ = gateway(migrated_db, {"order": [baseline.chunks[0].id], "drop": []})
    parsed = analyse_bytes(f.build_mixed(), make_reorder(gw))
    assert [c.id for c in parsed.chunks] == [c.id for c in baseline.chunks]
    assert "structure model failed" in parsed.warnings[0]


def test_simple_pages_never_call_the_model(migrated_db):
    gw, seen = gateway(migrated_db, {"order": [], "drop": []})
    analyse_bytes(f.build_book(), make_reorder(gw))
    assert seen == []


# ----------------------------------------------------------------------------- storage


def test_store_parsed_writes_chapters_blocks_segments(migrated_db):
    data = f.build_book()
    parsed = analyse_bytes(data)
    with Session(migrated_db) as s:
        project = Project(name="ingest")
        s.add(project)
        s.flush()
        doc = Document(project_id=project.id, filename="book.pdf", format="pdf", storage_key="k",
                       sha256=hashlib.sha256(data).hexdigest())
        s.add(doc)
        s.flush()
        stats = store_parsed(s, doc, parsed)
        s.commit()
        assert stats == {"chapters": 2, "blocks": 26, "segments": 26, "assets": 0}
        assert (doc.status, doc.page_count) == ("parsed", 4)
        assert doc.meta["parse"]["dropped_running_heads"] == 8

        chapters = list(s.scalars(select(Chapter).where(Chapter.document_id == doc.id).order_by(Chapter.idx)))
        per_chapter = [s.scalar(select(func.count()).select_from(Block).where(Block.chapter_id == c.id))
                       for c in chapters]
        assert per_chapter == [13, 13]
        first = s.scalars(select(Block).where(Block.document_id == doc.id).order_by(Block.idx)).first()
        assert first.type == "heading" and first.page_no == 1 and set(first.bbox) == {"x0", "y0", "x1", "y1"}
        seg = s.scalars(select(Segment).where(Segment.block_id == first.id)).one()
        assert seg.source_text == "CHAPTER 1 Arterial Layout Basics" and len(seg.source_hash) == 64

        with pytest.raises(ValueError, match="already has"):
            store_parsed(s, doc, parsed)
        assert store_parsed(s, doc, parsed, replace=True)["blocks"] == 26
        s.commit()
        assert s.scalar(select(func.count()).select_from(Block).where(Block.document_id == doc.id)) == 26
