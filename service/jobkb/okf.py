"""Open Knowledge Format (OKF v0.1) reader and writer.

The format is deliberately plain: a markdown file with YAML frontmatter, in a
directory tree whose shape *is* the concept hierarchy. Only `type` is required;
`title`, `description`, `resource`, `tags` and `timestamp` are the standard
optional fields, and a producer may add its own.

We add three producer fields:
  fields   a flat map of machine-readable values, for records that are data
           rather than prose (a job, a degree, a set of contact details)
  aliases  other wordings of the same question, learned from forms
  seen_on  which site and application a record has been used on

Prose records (an interview answer) keep their text in the body, which is where
OKF wants human-readable content to live. Data records get a generated table in
the body so the file still reads correctly in an editor or on GitHub, but the
frontmatter is always the source of truth — the body is rewritten on save.
"""

from __future__ import annotations

import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)

# Record types. The values are human strings, matching OKF's own examples
# ("BigQuery Table"), not enum-ish slugs.
T_PERSONAL = "Personal"
T_EXPERIENCE = "Work Experience"
T_EDUCATION = "Education"
T_SKILLS = "Skill Set"
T_CUSTOM = "Custom Fact"
T_ANSWER = "Answer"
T_APPLICATION = "Job Application"
T_RESUME = "Resume"
# The original document. A separate type from the text on purpose: the text
# grounds drafted answers, the file gets uploaded, and mixing them means a
# drafted answer is grounded in a filename.
T_RESUME_FILE = "Resume File"
T_MAPPING = "Form Mapping"

DATA_TYPES = {T_PERSONAL, T_EXPERIENCE, T_EDUCATION, T_SKILLS, T_CUSTOM, T_MAPPING}

STANDARD_KEYS = ["type", "title", "description", "resource", "tags", "timestamp"]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(text: str, fallback: str = "record") -> str:
    """A filename that stays readable. Accents folded, punctuation dropped."""
    norm = unicodedata.normalize("NFKD", str(text or ""))
    norm = "".join(c for c in norm if not unicodedata.combining(c))
    norm = re.sub(r"[^a-zA-Z0-9]+", "-", norm).strip("-").lower()
    norm = re.sub(r"-{2,}", "-", norm)
    return (norm or fallback)[:80]


@dataclass
class Record:
    """One OKF document. `path` is relative to the bundle root, POSIX style."""

    path: str
    type: str
    title: str = ""
    description: str = ""
    resource: str = ""
    tags: list[str] = field(default_factory=list)
    timestamp: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    seen_on: list[dict[str, Any]] = field(default_factory=list)
    body: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    # -- pointers ---------------------------------------------------------

    def pointer(self, key: str | None = None) -> str:
        """`profile/personal.md#email` — a path, optionally a field within it."""
        return f"{self.path}#{key}" if key else self.path

    def value(self, key: str | None = None) -> str | None:
        """The stored text a pointer refers to, or None when it is absent."""
        if key:
            raw = self.fields.get(key)
        elif self.type in DATA_TYPES:
            raw = self.fields.get("value")
        else:
            raw = self.body
        if raw is None:
            return None
        if isinstance(raw, (list, tuple)):
            raw = ", ".join(str(x) for x in raw if str(x).strip())
        text = str(raw).strip()
        return text or None

    def searchable(self) -> str:
        """Everything a retriever should match a form label against. Deliberately
        excludes stored answers for data records — matching a question against
        an email address produces noise, not hits."""
        parts = [self.title, self.description, " ".join(self.tags)]
        parts.extend(self.aliases)
        if self.type in (T_EXPERIENCE, T_EDUCATION):
            parts.extend(str(v) for v in self.fields.values())
        elif self.type in (T_PERSONAL, T_CUSTOM, T_SKILLS):
            parts.extend(self.fields.keys())
        return " ".join(p for p in parts if p).strip()


# -- parsing --------------------------------------------------------------


