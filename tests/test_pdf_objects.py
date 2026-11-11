import hashlib

import pymupdf
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ingest.pdf_layout import analyse_bytes
from app.ingest.store import store_parsed
from app.models import Asset, Block, Document, Project, Segment
from app.storage import LocalStorage
from tests import pdf_factory as f


def kinds(parsed):
    return [c.type for c in parsed.chunks]


@pytest.mark.parametrize("grid", [True, False])
def test_table_is_one_block_with_exact_cells(grid):
    parsed = analyse_bytes(f.build_table(grid))
    assert kinds(parsed) == ["paragraph", "caption", "table", "paragraph"]
    table = parsed.chunks[2]
    assert table.rows == f.TABLE
    assert table.caption_id == parsed.chunks[1].id
    flow = " ".join(c.text for c in parsed.chunks)
    assert "36 months" not in flow and "Diameter" not in flow      # cell text left the paragraph flow
    assert parsed.pages[0].tables == 1 and parsed.pages[0].complex


def test_table_detection_route():
    assert next(c for c in analyse_bytes(f.build_table(True)).chunks if c.type == "table").detection == "bands"
    booktabs = next(c for c in analyse_bytes(f.build_table(False)).chunks if c.type == "table")
    assert booktabs.detection == "rules" and booktabs.header_rows == 1 and booktabs.label == "2.1"


def test_raster_figure_links_to_its_caption():
    parsed = analyse_bytes(f.build_mixed())
    figure = next(c for c in parsed.chunks if c.type == "figure")
    caption = next(c for c in parsed.chunks if c.type == "caption")
    assert figure.detection == "image" and figure.caption_id == caption.id
    assert figure.bbox[0] == pytest.approx(60, abs=1) and figure.bbox[2] == pytest.approx(300, abs=1)
    assert parsed.chunks.index(caption) == parsed.chunks.index(figure) + 1


def test_vector_figure_absorbs_its_labels():
    parsed = analyse_bytes(f.build_vector_figure())
    assert kinds(parsed) == ["paragraph", "figure", "caption", "paragraph"]
    figure = parsed.chunks[1]
    assert figure.detection == "vector"
    assert sorted(figure.labels) == ["120%", "40%", "70%", "90%"]
    assert figure.caption_id == parsed.chunks[2].id


def test_plain_text_pages_have_no_objects():
    parsed = analyse_bytes(f.build_book())
    assert not any(c.type in ("table", "figure") for c in parsed.chunks)
    assert not any(p.complex for p in parsed.pages)


def _document(session, data, name):
    project = Project(name=name)
    session.add(project)
    session.flush()
    doc = Document(project_id=project.id, filename=f"{name}.pdf", format="pdf", storage_key="k",
                   sha256=hashlib.sha256(data + name.encode()).hexdigest())
    session.add(doc)
    session.flush()
    return doc


def test_store_table_cells_and_rendered_assets(migrated_db, tmp_path):
    data = f.build_table()
    storage = LocalStorage(tmp_path)
    with Session(migrated_db) as s, pymupdf.open(stream=data, filetype="pdf") as pdf:
        doc = _document(s, data, "table")
        stats = store_parsed(s, doc, analyse_bytes(data), pdf=pdf, storage=storage)
        s.commit()
        assert stats == {"chapters": 1, "blocks": 4, "segments": 3 + 12, "assets": 1}

        block = s.scalars(select(Block).where(Block.document_id == doc.id, Block.type == "table")).one()
        assert block.data["rows"] == f.TABLE and block.data["cols"] == 3
        assert block.data["caption_block_idx"] == 1
        cells = {seg.idx: seg.source_text for seg in s.scalars(select(Segment).where(Segment.block_id == block.id))}
        assert cells[0] == "Diameter" and cells[1 * 3 + 1] == "36 months" and len(cells) == 12

        asset = s.get(Asset, block.asset_id)
        png = storage.get(asset.storage_key)
        assert asset.kind == "table_image" and png.startswith(b"\x89PNG") and asset.width > 1000
        assert doc.meta["parse"]["tables"] == 1

        store_parsed(s, doc, analyse_bytes(data), pdf=pdf, storage=storage, replace=True)
        s.commit()
        assert s.scalar(select(func.count()).select_from(Asset).where(Asset.document_id == doc.id)) == 1
        assert len(list((tmp_path / "documents" / str(doc.id) / "assets").iterdir())) == 1


def test_store_figure_has_asset_and_no_segment(migrated_db, tmp_path):
    data = f.build_vector_figure()
    with Session(migrated_db) as s, pymupdf.open(stream=data, filetype="pdf") as pdf:
        doc = _document(s, data, "figure")
        stats = store_parsed(s, doc, analyse_bytes(data), pdf=pdf, storage=LocalStorage(tmp_path))
        s.commit()
        assert stats["assets"] == 1 and stats["segments"] == 3
        block = s.scalars(select(Block).where(Block.document_id == doc.id, Block.type == "figure")).one()
        assert sorted(block.data["labels"]) == ["120%", "40%", "70%", "90%"]
        assert s.get(Asset, block.asset_id).kind == "figure"
        assert s.scalar(select(func.count()).select_from(Segment).where(Segment.block_id == block.id)) == 0


def test_storage_rejects_path_traversal(tmp_path):
    storage = LocalStorage(tmp_path)
    for key in ("../x", "/etc/x", "a/../../x", ""):
        with pytest.raises(ValueError):
            storage.put(key, b"x")
