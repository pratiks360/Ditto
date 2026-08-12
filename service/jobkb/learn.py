"""The learning loop: a submitted form becomes knowledge.

Every submission teaches three things, and they are worth separating:

  * what you answered — a reusable Answer record, or a correction to one;
  * where it came from — the Job Application record, linking to the answers used;
  * how this form is shaped — a Form Mapping, so the next visit to the same form
    resolves with no retrieval and no model call at all.

Classification (is this a reusable answer, a profile fact, or noise?) uses the
model when one is available and falls back to rules when it is not. Nothing is
written to a profile record without the model or the rules being confident;
guessing wrong here pollutes the store permanently.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field as dc_field
from datetime import date
from typing import Any

import httpx

from .okf import Record, T_APPLICATION, now_iso, slugify
from .openrouter import ModelError, router
from .store import Store
from .text import is_opinion_question, looks_sensitive, normalize_label

log = logging.getLogger("jobkb.learn")

# Values that are noise: consent boxes, search boxes, single characters.
MIN_ANSWER_CHARS = 2
MAX_FACT_CHARS = 120

CLASSIFY_SYSTEM = """You sort answers a person typed into a job application form.

For each item decide what it is:
  "answer"      a reusable reply to a question that other applications will ask
                (motivation, strengths, notice period explained in prose,
                availability, why this company)
  "fact"        a short reusable fact worth storing under a key
                (notice period, expected salary, visa status, years of
                experience, willing to relocate)
  "profile"     something already covered by name/contact/work history/education
  "ignore"      one-off, site-specific, a consent checkbox, a search box, or
                anything containing a password or one-time code

Reply with a JSON object only: {"<item id>": {"kind": "...", "key": "<short
snake_case key, only when kind is fact>"}}.
No prose, no markdown fences, no lists, no pipe tables."""


@dataclass
class CapturedField:
    label: str = ""
    value: str = ""
    id: str = ""
    pointer: str = ""     # what the plan used, when the extension knows

    def key(self) -> str:
        return self.id or hashlib.sha1(
            normalize_label(self.label).encode("utf-8")
        ).hexdigest()[:10]


@dataclass
class Submission:
    site: str = ""
    url: str = ""
    signature: str = ""
    job_title: str = ""
    company: str = ""
    job_description: str = ""
    items: list[CapturedField] = dc_field(default_factory=list)


def _worth_keeping(item: CapturedField) -> bool:
    if not item.label.strip() or not str(item.value).strip():
        return False
    if len(str(item.value).strip()) < MIN_ANSWER_CHARS:
        return False
    if looks_sensitive(item.label):
        return False
    return True


def _rule_classify(item: CapturedField) -> dict[str, str]:
    """Used when no model is reachable. Deliberately conservative: it stores
    prose answers and leaves everything else alone."""
    value = str(item.value).strip()
    if is_opinion_question(item.label) or len(value) > MAX_FACT_CHARS:
        return {"kind": "answer"}
    if len(value.split()) > 4:
        return {"kind": "answer"}
    return {"kind": "ignore"}


async def classify(
    client: httpx.AsyncClient, items: list[CapturedField], allow_ai: bool = True
) -> tuple[dict[str, dict[str, str]], str]:
    if not items:
        return {}, "none"
    if allow_ai:
        try:
            listing = "\n".join(
                f"- id={i.key()} question: {i.label}\n  answer: {str(i.value)[:400]}"
                for i in items
            )
            parsed, model_id = await router.complete(
                client,
                [
                    {"role": "system", "content": CLASSIFY_SYSTEM},
                    {"role": "user", "content": listing},
                ],
                want_json=True, max_tokens=900,
            )
            if isinstance(parsed, dict):
                out: dict[str, dict[str, str]] = {}
                for k, v in parsed.items():
                    if isinstance(v, dict):
                        out[str(k)] = {"kind": str(v.get("kind") or "ignore"),
                                       "key": str(v.get("key") or "")}
                    else:
                        out[str(k)] = {"kind": str(v), "key": ""}
                return out, model_id
        except ModelError as exc:
            log.warning("classification unavailable, using rules: %s", exc)

    return {i.key(): _rule_classify(i) for i in items}, "rules"


async def learn(
    client: httpx.AsyncClient, store: Store, sub: Submission, allow_ai: bool = True
) -> dict[str, Any]:
    keepers = [i for i in sub.items if _worth_keeping(i)]
    dropped = len(sub.items) - len(keepers)

    verdicts, classifier = await classify(client, keepers, allow_ai)

    saved: list[dict[str, str]] = []
    used_pointers: list[str] = []
    mapping: dict[str, str] = {}

    for item in keepers:
        verdict = verdicts.get(item.key(), {"kind": "ignore", "key": ""})
        kind = verdict.get("kind", "ignore")

        if kind == "answer":
            rec = store.upsert_answer(item.label, item.value, tags=["learned"])
            _note_use(rec, sub)
            store.save(rec)
            saved.append({"kind": "answer", "path": rec.path, "title": rec.title})
            used_pointers.append(rec.pointer())
            mapping[normalize_label(item.label)] = rec.pointer()

        elif kind == "fact":
            key = verdict.get("key") or slugify(item.label, "fact").replace("-", "_")
            rec = store.upsert_custom(key, item.value)
            saved.append({"kind": "fact", "path": rec.path, "title": key})
            mapping[normalize_label(item.label)] = rec.pointer(key)

        elif kind == "profile" and item.pointer:
            # Already covered by a profile record; remember the wiring only.
            mapping[normalize_label(item.label)] = item.pointer

        if item.pointer and normalize_label(item.label) not in mapping:
            mapping[normalize_label(item.label)] = item.pointer

    application = _write_application(store, sub, used_pointers, len(keepers))

    if sub.signature and mapping:
        store.remember_mapping(sub.signature, sub.site or "unknown", mapping)

    store.refresh_indexes()

    return {
        "classifier": classifier,
        "captured": len(sub.items),
        "kept": len(keepers),
        "dropped": dropped,
        "saved": saved,
        "application": application.path,
        "mapped_fields": len(mapping),
    }


def _note_use(rec: Record, sub: Submission) -> None:
    entry = {"site": sub.site or "unknown", "date": date.today().isoformat()}
    if not any(
        s.get("site") == entry["site"] and s.get("date") == entry["date"]
        for s in rec.seen_on
    ):
        rec.seen_on.append(entry)


def _write_application(
    store: Store, sub: Submission, pointers: list[str], answered: int
) -> Record:
    title = " — ".join(p for p in [sub.job_title, sub.company or sub.site] if p) or sub.site
    slug = f"{date.today().isoformat()}-{slugify(title, 'application')}"
    path = f"applications/{slug}.md"

    rec = store.get(path) or Record(path=path, type=T_APPLICATION)
    rec.type = T_APPLICATION
    rec.title = title or "Job application"
    rec.description = f"Submitted on {sub.site or 'a site'}."
    rec.resource = sub.url
    rec.tags = sorted(set(rec.tags) | {"applied", slugify(sub.site or "site")})
    rec.fields.update({
        "site": sub.site, "signature": sub.signature,
        "company": sub.company, "role": sub.job_title,
        "submitted": now_iso(), "answered_fields": answered,
    })

    links = "\n".join(f"- [{store.describe(p)}](/{p.split('#')[0]})" for p in dict.fromkeys(pointers))
    rec.body = ("# Answers used\n" + links) if links else "# Answers used\n_none reusable_"
    return store.save(rec)
