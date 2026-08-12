/**
 * Content script — scans job application forms and reads back what the user
 * typed. LEARN ONLY: it never writes to a field and never touches a submit
 * control. See SUBMIT_SELECTOR / isSubmitControl below.
 */
(function () {
  "use strict";

  const JAF = globalThis.JAF;

  /** Anything matching this is invisible to the extension, forever. */
  const SUBMIT_SELECTOR =
    'button[type="submit"], input[type="submit"], [role="button"][aria-label*="submit" i]';

  /** Input types we never scan: no value of ours, or actively unsafe to store. */
  const SKIPPED_INPUT_TYPES = new Set([
    "password", "hidden", "submit", "button", "image", "reset", "file"
  ]);

  const FREE_TEXT_MIN_LENGTH = 120;

  // Label pattern -> profile.personal key.
  const PROFILE_PATTERNS = [
    [/\b(first|given)\s*name\b/i, "personal.firstName"],
    [/\b(last|family|sur)\s*name\b/i, "personal.lastName"],
    [/\bfull\s*name\b|^name$/i, "personal.fullName"],
    [/\be-?mail\b/i, "personal.email"],
    [/\b(phone|mobile|contact number|telephone)\b/i, "personal.phone"],
    [/\b(address|street|city|state|postal|zip|country|location)\b/i, "personal.address"],
    [/\blinked ?in\b/i, "personal.linkedin"],
    [/\b(portfolio|website|github|personal site)\b/i, "personal.portfolio"],
    [/\b(university|college|school|institution)\b/i, "education.institution"],
    [/\b(degree|qualification|major|field of study)\b/i, "education.degree"],
    [/\bgpa\b|\bgrade\b/i, "education.gpa"],
    [/\b(employer|company|organisation|organization)\b/i, "experience.company"],
    [/\b(job title|position|role|current title)\b/i, "experience.title"]
  ];

  /**
   * Labels that mean different things depending on the block they sit in —
   * "Start Date" is a startYear under Education and a startDate under
   * Experience. Resolved once the block's section is known; see resolveBlocks.
   */
  const CONTEXT_PATTERNS = [
    [/\b(start|from|joining|began)\b|\bdate from\b/i, "start"],
    [/\b(end|to|until|completion|graduat\w*|leaving)\b|\bdate to\b/i, "end"],
    [/\b(description|responsibilities|duties|achievements|summary)\b/i, "description"],
    [/\b(location|city|based in)\b/i, "location"]
  ];

  /** The month/day/year sub-controls a split date picker is made of. */
  const DATE_PART = /^(month|day|year|mm|dd|yy|yyyy|from|to)$/i;

  const DATE_CONTEXT = /\b(date|year|from|to|start|end|graduat|period|duration)\b/i;

  const AUTOCOMPLETE_MAP = {
    "given-name": "personal.firstName",
    "family-name": "personal.lastName",
    name: "personal.fullName",
    email: "personal.email",
    tel: "personal.phone",
    "street-address": "personal.address",
    "address-line1": "personal.address",
    "postal-code": "personal.address",
    url: "personal.portfolio",
    organization: "experience.company",
    "organization-title": "experience.title"
  };

  // ------------------------------------------------------------------ elements

  function isSubmitControl(el) {
    try {
      return !!(el && el.matches && el.matches(SUBMIT_SELECTOR));
    } catch (e) {
      return true; // if in doubt, treat as submit and leave it alone
    }
  }

  function isVisible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    const style = getComputedStyle(el);
    return style.visibility !== "hidden" && style.display !== "none";
  }

  /** querySelectorAll that also descends into open shadow roots. */
  function deepQuery(selector, root, out) {
    const acc = out || [];
    const scope = root || document;
    scope.querySelectorAll(selector).forEach((el) => acc.push(el));
    scope.querySelectorAll("*").forEach((el) => {
      if (el.shadowRoot) deepQuery(selector, el.shadowRoot, acc);
    });
    return acc;
  }

  function collectFields() {
    return deepQuery("input, select, textarea")
      .filter((el) => {
        if (isSubmitControl(el)) return false;
        if (el.tagName === "INPUT" && SKIPPED_INPUT_TYPES.has(el.type)) return false;
        if (el.disabled) return false;
        return isVisible(el);
      });
  }

  // -------------------------------------------------------------------- labels

  function cleanText(text) {
    return dedupeLabel(
      String(text || "")
        .replace(/\s+/g, " ")
        .replace(/[*:]\s*$/, "")
        .trim()
    );
  }

  /**
   * Collapses a label that says the same thing twice.
   *
   * Sites that render a visible label *and* a screen-reader copy inside the same
   * container produce "Do you have X?Do you have X?" once both are read. Left
   * alone that doubled string becomes the stored question, so the knowledge base
   * fills up with keys no future form will ever match.
   */
  function dedupeLabel(text) {
    if (text.length < 12) return text;
    // The two copies may be joined directly ("...?Do you...") or by a single
    // space, which makes the total length odd.
    const half = text.length % 2 === 0 ? text.length / 2 : (text.length - 1) / 2;
    const a = text.slice(0, half).trim();
    const b = text.slice(text.length - half).trim();
    return a && a === b ? a : text;
  }

  /** Text of an element, ignoring nested form controls. */
  function ownText(el) {
    if (!el) return "";
    const clone = el.cloneNode(true);
    clone.querySelectorAll("input, select, textarea").forEach((n) => n.remove());
    return cleanText(clone.textContent);
  }

  function isControl(node) {
    return /^(INPUT|SELECT|TEXTAREA)$/.test(node.tagName);
  }

  /**
   * Walks backwards through the DOM for the nearest preceding text, stopping
   * dead at the first thing that holds another form control — otherwise a
   * field with no label of its own borrows the previous field's.
   */
  function precedingText(el) {
    let node = el;
    for (let hops = 0; hops < 6 && node; hops++) {
      let sib = node.previousElementSibling;
      while (sib) {
        if (isControl(sib) || controlsIn(sib).length) return "";
        const text = ownText(sib);
        if (text && text.length < 200) return text;
        sib = sib.previousElementSibling;
      }
      node = node.parentElement;
      if (node && /^(FORM|BODY|HTML)$/.test(node.tagName)) break;
    }
    return "";
  }

  /** Every form control inside a node, submit buttons excluded. */
  function controlsIn(node) {
    return Array.from(node.querySelectorAll("input, select, textarea")).filter(
      (c) => !(c.tagName === "INPUT" && SKIPPED_INPUT_TYPES.has(c.type))
    );
  }

  /**
   * A <label> that sits beside the input rather than wrapping it or pointing at
   * it — the React/Tailwind pattern where the input has no id to reference.
   * Only trusted when the surrounding container holds exactly one control, so
   * we can't steal a neighbouring field's label.
   */
  function siblingLabel(el) {
    let node = el.parentElement;
    for (let hops = 0; hops < 3 && node; hops++) {
      if (/^(FORM|BODY|HTML)$/.test(node.tagName)) break;
      if (controlsIn(node).length === 1) {
        const candidate = Array.from(node.querySelectorAll("label")).find(
          (l) => !l.getAttribute("for") && !controlsIn(l).length
        );
        const text = ownText(candidate);
        if (text) return text;
      }
      node = node.parentElement;
    }
    return "";
  }

  /** Returns { text, source } — source tells us how much to trust it. */
  function extractLabel(el) {
    const root = el.getRootNode();

    if (el.id) {
      const forLabel = root.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      const text = ownText(forLabel);
      if (text) return { text, source: "label[for]" };
    }

    const wrapping = el.closest && el.closest("label");
    if (wrapping) {
      const text = ownText(wrapping);
      if (text) return { text, source: "wrapping-label" };
    }

    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const text = labelledBy
        .split(/\s+/)
        .map((id) => ownText(root.getElementById && root.getElementById(id)))
        .filter(Boolean)
        .join(" ");
      if (text) return { text, source: "aria-labelledby" };
    }

    const ariaLabel = cleanText(el.getAttribute("aria-label"));
    if (ariaLabel) return { text: ariaLabel, source: "aria-label" };

    // Fieldset legend — the usual home of a radio group's real question.
    const fieldset = el.closest && el.closest("fieldset");
    if (fieldset) {
      const legend = ownText(fieldset.querySelector("legend"));
      if (legend) return { text: legend, source: "legend" };
    }

    const sibling = siblingLabel(el);
    if (sibling) return { text: sibling, source: "sibling-label" };

    // Placeholders are example content ("ada lovelace", "you@domain.com"), so
    // they rank below any real label text we can find nearby.
    const preceding = precedingText(el);
    if (preceding) return { text: preceding, source: "preceding-text" };

    const placeholder = cleanText(el.getAttribute("placeholder"));
    if (placeholder) return { text: placeholder, source: "placeholder" };

    const name = cleanText(el.name || el.id);
    if (name) {
      return {
        text: cleanText(name.replace(/[_\-.]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2")),
        source: "name-attr"
      };
    }
    return { text: "", source: "none" };
  }

  /** Attributes that carry an input mask on one widget library or another. */
  const FORMAT_ATTRS = [
    "title", "alt", "aria-placeholder", "data-format", "data-date-format",
    "data-mask", "data-inputmask", "data-pattern", "format"
  ];

  /**
   * Everything the page says about the format it wants, gathered from wherever
   * it happens to live. A date control rarely puts "MM/YYYY" in `placeholder` —
   * it turns up in a title, an alt, a data-* mask, or the hint paragraph the
   * field points at with aria-describedby.
   */
  function formatHint(el) {
    const bits = [];

    FORMAT_ATTRS.forEach((attr) => {
      const v = cleanText(el.getAttribute(attr));
      if (v) bits.push(v);
    });

    const root = el.getRootNode ? el.getRootNode() : document;
    const describedBy = el.getAttribute("aria-describedby");
    if (describedBy) {
      describedBy.split(/\s+/).forEach((id) => {
        const text = ownText(root.getElementById && root.getElementById(id));
        if (text) bits.push(text);
      });
    }

    // The little grey hint rendered next to the box.
    const parent = el.parentElement;
    if (parent) {
      Array.from(parent.querySelectorAll("small, .hint, .help, .helper, .format, [class*='hint'], [class*='help']"))
        .slice(0, 3)
        .forEach((node) => {
          const text = cleanText(node.textContent);
          if (text && text.length <= 40) bits.push(text);
        });
    }

    return cleanText(bits.join(" ")).slice(0, 160);
  }

  // ----------------------------------------------------------------- selectors

  function cssPath(el) {
    if (el.id) return `#${CSS.escape(el.id)}`;
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 6) {
      let part = node.tagName.toLowerCase();
      if (node.name) {
        part += `[name="${CSS.escape(node.name)}"]`;
        parts.unshift(part);
        break;
      }
      const parent = node.parentElement;
      if (parent) {
        const sameTag = Array.from(parent.children).filter(
          (c) => c.tagName === node.tagName
        );
        if (sameTag.length > 1) {
          part += `:nth-of-type(${sameTag.indexOf(node) + 1})`;
        }
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(" > ");
  }

  // -------------------------------------------------------------- field groups

  /**
   * Turns raw elements into logical fields:
   * radios sharing a name collapse into one choice group; selects and
   * checkboxes stand alone; everything else is a plain field.
   */
  function buildGroups(elements) {
    const groups = [];
    const radioGroups = new Map();

    elements.forEach((el) => {
      const tag = el.tagName.toLowerCase();
      const type = tag === "input" ? el.type : tag;

      if (type === "radio" && el.name) {
        const key = `radio:${el.name}`;
        if (!radioGroups.has(key)) {
          const group = {
            kind: "radio",
            key,
            name: el.name,
            elements: [],
            options: []
          };
          radioGroups.set(key, group);
          groups.push(group);
        }
        const group = radioGroups.get(key);
        group.elements.push(el);
        group.options.push({
          value: el.value,
          label: extractLabel(el).text || el.value
        });
        return;
      }

      // A radio with no name can't be grouped with siblings — nothing shares
      // its key — so treat it like a standalone checkbox instead of "radio",
      // otherwise groupLabel's radio-only fallback reads group.name (undefined).
      const kind = type === "select" ? "select" : type === "radio" ? "radio-lone" : type;
      groups.push({ kind, elements: [el], options: [] });
    });

    return mergeDateParts(groups);
  }

  /**
   * Split date pickers ("Start Date" rendered as Month + Year selects) arrive as
   * separate controls whose own labels are just "Month" / "Year". Left alone
   * they become junk question-bank entries like Month = "May". Cluster the parts
   * under their shared container and treat them as one date field.
   */
  const MONTH_NAME = /^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)/i;

  /**
   * Identifies a <select> by the shape of its options rather than its label —
   * a list of month names is a month picker whatever the markup calls it.
   */
  function selectShape(el) {
    if (el.tagName !== "SELECT") return null;
    const opts = Array.from(el.options)
      .filter((o) => o.value !== "")
      .map((o) => cleanText(o.textContent));
    if (opts.length < 2) return null;
    if (opts.every((o) => MONTH_NAME.test(o))) return "month";
    if (opts.every((o) => /^(19|20)\d\d$/.test(o))) return "year";
    if (opts.every((o) => /^\d{1,2}$/.test(o)) && opts.length > 12) return "day";
    return null;
  }

  function mergeDateParts(groups) {
    const partOf = new Map(); // container element -> group indices

    groups.forEach((group, i) => {
      if (group.elements.length !== 1) return;
      const el = group.elements[0];
      const own = extractLabel(el).text;
      // Underscores are word characters, so "e1_start_month" needs splitting
      // before \bmonth\b can see it.
      const hint = `${own} ${el.name || ""} ${el.id || ""} ${el.getAttribute("data-automation-id") || ""}`
        .replace(/[_\-.]+/g, " ");
      const isPart =
        DATE_PART.test(own.trim()) ||
        !!selectShape(el) ||
        (/\b(month|day|year)\b/i.test(hint) && DATE_CONTEXT.test(hint));
      if (!isPart) return;

      // The nearest ancestor whose label looks like a date question.
      let node = el.parentElement;
      for (let hops = 0; hops < 4 && node; hops++) {
        if (/^(FORM|BODY|HTML)$/.test(node.tagName)) break;
        const context = containerLabel(node);
        if (context && DATE_CONTEXT.test(context)) {
          if (!partOf.has(node)) partOf.set(node, []);
          partOf.get(node).push(i);
          return;
        }
        node = node.parentElement;
      }
    });

    const merged = new Set();
    const out = [];
    partOf.forEach((indices, container) => {
      if (indices.length < 2) return;
      indices.forEach((i) => merged.add(i));
      out.push({
        kind: "date-parts",
        container,
        elements: indices.map((i) => groups[i].elements[0]),
        parts: indices.map((i) => extractLabel(groups[i].elements[0]).text),
        options: []
      });
    });

    if (!out.length) return groups;
    const kept = groups.filter((g, i) => !merged.has(i));
    // Merged groups were appended; restore document order so the review list
    // reads top-to-bottom like the page does.
    return kept.concat(out).sort((a, b) => {
      const pos = a.elements[0].compareDocumentPosition(b.elements[0]);
      if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
      if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
      return 0;
    });
  }

  /** Label describing a container: legend, aria-label, or its own leading text. */
  function containerLabel(node) {
    const legend = ownText(node.querySelector && node.querySelector("legend"));
    if (legend) return legend;
    const aria = cleanText(node.getAttribute("aria-label"));
    if (aria) return aria;
    const label = Array.from(node.querySelectorAll ? node.querySelectorAll("label") : [])
      .find((l) => !controlsIn(l).length);
    const labelText = ownText(label);
    if (labelText) return labelText;
    return precedingText(node);
  }

  /** For a radio group the per-option labels are noise — find the question. */
  function groupLabel(group) {
    if (group.kind === "date-parts") {
      return { text: containerLabel(group.container), source: "date-container" };
    }
    if (group.kind !== "radio") return extractLabel(group.elements[0]);
    const first = group.elements[0];
    const fieldset = first.closest && first.closest("fieldset");
    if (fieldset) {
      const legend = ownText(fieldset.querySelector("legend"));
      if (legend) return { text: legend, source: "legend" };
    }
    const grouped = first.closest && first.closest('[role="radiogroup"]');
    if (grouped) {
      const aria = cleanText(grouped.getAttribute("aria-label"));
      if (aria) return { text: aria, source: "radiogroup-aria-label" };
    }
    const preceding = precedingText(first.parentElement || first);
    if (preceding) return { text: preceding, source: "preceding-text" };
    return {
      text: cleanText(String(group.name || "").replace(/[_\-.]+/g, " ")),
      source: "name-attr"
    };
  }

  function optionsOf(group) {
    if (group.kind === "radio") return group.options;
    if (group.kind === "date-parts") return []; // month/year lists aren't choices
    const el = group.elements[0];
    if (el.tagName === "SELECT") {
      return Array.from(el.options)
        .filter((o) => o.value !== "")
        .map((o) => ({ value: o.value, label: cleanText(o.textContent) }));
    }
    if (group.kind === "checkbox" || group.kind === "radio-lone") {
      return [{ value: el.value || "on", label: "checked" }];
    }
    return [];
  }

  function guessProfileKey(el, label) {
    const auto = (el.getAttribute("autocomplete") || "").toLowerCase();
    if (AUTOCOMPLETE_MAP[auto]) return AUTOCOMPLETE_MAP[auto];
    if (el.type === "email") return "personal.email";
    if (el.type === "tel") return "personal.phone";
    // Test the raw label and the normalized token string, so decoration like
    // "$ name" or "Name *" still resolves.
    const norm = JAF.normalizeLabel(label).join(" ");
    const hit = PROFILE_PATTERNS.find(([re]) => re.test(label) || re.test(norm));
    return hit ? hit[1] : null;
  }

  function isFreeText(group, label) {
    const el = group.elements[0];
    if (el.tagName === "TEXTAREA") return true;
    const maxLength = parseInt(el.getAttribute("maxlength") || "0", 10);
    if (maxLength >= FREE_TEXT_MIN_LENGTH) return true;
    return /\bwhy\b|\bdescribe\b|\btell us\b|cover letter|in your own words/i.test(label);
  }

  // -------------------------------------------------------------------- values

  /** Reads one control — the building block for grouped values. */
  function readOne(el) {
    if (el.tagName === "SELECT") {
      const opt = el.options[el.selectedIndex];
      return opt && opt.value !== "" ? cleanText(opt.textContent) : "";
    }
    if (el.type === "checkbox" || el.type === "radio") return el.checked ? "Yes" : "";
    return el.value || "";
  }

  function readValue(group) {
    if (group.kind === "date-parts") {
      // Document order reads naturally ("September 2015"); the review screen
      // lets the user rewrite it before it's stored.
      return group.elements.map(readOne).filter(Boolean).join(" ").trim();
    }
    if (group.kind === "radio") {
      const checked = group.elements.find((el) => el.checked);
      if (!checked) return "";
      const opt = group.options.find((o) => o.value === checked.value);
      return (opt && opt.label) || checked.value;
    }
    const el = group.elements[0];
    if (group.kind === "checkbox" || group.kind === "radio-lone") {
      return el.checked ? "Yes" : "No";
    }
    if (el.tagName === "SELECT") {
      const opt = el.options[el.selectedIndex];
      return opt ? cleanText(opt.textContent) : "";
    }
    return el.value || "";
  }

  // --------------------------------------------------------------- write back

  /**
   * Frameworks like React track the last value they set, so a plain
   * `el.value = x` is silently reverted. Going through the prototype's native
   * setter updates the node in a way their onChange handlers accept.
   */
  function setNativeValue(el, value) {
    const proto = el instanceof HTMLSelectElement
      ? HTMLSelectElement.prototype
      : el instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, value);
  }

  /** Same reasoning as setNativeValue, for the checked property. */
  function setNativeChecked(el, checked) {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "checked").set;
    setter.call(el, !!checked);
  }

  /**
   * Masked date boxes often only commit on the events a real typist produces,
   * and some redraw from internal state on blur. Focusing first and blurring
   * last makes the write look like typing.
   */
  function notify(el, opts) {
    const full = opts && opts.full;
    if (full && el.focus) { try { el.focus({ preventScroll: true }); } catch (e) { el.focus(); } }
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    if (full) {
      el.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, key: "0" }));
      el.dispatchEvent(new Event("blur", { bubbles: false }));
      el.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
      if (el.blur) el.blur();
    }
  }

  const looseEqual = (a, b) =>
    String(a).trim().toLowerCase() === String(b).trim().toLowerCase();

  /** Picks the option that best represents `value`, or null. */
  function matchOption(options, value) {
    const exact = options.find((o) => looseEqual(o.label, value) || looseEqual(o.value, value));
    if (exact) return exact;
    const v = String(value).trim().toLowerCase();
    return options.find((o) => String(o.label).trim().toLowerCase().includes(v)) || null;
  }

  /**
   * Writes one value into one group. Only ever touches controls that came out
   * of scan(), which excludes every submit control by construction — nothing
   * here can submit a form.
   */
  function fillGroup(group, value) {
    const el = group.elements[0];
    if (isSubmitControl(el)) return { ok: false, reason: "submit-control" };

    if (group.kind === "date-parts") {
      return { ok: false, reason: "split-date-unsupported" };
    }

    if (group.kind === "radio") {
      const opt = matchOption(group.options, value);
      if (!opt) return { ok: false, reason: "no-matching-option" };
      const target = group.elements.find((e) => e.value === opt.value);
      if (!target) return { ok: false, reason: "no-matching-option" };
      setNativeChecked(target, true);
      notify(target);
      if (!target.checked) return { ok: false, reason: "rejected (did not stay checked)" };
      return { ok: true };
    }

    if (group.kind === "checkbox" || group.kind === "radio-lone") {
      const want = /^(yes|true|on|checked)$/i.test(String(value).trim());
      setNativeChecked(el, want);
      notify(el);
      if (el.checked !== want) return { ok: false, reason: "rejected (state reverted)" };
      return { ok: true };
    }

    if (el.tagName === "SELECT") {
      const options = Array.from(el.options).map((o) => ({
        value: o.value, label: cleanText(o.textContent)
      }));
      const opt = matchOption(options.filter((o) => o.value !== ""), value);
      if (!opt) return { ok: false, reason: "no-matching-option" };

      // A framework-controlled <select> reverts a plain assignment the same way
      // a controlled <input> does, and then the form validates as empty. Go
      // through the native setter, and verify it stuck rather than assuming.
      setNativeValue(el, opt.value);
      notify(el, { full: true });
      if (String(el.value) !== String(opt.value)) {
        return { ok: false, reason: `rejected (kept "${el.value}")` };
      }
      return { ok: true };
    }

    // `disabled` means the page will not submit this control, so writing to it
    // would only mislead. `readonly` is different: date and masked inputs set it
    // to force you through their picker, while still submitting whatever value
    // they hold. Lift it for the write and put it back.
    if (el.disabled) return { ok: false, reason: "disabled" };

    const wasReadOnly = el.readOnly;
    if (wasReadOnly) el.readOnly = false;
    try {
      setNativeValue(el, String(value));
      notify(el, { full: true });
    } finally {
      if (wasReadOnly) el.readOnly = true;
    }

    // Widgets that reject a value silently revert it; report that honestly
    // rather than claiming a fill that did not take.
    if (String(el.value) !== String(value)) {
      return { ok: false, reason: `rejected (field kept "${el.value}")`, wasReadOnly };
    }
    return { ok: true, wasReadOnly };
  }

  const HIGHLIGHT_STYLE_ID = "jaf-highlight-style";

  /**
   * Marks fields on the page that want the user's own eyes: a drafted answer to
   * a vague question ("why us"), or a question nothing stored could answer.
   * The pulse stops as soon as the field is touched, so it guides without
   * nagging, and only ever changes outline colour — never layout, never value.
   */
  function ensureHighlightStyle() {
    if (document.getElementById(HIGHLIGHT_STYLE_ID)) return;
    const style = document.createElement("style");
    style.id = HIGHLIGHT_STYLE_ID;
    style.textContent = `
      @keyframes jaf-pulse {
        0%,100% { outline-color: rgba(79,70,229,.95); box-shadow: 0 0 0 3px rgba(79,70,229,.18); }
        50%     { outline-color: rgba(79,70,229,.25); box-shadow: 0 0 0 6px rgba(79,70,229,.05); }
      }
      .jaf-needs-you {
        outline: 2px solid rgba(79,70,229,.95) !important;
        outline-offset: 1px;
        border-radius: 3px;
        animation: jaf-pulse 1.4s ease-in-out infinite;
      }
      .jaf-needs-you-review { outline-color: rgba(217,119,6,.95) !important; }
      @media (prefers-reduced-motion: reduce) { .jaf-needs-you { animation: none; } }
    `;
    (document.head || document.documentElement).append(style);
  }

  function clearHighlights() {
    deepQuery(".jaf-needs-you").forEach((el) =>
      el.classList.remove("jaf-needs-you", "jaf-needs-you-review"));
  }

  /** @param {{index:number,tone:string}[]} marks */
  function highlightFields(marks) {
    ensureHighlightStyle();
    clearHighlights();
    let marked = 0;
    (marks || []).forEach((mark) => {
      const field = lastScan.find((f) => f.index === mark.index);
      if (!field) return;
      const el = field._group.elements[0];
      if (!el) return;
      el.classList.add("jaf-needs-you");
      if (mark.tone === "review") el.classList.add("jaf-needs-you-review");
      const stop = () => {
        el.classList.remove("jaf-needs-you", "jaf-needs-you-review");
        el.removeEventListener("focus", stop);
        el.removeEventListener("input", stop);
      };
      el.addEventListener("focus", stop);
      el.addEventListener("input", stop);
      marked++;
    });

    // Bring the first one into view so the user knows where to look.
    const first = deepQuery(".jaf-needs-you")[0];
    if (first && first.scrollIntoView) {
      try { first.scrollIntoView({ behavior: "smooth", block: "center" }); } catch (e) { /* older engines */ }
    }
    return marked;
  }

  /** @param {{index:number,value:string}[]} items */
  function applyFill(items) {
    if (!lastScan.length) scan();
    const byIndex = new Map(lastScan.map((f) => [f.index, f]));
    return (items || []).map((item) => {
      const field = byIndex.get(item.index);
      if (!field) return { index: item.index, ok: false, reason: "field-gone" };
      let result;
      try {
        result = fillGroup(field._group, item.value);
      } catch (e) {
        result = { ok: false, reason: String(e && e.message) };
      }
      return Object.assign({ index: item.index, label: field.label }, result);
    });
  }

  // ---------------------------------------------------------------- scan entry

  let lastScan = [];

  function scan() {
    const groups = buildGroups(collectFields());

    lastScan = groups.map((group, i) => {
      const label = groupLabel(group);
      const el = group.elements[0];
      const options = optionsOf(group);
      return {
        index: i,
        kind: group.kind,
        label: label.text,
        labelSource: label.source,
        name: el.name || "",
        id: el.id || "",
        selector: cssPath(el),
        required: el.required || el.getAttribute("aria-required") === "true",
        // Format hints. A date widget accepts only its own encoding — an
        // <input type="month"> shows "MM/YYYY" but stores "2025-09" — so the
        // filler has to know the shape before it can offer a stored date.
        inputType: el.tagName === "INPUT" ? (el.type || "text") : "",
        placeholder: el.getAttribute("placeholder") || "",
        pattern: el.getAttribute("pattern") || "",
        hint: formatHint(el),
        maxLength: el.maxLength > 0 ? el.maxLength : null,
        options,
        hasFixedOptions: options.length > 0 && group.kind !== "text",
        isFreeText: isFreeText(group, label.text),
        profileKey: guessProfileKey(el, label.text),
        contextKey: guessContextKey(label.text),
        sensitive: JAF.looksSensitive(label.text),
        _group: group
      };
    });

    resolveBlocks(lastScan);
    return lastScan;
  }

  function guessContextKey(label) {
    const norm = JAF.normalizeLabel(label).join(" ");
    const hit = CONTEXT_PATTERNS.find(([re]) => re.test(label) || re.test(norm));
    return hit ? hit[1] : null;
  }

  /**
   * A form can repeat "Education" or "Employment" blocks. Each block is the
   * nearest ancestor holding two or more of that section's fields — so two
   * degrees land as two entries instead of overwriting each other.
   *
   * Resolving the block also settles the ambiguous labels: "Start Date" becomes
   * education.startYear or experience.startDate depending on its neighbours.
   */
  function resolveBlocks(fields) {
    const anchored = fields.filter((f) => f.profileKey && !f.profileKey.startsWith("personal."));
    if (!anchored.length) return;

    // Nearest ancestor containing 2+ fields of the same section.
    const blockOf = (field) => {
      const section = field.profileKey.split(".")[0];
      let node = field._group.elements[0].parentElement;
      for (let hops = 0; hops < 8 && node; hops++) {
        const inside = anchored.filter(
          (f) => f.profileKey.startsWith(section) && node.contains(f._group.elements[0])
        );
        if (inside.length >= 2) return node;
        if (/^(FORM|BODY|HTML)$/.test(node.tagName)) return node;
        node = node.parentElement;
      }
      return null;
    };

    const blocks = [];
    anchored.forEach((field) => {
      const node = blockOf(field);
      if (!node) return;
      const section = field.profileKey.split(".")[0];
      let block = blocks.find((b) => b.node === node && b.section === section);
      if (!block) {
        block = { node, section, index: blocks.filter((b) => b.section === section).length };
        blocks.push(block);
      }
      field.sectionKey = `${section}#${block.index}`;
    });

    // Ambiguous fields inherit the section of the block they sit inside.
    // "Location" reads as a personal address on its own, but inside a job block
    // it belongs to that job — block context outranks the generic guess.
    const overridable = (f) =>
      f.contextKey === "location" && f.profileKey === "personal.address";

    fields.forEach((field) => {
      if (!field.contextKey) return;
      if (field.profileKey && !overridable(field)) return;
      const el = field._group.elements[0];
      const block = blocks.find((b) => b.node.contains(el));
      if (!block) return;
      const key = DATE_KEYS[block.section][field.contextKey] || field.contextKey;
      field.profileKey = `${block.section}.${key}`;
      field.sectionKey = `${block.section}#${block.index}`;
    });
  }

  /** Schema uses years for education and full dates for experience. */
  const DATE_KEYS = {
    education: { start: "startYear", end: "endYear", description: "description", location: "location" },
    experience: { start: "startDate", end: "endDate", description: "description", location: "location" }
  };

  /** Public shape — no DOM references, safe to send over the message channel. */
  function serialize(field) {
    const out = Object.assign({}, field);
    delete out._group;
    return out;
  }

  /** Re-reads current values for everything the last scan found. */
  function capture() {
    if (!lastScan.length) scan();
    return lastScan.map((f) =>
      Object.assign(serialize(f), { value: readValue(f._group) })
    );
  }

  async function classifyAgainstStore(fields) {
    const store = await JAF.getStore();
    return fields.map((f) => {
      const match = JAF.findQuestionMatch(f.label, store.questionBank);
      let status = "new";
      if (match) status = "known-question";
      else if (f.profileKey) {
        const key = f.profileKey.split(".")[1];
        if (store.profile.personal[key]) status = "known-profile";
        else status = "profile-candidate";
      } else if (f.isFreeText) status = "free-text";
      return Object.assign(serialize(f), {
        status,
        matchedQuestionId: match ? match.entry.id : null,
        matchScore: match ? Number(match.score.toFixed(2)) : null
      });
    });
  }

  function signatureOf(fields) {
    return JAF.formSignature(fields.map((f) => f.label));
  }

  // ------------------------------------------------------------------- debug

  /**
   * One line per field, values excluded on purpose — the log is for pasting
   * into a bug report.
   */
  function debugShape(fields) {
    return fields.map((f) => ({
      label: f.label,
      via: f.labelSource,
      kind: f.kind,
      status: f.status,
      // Format hints decide whether a stored date can be re-encoded for this
      // control, so a date that won't fill is diagnosed from these three.
      inputType: f.inputType || null,
      placeholder: f.placeholder || null,
      hint: f.hint || null,
      readOnly: f._group && f._group.elements[0] ? !!f._group.elements[0].readOnly : null,
      profileKey: f.profileKey || null,
      sectionKey: f.sectionKey || null,
      options: f.options.length,
      free: f.isFreeText,
      sensitive: f.sensitive
    }));
  }

  /** Capture summary for the debug log: value LENGTHS only, never values. */
  function capturePayload(fields) {
    return {
      formSignature: signatureOf(fields),
      fieldCount: fields.length,
      filled: fields.filter((f) => String(f.value || "").trim() !== "").length,
      shape: fields.map((f) => ({
        label: f.label,
        kind: f.kind,
        profileKey: f.profileKey || null,
        sectionKey: f.sectionKey || null,
        valueLength: String(f.value || "").length
      }))
    };
  }

  async function logScan() {
    const fields = scan();
    if (fields.length < 3) return; // not a form page worth reporting
    const classified = await classifyAgainstStore(fields);

    JAF.debugLog("content", "scan", {
      url: location.href,
      frame: window.top === window ? "top" : "iframe",
      formSignature: signatureOf(classified),
      fieldCount: classified.length,
      fields: debugShape(classified)
    });

    const settings = await JAF.getSettings();
    if (!settings.debug) return; // console stays quiet unless asked for

    const label = `[JAF] ${fields.length} field(s) — ${location.host} — sig ${signatureOf(fields)}`;
    console.groupCollapsed(label);
    console.table(
      classified.map((f) => ({
        label: f.label,
        via: f.labelSource,
        kind: f.kind,
        status: f.status,
        options: f.options.map((o) => o.label).join(" | ").slice(0, 60),
        free: f.isFreeText,
        sensitive: f.sensitive,
        selector: f.selector
      }))
    );
    console.log("[JAF] full scan:", classified);
    console.groupEnd();
  }

  // -------------------------------------------------------------- message port

  // ------------------------------------------------------------- autofill
  //
  // One keypress (Ctrl+Shift+F) or one context-menu click fills the form. There
  // is no confirmation step: everything the knowledge base can answer is
  // written straight in. What is left over — a value the widget refused, a
  // drafted answer worth reading, a question nothing has answered yet — goes
  // into a panel on the page, next to the fields it belongs to.

  function ask(message) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(message, (res) => {
          void chrome.runtime.lastError;
          resolve(res || { ok: false, error: "no reply from the extension worker" });
        });
      } catch (e) {
        resolve({ ok: false, error: String(e.message || e) });
      }
    });
  }

  const blockOf = (field) => {
    const m = /#(\d+)$/.exec(field.sectionKey || "");
    return m ? Number(m[1]) : -1;
  };

  /** The scan, in the shape the knowledge service expects. */
  function serviceFields(fields) {
    return fields.map((f) => {
      const el = f._group.elements[0];
      return {
        id: String(f.index),
        label: f.label || "",
        inputType: f.inputType || "",
        tag: f.kind === "select" ? "select"
          : el.tagName === "TEXTAREA" ? "textarea" : "input",
        required: !!f.required,
        placeholder: f.placeholder || "",
        pattern: f.pattern || "",
        hint: f.hint || "",
        maxLength: f.maxLength || 0,
        options: (f.options || []).map((o) => o.label || o.value || ""),
        readOnly: !!el.readOnly,
        blockIndex: blockOf(f),
        value: String(readValue(f._group) || "")
      };
    });
  }

  /**
   * What we can decide without the service running. Deliberately small: profile
   * fields and questions already answered word-for-word. It exists so a dead
   * service costs you the clever half of the fill, not the whole thing.
   */
  async function localPlan(fields) {
    const store = await JAF.getStore();
    return fields.map((f) => {
      const out = { id: String(f.index), label: f.label, action: "skip", value: "", source: "" };
      if (f.sensitive) return out;

      const match = JAF.findQuestionMatch(f.label, store.questionBank);
      if (match) {
        out.value = match.entry.answer;
        out.source = "saved answer (local)";
      } else if (f.profileKey) {
        const [section, key] = f.profileKey.split(".");
        if (section === "personal") {
          out.value = (store.profile.personal || {})[key] || "";
        } else if (section === "skills") {
          out.value = (store.profile.skills || []).join(", ");
        } else {
          const entry = (store.profile[section] || [])[Math.max(blockOf(f), 0)];
          out.value = (entry && entry[key]) || "";
        }
        if (out.value) out.source = `profile.${f.profileKey} (local)`;
      }

      if (!out.value) return out;
      const hints = {
        inputType: f.inputType, placeholder: f.placeholder,
        pattern: f.pattern, hint: f.hint, label: f.label
      };
      if (JAF.isDateField(hints)) {
        out.value = JAF.formatDateForField(out.value, hints);
        if (!out.value) return out;
      }
      out.action = "fill";
      return out;
    });
  }

  /**
   * Is this field marked required? The attribute is the reliable signal, but
   * plenty of sites (LinkedIn Easy Apply among them) mark required fields with
   * an asterisk in the label text and nothing else — and those are exactly the
   * ones that must not be skipped quietly.
   */
  function looksRequired(field) {
    if (field.required) return true;
    return /\*\s*$|\brequired\b|\(required\)/i.test(field.label || "");
  }

  /**
   * Fields worth putting in the panel when nothing could answer them.
   *
   * The bar is deliberately low. Saying "nothing else needs you" while leaving
   * a required dropdown blank is the worst thing this can do — you submit an
   * incomplete application believing it was handled. A field only stays out of
   * the panel when it already has a value, is a demographic question we refuse
   * to touch, or has no label to show you.
   */
  function worthAsking(field) {
    if (field.sensitive) return false;
    if (!String(field.label || "").trim()) return false;
    if (String(readValue(field._group) || "").trim()) return false;
    if (looksRequired(field)) return true;
    if (field.isFreeText || JAF.isOpinionQuestion(field.label)) return true;
    // A dropdown or radio group with real options is a question being asked.
    return (field.options || []).length > 0;
  }

  async function autofill() {
    ensureHud("Job Application Learner");
    const started = Date.now();

    const scanning = hudLine("scanning the page…", "work");
    const fields = scan();
    if (!fields.length) {
      scanning.set("no fillable fields on this page", "bad");
      showPanel([], { message: "No fillable fields found on this page." });
      return { filled: 0, asked: 0 };
    }
    const answered = fields.filter((f) => String(readValue(f._group) || "").trim()).length;
    scanning.set(
      `${fields.length} field${fields.length === 1 ? "" : "s"} found` +
      (answered ? ` (${answered} already answered)` : ""),
      "ok"
    );

    const payload = {
      fields: serviceFields(fields),
      site: location.hostname,
      url: location.href,
      signature: signatureOf(fields.map(serialize)),
      jobDescription: pageJobText()
    };

    // Upload fields are not part of the plan — they take a file, not a value —
    // so they are handled alongside it rather than waiting for it.
    const uploads = resumeInputs();
    const resumeLine = uploads.length ? hudLine("resume upload field found", "work") : null;
    const resumeJob = uploads.length ? attachResume(resumeLine) : Promise.resolve(null);

    const asking = hudLine("asking the knowledge base…", "work");
    const res = await ask({ type: "JAF_SERVICE_PLAN", payload });
    let decisions;
    let banner = "";
    if (res && res.ok && res.data && Array.isArray(res.data.decisions)) {
      decisions = res.data.decisions;
      asking.set(`knowledge base answered in ${((Date.now() - started) / 1000).toFixed(1)}s`, "ok");
      // Each note is one step the service took — routing, dropdowns, drafting.
      (res.data.notes || []).forEach((note) => hudLine(note, /unavailable|failed/i.test(note) ? "bad" : "info"));
      if ((res.data.notes || []).length) banner = res.data.notes.join(" · ");
    } else {
      const why = (res && res.error) || "no reply";
      asking.set(`knowledge service unreachable`, "bad");
      hudLine(why, "bad");
      hudLine("falling back to local storage", "info");
      decisions = await localPlan(fields);
      banner = `Knowledge service unavailable (${why}). ` +
               "Filled from local storage only — no AI routing or drafting.";
    }

    const entries = [];
    let filled = 0;

    decisions.forEach((d) => {
      const field = fields[Number(d.id)];
      if (!field) return;

      if ((d.action === "fill" || d.action === "review") && d.value) {
        const result = fillGroup(field._group, d.value);
        if (result.ok) {
          filled++;
          // A drafted answer is written, but it is still the model's words.
          if (d.generated || d.action === "review") {
            entries.push({ field, decision: d, tone: "review" });
          }
          return;
        }
        // The widget refused the write. We still know the answer, so show it
        // rather than leaving an empty box: this is the date-picker case.
        entries.push({ field, decision: d, tone: "value", why: result.reason });
        return;
      }

      if (d.action === "highlight_with_value" && d.value) {
        entries.push({ field, decision: d, tone: "value", why: d.reason });
        return;
      }

      if (worthAsking(field)) {
        entries.push({ field, decision: d, tone: "ask" });
      }
    });

    // Where the written values actually came from. "from your knowledge base"
    // means stored text, substituted here; the model never handled it.
    const resume = await resumeJob;

    const fromStore = decisions.filter((d) => d.pointer && !d.generated).length;
    const fromModel = decisions.filter((d) => d.generated).length;
    const rejected = entries.filter((e) => e.why).length;

    hudLine(`filled ${filled} field${filled === 1 ? "" : "s"}`, filled ? "ok" : "info");
    if (fromStore) hudLine(`${fromStore} from your knowledge base`, "info");
    if (fromModel) hudLine(`${fromModel} written by the model — check them`, "info");
    if (rejected) hudLine(`${rejected} refused by the page — shown to type in`, "info");
    const asks = entries.filter((e) => e.tone === "ask").length;
    hudLine(
      asks ? `${asks} need${asks === 1 ? "s" : ""} your answer`
           : "nothing else needs you",
      asks ? "bad" : "ok"
    );

    highlightFields(entries.map((e) => ({
      index: e.field.index,
      tone: e.tone === "review" ? "review" : "needs-you"
    })));

    showPanel(entries, { message: banner, filled });

    JAF.debugLog("content", "autofill", {
      fields: fields.length,
      resumeAttached: resume ? resume.attached : 0,
      filled,
      asked: entries.filter((e) => e.tone === "ask").length,
      shown: entries.length,
      service: !!(res && res.ok)
    });
    return { filled, asked: entries.length };
  }

  // ------------------------------------------------------- resume attachment
  //
  // A file input cannot be given a path — that would let any page read any file
  // off your disk. It *can* be given a File object the extension built itself,
  // through a DataTransfer, which is the same mechanism a drag-and-drop uses.
  // The bytes come from the knowledge service, so nothing is read from disk by
  // the page or by us.

  const RESUME_FIELD =
    /\b(resume|c\.?v\.?|curriculum vitae)\b/i;

  /** Upload controls on the page that look like they want a resume. */
  function resumeInputs() {
    return deepQuery('input[type="file"]').filter((el) => {
      if (el.disabled || !el.isConnected) return false;
      if (el.files && el.files.length) return false;      // you already chose one
      const context = [
        el.name, el.id, el.getAttribute("aria-label"), el.accept,
        extractLabel(el).text,
        el.closest("label") ? el.closest("label").textContent : "",
        el.parentElement ? el.parentElement.textContent : ""
      ].join(" ");
      // An unlabelled lone file input on an application form is a resume box
      // often enough to try; a labelled one has to actually say so.
      return RESUME_FIELD.test(context) || !/cover letter|portfolio|photo|id\b/i.test(context);
    });
  }

  function base64ToFile(b64, filename, mime) {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new File([bytes], filename, { type: mime || "application/octet-stream" });
  }

  /**
   * Attaches the stored resume to every empty upload field.
   * Returns {attached, total, error} — never throws.
   */
  async function attachResume(line) {
    const inputs = resumeInputs();
    if (!inputs.length) return { attached: 0, total: 0 };

    if (line) line.set("fetching your resume file…", "work");
    const res = await ask({ type: "JAF_SERVICE_RESUME_FILE" });
    if (!res || !res.ok || !res.data || !res.data.base64) {
      const why = (res && res.error) || "no resume file stored";
      if (line) line.set(`resume not attached (${/404/.test(why) ? "none stored" : why})`, "info");
      return { attached: 0, total: inputs.length, error: why };
    }

    const { filename, mime, base64 } = res.data;
    let attached = 0;
    inputs.forEach((el) => {
      try {
        const dt = new DataTransfer();
        dt.items.add(base64ToFile(base64, filename, mime));
        el.files = dt.files;
        // Frameworks watch `change`; drop-zone widgets watch `drop`.
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        if (el.files.length) attached++;
      } catch (e) {
        /* a widget that refuses assignment is reported by the count below */
      }
    });

    if (line) {
      line.set(
        attached ? `attached ${filename} to ${attached} field${attached === 1 ? "" : "s"}`
                 : "the page refused the file — attach it yourself",
        attached ? "ok" : "bad"
      );
    }
    return { attached, total: inputs.length, filename };
  }

  /** Job description text, used to ground a drafted answer. Trimmed hard. */
  function pageJobText() {
    const node = document.querySelector(
      '[class*="job-description" i], [class*="jobDescription" i], ' +
      '[id*="job-description" i], article, main'
    );
    return cleanText((node || document.body).innerText || "").slice(0, 4000);
  }

  // ------------------------------------------------------------------ hud
  //
  // A small translucent readout that appears the moment you press the shortcut
  // and narrates what is happening: how many fields were found, whether the
  // knowledge service answered, which came from storage and which the model
  // wrote. A fill can take a few seconds against a rate-limited free model, and
  // without this the page just sits there looking broken.
  //
  // Drag it by the title bar; it stays where you put it for the session.

  const HUD_ID = "jaf-hud-host";
  let hudBody = null;
  let hudPosition = null;   // remembered across fills on this page

  const HUD_CSS = `
    :host { all: initial; }
    .hud {
      position: fixed; top: 16px; right: 16px; width: 290px;
      z-index: 2147483647;
      font: 12px/1.5 ui-monospace, "Cascadia Mono", Consolas, monospace;
      color: #f4f4f5; background: rgba(17, 17, 19, .88);
      -webkit-backdrop-filter: blur(6px); backdrop-filter: blur(6px);
      border: 1px solid rgba(255,255,255,.14); border-radius: 8px;
      box-shadow: 0 10px 30px rgba(0,0,0,.4);
      user-select: none;
    }
    .bar {
      display: flex; align-items: center; gap: 8px; padding: 7px 10px;
      border-bottom: 1px solid rgba(255,255,255,.1);
      cursor: grab; font-weight: 600; letter-spacing: .02em;
    }
    .bar.dragging { cursor: grabbing; }
    .bar .grow { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .bar button {
      border: 0; background: none; color: #a1a1aa; font: inherit; font-size: 14px;
      line-height: 1; cursor: pointer; padding: 0 2px;
    }
    .bar button:hover { color: #fff; }
    .lines { padding: 6px 10px 9px; max-height: 45vh; overflow-y: auto; }
    .line { display: flex; gap: 7px; padding: 1px 0; align-items: baseline; }
    .dot { flex: none; width: 6px; height: 6px; border-radius: 50%; margin-top: 5px; }
    .dot.work { background: #fbbf24; animation: jaf-blink 1s ease-in-out infinite; }
    .dot.ok   { background: #34d399; }
    .dot.bad  { background: #f87171; }
    .dot.info { background: #52525b; }
    .txt { flex: 1; word-break: break-word; }
    .line.info .txt { color: #a1a1aa; }
    .line.bad  .txt { color: #fca5a5; }
    @keyframes jaf-blink { 0%,100% { opacity: 1 } 50% { opacity: .25 } }
  `;

  function closeHud() {
    const host = document.getElementById(HUD_ID);
    if (host) host.remove();
    hudBody = null;
  }

  function makeDraggable(bar, wrap) {
    let startX = 0, startY = 0, originX = 0, originY = 0;

    const onMove = (e) => {
      // Clamped to the viewport, so it can never be dragged out of reach.
      const w = wrap.offsetWidth, h = wrap.offsetHeight;
      const x = Math.min(Math.max(0, originX + e.clientX - startX), innerWidth - w);
      const y = Math.min(Math.max(0, originY + e.clientY - startY), innerHeight - h);
      wrap.style.left = `${x}px`;
      wrap.style.top = `${y}px`;
      wrap.style.right = "auto";
      hudPosition = { x, y };
    };

    const onUp = () => {
      bar.classList.remove("dragging");
      removeEventListener("pointermove", onMove);
      removeEventListener("pointerup", onUp);
    };

    bar.addEventListener("pointerdown", (e) => {
      if (e.target.tagName === "BUTTON") return;
      const rect = wrap.getBoundingClientRect();
      startX = e.clientX; startY = e.clientY;
      originX = rect.left; originY = rect.top;
      bar.classList.add("dragging");
      addEventListener("pointermove", onMove);
      addEventListener("pointerup", onUp);
      e.preventDefault();
    });
  }

  function ensureHud(title) {
    closeHud();

    const host = document.createElement("div");
    host.id = HUD_ID;
    const root = host.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = HUD_CSS;
    root.appendChild(style);

    const wrap = document.createElement("div");
    wrap.className = "hud";
    if (hudPosition) {
      wrap.style.left = `${hudPosition.x}px`;
      wrap.style.top = `${hudPosition.y}px`;
      wrap.style.right = "auto";
    }

    const bar = document.createElement("div");
    bar.className = "bar";
    const label = document.createElement("span");
    label.className = "grow";
    label.textContent = title;
    const close = document.createElement("button");
    close.textContent = "×";
    close.title = "Close";
    close.addEventListener("click", closeHud);
    bar.append(label, close);

    hudBody = document.createElement("div");
    hudBody.className = "lines";

    wrap.append(bar, hudBody);
    root.appendChild(wrap);
    document.documentElement.appendChild(host);
    makeDraggable(bar, wrap);
    return hudBody;
  }

  /**
   * Adds a line. Returns a handle so a step that is still running can be
   * rewritten in place when it finishes, rather than repeating itself.
   */
  function hudLine(text, tone) {
    if (!hudBody) return null;
    const line = document.createElement("div");
    line.className = `line ${tone || "info"}`;
    const dot = document.createElement("span");
    dot.className = `dot ${tone || "info"}`;
    const txt = document.createElement("span");
    txt.className = "txt";
    txt.textContent = text;
    line.append(dot, txt);
    hudBody.appendChild(line);
    hudBody.scrollTop = hudBody.scrollHeight;

    return {
      set(newText, newTone) {
        txt.textContent = newText;
        line.className = `line ${newTone || "info"}`;
        dot.className = `dot ${newTone || "info"}`;
        hudBody.scrollTop = hudBody.scrollHeight;
      }
    };
  }

  // ---------------------------------------------------------------- panel
  //
  // Lives in a shadow root so the page's stylesheet cannot reach it and ours
  // cannot leak out. It never covers the form: bottom-right, fixed, scrollable,
  // and dismissable.

  const PANEL_ID = "jaf-panel-host";

  // Note: this must NOT close the readout — showPanel() calls it to replace an
  // earlier panel, and the readout is built before the panel exists.
  function closePanel() {
    const host = document.getElementById(PANEL_ID);
    if (host) host.remove();
    clearHighlights();
  }

  const PANEL_CSS = `
    :host { all: initial; }
    .wrap {
      position: fixed; right: 16px; bottom: 16px; width: 380px; max-height: 70vh;
      display: flex; flex-direction: column; z-index: 2147483647;
      font: 13px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
      color: #111; background: #fff; border: 1px solid #d5d7db; border-radius: 10px;
      box-shadow: 0 12px 32px rgba(0,0,0,.22);
    }
    header {
      display: flex; align-items: center; gap: 8px; padding: 10px 12px;
      border-bottom: 1px solid #eceef1; font-weight: 600;
    }
    header .grow { flex: 1; }
    header button {
      border: 0; background: none; font-size: 18px; line-height: 1; cursor: pointer;
      color: #6b7280; padding: 0 4px;
    }
    .note { padding: 8px 12px; background: #f7f8fa; color: #4b5563; font-size: 12px; }
    .list { overflow-y: auto; padding: 4px 0; }
    .item { padding: 10px 12px; border-top: 1px solid #f0f1f3; }
    .item:first-child { border-top: 0; }
    .label { font-weight: 600; margin-bottom: 2px; }
    .why { color: #6b7280; font-size: 12px; margin-bottom: 6px; }
    .tag {
      display: inline-block; font-size: 11px; font-weight: 600; padding: 1px 6px;
      border-radius: 999px; margin-left: 6px; vertical-align: 1px;
    }
    .tag.value  { background: #fff4e5; color: #92400e; }
    .tag.review { background: #eef2ff; color: #3730a3; }
    .tag.ask    { background: #fee2e2; color: #991b1b; }
    textarea, input.answer, select {
      width: 100%; box-sizing: border-box; font: inherit; padding: 6px 8px;
      border: 1px solid #d5d7db; border-radius: 6px; resize: vertical;
      background: #fff; color: #111;
    }
    .req { color: #b42318; margin-left: 3px; }
    .row { display: flex; gap: 6px; margin-top: 6px; flex-wrap: wrap; }
    .row button {
      font: inherit; padding: 4px 10px; border-radius: 6px; cursor: pointer;
      border: 1px solid #d5d7db; background: #fff;
    }
    .row button.primary { background: #111827; color: #fff; border-color: #111827; }
    .row button:disabled { opacity: .5; cursor: default; }
    .status { font-size: 12px; color: #157347; margin-top: 4px; min-height: 1em; }
    .status.bad { color: #b42318; }
    .empty { padding: 14px 12px; color: #4b5563; }
  `;

  function showPanel(entries, opts) {
    closePanel();
    const o = opts || {};

    const host = document.createElement("div");
    host.id = PANEL_ID;
    const root = host.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = PANEL_CSS;
    root.appendChild(style);

    const wrap = document.createElement("div");
    wrap.className = "wrap";
    root.appendChild(wrap);

    const header = document.createElement("header");
    const title = document.createElement("span");
    title.className = "grow";
    title.textContent = entries.length
      ? `Filled ${o.filled || 0} · ${entries.length} need you`
      : `Filled ${o.filled || 0} field${o.filled === 1 ? "" : "s"}`;
    const close = document.createElement("button");
    close.textContent = "×";
    close.title = "Close";
    close.addEventListener("click", () => { closePanel(); closeHud(); });
    header.append(title, close);
    wrap.appendChild(header);

    if (o.message) {
      const note = document.createElement("div");
      note.className = "note";
      note.textContent = o.message;
      wrap.appendChild(note);
    }

    if (entries.length) {
      // Nothing below is written to the page until the button is pressed.
      // Without saying so, an entry showing a value looks like it was applied.
      const hint = document.createElement("div");
      hint.className = "note";
      hint.textContent = "Nothing below is on the form yet — press Save & fill "
                       + "on each one you want.";
      wrap.appendChild(hint);
    }

    const list = document.createElement("div");
    list.className = "list";
    wrap.appendChild(list);

    if (!entries.length) {
      const done = document.createElement("div");
      done.className = "empty";
      // Careful with this claim: it is only true of the fields that were
      // scanned, and a wizard shows one step at a time.
      done.textContent = o.filled
        ? "Nothing else on this step needs you. Check it before continuing."
        : "Nothing here could be filled or asked about.";
      list.appendChild(done);
    }

    entries.forEach((entry) => list.appendChild(panelItem(entry)));
    document.documentElement.appendChild(host);

    const first = entries[0];
    if (first) {
      try {
        first.field._group.elements[0].scrollIntoView({ block: "center", behavior: "smooth" });
      } catch (e) { /* detached or in a scroll container that refuses it */ }
    }
  }

  const TAG_TEXT = {
    value: "type this in",
    review: "drafted — read it",
    ask: "needs your answer"
  };

  function panelItem(entry) {
    const { field, decision, tone } = entry;
    const item = document.createElement("div");
    item.className = "item";

    const label = document.createElement("div");
    label.className = "label";
    label.textContent = (field.label || "(unlabelled field)").replace(/\s*\*\s*$/, "");
    if (looksRequired(field)) {
      const star = document.createElement("span");
      star.className = "req";
      star.textContent = "*";
      star.title = "Required";
      label.appendChild(star);
    }
    const tag = document.createElement("span");
    tag.className = `tag ${tone}`;
    tag.textContent = TAG_TEXT[tone];
    label.appendChild(tag);
    item.appendChild(label);

    const why = document.createElement("div");
    why.className = "why";
    why.textContent = entry.why || decision.reason ||
      (tone === "ask" ? "Nothing stored answers this yet." : "");
    if (why.textContent) item.appendChild(why);

    // A question with a fixed list gets that list, not a free-text box — typing
    // "Yes" into a control whose option reads "Yes, I have" fills nothing.
    const choices = field.options || [];
    let input;
    if (choices.length) {
      input = document.createElement("select");
      const blank = document.createElement("option");
      blank.value = "";
      blank.textContent = "— choose —";
      input.appendChild(blank);
      choices.forEach((opt) => {
        const o = document.createElement("option");
        o.value = opt.label || opt.value || "";
        o.textContent = o.value;
        input.appendChild(o);
      });
      input.value = decision.value || "";
    } else {
      const long = field.isFreeText || tone === "review";
      input = document.createElement(long ? "textarea" : "input");
      if (long) input.rows = 3;
      else input.className = "answer";
      input.value = decision.value || "";
      input.placeholder = tone === "ask" ? "Your answer — saved for next time" : "";
    }
    item.appendChild(input);

    const row = document.createElement("div");
    row.className = "row";
    const status = document.createElement("div");
    status.className = "status";

    const insert = document.createElement("button");
    insert.className = "primary";
    insert.textContent = tone === "ask" ? "Save & fill" : "Insert";
    insert.addEventListener("click", async () => {
      const value = input.value.trim();
      if (!value) {
        status.className = "status bad";
        status.textContent = "Type something first.";
        return;
      }

      const result = fillGroup(field._group, value);
      if (!result.ok) {
        status.className = "status bad";
        status.textContent = `Could not write it (${result.reason}). Copy it in yourself.`;
      } else {
        status.className = "status";
        status.textContent = "Filled.";
        field._group.elements[0].classList.remove("jaf-needs-you", "jaf-needs-you-review");
      }

      // Whatever the field did, the answer is worth keeping — that is how the
      // next site gets it without asking.
      if (tone === "ask" || tone === "review") {
        insert.disabled = true;
        const saved = await ask({
          type: "JAF_SERVICE_ANSWER",
          payload: { question: field.label, answer: value, site: location.hostname }
        });
        insert.disabled = false;
        if (saved && saved.ok) {
          status.textContent += " Saved to the knowledge base.";
        } else {
          status.className = "status bad";
          status.textContent += ` Not saved (${(saved && saved.error) || "no reply"}).`;
        }
      }
    });

    const show = document.createElement("button");
    show.textContent = "Show me";
    show.addEventListener("click", () => {
      const el = field._group.elements[0];
      el.scrollIntoView({ block: "center", behavior: "smooth" });
      highlightFields([{ index: field.index, tone: "needs-you" }]);
      try { el.focus({ preventScroll: true }); } catch (e) { /* not focusable */ }
    });

    const copy = document.createElement("button");
    copy.textContent = "Copy";
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(input.value);
        status.className = "status";
        status.textContent = "Copied.";
      } catch (e) {
        status.className = "status bad";
        status.textContent = "Clipboard blocked on this page — select and copy.";
      }
    });

    row.append(insert, show, copy);
    item.append(row, status);
    return item;
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || !msg.type) return;

    if (msg.type === "JAF_AUTOFILL") {
      autofill().then(
        (out) => sendResponse(Object.assign({ ok: true }, out)),
        (e) => {
          // Never leave the readout spinning on a step that has already failed.
          hudLine(String(e.message || e), "bad");
          sendResponse({ ok: false, error: String(e.message || e) });
        }
      );
      return true;
    }

    if (msg.type === "JAF_SCAN") {
      classifyAgainstStore(scan()).then((fields) => {
        sendResponse({
          ok: true,
          url: location.href,
          domain: location.hostname,
          formSignature: signatureOf(fields),
          fields
        });
      });
      return true;
    }
    if (msg.type === "JAF_CAPTURE") {
      const fields = capture();
      JAF.debugLog("content", "capture", capturePayload(fields));
      sendResponse({
        ok: true,
        domain: location.hostname,
        formSignature: signatureOf(fields),
        fields
      });
      return true;
    }
    if (msg.type === "JAF_FILL") {
      const results = applyFill(msg.items);
      JAF.debugLog("content", "fill", {
        requested: (msg.items || []).length,
        // labels and outcomes only — never the values written
        results: results.map((r) => ({ label: r.label, ok: r.ok, reason: r.reason || null }))
      });
      sendResponse({ ok: true, results });
      return true;
    }

    if (msg.type === "JAF_HIGHLIGHT") {
      if (!lastScan.length) scan();
      const marked = highlightFields(msg.marks || []);
      JAF.debugLog("content", "highlight", { requested: (msg.marks || []).length, marked });
      sendResponse({ ok: true, marked });
      return true;
    }
    return undefined;
  });

  // Debug handle. Content scripts live in an isolated world, so this is not
  // reachable from the page's own JS — it's for poking at scans from devtools.
  globalThis.__JAF_DEV = {
    scan: () => scan().map(serialize),
    capture,
    logScan,
    capturePayload: () => capturePayload(capture())
  };

  /**
   * Reads the form on submit and hands it to the service worker for the review
   * queue.
   *
   * This is a passive listener: it never calls preventDefault, never blocks,
   * and never touches the event. The submit proceeds exactly as it would with
   * the extension uninstalled — capturing must not be able to cost someone an
   * application.
   */
  /**
   * Buttons that mean "I am done with this step".
   *
   * Modern application forms (Ashby, Greenhouse, LinkedIn Easy Apply) post with
   * fetch() on a click and never raise a `submit` event, so listening only for
   * `submit` silently learns nothing from them. Wizard steps count too: each
   * "Next" carries answers that will not be on the page a moment later.
   */
  const SUBMIT_TEXT =
    /^(submit|submit application|apply|apply now|send|send application|next|continue|review|finish|done|save( and continue)?|complete)\b/i;

  let lastCapture = { signature: "", ts: 0 };

  function onSubmitClick(event) {
    const el = event.target && event.target.closest
      ? event.target.closest('button, [role="button"], input[type="submit"], a[href="#"]')
      : null;
    if (!el) return;
    const text = cleanText(el.value || el.textContent || el.getAttribute("aria-label") || "");
    if (!text || text.length > 40 || !SUBMIT_TEXT.test(text)) return;
    onSubmit();
  }

  function onSubmit() {
    let fields;
    try {
      fields = capture();   // capturePayload is the log's shape-only view
    } catch (e) {
      return;   // never let a capture error surface in the page's submit path
    }
    const items = fields
      .filter((f) => String(f.value || "").trim())
      .map((f) => ({
        index: f.index,
        label: f.label,
        value: String(f.value),
        kind: f.kind,
        profileKey: f.profileKey || null,
        sectionKey: f.sectionKey || null,
        sensitive: !!f.sensitive,
        options: (f.options || []).length
      }));
    if (!items.length) return;

    const signature = signatureOf(fields);

    // A click on "Submit" is usually followed by a real submit event too, and a
    // wizard step can fire several clicks. Send each state of a form once.
    const now = Date.now();
    if (lastCapture.signature === signature && now - lastCapture.ts < 3000) return;
    lastCapture = { signature, ts: now };

    // Every submitted form teaches the knowledge base. This runs after the
    // values have been read, off the submit path, and cannot delay or cancel
    // the submit — the form behaves exactly as if the extension were absent.
    ask({
      type: "JAF_SERVICE_LEARN",
      payload: {
        site: location.hostname,
        url: location.href,
        signature,
        jobTitle: cleanText(document.title).slice(0, 120),
        items: items
          .filter((f) => !f.sensitive)
          .map((f) => ({ id: String(f.index), label: f.label, value: f.value }))
      }
    }).then((res) => {
      if (res && res.ok) return;
      // Service down: fall back to the local review queue so the answers are
      // not simply lost.
      try {
        chrome.runtime.sendMessage({
          type: "JAF_CAPTURE_SUBMIT",
          capture: {
            ts: new Date().toISOString(),
            domain: location.hostname,
            url: location.href,
            formSignature: signature,
            items
          }
        }, () => { void chrome.runtime.lastError; });
      } catch (e) {
        /* worker asleep or context invalidated — the submit still went through */
      }
    });
  }

  /**
   * Tell the service worker this frame exists and how many fields it holds, so
   * the popup can address iframed forms (Greenhouse/Lever embeds) by frameId.
   */
  function announce() {
    const count = scan().length;
    if (!count) return;
    try {
      chrome.runtime.sendMessage({
        type: "JAF_FRAME_HELLO",
        fieldCount: count,
        url: location.href
      });
    } catch (e) {
      /* worker asleep or context invalidated — popup will re-probe */
    }
  }

  function onReady() {
    setTimeout(() => {
      announce();
      logScan();
    }, 500);
    // Capture phase, so the values are read before any handler can reset the
    // form. Passive: neither listener can cancel or delay anything.
    document.addEventListener("submit", onSubmit, { capture: true, passive: true });
    document.addEventListener("click", onSubmitClick, { capture: true, passive: true });
  }

  if (document.readyState === "complete") onReady();
  else window.addEventListener("load", onReady);
})();
