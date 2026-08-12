"""Fill planning. The model is faked so these assert our behaviour, not the
model's — including what happens when it misbehaves."""

import pytest

from jobkb import openrouter, planner as planner_mod
from jobkb.openrouter import ModelError
from jobkb.planner import (
    ACTION_FILL, ACTION_HIGHLIGHT, ACTION_REVIEW, ACTION_SKIP, Field, Planner,
)


@pytest.fixture()
def fake_model(monkeypatch):
    """Replaces the ladder with a scripted reply. `calls` records the prompts,
    so a test can assert what was and was not sent off the machine."""
    state = {"reply": {}, "calls": [], "error": None}

    async def complete(client, messages, want_json=True, max_tokens=1500, depth=None):
        state["calls"].append(messages)
        if state["error"]:
            raise state["error"]
        reply = state["reply"]
        return (reply(messages) if callable(reply) else reply), "fake/model:free"

    monkeypatch.setattr(openrouter.router, "complete", complete)
    return state


def plan_of(result, field_id):
    return next(d for d in result["decisions"] if d["id"] == field_id)


@pytest.mark.asyncio
async def test_routing_fills_from_the_store_not_the_model(kb, fake_model):
    """The model names a pointer; the value written is the stored one."""
    fake_model["reply"] = {"f1": "profile/personal.md#email"}
    p = Planner(kb)
    out = await p.plan(None, [Field(id="f1", label="Work e-mail")])

    d = plan_of(out, "f1")
    assert d["action"] == ACTION_FILL
    assert d["value"] == "alex.rivera@example.com"
    assert d["pointer"] == "profile/personal.md#email"


@pytest.mark.asyncio
async def test_routing_prompt_never_carries_stored_values(kb, fake_model):
    fake_model["reply"] = {"f1": "none"}
    p = Planner(kb)
    await p.plan(None, [Field(id="f1", label="Email")])

    sent = "".join(m["content"] for m in fake_model["calls"][0])
    assert "alex.rivera@example.com" not in sent
    assert "streaming work matches" not in sent
    assert "profile/personal.md#email" in sent


@pytest.mark.asyncio
async def test_a_forged_pointer_fills_nothing(kb, fake_model):
    fake_model["reply"] = {"f1": "Northwind Trading FZ", "f2": "profile/personal.md#ssn"}
    p = Planner(kb)
    out = await p.plan(None, [Field(id="f1", label="Employer"), Field(id="f2", label="ID")])

    for fid in ("f1", "f2"):
        d = plan_of(out, fid)
        assert d["value"] == "" and d["action"] == ACTION_SKIP


@pytest.mark.asyncio
async def test_dates_are_reencoded_for_the_widget(kb, fake_model):
    ptr = "profile/experience/northwind-solution-architect.md#startDate"
    fake_model["reply"] = {"f1": ptr, "f2": ptr}
    p = Planner(kb)
    out = await p.plan(None, [
        Field(id="f1", label="From", placeholder="MM/YYYY"),
        Field(id="f2", label="Start", inputType="month"),
    ])
    assert plan_of(out, "f1")["value"] == "09/2025"       # stored "Sept 2025"
    assert plan_of(out, "f2")["value"] == "2025-09"


@pytest.mark.asyncio
async def test_a_picker_only_date_is_highlighted_with_its_value(kb, fake_model):
    """The widget refuses programmatic writes, so the user types it — but they
    are told exactly what to type instead of being left to look it up."""
    ptr = "profile/experience/northwind-solution-architect.md#startDate"
    fake_model["reply"] = {"f1": ptr}
    p = Planner(kb)
    out = await p.plan(None, [
        Field(id="f1", label="From", placeholder="MM/YYYY", pickerOnly=True),
    ])
    d = plan_of(out, "f1")
    assert d["action"] == ACTION_HIGHLIGHT
    assert d["value"] == "09/2025"


@pytest.mark.asyncio
async def test_ongoing_role_leaves_the_end_date_empty(kb, fake_model):
    ptr = "profile/experience/northwind-solution-architect.md#endDate"
    fake_model["reply"] = {"f1": ptr}
    p = Planner(kb)
    out = await p.plan(None, [Field(id="f1", label="To", placeholder="MM/YYYY")])
    assert plan_of(out, "f1")["action"] == ACTION_SKIP


@pytest.mark.asyncio
async def test_currently_work_here_is_derived_not_asked(kb, fake_model):
    """It follows from the data — the newest role has no end date — so no model
    call is needed for it at all."""
    fake_model["reply"] = {}
    p = Planner(kb)
    out = await p.plan(None, [
        Field(id="c0", label="I currently work here", inputType="checkbox", blockIndex=0),
        Field(id="c1", label="I currently work here", inputType="checkbox", blockIndex=1),
    ])
    assert plan_of(out, "c0")["value"] == "true"
    assert plan_of(out, "c1")["value"] == ""       # Capgemini ended in Apr 2021


