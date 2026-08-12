"""The free-model ladder, and the failure modes free models actually have."""

import httpx
import pytest

from jobkb import openrouter
from jobkb.config import settings
from jobkb.openrouter import ModelError, ModelInfo, Router, parse_json, slice_json


def transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


MODELS_BODY = {
    "data": [
        {"id": "free/big:free", "context_length": 128000,
         "pricing": {"prompt": "0", "completion": "0"},
         "supported_parameters": ["response_format"]},
        {"id": "free/small:free", "context_length": 8000,
         "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": []},
        {"id": "paid/model", "context_length": 200000,
         "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
    ]
}


def chat(content: str, status: int = 200):
    if status != 200:
        return httpx.Response(status, text=content)
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


# -- JSON recovery --------------------------------------------------------


def test_json_is_recovered_from_a_chatty_reply():
    reply = 'Sure! Here is the JSON:\n```json\n{"a": 1}\n```\nHope that helps.'
    assert parse_json(reply) == {"a": 1}


def test_bracket_scan_respects_strings_and_escapes():
    """A brace inside a string value must not end the object early — this is
    what broke on a real reply at position 7271."""
    reply = 'text {"q": "a } brace \\" and quote", "b": [1, 2]} trailing'
    assert parse_json(reply) == {"q": 'a } brace " and quote', "b": [1, 2]}


def test_no_json_at_all_is_an_error_not_a_guess():
    assert slice_json("no json here") is None
    with pytest.raises(ValueError):
        parse_json("no json here")


# -- discovery and ranking ------------------------------------------------


@pytest.mark.asyncio
async def test_only_zero_priced_models_are_kept(monkeypatch):
    monkeypatch.setattr(settings, "pinned_model", "")
    monkeypatch.setattr(settings, "max_price", 0.0)
    r = Router()
    async with transport(lambda req: httpx.Response(200, json=MODELS_BODY)) as c:
        found = await r.discover(c)

    ids = [m.id for m in found]
    assert ids == ["free/big:free", "free/small:free"], "a paid model must never enter the ladder"
    # Bigger context and JSON support rank first: the routing prompt carries the
    # whole candidate catalog and a small window truncates it into nonsense.
    assert found[0].id == "free/big:free"


@pytest.mark.asyncio
async def test_a_budget_admits_cheap_paid_models_below_the_free_ones(monkeypatch):
    """On a funded account the free tier's 20-per-minute ceiling is the binding
    constraint, not money. A cheap paid rung removes it."""
    monkeypatch.setattr(settings, "pinned_model", "")
    # The fixture's paid model is $3 per million prompt tokens.
    monkeypatch.setattr(settings, "max_price", 5.0)
    r = Router()
    async with transport(lambda req: httpx.Response(200, json=MODELS_BODY)) as c:
        found = await r.discover(c)

    ids = [m.id for m in found]
    assert "paid/model" in ids
    assert ids[:2] == ["free/big:free", "free/small:free"], "free must still rank first"
    assert found[-1].free is False
    assert found[-1].price == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_image_and_audio_models_never_enter_the_ladder(monkeypatch):
    """They are priced per token and have huge context windows, so they rank
    high and then fail every probe — expensive on a 20-per-minute budget."""
    monkeypatch.setattr(settings, "pinned_model", "")
    monkeypatch.setattr(settings, "max_price", 0.0)
    body = {"data": [
        # Real shapes from the live model list.
        {"id": "google/lyria-3-pro-preview", "context_length": 262144,
         "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"output_modalities": ["text", "audio"]}},
        {"id": "vendor/imagegen", "context_length": 262144,
         "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"output_modalities": ["text", "image"]}},
        {"id": "openrouter/auto", "context_length": 262144,
         "pricing": {"prompt": "-1", "completion": "-1"},
         "architecture": {"output_modalities": ["text"]}},
        {"id": "openrouter/free", "context_length": 262144,
         "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"output_modalities": ["text"]}},
        {"id": "vendor/chat:free", "context_length": 32000,
         "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"output_modalities": ["text"]}},
    ]}
    r = Router()
    async with transport(lambda req: httpx.Response(200, json=body)) as c:
        found = await r.discover(c)

    # A music model declaring ["text", "audio"] must not count as a text model,
    # and a router pricing at -1 ("varies") must not count as free.
    assert [m.id for m in found] == ["vendor/chat:free"]


@pytest.mark.asyncio
async def test_a_model_over_budget_is_excluded(monkeypatch):
    monkeypatch.setattr(settings, "pinned_model", "")
    monkeypatch.setattr(settings, "max_price", 1.0)
    body = {"data": [{"id": "pricey/model", "context_length": 100000,
                      "pricing": {"prompt": "0.00001", "completion": "0.00003"}}]}
    r = Router()
    async with transport(lambda req: httpx.Response(200, json=body)) as c:
        found = await r.discover(c)
    assert found == [], "$10 per million is well over a $1 budget"


@pytest.mark.asyncio
async def test_a_rate_limited_free_pool_falls_through_to_a_paid_rung(monkeypatch):
    """Waiting is right when everything is free. When a paid rung exists it is
    metered separately and answers immediately."""
    import time as _time

    monkeypatch.setattr(settings, "api_key", "k")
    monkeypatch.setattr(settings, "pinned_model", "")
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(openrouter.asyncio, "sleep", fake_sleep)
    body = FREE_TIER_429 % int((_time.time() + 8) * 1000)
    used = []

    def handler(req):
        import json as _json
        model = _json.loads(req.content)["model"]
        used.append(model)
        return chat('{"ok": true}') if model == "paid/cheap" else chat(body, status=429)

    r = Router(
        ladder=[ModelInfo(id="free/a:free", healthy=True),
                ModelInfo(id="free/b:free", healthy=True),
                ModelInfo(id="paid/cheap", healthy=True, free=False, price=0.3)],
        fetched_at=float("inf"),
    )
    async with transport(handler) as c:
        parsed, chosen = await r.complete(c, [{"role": "user", "content": "hi"}])

    assert parsed == {"ok": True}
    assert chosen == "paid/cheap"
    assert slept == [], "with a paid rung available there is nothing to wait for"


@pytest.mark.asyncio
async def test_a_pinned_model_skips_discovery(monkeypatch):
    monkeypatch.setattr(settings, "pinned_model", "my/model")
    r = Router()
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json=MODELS_BODY)

    async with transport(handler) as c:
        found = await r.discover(c)

    assert [m.id for m in found] == ["my/model"]
    assert calls == [], "pinning a model must not cost a discovery request"


# -- the ladder -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_capacity_error_falls_to_the_next_model(monkeypatch):
    """The free-tier failure we actually hit:
    'ResourceExhausted: Worker local total request limit reached (32/32)'."""
    monkeypatch.setattr(settings, "api_key", "k")
    monkeypatch.setattr(settings, "pinned_model", "")
    seen = []

    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS_BODY)
        import json as _json
        model = _json.loads(req.content)["model"]
        seen.append(model)
        if model == "free/big:free":
            return chat("ResourceExhausted: Worker local total request "
                        "limit reached (32/32)", status=429)
        return chat('{"ok": true}')

    r = Router()
    async with transport(handler) as c:
        parsed, used = await r.complete(c, [{"role": "user", "content": "hi"}])

    assert parsed == {"ok": True}
    assert used == "free/small:free"
    assert "free/big:free" in seen, "the exhausted model must have been tried first"


@pytest.mark.asyncio
async def test_a_pipe_table_earns_one_stricter_reask(monkeypatch):
    """Free models mirror the shape of whatever is in the prompt — a resume full
    of pipe rows comes back as a pipe table."""
    monkeypatch.setattr(settings, "api_key", "k")
    monkeypatch.setattr(settings, "pinned_model", "")
    replies = ["1. Consultant | Capgemini | Sep 2018\n2. ...", '{"ok": true}']

    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS_BODY)
        return chat(replies.pop(0))

    # A ready ladder, so the health probe does not eat the scripted replies.
    r = Router(ladder=[ModelInfo(id="free/big:free", healthy=True)],
               fetched_at=float("inf"))
    async with transport(handler) as c:
        parsed, _ = await r.complete(c, [{"role": "user", "content": "hi"}])

    assert parsed == {"ok": True}
    assert not replies, "both the first reply and the re-ask should have been used"


@pytest.mark.asyncio
async def test_a_rejected_key_stops_after_one_probe(monkeypatch):
    """401 is about the account, not the model. Walking ten rungs to be told the
    same thing ten times is ten wasted requests at every boot."""
    monkeypatch.setattr(settings, "api_key", "bad")
    monkeypatch.setattr(settings, "pinned_model", "")
    attempts = []

    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS_BODY)
        attempts.append(1)
        return chat('{"error":{"message":"User not found.","code":401}}', status=401)

    r = Router()
    async with transport(handler) as c:
        await r.ensure_ladder(c)
        assert len(attempts) == 1, f"probed {len(attempts)} models with a bad key"
        assert "401" in r.auth_error

        with pytest.raises(ModelError) as caught:
            await r.complete(c, [{"role": "user", "content": "hi"}])

    assert caught.value.auth
    assert "OPENROUTER_API_KEY" in str(caught.value)


FREE_TIER_429 = (
    '{"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,'
    '"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0",'
    '"X-RateLimit-Reset":"%d"},"limit_source":"openrouter_free_tier_per_minute"}}}'
)


@pytest.mark.asyncio
async def test_the_account_wide_limit_waits_instead_of_burning_the_ladder(monkeypatch):
    """OpenRouter meters the free tier per account, not per model: 20 requests a
    minute across all of them. Falling to the next rung spends more of the same
    budget to be told the same thing."""
    import time as _time

    monkeypatch.setattr(settings, "api_key", "k")
    monkeypatch.setattr(settings, "pinned_model", "")
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(openrouter.asyncio, "sleep", fake_sleep)
    calls = []
    body = FREE_TIER_429 % int((_time.time() + 8) * 1000)

    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS_BODY)
        import json as _json
        calls.append(_json.loads(req.content)["model"])
        return chat(body, status=429) if len(calls) == 1 else chat('{"ok": true}')

    r = Router(ladder=[ModelInfo(id="free/big:free", healthy=True),
                       ModelInfo(id="free/small:free", healthy=True)],
               fetched_at=float("inf"))
    async with transport(handler) as c:
        parsed, used = await r.complete(c, [{"role": "user", "content": "hi"}])

    assert parsed == {"ok": True}
    assert used == "free/big:free", "it should retry the same model, not walk down"
    assert calls == ["free/big:free", "free/big:free"]
    assert slept and 7 < slept[0] < 10, f"should wait for the reset window, slept {slept}"


