"use strict";

const DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct:free";

const DEFAULT_TIMEOUT_MS = 90000;

const keyInput = document.getElementById("apiKey");
const modelInput = document.getElementById("model");
const timeoutInput = document.getElementById("timeout");
const debugInput = document.getElementById("debug");
const autoCaptureInput = document.getElementById("autoCapture");
const statusEl = document.getElementById("status");
const logCountEl = document.getElementById("log-count");

const DEFAULT_SERVICE_URL = "http://127.0.0.1:8765";
const serviceEnabled = document.getElementById("serviceEnabled");
const serviceUrl = document.getElementById("serviceUrl");
const serviceToken = document.getElementById("serviceToken");
const serviceStatus = document.getElementById("service-status");

function say(text) {
  statusEl.textContent = text;
  setTimeout(() => { statusEl.textContent = ""; }, 2500);
}

async function refreshLogCount() {
  const log = await JAF.getDebugLog();
  logCountEl.textContent = log.length
    ? `${log.length} entr${log.length === 1 ? "y" : "ies"} stored`
    : "empty";
}

async function load() {
  const s = await JAF.getSettings();
  keyInput.value = s.openRouterKey || "";
  modelInput.value = s.model || DEFAULT_MODEL;
  timeoutInput.value = Math.round((Number(s.requestTimeoutMs) || DEFAULT_TIMEOUT_MS) / 1000);
  debugInput.checked = !!s.debug;
  autoCaptureInput.checked = s.autoCapture !== false;   // on unless switched off
  serviceEnabled.checked = s.serviceEnabled !== false;  // on unless switched off
  serviceUrl.value = s.serviceUrl || DEFAULT_SERVICE_URL;
  serviceToken.value = s.serviceToken || "";
  refreshLogCount();
  refreshResume();
  refreshServiceResume();
}

async function patchSettings(changes) {
  const current = await JAF.getSettings();
  await JAF.setSettings(Object.assign(current, changes));
}

document.getElementById("save").addEventListener("click", async () => {
  const seconds = Math.min(600, Math.max(15, Number(timeoutInput.value) || 90));
  timeoutInput.value = seconds;
  await patchSettings({
    openRouterKey: keyInput.value.trim(),
    model: modelInput.value.trim() || DEFAULT_MODEL,
    requestTimeoutMs: seconds * 1000
  });
  say("Saved.");
});

document.getElementById("clear").addEventListener("click", async () => {
  keyInput.value = "";
  await patchSettings({ openRouterKey: "" });
  say("Key cleared. Local classification still works.");
});

autoCaptureInput.addEventListener("change", async () => {
  await patchSettings({ autoCapture: autoCaptureInput.checked });
  say(autoCaptureInput.checked
    ? "Submitted forms will be queued for review."
    : "Capture off. Use Save answers in the popup instead.");
});

debugInput.addEventListener("change", async () => {
  await patchSettings({ debug: debugInput.checked });
  say(debugInput.checked
    ? "Debug capture on. Reload any open tabs to start recording."
    : "Debug capture off.");
});

document.getElementById("download-log").addEventListener("click", async () => {
  const log = await JAF.getDebugLog();
  if (!log.length) {
    return say("Log is empty — tick “Capture debug log” below, retry the action, then download.");
  }
  const text = JAF.formatDebugLog(log);
  const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  const filename = `job-application-learner-debug-${stamp}.txt`;

  // A blob: URL from this page dies with the page, and chrome.downloads
  // reports failure only through lastError — silence here read as "button
  // does nothing". A data: URL has no lifetime problem, and errors now surface.
  const url = "data:text/plain;charset=utf-8," + encodeURIComponent(text);
  chrome.downloads.download({ url, filename, saveAs: true }, (id) => {
    if (chrome.runtime.lastError) {
      return say(`Download failed: ${chrome.runtime.lastError.message}`);
    }
    if (id === undefined) return say("Download was cancelled.");
    say(`Saved ${filename} (${log.length} entries).`);
  });
});

