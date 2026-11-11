import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pymupdf
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.ingest.pdf_layout import analyse
from app.ingest.store import store_parsed
from app.llm.gateway import LLMGateway
from app.models import Chapter, Document, Job, LLMCall, Project, QAFlag, Segment, SegmentVersion, TMEntry
from app.pipeline import translate
from app.pipeline.checks import check_segment
from app.terminology import GlossaryIndex, Term, tm_add, upsert_terms
from app.worker import Worker, default_handlers

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_vascular_chapter.pdf"


def checks(source, target, glossary=None):
    return sorted(f.check for f in check_segment(source, target, glossary))


def test_checks_catch_what_matters():
    src = "The index fell to ≤ 0.80 in 3 of 12 records (25%), 5 × 10<sup>3</sup> cells; no occlusion.<sup>4</sup>"
    good = "ინდექსი დაეცა ≤ 0.80-მდე 12-დან 3 ჩანაწერში (25%), 5 × 10<sup>3</sup> უჯრედი; ოკლუზია არ იყო.<sup>4</sup>"
    assert checks(src, good) == []
    assert checks(src, good.replace("0.80", "0.8")) == ["numbers"]
    assert checks(src, good.replace("≤", "≥")) == ["symbols"]
    # a flattened exponent is both lost markup and a different number (10 and 3 became 103)
    assert checks(src, good.replace("<sup>3</sup>", "3")) == ["markup", "numbers"]
    assert checks(src, good.replace("<sup>4</sup>", "<sub>4</sub>")) == ["markup"]
    assert checks(src, good.replace("(25%)", "")) == ["numbers", "symbols"]
    assert checks("1,250 units", "1.250 ერთეული") == ["numbers"]
    assert checks("A parser should keep the page image.", "A parser should keep the page image.") == ["untranslated"]
    assert checks("T001", "T001") == [] and checks("0.05", "0.05") == []
    assert checks("Some text here", "  ") == ["empty"]
    glossary = GlossaryIndex([Term("endoleak", "ენდოლიკი")])
    assert checks("An endoleak was seen.", "გაჟონვა გამოვლინდა.", glossary) == ["glossary"]
    assert checks("An endoleak was seen.", "ენდოლიკი გამოვლინდა.", glossary) == []


