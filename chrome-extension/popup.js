/**
 * Popup: scan a form, review what was typed, save it, browse the knowledge base.
 * The popup never writes to the page — it only asks the content script to read.
 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const el = (tag, props, children) => {
  const node = Object.assign(document.createElement(tag), props || {});
  (children || []).forEach((c) => node.append(c));
  return node;
};

/** <textarea> has no writable .type, so only <input> gets one. */
const textField = (long, value) =>
  long ? el("textarea", { value }) : el("input", { type: "text", value });

let scanState = null;   // { tabId, frameId, domain, formSignature, fields }
let reviewRows = [];    // [{ field, destination, value, sensitive }]

// ------------------------------------------------------------------ plumbing

function toast(text, ms) {
  const t = $("#toast");
  t.textContent = text;
  t.classList.remove("hidden");
  setTimeout(() => t.classList.add("hidden"), ms || 2200);
}

function showView(name) {
  ["scan", "review", "fill", "kb", "pending"].forEach((v) => {
    $(`#view-${v}`).classList.toggle("hidden", v !== name);
  });
  const tabFor = name === "kb" ? "kb" : "scan";
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.view === tabFor);
  });
}

function activeTab() {
  return new Promise((resolve) => {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => resolve(tabs[0]));
  });
}

function ask(msg) {
  return new Promise((resolve) => chrome.runtime.sendMessage(msg, resolve));
}

function askTab(tabId, frameId, msg) {
  return new Promise((resolve) => {
    const done = (res) => resolve(chrome.runtime.lastError ? null : res);
    if (typeof frameId === "number") chrome.tabs.sendMessage(tabId, msg, { frameId }, done);
    else chrome.tabs.sendMessage(tabId, msg, done);
  });
}

/**
 * Forms are often inside an iframe (Greenhouse, Lever). Frames announce
 * themselves to the service worker; pick whichever holds the most fields.
 */
/**
 * Every registered frame plus the top frame, most-fields-first. A form's
 * fields can be split across frames (an Easy-Apply-style modal that lives in
 * its own iframe while the search box behind it sits in the top frame), so
 * scanning is done per-frame and the results are merged — not "pick one".
 */
async function candidateFrames(tabId) {
  const res = await ask({ type: "JAF_GET_FRAMES", tabId });
  const frames = (res && res.frames) || {};
  const ranked = Object.keys(frames)
    .map((id) => ({ frameId: Number(id), count: frames[id].fieldCount || 0 }))
    .sort((a, b) => b.count - a.count)
    .map((f) => f.frameId);
  return ranked.includes(0) ? ranked : ranked.concat(0);
}

// ---------------------------------------------------------------------- scan

const STATUS_BADGE = {
  "known-question": ["known", "known answer"],
  "known-profile": ["known", "in profile"],
  "profile-candidate": ["new", "profile field"],
  "free-text": ["free", "form-specific"],
  new: ["new", "new"]
};

function renderScan() {
  const list = $("#scan-list");
  list.textContent = "";
  if (!scanState || !scanState.fields.length) {
    list.append(el("div", { className: "empty", textContent: "No form fields found on this page." }));
    return;
  }

  scanState.fields.forEach((f) => {
    const [cls, text] = STATUS_BADGE[f.status] || STATUS_BADGE.new;
    const row = el("div", { className: "row" });
    const label = el("div", { className: "label", textContent: f.label || "(unlabelled)" });
    label.append(el("span", { className: `badge ${cls}`, textContent: text }));
    if (f.sensitive) label.append(el("span", { className: "badge sensitive", textContent: "sensitive" }));
    row.append(label);
    const detail = [f.kind, f.options.length ? `${f.options.length} options` : null,
      f.matchScore ? `match ${f.matchScore}` : null].filter(Boolean).join(" · ");
    row.append(el("div", { className: "sub", textContent: detail }));
    list.append(row);
  });
}

async function doScan() {
  const tab = await activeTab();
  if (!tab) return;
  const frames = await candidateFrames(tab.id);

  const perFrame = [];
  for (const frameId of frames) {
    const res = await askTab(tab.id, frameId, { type: "JAF_SCAN" });
    if (res && res.fields && res.fields.length) perFrame.push({ frameId, res });
  }

  if (!perFrame.length) {
    $("#scan-meta").textContent =
      "Can't read this page. Reload it after installing the extension, or try a normal http(s) page.";
    $("#scan-list").textContent = "";
    $("#btn-save").disabled = true;
    $("#btn-fill").disabled = true;
    scanState = null;
    return;
  }

  const fields = [];
  perFrame.forEach(({ frameId, res }) => {
    res.fields.forEach((f) => fields.push(Object.assign({}, f, { frameId })));
  });

  scanState = {
    tabId: tab.id,
    domain: perFrame[0].res.domain,
    formSignature: JAF.formSignature(fields.map((f) => f.label)),
    fields
  };
  const known = fields.filter((f) => f.status.startsWith("known")).length;
  const frameNote = perFrame.length > 1 ? ` across ${perFrame.length} frames` : "";
  $("#scan-meta").textContent =
    `${fields.length} field(s) on ${scanState.domain}${frameNote} — ${known} already known, ${fields.length - known} new.`;
  $("#btn-save").disabled = false;
  $("#btn-fill").disabled = false;
  renderScan();
}

// -------------------------------------------------------------- save: review

const DESTINATIONS = [
  ["question", "Question bank (reusable answer)"],
  ["profile", "Profile field"],
  ["form", "Only this form (job-specific)"],
  ["skip", "Don't save"]
];

