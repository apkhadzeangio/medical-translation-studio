import json

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Chapter, Job, LLMCall, QAFlag, Segment, SegmentVersion
from app.pipeline import review
from tests.test_review_qa import FakeProvider, current_text
from tests.test_review_qa import setup as setup


class FourStageProvider(FakeProvider):
    def __init__(self, refine_edits=None, malformed=False):
        super().__init__()
        self.refine_edits = refine_edits or (lambda segments: [])
        self.malformed = malformed
        self.requests["refine"] = []

    def __call__(self, request):
        body = json.loads(request.content)
        if body["messages"][0]["content"] != review.REFINE_SYSTEM:
            return super().__call__(request)
        payload = json.loads(body["messages"][1]["content"])
        self.requests["refine"].append(payload)
        answer = {"wrong": []} if self.malformed else {"edits": self.refine_edits(payload["segments"])}
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100}})


def test_all_four_stages_audit_the_refined_text_and_keep_the_draft(setup):
    engine, project_id, chapter_id, start = setup
    provider = FourStageProvider(refine_edits=lambda segments: [
        {"id": seg["id"], "ka": "აკადემიურად გამართული სათაური", "reason": "ქართული ფორმა"}
        for seg in segments if seg["en"] == "Grouped records"])
    assert set(start(provider, refine_model="gpt-6-astra")) == {"done"}
    assert all(provider.requests[stage] for stage in ("translate", "review", "refine", "qa"))
    audited = [seg for payload in provider.requests["qa"] for seg in payload["segments"]
               if seg["en"] == "Grouped records"]
    assert audited[0]["ka"] == "აკადემიურად გამართული სათაური"
    with Session(engine) as session:
        assert session.get(Chapter, chapter_id).status == "done"
        segment = session.scalar(select(Segment).where(
            Segment.chapter_id == chapter_id, Segment.source_text == "Grouped records"))
        versions = list(session.scalars(select(SegmentVersion).where(
            SegmentVersion.segment_id == segment.id).order_by(SegmentVersion.created_at)))
        assert [(version.stage, version.is_current) for version in versions] == [("translate", False), ("refine", True)]
        assert segment.refine_done
        assert set(session.scalars(select(LLMCall.stage).where(LLMCall.project_id == project_id))) == {
            "translate", "review", "refine", "qa"}
        assert set(session.scalars(select(LLMCall.prompt_version).where(LLMCall.project_id == project_id))) == {
            "medical-translate-v1", "medical-review-v1", "academic-refine-v1", "independent-qa-v1"}


def test_refinement_cannot_introduce_a_changed_number(setup):
    engine, _, chapter_id, start = setup
    source = "0.05 / 0.5 / 5.0"
    provider = FourStageProvider(refine_edits=lambda segments: [
        {"id": seg["id"], "ka": "0.5 / 0.5 / 5.0", "reason": "bad edit"}
        for seg in segments if seg["en"] == source])
    start(provider, refine_model="gpt-6-astra")
    with Session(engine) as session:
        assert "0.05" in current_text(session, chapter_id, source)
        assert session.scalar(select(QAFlag).join(Segment).where(
            Segment.chapter_id == chapter_id, QAFlag.check_name == "refine_rejected")) is not None


def test_malformed_refinement_does_not_pass_the_chapter_to_qa(setup):
    engine, project_id, chapter_id, start = setup
    provider = FourStageProvider(malformed=True)
    start(provider, refine_model="gpt-6-astra")
    assert provider.requests["refine"] and not provider.requests["qa"]
    with Session(engine) as session:
        assert session.get(Chapter, chapter_id).status == "refining"
        failed = list(session.scalars(select(Job).where(Job.project_id == project_id, Job.kind == "refine_chunk")))
        assert failed and all("edits array" in job.last_error for job in failed)