def fake_model(mutate=lambda sid, text: text, drop=()):
    """A provider that 'translates' by prefixing Georgian text; records every request."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payload = json.loads(body["messages"][1]["content"])
        seen.append(payload)
        out = [{"id": s["id"], "ka": mutate(s["id"], "თარგმანი: " + s["en"])}
               for s in payload["segments"] if s["id"] not in drop]
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({"segments": out}, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 2000, "completion_tokens": 4000}})

    return handler, seen


@pytest.fixture()
def chapter(migrated_db, monkeypatch):
    """The fixture chapter parsed into a fresh project; returns (engine, project_id, chapter_id, install)."""
    data = FIXTURE.read_bytes()
    with Session(migrated_db) as s, pymupdf.open(stream=data, filetype="pdf") as pdf:
        s.execute(update(Job).where(Job.status.in_(("queued", "running"))).values(status="cancelled"))
        project = Project(name="translate", budget_usd=Decimal("50"))
        s.add(project)
        s.flush()
        doc = Document(project_id=project.id, filename="c.pdf", format="pdf", storage_key="k",
                       sha256=hashlib.sha256(data + str(project.id).encode()).hexdigest())
        s.add(doc)
        s.flush()
        store_parsed(s, doc, analyse(pdf))
        s.commit()
        chapter_id = s.scalars(select(Chapter.id).where(Chapter.document_id == doc.id)).first()
        ids = (migrated_db, project.id, chapter_id)

    def install(handler, **overrides):
        settings = Settings(llm_api_keys="sk-test", llm_base_url="https://llm.test/v1", llm_max_attempts=1,
                            **overrides)
        gw = LLMGateway(migrated_db, settings, transport=httpx.MockTransport(handler), sleep=lambda s: None)
        monkeypatch.setattr(translate, "get_gateway", lambda session: gw)
        return gw

    return (*ids, install)


def run_all(engine):
    worker, results = Worker(engine, default_handlers()), []
    while (status := worker.run_once()) is not None:
        results.append(status)
        if len(results) > 50:
            break
    return results


def test_chapter_is_translated_end_to_end(chapter):
    engine, project_id, chapter_id, install = chapter
    handler, seen = fake_model()
    install(handler)
    with Session(engine) as s:
        upsert_terms(s, project_id, [("reconstruction", "რეკონსტრუქცია", None), ("endoleak", "ენდოლიკი", None)])
        plan = translate.plan_chapter(s, chapter_id, max_words=500)
        again = translate.plan_chapter(s, chapter_id, max_words=500)
        s.commit()
    assert plan["chunks"] >= 3 and plan["jobs_queued"] == plan["chunks"] and plan["estimated_cost_usd"] > 0
    assert again["chunks"] == plan["chunks"]                     # planning twice adds no jobs

    assert run_all(engine) == ["done"] * plan["chunks"]
    with Session(engine) as s:
        translatable = s.scalar(select(func.count()).select_from(Segment).where(
            Segment.chapter_id == chapter_id, Segment.translatable))
        current = s.scalar(select(func.count()).select_from(SegmentVersion).join(Segment).where(
            Segment.chapter_id == chapter_id, SegmentVersion.is_current))
        assert current == translatable
        assert s.get(Chapter, chapter_id).status == "reviewing"
        # references, the equation and the chapter numeral are left as printed
        skipped = s.scalar(select(func.count()).select_from(Segment).where(
            Segment.chapter_id == chapter_id, ~Segment.translatable))
        assert skipped == 8 + 1 + 1
        calls = list(s.scalars(select(LLMCall).where(LLMCall.project_id == project_id)))
        assert len(calls) == plan["chunks"] and all(c.stage == "translate" and c.job_id for c in calls)
        assert s.scalar(select(func.count()).select_from(TMEntry).where(TMEntry.project_id == project_id)) > 50

    first = seen[0]
    assert first["chapter"].startswith("17 Infrainguinal Reconstruction")
    assert {"en": "reconstruction", "ka": "რეკონსტრუქცია"} in first["glossary"]
    assert not any(g["en"] == "endoleak" for payload in seen for g in payload["glossary"])   # not in the text
    kinds = {seg["kind"] for payload in seen for seg in payload["segments"]}
    assert {"heading", "paragraph", "table_cell", "caption", "callout", "list_item", "footnote"} <= kinds
    assert seen[1]["context_before"]                              # later chunks see what came before
    # repeated table cells ("present", "R", "L", header rows) are sent to the model at most once per chunk run
    sent = [seg["en"] for payload in seen for seg in payload["segments"]]
    assert sent.count("Baseline only") <= len(seen)


def test_translation_memory_saves_model_calls(chapter):
    engine, project_id, chapter_id, install = chapter
    handler, seen = fake_model()
    install(handler)
    with Session(engine) as s:
        tm_add(s, project_id, "Anatomic description", "ანატომიური აღწერა", "human")
        translate.plan_chapter(s, chapter_id)
        s.commit()
    run_all(engine)
    assert "Anatomic description" not in [seg["en"] for p in seen for seg in p["segments"]]
    with Session(engine) as s:
        version = s.execute(select(SegmentVersion).join(Segment).where(
            Segment.chapter_id == chapter_id, Segment.source_text == "Anatomic description")).scalar_one()
        assert (version.text, version.model, version.llm_call_id) == ("ანატომიური აღწერა", "translation-memory", None)


def test_changed_number_is_flagged_and_kept_out_of_memory(chapter):
    engine, project_id, chapter_id, install = chapter
    handler, _ = fake_model(mutate=lambda sid, text: text.replace("0.05", "0.5"))
    install(handler)
    with Session(engine) as s:
        translate.plan_chapter(s, chapter_id)
        s.commit()
    run_all(engine)
    with Session(engine) as s:
        flagged = list(s.scalars(select(Segment).where(Segment.chapter_id == chapter_id, Segment.status == "flagged")))
        assert flagged and all("0.05" in seg.source_text for seg in flagged)
        flags = list(s.scalars(select(QAFlag).where(QAFlag.segment_id.in_([f.id for f in flagged]))))
        assert {f.check_name for f in flags} == {"numbers"} and all(f.severity == "critical" for f in flags)
        bad = set(s.scalars(select(TMEntry.source_text).where(TMEntry.project_id == project_id)))
        assert not bad & {seg.source_text for seg in flagged}


def test_partial_answer_saves_nothing_and_retry_only_pays_for_the_rest(chapter):
    engine, project_id, chapter_id, install = chapter
    install(fake_model(drop={"s2"})[0])
    with Session(engine) as s:
        translate.plan_chapter(s, chapter_id, max_words=100000)       # one chunk
        s.commit()
    assert run_all(engine) == ["queued"]                              # failed once, will retry
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(SegmentVersion).join(Segment).where(
            Segment.chapter_id == chapter_id)) == 0
        job = s.execute(select(Job).where(Job.project_id == project_id, Job.kind == "translate_chunk")).scalar_one()
        assert "missing 1 of" in job.last_error
        job.run_after = datetime.now(UTC)
        # half of the chunk gets translated by other means before the retry
        half = list(s.scalars(select(Segment).where(Segment.chapter_id == chapter_id, Segment.translatable)))[::2]
        for seg in half:
            s.add(SegmentVersion(segment_id=seg.id, stage="human", text="ხელით", is_current=True))
            seg.status = "approved"
        s.commit()
        total = s.scalar(select(func.count()).select_from(Segment).where(
            Segment.chapter_id == chapter_id, Segment.translatable))
    handler, seen = fake_model()
    install(handler)
    assert run_all(engine) == ["done"]
    assert len(seen) == 1 and len(seen[0]["segments"]) <= total - len(half)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(SegmentVersion).join(Segment).where(
            Segment.chapter_id == chapter_id, SegmentVersion.is_current)) == total


def test_budget_stop_pauses_jobs(chapter):
    engine, project_id, chapter_id, install = chapter
    install(fake_model()[0])
    with Session(engine) as s:
        s.get(Project, project_id).budget_usd = Decimal("0.30")        # one call costs $0.22
        translate.plan_chapter(s, chapter_id, max_words=400)
        s.commit()
    results = run_all(engine)
    assert results.count("done") == 2 and set(results[2:]) == {"deferred"}
    with Session(engine) as s:
        paused = list(s.scalars(select(Job).where(Job.project_id == project_id, Job.status == "queued")))
        assert paused and all("budget" in j.last_error and j.attempts == 0 for j in paused)


def test_translation_api(chapter, client, monkeypatch):
    engine, project_id, chapter_id, install = chapter
    install(fake_model(mutate=lambda sid, text: text.replace("1.20", "1.2"))[0])
    plan = client.post(f"/api/chapters/{chapter_id}/translate").json()
    assert plan["chunks"] >= 1 and plan["estimate_is_rough"] is True
    run_all(engine)
    page = client.get(f"/api/chapters/{chapter_id}/segments", params={"limit": 1000}).json()
    assert page["chapter"]["status"] == "reviewing" and page["counts"]["flagged"] >= 1
    by_source = {s["source"]: s for s in page["segments"]}
    assert by_source["Grouped records"]["translation"] == "თარგმანი: Grouped records"
    assert by_source["Dtest = (Qout / Qin) × 100%".replace("Dtest", "D<sub>test</sub>").replace(
        "Qout", "Q<sub>out</sub>").replace("Qin", "Q<sub>in</sub>")]["translatable"] is False
    flagged = client.get(f"/api/chapters/{chapter_id}/segments", params={"flagged_only": True}).json()["segments"]
    assert flagged and all(s["flags"][0]["check"] == "numbers" for s in flagged)
    cost = client.get(f"/api/projects/{project_id}/cost").json()
    assert cost["budget_usd"] == 50.0 and cost["spent_usd"] > 0 and cost["stages"]["translate"]["calls"] >= 1
    assert client.post("/api/chapters/00000000-0000-0000-0000-000000000000/translate").status_code == 404