/** Spec's classification order 1–4; step 5 (AI) fills the gaps afterwards. */
function defaultDestination(field) {
  if (field.matchedQuestionId) return "question";
  if (field.hasFixedOptions && !field.profileKey) return "question";
  if (field.profileKey) return "profile";
  if (field.isFreeText) return "form";
  return null; // ambiguous — hand to AI, else fall back to "form"
}

async function resolveAmbiguous(rows) {
  const pending = rows.filter((r) => r.destination === null);
  if (!pending.length) return;

  const store = await JAF.getStore();
  const res = await ask({
    type: "JAF_AI_CLASSIFY",
    labels: pending.map((r) => r.field.label), // labels only — never values
    canonicalQuestions: store.questionBank.map((q) => q.canonicalQuestion)
  });

  const verdicts = new Map();
  if (res && res.ok) {
    (res.results || []).forEach((r) => verdicts.set(String(r.label || "").trim(), String(r.verdict || "")));
  }

  pending.forEach((row) => {
    const verdict = verdicts.get(row.field.label.trim()) || "";
    if (verdict.startsWith("question:")) {
      const target = verdict.slice("question:".length).trim();
      const match = store.questionBank.find((q) => q.canonicalQuestion === target);
      row.destination = "question";
      row.aiMatchedId = match ? match.id : null;
      row.aiNote = match ? `AI matched: ${target}` : "AI: reusable question";
    } else if (verdict === "profile") {
      row.destination = "profile";
      row.aiNote = "AI: profile data";
    } else if (verdict === "new_question") {
      row.destination = "question";
      row.aiNote = "AI: new reusable question";
    } else {
      row.destination = "form";
      row.aiNote = res && res.ok ? "AI: form-specific" : null;
    }
  });
}

function renderReview() {
  const list = $("#review-list");
  list.textContent = "";

  reviewRows.forEach((row, i) => {
    const f = row.field;
    const node = el("div", { className: "row" });
    node.append(el("div", { className: "label", textContent: f.label || "(unlabelled)" }));

    const notes = [f.kind, row.aiNote, f.matchedQuestionId ? `updates "${f.matchedQuestionId}"` : null]
      .filter(Boolean).join(" · ");
    if (notes) node.append(el("div", { className: "sub", textContent: notes }));

    const input = textField(row.value.length > 60 || f.isFreeText, row.value);
    input.addEventListener("input", () => { reviewRows[i].value = input.value; });
    node.append(input);

    const dest = el("select");
    DESTINATIONS.forEach(([value, text]) => {
      dest.append(el("option", { value, textContent: text, selected: value === row.destination }));
    });
    const sensWrap = el("div", { className: "inline" });
    const sens = el("input", { type: "checkbox", id: `sens-${i}`, checked: row.sensitive });
    sens.addEventListener("change", () => { reviewRows[i].sensitive = sens.checked; });
    sensWrap.append(sens, el("label", { htmlFor: `sens-${i}`, textContent: "Sensitive (EEO / self-ID)" }));
    sensWrap.classList.toggle("hidden", row.destination !== "question");

    dest.addEventListener("change", () => {
      reviewRows[i].destination = dest.value;
      sensWrap.classList.toggle("hidden", dest.value !== "question");
    });

    node.append(el("div", { className: "inline" }, [dest]));
    node.append(sensWrap);
    list.append(node);
  });
}

async function doSave() {
  if (!scanState) return;

  // Fields can span frames (see doScan) — capture separately per frame and
  // join back by "frameId:index", since each frame numbers its own fields
  // from 0.
  const frameIds = Array.from(new Set(scanState.fields.map((f) => f.frameId)));
  const capturedByKey = new Map();
  for (const frameId of frameIds) {
    const res = await askTab(scanState.tabId, frameId, { type: "JAF_CAPTURE" });
    if (!res) continue;
    res.fields.forEach((f) => capturedByKey.set(`${frameId}:${f.index}`, f));
  }
  if (!capturedByKey.size) return toast("Couldn't read the form — reload the page and rescan.");

  reviewRows = scanState.fields
    .map((scanned) => {
      const captured = capturedByKey.get(`${scanned.frameId}:${scanned.index}`);
      return { scanned, value: captured ? String(captured.value || "") : "" };
    })
    .filter(({ value }) => value.trim() !== "")
    .map(({ scanned, value }) => {
      const field = Object.assign({}, scanned, { value });
      return {
        field,
        value,
        destination: defaultDestination(field),
        sensitive: !!field.sensitive
      };
    });

  if (!reviewRows.length) {
    return toast("Nothing filled in yet — fill the form first, then save.");
  }

  $("#review-meta").textContent = "Resolving…";
  showView("review");
  renderReview();
  await resolveAmbiguous(reviewRows);
  $("#review-meta").textContent =
    `${reviewRows.length} filled field(s) on ${scanState.domain}. Edit anything before saving.`;
  renderReview();
}

// ------------------------------------------------------- save: write to store

const TITLE_CASE_KEYS = JAF.TITLE_CASE_KEYS;

function slugify(text) {
  return String(text).toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 48)
    || `question_${Date.now()}`;
}

function uniqueId(base, questionBank) {
  let id = base;
  let n = 2;
  while (questionBank.some((q) => q.id === id)) id = `${base}_${n++}`;
  return id;
}

function normalizeValue(field, rawValue) {
  return TITLE_CASE_KEYS.has(field) ? JAF.toTitleCase(rawValue) : rawValue.trim();
}

