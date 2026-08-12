"""Retrieval: turn the knowledge base into a catalog of pointers, then narrow
that catalog to the handful of candidates worth showing a model.

Two layers, both semantic:

  1. a cheap local prefilter — embeddings (cosine) blended with BM25 — that cuts
     hundreds of pointers down to tens;
  2. the routing model, which reads the survivors and decides.

The catalog carries pointer *names and descriptions only*. No answers, no email
address, no phone number. The model chooses which record belongs in a field; the
service substitutes the stored text itself. That is what makes it impossible for
a model to invent your employment dates — it never handles them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .embed import get_embedder
from .okf import T_ANSWER, T_CUSTOM, T_EDUCATION, T_EXPERIENCE, T_PERSONAL, T_SKILLS
from .store import Store
from .text import BM25, jaccard

ORDINALS = ["most recent", "second most recent", "third most recent", "fourth", "fifth"]

# How a personal field is usually worded on a form. Improves the lexical half of
# retrieval, which otherwise only has the camelCase key to work with.
PERSONAL_SYNONYMS = {
    "firstName": "first name given name forename",
    "lastName": "last name surname family name",
    "fullName": "full name your name",
    "email": "email address e-mail",
    "phone": "phone number mobile contact number telephone",
    "address": "street address address line",
    "city": "city town",
    "state": "state province region",
    "country": "country",
    "postalCode": "postal code zip code pin code",
    "linkedin": "linkedin profile url",
    "github": "github profile url",
    "website": "website portfolio personal site url",
    "nationality": "nationality citizenship",
}

EXPERIENCE_SYNONYMS = {
    "title": "job title role designation position",
    "company": "company employer organisation organization firm",
    "location": "location city country where based",
    "startDate": "start date from date joined date of joining",
    "endDate": "end date to date until left date of leaving",
    "description": "responsibilities duties role description what you did",
}

EDUCATION_SYNONYMS = {
    "degree": "degree qualification course",
    "field": "field of study major specialisation stream branch",
    "institution": "university college school institute institution",
    "location": "location city country",
    "startYear": "start year from year year of admission",
    "endYear": "end year to year graduation year year of passing completion",
    "gpa": "gpa grade percentage marks cgpa",
}


@dataclass
class Candidate:
    pointer: str
    label: str          # what the model reads
    kind: str           # personal | experience | education | skills | custom | answer
    block: int = -1     # which repeated block, 0 = most recent
    text: str = ""      # the searchable string (never the stored value)
    score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"pointer": self.pointer, "label": self.label,
                "kind": self.kind, "score": round(self.score, 3)}


def build_catalog(store: Store) -> list[Candidate]:
    """Every fillable thing the knowledge base knows, as a pointer."""
    out: list[Candidate] = []

    personal = store.personal()
    if personal:
        for key in personal.fields:
            syn = PERSONAL_SYNONYMS.get(key, "")
            out.append(Candidate(
                pointer=personal.pointer(key),
                label=f"your {key}",
                kind="personal",
                text=f"{key} {syn}",
            ))

    for i, rec in enumerate(store.experience()):
        who = f"{rec.fields.get('title', '')} at {rec.fields.get('company', '')}".strip(" at ")
        ordinal = ORDINALS[i] if i < len(ORDINALS) else f"job {i + 1}"
        for key in rec.fields:
            out.append(Candidate(
                pointer=rec.pointer(key),
                label=f"job {i + 1} ({ordinal}) — {who} — {key}",
                kind="experience",
                block=i,
                text=f"employment job {i + 1} {ordinal} {who} "
                     f"{EXPERIENCE_SYNONYMS.get(key, key)}",
            ))

    for i, rec in enumerate(store.education()):
        what = f"{rec.fields.get('degree', '')} {rec.fields.get('institution', '')}".strip()
        for key in rec.fields:
            out.append(Candidate(
                pointer=rec.pointer(key),
                label=f"education {i + 1} — {what} — {key}",
                kind="education",
                block=i,
                text=f"education qualification {i + 1} {what} "
                     f"{EDUCATION_SYNONYMS.get(key, key)}",
            ))

    skills = store.skills()
    if skills:
        out.append(Candidate(
            pointer=skills.pointer("skills"),
            label="your skills, comma separated",
            kind="skills",
            text="skills technologies tools competencies expertise proficiencies",
        ))

    customs = store.customs()
    if customs:
        for key in customs.fields:
            out.append(Candidate(
                pointer=customs.pointer(key),
                label=f"your saved fact: {key}",
                kind="custom",
                text=key,
            ))

    for rec in store.answers():
        aliases = "; ".join(rec.aliases)
        out.append(Candidate(
            pointer=rec.pointer(),
            label=f'your saved answer to "{rec.title}"',
            kind="answer",
            text=f"{rec.title} {aliases} {' '.join(rec.tags)}",
        ))

    return out


class Retriever:
    """Prefilter over the catalog. Rebuilt whenever the store changes; cheap
    enough that rebuilding is not worth avoiding."""

    def __init__(self, catalog: list[Candidate]) -> None:
        self.catalog = catalog
        corpus = [f"{c.label} {c.text}" for c in catalog]
        self.bm25 = BM25(corpus)
        self.embedder = get_embedder()
        self.matrix = self.embedder.encode(corpus) if catalog else None

    @property
    def semantic(self) -> bool:
        """Whether embeddings are available at all — not whether this particular
        catalog has any, which would report False on an empty knowledge base."""
        return self.embedder.available

    def search(self, query: str, k: int = 12) -> list[Candidate]:
        if not self.catalog:
            return []

        lex = self.bm25.scores(query)
        top_lex = max(lex) or 1.0
        lex = [s / top_lex for s in lex]

        if self.matrix is not None:
            qv = self.embedder.encode([query])
            sem = self.embedder.similarity(qv[0], self.matrix)
            blended = [0.55 * s + 0.45 * l for s, l in zip(sem, lex)]
        else:
            blended = lex

        ranked = sorted(zip(self.catalog, blended), key=lambda p: p[1], reverse=True)
        out = []
        for cand, score in ranked[:k]:
            copy = Candidate(**{**cand.__dict__, "score": float(score)})
            out.append(copy)
        return out


def exact_answer_match(store: Store, label: str, threshold: float = 0.6) -> tuple[str, float] | None:
    """A question we have literally seen before, by wording. Free, instant, and
    correct often enough to be worth trying before any model is involved."""
    best, best_score = None, 0.0
    for rec in store.answers():
        for candidate in [rec.title, *rec.aliases]:
            s = jaccard(label, candidate)
            if s > best_score:
                best, best_score = rec, s
    if best is not None and best_score >= threshold:
        return best.pointer(), best_score
    return None
