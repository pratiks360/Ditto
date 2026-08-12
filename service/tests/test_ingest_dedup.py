"""Re-importing a resume must not multiply your history.

Two extraction runs word things differently — one model writes "Post Graduate
Diploma", another "Post Graduate Diploma in Advanced Computing" — so exact
string comparison silently produces a second copy of the same qualification.
"""

import pytest

from jobkb import ingest
from jobkb.okf import Record, T_EDUCATION, T_EXPERIENCE


@pytest.fixture()
def blank(tmp_path, monkeypatch):
    from jobkb import store as store_mod
    from jobkb.config import settings

    monkeypatch.setattr(settings, "root", tmp_path)
    return store_mod.reset_store(tmp_path)


DIPLOMA_SHORT = {"degree": "Post Graduate Diploma",
                 "institution": "Mumbai Education Trust", "endYear": "2014"}
DIPLOMA_LONG = {"degree": "Post Graduate Diploma in Advanced Computing",
                "institution": "Mumbai Education Trust"}


def test_a_longer_wording_of_the_same_degree_is_not_a_second_record(blank):
    ingest.apply(blank, {"education": [DIPLOMA_SHORT]})
    assert len(blank.education()) == 1

    ingest.apply(blank, {"education": [DIPLOMA_LONG]})
    assert len(blank.education()) == 1, [r.fields for r in blank.education()]


def test_the_report_calls_it_a_duplicate_before_writing(blank):
    ingest.apply(blank, {"education": [DIPLOMA_SHORT]})
    report = ingest.plan_apply(blank, {"education": [DIPLOMA_LONG]})
    assert [r["action"] for r in report] == ["dup"]


def test_a_rerun_fills_gaps_without_overwriting_your_edits(blank):
    ingest.apply(blank, {"education": [DIPLOMA_SHORT]})
    rec = blank.education()[0]
    rec.fields["endYear"] = "2015"                 # you corrected this by hand
    blank.save(rec)

    ingest.apply(blank, {"education": [dict(DIPLOMA_LONG, endYear="2014", gpa="A")]})

    assert len(blank.education()) == 1
    after = blank.education()[0]
    assert after.fields["endYear"] == "2015", "a hand edit must survive a re-import"
    assert after.fields["gpa"] == "A", "a genuinely new field should be filled in"


def test_genuinely_different_degrees_at_one_institution_stay_separate(blank):
    ingest.apply(blank, {"education": [
        {"degree": "Bachelor of Engineering (B.E.)", "institution": "Raisoni College"},
        {"degree": "Master of Technology", "institution": "Raisoni College"},
    ]})
    assert len(blank.education()) == 2


def test_the_same_job_worded_differently_is_one_job(blank):
    ingest.apply(blank, {"experience": [
        {"title": "Solution Architect", "company": "Northwind Trading FZE",
         "startDate": "Sept 2025", "endDate": "Present"},
    ]})
    ingest.apply(blank, {"experience": [
        {"title": "Solution Architect", "company": "Northwind Trading",
         "startDate": "Sep 2025"},
    ]})
    assert len(blank.experience()) == 1


def test_two_roles_at_one_employer_stay_separate(blank):
    """A promotion is two entries, and forms ask for both."""
    ingest.apply(blank, {"experience": [
        {"title": "Software Developer", "company": "IBM", "startDate": "Apr 2021"},
        {"title": "Solution Architect", "company": "IBM", "startDate": "Jan 2023"},
    ]})
    assert len(blank.experience()) == 2


def test_skills_merge_without_duplicating(blank):
    ingest.apply(blank, {"skills": ["Kafka", "Python"]})
    ingest.apply(blank, {"skills": ["python", "Java"]})
    skills = blank.skills().fields["skills"]
    assert skills == ["Kafka", "Python", "Java"]