function writeProfile(store, key, rawValue) {
  const [section, field] = key.split(".");
  const value = normalizeValue(field, rawValue);

  if (section !== "personal") return; // arrays are handled by writeEntries
  if (field === "fullName") {
    const parts = value.split(/\s+/);
    store.profile.personal.firstName = parts[0] || "";
    store.profile.personal.lastName = parts.slice(1).join(" ");
    return;
  }
  store.profile.personal[field] = value;
}

/** What identifies an education / experience entry as "the same one". */
const ENTRY_IDENTITY = { education: "institution", experience: "company" };

/**
 * Each repeating block on the form becomes one array entry. An entry that names
 * an institution/company already on file updates it; anything else is appended,
 * so a second degree doesn't overwrite the first.
 */
function writeEntries(store, entries) {
  let written = 0;
  entries.forEach((entry) => {
    const list = store.profile[entry.section];
    const idKey = ENTRY_IDENTITY[entry.section];
    const identity = String(entry.data[idKey] || "").toLowerCase();
    const existing = identity
      ? list.find((e) => String(e[idKey] || "").toLowerCase() === identity)
      : null;
    if (existing) Object.assign(existing, entry.data);
    else list.push(entry.data);
    written += Object.keys(entry.data).length;
  });
  return written;
}

async function applySave() {
  if (!scanState) return toast("Scan the form again before saving.");
  const store = await JAF.getStore();
  const now = new Date().toISOString();
  const formFields = [];
  const entries = [];
  let counts = { question: 0, profile: 0, form: 0 };

  reviewRows.forEach((row) => {
    const f = row.field;
    const value = String(row.value).trim();
    if (!value || row.destination === "skip") return;

    if (row.destination === "question") {
      // Re-match against the store as it is *now*: the scan-time verdict can be
      // stale, and two fields in one save can target the same question.
      const targetId = f.matchedQuestionId || row.aiMatchedId;
      const fresh = JAF.findQuestionMatch(f.label, store.questionBank);
      const existing =
        (targetId && store.questionBank.find((q) => q.id === targetId)) ||
        (fresh && fresh.entry);
      if (existing) {
        if (f.label && existing.canonicalQuestion !== f.label &&
            !(existing.aliases || []).includes(f.label)) {
          existing.aliases = (existing.aliases || []).concat(f.label);
        }
        existing.answer = value;
        existing.sensitive = row.sensitive;
        existing.lastUpdated = now;
      } else {
        store.questionBank.push({
          id: uniqueId(slugify(f.label), store.questionBank),
          canonicalQuestion: f.label,
          aliases: [],
          answer: value,
          sensitive: row.sensitive,
          lastUpdated: now
        });
      }
      counts.question++;
      return;
    }

    if (row.destination === "profile" && f.profileKey) {
      const [section, key] = f.profileKey.split(".");
      if (section === "personal") {
        writeProfile(store, f.profileKey, value);
        counts.profile++;
      } else {
        // Buffer until every row is seen, so one block becomes one entry.
        const sectionKey = f.sectionKey || `${section}#0`;
        let entry = entries.find((e) => e.sectionKey === sectionKey);
        if (!entry) {
          entry = { sectionKey, section, data: {} };
          entries.push(entry);
        }
        entry.data[key] = normalizeValue(key, value);
      }
      return;
    }

    if (row.destination === "profile") {
      store.profile.custom[f.label] = value;
      counts.profile++;
      return;
    }

    formFields.push({
      label: f.label,
      selector: f.selector,
      type: f.kind,
      value,
      isFreeText: !!f.isFreeText
    });
    counts.form++;
  });

  counts.profile += writeEntries(store, entries);

  if (formFields.length) {
    const existing = store.formMappings.find((m) => m.formSignature === scanState.formSignature);
    if (existing) {
      const bySelector = new Map(existing.fields.map((x) => [x.selector, x]));
      formFields.forEach((x) => bySelector.set(x.selector, x));
      existing.fields = Array.from(bySelector.values());
      existing.lastUsed = now;
    } else {
      store.formMappings.push({
        formSignature: scanState.formSignature,
        domain: scanState.domain,
        fields: formFields,
        lastUsed: now
      });
    }
  }

  JAF.pruneFormMappings(store);
  await JAF.setStore(store);
  JAF.debugLog("popup", "save", {
    domain: scanState.domain,
    formSignature: scanState.formSignature,
    counts,
    entries: entries.map((e) => ({ sectionKey: e.sectionKey, keys: Object.keys(e.data) })),
    routed: reviewRows.map((r) => ({ label: r.field.label, to: r.destination }))
  });
  toast(`Saved — ${counts.question} question(s), ${counts.profile} profile, ${counts.form} form-specific.`);
  showView("scan");
  await doScan();
}

// ---------------------------------------------------------------------- fill

let fillRows = []; // [{ field, value, source, include, alreadyFilled }]

/** The repeated block a field belongs to: "experience#1" -> 1. */
function blockIndex(field) {
  return Number(String(field.sectionKey || "").split("#")[1] || 0);
}

