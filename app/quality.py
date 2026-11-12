"""Publication readiness, also used by the final-export gate."""
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Block, QAFlag, Segment, SegmentVersion


def report(session: Session, chapter_ids: list[uuid.UUID]) -> dict:
    counts = dict(session.execute(select(Segment.status, func.count()).where(
        Segment.chapter_id.in_(chapter_ids), Segment.translatable).group_by(Segment.status)).all())
    missing = session.scalar(select(func.count()).select_from(Segment).where(
        Segment.chapter_id.in_(chapter_ids), Segment.translatable,
        ~select(SegmentVersion.id).where(
            SegmentVersion.segment_id == Segment.id, SegmentVersion.is_current).exists()))
    flags = session.execute(select(QAFlag, Segment.id, Block.page_no).join(
        Segment, Segment.id == QAFlag.segment_id).join(Block, Block.id == Segment.block_id).where(
            Segment.chapter_id.in_(chapter_ids), ~QAFlag.resolved).order_by(Block.idx, Segment.idx)).all()
    critical = sum(flag.severity == "critical" for flag, _, _ in flags)
    total, approved = sum(counts.values()), counts.get("approved", 0)
    return {"segments": total, "approved": approved, "missing_translations": missing,
            "open_critical": critical, "open_findings": len(flags),
            "ready_for_final_export": total > 0 and approved == total and missing == 0 and critical == 0,
            "counts": counts,
            "findings": [{"id": str(flag.id), "segment_id": str(segment_id), "page": page,
                          "check": flag.check_name, "severity": flag.severity, "message": flag.message}
                         for flag, segment_id, page in flags]}
