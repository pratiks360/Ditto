"""Label normalisation and lexical similarity.

Kept deliberately identical in behaviour to storage.js so the extension's local
fallback and the service agree about what counts as "the same question".
"""

from __future__ import annotations

import math
import re
from collections import Counter

# Curly quotes are flattened before comparison; a form that writes "What's your
# notice period?" with a typographic apostrophe must match one that doesn't.
CURLY = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})

# Function words carry no signal about *which* question is being asked. Dropping
# them is what lets "How many years of experience do you have with Apigee?" and
# "Years of experience with Apigee" reduce to the same three words and match.
STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "do", "does", "did",
    "of", "to", "in", "on", "at", "for", "with", "your", "you", "our", "we", "us",
    "please", "enter", "provide", "this", "that", "and", "or", "if", "any", "it",
    "how", "many", "much", "have", "has", "had", "will", "would", "can", "could",
    "should", "my", "me", "i", "s",
}

SIMILARITY_THRESHOLD = 0.5


def collapse_doubled(text: str) -> str:
    """Collapse a label that says the same thing twice.

    A page that renders a visible label and a screen-reader copy in one
    container reads back as "Do you have X?Do you have X?". Stored as-is that
    becomes the question's title and its filename. Token matching survives it
    (a set of words is the same set either way), but the record is unreadable
    and the path is twice as long as it needs to be.
    """
    s = str(text or "").strip()
    if len(s) < 12:
        return s
    half = len(s) // 2
    a, b = s[:half].strip(), s[len(s) - half:].strip()
    return a if a and a == b else s


def normalize_label(text: str) -> str:
    s = str(text or "").translate(CURLY).lower()
    s = re.sub(r"\(.*?\)", " ", s)          # "(optional)", "(if applicable)"
    s = re.sub(r"[*:?.,/\\_\-]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def tokens(text: str) -> list[str]:
    return [t for t in normalize_label(text).split(" ") if t and t not in STOPWORDS]


def jaccard(a: str, b: str) -> float:
    ta, tb = set(tokens(a)), set(tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class BM25:
    """Small in-memory BM25. No dependency, and good enough as the lexical half
    of retrieval — it rewards rare words, which is what distinguishes
    "notice period" from every other field on the form."""

    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs = [tokens(d) for d in docs]
        self.n = len(self.docs) or 1
        self.avg_len = (sum(len(d) for d in self.docs) / self.n) or 1.0
        self.tf = [Counter(d) for d in self.docs]
        df: Counter[str] = Counter()
        for d in self.docs:
            df.update(set(d))
        self.idf = {
            t: math.log(1 + (self.n - c + 0.5) / (c + 0.5)) for t, c in df.items()
        }

    def scores(self, query: str) -> list[float]:
        q = tokens(query)
        out = []
        for i, tf in enumerate(self.tf):
            dl = len(self.docs[i]) or 1
            s = 0.0
            for t in q:
                f = tf.get(t, 0)
                if not f:
                    continue
                idf = self.idf.get(t, 0.0)
                s += idf * (f * (self.k1 + 1)) / (
                    f + self.k1 * (1 - self.b + self.b * dl / self.avg_len)
                )
            out.append(s)
        return out


# Words that must not be title-cased. A resume is full of them, and "Ibm",
# "Fze" or "Mba" in a form field looks like a typo you made.
KEEP_AS_IS = {
    "IBM", "FZE", "LLC", "LLP", "LTD", "PVT", "INC", "PLC", "GMBH", "NV", "SA",
    "MBA", "BE", "BSC", "MSC", "BTECH", "MTECH", "MCA", "BCA", "PGDM", "PHD",
    "AWS", "GCP", "API", "APIS", "SQL", "ETL", "CI", "CD", "UI", "UX", "AI",
    "ML", "IT", "HR", "QA", "SAP", "CRM", "ERP", "SDK", "REST", "SOAP", "RFP",
    "USA", "UAE", "UK", "US", "EU", "NCR", "KJ",
}


def _cap_word(word: str) -> str:
    letters = re.sub(r"[^A-Za-z]", "", word)
    if not letters:
        return word
    # "B.E." / "K.J." — dotted initialisms stay upper.
    if re.fullmatch(r"(?:[A-Za-z]\.){2,}", word):
        return word.upper()
    if letters.upper() in KEEP_AS_IS:
        return word.upper()
    return word[:1].upper() + word[1:].lower()


def title_case(text: str) -> str:
    """Re-case a string that arrived in one uniform case.

    Deliberately conservative: text that is already mixed case was written that
    way on purpose ("iPhone", "eBay", "McKinsey"), so it is left alone. Only
    ALL-CAPS or all-lowercase input is touched.
    """
    s = str(text or "").strip()
    letters = re.sub(r"[^A-Za-z]", "", s)
    if not letters or not (letters.isupper() or letters.islower()):
        return s
    return re.sub(r"[^\s/]+", lambda m: _cap_word(m.group(0)), s)


OPINION_QUESTION = re.compile(
    r"\bwhy (do you |would you |are you )?(want|wish|like|interested|choose|apply|us|this)\b"
    r"|\bwhat (makes|attracts|draws|excites|motivates)\b"
    r"|\bgood fit\b|\bright fit\b|\bfit for (this|the) role\b"
    r"|\btell us about\b|\bcover letter\b|\bin your own words\b"
    r"|\bwhy should we\b|\bmotivat",
    re.I,
)

CURRENT_ROLE_LABEL = re.compile(
    r"\b(currently|presently|still)\b[^.]*\b(work|working|employed|here|role|position)\b"
    r"|\bcurrent (job|role|position|employer)\b|\bpresent employer\b|\bi work here\b",
    re.I,
)

SENSITIVE = re.compile(
    r"disabilit|veteran|\brace\b|ethnic|gender|sexual orientation"
    r"|self.?identif|\beeo\b|pronoun",
    re.I,
)


def is_opinion_question(label: str) -> bool:
    return bool(OPINION_QUESTION.search(str(label or "")))


def is_current_role_label(label: str) -> bool:
    return bool(CURRENT_ROLE_LABEL.search(str(label or "")))


def looks_sensitive(label: str) -> bool:
    """Demographic self-identification. Never auto-filled, never generated."""
    return bool(SENSITIVE.search(str(label or "")))