/** Where a proposed value came from, in priority order. */
function proposeValue(field, store, mapping) {
  // "I currently work here" is derived, not stored: it's true when the job in
  // this block has no end date. Checked before the question bank so a stale
  // saved answer can't tick it for a role that has since ended.
  if ((field.kind === "checkbox" || field.kind === "radio-lone") &&
      JAF.CURRENT_ROLE_LABEL.test(field.label)) {
    const idx = blockIndex(field);
    const entry = (store.profile.experience || [])[idx];
    if (entry) {
      return {
        value: JAF.isOngoingRole(entry) ? "Yes" : "No",
        source: `experience #${idx + 1}: ${JAF.isOngoingRole(entry) ? "still current" : "ended"}`
      };
    }
  }

  const match = JAF.findQuestionMatch(field.label, store.questionBank);
  if (match) return { value: match.entry.answer, source: `question: ${match.entry.canonicalQuestion}` };

  if (field.profileKey) {
    const [section, key] = field.profileKey.split(".");
    if (section === "personal") {
      const v = store.profile.personal[key];
      if (v) return { value: v, source: `profile: ${key}` };
    } else {
      // sectionKey carries which repeated block this field belonged to.
      const idx = blockIndex(field);
      const entry = (store.profile[section] || [])[idx];
      if (entry && entry[key]) return { value: entry[key], source: `${section} #${idx + 1}: ${key}` };
    }
  }

  // Facts extracted from the resume that aren't part of the fixed schema —
  // notice period, work authorisation, years of experience.
  const custom = JAF.findCustomMatch(field.label, store.profile.custom);
  if (custom) return { value: custom.value, source: `profile: ${custom.key}` };

  if (mapping) {
    const hit = mapping.fields.find((f) => f.label === field.label);
    if (hit && hit.value) return { value: hit.value, source: "this form's saved answer" };
  }
  return null;
}

/**
 * Asks the model which stored value belongs in each field, then resolves every
 * pointer it returns to the actual stored text. The model never supplies the
 * value, so a routing mistake shows up as a wrong-but-real answer you can spot,
 * never as a plausible invention.
 */
async function routeFields(store) {
  const out = new Map();
  const catalogueSize = JAF.buildSourceCatalog(store).length;
  if (!catalogueSize) return out;

  const res = await ask({
    type: "JAF_AI_ROUTE",
    fields: scanState.fields.map((f) => ({
      // Labels and structure only — no stored answers leave here.
      index: f.index,
      frameId: f.frameId,
      label: f.label,
      kind: f.kind,
      block: f.sectionKey || "",
      options: (f.options || []).map((o) => o.label)
    }))
  });

  if (!res || !res.ok || !Array.isArray(res.results)) {
    JAF.debugLog("popup", "fill-route-unavailable", {
      reason: (res && res.reason) || (res && res.ok ? "reply had no results" : "no response")
    });
    return out;
  }

  const byIndex = new Map(scanState.fields.map((f) => [f.index, f]));
  const unresolved = [];
  res.results.forEach((r) => {
    const field = byIndex.get(r.index);
    if (!field) return;
    const value = JAF.resolveSource(store, r.source);
    if (!value) {
      // "generate"/"none", or a pointer that no longer exists. Left for the
      // local chain and then the drafting pass.
      if (r.source !== "none") unresolved.push({ index: r.index, source: r.source });
      return;
    }
    out.set(`${field.frameId}:${field.index}`, {
      value,
      source: `AI matched: ${JAF.describeSource(store, r.source)}`
    });
  });

  JAF.debugLog("popup", "fill-routed", { resolved: out.size, unresolved });
  return out;
}

async function doFillPreview() {
  if (!scanState) return;
  const store = await JAF.getStore();
  const mapping = store.formMappings.find((m) => m.formSignature === scanState.formSignature);

  // Current values decide what's already filled, so we don't clobber typing.
  const current = new Map();
  const frameIds = Array.from(new Set(scanState.fields.map((f) => f.frameId)));
  for (const frameId of frameIds) {
    const res = await askTab(scanState.tabId, frameId, { type: "JAF_CAPTURE" });
    if (res) res.fields.forEach((f) => current.set(`${frameId}:${f.index}`, String(f.value || "")));
  }

  // The model maps the whole form to stored values in one call; the local
  // chain below still runs for anything it declined or couldn't be asked about.
  const routed = await routeFields(store);

  fillRows = [];
  const skipped = [];
  scanState.fields.forEach((field) => {
    const proposal = routed.get(`${field.frameId}:${field.index}`) ||
      proposeValue(field, store, mapping);
    if (!proposal) {
      skipped.push({
        label: field.label, kind: field.kind,
        profileKey: field.profileKey || null, sectionKey: field.sectionKey || null,
        why: "nothing stored matches this field"
      });
      return;
    }

    // A date control takes only its own encoding, so re-encode before offering
    // it. An empty result means the value can't go here at all — an ongoing
    // role has no end date to put in a "To" box — so don't propose anything.
    const value = JAF.formatDateForField(proposal.value, field);
    if (!String(value).trim()) {
      skipped.push({
        label: field.label, kind: field.kind, inputType: field.inputType || null,
        placeholder: field.placeholder || null, hint: field.hint || null,
        mask: JAF.detectDateMask(field),
        // The stored value's shape, not the value itself.
        storedShape: JAF.parseDateParts(proposal.value) ? "date" : "not-a-date",
        why: "value cannot be encoded for this control"
      });
      return;
    }

    const existing = (current.get(`${field.frameId}:${field.index}`) || "").trim();
    fillRows.push({
      field,
      value,
      source: JAF.isDateField(field) && value !== proposal.value
        ? `${proposal.source} (as ${value})`
        : proposal.source,
      alreadyFilled: existing !== "",
      // Don't overwrite what's there, and make sensitive answers a deliberate
      // opt-in rather than something that slips through on a fast confirm.
      include: existing === "" && !field.sensitive
    });
  });

  JAF.debugLog("popup", "fill-preview", {
    fields: scanState.fields.length,
    proposed: fillRows.map((r) => ({
      label: r.field.label,
      kind: r.field.kind,
      inputType: r.field.inputType || null,
      isDate: JAF.isDateField(r.field),
      mask: JAF.detectDateMask(r.field),
      source: r.source,
      // length only — proposed answers stay out of the log
      valueChars: String(r.value).length
    })),
    skipped
  });

  // Show what's known immediately; AI drafting runs after and appends.
  $("#fill-meta").textContent = "Matching…";
  renderFill();
  showView("fill");

  await draftUnmatched(store, current);

  if (!fillRows.length) {
    showView("scan");
    return toast("Nothing stored yet that matches this form.");
  }

  const ready = fillRows.filter((r) => r.include).length;
  const drafts = fillRows.filter((r) => r.aiDraft).length;
  $("#fill-meta").textContent =
    `${fillRows.length} field(s) can be filled on ${scanState.domain} — ${ready} checked` +
    (drafts ? `, ${drafts} AI draft(s)` : "") +
    `. Already-filled, sensitive and AI-drafted fields are unchecked by default.`;
  renderFill();
}