document.getElementById("test-key").addEventListener("click", async () => {
  const testBtn = document.getElementById("test-key");
  const out = document.getElementById("test-result");
  const key = keyInput.value.trim();
  if (!key) {
    out.hidden = false;
    out.textContent = "No API key entered.";
    return;
  }
  // Test what is typed in the box, not a stale saved value.
  await patchSettings({ openRouterKey: key, model: modelInput.value.trim() || DEFAULT_MODEL });

  testBtn.disabled = true;
  testBtn.textContent = "Testing…";
  out.hidden = false;
  out.textContent = "Calling OpenRouter…";

  const res = await ask({ type: "JAF_AI_PING" }, 45000);

  testBtn.disabled = false;
  testBtn.textContent = "Test connection";
  out.textContent = res && res.ok
    ? `OK — ${res.model} replied in ${res.ms} ms.\nReply: ${res.reply}`
    : `FAILED — ${(res && res.reason) || "no response"}\n\n` +
      "401/403 = key rejected · 402 = out of credit · 429 = rate limited\n" +
      "404 = model id not found · timed out = model too slow, try another.";
});

// ---------------------------------------------------------------------- resume

// --------------------------------------------------------- knowledge service

document.getElementById("service-save").addEventListener("click", async () => {
  const url = serviceUrl.value.trim().replace(/\/+$/, "") || DEFAULT_SERVICE_URL;
  serviceUrl.value = url;
  await patchSettings({
    serviceEnabled: serviceEnabled.checked,
    serviceUrl: url,
    serviceToken: serviceToken.value.trim()
  });
  serviceStatus.textContent = "Saved.";
  setTimeout(() => { serviceStatus.textContent = ""; }, 2500);
});

document.getElementById("service-test").addEventListener("click", async () => {
  const btn = document.getElementById("service-test");
  const out = document.getElementById("service-result");

  // Test what is typed in, not what was last saved.
  await patchSettings({
    serviceEnabled: true,
    serviceUrl: serviceUrl.value.trim().replace(/\/+$/, "") || DEFAULT_SERVICE_URL,
    serviceToken: serviceToken.value.trim()
  });

  btn.disabled = true;
  serviceStatus.textContent = "Checking…";
  out.hidden = true;

  const res = await ask({ type: "JAF_SERVICE_HEALTH" }, 20000);
  btn.disabled = false;

  if (!res || !res.ok) {
    serviceStatus.style.color = "#b42318";
    serviceStatus.textContent = "Not reachable.";
    out.hidden = false;
    out.textContent = (res && res.error) || "no reply from the extension worker";
    return;
  }

  const d = res.data || {};
  serviceStatus.style.color = "#157347";
  serviceStatus.textContent = "Connected.";
  out.hidden = false;
  out.textContent = [
    `knowledge base : ${d.root}`,
    `records        : ${d.records}  (${d.pointers} fillable pointers)`,
    `retrieval      : ${d.semantic ? `semantic + lexical (${d.embedder})` : "lexical only"}`,
    `openrouter key : ${d.hasKey ? "set" : "NOT SET — no routing or drafting"}`,
    "free models    :",
    ...(d.models || []).map(
      (m) => `  ${m.healthy === false ? "x" : m.healthy ? "ok" : "? "} ${m.id}` +
             `${m.json_mode ? "  [json]" : ""}${m.last_error ? "  " + m.last_error : ""}`
    ),
    d.authError ? `KEY REJECTED   : ${d.authError}` : "",
    d.modelError ? `discovery      : ${d.modelError}` : ""
  ].filter(Boolean).join("\n");
});

// ------------------------------------------- resume held by the service

const resumeSendStatus = document.getElementById("resume-send-status");

function saySend(text, bad) {
  resumeSendStatus.style.color = bad ? "#b42318" : "#157347";
  resumeSendStatus.textContent = text;
}

