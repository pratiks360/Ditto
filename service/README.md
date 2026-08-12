# Job Knowledge Service

A local service that owns your job-application knowledge and answers "what goes
in this field?" for the browser extension.

Everything is stored as plain markdown in [Open Knowledge Format][okf] — files
you can read in any editor, keep in git, hand-edit, or delete. There is no
database you cannot open.

[okf]: https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf

---

## Run it under Podman (Windows)

```bat
podman.bat up
```

Builds the image if needed and starts it. `podman.bat down` stops it,
`podman.bat logs` follows the log, `podman.bat rebuild` forces a fresh image.

Two details that matter:

- Inside the container the service listens on `0.0.0.0` (`JOBKB_HOST`), because
  `127.0.0.1` there would only be reachable from within the container. The port
  is published as `127.0.0.1:8765:8765`, so the host still exposes it on
  loopback only — dropping that prefix would put your employment history on
  every network interface.
- Your knowledge base is a **bind mount** of `%USERPROFILE%\.jobkb`, not image
  content. It survives every rebuild and stays editable in Notepad.

The embedding model is baked into the image, so the first fill does not wait for
a download. `podman-compose up -d --build` works too — see `compose.yaml`.

## Run it directly (Windows)

The venv already exists. Set your key once, then start it:

```bat
setx OPENROUTER_API_KEY sk-or-v1-your-key-here
```

`setx` writes it permanently to your user environment — open a **new** terminal
afterwards, the current one will not see it. Then double-click `start.bat`, or:

```bat
start.bat
```

That is all it needs. To rebuild the venv from scratch:

```bat
py -m venv .venv && .venv\Scripts\pip install -r requirements.txt
```

Then in the extension's Options page, tick **Use the knowledge service**, leave
the address as `http://127.0.0.1:8765`, and press **Test service**.

### Start it with Windows

The service has to be running for `Ctrl+Shift+F` to do its clever half, so it is
worth starting automatically. Press `Win+R`, type `shell:startup`, and put a
shortcut to `start-hidden.vbs` in the folder that opens. That runs it with no
console window.

To stop it: Task Manager, or

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object CommandLine -like '*run.py*' |
  ForEach-Object { Stop-Process -Id $_.ProcessId }
