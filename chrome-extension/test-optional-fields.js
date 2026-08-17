/**
 * Pulls worthOffering and its two tables straight out of content.js and runs
 * them against the QAD/Redzone form shape, so the test breaks if the real
 * source changes. Field objects mirror what scan() builds.
 */
const fs = require("fs");
const src = fs.readFileSync(require("path").join(__dirname, "content.js"), "utf8");

function lift(startMarker, endMarker) {
  const a = src.indexOf(startMarker);
  const b = src.indexOf(endMarker, a);
  if (a < 0 || b < 0) throw new Error(`could not lift ${startMarker}`);
  return src.slice(a, b);
}

const code = [
  lift("const NOT_AN_ANSWER", "  /**\n   * A blank optional text field"),
  lift("  function worthOffering(field)", "\n  }\n") + "\n}\n"
].join("\n");

// scan() reads the live control; here the field carries its own value.
const readValue = (group) => group.value;
const worthOffering = new Function("readValue", code + "; return worthOffering;")(readValue);

const f = (o) => Object.assign(
  { kind: "text", label: "", name: "", sensitive: false, _group: { value: "" } }, o
);

const cases = [
  // The four that prompted this. LinkedIn was filled; the rest were dropped.
  ["LinkedIn, already filled", f({ kind: "url", label: "LinkedIn", _group: { value: "https://x" } }), false],
  ["Facebook, blank url", f({ kind: "url", label: "Facebook" }), true],
  ["X (fka Twitter), blank", f({ kind: "text", label: "X (fka Twitter)" }), true],
  ["Website, blank url", f({ kind: "url", label: "Website" }), true],

  // Kinds that are not a typed answer.
  ["a select", f({ kind: "select", label: "Country" }), false],
  ["a radio group", f({ kind: "radio", label: "Authorised?" }), false],
  ["a date part", f({ kind: "date-parts", label: "Start date" }), false],
  ["a checkbox", f({ kind: "checkbox", label: "Agree" }), false],

  // Kinds that are.
  ["a tel", f({ kind: "tel", label: "Mobile" }), true],
  ["an email", f({ kind: "email", label: "Personal email" }), true],
  ["a number", f({ kind: "number", label: "Years with Kafka" }), true],
  ["a textarea", f({ kind: "textarea", label: "Anything else?" }), true],

  // Page furniture.
  ["a search box by label", f({ label: "Search" }), false],
  ["a search box by name", f({ label: "Find a job", name: "search_query" }), false],
  ["a camelCase search name", f({ label: "Find a job", name: "searchQuery" }), false],
  ["a hyphenated filter name", f({ label: "Narrow it down", name: "job-filter" }), false],
  ["a normal name with no furniture", f({ label: "Website", name: "personal_website_url" }), true],
  ["a promo code", f({ label: "Discount code" }), false],

  // Guards carried over from worthAsking.
  ["an unlabelled field", f({ label: "   " }), false],
  ["a demographic question", f({ label: "Race / ethnicity", sensitive: true }), false],
  ["a field with whitespace only", f({ label: "Website", _group: { value: "   " } }), true]
];

let bad = 0;
for (const [name, field, want] of cases) {
  const got = worthOffering(field);
  const ok = got === want;
  if (!ok) bad++;
  console.log(`${ok ? "ok  " : "FAIL"}  ${name}: expected ${want}, got ${got}`);
}
console.log(bad ? `\n${bad} failing` : "\nall passing");
process.exit(bad ? 1 : 0);
