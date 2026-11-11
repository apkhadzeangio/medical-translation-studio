import pytest
from sqlalchemy.orm import Session

from app.models import Project
from app.terminology import GlossaryIndex, Term, load_index, read_pairs, tm_add, tm_hash, tm_lookup, upsert_terms

TERMS = [
    Term("aneurysm", "ანევრიზმა"),
    Term("abdominal aortic aneurysm", "მუცლის აორტის ანევრიზმა"),
    Term("endoleak", "ენდოლიკი"),
    Term("graft", "გრაფტი"),
    Term("follow-up", "შემდგომი დაკვირვება"),
]


def sources(terms):
    return [t.source for t in terms]


def test_longest_term_wins_and_plurals_match():
    index = GlossaryIndex(TERMS)
    text = "An Abdominal  aortic\naneurysm was treated; endoleaks and two Grafts were seen at follow-up."
    assert sources(index.find(text)) == ["abdominal aortic aneurysm", "endoleak", "graft", "follow-up"]
    assert sources(index.find("A popliteal aneurysm, then an abdominal aortic aneurysm.")) == [
        "aneurysm", "abdominal aortic aneurysm"]


def test_whole_words_only():
    index = GlossaryIndex(TERMS)
    assert index.find("Autografting and engraftment are different words; pseudoaneurysmal too.") == []
    assert GlossaryIndex([]).find("anything") == []


def test_violations_tolerate_georgian_case_endings():
    index = GlossaryIndex(TERMS)
    source = "The abdominal aortic aneurysm showed an endoleak near the graft."
    good = "მუცლის აორტის ანევრიზმის შემთხვევაში გრაფტთან ენდოლიკი გამოვლინდა."
    assert index.violations(source, good) == []
    bad = "მუცლის აორტის გაფართოების შემთხვევაში პროთეზთან გაჟონვა გამოვლინდა."
    assert sources(index.violations(source, bad)) == ["abdominal aortic aneurysm", "endoleak", "graft"]


def test_read_pairs_formats_and_problems():
    rows, problems = read_pairs("﻿en;ka;note\nendoleak;ენდოლიკი;device term\n\ngraft;\nstent;სტენტი\n".encode())
    assert rows == [("endoleak", "ენდოლიკი", "device term"), ("stent", "სტენტი", None)]
    assert problems == ["line 4: needs a source and a target"]
    assert read_pairs(b"a\tb\n")[0] == [("a", "b", None)]
    assert read_pairs("x,\xff".encode("latin-1"))[1] == ["line 1: not valid UTF-8"]


@pytest.fixture()
def project(migrated_db):
    with Session(migrated_db) as s:
        p = Project(name="terms")
        s.add(p)
        s.commit()
        return p.id


def test_upsert_and_index_from_database(migrated_db, project):
    with Session(migrated_db) as s:
        assert upsert_terms(s, project, [("Endoleak", "ენდოლიკი", None), ("graft", "გრაფტი", "n")]) == {
            "created": 2, "updated": 0, "unchanged": 0}
        assert upsert_terms(s, project, [("endoleak", "ენდოლიკი", None), ("GRAFT", "პროთეზი", None)]) == {
            "created": 0, "updated": 1, "unchanged": 1}
        s.commit()
        found = load_index(s, project).find("An endoleak beside the graft.")
        assert [(t.source, t.target) for t in found] == [("Endoleak", "ენდოლიკი"), ("graft", "პროთეზი")]


def test_translation_memory_exact_and_ranked(migrated_db, project):
    with Session(migrated_db) as s:
        assert tm_add(s, project, "No occlusion  was\nrecorded.", "ოკლუზია არ დაფიქსირებულა.", "pipeline")
        assert tm_add(s, project, "No occlusion was recorded.", "ოკლუზია არ აღინიშნა.", "human")
        assert not tm_add(s, project, "No occlusion was recorded.", "machine again", "pipeline")
        tm_add(s, project, "12 MM", "12 MM", "import")
        s.commit()
        hits = tm_lookup(s, project, ["No occlusion was recorded.", "Occlusion was recorded.", "12 mm"])
        assert hits == {"No occlusion was recorded.": "ოკლუზია არ აღინიშნა."}
        assert tm_lookup(s, project, []) == {}
    assert tm_hash("12 MM") != tm_hash("12 mm")


def test_glossary_and_tm_api(client, project):
    base = f"/api/projects/{project}"
    csv_file = "en,ka\nendoleak,ენდოლიკი\ngraft,გრაფტი\nbroken\n".encode()
    result = client.post(f"{base}/glossary/import", files={"file": ("g.csv", csv_file, "text/csv")}).json()
    assert (result["created"], result["skipped"]) == (2, 1)
    saved = client.post(f"{base}/glossary", json={"source_term": "Graft", "target_term": "პროთეზი"}).json()
    assert saved["target_term"] == "პროთეზი"
    assert [t["source_term"] for t in client.get(f"{base}/glossary").json()] == ["endoleak", "graft"]
    assert [t["source_term"] for t in client.get(f"{base}/glossary", params={"q": "ენდო"}).json()] == ["endoleak"]
    exported = client.get(f"{base}/glossary/export")
    assert "graft,პროთეზი" in exported.text and exported.headers["content-type"].startswith("text/csv")
    assert client.delete(f"/api/glossary/{saved['id']}").status_code == 204
    assert len(client.get(f"{base}/glossary").json()) == 1

    tm_file = "source\ttarget\nNot assessed.\tარ შეფასებულა.\n".encode()
    assert client.post(f"{base}/tm/import", files={"file": ("tm.tsv", tm_file, "text/tab-separated-values")}
                       ).json()["stored"] == 1
    assert client.get(f"{base}/tm").json() == {"import": 1}
    assert client.post("/api/projects/00000000-0000-0000-0000-000000000000/glossary/import",
                       files={"file": ("g.csv", csv_file, "text/csv")}).status_code == 404
