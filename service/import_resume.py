"""Put a resume into the knowledge base.

    python import_resume.py "C:\\path\\to\\resume.txt"

Two things happen, and the second one is optional:

  1. The text is stored. This alone matters: every drafted answer is grounded in
     it, so a question nothing has answered before gets written from your actual
     history rather than invented. No API key needed.

  2. If a key is set, the model reads it into structured profile records — jobs,
     degrees, skills — which is what fills "Employer", "From", "Job title" on a
     form. You see every value and what it would do before anything is written.

The service must be running. Plain .txt or .md; PDF is not read here — use
tools-resume-to-txt.py in the project root for that.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:8765"


def call(base: str, path: str, payload=None, token: str = ""):
    url = base.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-JobKB-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=300) as res:
            return json.loads(res.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise SystemExit(f"{path} failed: HTTP {exc.code} {detail}")
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"Cannot reach the service at {base} ({exc.reason}).\n"
            f"Start it with service\\start.bat, then try again."
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Import a resume into the knowledge base.")
    ap.add_argument("path", help="a .txt or .md resume")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--token", default="", help="only if you set JOBKB_TOKEN")
    ap.add_argument("--attach", metavar="FILE",
                    help="also store the original document (PDF/DOCX) so the "
                         "extension can attach it to upload fields")
    ap.add_argument("--store-only", action="store_true",
                    help="store the text, skip the AI extraction")
    ap.add_argument("--yes", action="store_true",
                    help="apply the extraction without asking")
    args = ap.parse_args()

    src = Path(args.path).expanduser()
    if not src.exists():
        raise SystemExit(f"No such file: {src}")
    if src.suffix.lower() == ".pdf":
        raise SystemExit(
            "PDFs are not read here. Convert it first:\n"
            '  python "tools-resume-to-txt.py" "%s"' % src
        )

    text = src.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        raise SystemExit(f"{src} is empty.")

    health = call(args.base, "/health", token=args.token)
    stored = call(args.base, "/ingest/resume",
                  {"text": text, "name": src.stem}, args.token)
    print(f"stored {stored['chars']} characters -> {stored['path']}")

    if args.attach:
        import base64
        import mimetypes

        doc = Path(args.attach).expanduser()
        if not doc.is_file():
            raise SystemExit(f"No such file: {doc}")
        mime = mimetypes.guess_type(doc.name)[0] or "application/octet-stream"
        out = call(args.base, "/ingest/resume-file", {
            "filename": doc.name, "mime": mime,
            "base64": base64.b64encode(doc.read_bytes()).decode("ascii"),
        }, args.token)
        print(f"attached {out['bytes'] / 1024:.0f} KB -> {out['file']}")

    if args.store_only:
        return 0

    if health.get("authError"):
        print("\nSkipping extraction: OpenRouter rejected the key.")
        print(f"  {health['authError']}")
        print("  The resume is still stored and still grounds drafted answers.")
        return 0
    if not health.get("hasKey"):
        print("\nSkipping extraction: no OPENROUTER_API_KEY set for the service.")
        print("  The resume is still stored and still grounds drafted answers.")
        return 0

    print("\nExtracting (three passes, this takes a minute)...")
    out = call(args.base, "/ingest/extract", {}, args.token)

    for failure in out.get("failures", []):
        print(f"  pass failed: {failure}")
    if out.get("models"):
        print("  " + ", ".join(out["models"]))

    report = out.get("report", [])
    if not report:
        print("Nothing was extracted.")
        return 1

    print(f"\n{'action':7} {'target':38} value")
    print("-" * 100)
    for row in report:
        note = f'   (keeping "{row["current"]}")' if row["action"] == "keep" else ""
        print(f'{row["action"]:7} {row["target"][:38]:38} {row["value"][:50]}{note}')

    counts: dict[str, int] = {}
    for row in report:
        counts[row["action"]] = counts.get(row["action"], 0) + 1
    print("\n" + "  ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    print("fill/add are written; keep/dup are skipped because you already have a value.")

    if not args.yes:
        reply = input("\nApply this? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("Nothing written.")
            return 0

    applied = call(args.base, "/ingest/apply", {"data": out["data"]}, args.token)
    for path in applied.get("written", []):
        print(f"  wrote {path}")
    print(f"\n{len(applied.get('written', []))} record(s) written to {health['root']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
