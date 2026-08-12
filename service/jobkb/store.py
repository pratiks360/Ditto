"""The knowledge base: OKF records on disk, plus an in-memory view of them.

Markdown is the source of truth. Everything here either reads the files or
writes them; nothing is held only in memory, and deleting a file deletes the
fact.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Iterable

from . import okf
from .config import settings
from .dates import sort_key
from .okf import (
    Record, T_ANSWER, T_APPLICATION, T_CUSTOM, T_EDUCATION, T_EXPERIENCE,
    T_MAPPING, T_PERSONAL, T_RESUME, T_SKILLS, slugify,
)

log = logging.getLogger("jobkb.store")

DIR_TITLES = {
    "": ("Job knowledge base", "Everything this machine knows about your applications."),
    "profile": ("Profile", "Who you are, where you worked, what you studied."),
    "profile/experience": ("Work experience", "Newest first."),
    "profile/education": ("Education", "Newest first."),
    "answers": ("Answers", "Reusable answers, matched to questions by wording."),
    "applications": ("Applications", "One record per job applied to."),
    "resume": ("Resume", "Source text."),
    "mappings": ("Form mappings", "Which field on which form maps to which record."),
}


class Store:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or settings.root)
        self.lock = threading.RLock()
        self.records: dict[str, Record] = {}
        self.load()

    # -- loading ----------------------------------------------------------

    def load(self) -> None:
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            self.records = {r.path: r for r in okf.walk(self.root)}
            log.info("loaded %d records from %s", len(self.records), self.root)

    def all(self) -> list[Record]:
        with self.lock:
            return list(self.records.values())

    def by_type(self, *types: str) -> list[Record]:
        wanted = set(types)
        return [r for r in self.all() if r.type in wanted]

    def get(self, path: str) -> Record | None:
        return self.records.get(str(path or "").lstrip("/"))

    # -- pointers ---------------------------------------------------------

    def resolve(self, pointer: str) -> str | None:
        """`profile/personal.md#email` -> the stored text, or None.

        Fails closed on purpose. A routing model returns pointers, never values,
        so anything it invents that is not a real pointer resolves to nothing
        and no fabricated text can reach a form field.
        """
        p = str(pointer or "").strip().lstrip("/")
        if not p or p in ("none", "generate"):
            return None
        path, _, key = p.partition("#")
        rec = self.get(path)
        if rec is None:
            return None
        return rec.value(key or None)

    def describe(self, pointer: str) -> str:
        p = str(pointer or "").strip().lstrip("/")
        path, _, key = p.partition("#")
        rec = self.get(path)
        if rec is None:
            return p
        return f"{rec.title or path}{' / ' + key if key else ''}"

    # -- writing ----------------------------------------------------------

    def save(self, rec: Record) -> Record:
        with self.lock:
            okf.write_file(self.root, rec)
            self.records[rec.path] = rec
            return rec

    def delete(self, path: str) -> bool:
        with self.lock:
            p = str(path or "").lstrip("/")
            full = self.root / p
            if not full.exists():
                return False
            full.unlink()
            self.records.pop(p, None)
            return True

    def refresh_indexes(self) -> None:
        """Regenerate every index.md. OKF wants one per directory."""
        with self.lock:
            dirs = {""}
            for path in self.records:
                parts = path.split("/")[:-1]
                for i in range(len(parts)):
                    dirs.add("/".join(parts[: i + 1]))
            for d in sorted(dirs):
                title, desc = DIR_TITLES.get(d, (d.split("/")[-1] or "Index", ""))
                okf.write_index_md(self.root, d, title, desc)

    # -- typed views ------------------------------------------------------

    def experience(self) -> list[Record]:
        """Newest first — every form that asks for work history expects it."""
        return sorted(
            self.by_type(T_EXPERIENCE),
            key=lambda r: sort_key(r.fields.get("startDate")),
            reverse=True,
        )

    def education(self) -> list[Record]:
        return sorted(
            self.by_type(T_EDUCATION),
            key=lambda r: (
                sort_key(r.fields.get("endYear")),
                sort_key(r.fields.get("startYear")),
            ),
            reverse=True,
        )

    def personal(self) -> Record | None:
        recs = self.by_type(T_PERSONAL)
        return recs[0] if recs else None

    def skills(self) -> Record | None:
        recs = self.by_type(T_SKILLS)
        return recs[0] if recs else None

    def customs(self) -> Record | None:
        recs = self.by_type(T_CUSTOM)
        return recs[0] if recs else None

    def answers(self) -> list[Record]:
        return self.by_type(T_ANSWER)

    def resume_text(self) -> str:
        """The resume as prose, for grounding drafted answers.

        Only T_RESUME — the attachment record is T_RESUME_FILE, and returning
        its body would ground every generated answer in a filename.
        """
        recs = [r for r in self.by_type(T_RESUME) if r.body.strip()]
        return recs[0].body if recs else ""

    def mapping_for(self, signature: str) -> Record | None:
        for rec in self.by_type(T_MAPPING):
            if rec.fields.get("signature") == signature:
                return rec
        return None

    # -- constructors -----------------------------------------------------

    def upsert_answer(self, question: str, answer: str, tags: Iterable[str] = ()) -> Record:
        """One record per question. A new wording of a question already stored
        becomes an alias on that record rather than a second file — the whole
        point of a shared knowledge base is that the answer exists once."""
        from .text import collapse_doubled, jaccard, normalize_label  # avoids a cycle

        q = collapse_doubled(question)
        with self.lock:
            best, best_score = None, 0.0
            for rec in self.answers():
                for candidate in [rec.title, *rec.aliases]:
                    s = jaccard(q, candidate)
                    if s > best_score:
                        best, best_score = rec, s

            if best is not None and best_score >= 0.6:
                known = {normalize_label(x) for x in [best.title, *best.aliases]}
                if normalize_label(q) not in known:
                    best.aliases.append(q)
                best.body = str(answer or "").strip()
                for t in tags:
                    if t not in best.tags:
                        best.tags.append(t)
                return self.save(best)

            rec = Record(
                path=f"answers/{slugify(q, 'answer')}.md",
                type=T_ANSWER,
                title=q,
                description="Reusable answer learned from a submitted form.",
                tags=list(tags),
                body=str(answer or "").strip(),
            )
            # Never silently overwrite a different question that slugified the
            # same way.
            if rec.path in self.records and self.records[rec.path].title != q:
                rec.path = rec.path[:-3] + f"-{abs(hash(q)) % 9973}.md"
            return self.save(rec)

    def upsert_custom(self, key: str, value: str) -> Record:
        rec = self.customs() or Record(
            path="profile/custom.md", type=T_CUSTOM, title="Custom facts",
            description="Short reusable facts: notice period, visa status, expected salary.",
        )
        rec.fields[str(key).strip()] = str(value).strip()
        return self.save(rec)

    def remember_mapping(self, signature: str, site: str, mapping: dict[str, str]) -> Record:
        """Field label -> pointer, for one form on one site. The next visit to
        the same form resolves locally: no retrieval, no model, no tokens."""
        rec = self.mapping_for(signature)
        if rec is None:
            rec = Record(
                path=f"mappings/{slugify(site, 'site')}-{signature[:8]}.md",
                type=T_MAPPING,
                title=f"Form mapping — {site}",
                description="Learned field-to-record mapping for one form.",
                fields={"signature": signature, "site": site},
            )
        existing = dict(rec.fields.get("map") or {})
        existing.update({k: v for k, v in mapping.items() if v})
        rec.fields["map"] = existing
        return self.save(rec)


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def reset_store(root: Path | None = None) -> Store:
    global _store
    _store = Store(root)
    return _store
