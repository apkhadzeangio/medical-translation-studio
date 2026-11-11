"""Group a chapter's segments into translation chunks.

A chunk is what one model call translates. Blocks are never split: a table's cells, a list's
items or a callout's lines always travel together, and a heading stays with what follows it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Block, Segment


@dataclass
class Chunk:
    segment_ids: list[uuid.UUID] = field(default_factory=list)
    words: int = 0


def plan_chunks(session: Session, chapter_id: uuid.UUID, max_words: int = 1200) -> list[Chunk]:
    rows = session.execute(
        select(Segment.id, Segment.source_text, Segment.block_id, Block.type)
        .join(Block, Block.id == Segment.block_id)
        .where(Segment.chapter_id == chapter_id, Segment.translatable)
        .order_by(Block.idx, Segment.idx)
    ).all()
    groups: list[tuple[str, list[uuid.UUID], int]] = []       # one entry per block
    for seg_id, text, block_id, block_type in rows:
        words = len(text.split())
        if groups and groups[-1][0] == str(block_id):
            groups[-1][1].append(seg_id)
            groups[-1] = (groups[-1][0], groups[-1][1], groups[-1][2] + words)
        else:
            groups.append((str(block_id), [seg_id], words))
        if block_type == "heading":
            groups[-1] = (groups[-1][0] + ":heading", groups[-1][1], groups[-1][2])

    chunks: list[Chunk] = [Chunk()]
    for i, (key, ids, words) in enumerate(groups):
        current = chunks[-1]
        # a heading opens a new chunk when the current one is already half full
        heading_break = key.endswith(":heading") and current.words >= max_words / 2
        if current.segment_ids and (current.words + words > max_words or heading_break):
            carried = None
            if i and groups[i - 1][0].endswith(":heading") and len(current.segment_ids) > len(groups[i - 1][1]):
                carried = groups[i - 1]          # do not leave a heading stranded at the end of a chunk
                del current.segment_ids[-len(carried[1]):]
                current.words -= carried[2]
            chunks.append(Chunk())
            current = chunks[-1]
            if carried:
                current.segment_ids += carried[1]
                current.words += carried[2]
        current.segment_ids += ids
        current.words += words
    return [c for c in chunks if c.segment_ids]
