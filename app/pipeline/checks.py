"""Deterministic checks on one translated segment. No model involved.

These catch the errors that matter most in medical text and that a model reviewer misses
most easily: a changed number, a lost exponent or citation marker, a text left in English,
a glossary term not used.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from app.terminology import GlossaryIndex

NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
MARKUP_RE = re.compile(r"<(su[pb])>(.*?)</\1>")
SYMBOLS = ("≤", "≥", "<", ">", "±", "×", "%", "°")
GEORGIAN_RE = re.compile(r"[ა-ჿ]")


@dataclass(frozen=True)
class Finding:
    check: str
    severity: str      # critical | major | minor
    message: str


def _plain(text: str) -> str:
    return re.sub(r"</?su[pb]>", " ", text)


def numbers(text: str) -> Counter:
    # 1,250 and 1.250 are different numbers; nothing is normalised
    return Counter(n.rstrip(".,") for n in NUMBER_RE.findall(_plain(text)))


def check_segment(source: str, target: str, glossary: GlossaryIndex | None = None) -> list[Finding]:
    findings: list[Finding] = []
    if not target.strip():
        return [Finding("empty", "critical", "თარგმანი ცარიელია")]

    want, got = numbers(source), numbers(target)
    if want != got:
        missing, extra = sorted((want - got).elements()), sorted((got - want).elements())
        findings.append(Finding("numbers", "critical",
                                f"რიცხვები არ ემთხვევა: აკლია {missing or '—'}, ზედმეტია {extra or '—'}"))

    if Counter(MARKUP_RE.findall(source)) != Counter(MARKUP_RE.findall(target)):
        findings.append(Finding("markup", "critical", "ზედა/ქვედა ინდექსი (<sup>/<sub>) შეცვლილია ან დაკარგულია"))

    plain_source, plain_target = _plain(source), _plain(target)
    changed = [sym for sym in SYMBOLS if plain_source.count(sym) != plain_target.count(sym)]
    if changed:
        findings.append(Finding("symbols", "critical",
                                f"სიმბოლოები წყაროსა და თარგმანში განსხვავდება: {' '.join(changed)}"))

    letters = re.findall(r"[A-Za-z]{2,}", plain_source)
    if len(letters) >= 4 and not GEORGIAN_RE.search(target):
        findings.append(Finding("untranslated", "major", "სეგმენტი უთარგმნელია"))

    ratio = len(plain_target) / max(1, len(plain_source))
    if len(plain_source) >= 80 and not 0.5 <= ratio <= 2.2:
        findings.append(Finding("length", "minor", f"საეჭვო სიგრძე (თარგმანი/წყარო = {ratio:.2f})"))

    if glossary is not None:
        for term in glossary.violations(source, target):
            findings.append(Finding("glossary", "major",
                                    f"ლექსიკონი: «{term.source}» → «{term.target}» არ გამოიყენება"))
    return findings
