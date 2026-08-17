"""
Converts the PDF resume to the plain text the extension's resume upload expects.

Two things make this more than a pdftotext call:

1. The PDF is LaTeX-set with T1 (Cork) encoded fonts. Ligature glyphs live at
   control-code positions that every extractor silently drops, so a naive
   extract yields "Conuent", "Clayn", "rst-of-its-kind". T1 maps them back.
2. Layout is column-based. Column x-offsets identify wrapped continuations, the
   date column and the location column, which is what lets work experience and
   education be re-emitted as ordered entries rather than loose lines.
"""

import re
import sys
import pymupdf

SRC = r"C:\Users\you\Documents\resume.pdf"
OUT = "resume.txt"
BULLET = "\u2022"

T1 = {0x15: "-", 0x16: "--", 0x19: "i", 0x1a: "j",
      0x1b: "ff", 0x1c: "fi", 0x1d: "fl", 0x1e: "ffi", 0x1f: "ffl",
      0x88: BULLET + " "}

# Column x-offsets, from the layout histogram.
CONTINUATION = {59, 183}     # wrapped body / skills-table second line
DATE_COL = range(450, 480)
LOC_COL = range(500, 600)

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def read_lines(path):
    """Flat list of (text, x0, y, page, block) with ligatures restored."""
    out = []
    for pno, page in enumerate(pymupdf.open(path)):
        for bno, block in enumerate(page.get_text("rawdict")["blocks"]):
            for line in block.get("lines", []):
                parts, prev_x1 = [], None
                for span in line["spans"]:
                    x0, x1 = span["bbox"][0], span["bbox"][2]
                    if prev_x1 is not None and x0 - prev_x1 > 1.5:
                        parts.append("  ")
                    parts.append("".join(T1.get(ord(c["c"]), c["c"])
                                         for c in span["chars"]))
                    prev_x1 = x1
                text = re.sub(r"\s+", " ", "".join(parts)).strip()
                if text and not re.fullmatch(r"\d{1,2}", text):
                    out.append((text, round(line["bbox"][0]),
                                line["bbox"][1], pno, bno))
    return out


def join_wrapped(base, tail):
    """Append a wrapped line, undoing the wrap hyphen but keeping real ones.

    "environ-" + "ment" -> "environment", but "proof-of-" + "value" keeps its
    hyphen: a trailing token that already contains a hyphen is a compound word
    broken at an existing hyphen, not a word split by the typesetter.
    """
    if base.endswith("-") and re.match(r"[a-z]", tail):
        token = base.rsplit(" ", 1)[-1]
        if "-" in token[:-1]:
            return base + tail          # proof-of- + value -> proof-of-value
        return base[:-1] + tail         # environ-  + ment  -> environment
    return base + " " + tail


def start_key(period):
    """Sort key for 'Sept 2025 - Present' -> (2025, 9). Missing -> (0, 0)."""
    m = re.match(r"([A-Za-z]+)?\s*(\d{4})", period or "")
    if not m:
        return (0, 0)
    month = MONTHS.get((m.group(1) or "")[:3].lower(), 0)
    return (int(m.group(2)), month)


def main():
    lines = read_lines(SRC)

    section = None
    flat = []          # rendered non-entry content
    jobs, edu = [], []
    cur = None

    for i, (text, x0, y, pno, bno) in enumerate(lines):
        upper = text.upper()
        if upper in {"PROFILE SUMMARY", "AREAS OF EXCELLENCE", "TECHNICAL SKILLS",
                     "WORK EXPERIENCE", "EDUCATION"} or "NOTEWORTHY ACCOMPLISHMENTS" in upper:
            section, cur = text, None
            flat.append(("section", text))
            continue

        if section == "WORK EXPERIENCE":
            nxt = lines[i + 1] if i + 1 < len(lines) else None
            if x0 in DATE_COL:
                cur["period"] = text
            elif x0 in LOC_COL:
                cur["location"] = text
            elif text.startswith(BULLET):
                cur["bullets"].append(text[1:].strip())
            elif x0 in CONTINUATION and cur and cur["bullets"]:
                cur["bullets"][-1] = join_wrapped(cur["bullets"][-1], text)
            elif nxt and nxt[1] in DATE_COL:          # title, date follows
                cur = {"title": text, "period": "", "company": "",
                       "location": "", "bullets": []}
                jobs.append(cur)
            elif cur and not cur["company"]:
                cur["company"] = text
            continue

        if section == "EDUCATION":
            if text.startswith(BULLET):
                cur = {"degree": text[1:].strip(), "institution": "", "location": ""}
                edu.append(cur)
            elif x0 in LOC_COL and cur:
                cur["location"] = text
            elif cur:
                cur["institution"] = (cur["institution"] + " " + text).strip()
            continue

        # Everything else keeps its lines, folding wraps back together. A
        # trailing hyphen means the wrap split a word, whatever the column.
        if flat and flat[-1][0] == "text" and flat[-1][1].endswith("-") \
                and re.match(r"[a-z]", text):
            flat[-1] = ("text", join_wrapped(flat[-1][1], text))
        elif x0 in CONTINUATION and flat and flat[-1][0] == "text":
            flat[-1] = ("text", join_wrapped(flat[-1][1], text))
        elif (flat and flat[-1][0] == "text" and not text.startswith(BULLET)
              and not flat[-1][1].startswith(BULLET)
              and (pno, bno) == (lines[i - 1][3], lines[i - 1][4])):
            flat[-1] = ("text", join_wrapped(flat[-1][1], text))
        else:
            flat.append(("text", text))

    # Newest first. Education carries no dates in this PDF, so its listed order
    # (MBA, PG Diploma, B.E.) is already highest/most-recent first and is kept.
    jobs_sorted = sorted(jobs, key=lambda j: start_key(j["period"]), reverse=True)
    edu_sorted = edu

    body = []
    for kind, text in flat:
        if kind == "section":
            body += ["", text, ""]
            if text.upper() == "WORK EXPERIENCE":
                for j in jobs_sorted:
                    where = ", ".join(x for x in (j["company"], j["location"]) if x)
                    body.append(f"{j['title']} | {where} | {j['period']}")
                    body += [f"{BULLET} {b}" for b in j["bullets"]]
                    body.append("")
            elif text.upper() == "EDUCATION":
                for e in edu_sorted:
                    where = ", ".join(x for x in (e["institution"], e["location"]) if x)
                    body.append(f"{BULLET} {e['degree']} | {where}")
        else:
            body.append(text)

    out = "\n".join(body)
    while "\n\n\n" in out:
        out = out.replace("\n\n\n", "\n\n")
    open(OUT, "w", encoding="utf-8").write(out.strip() + "\n")

    print("work experience (newest first):")
    for j in jobs_sorted:
        print(f"   {start_key(j['period'])}  {j['period']:<22} {j['title']} @ {j['company']}")
    print("\neducation (as listed):")
    for e in edu_sorted:
        print(f"   {e['degree'][:52]}")

    keys = [start_key(j["period"]) for j in jobs_sorted]
    print("\ndescending:", keys == sorted(keys, reverse=True))
    print("dangling hyphens:", [l for l in body if l.endswith("-")])
    for probe in ("Confluent", "Clayfin", "first-of-its-kind", "proof-of-value",
                  "end-to-end", "effort", "workflows"):
        if probe not in out:
            print("MISSING:", probe)
            sys.exit(1)
    print("ligature probes: all present |", len(out), "chars")


main()
