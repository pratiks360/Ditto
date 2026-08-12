"""Fill planning — the endpoint the extension actually lives on.

Scanned fields go in, a plan comes out: one decision per field, with the value,
where it came from, how confident we are, and what the extension should do with
it.

The order of attack, cheapest first:

  1. a remembered mapping for this exact form (free, instant, no model);
  2. a stored answer whose wording matches (free);
  3. a derived value — "I currently work here" follows from an ongoing role;
  4. AI routing: local prefilter narrows the pointer catalog, the model picks
     which pointer belongs in which field, and *the service* substitutes the
     stored text;
  5. AI generation, for questions nothing in the store answers.

Steps 4 and 5 are the only ones that leave the machine, and they carry very
different payloads: routing sends labels and pointer names, generation sends
your resume and prior answers because it has to write prose.

Actions the extension honours:
  fill                  write it
  highlight_with_value  show the value on the field, user enters it themselves
  review                written, but flagged — a model wrote this, read it
  skip                  nothing known, or nothing that should be guessed
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field as dc_field
from typing import Any

import httpx

from .dates import format_for_field, is_date_field, is_ongoing, parse_date
from .openrouter import ModelError, router
from .retrieve import Retriever, build_catalog, exact_answer_match
from .store import Store
from .text import (
    is_current_role_label, is_opinion_question, jaccard, looks_sensitive,
    normalize_label,
)

log = logging.getLogger("jobkb.planner")

ACTION_FILL = "fill"
ACTION_HIGHLIGHT = "highlight_with_value"
ACTION_REVIEW = "review"
ACTION_SKIP = "skip"


@dataclass
class Field:
    id: str
    label: str = ""
    inputType: str = "text"
    tag: str = "input"
    required: bool = False
    placeholder: str = ""
    pattern: str = ""
    hint: str = ""
    maxLength: int = 0
    options: list[str] = dc_field(default_factory=list)
    readOnly: bool = False
    pickerOnly: bool = False      # widget refuses programmatic writes
    blockIndex: int = -1          # repeated section this field belongs to
    value: str = ""               # what is already in the field

    def hints(self) -> dict[str, Any]:
        return {
            "inputType": self.inputType, "placeholder": self.placeholder,
            "pattern": self.pattern, "hint": self.hint, "label": self.label,
        }

    @property
    def is_choice(self) -> bool:
        return bool(self.options) or self.tag == "select"

    @property
    def is_checkbox(self) -> bool:
        return self.inputType == "checkbox"

    @property
    def is_long_text(self) -> bool:
        return self.tag == "textarea" or self.maxLength > 300


@dataclass
class Decision:
    id: str
    label: str
    action: str = ACTION_SKIP
    value: str = ""
    pointer: str = ""
    source: str = ""
    confidence: float = 0.0
    reason: str = ""
    generated: bool = False
    candidates: list[dict[str, Any]] = dc_field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "action": self.action,
            "value": self.value, "pointer": self.pointer, "source": self.source,
            "confidence": round(self.confidence, 2), "reason": self.reason,
            "generated": self.generated,
        }


# -- prompts ---------------------------------------------------------------

ROUTE_SYSTEM = """You map job-application form fields onto a person's stored records.

You are given FIELDS (labels from a form) and, for each, CANDIDATES: pointers
into a knowledge base. You never see the stored values and you must never invent
one.

For every field return exactly one of:
  - a pointer copied character-for-character from that field's candidate list
  - "generate" if the field needs prose that no stored record holds (opinion
    questions: why this company, what makes you a fit, cover letter)
  - "none" if nothing fits, or the field asks for something personal and
    situational you should not guess

Rules:
  - Repeated sections matter. A form's "Employer 1" is the most recent job,
    which is block 0. Match block numbers, do not mix jobs.
  - A start-date field takes a startDate pointer, never an endDate pointer.
  - Demographic or self-identification questions: always "none".
  - Reply with a JSON object only: {"<field id>": "<pointer|generate|none>"}.
  - No prose, no markdown fences, no lists, no pipe tables, no numbering."""

GENERATE_SYSTEM = """You draft answers to job-application questions in the
applicant's own voice, using only the facts given.

