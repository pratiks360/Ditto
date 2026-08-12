"""Labels that a page renders twice.

A visible label plus a screen-reader copy in one container reads back doubled.
The extension collapses it, and the service does too — it is not the only client,
and a doubled title is a permanent, unreadable record.
"""

import pytest

from jobkb.text import collapse_doubled

Q = ("Do you have experience designing AI/ML or Generative AI solutions, "
     "including areas such as MLOps, Azure OpenAI, RAG pipelines, AI agents, "
     "or AI governance?")


@pytest.mark.parametrize("doubled,expected", [
    (Q + Q, Q),                       # the real LinkedIn string
    (Q + " " + Q, Q),
    ("What is your notice period?What is your notice period?",
     "What is your notice period?"),
])
def test_doubled_labels_collapse(doubled, expected):
    assert collapse_doubled(doubled) == expected


@pytest.mark.parametrize("label", [
    Q,
    "How many years of experience do you have as a Solution Architect?",
    "Email address",
    "Current company Previous company",
    "Have you worked with Kafka and Kafka Streams?",
    "Yes",
])
def test_real_labels_are_untouched(label):
    assert collapse_doubled(label) == label


def test_a_doubled_question_is_stored_once(kb):
    rec = kb.upsert_answer(Q + Q, "Yes, extensively.")
    assert rec.title == Q
    assert len(rec.path) < 120, "the filename should not be doubled either"


def test_the_doubled_and_plain_wordings_are_one_record(kb):
    """Someone who already stored the doubled form must not end up with two."""
    kb.upsert_answer(Q + Q, "Yes.")
    before = len(kb.answers())
    kb.upsert_answer(Q, "Yes, extensively.")
    assert len(kb.answers()) == before
