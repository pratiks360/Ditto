import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobkb import store as store_mod  # noqa: E402
from jobkb.config import settings  # noqa: E402
from jobkb.okf import (  # noqa: E402
    Record, T_ANSWER, T_CUSTOM, T_EDUCATION, T_EXPERIENCE, T_PERSONAL, T_RESUME, T_SKILLS,
)


@pytest.fixture()
def kb(tmp_path, monkeypatch):
    """A knowledge base with one real-ish profile in it."""
    monkeypatch.setattr(settings, "root", tmp_path)
    s = store_mod.reset_store(tmp_path)

    s.save(Record(path="profile/personal.md", type=T_PERSONAL, title="Personal details",
                  fields={"firstName": "Alex", "lastName": "Rivera",
                          "email": "alex.rivera@example.com", "phone": "+971-500000000",
                          "city": "Dubai", "country": "UAE"}))

    s.save(Record(path="profile/experience/northwind-solution-architect.md",
                  type=T_EXPERIENCE, title="Solution Architect — Northwind Trading FZE",
                  fields={"title": "Solution Architect", "company": "Northwind Trading FZE",
                          "location": "UAE", "startDate": "Sept 2025", "endDate": "Present"}))
    s.save(Record(path="profile/experience/capgemini-consultant.md",
                  type=T_EXPERIENCE, title="Consultant — Capgemini",
                  fields={"title": "Consultant", "company": "Capgemini",
                          "location": "India", "startDate": "Sep 2018",
                          "endDate": "Apr 2021"}))

    s.save(Record(path="profile/education/kj-somaiya-mba.md", type=T_EDUCATION,
                  title="MBA — K.J. Somaiya",
                  fields={"degree": "MBA", "institution": "K.J. Somaiya",
                          "endYear": "2017"}))

    s.save(Record(path="profile/skills.md", type=T_SKILLS, title="Skills",
                  fields={"skills": ["Kafka", "Python", "AWS"]}))

    s.save(Record(path="profile/custom.md", type=T_CUSTOM, title="Custom facts",
                  fields={"notice_period": "30 days", "expected_salary": "AED 45,000"}))

    s.save(Record(path="answers/why-us.md", type=T_ANSWER,
                  title="Why do you want to work here?",
                  aliases=["What attracts you to this position?"],
                  body="Your streaming work matches my Kafka background."))

    s.save(Record(path="resume/alex.md", type=T_RESUME, title="Resume",
                  body="Solution Architect at Northwind Trading FZE since Sept 2025."))

    s.load()
    return s
