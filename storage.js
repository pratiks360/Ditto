/**
 * Shared helpers for every context (content script, popup, options, service worker).
 * Classic script — no modules — so it can be listed in content_scripts and
 * <script src>'d from the pages.
 *
 * The service worker cannot load this file at all (MV3 forbids importScripts in
 * a module-less worker), so background.js carries a generated copy inline. This
 * file stays the single source of truth: after editing it run
 *     node tools-inline-storage.js
 * or the worker keeps running the previous version.
 */
(function (root) {
  "use strict";

  const SCHEMA_VERSION = 3; // v2 added `resume`; v3 added `pending`
  const STORAGE_KEY = "jaf";

  /** How long an unused formMappings entry survives. */
  const FORM_MAPPING_TTL_DAYS = 90;

  function emptyStore() {
    return {
      schemaVersion: SCHEMA_VERSION,
      profile: {
        personal: {
          firstName: "",
          lastName: "",
          email: "",
          phone: "",
          address: "",
          linkedin: "",
          portfolio: ""
        },
        education: [],
        experience: [],
        skills: [],
        custom: {}
      },
      questionBank: [],
      formMappings: [],
      resume: { text: "", filename: "", updatedAt: "" },
      // Answers captured on submit, waiting for review. Nothing here has
      // reached the profile or the question bank yet.
      pending: []
    };
  }

  /** Keeps the review queue from growing without bound. */
  const PENDING_MAX = 25;

  // ---------------------------------------------------------------- storage io

  function getStore() {
    return new Promise((resolve) => {
      chrome.storage.local.get(STORAGE_KEY, (res) => {
        resolve(migrate(res && res[STORAGE_KEY]));
      });
    });
  }

  function setStore(store) {
    return new Promise((resolve) => {
      chrome.storage.local.set({ [STORAGE_KEY]: store }, resolve);
    });
  }

  /** Fills in anything a older/partial store is missing. Bump on schema changes. */
  function migrate(raw) {
    const base = emptyStore();
    if (!raw || typeof raw !== "object") return base;
    const store = {
      schemaVersion: SCHEMA_VERSION,
      profile: Object.assign(base.profile, raw.profile || {}),
      questionBank: Array.isArray(raw.questionBank) ? raw.questionBank : [],
      formMappings: Array.isArray(raw.formMappings) ? raw.formMappings : [],
      resume: Object.assign(base.resume, raw.resume || {}),
      pending: Array.isArray(raw.pending) ? raw.pending : []
    };
    store.profile.personal = Object.assign(
      base.profile.personal,
      (raw.profile && raw.profile.personal) || {}
    );
    return store;
  }

  // ------------------------------------------------------------------- debug

  const SETTINGS_KEY = "jafSettings";
  const DEBUG_KEY = "jafDebug";
  const DEBUG_MAX = 400;

  function getSettings() {
    return new Promise((resolve) => {
      chrome.storage.local.get(SETTINGS_KEY, (res) => resolve(res[SETTINGS_KEY] || {}));
    });
  }

  function setSettings(settings) {
    return new Promise((resolve) => {
      chrome.storage.local.set({ [SETTINGS_KEY]: settings }, resolve);
    });
  }

  /**
   * Appends one line to the rolling debug log, when the user has switched it on
   * in Options. NEVER pass captured answers in `data` — the log is meant to be
   * pasted into a bug report, so it carries labels, kinds and counts only.
   */
  async function debugLog(context, event, data) {
    const settings = await getSettings();
    if (!settings.debug) return;
    const entry = {
      ts: new Date().toISOString(),
      ctx: context,
      event,
      data: data === undefined ? null : data
    };
    return new Promise((resolve) => {
      chrome.storage.local.get(DEBUG_KEY, (res) => {
        const log = Array.isArray(res[DEBUG_KEY]) ? res[DEBUG_KEY] : [];
        log.push(entry);
        chrome.storage.local.set({ [DEBUG_KEY]: log.slice(-DEBUG_MAX) }, resolve);
      });
    });
  }

  function getDebugLog() {
    return new Promise((resolve) => {
      chrome.storage.local.get(DEBUG_KEY, (res) =>
        resolve(Array.isArray(res[DEBUG_KEY]) ? res[DEBUG_KEY] : [])
      );
    });
  }

  function clearDebugLog() {
    return new Promise((resolve) => chrome.storage.local.remove(DEBUG_KEY, resolve));
  }

  /** Renders the log as plain text for download. */
  function formatDebugLog(log) {
    return (log || [])
      .map((e) => `${e.ts}  [${e.ctx}] ${e.event}` +
        (e.data === null || e.data === undefined ? "" : `  ${JSON.stringify(e.data)}`))
      .join("\n");
  }

  // ------------------------------------------------------------ text utilities

  const LOWERCASE_WORDS = new Set(["of", "and", "the", "for", "in", "at"]);
  /** Tokens that must survive title-casing intact — otherwise "FZE" -> "Fze". */
  const KEEP_AS_IS = new Set([
    // regions / orgs
    "SAP", "IT", "HR", "UAE", "KSA", "USA", "UK", "AI", "ABAP", "IBM", "RBI",
    "EMEA", "APAC", "BFSI",
    // company-name suffixes
    "FZE", "FZCO", "LLC", "LTD", "INC", "PLC", "LLP", "DMCC", "PVT", "GMBH",
    "NV", "BV", "AG", "SA",
    // qualifications
    "MBA", "BE", "BSC", "MSC", "PHD", "BTECH", "MTECH",
    // tech
    "AWS", "GCP", "API", "APIS", "SQL", "ML", "CI", "CD", "DR", "HA", "XDCR"
  ]);

  function toTitleCase(str) {
    return String(str)
      .trim()
      .split(/\s+/)
      .map((word, i) => {
        // Compare on letters alone so "b.e." and "(mba)" match their acronym.
        const bare = word.replace(/[^A-Za-z]/g, "").toUpperCase();
        if (KEEP_AS_IS.has(bare)) return word.toUpperCase();
        // Dotted initialisms the list doesn't know: "k.j." -> "K.J."
        if (/^(?:[A-Za-z]\.){2,}$/.test(word)) return word.toUpperCase();
        if (i !== 0 && LOWERCASE_WORDS.has(word.toLowerCase())) {
          return word.toLowerCase();
        }
        return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
      })
      .join(" ");
  }

  /** Profile fields that are proper nouns, so "sap labs" saves as "SAP Labs". */
  const TITLE_CASE_KEYS = new Set([
    "firstName", "lastName", "fullName", "institution", "company", "degree",
    "title", "location"
  ]);

  const STOPWORDS = new Set([
    "a", "an", "the", "do", "does", "did", "you", "your", "yours", "i", "me",
    "my", "we", "our", "is", "are", "was", "were", "be", "been", "have", "has",
    "had", "of", "to", "in", "on", "at", "for", "with", "any", "please",
    "select", "choose", "enter", "provide", "if", "or", "and", "this", "that",
    "would", "will", "can", "may", "am", "as", "it", "us", "from", "about",
    "required", "optional"
  ]);

  /** Lowercase, strip punctuation and stopwords -> array of meaningful tokens. */
  function normalizeLabel(label) {
    return String(label || "")
      .toLowerCase()
      .replace(/[‘’“”]/g, "")
      .replace(/[^a-z0-9\s]/g, " ")
      .split(/\s+/)
      .filter((t) => t && !STOPWORDS.has(t));
  }

  /** Token-overlap (Jaccard) similarity of two label strings. 0..1 */
  function similarity(a, b) {
    const A = new Set(normalizeLabel(a));
    const B = new Set(normalizeLabel(b));
    if (!A.size || !B.size) return 0;
    let shared = 0;
    A.forEach((t) => { if (B.has(t)) shared++; });
    return shared / (A.size + B.size - shared);
  }

  const SIMILARITY_THRESHOLD = 0.5;

  /**
   * Best questionBank match for a label, or null.
   * Compares against canonicalQuestion and every alias.
   */
  function findQuestionMatch(label, questionBank, threshold) {
    const min = typeof threshold === "number" ? threshold : SIMILARITY_THRESHOLD;
    let best = null;
    (questionBank || []).forEach((entry) => {
      const candidates = [entry.canonicalQuestion].concat(entry.aliases || []);
      candidates.forEach((c) => {
        const score = similarity(label, c);
        if (score > min && (!best || score > best.score)) {
          best = { entry, score, matchedOn: c };
        }
      });
    });
    return best;
  }

  const SENSITIVE_PATTERNS = [
    "disability", "veteran", "race", "ethnic", "gender", "sexual orientation",
    "self-identif", "self identif", "eeo", "protected veteran", "pronoun"
  ];

  function looksSensitive(label) {
    const l = String(label || "").toLowerCase();
    return SENSITIVE_PATTERNS.some((p) => l.includes(p));
  }

  // -------------------------------------------------------------- resume parse

  // Leading qualifiers are optional so "WORK EXPERIENCE" and "PROFILE SUMMARY"
  // match as readily as bare "EXPERIENCE".
  const SECTION_HEADINGS = {
    education: /^(academic\s+)?(education|academics?|qualifications?)\b/i,
    experience: /^(work|professional|relevant|employment)?\s*(experience|employment|work history|career)\b/i,
    skills: /^(technical\s+|core\s+|key\s+)?(skills?|competenc|expertise|areas of excellence)\b/i,
    summary: /^(profile\s+)?(summary|profile|objective|about)\b/i
  };

  /**
   * Best-effort split of a plain-text resume into labelled sections. Resumes
   * have no standard shape, so this is a heuristic: a short line matching a
   * known heading starts a new section, everything after belongs to it.
   */
  function splitResumeSections(text) {
    const lines = String(text || "").split(/\r?\n/);
    const sections = { header: [] };
    let current = "header";
    lines.forEach((line) => {
      const trimmed = line.trim();
      const isHeading = trimmed.length > 0 && trimmed.length < 40 &&
        Object.keys(SECTION_HEADINGS).find((k) => SECTION_HEADINGS[k].test(trimmed));
      if (isHeading) {
        current = isHeading;
        if (!sections[current]) sections[current] = [];
        return;
      }
      if (!sections[current]) sections[current] = [];
      sections[current].push(line);
    });
    Object.keys(sections).forEach((k) => { sections[k] = sections[k].join("\n").trim(); });
    return sections;
  }

  const RE_EMAIL = /[\w.+-]+@[\w-]+\.[\w.-]+/;
  const RE_PHONE = /(\+?\d[\d\s().-]{7,}\d)/;
  const RE_LINKEDIN = /(?:https?:\/\/)?(?:www\.)?linkedin\.com\/in\/[\w-]+/i;
  const RE_GITHUB = /(?:https?:\/\/)?(?:www\.)?(?:github\.com|[\w-]+\.github\.io)\/?[\w-]*/i;
  const RE_YEARS = /\b(19|20)\d{2}\b/g;

  /** Extracts what can be recognised locally. Never calls out to any API. */
  function parseResume(text) {
    const sections = splitResumeSections(text);
    const head = sections.header || "";
    const all = String(text || "");

    const email = (all.match(RE_EMAIL) || [])[0] || "";
    const phoneRaw = (all.match(RE_PHONE) || [])[0] || "";
    const linkedin = (all.match(RE_LINKEDIN) || [])[0] || "";
    const github = (all.match(RE_GITHUB) || [])[0] || "";

    // Name: first header line that looks like a person, not contact details.
    const name = head.split(/\r?\n/)
      .map((l) => l.trim())
      .find((l) =>
        l && l.length < 60 && !RE_EMAIL.test(l) && !RE_PHONE.test(l) &&
        !/https?:|@|\d{4}/.test(l) && /^[A-Za-z][A-Za-z.'-]*(\s+[A-Za-z.'-]+){1,3}$/.test(l)
      ) || "";

    const skills = (sections.skills || "")
      .split(/[\n,;|•·]+/)
      .map((s) => s.trim())
      .filter((s) => s && s.length < 40)
      .slice(0, 60);

    /** Each non-empty block separated by a blank line is one entry. */
    const blocks = (raw) => String(raw || "")
      .split(/\n\s*\n/)
      .map((b) => b.trim())
      .filter(Boolean);

    const linesOf = (block) => block.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
    const stripBullet = (s) => String(s || "").replace(/^[•·●*\-–]\s*/, "").trim();

    /**
     * Recognises the one-line entry shape "Title | Company, Location | Period",
     * which is how an exported/flattened resume usually renders an entry that a
     * PDF laid out across several columns.
     */
    function pipeParts(line) {
      const parts = stripBullet(line).split(/\s*\|\s*/).map((s) => s.trim()).filter(Boolean);
      return parts.length >= 2 ? parts : null;
    }

    /** Splits "SAP Labs, Dubai" into company and location. */
    function splitWhere(where) {
      const at = String(where || "").lastIndexOf(",");
      return at > -1
        ? { name: where.slice(0, at).trim(), location: where.slice(at + 1).trim() }
        : { name: String(where || "").trim(), location: "" };
    }

    const education = [];
    blocks(sections.education).forEach((block) => {
      const lines = linesOf(block);
      // Entries may be one per line rather than one per blank-line block.
      if (lines.length && lines.every((l) => pipeParts(l))) {
        lines.forEach((line) => {
          const parts = pipeParts(line);
          const where = splitWhere(parts[1]);
          const years = line.match(RE_YEARS) || [];
          education.push({
            degree: parts[0],
            institution: where.name,
            startYear: years[0] || "",
            endYear: years[years.length - 1] || "",
            gpa: (line.match(/\b(?:gpa|cgpa)\s*:?\s*([\d.]+)/i) || [])[1] || ""
          });
        });
        return;
      }
      const years = block.match(RE_YEARS) || [];
      education.push({
        degree: lines.find((l) => /(bachelor|master|b\.?s|m\.?s|b\.?tech|m\.?tech|phd|diploma|mba)/i.test(l)) || lines[0] || "",
        institution: lines.find((l) => /(university|college|institute|school)/i.test(l)) || lines[1] || "",
        startYear: years[0] || "",
        endYear: years[years.length - 1] || "",
        gpa: (block.match(/\b(?:gpa|cgpa)\s*:?\s*([\d.]+)/i) || [])[1] || ""
      });
    });

    const experience = blocks(sections.experience).map((block) => {
      const lines = linesOf(block);
      const head = pipeParts(lines[0]);
      if (head) {
        const where = splitWhere(head[1]);
        const period = head[2] || "";
        const years = period.match(RE_YEARS) || [];
        return {
          title: head[0],
          company: where.name,
          location: where.location,
          startDate: years[0] || "",
          endDate: /present|current/i.test(period) ? "Present" : (years[years.length - 1] || ""),
          description: lines.slice(1).map(stripBullet).join(" ").slice(0, 600)
        };
      }
      const years = block.match(RE_YEARS) || [];
      return {
        title: lines[0] || "",
        company: lines[1] || "",
        startDate: years[0] || "",
        endDate: /present|current/i.test(block) ? "Present" : (years[years.length - 1] || ""),
        location: "",
        description: lines.slice(2).join(" ").slice(0, 600)
      };
    });

    return {
      personal: {
        firstName: name ? name.split(/\s+/)[0] : "",
        lastName: name ? name.split(/\s+/).slice(1).join(" ") : "",
        email,
        phone: phoneRaw.trim(),
        linkedin,
        portfolio: github
      },
      skills,
      education,
      experience,
      sections
    };
  }

  // ------------------------------------------------- extracted profile merge

  const MONTHS = {
    jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6,
    jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12
  };

  /**
   * Sortable number for the many date shapes a resume uses: "Sept 2025",
   * "2022-08-01", "01/06/2019", "2019", "Present". Bigger = more recent, so
   * anything ongoing sorts to the top. Unparseable dates sort last.
   */
  function dateSortKey(value) {
    const s = String(value || "").trim();
    if (!s) return -1;
    if (/present|current|ongoing|to date/i.test(s)) return Infinity;

    const iso = s.match(/\b(19|20)(\d{2})-(\d{1,2})/);
    if (iso) return Number(iso[1] + iso[2]) * 100 + Number(iso[3]);

    const dmy = s.match(/\b(\d{1,2})[/.-](\d{1,2})[/.-]((?:19|20)\d{2})\b/);
    if (dmy) return Number(dmy[3]) * 100 + Number(dmy[2]);

    const monthYear = s.match(/\b([A-Za-z]{3,9})\.?\s+((?:19|20)\d{2})\b/);
    if (monthYear) {
      const m = MONTHS[monthYear[1].slice(0, 3).toLowerCase()];
      if (m) return Number(monthYear[2]) * 100 + m;
    }

    const year = s.match(/\b((?:19|20)\d{2})\b/);
    return year ? Number(year[1]) * 100 : -1;
  }

  const ONGOING = /present|current|ongoing|to date|now\b/i;

  /**
   * Breaks a resume date into parts. Returns {ongoing:true} for "Present",
   * null when nothing date-like is there.
   */
  function parseDateParts(value) {
    const s = String(value || "").trim();
    if (!s) return null;
    if (ONGOING.test(s)) return { ongoing: true };

    let m = s.match(/\b((?:19|20)\d{2})-(\d{1,2})(?:-(\d{1,2}))?/);      // 2022-08-01
    if (m) return { year: +m[1], month: +m[2], day: m[3] ? +m[3] : null };

    m = s.match(/\b(\d{1,2})[/.-](\d{1,2})[/.-]((?:19|20)\d{2})\b/);      // 01/06/2019
    if (m) return { year: +m[3], month: +m[2], day: +m[1] };

    m = s.match(/\b(\d{1,2})[/.-]((?:19|20)\d{2})\b/);                    // 06/2019
    if (m) return { year: +m[2], month: +m[1], day: null };

    m = s.match(/\b([A-Za-z]{3,9})\.?\s+((?:19|20)\d{2})\b/);             // Sept 2025
    if (m) {
      const month = MONTHS[m[1].slice(0, 3).toLowerCase()];
      if (month) return { year: +m[2], month, day: null };
    }

    m = s.match(/\b((?:19|20)\d{2})\b/);                                  // 2019
    if (m) return { year: +m[1], month: null, day: null };

    return null;
  }

  /**
   * Written-out date masks, longest first so "DD/MM/YYYY" is not mistaken for
   * the "MM/YYYY" hiding inside it. The backreference keeps the separator the
   * page actually used, so "YYYY-MM" and "YYYY/MM" each come back in kind.
   */
  const MASK_PATTERNS = [
    [/\bY{2,4}(\s*[/.\-]\s*)M{1,2}\1D{1,2}\b/i, "YMD"],
    [/\bD{1,2}(\s*[/.\-]\s*)M{1,2}\1Y{2,4}\b/i, "DMY"],
    [/\bM{1,2}(\s*[/.\-]\s*)D{1,2}\1Y{2,4}\b/i, "MDY"],
    [/\bY{2,4}(\s*[/.\-]\s*)M{1,2}\b/i, "YM"],
    [/\bM{1,2}(\s*[/.\-]\s*)Y{2,4}\b/i, "MY"],
    [/(^|\s)Y{4}(\s|$)/i, "Y"]
  ];

  /** Everywhere a control might advertise the format it expects. */
  function hintText(hints) {
    const h = hints || {};
    return `${h.placeholder || ""} ${h.pattern || ""} ${h.hint || ""}`;
  }

  /**
   * The date layout a text box is asking for, e.g. {order:"MY", sep:"/"}.
   * Null when nothing in the field advertises a date format.
   */
  function detectDateMask(hints) {
    const text = hintText(hints);
    for (let i = 0; i < MASK_PATTERNS.length; i++) {
      const [re, order] = MASK_PATTERNS[i];
      const m = text.match(re);
      if (m) return { order, sep: (m[1] || "/").trim() || "/" };
    }
    return null;
  }

  /** True when the control only accepts a date, in some specific encoding. */
  function isDateField(hints) {
    const h = hints || {};
    const type = String(h.inputType || "").toLowerCase();
    if (type === "month" || type === "date" || type === "week") return true;
    return detectDateMask(h) !== null;
  }

  const pad2 = (n) => String(n).padStart(2, "0");

  /**
   * Re-encodes a stored date for the control it is going into. A date widget
   * rejects anything but its own format — "Sept 2025" typed into an
   * <input type="month"> is simply dropped — so a stored date has to be
   * rewritten as "2025-09" before it will stick.
   *
   * Returns "" when the value cannot be represented (an ongoing role has no
   * end date), and leaves non-date fields untouched.
   */
  function formatDateForField(value, hints) {
    if (!isDateField(hints)) return value;

    const parts = parseDateParts(value);
    if (!parts || parts.ongoing) return "";

    const h = hints || {};
    const type = String(h.inputType || "").toLowerCase();
    if (type === "month") return parts.month ? `${parts.year}-${pad2(parts.month)}` : "";
    if (type === "date") {
      return parts.month ? `${parts.year}-${pad2(parts.month)}-${pad2(parts.day || 1)}` : "";
    }

    const mask = detectDateMask(h);
    if (!mask) return value;
    if (mask.order === "Y") return String(parts.year);
    if (!parts.month) return "";

    // Resume dates are month + year, so the day is ours to choose: the 1st is
    // the only defensible answer, and it is what these forms expect.
    const dd = pad2(parts.day || 1);
    const mm = pad2(parts.month);
    const yyyy = String(parts.year);
    const s = mask.sep;

    if (mask.order === "YMD") return `${yyyy}${s}${mm}${s}${dd}`;
    if (mask.order === "DMY") return `${dd}${s}${mm}${s}${yyyy}`;
    if (mask.order === "MDY") return `${mm}${s}${dd}${s}${yyyy}`;
    if (mask.order === "YM") return `${yyyy}${s}${mm}`;
    return `${mm}${s}${yyyy}`;   // MY
  }

  /**
   * Open questions whose answer is a judgement about *this* employer, not a
   * fact about the candidate. Nothing stored can answer these honestly, so
   * whatever is offered for them is a starting draft the user must own.
   */
  const OPINION_QUESTION =
    /\bwhy (do you |would you |are you )?(want|wish|like|interested|choose|apply|us|this)\b|\bwhat (makes|attracts|draws|excites|motivates)\b|\bgood fit\b|\bright fit\b|\bfit for (this|the) role\b|\btell us about\b|\bcover letter\b|\bin your own words\b|\bwhy should we\b|\bmotivat/i;

  function isOpinionQuestion(label) {
    return OPINION_QUESTION.test(String(label || ""));
  }

  /** Labels meaning "this is the job I'm in now". */
  const CURRENT_ROLE_LABEL =
    /\b(currently|presently|still)\b[^.]*\b(work|working|employed|here|role|position)\b|\bcurrent (job|role|position|employer)\b|\bpresent employer\b|\bi work here\b/i;

  /** True when this experience entry has no end — the role is ongoing. */
  function isOngoingRole(entry) {
    if (!entry) return false;
    return !String(entry.endDate || "").trim() || ONGOING.test(String(entry.endDate));
  }

  /** Orders work history and education newest-first, in place. */
  function sortProfileHistory(store) {
    (store.profile.experience || []).sort(
      (a, b) => dateSortKey(b.startDate) - dateSortKey(a.startDate)
    );
    (store.profile.education || []).sort(
      (a, b) => (dateSortKey(b.endYear) - dateSortKey(a.endYear)) ||
                (dateSortKey(b.startYear) - dateSortKey(a.startYear))
    );
    return store;
  }

  const EXPERIENCE_KEYS = ["title", "company", "location", "startDate", "endDate", "description"];
  const EDUCATION_KEYS = ["degree", "field", "institution", "location", "startYear", "endYear", "gpa"];

  const str = (v) => (v === null || v === undefined ? "" : String(v).trim());

  /**
   * Title-cases a proper-noun field, but only when the value is uniformly
   * lower or upper case — that is the shape a form dump or a model produces.
   * Mixed case means it was already written properly ("Master of Science
   * (MBA)"), and re-casing it would do more harm than good.
   */
  function normalizeEntryText(key, value) {
    const v = str(value);
    if (!v || !TITLE_CASE_KEYS.has(key)) return v;
    const uniform = v === v.toLowerCase() || v === v.toUpperCase();
    return uniform ? toTitleCase(v) : v;
  }

  function pick(raw, keys) {
    const out = {};
    keys.forEach((k) => { out[k] = normalizeEntryText(k, raw && raw[k]); });
    return out;
  }

  /**
   * Coerces whatever the model returned into the profile shape. The model is
   * asked for this schema, but it is a language model — assume nothing about
   * types, drop entries with no identifying field, and keep only strings.
   */
  function normalizeExtractedProfile(raw) {
    const src = (raw && typeof raw === "object") ? raw : {};
    const personalSrc = (src.personal && typeof src.personal === "object") ? src.personal : {};
    const personal = pick(personalSrc, [
      "firstName", "lastName", "email", "phone", "address", "linkedin", "portfolio"
    ]);

    const asArray = (v) => (Array.isArray(v) ? v : []);

    const experience = asArray(src.experience)
      .map((e) => pick(e, EXPERIENCE_KEYS))
      .filter((e) => e.title || e.company)
      .map((e) => {
        // Models often fall back to echoing the job title when the resume gives
        // no summary line. A description that only repeats the title tells a
        // reader nothing, and filling "Role Description" with "Software
        // Developer" is worse than leaving it for the user to write.
        const same = normalizeLabel(e.description).join(" ") === normalizeLabel(e.title).join(" ");
        if (same) e.description = "";
        return e;
      });

    const education = asArray(src.education)
      .map((e) => pick(e, EDUCATION_KEYS))
      .filter((e) => e.degree || e.institution);

    const skills = Array.from(new Set(
      asArray(src.skills).map(str).filter((s) => s && s.length < 60)
    ));

    // "Other data" — certifications, languages, notice period, work
    // authorisation. Kept as label -> answer so form fill can match on label.
    const custom = {};
    const customSrc = (src.custom && typeof src.custom === "object") ? src.custom : {};
    Object.keys(customSrc).forEach((k) => {
      const key = str(k);
      const val = Array.isArray(customSrc[k])
        ? customSrc[k].map(str).filter(Boolean).join(", ")
        : str(customSrc[k]);
      if (key && val) custom[key] = val;
    });

    return { personal, experience, education, skills, custom };
  }

  /** Identity of a history entry, for spotting one already stored. */
  function entryKey(entry, keys) {
    return keys.map((k) => normalizeLabel(entry[k]).join(" ")).join("|");
  }

  /**
   * Writes an extracted profile into the store. Existing values win — this
   * fills gaps and appends history entries that aren't already there, so
   * running it twice, or after hand-editing the profile, is safe.
   * @returns {{filled: string[], added: object, skipped: number}}
   */
  function mergeExtractedProfile(store, extracted) {
    const parsed = normalizeExtractedProfile(extracted);
    const filled = [];
    const added = { experience: 0, education: 0, skills: 0, custom: 0 };
    let skipped = 0;

    Object.keys(parsed.personal).forEach((k) => {
      if (!store.profile.personal[k] && parsed.personal[k]) {
        store.profile.personal[k] = normalizeEntryText(k, parsed.personal[k]);
        filled.push(k);
      }
    });

    [["experience", EXPERIENCE_KEYS.slice(0, 2)],
     ["education", ["degree", "institution"]]].forEach(([section, idKeys]) => {
      const existing = new Set((store.profile[section] || []).map((e) => entryKey(e, idKeys)));
      parsed[section].forEach((entry) => {
        if (existing.has(entryKey(entry, idKeys))) {
          skipped++;
          return;
        }
        existing.add(entryKey(entry, idKeys));
        store.profile[section].push(entry);
        added[section]++;
      });
    });

    const skills = new Set(store.profile.skills || []);
    const before = skills.size;
    parsed.skills.forEach((s) => skills.add(s));
    store.profile.skills = Array.from(skills);
    added.skills = skills.size - before;

    store.profile.custom = store.profile.custom || {};
    Object.keys(parsed.custom).forEach((k) => {
      if (!store.profile.custom[k]) {
        store.profile.custom[k] = parsed.custom[k];
        added.custom++;
      }
    });

    sortProfileHistory(store);
    return { filled, added, skipped };
  }

  /**
   * Works out what mergeExtractedProfile would do, without doing it, so the
   * review screen can show every value against the profile field it lands in
   * and whether it will be written or held back.
   * @returns {{target:string,value:string,action:"fill"|"keep"|"add"|"duplicate"}[]}
   */
  function planExtractedProfile(store, extracted) {
    const parsed = normalizeExtractedProfile(extracted);
    const rows = [];

    Object.keys(parsed.personal).forEach((k) => {
      const value = parsed.personal[k];
      if (!value) return;
      const current = store.profile.personal[k];
      rows.push({
        target: `personal.${k}`,
        value: normalizeEntryText(k, value),
        action: current ? "keep" : "fill",
        current: current || ""
      });
    });

    [["experience", ["title", "company"], (e) => `${e.title} @ ${e.company}`],
     ["education", ["degree", "institution"], (e) => `${e.degree} @ ${e.institution}`]
    ].forEach(([section, idKeys, render]) => {
      const existing = new Set((store.profile[section] || []).map((e) => entryKey(e, idKeys)));
      parsed[section].forEach((entry, i) => {
        const dupe = existing.has(entryKey(entry, idKeys));
        rows.push({
          target: `${section}[${i}]`,
          value: render(entry),
          action: dupe ? "duplicate" : "add",
          current: ""
        });
      });
    });

    const known = new Set(store.profile.skills || []);
    const fresh = parsed.skills.filter((s) => !known.has(s));
    if (parsed.skills.length) {
      rows.push({
        target: "skills",
        value: `${fresh.length} new of ${parsed.skills.length}` +
               (fresh.length ? `: ${fresh.slice(0, 12).join(", ")}${fresh.length > 12 ? "…" : ""}` : ""),
        action: fresh.length ? "add" : "duplicate",
        current: ""
      });
    }

    Object.keys(parsed.custom).forEach((k) => {
      const current = (store.profile.custom || {})[k];
      rows.push({
        target: `custom.${k}`,
        value: parsed.custom[k],
        action: current ? "keep" : "fill",
        current: current || ""
      });
    });

    return rows;
  }

  // ------------------------------------------------------------ source routing

  /**
   * The set of values the profile can offer a form, as addressable pointers.
   *
   * This is what the model is shown when it decides which stored value belongs
   * in which field. It deliberately carries no answers — only keys, plus enough
   * of each job/degree to tell blocks apart ("end date of Consultant at
   * Capgemini"). The model returns a pointer and the extension substitutes the
   * real value, so a stored fact can never be reworded on its way into a form.
   */
  function buildSourceCatalog(store) {
    const out = [];
    const personal = store.profile.personal || {};
    Object.keys(personal).forEach((k) => {
      if (personal[k]) out.push({ source: `personal.${k}`, label: `your ${k}` });
    });

    (store.profile.experience || []).forEach((e, i) => {
      const who = [e.title, e.company].filter(Boolean).join(" at ") || `job ${i + 1}`;
      EXPERIENCE_KEYS.forEach((k) => {
        if (e[k]) out.push({ source: `experience[${i}].${k}`, label: `${k} of ${who}` });
      });
    });

    (store.profile.education || []).forEach((e, i) => {
      const what = [e.degree, e.institution].filter(Boolean).join(" at ") || `study ${i + 1}`;
      EDUCATION_KEYS.forEach((k) => {
        if (e[k]) out.push({ source: `education[${i}].${k}`, label: `${k} of ${what}` });
      });
    });

    if ((store.profile.skills || []).length) {
      out.push({ source: "skills", label: "your full list of skills" });
    }
    Object.keys(store.profile.custom || {}).forEach((k) => {
      out.push({ source: `custom:${k}`, label: k });
    });
    (store.questionBank || []).forEach((q) => {
      out.push({ source: `question:${q.id}`, label: `your saved answer to "${q.canonicalQuestion}"` });
    });
    return out;
  }

  /** Turns a catalog pointer back into the stored value, or null. */
  function resolveSource(store, source) {
    const s = String(source || "").trim();
    if (!s || s === "none" || s === "generate") return null;

    if (s === "skills") return (store.profile.skills || []).join(", ") || null;

    let m = s.match(/^personal\.(.+)$/);
    if (m) return (store.profile.personal || {})[m[1]] || null;

    m = s.match(/^(experience|education)\[(\d+)\]\.(.+)$/);
    if (m) {
      const entry = (store.profile[m[1]] || [])[Number(m[2])];
      return (entry && entry[m[3]]) || null;
    }

    m = s.match(/^custom:(.+)$/);
    if (m) return (store.profile.custom || {})[m[1]] || null;

    m = s.match(/^question:(.+)$/);
    if (m) {
      const q = (store.questionBank || []).find((x) => x.id === m[1]);
      return q ? q.answer : null;
    }
    return null;
  }

  /** Readable provenance for the review screen. */
  function describeSource(store, source) {
    const hit = buildSourceCatalog(store).find((c) => c.source === source);
    return hit ? hit.label : String(source || "");
  }

  /**
   * Fuzzy lookup of a form label against the free-form `custom` facts, so
   * "What is your notice period?" reaches a stored "notice period" entry.
   */
  function findCustomMatch(label, custom, threshold) {
    const min = typeof threshold === "number" ? threshold : SIMILARITY_THRESHOLD;
    let best = null;
    Object.keys(custom || {}).forEach((key) => {
      const score = similarity(label, key);
      if (score > min && (!best || score > best.score)) {
        best = { key, value: custom[key], score };
      }
    });
    return best;
  }

  /** Fills only the profile gaps — never overwrites what you've already saved. */
  function applyResumeToProfile(store, parsed) {
    const filled = [];
    Object.keys(parsed.personal).forEach((k) => {
      if (!store.profile.personal[k] && parsed.personal[k]) {
        store.profile.personal[k] = parsed.personal[k];
        filled.push(k);
      }
    });
    if (!store.profile.skills.length && parsed.skills.length) {
      store.profile.skills = parsed.skills;
      filled.push("skills");
    }
    ["education", "experience"].forEach((section) => {
      if (!store.profile[section].length && parsed[section].length) {
        store.profile[section] = parsed[section];
        filled.push(section);
      }
    });
    return filled;
  }

  // ------------------------------------------------------------- form identity

  /** Stable non-crypto hash (FNV-1a) rendered as hex. */
  function hashString(str) {
    let h = 0x811c9dc5;
    for (let i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
    }
    return h.toString(16).padStart(8, "0");
  }

  /** Signature of a form = hash of its sorted, normalized field labels. */
  function formSignature(labels) {
    const normalized = (labels || [])
      .map((l) => normalizeLabel(l).join(" "))
      .filter(Boolean)
      .sort();
    return hashString(normalized.join("|"));
  }

  // ------------------------------------------------------------ import / merge

  function newer(a, b) {
    return new Date(a || 0).getTime() >= new Date(b || 0).getTime();
  }

  /**
   * Merge an imported store into the existing one.
   *   profile scalars: incoming wins only where existing is empty
   *   questionBank:    per-entry, newer lastUpdated wins; aliases unioned
   *   formMappings:    union by formSignature, newer lastUsed wins
   */
  function mergeStores(existing, incoming) {
    const base = migrate(existing);
    const add = migrate(incoming);
    const out = migrate(base);

    Object.keys(add.profile.personal).forEach((k) => {
      if (!out.profile.personal[k] && add.profile.personal[k]) {
        out.profile.personal[k] = add.profile.personal[k];
      }
    });
    out.profile.custom = Object.assign({}, add.profile.custom, base.profile.custom);
    out.profile.skills = Array.from(new Set(
      (base.profile.skills || []).concat(add.profile.skills || [])
    ));
    ["education", "experience"].forEach((k) => {
      const seen = new Set((base.profile[k] || []).map((e) => JSON.stringify(e)));
      out.profile[k] = (base.profile[k] || []).slice();
      (add.profile[k] || []).forEach((e) => {
        if (!seen.has(JSON.stringify(e))) out.profile[k].push(e);
      });
    });

    const byId = new Map(out.questionBank.map((q) => [q.id, q]));
    add.questionBank.forEach((q) => {
      const cur = byId.get(q.id);
      if (!cur) {
        byId.set(q.id, q);
        return;
      }
      const aliases = Array.from(new Set((cur.aliases || []).concat(q.aliases || [])));
      const winner = newer(cur.lastUpdated, q.lastUpdated) ? cur : q;
      byId.set(q.id, Object.assign({}, winner, { aliases }));
    });
    out.questionBank = Array.from(byId.values());

    const bySig = new Map(out.formMappings.map((f) => [f.formSignature, f]));
    add.formMappings.forEach((f) => {
      const cur = bySig.get(f.formSignature);
      if (!cur || !newer(cur.lastUsed, f.lastUsed)) bySig.set(f.formSignature, f);
    });
    out.formMappings = Array.from(bySig.values());

    out.resume = newer(base.resume.updatedAt, add.resume.updatedAt) ? base.resume : add.resume;

    return out;
  }

  /**
   * Files a submitted form into the review queue. Replaces an earlier capture
   * of the same form rather than stacking near-identical entries, so correcting
   * a mistake and resubmitting leaves one row to review, not two.
   */
  function addPendingCapture(store, capture) {
    const items = (capture.items || []).filter((i) => i && String(i.value || "").trim());
    if (!items.length) return store;

    store.pending = (store.pending || []).filter(
      (p) => !(p.formSignature === capture.formSignature && p.domain === capture.domain)
    );
    store.pending.push({
      id: `cap_${Date.now()}_${Math.random().toString(16).slice(2, 8)}`,
      ts: capture.ts || new Date().toISOString(),
      domain: capture.domain || "",
      url: capture.url || "",
      formSignature: capture.formSignature || "",
      items
    });
    store.pending = store.pending.slice(-PENDING_MAX);
    return store;
  }

  /** Drops formMappings untouched for FORM_MAPPING_TTL_DAYS. */
  function pruneFormMappings(store, now) {
    const cutoff = (now || Date.now()) - FORM_MAPPING_TTL_DAYS * 86400000;
    store.formMappings = (store.formMappings || []).filter(
      (f) => new Date(f.lastUsed || 0).getTime() >= cutoff
    );
    return store;
  }

  root.JAF = {
    SCHEMA_VERSION,
    STORAGE_KEY,
    SETTINGS_KEY,
    DEBUG_KEY,
    DEBUG_MAX,
    getSettings,
    setSettings,
    debugLog,
    getDebugLog,
    clearDebugLog,
    formatDebugLog,
    FORM_MAPPING_TTL_DAYS,
    SIMILARITY_THRESHOLD,
    emptyStore,
    getStore,
    setStore,
    migrate,
    toTitleCase,
    TITLE_CASE_KEYS,
    dateSortKey,
    parseDateParts,
    detectDateMask,
    isDateField,
    formatDateForField,
    isOngoingRole,
    isOpinionQuestion,
    CURRENT_ROLE_LABEL,
    OPINION_QUESTION,
    sortProfileHistory,
    normalizeExtractedProfile,
    mergeExtractedProfile,
    planExtractedProfile,
    findCustomMatch,
    buildSourceCatalog,
    resolveSource,
    describeSource,
    normalizeLabel,
    similarity,
    findQuestionMatch,
    looksSensitive,
    hashString,
    formSignature,
    parseResume,
    applyResumeToProfile,
    splitResumeSections,
    mergeStores,
    PENDING_MAX,
    addPendingCapture,
    pruneFormMappings
  };
})(typeof globalThis !== "undefined" ? globalThis : self);