/** Reads a File as base64 without the `data:...;base64,` prefix. */
function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`could not read ${file.name}`));
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
    reader.readAsDataURL(file);
  });
}

async function refreshServiceResume() {
  const box = document.getElementById("resume-service-state");
  box.textContent = "checking…";

  const res = await ask({ type: "JAF_SERVICE_RESUME_STATUS" }, 15000);
  if (!res || !res.ok) {
    box.textContent = `Service not reachable — ${(res && res.error) || "no reply"}`;
    return;
  }

  const d = res.data || {};
  const lines = [
    d.file.stored
      ? `document: ${d.file.filename} (${Math.round(d.file.bytes / 1024)} KB) — attached to upload fields`
      : "document: none — upload fields will be left for you",
    d.text.stored
      ? `text: ${d.text.chars.toLocaleString()} characters — grounds written answers`
      : "text: none — written answers will have nothing to draw on",
  ];
  // A path set in the environment wins at every restart, so say so rather than
  // letting an upload here look like it was ignored.
  if (d.envFile) lines.push(`JOBKB_RESUME_FILE=${d.envFile} (re-read at every start)`);
  if (d.envText) lines.push(`JOBKB_RESUME=${d.envText} (re-read at every start)`);
  box.textContent = lines.join("\n");
  box.style.whiteSpace = "pre-wrap";
}

document.getElementById("resume-refresh").addEventListener("click", refreshServiceResume);

document.getElementById("resume-send").addEventListener("click", async () => {
  const btn = document.getElementById("resume-send");
  const doc = document.getElementById("resume-doc").files[0];
  const txt = document.getElementById("resume-txt").files[0];

  if (!doc && !txt) {
    saySend("Choose a file first.", true);
    return;
  }

  btn.disabled = true;
  const done = [];
  try {
    if (doc) {
      saySend(`Sending ${doc.name}…`);
      const res = await ask({
        type: "JAF_SERVICE_RESUME_UPLOAD",
        payload: {
          filename: doc.name,
          mime: doc.type || "application/pdf",
          base64: await fileToBase64(doc)
        }
      }, 120000);
      if (!res || !res.ok) throw new Error((res && res.error) || "no reply");
      done.push(`${doc.name} (${Math.round(res.data.bytes / 1024)} KB)`);
    }

    if (txt) {
      saySend(`Sending ${txt.name}…`);
      const res = await ask({
        type: "JAF_SERVICE_RESUME_TEXT",
        payload: { text: await txt.text(), name: txt.name.replace(/\.[^.]+$/, "") }
      }, 60000);
      if (!res || !res.ok) throw new Error((res && res.error) || "no reply");
      done.push(`${txt.name} (${res.data.chars} characters)`);
    }

    saySend(`Stored ${done.join(" and ")}.`);
  } catch (e) {
    saySend(`Failed: ${e.message}`, true);
  } finally {
    btn.disabled = false;
    refreshServiceResume();
  }
});

const resumeStatus = document.getElementById("resume-status");
const resumePreview = document.getElementById("resume-preview");

function sayResume(text) {
  resumeStatus.textContent = text;
  setTimeout(() => { resumeStatus.textContent = ""; }, 4000);
}

async function refreshResume() {
  const store = await JAF.getStore();
  const resume = store.resume || {};
  if (!resume.text) {
    resumePreview.textContent = "No resume stored.";
    return;
  }
  const parsed = JAF.parseResume(resume.text);
  const found = [
    parsed.personal.firstName && `name: ${parsed.personal.firstName} ${parsed.personal.lastName}`.trim(),
    parsed.personal.email && `email: ${parsed.personal.email}`,
    parsed.personal.phone && `phone: ${parsed.personal.phone}`,
    parsed.personal.linkedin && `linkedin: ${parsed.personal.linkedin}`,
    `education: ${parsed.education.length}`,
    `experience: ${parsed.experience.length}`,
    `skills: ${parsed.skills.length}`
  ].filter(Boolean);
  resumePreview.textContent =
    `${resume.filename || "resume.txt"} — ${resume.text.length} chars, saved ${String(resume.updatedAt).slice(0, 10)}\n\n` +
    `Detected locally:\n  ${found.join("\n  ")}`;
}

