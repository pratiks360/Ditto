<p align="center">
  <h1 align="center">🧬 Ditto</h1>
  <p align="center">
    <strong>Answer a job application question once. Every form after that fills itself.</strong>
  </p>
  <p align="center">
    <img src="https://img.shields.io/badge/Chrome-Manifest_V3-4285f4?style=for-the-badge&logo=googlechrome&logoColor=white" alt="Chrome MV3">
    <img src="https://img.shields.io/badge/Python-3.13-3776ab?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.13">
    <img src="https://img.shields.io/badge/API-OpenRouter_(free_tier)-7c3aed?style=for-the-badge" alt="OpenRouter">
    <img src="https://img.shields.io/github/license/pratiks360/Ditto?style=for-the-badge" alt="License">
    <img src="https://img.shields.io/github/last-commit/pratiks360/Ditto?style=for-the-badge&label=Last+Commit" alt="Last Commit">
  </p>
</p>

---

## 📋 Table of Contents

- [What Is This?](#-what-is-this)
- [Features](#-features)
- [Screenshots](#-screenshots)
- [Quick Start](#-quick-start)
- [How It Works](#-how-it-works)
- [Why Pointers, Not Values](#-why-pointers-not-values)
- [Settings Reference](#️-settings-reference)
- [API Reference](#-api-reference)
- [Tech Stack](#️-tech-stack)
- [Security Notes](#-security-notes)
- [Troubleshooting](#-troubleshooting)
- [Contributing](#-contributing)
- [License](#-license)

---

## ✨ What Is This?

**Ditto** is a Chrome extension plus a local knowledge service that fills job application forms from a knowledge base which learns from every form you submit. Press `Ctrl+Shift+F` and it fills what it knows, highlights what it could not, and asks you — once — for anything genuinely new. The next site that asks the same question, however differently worded, already has your answer.

Everything runs on your machine. The service binds `127.0.0.1` only, your data lives in plain Markdown you can read and edit, and **it never submits a form** — that click stays yours.

> 🧠 **The core idea:** the model chooses *which stored record* answers a question. The service substitutes the text. The model never writes your facts, so it cannot invent them.

---

## 🎯 Features

| Feature | Description |
|---|---|
| ⌨️ **One-press fill** | `Ctrl+Shift+F` or the right-click menu. No confirmation step, no wizard |
| 🧠 **Learns from every submit** | Answers you type are captured and reused across any site, any wording |
| 🔀 **Cross-site reuse** | Fuzzy question matching means "Years with Kafka?" answers "How much Kafka experience do you have?" |
| 📋 **Custom widgets handled** | Native selects, React-controlled inputs, radio groups, and combobox popups that only commit on click |
| ☎️ **Phone + country split** | Strips punctuation, splits the dial code, and sets the country selector beside the digits |
| 📎 **Resume attachment** | Fills file-upload fields with your PDF via a constructed `File` object |
| 🖊️ **Drafts genuinely new answers** | Grounded in your resume, flagged for you to read before it goes anywhere |
| 🙈 **Refuses demographics** | Race, gender, disability, veteran and self-identification questions are never auto-answered |
| 📂 **Plain-Markdown storage** | [Open Knowledge Format](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing) records — greppable, diffable, yours |
| 💸 **Free models only** | Discovers OpenRouter's free tier at boot and walks a fallback ladder when one fails |

---

## 📸 Screenshots

> Add a GIF of a fill in progress here — the HUD counting fields and the panel asking for the one unknown answer is the whole product in five seconds.
> `![Filling a form](docs/screenshot.png)`

---

## 🚀 Quick Start

### 1 · Start the service

**Prerequisites:** [Podman](https://podman.io/) (or Python 3.13+ to run it directly)

```bash
git clone https://github.com/pratiks360/Ditto.git
cd Ditto/service
cp .env.example .env
```

Put your [OpenRouter key](https://openrouter.ai/keys) in `.env`, then:

```bash
podman.bat up
```

Health check — it should report your record count and `"semantic": true`:

```bash
curl http://127.0.0.1:8765/health
```

> 💡 **No Podman?** `start.bat` creates a venv and runs the service directly. Use Windows paths for the resume settings in that case — see `.env.example`.

### 2 · Load the extension

1. Open `chrome://extensions`
2. Turn on **Developer mode**
3. **Load unpacked** → select the `chrome-extension/` folder

### 3 · Add your resume

Either point `JOBKB_RESUME` / `JOBKB_RESUME_FILE` at your files in `.env`, or use the file picker in the extension's **Options** page — that uploads through the browser, so no mount and no path translation.

### 4 · Fill a form

Open any job application and press **`Ctrl+Shift+F`**.

---

## 🧩 How It Works

```
┌──────────────┐   fields    ┌──────────────┐  pointers  ┌──────────────┐
│              │ ──────────► │              │ ─────────► │              │
│  Extension   │             │   Service    │            │  OpenRouter  │
│ (content.js) │             │  (FastAPI)   │            │ (free models)│
│              │ ◄────────── │              │ ◄───────── │              │
└──────────────┘  decisions  └──────────────┘  which     └──────────────┘
       │                            │          record?
       │ writes values              │ reads / writes
       ▼                            ▼
┌──────────────┐             ┌──────────────┐
│   The form   │             │  kb/  (OKF   │
│  (never      │             │   Markdown)  │
│   submitted) │             │              │
└──────────────┘             └──────────────┘
```

1. **Scan** — the extension reads every visible field, its label, its options, and the block it sits in. Frames are probed and only the one holding the form runs
2. **Retrieve** — the service blends semantic similarity (model2vec embeddings) with BM25 lexical scoring to shortlist candidate records
3. **Route** — the model is handed a catalogue of *pointers* (`profile/personal.md#email`) and returns which one answers each field. It never sees or writes the values
4. **Choose & draft** — dropdowns are matched against their real options; only genuinely new prose is drafted, grounded in your resume, and flagged for review
5. **Fill** — values are written through native setters so React-controlled inputs keep them, then verified. Anything the widget refuses is shown to you with the value ready to paste
6. **Learn** — on submit, every answer is captured back into `kb/` as a reusable record

---

## 🔑 Why Pointers, Not Values

Most autofill tools ask a model to write the answer. Ditto asks it to *pick a record*:

```jsonc
// What the model returns
{ "id": "3", "pointer": "profile/personal.md#email" }

// What the service substitutes, from disk
{ "id": "3", "value": "you@example.com" }
```

An invalid pointer resolves to nothing and the field is asked about instead. A model having a bad day can pick the wrong record — it cannot invent a phone number that looks plausible. Only questions with no stored answer at all reach the drafting path, and those are highlighted for you to read.

---

## ⚙️ Settings Reference

All settings live in `service/.env` — copy `.env.example` to start.

| Setting | What It Does | Default |
|---|---|---|
| **`OPENROUTER_API_KEY`** | Enables routing and drafting. Without it, storage matching still works | — |
| **`JOBKB_MAX_PRICE`** | Max $/M tokens. `0` means free models only; `1.0` admits cheap paid fallbacks | `0` |
| **`JOBKB_RESUME_DIR`** | **Windows** folder holding your resume, mounted read-only at `/resume` | — |
| **`JOBKB_RESUME`** | Resume text (`.txt`/`.md`) — grounds drafted answers | — |
| **`JOBKB_RESUME_FILE`** | Resume document (`.pdf`/`.docx`) — attached to upload fields | — |
| **`JOBKB_PORT`** | Port to listen on | `8765` |
| **`JOBKB_TOKEN`** | Shared secret the extension must also send | _unset_ |
| **`JOBKB_MODEL`** | Pin one model instead of discovering free ones | _auto_ |
| **`JOBKB_TIMEOUT`** | Seconds per model call | `90` |

> ⚠️ **Running under Podman?** `JOBKB_RESUME` and `JOBKB_RESUME_FILE` are paths *inside* the container (`/resume/...`), while `JOBKB_RESUME_DIR` is a Windows path. A container cannot see `C:\`.

---

## 🔌 API Reference

The service listens on `127.0.0.1:8765`.

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | `GET` | Record count, embedder status, model ladder |
| `/plan` | `POST` | Given form fields, return a fill decision per field |
| `/learn` | `POST` | Capture a whole submitted form into the knowledge base |
| `/answer` | `POST` | Store one question→answer pair |
| `/records` | `GET` | List every stored record |
| `/resolve` | `POST` | Resolve a pointer to its stored text |
| `/reindex` | `POST` | Reload from disk and rebuild the indexes |
| `/ingest/resume` | `POST` | Parse a resume into profile, experience and education records |
| `/resume/status` | `GET` | What resume text and file are currently stored |

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| **Extension** | Chrome Manifest V3, vanilla JS, Shadow DOM UI |
| **Service** | Python 3.13, FastAPI, Uvicorn, Pydantic |
| **Retrieval** | model2vec (`potion-base-8M`) blended with BM25 |
| **Models** | OpenRouter free tier, auto-discovered with a fallback ladder |
| **Storage** | Open Knowledge Format — Markdown + YAML frontmatter |
| **Packaging** | Podman / podman-compose, or a plain venv |
| **Tests** | 144 pytest cases, plus browser harnesses per widget |

---

## 🔒 Security Notes

- **Local only.** The service binds `127.0.0.1`. Your employment history is never on a network interface
- **`kb/` ships empty.** It holds your name, email, phone, employment history and every answer you have given, so it is deliberately untracked. Keep your own copy in a **private** repo if you want its history
- **The key stays in `.env`**, which is gitignored. Only the format placeholder appears in this repository
- **Pointers, not values.** Only field labels and option text reach the model — never your stored answers, unless a question genuinely needs drafting
- **It never submits.** Submit controls are excluded from scanning by construction

---

## 🐛 Troubleshooting

**Q: The panel says "knowledge service unreachable".**
A: Check `curl http://127.0.0.1:8765/health`. If Podman is not running, `podman machine start` first — and on a managed Windows machine, WSL may need the *Log on as a service* right granted by IT.

**Q: A dropdown filled visually but the form still says it is empty.**
A: That is a custom combobox committing its own state. Ditto clicks the option rather than typing into it, and verifies the widget kept it. If you see this on a new site, open an issue with the field's HTML.

**Q: "The list came back empty" on a location or university field.**
A: That widget searches as you type over the network, and your stored wording found nothing. Type the site's own wording in the panel and press **Save & fill** — it is remembered from then on.

**Q: Nothing filled and it says "no fillable fields on this page".**
A: Embedded forms (Greenhouse, Lever, SmartRecruiters) live in an iframe. Ditto probes every frame and runs in the one holding the form — if you still see this, the form may have rendered after the scan, so press `Ctrl+Shift+F` again.

**Q: A fill stalled and the log mentions `free-models-per-min`.**
A: OpenRouter's free tier allows 20 requests/minute across your whole account. That is a rate, not a quota — credit raises the daily cap, not this ceiling. The service waits for the window to reset. Set `JOBKB_MAX_PRICE=1.0` to admit cheap paid fallbacks.

---

## 🤝 Contributing

This project is primarily for personal use, but PRs and issues are welcome. New sites break autofill in interesting ways — if a field is missed or filled wrongly, an issue with the field's HTML is the single most useful thing you can send.

Running the tests:

```bash
cd service
python -m pytest -q
```

Widget behaviour is covered by browser harnesses in `chrome-extension/` (`test-combobox.html`, `test-phone.html`, and friends) — open one and press the button. Each asserts the widget's *own* committed state, not the text on screen, because that distinction is where the real bugs hide.

---

## 📄 License

This project is open source under the [MIT License](LICENSE).

---

<p align="center">
  <sub>Built with ❤️ for everyone who has typed their notice period into a form for the fortieth time.</sub>
</p>
