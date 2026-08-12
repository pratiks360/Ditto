"""OpenRouter client: free-model discovery, a fallback ladder, and JSON coercion.

Free models are what we have to work with, and they misbehave in specific,
repeatable ways:

  * capacity errors under load — "ResourceExhausted: Worker local total request
    limit reached (32/32)" — which are transient and worth retrying elsewhere;
  * prose or numbered pipe-tables where JSON was asked for, especially when the
    prompt itself contains a table the model can mirror;
  * silent truncation on long generations.

So: discover every zero-priced model at boot, rank them, health-check the top
few, and keep a ladder. A capacity error drops to the next rung rather than
sleeping and retrying the same exhausted worker. JSON is extracted by bracket
scanning rather than trusted, and one stricter re-ask is allowed before a call
is declared failed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("jobkb.openrouter")

BASE = "https://openrouter.ai/api/v1"

# Errors that mean "this model, right now" rather than "this request".
TRANSIENT = re.compile(
    r"rate.?limit|resourceexhausted|capacity|overload|too many requests|temporarily"
    r"|502|503|504|upstream|timeout|timed out",
    re.I,
)

# Families that in practice hold instruction-following at the free tier. Used
# only for ranking; nothing is excluded on the basis of its name.
PREFERRED = [
    "deepseek", "llama-3.3", "llama-3.1", "qwen", "mistral", "gemma",
    "glm", "kimi", "nemotron", "phi",
]


class ModelError(RuntimeError):
    def __init__(
        self,
        message: str,
        transient: bool = False,
        auth: bool = False,
        wait: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.transient = transient
        # An auth failure is about the account, not the model. Trying another
        # model cannot fix it, so it stops the ladder rather than walking it.
        self.auth = auth
        # Seconds until an account-wide limit resets. Non-zero means every model
        # is equally blocked, so walking the ladder only burns more budget.
        self.wait = wait


# OpenRouter's free tier meters *the account*, not the model: 20 requests a
# minute shared across every free model. Walking a five-rung ladder on one of
# these spends five requests to be told the same thing five times.
ACCOUNT_LIMIT = re.compile(
    r"free-models-per-min|openrouter_free_tier_per_minute", re.I
)


@dataclass
class ModelInfo:
    id: str
    name: str = ""
    context: int = 0
    json_mode: bool = False
    score: float = 0.0
    healthy: bool | None = None
    failures: int = 0
    last_error: str = ""
    free: bool = True
    price: float = 0.0          # USD per million prompt tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "context": self.context,
            "json_mode": self.json_mode, "score": round(self.score, 2),
            "healthy": self.healthy, "failures": self.failures,
            "last_error": self.last_error[:200],
            "free": self.free, "price": self.price,
        }


# -- JSON out of a model that was not asked nicely -------------------------


def slice_json(text: str) -> str | None:
    """First balanced {...} or [...] in the text, respecting string literals.

    A model that prefixes "Here is the JSON:" or trails an explanation breaks
    json.loads on the whole reply; scanning brackets recovers the object.
    """
    s = str(text or "")
    start = None
    for i, ch in enumerate(s):
        if ch in "{[":
            start = i
            break
    if start is None:
        return None

    opener = s[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def _seconds_until_reset(body: str, cap: float = 30.0) -> float:
    """How long to wait, from the X-RateLimit-Reset OpenRouter embeds in the
    error body. Falls back to a short wait when the header is absent, and is
    capped so a bad clock cannot stall a fill."""
    m = re.search(r'"X-RateLimit-Reset"\s*:\s*"?(\d+)"?', body)
    if m:
        reset_ms = int(m.group(1))
        seconds = reset_ms / 1000 - time.time()
        if 0 < seconds <= cap:
            return seconds + 0.5
    return 5.0


def parse_json(text: str) -> Any:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw).strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    sliced = slice_json(raw)
    if sliced is None:
        raise ValueError(f"no JSON in reply: {raw[:200]}")
    return json.loads(sliced)


# -- the client ------------------------------------------------------------


@dataclass
class Router:
    ladder: list[ModelInfo] = field(default_factory=list)
    fetched_at: float = 0.0
    last_discovery_error: str = ""
    auth_error: str = ""

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": settings.referer,
            "X-Title": settings.app_title,
        }

    # -- discovery --------------------------------------------------------

    @staticmethod
    def _price(entry: dict[str, Any]) -> float | None:
        """USD per million prompt tokens, or None when there is no usable price.

        A negative price means "varies" — the router ids report -1 because the
        cost depends on whichever model they pick. Treating that as free would
        put an unpriced model at the top of the ladder.
        """
        pricing = entry.get("pricing") or {}
        try:
            price = float(pricing.get("prompt", "")) * 1_000_000
        except (TypeError, ValueError):
            return None
        return None if price < 0 else price

    @staticmethod
    def _is_text_model(entry: dict[str, Any]) -> bool:
        """Text in, text out, and nothing else out.

        Media models are priced per token and advertise huge context windows, so
        they rank near the top and then fail every probe. Checking only that
        `text` appears is not enough — a music model declares
        `output_modalities: ["text", "audio"]`.
        """
        arch = entry.get("architecture") or {}
        out = arch.get("output_modalities") or ["text"]
        return set(out) == {"text"}

    @staticmethod
    def _rank(info: ModelInfo) -> float:
        score = 0.0
        # Context length matters: the routing prompt carries the whole candidate
        # catalog, and a 4k window truncates it into nonsense.
        score += min(info.context, 200_000) / 20_000
        if info.json_mode:
            score += 6.0
        low = info.id.lower()
        for i, fam in enumerate(PREFERRED):
            if fam in low:
                score += (len(PREFERRED) - i) * 0.5
                break
        if ":free" in low:
            score += 1.0
        return score

    async def discover(self, client: httpx.AsyncClient) -> list[ModelInfo]:
        if settings.pinned_model:
            self.ladder = [ModelInfo(id=settings.pinned_model, name="pinned", score=99)]
            self.fetched_at = time.time()
            return self.ladder

        r = await client.get(f"{BASE}/models", headers=self._headers(), timeout=30)
        r.raise_for_status()
        data = r.json().get("data") or []

        found: list[ModelInfo] = []
        for entry in data:
            price = self._price(entry)
            if price is None or price > settings.max_price:
                continue
            if not self._is_text_model(entry):
                continue
            # Router ids pick a different model per call, so speed, quality and
            # JSON compliance all vary between two identical requests. Pin real
            # models instead; the ladder is our own router.
            if str(entry.get("id") or "").startswith("openrouter/"):
                continue
            supported = entry.get("supported_parameters") or []
            info = ModelInfo(
                id=str(entry.get("id") or ""),
                name=str(entry.get("name") or ""),
                context=int(entry.get("context_length") or 0),
                json_mode="response_format" in supported,
                free=price == 0.0,
                price=price,
            )
            if not info.id:
                continue
            info.score = self._rank(info)
            found.append(info)

        # Free first, then paid. The free rungs cost nothing when the shared
        # per-minute pool has room; the paid ones are there for when it does not,
        # which is the case that used to stall a fill halfway through a form.
        found.sort(key=lambda m: (not m.free, -m.score))
        self.ladder = found
        free_count = sum(1 for m in found if m.free)
        log.info("discovered %d models (%d free, %d paid under $%.2f/M); top: %s",
                 len(found), free_count, len(found) - free_count, settings.max_price,
                 ", ".join(m.id for m in found[:5]))
        self.fetched_at = time.time()
        return found

    async def health_check(self, client: httpx.AsyncClient, depth: int | None = None) -> None:
        """Probe the top of the ladder with a tiny JSON task and demote anything
        that cannot answer it. A model that fails this will fail a real routing
        call too, and finding out now costs one second.

        The first probe runs alone. If it comes back 401/403 the key is wrong or
        has no access, and every other model will say the same thing — so the
        sweep stops there instead of firing nine more doomed requests.
        """
        # Every probe spends one request from a 20-per-minute account budget, so
        # probing twice the ladder depth at boot left a third of the first
        # minute for actual work. Probe exactly what the ladder will use.
        depth = depth or settings.ladder_depth
        probes = self.ladder[:depth]
        if not probes:
            return

        async def probe(m: ModelInfo) -> bool:
            """True unless the failure was about the account rather than the model."""
            try:
                out = await self._call_one(
                    client, m,
                    [{"role": "user", "content": 'Reply with only this JSON: {"ok":true}'}],
                    want_json=True, max_tokens=32, timeout=25,
                )
                m.healthy = bool(parse_json(out).get("ok") is not None)
                self.auth_error = ""
                return True
            except ModelError as exc:
                m.last_error = str(exc)
                if exc.auth:
                    m.healthy = False
                    self.auth_error = str(exc)
                    log.error("OpenRouter rejected the key: %s", exc)
                    return False
                if exc.wait:
                    # Rate-limited, not broken. Marking it unhealthy would demote
                    # a perfectly good model for the rest of the session.
                    m.healthy = None
                    return False
                m.healthy = False
                return True
            except Exception as exc:  # noqa: BLE001 - a probe failure is data
                m.healthy = False
                m.last_error = str(exc)
                return True

        if not await probe(probes[0]):
            return
        await asyncio.gather(*(probe(m) for m in probes[1:]))
        # Healthy first, then rank. Unprobed models keep their place behind
        # probed-healthy ones but ahead of known-bad.
        order = {True: 0, None: 1, False: 2}
        self.ladder.sort(key=lambda m: (order[m.healthy], -m.score))
        log.info("health check: %s",
                 ", ".join(f"{m.id}={m.healthy}" for m in self.ladder[:depth]))

    async def ensure_ladder(self, client: httpx.AsyncClient) -> None:
        stale = (time.time() - self.fetched_at) > settings.model_refresh_seconds
        if self.ladder and not stale:
            return
        try:
            await self.discover(client)
            await self.health_check(client)
            self.last_discovery_error = ""
        except Exception as exc:  # noqa: BLE001
            self.last_discovery_error = str(exc)
            log.warning("model discovery failed: %s", exc)

    # -- calling ----------------------------------------------------------

    async def _call_one(
        self,
        client: httpx.AsyncClient,
        model: ModelInfo,
        messages: list[dict[str, str]],
        want_json: bool,
        max_tokens: int,
        timeout: int,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model.id,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        if want_json and model.json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            r = await client.post(
                f"{BASE}/chat/completions",
                headers=self._headers(), json=payload, timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise ModelError(f"{model.id}: timed out after {timeout}s", transient=True) from exc
        except httpx.HTTPError as exc:
            raise ModelError(f"{model.id}: {exc}", transient=True) from exc

        if r.status_code in (401, 403):
            raise ModelError(
                # Plain ASCII: this string is printed to the Windows console,
                # which is cp1252 and turns an em dash into mojibake.
                f"HTTP {r.status_code} {r.text[:200]} - check OPENROUTER_API_KEY",
                auth=True,
            )

        if r.status_code >= 400:
            body = r.text[:1000]
            if r.status_code == 429 and ACCOUNT_LIMIT.search(body):
                raise ModelError(
                    "free-tier limit reached (20 requests/minute across all free models)",
                    transient=True,
                    wait=_seconds_until_reset(body),
                )
            raise ModelError(
                f"{model.id}: HTTP {r.status_code} {body[:400]}",
                transient=r.status_code in (408, 429, 500, 502, 503, 504)
                or bool(TRANSIENT.search(body)),
            )

        data = r.json()
        # OpenRouter can return HTTP 200 with an error object inside.
        if isinstance(data.get("error"), dict):
            msg = str(data["error"].get("message") or data["error"])
            raise ModelError(f"{model.id}: {msg}", transient=bool(TRANSIENT.search(msg)))

        choices = data.get("choices") or []
        if not choices:
            raise ModelError(f"{model.id}: no choices in reply", transient=True)
        content = (choices[0].get("message") or {}).get("content") or ""
        if not str(content).strip():
            raise ModelError(f"{model.id}: empty reply", transient=True)
        return str(content)

    async def complete(
        self,
        client: httpx.AsyncClient,
        messages: list[dict[str, str]],
        want_json: bool = True,
        max_tokens: int = 1500,
        depth: int | None = None,
    ) -> tuple[Any, str]:
        """Walk the ladder until one model answers. Returns (parsed, model_id).

        Parsed is the decoded JSON when want_json, otherwise the raw string.
        """
        if not settings.has_key:
            raise ModelError("no OPENROUTER_API_KEY set", auth=True)

        await self.ensure_ladder(client)

        # No point walking a ladder the account cannot climb.
        if self.auth_error:
            raise ModelError(self.auth_error, auth=True)
        rungs = [m for m in self.ladder if m.healthy is not False][: depth or settings.ladder_depth]
        if not rungs:
            rungs = self.ladder[: depth or settings.ladder_depth]
        if not rungs:
            raise ModelError(
                f"no free models available ({self.last_discovery_error or 'empty ladder'})",
                transient=True,
            )

        errors: list[str] = []
        waited = False
        for model in rungs:
            for attempt in (1, 2):
                try:
                    raw = await self._call_one(
                        client, model, messages, want_json, max_tokens,
                        settings.request_timeout,
                    )
                    if not want_json:
                        return raw, model.id
                    try:
                        return parse_json(raw), model.id
                    except Exception as exc:  # noqa: BLE001
                        if attempt == 1:
                            # One stricter re-ask. Free models mirror the shape
                            # of whatever is in the prompt, so say it plainly.
                            messages = messages + [
                                {"role": "assistant", "content": raw[:500]},
                                {"role": "user", "content":
                                 "That was not valid JSON. Reply with the JSON "
                                 "object only. No prose, no markdown fences, no "
                                 "lists, no pipe tables, no numbering."},
                            ]
                            errors.append(f"{model.id}: bad JSON ({exc})")
                            continue
                        model.failures += 1
                        model.last_error = str(exc)
                        errors.append(f"{model.id}: bad JSON twice ({exc})")
                        break
                except ModelError as exc:
                    model.failures += 1
                    model.last_error = str(exc)
                    errors.append(str(exc))
                    if exc.auth:
                        # Same answer from every rung; stop and say so.
                        self.auth_error = str(exc)
                        raise
                    if exc.wait:
                        # Account-wide limit. It applies to every *free* model at
                        # once, so dropping to another free rung is pointless —
                        # but a paid rung is metered separately and answers now.
                        if any(not m.free for m in rungs[rungs.index(model) + 1:]):
                            log.info("free pool is rate-limited; using a paid model")
                            break
                        # Nothing paid available: wait for the window instead of
                        # spending the next minute's budget walking the ladder.
                        if waited:
                            raise ModelError(
                                f"{exc} - still limited after waiting", transient=True
                            ) from exc
                        waited = True
                        log.info("free-tier limit hit; waiting %.1fs for reset", exc.wait)
                        await asyncio.sleep(exc.wait)
                        continue
                    break  # next model, not the same one again

        raise ModelError("all models failed: " + " | ".join(errors[-6:]), transient=True)


router = Router()
