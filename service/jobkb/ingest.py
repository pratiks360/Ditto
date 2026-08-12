"""Resume ingestion: plain text in, OKF profile records out.

Done in three small passes rather than one large one. A single request that has
to emit the whole profile produces several kilobytes of JSON, and free models
either time out or truncate halfway through; three short requests each finish
comfortably and a failure only costs one of them.

Nothing is written to disk here. The caller gets a proposal, shows it, and calls
`apply` once you have approved it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

from .dates import sort_key
from .okf import (
    Record, T_EDUCATION, T_EXPERIENCE, T_PERSONAL, T_RESUME, T_RESUME_FILE,
    T_SKILLS, slugify,
)
from .openrouter import ModelError, router
from .store import Store
from .text import jaccard, normalize_label, title_case

log = logging.getLogger("jobkb.ingest")

BASE_RULES = """Read the resume and return only what it actually says. Invent
nothing. Leave a field out rather than guessing it.

Reply with a JSON object only. No prose, no markdown fences, no lists, no pipe
tables, no numbering. Do not mirror the resume's own layout."""

PASSES: list[dict[str, str]] = [
    {
        "name": "personal",
        "prompt": BASE_RULES + """

Return: {"personal": {"firstName","lastName","email","phone","city","country",
"linkedin","github","website"}}

Only keys you can fill from the text.""",
    },
    {
        "name": "experience",
        "prompt": BASE_RULES + """

Return: {"experience": [{"title","company","location","startDate","endDate",
"description"}]}

  - Newest job first.
  - Dates exactly as written in the resume ("Sept 2025", "Present").
  - "description" is ONE sentence of at most 25 words summarising the role. If
    it would just repeat the job title, leave it out.""",
    },
    {
        "name": "education_skills",
        "prompt": BASE_RULES + """

Return: {"education": [{"degree","field","institution","location","startYear",
"endYear","gpa"}], "skills": ["..."]}

  - Newest qualification first.
  - "skills" are individual technologies or competencies, not sentences.""",
    },
]


async def extract(
    client: httpx.AsyncClient, resume_text: str, on_progress=None
) -> dict[str, Any]:
    """Run the passes and merge them into one proposal."""
    merged: dict[str, Any] = {}
    models: list[str] = []
    failures: list[str] = []

    for i, spec in enumerate(PASSES, start=1):
        if on_progress:
            on_progress(f"pass {i}/{len(PASSES)}: {spec['name']}")
        try:
            parsed, model_id = await router.complete(
                client,
                [
                    {"role": "system", "content": spec["prompt"]},
                    {"role": "user", "content": resume_text[:16000]},
                ],
                want_json=True, max_tokens=1800,
            )
            models.append(f"{spec['name']}={model_id}")
            if isinstance(parsed, dict):
                merged.update(parsed)
        except ModelError as exc:
            failures.append(f"{spec['name']}: {exc}")
            log.warning("extraction pass %s failed: %s", spec["name"], exc)

    if not merged and failures:
        raise ModelError("; ".join(failures), transient=True)

    return {"data": _normalize(merged), "models": models, "failures": failures}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


# Fields a resume commonly shouts in ALL CAPS, and that a form should not.
# Email, phone, URLs and free prose are left exactly as written.
RECASE = {
    "firstName", "lastName", "fullName", "city", "country", "state",
    "title", "company", "location", "degree", "field", "institution",
}


def _clean_field(key: str, value: Any) -> str:
    text = _clean(value)
    return title_case(text) if key in RECASE else text


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    personal = {
        k: _clean_field(k, v)
        for k, v in (data.get("personal") or {}).items()
        if _clean(v)
    }

    experience = []
    for row in data.get("experience") or []:
        if not isinstance(row, dict):
            continue
        entry = {k: _clean_field(k, v) for k, v in row.items() if _clean(v)}
        if not entry.get("company") and not entry.get("title"):
            continue
        # A description that merely repeats the job title is noise, and models
        # produce it constantly.
        if entry.get("description", "").lower() == entry.get("title", "").lower():
            entry.pop("description", None)
        experience.append(entry)
    experience.sort(key=lambda e: sort_key(e.get("startDate")), reverse=True)

    education = []
    for row in data.get("education") or []:
        if not isinstance(row, dict):
            continue
        entry = {k: _clean_field(k, v) for k, v in row.items() if _clean(v)}
        if entry.get("degree") or entry.get("institution"):
            education.append(entry)
    education.sort(key=lambda e: sort_key(e.get("endYear")), reverse=True)

    skills, seen = [], set()
    for s in data.get("skills") or []:
        c = _clean(s)
        if c and c.lower() not in seen and len(c) < 60:
            seen.add(c.lower())
            skills.append(c)

    return {"personal": personal, "experience": experience,
            "education": education, "skills": skills}


def _same(a: Any, b: Any, threshold: float) -> bool:
    """Whether two pieces of resume text name the same thing.

    Exact comparison is not enough: run the extraction twice and one model
    writes "Post Graduate Diploma" where another writes "Post Graduate Diploma
    in Advanced Computing". Same qualification, and storing both means a form
    offering three degrees gets four.
    """
    x, y = normalize_label(a), normalize_label(b)
    if not x or not y:
        return False
    if x == y or x in y or y in x:
        return True
    return jaccard(x, y) >= threshold