/**
 * Questions with no stored match go to the model, which sees the resume and
 * the knowledge base. Drafts come back unchecked and badged — never written
 * without an explicit tick.
 */
async function draftUnmatched(store, current) {
  const matched = new Set(fillRows.map((r) => r.field.index + ":" + r.field.frameId));
  const candidates = scanState.fields.filter((f) => {
    if (matched.has(f.index + ":" + f.frameId)) return false;
    if ((current.get(`${f.frameId}:${f.index}`) || "").trim()) return false; // already answered
    // A model asked for a date will write prose into a date box, and it has no
    // way to know a date the resume never stated.
    if (JAF.isDateField(f)) return false;
    if (f.kind === "file" || f.kind === "date-parts") return false;
    // Anything else with a real question on it: long answers, option lists, and
    // ordinary short-text questions the profile happens not to cover.
    return Boolean(f.label) && (f.isFreeText || f.hasFixedOptions || f.kind === "text" ||
      f.kind === "textarea" || f.kind === "email" || f.kind === "tel" || f.kind === "url");
  });
  if (!candidates.length) {
    JAF.debugLog("popup", "ai-fill-skipped", {
      reason: "every field already matched or excluded",
      unmatched: scanState.fields.length - fillRows.length
    });
    return;
  }

  JAF.debugLog("popup", "ai-fill-asking", { labels: candidates.map((f) => f.label) });

  const res = await ask({
    type: "JAF_AI_ANSWER",
    questions: candidates.map((f) => ({
      label: f.label,
      kind: f.kind,
      options: f.options.map((o) => o.label)
    }))
  });
  if (!res || !res.ok) {
    JAF.debugLog("popup", "ai-fill-failed", { reason: (res && res.reason) || "no response" });
    return;
  }

  JAF.debugLog("popup", "ai-fill-answered", {
    asked: candidates.length,
    // labels and lengths only — drafted text stays out of the log
    answered: res.results.map((r) => ({ label: r.label, length: String(r.answer || "").length }))
  });

  const byLabel = new Map(res.results.map((r) => [String(r.label).trim(), String(r.answer)]));
  candidates.forEach((field) => {
    const answer = byLabel.get(String(field.label).trim());
    if (!answer || !answer.trim()) return;
    fillRows.push({
      field,
      value: answer,
      source: "AI draft — from your resume and past answers",
      alreadyFilled: false,
      aiDraft: true,
      include: false
    });
  });
}

function renderFill() {
  const list = $("#fill-list");
  list.textContent = "";

  fillRows.forEach((row, i) => {
    const node = el("div", { className: "row" });
    const head = el("div", { className: "label", textContent: row.field.label || "(unlabelled)" });
    if (row.field.sensitive) {
      head.append(el("span", { className: "badge sensitive", textContent: "sensitive" }));
    }
    if (row.alreadyFilled) {
      head.append(el("span", { className: "badge known", textContent: "already filled" }));
    }
    if (row.aiDraft) {
      head.append(el("span", { className: "badge free", textContent: "AI draft" }));
    }
    node.append(head);
    node.append(el("div", { className: "sub", textContent: row.source }));

    const input = textField(row.value.length > 60, row.value);
    input.addEventListener("input", () => { fillRows[i].value = input.value; });
    node.append(input);

    const wrap = el("div", { className: "inline" });
    const box = el("input", { type: "checkbox", id: `fill-${i}`, checked: row.include });
    box.addEventListener("change", () => { fillRows[i].include = box.checked; });
    wrap.append(box, el("label", { htmlFor: `fill-${i}`, textContent: "Fill this field" }));
    node.append(wrap);
    list.append(node);
  });
}

async function applyFill() {
  const chosen = fillRows.filter((r) => r.include && String(r.value).trim() !== "");
  if (!chosen.length) return toast("Nothing checked.");

  const byFrame = new Map();
  chosen.forEach((row) => {
    const list = byFrame.get(row.field.frameId) || [];
    list.push({ index: row.field.index, value: row.value });
    byFrame.set(row.field.frameId, list);
  });

  let filled = 0;
  const failures = [];
  for (const [frameId, items] of byFrame) {
    const res = await askTab(scanState.tabId, frameId, { type: "JAF_FILL", items });
    if (!res) {
      failures.push("frame unreachable");
      continue;
    }
    res.results.forEach((r) => {
      if (r.ok) filled++;
      else failures.push(`${r.label}: ${r.reason}`);
    });
  }

  const marked = await markFieldsNeedingYou(chosen);

  JAF.debugLog("popup", "fill", {
    domain: scanState.domain,
    requested: chosen.length,
    filled,
    highlighted: marked,
    failures
  });

  const note = marked ? ` ${marked} field(s) highlighted for your own words.` : "";
  toast(failures.length
    ? `Filled ${filled}. Skipped ${failures.length} — see debug log.${note}`
    : `Filled ${filled} field(s).${note} Review the page, then submit it yourself.`, 3600);
  showView("scan");
}