def parse(text: str, path: str) -> Record:
    m = FRONTMATTER.match(text.lstrip("\ufeff"))
    if not m:
        # A body-only markdown file is still usable knowledge; treat the first
        # heading as its title rather than rejecting the file.
        head = next((l for l in text.splitlines() if l.strip()), "")
        return Record(path=path, type="Note", title=head.lstrip("# ").strip(), body=text.strip())

    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        meta = {}
    body = m.group(2).strip()

    tags = meta.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    aliases = meta.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]

    known = set(STANDARD_KEYS) | {"fields", "aliases", "seen_on"}
    rec = Record(
        path=path,
        type=str(meta.get("type") or "Note"),
        title=str(meta.get("title") or ""),
        description=str(meta.get("description") or ""),
        resource=str(meta.get("resource") or ""),
        tags=[str(t) for t in tags],
        timestamp=str(meta.get("timestamp") or ""),
        fields=dict(meta.get("fields") or {}),
        aliases=[str(a) for a in aliases],
        seen_on=list(meta.get("seen_on") or []),
        body=body,
        extra={k: v for k, v in meta.items() if k not in known},
    )
    return rec


def _render_body(rec: Record) -> str:
    """Data records get their frontmatter mirrored into a readable table.
    Prose records keep whatever the author wrote."""
    if rec.type not in DATA_TYPES or not rec.fields:
        return rec.body.strip()

    rows = []
    for k, v in rec.fields.items():
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v)
        v = str(v).replace("|", "\\|").replace("\n", " ")
        rows.append(f"| {k} | {v} |")
    return "\n".join(["| Field | Value |", "|---|---|", *rows])


def serialize(rec: Record) -> str:
    meta: dict[str, Any] = {"type": rec.type}
    if rec.title:
        meta["title"] = rec.title
    if rec.description:
        meta["description"] = rec.description
    if rec.resource:
        meta["resource"] = rec.resource
    if rec.tags:
        meta["tags"] = rec.tags
    meta["timestamp"] = rec.timestamp or now_iso()
    if rec.fields:
        meta["fields"] = rec.fields
    if rec.aliases:
        meta["aliases"] = rec.aliases
    if rec.seen_on:
        meta["seen_on"] = rec.seen_on
    meta.update(rec.extra)

    head = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=100).rstrip()
    return f"---\n{head}\n---\n\n{_render_body(rec)}\n"


# -- disk -----------------------------------------------------------------


def read_file(root: Path, rel: str) -> Record:
    full = root / rel
    return parse(full.read_text(encoding="utf-8"), rel)


def write_file(root: Path, rec: Record) -> Path:
    """Atomic write, so a crash mid-save cannot leave a half-written record."""
    full = root / rec.path
    full.parent.mkdir(parents=True, exist_ok=True)
    rec.timestamp = now_iso()
    text = serialize(rec)

    fd, tmp = tempfile.mkstemp(dir=str(full.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, full)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return full


def walk(root: Path) -> list[Record]:
    """Every record in the bundle. `.index/` and `index.md` are skipped —
    the former is derived, the latter is navigation, not knowledge."""
    out: list[Record] = []
    if not root.exists():
        return out
    for full in sorted(root.rglob("*.md")):
        rel = full.relative_to(root).as_posix()
        if rel.startswith(".") or "/." in rel:
            continue
        if full.name == "index.md":
            continue
        try:
            out.append(parse(full.read_text(encoding="utf-8"), rel))
        except Exception:
            # One malformed file must not blind the whole service.
            continue
    return out


def write_index_md(root: Path, rel_dir: str, title: str, description: str = "") -> None:
    """OKF wants an index.md per directory. Regenerated, never hand-owned."""
    d = root / rel_dir if rel_dir else root
    d.mkdir(parents=True, exist_ok=True)
    children = []
    for child in sorted(d.iterdir()):
        if child.name == "index.md" or child.name.startswith("."):
            continue
        if child.is_dir():
            children.append(f"- [{child.name}/]({child.name}/index.md)")
        elif child.suffix == ".md":
            children.append(f"- [{child.stem}]({child.name})")

    # Deliberately no timestamp. An index is derived navigation, regenerated on
    # every write, so a timestamp would make all of them dirty in version
    # control every time anything at all changed.
    meta: dict[str, Any] = {"type": "Index", "title": title}
    if description:
        meta["description"] = description
    head = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).rstrip()
    body = "\n".join(children) if children else "_empty_"
    (d / "index.md").write_text(f"---\n{head}\n---\n\n{body}\n", encoding="utf-8", newline="\n")