@pytest.mark.asyncio
async def test_a_known_question_never_reaches_the_model(kb, fake_model):
    p = Planner(kb)
    out = await p.plan(None, [
        Field(id="f1", label="What attracts you to this position?", tag="textarea"),
    ])
    assert fake_model["calls"] == [], "a stored answer must not cost a request"
    assert plan_of(out, "f1")["value"].startswith("Your streaming work")


@pytest.mark.asyncio
async def test_unknown_opinion_question_is_generated_and_flagged(kb, fake_model):
    def reply(messages):
        if "GENERATE" in messages[0]["content"] or "draft" in messages[0]["content"]:
            return {"f1": "I have shipped event-driven platforms end to end."}
        return {"f1": "generate"}

    fake_model["reply"] = reply
    p = Planner(kb)
    out = await p.plan(None, [
        Field(id="f1", label="What makes you a good fit for this role?", tag="textarea"),
    ])
    d = plan_of(out, "f1")
    assert d["generated"] is True
    assert d["action"] == ACTION_REVIEW, "a drafted answer must be flagged, not filled silently"
    assert d["value"]


@pytest.mark.asyncio
async def test_generation_prompt_carries_resume_and_prior_answers(kb, fake_model):
    def reply(messages):
        return {"f1": "generate"} if "CANDIDATES" in messages[1]["content"] else {"f1": "text"}

    fake_model["reply"] = reply
    p = Planner(kb)
    await p.plan(None, [Field(id="f1", label="Why us?", tag="textarea")])

    drafting = fake_model["calls"][-1][1]["content"]
    assert "=== RESUME ===" in drafting
    assert "=== PREVIOUS ANSWERS ===" in drafting


@pytest.mark.asyncio
async def test_generated_text_is_trimmed_to_the_field_limit(kb, fake_model):
    def reply(messages):
        if "CANDIDATES" in messages[1]["content"]:
            return {"f1": "generate"}
        return {"f1": "word " * 200}

    fake_model["reply"] = reply
    p = Planner(kb)
    out = await p.plan(None, [Field(id="f1", label="Why us?", tag="textarea", maxLength=50)])
    assert len(plan_of(out, "f1")["value"]) <= 50


@pytest.mark.asyncio
async def test_select_options_are_matched_not_typed(kb, fake_model):
    fake_model["reply"] = {"f1": "profile/personal.md#country"}
    p = Planner(kb)
    out = await p.plan(None, [
        Field(id="f1", label="Country", tag="select",
              options=["India", "United Arab Emirates", "UAE", "Qatar"]),
    ])
    assert plan_of(out, "f1")["value"] == "UAE"


@pytest.mark.asyncio
async def test_value_with_no_matching_option_is_flagged(kb, fake_model):
    fake_model["reply"] = {"f1": "profile/personal.md#city"}
    p = Planner(kb)
    out = await p.plan(None, [
        Field(id="f1", label="City", tag="select", options=["Mumbai", "Pune"]),
    ])
    d = plan_of(out, "f1")
    assert d["action"] == ACTION_REVIEW and d["value"] == "Dubai"


@pytest.mark.asyncio
async def test_demographic_questions_are_never_answered(kb, fake_model):
    fake_model["reply"] = {"f1": "profile/personal.md#firstName"}
    p = Planner(kb)
    out = await p.plan(None, [
        Field(id="f1", label="Do you identify as a protected veteran?"),
        Field(id="f2", label="Gender"),
        Field(id="f3", label="Disability status"),
    ])
    for fid in ("f1", "f2", "f3"):
        assert plan_of(out, fid)["action"] == ACTION_SKIP


@pytest.mark.asyncio
async def test_service_still_fills_when_the_model_is_down(kb, fake_model):
    """Every free model failing must degrade to local matching, not to nothing."""
    fake_model["error"] = ModelError("all models failed", transient=True)
    p = Planner(kb)
    out = await p.plan(None, [
        Field(id="f1", label="Email address"),
        Field(id="f2", label="What attracts you to this position?"),
    ])
    assert plan_of(out, "f1")["value"] == "alex.rivera@example.com"
    assert plan_of(out, "f2")["value"].startswith("Your streaming work")
    assert any("routing unavailable" in n for n in out["notes"])


@pytest.mark.asyncio
async def test_a_remembered_form_costs_no_request(kb, fake_model):
    kb.remember_mapping("sig123", "careers.acme.com",
                        {"e mail": "profile/personal.md#email"})
    p = Planner(kb)
    out = await p.plan(None, [Field(id="f1", label="E-mail")], signature="sig123")

    assert fake_model["calls"] == []
    d = plan_of(out, "f1")
    assert d["value"] == "alex.rivera@example.com" and d["confidence"] == 1.0


@pytest.mark.asyncio
async def test_unanswerable_question_is_marked_for_the_user(kb, fake_model):
    """"How much experience do you have with Apigee?" is not in the store and
    must not be invented — it comes back as skip, for the user to answer."""
    fake_model["reply"] = {"f1": "none"}
    p = Planner(kb)
    out = await p.plan(None, [
        Field(id="f1", label="How many years of experience do you have with Apigee?"),
    ])
    d = plan_of(out, "f1")
    assert d["action"] == ACTION_SKIP and d["value"] == ""
    assert d["reason"]