/**
 * Outlines the fields the user still has to own: an AI-drafted answer, and any
 * open question about *this* employer that nothing stored could answer. These
 * are the answers a reader can tell were not written by you.
 */
async function markFieldsNeedingYou(chosen) {
  const written = new Set(chosen.map((r) => `${r.field.frameId}:${r.field.index}`));
  const drafted = new Set(
    chosen.filter((r) => r.aiDraft).map((r) => `${r.field.frameId}:${r.field.index}`)
  );

  const marksByFrame = new Map();
  const add = (field, tone) => {
    const list = marksByFrame.get(field.frameId) || [];
    list.push({ index: field.index, tone });
    marksByFrame.set(field.frameId, list);
  };

  scanState.fields.forEach((field) => {
    const key = `${field.frameId}:${field.index}`;
    if (drafted.has(key)) return add(field, "review");        // filled, but by the model
    if (written.has(key)) return;                              // filled from your own data
    // Left empty, and it's the kind of question only you can answer.
    if (JAF.isOpinionQuestion(field.label) || field.isFreeText) add(field, "empty");
  });

  let marked = 0;
  for (const [frameId, marks] of marksByFrame) {
    const res = await askTab(scanState.tabId, frameId, { type: "JAF_HIGHLIGHT", marks });
    if (res && res.ok) marked += res.marked;
  }
  return marked;
}

// ------------------------------------------------------ captured on submit

let pendingRows = [];      // [{ item, keep }]
let pendingEntry = null;   // the queued capture being reviewed

/** Shows the badge/button when submitted forms are waiting to be reviewed. */
async function refreshPendingButton() {
  const store = await JAF.getStore();
  const n = (store.pending || []).length;
  const btn = $("#btn-pending");
  btn.classList.toggle("hidden", n === 0);
  btn.textContent = n === 1
    ? "Review 1 captured form"
    : `Review ${n} captured forms`;
}

async function openPending() {
  const store = await JAF.getStore();
  pendingEntry = (store.pending || [])[0];
  if (!pendingEntry) {
    await refreshPendingButton();
    return toast("Nothing waiting.");
  }

  // Anything already known is unticked by default: re-saving what you have
  // wastes a click and risks overwriting a corrected answer with an older one.
  pendingRows = pendingEntry.items.map((item) => {
    const known = JAF.findQuestionMatch(item.label, store.questionBank);
    return {
      item,
      known: !!known,
      keep: !known && !item.sensitive
    };
  });

  const when = String(pendingEntry.ts).replace("T", " ").slice(0, 16);
  $("#pending-meta").textContent =
    `${pendingEntry.items.length} answer(s) from ${pendingEntry.domain} on ${when}. ` +
    `${(store.pending || []).length - 1} more form(s) queued.`;

  renderPending();
  showView("pending");
}

function renderPending() {
  const list = $("#pending-list");
  list.textContent = "";
  pendingRows.forEach((row, i) => {
    const node = el("div", { className: "row" });
    const head = el("div", { className: "label", textContent: row.item.label || "(unlabelled)" });
    if (row.item.sensitive) {
      head.append(el("span", { className: "badge sensitive", textContent: "sensitive" }));
    }
    if (row.known) {
      head.append(el("span", { className: "badge known", textContent: "already known" }));
    }
    node.append(head);

    const input = textField(String(row.item.value).length > 60, String(row.item.value));
    input.addEventListener("input", () => { pendingRows[i].item.value = input.value; });
    node.append(input);

    const wrap = el("div", { className: "inline" });
    const box = el("input", { type: "checkbox", id: `pend-${i}`, checked: row.keep });
    box.addEventListener("change", () => { pendingRows[i].keep = box.checked; });
    wrap.append(box, el("label", { htmlFor: `pend-${i}`, textContent: "Keep this answer" }));
    node.append(wrap);
    list.append(node);
  });
}

/** Drops the reviewed capture from the queue, whatever the outcome. */
async function dropPendingEntry() {
  const store = await JAF.getStore();
  store.pending = (store.pending || []).filter((p) => p.id !== pendingEntry.id);
  await JAF.setStore(store);
  chrome.action.setBadgeText({ text: store.pending.length ? String(store.pending.length) : "" });
  pendingEntry = null;
  pendingRows = [];
  await refreshPendingButton();
}

