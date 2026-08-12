"""The learning loop: a submitted form has to make the next form easier."""

import pytest

from jobkb import openrouter
from jobkb.learn import CapturedField, Submission, learn
from jobkb.planner import Field, Planner
from jobkb.retrieve import exact_answer_match


@pytest.fixture()
def fake_model(monkeypatch):
    state = {"reply": {}, "calls": []}

    async def complete(client, messages, want_json=True, max_tokens=1500, depth=None):
        state["calls"].append(messages)
        reply = state["reply"]
        return (reply(messages) if callable(reply) else reply), "fake/model:free"

    monkeypatch.setattr(openrouter.router, "complete", complete)
    return state


@pytest.mark.asyncio
async def test_a_new_question_is_learned_and_reused_on_another_site(kb, fake_model):
    """The whole point. Answer it once on one site, have it on the next."""
    q = "How many years of experience do you have with Apigee?"
    fake_model["reply"] = {}          # no model verdict -> rules classify it

    sub = Submission(
        site="careers.acme.com", signature="sig1",
        job_title="Solution Architect", company="Acme",
        items=[CapturedField(id="f1", label=q, value="Around four years, mostly on API proxies.")],
    )
    result = await learn(None, kb, sub, allow_ai=False)
    assert any(s["kind"] == "answer" for s in result["saved"])

    # A different site, asking it differently.
    hit = exact_answer_match(kb, "Years of experience with Apigee")
    assert hit, "the learned answer must be findable by a reworded question"
    assert kb.resolve(hit[0]).startswith("Around four years")


@pytest.mark.asyncio
async def test_learning_is_cross_site_not_per_site(kb, fake_model):
    fake_model["reply"] = {}
    await learn(None, kb, Submission(
        site="a.example", signature="s1",
        items=[CapturedField(id="f1", label="Why this company?",
                             value="I want to work on payments at scale.")],
    ), allow_ai=False)

    p = Planner(kb)
    out = await p.plan(None, [Field(id="x", label="Why this company?")], site="b.example")
    d = out["decisions"][0]
    assert d["value"] == "I want to work on payments at scale."
    assert fake_model["calls"] == [], "a learned answer must not need a model"


@pytest.mark.asyncio
async def test_a_correction_wins(kb, fake_model):
    fake_model["reply"] = {}
    for text in ["30 days notice, negotiable.", "Serving notice, free from 1 October."]:
        await learn(None, kb, Submission(
            site="a.example", signature="s1",
            items=[CapturedField(id="f1", label="What is your notice period?", value=text)],
        ), allow_ai=False)

    hit = exact_answer_match(kb, "What is your notice period?")
    assert kb.resolve(hit[0]) == "Serving notice, free from 1 October."


@pytest.mark.asyncio
async def test_an_application_record_is_written_with_links(kb, fake_model):
    fake_model["reply"] = {}
    result = await learn(None, kb, Submission(
        site="careers.acme.com", url="https://careers.acme.com/jobs/8821",
        signature="s1", job_title="Solution Architect", company="Acme",
        items=[CapturedField(id="f1", label="Why us?", value="Your streaming stack.")],
    ), allow_ai=False)

    rec = kb.get(result["application"])
    assert rec.resource == "https://careers.acme.com/jobs/8821"
    assert rec.fields["company"] == "Acme"
    assert "answers/" in rec.body


@pytest.mark.asyncio
async def test_the_form_shape_is_remembered(kb, fake_model):
    fake_model["reply"] = {}
    await learn(None, kb, Submission(
        site="careers.acme.com", signature="sig-abc",
        items=[CapturedField(id="f1", label="E-mail", value="alex.rivera@example.com",
                             pointer="profile/personal.md#email")],
    ), allow_ai=False)

    mapping = kb.mapping_for("sig-abc")
    assert mapping and mapping.fields["map"]["e mail"] == "profile/personal.md#email"


@pytest.mark.asyncio
async def test_sensitive_and_empty_answers_are_never_stored(kb, fake_model):
    fake_model["reply"] = {}
    result = await learn(None, kb, Submission(
        site="a.example", signature="s1",
        items=[
            CapturedField(id="f1", label="Gender", value="Male"),
            CapturedField(id="f2", label="Disability status", value="No"),
            CapturedField(id="f3", label="Notes", value="   "),
            CapturedField(id="f4", label="", value="orphan"),
        ],
    ), allow_ai=False)

    assert result["kept"] == 0
    assert not any("gender" in a["path"].lower() for a in result["saved"])


@pytest.mark.asyncio
async def test_ai_classification_can_store_a_short_fact_under_a_key(kb, fake_model):
    # The classifier keys its verdicts by the id it was given.
    fake_model["reply"] = {"f1": {"kind": "fact", "key": "willing_to_relocate"}}
    result = await learn(None, kb, Submission(
        site="a.example", signature="s1",
        items=[CapturedField(id="f1", label="Willing to relocate?", value="Yes")],
    ), allow_ai=True)

    assert result["classifier"] == "fake/model:free"
    assert kb.resolve("profile/custom.md#willing_to_relocate") == "Yes"


@pytest.mark.asyncio
async def test_learning_survives_the_model_being_down(kb, monkeypatch):
    async def boom(*a, **k):
        raise openrouter.ModelError("all models failed", transient=True)

    monkeypatch.setattr(openrouter.router, "complete", boom)
    result = await learn(None, kb, Submission(
        site="a.example", signature="s1",
        items=[CapturedField(id="f1", label="Why us?",
                             value="Because your platform work is unusual.")],
    ), allow_ai=True)

    assert result["classifier"] == "rules"
    assert result["saved"], "rules must still capture a prose answer"
