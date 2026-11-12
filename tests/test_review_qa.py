import hashlib
import json
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
from app.pipeline import review, translate
from app.worker import Worker, default_handlers

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_vascular_chapter.pdf"
PRICES = '{"gpt-6-astra": {"input": 10, "output": 50}, "other-vendor-qa": {"input": 3, "output": 15}}'


class FakeProvider:
    """Plays translator, reviewer and auditor; each role is scripted per test."""

    def __init__(self, translate_as=None, edits=None, findings=None):
        self.translate_as = translate_as or (lambda en: "თარგმანი: " + en)
        self.edits = edits or (lambda segments: [])
        self.findings = findings or (lambda segments: [])
        self.requests = {"translate": [], "review": [], "qa": []}
        self.models = {"translate": set(), "review": set(), "qa": set()}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system, payload = body["messages"][0]["content"], json.loads(body["messages"][1]["content"])
        if system == review.REVIEW_SYSTEM:
            stage, answer = "review", {"edits": self.edits(payload["segments"])}
        elif system == review.QA_SYSTEM:
            stage, answer = "qa", {"findings": self.findings(payload["segments"])}
        else:
            stage = "translate"
            answer = {"segments": [{"id": s["id"], "ka": self.translate_as(s["en"])} for s in payload["segments"]]}
        self.requests[stage].append(payload)
        self.models[stage].add(body["model"])
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 300}})


@pytest.fixture()
def setup(migrated_db, monkeypatch):
    data = FIXTURE.read_bytes()
    with Session(migrated_db) as s, pymupdf.open(stream=data, filetype="pdf") as pdf:
        s.execute(update(Job).where(Job.status.in_(("queued", "running"))).values(status="cancelled"))
        project = Project(name="review")
        s.add(project)
        s.flush()
        doc = Document(project_id=project.id, filename="c.pdf", format="pdf", storage_key="k",
                       sha256=hashlib.sha256(data + str(project.id).encode()).hexdigest())
        s.add(doc)
        s.flush()
        store_parsed(s, doc, analyse(pdf))
        s.commit()
        chapter_id = s.scalars(select(Chapter.id).where(Chapter.document_id == doc.id)).first()
        project_id = project.id

    def start(provider: FakeProvider, qa_model: str | None = "other-vendor-qa", refine_model: str | None = None):
        settings = Settings(llm_api_keys="sk-test", llm_base_url="https://llm.test/v1", llm_max_attempts=1,
                            llm_prices=PRICES, llm_qa_model=qa_model, llm_refine_model=refine_model)
        gw = LLMGateway(migrated_db, settings, transport=httpx.MockTransport(provider), sleep=lambda s: None)
        monkeypatch.setattr(translate, "get_gateway", lambda session: gw)
        monkeypatch.setattr(review, "get_settings", lambda: settings)
        with Session(migrated_db) as s:
            translate.plan_chapter(s, chapter_id)
            s.commit()
        worker, statuses = Worker(migrated_db, default_handlers()), []
        while (status := worker.run_once()) is not None and len(statuses) < 80:
            statuses.append(status)
        return statuses

    return migrated_db, project_id, chapter_id, start


def current_text(session, chapter_id, source):
    return session.execute(
        select(SegmentVersion.text).join(Segment)
        .where(Segment.chapter_id == chapter_id, Segment.source_text == source, SegmentVersion.is_current)
    ).scalar_one()


def test_clean_chapter_runs_all_three_stages_to_done(setup):
    engine, project_id, chapter_id, start = setup
    provider = FakeProvider()
    assert set(start(provider)) == {"done"}
    with Session(engine) as s:
        assert s.get(Chapter, chapter_id).status == "done"
        stages = dict(s.execute(select(LLMCall.stage, func.count()).where(LLMCall.project_id == project_id)
                                .group_by(LLMCall.stage)).all())
        assert stages["translate"] == stages["review"] == stages["qa"] >= 1
        statuses = dict(s.execute(select(Segment.status, func.count()).where(
            Segment.chapter_id == chapter_id, Segment.translatable).group_by(Segment.status)).all())
        assert set(statuses) == {"reviewed"}
    assert provider.models["qa"] == {"other-vendor-qa"} and provider.models["review"] == {"gpt-6-astra"}
    reviewed = provider.requests["review"][0]["segments"][0]
    assert set(reviewed) == {"id", "kind", "en", "ka", "issues"} and reviewed["ka"].startswith("თარგმანი: ")
    assert "issues" not in provider.requests["qa"][0]["segments"][0]      # the auditor sees no hints


def test_review_fixes_a_flagged_number_and_history_is_kept(setup):
    engine, project_id, chapter_id, start = setup
    source = "0.05 / 0.5 / 5.0"

    def edits(segments):
        return [{"id": s["id"], "ka": s["en"], "severity": "critical", "reason": "რიცხვი აღდგენილია"}
                for s in segments if s["en"] == source and s["issues"]]

    provider = FakeProvider(translate_as=lambda en: en.replace("0.05", "0.5") if en == source else "თარგმანი: " + en,
                            edits=edits)
    start(provider)
    sent = [s for p in provider.requests["review"] for s in p["segments"] if s["en"] == source]
    assert sent and "რიცხვები არ ემთხვევა" in sent[0]["issues"][0]         # the reviewer was told what is wrong
    with Session(engine) as s:
        assert current_text(s, chapter_id, source) == source
        seg = s.execute(select(Segment).where(Segment.chapter_id == chapter_id, Segment.source_text == source)
                        ).scalar_one()
        assert seg.status == "reviewed" and s.get(Chapter, chapter_id).status == "done"
        versions = list(s.scalars(select(SegmentVersion).where(SegmentVersion.segment_id == seg.id)
                                  .order_by(SegmentVersion.created_at)))
        assert [(v.stage, v.is_current) for v in versions] == [("translate", False), ("review", True)]
        assert versions[1].note == "რიცხვი აღდგენილია"
        flags = list(s.scalars(select(QAFlag).where(QAFlag.segment_id == seg.id)))
        assert [(f.check_name, f.resolved) for f in flags] == [("numbers", True)]
        assert s.execute(select(TMEntry.target_text).where(
            TMEntry.project_id == project_id, TMEntry.source_text == source)).scalar_one() == source


