"""Glossary matching and translation memory.

Glossary: which mandatory terms occur in a source text, and whether a translation used them.
Translation memory: exact reuse of an already approved translation of the same source text.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import GlossaryTerm, TMEntry

TM_ORIGIN_RANK = {"pipeline": 0, "import": 1, "human": 2}     # a higher rank is never overwritten by a lower


@dataclass(frozen=True)
class Term:
    source: str
    target: str
    note: str | None = None


class GlossaryIndex:
    """All of a project's terms compiled into one pattern; longest term wins at each position."""

    def __init__(self, terms: list[Term]) -> None:
        self.by_key = {self._key(t.source): t for t in terms if t.source.strip()}
        ordered = sorted(self.by_key, key=len, reverse=True)
        body = "|".join(re.escape(k).replace(r"\ ", r"\s+") for k in ordered)
        # whole words only; a plural "s"/"es" still counts as the term
        self.pattern = re.compile(rf"(?<![A-Za-z0-9])({body})(?:e?s)?(?![A-Za-z0-9])", re.I) if ordered else None

    @staticmethod
    def _key(text: str) -> str:
        return re.sub(r"\s+", " ", text.strip().lower())

    def find(self, text: str) -> list[Term]:
        """Terms present in `text`, each once, in order of first appearance."""
        if self.pattern is None:
            return []
        seen: dict[str, Term] = {}
        for match in self.pattern.finditer(text):
            key = self._key(match.group(1))
            if key in self.by_key:
                seen.setdefault(key, self.by_key[key])
        return list(seen.values())

    def violations(self, source: str, target: str) -> list[Term]:
        """Terms that occur in the source whose Georgian equivalent is missing from the target."""
        haystack = target.lower()
        return [t for t in self.find(source) if not _used(t.target, haystack)]


def _used(term: str, haystack: str) -> bool:
    """Georgian nouns change their ending by case, so compare word stems, not exact forms."""
    words = [w for w in re.split(r"[\s\-/]+", term.lower()) if w]
    stems = [w[:-1] if len(w) > 3 else w for w in words]
    return all(stem in haystack for stem in stems)


def load_index(session: Session, project_id: uuid.UUID) -> GlossaryIndex:
    rows = session.execute(
        select(GlossaryTerm.source_term, GlossaryTerm.target_term, GlossaryTerm.note)
        .where(GlossaryTerm.project_id == project_id, GlossaryTerm.approved))
    return GlossaryIndex([Term(*row) for row in rows])


def read_pairs(data: bytes) -> tuple[list[tuple[str, str, str | None]], list[str]]:
    """Parse an uploaded CSV/TSV of `source, target[, note]`. Returns rows and per-line problems."""
    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    delimiter = max((",", ";", "\t"), key=sample.count)
    rows, problems = [], []
    for number, row in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter), start=1):
        cells = [c.strip() for c in row]
        if not any(cells):
            continue
        if number == 1 and cells[0].lower() in {"en", "english", "source", "term", "source_term"}:
            continue
        if len(cells) < 2 or not cells[0] or not cells[1]:
            problems.append(f"line {number}: needs a source and a target")
            continue
        if "�" in cells[0] + cells[1]:
            problems.append(f"line {number}: not valid UTF-8")
            continue
        rows.append((cells[0], cells[1], cells[2] if len(cells) > 2 and cells[2] else None))
    return rows, problems


def upsert_terms(session: Session, project_id: uuid.UUID, rows: list[tuple[str, str, str | None]]) -> dict[str, int]:
    existing = {t.source_term.lower(): t for t in session.scalars(
        select(GlossaryTerm).where(GlossaryTerm.project_id == project_id))}
    created = updated = unchanged = 0
    for source, target, note in rows:
        current = existing.get(source.lower())
        if current is None:
            term = GlossaryTerm(project_id=project_id, source_term=source[:300], target_term=target[:300], note=note)
            session.add(term)
            existing[source.lower()] = term
            created += 1
        elif (current.target_term, current.note) != (target, note if note is not None else current.note):
            current.target_term = target[:300]
            current.note = note if note is not None else current.note
            updated += 1
        else:
            unchanged += 1
    session.flush()
    return {"created": created, "updated": updated, "unchanged": unchanged}


# ----------------------------------------------------------------------------- translation memory


def tm_hash(text: str) -> str:
    """Case-sensitive on purpose: `MM` and `mm` are different sources."""
    return hashlib.sha256(re.sub(r"\s+", " ", text).strip().encode()).hexdigest()


def tm_add(session: Session, project_id: uuid.UUID, source: str, target: str, origin: str = "pipeline") -> bool:
    """Remember a translation. Returns False when a better-ranked entry was kept instead."""
    digest = tm_hash(source)
    current = session.execute(
        select(TMEntry).where(TMEntry.project_id == project_id, TMEntry.source_hash == digest)).scalar_one_or_none()
    if current is not None:
        if TM_ORIGIN_RANK.get(origin, 0) < TM_ORIGIN_RANK.get(current.origin, 0):
            return False
        current.target_text, current.origin = target, origin
        return True
    session.execute(insert(TMEntry).values(
        project_id=project_id, source_text=source, source_hash=digest, target_text=target, origin=origin,
    ).on_conflict_do_nothing())
    return True


def tm_lookup(session: Session, project_id: uuid.UUID, sources: list[str]) -> dict[str, str]:
    """Exact matches for the given source texts."""
    if not sources:
        return {}
    by_hash = {tm_hash(s): s for s in sources}
    rows = session.execute(
        select(TMEntry.source_hash, TMEntry.target_text)
        .where(TMEntry.project_id == project_id, TMEntry.source_hash.in_(list(by_hash))))
    return {by_hash[digest]: target for digest, target in rows}