def _find_job(store: Store, row: dict[str, Any]):
    for rec in store.experience():
        if (_same(rec.fields.get("company"), row.get("company"), 0.8)
                and _same(rec.fields.get("title"), row.get("title"), 0.5)):
            return rec
    return None


def _find_degree(store: Store, row: dict[str, Any]):
    for rec in store.education():
        if (_same(rec.fields.get("institution"), row.get("institution"), 0.8)
                and _same(rec.fields.get("degree"), row.get("degree"), 0.5)):
            return rec
    return None


def plan_apply(store: Store, data: dict[str, Any]) -> list[dict[str, str]]:
    """What `apply` would do, so it can be reviewed first. Existing values are
    never overwritten — yours win."""
    report: list[dict[str, str]] = []

    personal = store.personal()
    for key, value in (data.get("personal") or {}).items():
        current = (personal.fields.get(key) if personal else None)
        report.append({
            "target": f"profile/personal.md#{key}", "value": value,
            "action": "keep" if current else "fill",
            "current": str(current or ""),
        })

    for row in data.get("experience") or []:
        report.append({
            "target": "profile/experience/",
            "action": "dup" if _find_job(store, row) else "add",
            # ASCII only: this report is printed to the Windows console.
            "value": f"{row.get('title', '')} - {row.get('company', '')} "
                     f"({row.get('startDate', '?')} to {row.get('endDate', '?')})",
            "current": "",
        })

    for row in data.get("education") or []:
        report.append({
            "target": "profile/education/",
            "action": "dup" if _find_degree(store, row) else "add",
            "value": f"{row.get('degree', '')} - {row.get('institution', '')}",
            "current": "",
        })

    skills_rec = store.skills()
    known = {s.lower() for s in (skills_rec.fields.get("skills") or [])} if skills_rec else set()
    new = [s for s in (data.get("skills") or []) if s.lower() not in known]
    if new:
        report.append({"target": "profile/skills.md#skills", "action": "add",
                       "value": ", ".join(new), "current": str(len(known))})

    return report


def apply(store: Store, data: dict[str, Any]) -> dict[str, Any]:
    written: list[str] = []

    personal_data = data.get("personal") or {}
    if personal_data:
        rec = store.personal() or Record(
            path="profile/personal.md", type=T_PERSONAL, title="Personal details",
            description="Name and contact details.",
        )
        for key, value in personal_data.items():
            if not rec.fields.get(key):
                rec.fields[key] = value
        store.save(rec)
        written.append(rec.path)

    for row in data.get("experience") or []:
        existing = _find_job(store, row)
        if existing is not None:
            # Already stored, possibly worded differently. Fill only the gaps —
            # a re-run must never overwrite an edit you made by hand.
            changed = False
            for key, value in row.items():
                if value and not existing.fields.get(key):
                    existing.fields[key] = value
                    changed = True
            if changed:
                store.save(existing)
                written.append(existing.path)
            continue
        slug = slugify(f"{row.get('company', '')}-{row.get('title', '')}", "job")
        rec = Record(
            path=f"profile/experience/{slug}.md", type=T_EXPERIENCE,
            title=f"{row.get('title', '')} — {row.get('company', '')}".strip(" —"),
            description=row.get("description", ""),
            tags=["experience"], fields=row,
        )
        store.save(rec)
        written.append(rec.path)

    for row in data.get("education") or []:
        existing = _find_degree(store, row)
        if existing is not None:
            changed = False
            for key, value in row.items():
                if value and not existing.fields.get(key):
                    existing.fields[key] = value
                    changed = True
            if changed:
                store.save(existing)
                written.append(existing.path)
            continue
        slug = slugify(f"{row.get('institution', '')}-{row.get('degree', '')}", "degree")
        rec = Record(
            path=f"profile/education/{slug}.md", type=T_EDUCATION,
            title=f"{row.get('degree', '')} — {row.get('institution', '')}".strip(" —"),
            tags=["education"], fields=row,
        )
        store.save(rec)
        written.append(rec.path)

    skills = data.get("skills") or []
    if skills:
        rec = store.skills() or Record(
            path="profile/skills.md", type=T_SKILLS, title="Skills",
            description="Technologies and competencies.", fields={"skills": []},
        )
        current = list(rec.fields.get("skills") or [])
        known = {s.lower() for s in current}
        current.extend(s for s in skills if s.lower() not in known)
        rec.fields["skills"] = current
        store.save(rec)
        written.append(rec.path)

    store.refresh_indexes()
    return {"written": written}


RESUME_FILE_RECORD = "resume/attachment.md"