Rules:
  - Ground every claim in the resume or the previous answers. Invent nothing:
    no employers, dates, tools, numbers or achievements that are not there.
  - Previous answers outrank the resume when they conflict.
  - First person, plain sentences, no salutation, no sign-off, no headings.
  - Respect each question's max_chars.
  - Reply with a JSON object only: {"<field id>": "<answer>"}.
  - No prose outside the JSON, no markdown fences, no lists, no pipe tables."""

CHOOSE_SYSTEM = """You answer job-application questions that offer a fixed list
of options, using only the applicant's resume and previous answers.

These are the "Have you done X?" and "How many years of Y?" dropdowns. The
answer is usually in the resume even though no stored record spells it out.

Rules:
  - Return one option, copied character-for-character from that question's
    options list. Never write anything that is not in the list.
  - Judge from the whole resume, not one line: a tool used at one employer
    counts as experience with it, and a closely related technology counts when
    the question is asked broadly.
  - For "how many years" questions, add up only the roles that actually match
    what the question names, using their dates. Do not use the resume's overall
    "N+ years of experience" summary line: a question about years as a Solution
    Architect is asking about time in that role, not about career length. Do not
    round upward, and if the total falls between two options, take the lower.
  - If the resume genuinely does not support any option, return "" for that
    field. A wrong yes on an application is worse than an unanswered question.
  - Reply with a JSON object only: {"<field id>": "<option>"}.
  - No prose, no markdown fences, no lists, no pipe tables, no explanations."""


class Planner:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.retriever = Retriever(build_catalog(store))

    def rebuild(self) -> None:
        self.retriever = Retriever(build_catalog(self.store))

    # -- entry point ------------------------------------------------------

    async def plan(
        self,
        client: httpx.AsyncClient,
        fields: list[Field],
        site: str = "",
        signature: str = "",
        job_description: str = "",
        allow_ai: bool = True,
    ) -> dict[str, Any]:
        decisions: dict[str, Decision] = {
            f.id: Decision(id=f.id, label=f.label) for f in fields
        }
        notes: list[str] = []
        remembered = self.store.mapping_for(signature) if signature else None
        remembered_map = dict((remembered.fields.get("map") or {})) if remembered else {}

        unresolved: list[Field] = []

        for f in fields:
            d = decisions[f.id]

            if looks_sensitive(f.label):
                d.reason = "self-identification question — never answered automatically"
                continue

            # 1. this exact form, seen before
            pointer = remembered_map.get(normalize_label(f.label))
            if pointer and self.store.resolve(pointer) is not None:
                self._apply_pointer(d, f, pointer, 1.0, "remembered from this form")
                continue

            # 2. a derived truth: the current-role checkbox follows from the data
            if f.is_checkbox and is_current_role_label(f.label):
                if self._current_role(f.blockIndex):
                    d.action, d.value, d.confidence = ACTION_FILL, "true", 0.95
                    d.source = "derived from your ongoing role"
                    d.reason = "the matching job has no end date"
                else:
                    d.reason = "that job has an end date"
                continue

            # 3. a question we have answered before, by wording
            hit = exact_answer_match(self.store, f.label)
            if hit:
                self._apply_pointer(d, f, hit[0], min(0.95, 0.6 + hit[1] / 2),
                                    f"matches a saved question ({hit[1]:.2f})")
                continue

            unresolved.append(f)

        # 4. AI routing over a locally-narrowed catalog
        routed: dict[str, str] = {}
        if unresolved and allow_ai:
            try:
                routed, model_id = await self._route(client, unresolved)
                notes.append(f"routed {len(unresolved)} field(s) via {model_id}")
            except ModelError as exc:
                notes.append(f"routing unavailable ({exc}); using local matches only")
                log.warning("routing failed: %s", exc)

        to_generate: list[Field] = []
        to_choose: list[Field] = []
        for f in unresolved:
            d = decisions[f.id]
            pointer = str(routed.get(f.id) or "").strip()

            # A question offering a fixed list is answerable from the resume even
            # when no stored record spells it out — "Have you delivered Azure
            # Data Factory solutions?" is a Yes/No whose answer is in the work
            # history. Prose generation cannot fill a <select>, so these get
            # their own pass that must return one of the offered options.
            if f.is_choice and f.options and pointer in ("", "none", "generate"):
                to_choose.append(f)
                continue

            if pointer == "generate" or (not pointer and is_opinion_question(f.label)):
                to_generate.append(f)
                continue
            if not pointer or pointer == "none":
                # Local prefilter still has an opinion; take it when it is strong.
                best = self.retriever.search(f.label, k=1)
                if best and best[0].score >= 0.75:
                    self._apply_pointer(d, f, best[0].pointer, 0.6,
                                        f"local match ({best[0].score:.2f})")
                else:
                    d.reason = d.reason or "nothing stored answers this"
                continue

            if self.store.resolve(pointer) is None:
                d.reason = "the model returned a pointer that does not resolve"
                continue
            self._apply_pointer(d, f, pointer, 0.8, "matched by the routing model")

        # 5a. option-constrained answers, from the resume
        if to_choose and allow_ai:
            try:
                chosen, model_id = await self._choose(client, to_choose, job_description)
                notes.append(f"answered {len(to_choose)} dropdown(s) via {model_id}")
                for f in to_choose:
                    d = decisions[f.id]
                    picked = self._match_option(str(chosen.get(f.id) or "").strip(), f.options)
                    if not picked:
                        d.reason = "no option is supported by your resume — pick one yourself"
                        continue
                    d.value, d.generated, d.confidence = picked, True, 0.6
                    d.source = "chosen from your resume"
                    d.reason = "picked from your work history — check it"
                    d.action = ACTION_REVIEW
            except ModelError as exc:
                notes.append(f"dropdown answering unavailable ({exc})")
                for f in to_choose:
                    decisions[f.id].reason = f"needs an answer from you ({exc})"
        elif to_choose:
            for f in to_choose:
                decisions[f.id].reason = "needs an answer from you"

        # 5b. generation for what is left
        if to_generate and allow_ai:
            try:
                drafted, model_id = await self._generate(client, to_generate, job_description)
                notes.append(f"drafted {len(drafted)} answer(s) via {model_id}")
                for f in to_generate:
                    d = decisions[f.id]
                    text = str(drafted.get(f.id) or "").strip()
                    if not text:
                        d.reason = "the model returned nothing for this question"
                        continue
                    if f.maxLength and len(text) > f.maxLength:
                        text = text[: f.maxLength].rsplit(" ", 1)[0]
                    d.value, d.generated, d.confidence = text, True, 0.55
                    d.source = "drafted from your resume and saved answers"
                    d.reason = "no stored answer — read this before you submit"
                    d.action = ACTION_REVIEW
            except ModelError as exc:
                notes.append(f"generation unavailable ({exc})")
                for f in to_generate:
                    decisions[f.id].reason = f"needs an answer from you ({exc})"
        elif to_generate:
            for f in to_generate:
                decisions[f.id].reason = "needs an answer from you"

        out = [decisions[f.id].as_dict() for f in fields]
        return {
            "decisions": out,
            "notes": notes,
            "semantic": self.retriever.semantic,
            "counts": {
                a: sum(1 for d in out if d["action"] == a)
                for a in (ACTION_FILL, ACTION_HIGHLIGHT, ACTION_REVIEW, ACTION_SKIP)
            },
        }

    # -- helpers ----------------------------------------------------------

    def _current_role(self, block: int) -> bool:
        jobs = self.store.experience()
        idx = block if block >= 0 else 0
        if idx >= len(jobs):
            return False
        end = jobs[idx].fields.get("endDate")
        return not str(end or "").strip() or is_ongoing(end)

    def _apply_pointer(
        self, d: Decision, f: Field, pointer: str, confidence: float, reason: str
    ) -> None:
        raw = self.store.resolve(pointer)
        if raw is None:
            d.reason = "pointer resolved to nothing"
            return

        d.pointer = pointer
        d.source = self.store.describe(pointer)
        d.confidence = confidence
        d.reason = reason
        value = raw

        if is_date_field(f.hints()):
            encoded = format_for_field(raw, f.hints())
            if not encoded:
                if is_ongoing(raw):
                    d.reason = "role is ongoing — leave the end date empty"
                    d.action = ACTION_SKIP
                    return
                # We know the date but not the encoding this widget wants.
                d.value = str(raw)
                d.action = ACTION_HIGHLIGHT
                d.reason = "this control did not say which date format it wants"
                return
            value = encoded

        if f.is_choice and f.options:
            matched = self._match_option(value, f.options)
            if matched is None:
                d.value = str(value)
                d.action = ACTION_REVIEW
                d.reason = "no option matches the stored value — pick one"
                return
            value = matched

        if f.maxLength and len(str(value)) > f.maxLength:
            d.value = str(value)
            d.action = ACTION_HIGHLIGHT
            d.reason = f"stored value is longer than the field allows ({f.maxLength})"
            return

        d.value = str(value)
        # A widget that refuses programmatic writes still gets its answer — the
        # user just types or picks it. This is the date-picker case.
        d.action = ACTION_HIGHLIGHT if (f.pickerOnly or f.readOnly) else ACTION_FILL
        if d.action == ACTION_HIGHLIGHT:
            d.reason = "this control only accepts input from you — the value is shown on it"

    @staticmethod
    def _match_option(value: Any, options: list[str]) -> str | None:
        v = normalize_label(value)
        for opt in options:
            if normalize_label(opt) == v:
                return opt
        best, score = None, 0.0
        for opt in options:
            s = jaccard(str(value), opt)
            if s > score:
                best, score = opt, s
        return best if score >= 0.5 else None

    # -- the two model calls ----------------------------------------------

    async def _route(
        self, client: httpx.AsyncClient, fields: list[Field]
    ) -> tuple[dict[str, str], str]:
        blocks: list[str] = []
        for f in fields:
            cands = self.retriever.search(f.label, k=10)
            lines = "\n".join(f"    {c.pointer}  ->  {c.label}" for c in cands)
            meta = [f"type={f.inputType or f.tag}"]
            if f.blockIndex >= 0:
                meta.append(f"section_block={f.blockIndex}")
            if f.required:
                meta.append("required")
            if f.options:
                meta.append("options=" + " | ".join(f.options[:12]))
            if f.hint or f.placeholder:
                meta.append(f"hint={f.hint or f.placeholder}")
            blocks.append(
                f"FIELD id={f.id}\n  label: {f.label}\n  {'; '.join(meta)}\n"
                f"  CANDIDATES:\n{lines or '    (none)'}"
            )

        messages = [
            {"role": "system", "content": ROUTE_SYSTEM},
            {"role": "user", "content": "\n\n".join(blocks)},
        ]
        parsed, model_id = await router.complete(
            client, messages, want_json=True, max_tokens=1200
        )
        if not isinstance(parsed, dict):
            raise ModelError("routing reply was not a JSON object", transient=True)
        return {str(k): str(v) for k, v in parsed.items()}, model_id

    def _grounding(self, job_description: str = "", jd_chars: int = 0) -> list[str]:
        """Resume plus prior answers — everything a drafted reply may rely on."""
        parts = []
        resume = self.store.resume_text()
        if resume:
            parts.append(f"=== RESUME ===\n{resume[:12000]}")
        prior = "\n\n".join(
            f"Q: {r.title}\nA: {r.body}" for r in self.store.answers() if r.body
        )
        if prior:
            parts.append(f"=== PREVIOUS ANSWERS ===\n{prior[:8000]}")
        if job_description and jd_chars:
            parts.append(f"=== THIS JOB ===\n{job_description[:jd_chars]}")
        return parts

    async def _choose(
        self, client: httpx.AsyncClient, fields: list[Field], job_description: str
    ) -> tuple[dict[str, str], str]:
        parts = self._grounding(job_description, 1500)

        questions = []
        for f in fields:
            opts = "\n".join(f"      - {o}" for o in f.options[:25])
            questions.append(f"- id={f.id}\n  question: {f.label}\n  options:\n{opts}")
        parts.append("=== QUESTIONS ===\n" + "\n".join(questions))

        parsed, model_id = await router.complete(
            client,
            [
                {"role": "system", "content": CHOOSE_SYSTEM},
                {"role": "user", "content": "\n\n".join(parts)},
            ],
            want_json=True, max_tokens=800,
        )
        if not isinstance(parsed, dict):
            raise ModelError("dropdown reply was not a JSON object", transient=True)
        return {str(k): str(v) for k, v in parsed.items()}, model_id

    async def _generate(
        self, client: httpx.AsyncClient, fields: list[Field], job_description: str
    ) -> tuple[dict[str, str], str]:
        parts = self._grounding(job_description, 4000)

        questions = "\n".join(
            f'- id={f.id} max_chars={f.maxLength or (1200 if f.is_long_text else 300)} '
            f'question: {f.label}'
            for f in fields
        )
        parts.append(f"=== QUESTIONS ===\n{questions}")

        messages = [
            {"role": "system", "content": GENERATE_SYSTEM},
            {"role": "user", "content": "\n\n".join(parts)},
        ]
        parsed, model_id = await router.complete(
            client, messages, want_json=True, max_tokens=2000
        )
        if not isinstance(parsed, dict):
            raise ModelError("generation reply was not a JSON object", transient=True)
        return {str(k): str(v) for k, v in parsed.items()}, model_id
