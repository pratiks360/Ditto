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
- [Keeping the Knowledge Base in Git](#-keeping-the-knowledge-base-in-git)
- [Deploy on a VM](#-deploy-on-a-vm)
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

## 🗂️ Keeping the Knowledge Base in Git

`kb/` ships empty and stays untracked here — it holds your name, email, phone, employment history and resume, and this repository is public. Its history is genuinely worth keeping though: it is how you see what an answer used to say and get it back. So give it a repository of its own, a **private** one, nested inside `kb/`.

```bash
python service/kb_autocommit.py --init --remote https://github.com/you/your-kb.git
```

```bash
python service/kb_autocommit.py
```

> ⚠️ **That repository must be private.** The script warns you on `--init`, but nothing enforces it.

Empty commits are never made, the derived `.index/` is ignored, and the subject line says what moved — `2 answers, 1 profile` rather than `Update knowledge base` — so scrolling back for the day an answer changed actually works.

To run it unattended, on a timer rather than per-write (one commit per filled field would be unreadable):

```bash
python service/kb_autocommit.py --watch 86400
```

Under Podman it runs as the `kb-sync` container in `compose.yaml`, which does exactly that once a day. It needs `GIT_TOKEN` — a personal access token with `repo` scope — because a container cannot reach Windows Credential Manager. Without one it still commits locally and skips the push.

> 💡 **Two machines?** Different records rebase cleanly. The same record edited in both places between syncs stops with a conflict for you to resolve — running the sync in one place at a time avoids it entirely.

---

## ☁️ Deploy on a VM

The image is published to **`ghcr.io/pratiks360/ditto-service`**, built for `amd64` and `arm64` — the second matters because Oracle Cloud's free tier is Ampere, and an amd64-only image fails there with an exec format error that reads like a corrupt image rather than a wrong architecture.

> 🔒 **Open no ports.** This service speaks plain HTTP and answers questions about you. Leave the cloud firewall closed and reach it through an SSH tunnel — the extension keeps pointing at `127.0.0.1:8765` and needs no change.

### 1 · Install

```bash
sudo dnf install -y podman git
```

### 2 · Clone your knowledge base

```bash
git clone https://github.com/you/your-kb.git ~/ditto/kb
```

### 3 · Write `~/ditto/.env`

Linux paths here, unlike the Windows ones on your laptop:

```bash
OPENROUTER_API_KEY=sk-or-v1-your-key-here
JOBKB_MAX_PRICE=0
JOBKB_ROOT=/data
JOBKB_HOST=0.0.0.0
JOBKB_PORT=8765
JOBKB_TOKEN=a-long-random-string
GIT_TOKEN=a-pat-with-repo-scope
JOBKB_RESUME=/resume/resume.txt
JOBKB_RESUME_FILE=/resume/resume.pdf
```

```bash
chmod 600 ~/ditto/.env
```

> ⚠️ `JOBKB_HOST=0.0.0.0` is required *inside* a container — `127.0.0.1` there would only be reachable from within the container itself. The published port below is what actually keeps it off the network.

### 4 · Start the service and the daily sync

```bash
podman run -d --name jobkb --env-file ~/ditto/.env -p 127.0.0.1:8765:8765 -v ~/ditto/kb:/data:Z -v ~/ditto/kb/resume:/resume:ro --restart unless-stopped ghcr.io/pratiks360/ditto-service:latest
```

```bash
podman run -d --name jobkb-sync --env-file ~/ditto/.env -v ~/ditto/kb:/data:Z --restart unless-stopped ghcr.io/pratiks360/ditto-service:latest python kb_autocommit.py --kb /data --watch 86400
```

### 5 · Check it

```bash
curl -s http://127.0.0.1:8765/health
```

### 6 · Survive reboots

`--restart unless-stopped` only holds while the podman socket is alive, which is not the same as surviving a reboot:

```bash
podman generate systemd --new --files --name jobkb && mkdir -p ~/.config/systemd/user && mv container-jobkb.service ~/.config/systemd/user/ && systemctl --user enable --now container-jobkb && sudo loginctl enable-linger $USER
```

### 7 · Reach it from your laptop

```bash
ssh -N -L 8765:127.0.0.1:8765 opc@YOUR_VM_IP
```

Set the same `JOBKB_TOKEN` in the extension's **Options** page. Mismatch it and every call returns 401 while `/health` keeps reporting fine — worth knowing, because that failure looks like a broken service rather than a wrong password.

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
| **`JOBKB_TOKEN`** | Shared secret the extension must also send. Empty means no auth at all | _unset_ |
| **`JOBKB_MODEL`** | Pin one model instead of discovering free ones | _auto_ |
| **`JOBKB_TIMEOUT`** | Seconds per model call | `90` |
| **`JOBKB_HOST`** | Interface to bind. `0.0.0.0` inside a container, `127.0.0.1` otherwise | `127.0.0.1` |
| **`GIT_TOKEN`** | PAT with `repo` scope, so the sync can push your knowledge base | _unset_ |

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
| **Images** | `ghcr.io/pratiks360/ditto-service`, built for amd64 and arm64 by GitHub Actions |
| **Tests** | 144 pytest cases, plus browser harnesses per widget |

---

## 🔒 Security Notes

- **Local only.** The service binds `127.0.0.1`. Your employment history is never on a network interface
- **`kb/` ships empty.** It holds your name, email, phone, employment history and every answer you have given, so it is deliberately untracked. Keep your own copy in a **private** repo if you want its history
- **On a VM, open no ports.** `JOBKB_TOKEN` is a bearer secret over plain HTTP — anyone who sees one request has it permanently, and there is no rotation, rate limit or lockout. Behind an SSH tunnel it is a second lock rather than the only one
- **The published image carries code only** — no `.env`, no knowledge base, no key. Verified against the built layers rather than inferred from the Containerfile
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

**Q: On the VM, `podman pull` says the image was not found.**
A: Packages published to ghcr are private by default even when the repository is public. Open the package's settings on GitHub and change its visibility, or `podman login ghcr.io` with a token that can read it.

**Q: The container starts and immediately exits with an exec format error.**
A: Wrong architecture — an amd64 image on an Ampere VM. The published image is multi-arch; if you built it yourself, build for `linux/arm64` too.

**Q: The sync commits but never pushes.**
A: No `GIT_TOKEN`, or one without `repo` scope. It says so on the line after the commit. A container cannot use Windows Credential Manager, which is why the token is needed there and not on your laptop.

**Q: Every call returns 401 but `/health` looks fine.**
A: `JOBKB_TOKEN` on the service and in the extension's Options do not match. `/health` is deliberately exempt so a liveness check does not need the secret — which is exactly why this looks like a broken service rather than a wrong password.

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