async function savePending() {
  const keep = pendingRows.filter((r) => r.keep && String(r.item.value).trim());
  if (!keep.length) {
    await dropPendingEntry();
    showView("scan");
    return toast("Nothing kept — capture discarded.");
  }

  const store = await JAF.getStore();
  const now = new Date().toISOString();
  let questions = 0;
  let profile = 0;

  keep.forEach(({ item }) => {
    const value = String(item.value).trim();
    if (item.profileKey) {
      // Profile data is written straight in; the review above was the gate.
      const [section, key] = item.profileKey.split(".");
      if (section === "personal") {
        store.profile.personal[key] = normalizeValue(key, value);
        profile++;
      }
      return;
    }
    const existing = JAF.findQuestionMatch(item.label, store.questionBank);
    if (existing) {
      if (item.label !== existing.entry.canonicalQuestion &&
          !(existing.entry.aliases || []).includes(item.label)) {
        existing.entry.aliases = (existing.entry.aliases || []).concat(item.label);
      }
      existing.entry.answer = value;
      existing.entry.lastUpdated = now;
    } else {
      store.questionBank.push({
        id: uniqueId(slugify(item.label), store.questionBank),
        canonicalQuestion: item.label,
        aliases: [],
        answer: value,
        sensitive: !!item.sensitive,
        lastUpdated: now
      });
    }
    questions++;
  });

  await JAF.setStore(store);
  JAF.debugLog("popup", "pending-saved", {
    domain: pendingEntry.domain, questions, profile, discarded: pendingRows.length - keep.length
  });
  await dropPendingEntry();

  toast(`Saved ${questions} answer(s), ${profile} profile field(s).`);
  const store2 = await JAF.getStore();
  if ((store2.pending || []).length) return openPending();
  showView("scan");
}

// ------------------------------------------------------------ knowledge base

function editableRow(label, value, onSave, opts) {
  const row = el("div", { className: "row" });
  const head = el("div", { className: "label", textContent: label });
  if (opts && opts.sensitive) {
    head.append(el("span", { className: "badge sensitive", textContent: "sensitive" }));
  }
  row.append(head);

  const view = el("div", { className: "sub", textContent: value || "—" });
  const editBtn = el("button", { className: "link", textContent: "Edit" });
  const controls = el("div", { className: "inline" }, [editBtn]);
  if (opts && opts.onDelete) {
    const del = el("button", { className: "link", textContent: "Delete" });
    del.addEventListener("click", opts.onDelete);
    controls.append(del);
  }
  if (opts && opts.subtitle) row.append(el("div", { className: "sub", textContent: opts.subtitle }));
  row.append(view, controls);

  editBtn.addEventListener("click", () => {
    const input = textField(String(value).length > 60, value || "");
    const ok = el("button", { className: "link", textContent: "Save" });
    ok.addEventListener("click", () => onSave(input.value));
    view.replaceWith(input);
    controls.replaceWith(el("div", { className: "inline" }, [ok]));
  });
  return row;
}

async function renderKB() {
  const store = await JAF.getStore();

  const profileBox = $("#kb-profile");
  profileBox.textContent = "";
  Object.keys(store.profile.personal).forEach((key) => {
    profileBox.append(editableRow(key, store.profile.personal[key], async (val) => {
      const s = await JAF.getStore();
      s.profile.personal[key] = val;
      await JAF.setStore(s);
      renderKB();
    }));
  });
  Object.keys(store.profile.custom).forEach((key) => {
    profileBox.append(editableRow(key, store.profile.custom[key], async (val) => {
      const s = await JAF.getStore();
      s.profile.custom[key] = val;
      await JAF.setStore(s);
      renderKB();
    }, {
      onDelete: async () => {
        const s = await JAF.getStore();
        delete s.profile.custom[key];
        await JAF.setStore(s);
        renderKB();
      }
    }));
  });
  ["education", "experience"].forEach((section) => {
    store.profile[section].forEach((entry, i) => {
      const summary = Object.keys(entry).map((k) => `${k}: ${entry[k]}`).join(" · ");
      profileBox.append(editableRow(`${section} #${i + 1}`, summary, () => {}, {
        onDelete: async () => {
          const s = await JAF.getStore();
          s.profile[section].splice(i, 1);
          await JAF.setStore(s);
          renderKB();
        }
      }));
    });
  });

  const qBox = $("#kb-questions");
  qBox.textContent = "";
  $("#kb-q-count").textContent = `(${store.questionBank.length})`;
  if (!store.questionBank.length) {
    qBox.append(el("div", { className: "empty", textContent: "Nothing learned yet." }));
  }
  store.questionBank.forEach((q) => {
    const aliases = (q.aliases || []).length ? `also seen as: ${q.aliases.join(" · ")}` : "";
    qBox.append(editableRow(q.canonicalQuestion, q.answer, async (val) => {
      const s = await JAF.getStore();
      const target = s.questionBank.find((x) => x.id === q.id);
      target.answer = val;
      target.lastUpdated = new Date().toISOString();
      await JAF.setStore(s);
      renderKB();
    }, {
      sensitive: q.sensitive,
      subtitle: aliases,
      onDelete: async () => {
        const s = await JAF.getStore();
        s.questionBank = s.questionBank.filter((x) => x.id !== q.id);
        await JAF.setStore(s);
        renderKB();
      }
    }));
  });

  const fBox = $("#kb-forms");
  fBox.textContent = "";
  $("#kb-f-count").textContent = `(${store.formMappings.length})`;
  if (!store.formMappings.length) {
    fBox.append(el("div", { className: "empty", textContent: "No job-specific answers stored." }));
  }
  store.formMappings.forEach((m) => {
    const row = el("div", { className: "row" });
    row.append(el("div", { className: "label", textContent: `${m.domain} · ${m.fields.length} field(s)` }));
    row.append(el("div", {
      className: "sub",
      textContent: `${m.fields.map((f) => f.label).join(" · ")} — last used ${String(m.lastUsed).slice(0, 10)}`
    }));
    const del = el("button", { className: "link", textContent: "Delete" });
    del.addEventListener("click", async () => {
      const s = await JAF.getStore();
      s.formMappings = s.formMappings.filter((x) => x.formSignature !== m.formSignature);
      await JAF.setStore(s);
      renderKB();
    });
    row.append(el("div", { className: "inline" }, [del]));
    fBox.append(row);
  });
}