document.getElementById("resume-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const text = await file.text();
  if (!text.trim()) return sayResume("That file is empty.");
  const store = await JAF.getStore();
  store.resume = { text, filename: file.name, updatedAt: new Date().toISOString() };
  await JAF.setStore(store);
  e.target.value = "";
  sayResume(`Saved ${file.name}.`);
  refreshResume();
});

document.getElementById("resume-import").addEventListener("click", async () => {
  const store = await JAF.getStore();
  if (!store.resume || !store.resume.text) return sayResume("Upload a resume first.");
  const filled = JAF.applyResumeToProfile(store, JAF.parseResume(store.resume.text));
  if (!filled.length) return sayResume("Nothing to add — those profile fields are already set.");
  await JAF.setStore(store);
  sayResume(`Filled empty profile fields: ${filled.join(", ")}.`);
});

// -------------------------------------------------- AI extraction (opt-in)

const extractReview = document.getElementById("extract-review");
const extractPreview = document.getElementById("extract-preview");
const extractBtn = document.getElementById("resume-extract");
let extracted = null;   // held until the user saves or discards

/**
 * sendMessage with a deadline. If the service worker is asleep, crashed, or
 * restarted mid-request the callback simply never fires — which is what left
 * the button stuck on "Extracting…". Always settle.
 */
function ask(message, timeoutMs) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (value) => { if (!done) { done = true; resolve(value); } };
    const timer = setTimeout(
      () => finish({ ok: false, reason: `no reply from the extension after ${Math.round((timeoutMs || 120000) / 1000)}s` }),
      timeoutMs || 120000
    );
    chrome.runtime.sendMessage(message, (res) => {
      clearTimeout(timer);
      if (chrome.runtime.lastError) {
        return finish({ ok: false, reason: chrome.runtime.lastError.message });
      }
      finish(res || { ok: false, reason: "empty reply from the extension" });
    });
  });
}

const ACTION_LABEL = { fill: "fill ", add: "add  ", keep: "keep ", duplicate: "dup  " };

/**
 * The mapping report: one line per extracted value, showing the profile field
 * it lands in and what will happen to it.
 */
function describeMapping(rows) {
  if (!rows.length) return "The model returned nothing usable.";
  const width = Math.max(...rows.map((r) => r.target.length));
  const counts = {};
  rows.forEach((r) => { counts[r.action] = (counts[r.action] || 0) + 1; });

  const body = rows.map((r) => {
    const target = r.target.padEnd(width);
    const note = r.action === "keep" ? `   (keeping "${r.current}")` : "";
    return `${ACTION_LABEL[r.action]} ${target}  ${r.value}${note}`;
  });

  const summary = ["fill", "add", "keep", "duplicate"]
    .filter((k) => counts[k])
    .map((k) => `${counts[k]} ${k}`)
    .join(", ");
  return `${rows.length} values mapped — ${summary}\n\n${body.join("\n")}`;
}

const progressEl = document.getElementById("extract-progress");

/**
 * Live progress. Extraction runs as several requests, and the service worker
 * reports each one, so this shows which step is in flight and keeps a running
 * list of the ones already finished.
 */