```

### Settings

| Variable | Default | Meaning |
|---|---|---|
| `OPENROUTER_API_KEY` | — | Optional. Without it the service does retrieval only: it still fills everything it can match, but cannot route or draft. |
| `JOBKB_ROOT` | `%USERPROFILE%\.jobkb` | Where the markdown lives. |
| `JOBKB_PORT` | `8765` | |
| `JOBKB_TOKEN` | — | Optional shared secret; the extension must then send it too. |
| `JOBKB_MODEL` | — | Pin one model instead of discovering free ones. |
| `JOBKB_TIMEOUT` | `90` | Seconds per model call. |

Set any of them the same way (`setx JOBKB_PORT 9000`), or edit `start.bat`.

It binds `127.0.0.1`. Nothing on your network can reach it, and Windows Firewall
will not prompt — loopback is never filtered.

## What it stores

```
C:\Users\<you>\.jobkb\
  profile/
    personal.md              name, contact
    experience/*.md          one file per job, newest first
    education/*.md
    skills.md
    custom.md                notice period, visa status, expected salary
  answers/*.md               one file per question, with every wording seen
  applications/*.md          one file per job applied to, linking to answers used
  mappings/*.md              learned field->record wiring, per form
  resume/*.md
  .index/                    derived, disposable
```

A record is frontmatter plus body:

```markdown
---
type: Answer
title: Why do you want to work here?
tags: [motivation, opinion]
timestamp: 2026-08-11T09:12:00Z
aliases:
  - What attracts you to this position?
seen_on:
  - {site: careers.acme.com, date: 2026-08-11}
---
Your streaming work maps onto the Kafka pipelines I built at Northwind.
```

Answers are **global**. The same record is reused across every site; a new
wording becomes another alias rather than a second file. `applications/` links to
answers, it does not copy them.

`.index/` holds the derived SQLite and embeddings. Delete it whenever you like —
`POST /reindex` rebuilds it from the markdown, which is the only source of truth.

## How a field gets filled

Cheapest first. Most fields never reach a model.

1. **Remembered form** — this exact form was filled before; the wiring is stored.
2. **Known question** — the wording matches a stored answer.
3. **Derived** — "I currently work here" follows from a role with no end date.
4. **AI routing** — the local prefilter (embeddings + BM25) narrows the pointer
   catalog to a handful of candidates per field; the model picks which pointer
   goes where. **The model sees pointer names, never values**, and the service
   substitutes the stored text itself. A model cannot invent your employment
   dates because it never handles them — anything that is not a real pointer
   resolves to nothing.
5. **AI generation** — only for questions nothing stored answers. This call does
   carry your resume and prior answers, because it has to write prose. The
   result is always flagged for you to read.

Each field comes back with an action:

| Action | Extension does |
|---|---|
| `fill` | writes it |
| `highlight_with_value` | shows the value on the field for you to enter — for pickers that refuse programmatic input |
| `review` | writes it, flagged: a model wrote this |
| `skip` | nothing known, or nothing that should be guessed |

Demographic and self-identification questions are always `skip`.

## Adding your resume

```bat
.venv\Scripts\python import_resume.py "C:\path\to\resume.txt"
```

Two steps. The text is **stored** — that alone matters, because every drafted
answer is grounded in it. Then, if a key is set, the model reads it into
structured records (jobs, degrees, skills), which is what fills "Employer",
"From" and "Job title". You see every value and what it would do before anything
is written; `--yes` skips the prompt, `--store-only` skips the extraction.

PDFs are not read here — convert first with `tools-resume-to-txt.py` in the
project root, which handles the ligature encoding that pypdf silently drops.

To also keep the original document, so the extension can attach it to upload
fields:

```bat
.venv\Scripts\python import_resume.py resume.txt --attach "resume.pdf"
```

The bytes are stored beside the markdown as a real file, not base64'd into it —
the bundle stays readable. The text and the file do different jobs and are kept
separately: the text grounds drafted answers, the file gets uploaded.

## How it learns

Submitting a form posts it to `/learn`. Answers become records, a Job
Application record is written, and the form's shape is remembered so the next
visit costs nothing.

When the extension asks you something in the page — "how many years with
Apigee?" — your answer goes to `/answer` and becomes an ordinary record. The
next site that asks it, however worded, is filled locally with no model call.

## Free models

At boot the service fetches OpenRouter's model list, keeps the zero-priced ones,
ranks them by context length and JSON support, health-checks the top of the list
concurrently, and builds a fallback ladder. A capacity error drops to the next
rung rather than retrying an exhausted worker.

Free models misbehave in specific ways, and the client handles each: JSON is
recovered by bracket-scanning rather than trusted, a reply that is prose or a
pipe table earns one stricter re-ask, and a model that fails is demoted.

`GET /models?refresh=true` re-runs discovery. `GET /health` shows the current
ladder.

## Optional: semantic retrieval

```bat
.venv\Scripts\pip install model2vec numpy
```

Static embeddings, numpy only — no torch, no ONNX. Without it, retrieval is
BM25 plus the routing model, which is where most of the semantic work happens
anyway; `GET /health` reports which is in use. OpenRouter serves chat
completions only, so embeddings have to be local.

## API

| Endpoint | |
|---|---|
| `POST /plan` | scanned fields in, fill plan out |
| `POST /learn` | a submitted form |
| `POST /answer` | one question you answered yourself |
| `GET /records`, `GET/PUT/DELETE /records/{path}` | browse and edit |
| `POST /ingest/resume`, `/ingest/extract`, `/ingest/apply` | resume to profile |
| `GET /health`, `GET /models`, `POST /reindex` | |
| `POST /resolve` | what a pointer resolves to |

Interactive docs at <http://127.0.0.1:8765/docs>.

## Tests

```bat
.venv\Scripts\python -m pytest -q
```

The model is faked throughout, so the suite asserts our behaviour — including
what happens when a model returns a forged pointer, when every free model is
down, and when a date widget refuses the write.