def save_resume_file(store: Store, data: bytes, filename: str, mime: str) -> Record:
    """Keep the original document — the one that gets attached to applications.

    The bytes go next to the markdown rather than inside it: a PDF base64'd into
    frontmatter would make the bundle unreadable, and OKF's whole point is files
    you can open. The record is the pointer to it.
    """
    safe = Path(filename).name or "resume.pdf"
    target = store.root / "resume" / safe
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

    rec = Record(
        path=RESUME_FILE_RECORD,
        type=T_RESUME_FILE,
        title=f"Resume file — {safe}",
        description="The original document, attached to applications as-is.",
        tags=["attachment"],
        fields={
            "filename": safe,
            "mime": mime or "application/pdf",
            "bytes": len(data),
            "file": f"resume/{safe}",
            "sha1": hashlib.sha1(data).hexdigest()[:12],
        },
        body=f"[{safe}](./{safe}) — {len(data) / 1024:.0f} KB",
    )
    store.save(rec)
    store.refresh_indexes()
    return rec


def read_resume_file(store: Store) -> tuple[bytes, str, str] | None:
    """(bytes, filename, mime) for the stored attachment, or None."""
    rec = store.get(RESUME_FILE_RECORD)
    if rec is None:
        return None
    path = store.root / str(rec.fields.get("file") or "")
    if not path.is_file():
        return None
    return (
        path.read_bytes(),
        str(rec.fields.get("filename") or path.name),
        str(rec.fields.get("mime") or "application/octet-stream"),
    )


TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".text"}


def _in_container() -> bool:
    return Path("/run/.containerenv").exists() or Path("/.dockerenv").exists()


def _path_hint(path: Path) -> str:
    """Why a path that exists on your desktop is not found by the service.

    The commonest cause by far: a Windows path given to a service running inside
    a container, where `C:\\Users\\...` means nothing and the file was never
    mounted. Saying so beats "no such file".
    """
    looks_windows = "\\" in str(path) or re.match(r"^[A-Za-z]:", str(path))
    if looks_windows and _in_container():
        return ("  <- that is a Windows path, but the service is running in a "
                "container. Mount the folder and use a path under /resume, or "
                "use the picker in the extension's Options.")
    if looks_windows and os.name != "nt":
        return "  <- that is a Windows path, but the service is not on Windows."
    return ""


def sync_from_disk(store: Store) -> list[str]:
    """Pick up the resume from wherever JOBKB_RESUME points.

    Runs at every start. Only writes when the file on disk differs from what is
    stored, so a resume you keep editing stays in step without the store being
    rewritten (and its timestamp churned) on every restart.

    A missing path is reported, not raised: a stale environment variable must
    not stop the service from starting.
    """
    from .config import settings  # noqa: PLC0415 - avoids an import cycle

    notes: list[str] = []

    text_path = settings.resume_path
    file_path = settings.resume_file_path

    # Pointing JOBKB_RESUME straight at a PDF is the obvious mistake; treat it
    # as the attachment rather than storing binary as "resume text".
    if text_path and text_path.suffix.lower() not in TEXT_SUFFIXES:
        if file_path is None:
            file_path, text_path = text_path, None
            notes.append("JOBKB_RESUME is not a text file — treating it as the attachment")

    if text_path:
        if not text_path.is_file():
            notes.append(f"JOBKB_RESUME: no such file: {text_path}{_path_hint(text_path)}")
        else:
            text = text_path.read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                notes.append(f"JOBKB_RESUME: {text_path.name} is empty")
            elif store.resume_text().strip() == text:
                notes.append(f"resume text already current ({text_path.name})")
            else:
                rec = save_resume(store, text, text_path.stem)
                notes.append(f"imported resume text from {text_path.name} -> {rec.path}")

    if file_path:
        if not file_path.is_file():
            notes.append(
                f"JOBKB_RESUME_FILE: no such file: {file_path}{_path_hint(file_path)}"
            )
        else:
            data = file_path.read_bytes()
            current = store.get(RESUME_FILE_RECORD)
            digest = hashlib.sha1(data).hexdigest()[:12]
            if current and current.fields.get("sha1") == digest:
                notes.append(f"resume file already current ({file_path.name})")
            else:
                import mimetypes  # noqa: PLC0415

                mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
                save_resume_file(store, data, file_path.name, mime)
                notes.append(f"imported resume file from {file_path.name} "
                             f"({len(data) / 1024:.0f} KB)")

    for note in notes:
        log.info("%s", note)
    return notes


RESUME_TEXT_RECORD = "resume/resume.md"


def save_resume(store: Store, text: str, name: str = "resume") -> Record:
    """Store the resume text. There is exactly one.

    The path is fixed rather than derived from the filename: naming it after the
    source meant importing `resume.txt` sat alongside an earlier
    `alex-rivera-resume.md`, and whichever loaded first silently became the
    text that grounds every drafted answer.
    """
    rec = Record(
        path=RESUME_TEXT_RECORD, type=T_RESUME,
        title="Resume", description="Source text, used to ground generated answers.",
        fields={"source": str(name or "resume")},
        body=str(text or "").strip(),
    )
    store.save(rec)

    # Retire any earlier copy stored under its old, source-derived name.
    for old in store.by_type(T_RESUME):
        if old.path != rec.path:
            store.delete(old.path)
            log.info("replaced earlier resume record %s", old.path)

    store.refresh_indexes()
    return rec