function startProgress(header) {
  const startedAt = Date.now();
  const done = [];
  let current = "Connecting…";
  progressEl.hidden = false;

  const render = () => {
    const s = Math.round((Date.now() - startedAt) / 1000);
    progressEl.textContent =
      [`${header} — ${s}s elapsed`]
        .concat(done, current ? `  … ${current}` : [])
        .join("\n");
  };

  const onProgress = (msg) => {
    if (!msg || msg.type !== "JAF_EXTRACT_PROGRESS") return;
    const step = `${msg.step}/${msg.total} ${msg.label}`;
    if (msg.state === "running") {
      current = step;
    } else if (msg.state === "retrying") {
      current = `${step} — provider busy, retry ${msg.attempt}/${msg.of} in ${Math.round(msg.waitMs / 1000)}s`;
    } else {
      const secs = msg.ms ? ` (${(msg.ms / 1000).toFixed(1)}s)` : "";
      done.push(msg.state === "done"
        ? `  ok   ${step}${secs}`
        : `  FAIL ${step}${secs} — ${msg.reason}`);
      current = "";
    }
    render();
  };

  chrome.runtime.onMessage.addListener(onProgress);
  render();
  const timer = setInterval(render, 1000);
  return () => {
    clearInterval(timer);
    chrome.runtime.onMessage.removeListener(onProgress);
    progressEl.hidden = true;
  };
}

extractBtn.addEventListener("click", async () => {
  const store = await JAF.getStore();
  if (!store.resume || !store.resume.text) return sayResume("Upload a resume first.");
  const settings = await JAF.getSettings();
  if (!settings.openRouterKey) return sayResume("Add an OpenRouter API key above first.");

  extractBtn.disabled = true;
  extractBtn.textContent = "Extracting…";
  extractReview.hidden = true;
  const stop = startProgress(
    `${store.resume.text.length} chars to ${settings.model || DEFAULT_MODEL}, in 3 passes`);

  // Must outlast every pass plus its retries, or the page gives up first.
  const perPass = Number(settings.requestTimeoutMs) || 90000;
  const res = await ask({ type: "JAF_AI_EXTRACT" }, perPass * 3 + 15000);

  stop();
  extractBtn.disabled = false;
  extractBtn.textContent = "Extract with AI";

  if (!res || !res.ok) {
    return sayResume(`Extraction failed — ${(res && res.reason) || "no response"}`);
  }
  extracted = res.profile;

  // Some passes may have failed while others succeeded; say so rather than
  // letting a partial result look complete.
  const failed = (res.passes || []).filter((p) => !p.ok);
  const plan = describeMapping(JAF.planExtractedProfile(store, extracted));
  extractPreview.textContent = failed.length
    ? `INCOMPLETE — ${failed.map((p) => `${p.label} (${p.reason})`).join("; ")}\n` +
      "Save what worked, then re-run to retry the rest.\n\n" + plan
    : plan;
  extractReview.hidden = false;
  sayResume(failed.length
    ? `${failed.length} of ${res.passes.length} passes failed — review below.`
    : "Review the mapping below, then save.");
});

document.getElementById("extract-apply").addEventListener("click", async () => {
  if (!extracted) return;
  const store = await JAF.getStore();
  const { filled, added, skipped } = JAF.mergeExtractedProfile(store, extracted);
  await JAF.setStore(store);

  const parts = [];
  if (filled.length) parts.push(`personal: ${filled.join(", ")}`);
  ["experience", "education", "skills", "custom"].forEach((k) => {
    if (added[k]) parts.push(`${added[k]} ${k}`);
  });
  if (skipped) parts.push(`${skipped} already stored`);

  extracted = null;
  extractReview.hidden = true;
  sayResume(parts.length ? `Saved — ${parts.join(", ")}.` : "Nothing new to add.");
  refreshResume();
});

document.getElementById("extract-discard").addEventListener("click", () => {
  extracted = null;
  extractReview.hidden = true;
  sayResume("Discarded — nothing saved.");
});

document.getElementById("resume-clear").addEventListener("click", async () => {
  const store = await JAF.getStore();
  store.resume = { text: "", filename: "", updatedAt: "" };
  await JAF.setStore(store);
  sayResume("Resume removed.");
  refreshResume();
});

document.getElementById("clear-log").addEventListener("click", async () => {
  await JAF.clearDebugLog();
  refreshLogCount();
  say("Log cleared.");
});

load();
