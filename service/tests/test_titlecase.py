"""Re-casing resume text. A resume shouts; a form field should not."""

import pytest

from jobkb.ingest import _normalize
from jobkb.text import title_case


@pytest.mark.parametrize("shouted,expected", [
    ("ALEX", "Alex"),
    ("RIVERA", "Rivera"),
    ("SOLUTION ARCHITECT", "Solution Architect"),
    ("solution architect", "Solution Architect"),
    ("NORTHWIND TRADING FZE", "Northwind Trading FZE"),
    ("IBM", "IBM"),
    ("MASTER OF BUSINESS ADMINISTRATION (MBA)", "Master Of Business Administration (MBA)"),
    ("BACHELOR OF ENGINEERING (B.E.)", "Bachelor Of Engineering (B.E.)"),
    ("K.J. SOMAIYA INSTITUTE", "K.J. Somaiya Institute"),
    ("DUBAI, UAE", "Dubai, UAE"),
])
def test_uniform_case_is_fixed_without_breaking_acronyms(shouted, expected):
    assert title_case(shouted) == expected


@pytest.mark.parametrize("written", [
    "iPhone", "eBay", "McKinsey", "PwC", "LinkedIn", "JPMorgan Chase",
    "Northwind Trading FZE", "K.J. Somaiya",
])
def test_deliberate_mixed_case_is_left_alone(written):
    """Mixed case was a choice. Touching it can only make it worse."""
    assert title_case(written) == written


def test_contact_details_are_never_recased():
    data = _normalize({"personal": {
        "firstName": "ALEX",
        "email": "Alex.Rivera@EXAMPLE.com",
        "phone": "+971-500000000",
        "linkedin": "https://LinkedIn.com/in/alexRivera",
    }})
    assert data["personal"]["firstName"] == "Alex"
    # An address is a literal. Re-casing one can stop it working.
    assert data["personal"]["email"] == "Alex.Rivera@EXAMPLE.com"
    assert data["personal"]["linkedin"] == "https://LinkedIn.com/in/alexRivera"


def test_entries_are_recased_but_dates_and_prose_are_not():
    data = _normalize({"experience": [{
        "title": "SOLUTION ARCHITECT",
        "company": "NORTHWIND TRADING FZE",
        "startDate": "Sept 2025",
        "description": "Led API and integration platform work across the group.",
    }]})
    job = data["experience"][0]
    assert job["title"] == "Solution Architect"
    assert job["company"] == "Northwind Trading FZE"
    assert job["startDate"] == "Sept 2025"
    assert job["description"] == "Led API and integration platform work across the group."


def test_a_description_that_only_repeats_the_title_is_dropped():
    data = _normalize({"experience": [
        {"title": "Consultant", "company": "Capgemini", "description": "consultant"},
    ]})
    assert "description" not in data["experience"][0]
