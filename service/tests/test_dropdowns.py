"""Option-constrained questions.

The "Have you delivered X?" and "How many years of Y?" dropdowns are the bulk of
LinkedIn Easy Apply. No stored record answers them, and prose generation cannot
fill a <select> — they need their own pass that must return one of the offered
options.
"""

import pytest

from jobkb import openrouter
from jobkb.okf import Record, T_RESUME
from jobkb.planner import ACTION_REVIEW, ACTION_SKIP, Field, Planner


@pytest.fixture()
def resume_kb(kb):
    kb.save(Record(
        path="resume/alex.md", type=T_RESUME, title="Resume",
        body=(
            "ALEX RIVERA - Solution Architect, 12+ years.\n"
            "Northwind Trading FZE, Sept 2025 - Present: Solution Architect. "
            "Azure Data Factory, Azure Databricks, ADLS, Generative AI solutioning, "
            "RAG pipelines, MLOps.\n"
            "IBM, Apr 2021 - Aug 2025: Solution Architect. Kafka, Kubernetes.\n"
            "Capgemini, Sep 2018 - Apr 2021: Consultant.\n"
        ),
    ))
    kb.load()
    return kb


@pytest.fixture()
def fake_model(monkeypatch):
    state = {"reply": {}, "calls": [], "error": None}

    async def complete(client, messages, want_json=True, max_tokens=1500, depth=None):
        state["calls"].append(messages)
        if state["error"]:
            raise state["error"]
        reply = state["reply"]
        return (reply(messages) if callable(reply) else reply), "fake/model:free"

    monkeypatch.setattr(openrouter.router, "complete", complete)
    return state


def is_choose_call(messages) -> bool:
    # Match on a phrase that cannot straddle a line break in the prompt.
    return "fixed list" in messages[0]["content"]


def plan_of(result, field_id):
    return next(d for d in result["decisions"] if d["id"] == field_id)


AZURE_Q = Field(
    id="q1", tag="select",
    label="Have you designed and delivered enterprise-scale Azure Data Platform "
          "solutions using Azure Data Factory, ADLS, Databricks and/or Fabric?",
    options=["Yes", "No"],
)
YEARS_Q = Field(
    id="q2", tag="select",
    label="How many years of experience do you have working as a Solution Architect?",
    options=["Less than 1", "1-3", "3-5", "5-10", "10+"],
)


@pytest.mark.asyncio
async def test_a_dropdown_is_answered_from_the_resume(resume_kb, fake_model):
    def reply(messages):
        return {"q1": "Yes"} if is_choose_call(messages) else {"q1": "none"}

    fake_model["reply"] = reply
    out = await Planner(resume_kb).plan(None, [AZURE_Q])

    d = plan_of(out, "q1")
    assert d["value"] == "Yes"
    assert d["generated"] is True
    assert d["action"] == ACTION_REVIEW, "an inferred answer must be flagged, not silent"


@pytest.mark.asyncio
async def test_the_dropdown_prompt_carries_the_resume_and_the_options(resume_kb, fake_model):
    fake_model["reply"] = lambda m: {"q1": "Yes"} if is_choose_call(m) else {"q1": "none"}
    await Planner(resume_kb).plan(None, [AZURE_Q])

    choose = next(m for m in fake_model["calls"] if is_choose_call(m))
    body = choose[1]["content"]
    assert "=== RESUME ===" in body
    assert "Azure Data Factory" in body
    assert "- Yes" in body and "- No" in body


@pytest.mark.asyncio
async def test_an_option_that_was_not_offered_is_refused(resume_kb, fake_model):
    """The model must not be able to invent an answer the form cannot accept."""
    fake_model["reply"] = lambda m: ({"q1": "Absolutely, extensively"}
                                     if is_choose_call(m) else {"q1": "none"})
    out = await Planner(resume_kb).plan(None, [AZURE_Q])

    d = plan_of(out, "q1")
    assert d["value"] == "" and d["action"] == ACTION_SKIP
    assert "pick one yourself" in d["reason"]


@pytest.mark.asyncio
async def test_an_empty_reply_leaves_it_for_you(resume_kb, fake_model):
    """A wrong yes on an application is worse than an unanswered question."""
    fake_model["reply"] = lambda m: {"q1": ""} if is_choose_call(m) else {"q1": "none"}
    out = await Planner(resume_kb).plan(None, [AZURE_Q])
    assert plan_of(out, "q1")["action"] == ACTION_SKIP


@pytest.mark.asyncio
async def test_a_years_band_is_matched_to_the_offered_wording(resume_kb, fake_model):
    fake_model["reply"] = lambda m: {"q2": "5-10"} if is_choose_call(m) else {"q2": "none"}
    out = await Planner(resume_kb).plan(None, [YEARS_Q])
    assert plan_of(out, "q2")["value"] == "5-10"


@pytest.mark.asyncio
async def test_dropdowns_and_prose_are_separate_calls(resume_kb, fake_model):
    """They need different instructions; mixing them produces prose in a
    <select> and one-word cover letters."""
    def reply(messages):
        if is_choose_call(messages):
            return {"q1": "Yes"}
        if "CANDIDATES" in messages[1]["content"]:
            return {"q1": "none", "w": "generate"}
        return {"w": "Your platform work matches my Kafka background."}

    fake_model["reply"] = reply
    out = await Planner(resume_kb).plan(None, [
        AZURE_Q, Field(id="w", label="Why do you want to join us?", tag="textarea"),
    ])

    assert plan_of(out, "q1")["value"] == "Yes"
    assert plan_of(out, "w")["value"].startswith("Your platform work")
    assert sum(1 for m in fake_model["calls"] if is_choose_call(m)) == 1


@pytest.mark.asyncio
async def test_a_stored_answer_still_wins_over_asking_the_model(resume_kb, fake_model):
    """Routing runs first; a dropdown it can resolve never reaches the model."""
    fake_model["reply"] = {"c": "profile/personal.md#country"}
    out = await Planner(resume_kb).plan(None, [
        Field(id="c", label="Country", tag="select",
              options=["India", "UAE", "Qatar"]),
    ])
    d = plan_of(out, "c")
    assert d["value"] == "UAE"
    assert not any(is_choose_call(m) for m in fake_model["calls"])