def test_review_edit_that_breaks_a_number_is_refused(setup):
    engine, project_id, chapter_id, start = setup
    source = "≤ 0.80 / ≥ 1.20"

    def edits(segments):
        return [{"id": s["id"], "ka": "≥ 0.8 / ≥ 1.20", "severity": "minor", "reason": "სტილი"}
                for s in segments if s["en"] == source]

    start(FakeProvider(translate_as=lambda en: en if en == source else "თარგმანი: " + en, edits=edits))
    with Session(engine) as s:
        assert current_text(s, chapter_id, source) == source               # the good translation stands
        seg = s.execute(select(Segment).where(Segment.chapter_id == chapter_id, Segment.source_text == source)
                        ).scalar_one()
        flag = s.execute(select(QAFlag).where(QAFlag.segment_id == seg.id)).scalar_one()
        assert flag.check_name == "review_rejected" and "numbers" in flag.message and "symbols" in flag.message
        assert seg.status == "reviewed"


def test_qa_reports_without_changing_text_and_holds_the_chapter(setup):
    engine, project_id, chapter_id, start = setup
    source = "Grouped records"

    def findings(segments):
        return [{"id": s["id"], "category": "Mistranslation!", "severity": "critical", "comment": "აზრი შეცვლილია"}
                for s in segments if s["en"] == source] + [{"id": "zz", "category": "x", "comment": "უცნობი id"}]

    start(FakeProvider(findings=findings))
    with Session(engine) as s:
        assert current_text(s, chapter_id, source) == "თარგმანი: " + source
        seg = s.execute(select(Segment).where(Segment.chapter_id == chapter_id, Segment.source_text == source)
                        ).scalar_one()
        flag = s.execute(select(QAFlag).where(QAFlag.segment_id == seg.id)).scalar_one()
        assert (flag.check_name, flag.severity, flag.message) == ("qa:mistranslation", "critical", "აზრი შეცვლილია")
        assert seg.status == "flagged" and seg.qa_done
        assert s.get(Chapter, chapter_id).status == "qa"                    # waits for a person
        assert s.scalar(select(func.count()).select_from(QAFlag).where(QAFlag.message == "უცნობი id")) == 0


def test_without_qa_model_the_stage_is_skipped(setup):
    engine, project_id, chapter_id, start = setup
    provider = FakeProvider()
    start(provider, qa_model=None)
    assert provider.requests["qa"] == [] and provider.requests["review"]
    with Session(engine) as s:
        assert s.get(Chapter, chapter_id).status == "done"


def test_stages_are_idempotent(setup):
    engine, project_id, chapter_id, start = setup
    provider = FakeProvider()
    start(provider)
    calls = {k: len(v) for k, v in provider.requests.items()}
    with Session(engine) as s:
        again = [translate.plan_chapter(s, chapter_id), review.plan_review(s, chapter_id),
                 review.plan_qa(s, chapter_id)]
        s.execute(update(Job).where(Job.project_id == project_id).values(status="queued", attempts=0))
        s.commit()
    assert all(plan["jobs_queued"] == 0 for plan in again)                   # same keys, nothing new
    worker = Worker(engine, default_handlers())
    while worker.run_once() is not None:
        pass
    assert {k: len(v) for k, v in provider.requests.items()} == calls         # re-running paid for nothing


def test_review_and_history_api(setup, client):
    engine, project_id, chapter_id, start = setup
    assert client.post(f"/api/chapters/{chapter_id}/qa").status_code == 409    # no QA model configured yet
    start(FakeProvider(edits=lambda segs: [{"id": s["id"], "ka": "შესწორებული სათაური", "reason": "უკეთესი ტერმინი"}
                                           for s in segs if s["en"] == "Grouped records"]))
    page = client.get(f"/api/chapters/{chapter_id}/segments", params={"limit": 1000}).json()
    seg = next(s for s in page["segments"] if s["source"] == "Grouped records")
    assert seg["translation"] == "შესწორებული სათაური" and seg["status"] == "reviewed"
    history = client.get(f"/api/segments/{seg['id']}/versions").json()
    assert [(v["stage"], v["current"]) for v in history["versions"]] == [("translate", False), ("review", True)]
    assert history["versions"][1]["note"] == "უკეთესი ტერმინი"
    assert client.post(f"/api/chapters/{chapter_id}/review").json()["jobs_queued"] == 0
    plan = client.post(f"/api/chapters/{chapter_id}/qa").json()
    assert (plan["stage"], plan["independent"], plan["jobs_queued"]) == ("qa", True, 0)
    cost = client.get(f"/api/projects/{project_id}/cost").json()
    assert set(cost["stages"]) == {"translate", "review", "qa"}