// ------------------------------------------------------------ export /import

async function doExport() {
  const store = await JAF.getStore();
  const blob = new Blob([JSON.stringify(store, null, 2)], { type: "application/json" });
  const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  chrome.downloads.download({
    url: URL.createObjectURL(blob),
    filename: `job-application-learner-${stamp}.json`,
    saveAs: true
  });
}

async function doImport(file) {
  let incoming;
  try {
    incoming = JSON.parse(await file.text());
  } catch (e) {
    return toast("That file isn't valid JSON.");
  }
  if (!incoming || typeof incoming !== "object" || !("schemaVersion" in incoming)) {
    return toast("Not a Ditto export.");
  }

  const replace = confirm(
    "OK = REPLACE everything with this file (current data is lost).\n" +
    "Cancel = merge it into what you already have."
  );
  if (replace && !confirm("Really replace all stored data? This can't be undone.")) return;

  const existing = await JAF.getStore();
  await JAF.setStore(replace ? JAF.migrate(incoming) : JAF.mergeStores(existing, incoming));
  toast(replace ? "Replaced." : "Merged.");
  renderKB();
}

// ----------------------------------------------------------------- bootstrap

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    showView(tab.dataset.view);
    if (tab.dataset.view === "kb") renderKB();
  });
});

/**
 * Is the knowledge service up? Shown in the header so the answer is visible
 * before you press anything, rather than discovered when a fill comes back
 * half-empty. Checked on every popup open — the popup is short-lived, so this
 * is a fresh answer, not a cached one.
 */
async function refreshServicePill() {
  const pill = $("#service-pill");
  pill.textContent = "checking…";
  pill.title = "Knowledge service";

  const res = await ask({ type: "JAF_SERVICE_HEALTH" });
  if (!res || !res.ok) {
    pill.textContent = "service off";
    pill.title = `Knowledge service unreachable: ${(res && res.error) || "no reply"}\n` +
                 "Fills use local storage only. Start it with service\\start.bat.";
    pill.dataset.state = "bad";
    return;
  }

  const d = res.data || {};
  if (d.authError) {
    pill.textContent = "no AI";
    pill.title = `Service is up (${d.records} records) but OpenRouter rejected the key:\n` +
                 `${d.authError}\nMatching still works; routing and drafting do not.`;
    pill.dataset.state = "warn";
    return;
  }

  pill.textContent = `${d.records} records`;
  pill.title = [
    `Knowledge service: up (${d.root})`,
    `${d.pointers} fillable pointers`,
    `retrieval: ${d.semantic ? "semantic + lexical" : "lexical only"}`,
    `AI: ${d.hasKey ? "on" : "off — no key set"}`
  ].join("\n");
  pill.dataset.state = "ok";
}

/** One-press fill, same path as Ctrl+Shift+F. The page does the asking. */
async function doAutofill() {
  const tab = await activeTab();
  if (!tab) return;
  const btn = $("#btn-autofill");
  btn.disabled = true;
  btn.textContent = "Filling…";
  try {
    const res = await askTab(tab.id, 0, { type: "JAF_AUTOFILL" });
    if (res && res.ok) window.close();      // the panel on the page takes over
    else btn.textContent = "No form found";
  } finally {
    btn.disabled = false;
    setTimeout(() => { btn.textContent = "Fill this form"; }, 2000);
  }
}

$("#btn-autofill").addEventListener("click", doAutofill);
$("#btn-scan").addEventListener("click", doScan);
$("#btn-save").addEventListener("click", doSave);
$("#btn-fill").addEventListener("click", doFillPreview);
$("#btn-fill-apply").addEventListener("click", applyFill);
$("#btn-fill-cancel").addEventListener("click", () => showView("scan"));
$("#btn-confirm").addEventListener("click", applySave);
$("#btn-cancel").addEventListener("click", () => showView("scan"));
$("#btn-pending").addEventListener("click", openPending);
$("#btn-pending-save").addEventListener("click", savePending);
$("#btn-pending-back").addEventListener("click", () => showView("scan"));
$("#btn-pending-discard").addEventListener("click", async () => {
  await dropPendingEntry();
  const store = await JAF.getStore();
  toast("Capture discarded.");
  if ((store.pending || []).length) return openPending();
  showView("scan");
});
$("#btn-export").addEventListener("click", doExport);
$("#btn-import").addEventListener("click", () => $("#import-file").click());
$("#import-file").addEventListener("change", (e) => {
  if (e.target.files[0]) doImport(e.target.files[0]);
  e.target.value = "";
});
$("#open-options").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
});

/**
 * Right-click "Scan this form" / "Save answers" hand off here via session
 * storage, since a context-menu click has no popup to act inside yet.
 */
async function runPendingAction() {
  const PENDING_KEY = "jafPendingAction";
  const tab = await activeTab();
  const stored = await new Promise((resolve) =>
    chrome.storage.session.get(PENDING_KEY, (res) => resolve(res[PENDING_KEY]))
  );
  if (!stored || !tab || stored.tabId !== tab.id) return false;
  chrome.storage.session.remove(PENDING_KEY);

  await doScan();
  if (stored.action === "save") await doSave();
  if (stored.action === "fill") await doFillPreview();
  return true;
}

refreshServicePill();
refreshPendingButton();
runPendingAction().then((ran) => { if (!ran) doScan(); });