@pytest.mark.asyncio
async def test_a_rate_limited_model_is_not_marked_broken(monkeypatch):
    """Demoting it would lose a good model for the whole session."""
    import time as _time

    monkeypatch.setattr(settings, "api_key", "k")
    monkeypatch.setattr(settings, "pinned_model", "")
    body = FREE_TIER_429 % int((_time.time() + 5) * 1000)

    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS_BODY)
        return chat(body, status=429)

    r = Router()
    async with transport(handler) as c:
        await r.discover(c)
        await r.health_check(c)

    assert all(m.healthy is not False for m in r.ladder)
    assert r.auth_error == "", "a rate limit is not an auth failure"


@pytest.mark.asyncio
async def test_boot_probes_do_not_outnumber_the_ladder(monkeypatch):
    """Each probe spends one request from the per-minute account budget."""
    monkeypatch.setattr(settings, "api_key", "k")
    monkeypatch.setattr(settings, "pinned_model", "")
    monkeypatch.setattr(settings, "ladder_depth", 2)
    probes = []

    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS_BODY)
        probes.append(1)
        return chat('{"ok": true}')

    r = Router()
    async with transport(handler) as c:
        await r.discover(c)
        await r.health_check(c)

    assert len(probes) <= 2, f"probed {len(probes)} models for a ladder of 2"


@pytest.mark.asyncio
async def test_health_check_demotes_a_broken_model(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "k")
    monkeypatch.setattr(settings, "pinned_model", "")

    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS_BODY)
        import json as _json
        if _json.loads(req.content)["model"] == "free/big:free":
            return chat("I am a helpful assistant!")     # never returns JSON
        return chat('{"ok": true}')

    r = Router()
    async with transport(handler) as c:
        await r.discover(c)
        await r.health_check(c)

    assert r.ladder[0].id == "free/small:free", "the healthy model must rank first"
    assert r.ladder[0].healthy is True
    assert any(m.id == "free/big:free" and m.healthy is False for m in r.ladder)


@pytest.mark.asyncio
async def test_no_key_at_all_is_reported_as_an_auth_problem(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "")
    r = Router()
    async with transport(lambda req: httpx.Response(200, json=MODELS_BODY)) as c:
        with pytest.raises(ModelError) as caught:
            await r.complete(c, [{"role": "user", "content": "hi"}])
    assert caught.value.auth
