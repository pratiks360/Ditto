/**
 * Regenerates the copy of storage.js embedded in background.js.
 *
 * MV3 service workers cannot importScripts() an extension file, so background.js
 * carries storage.js inline. storage.js stays the single source of truth — run
 * this after editing it, or the worker silently runs stale helpers.
 *
 *   node tools-inline-storage.js
 */
const fs = require("fs");

const BEGIN = "// ====== storage.js inlined for MV3 service worker compatibility ======";
const END = "// ====== end storage.js ======";

const storage = fs.readFileSync("storage.js", "utf8").trimEnd();
const background = fs.readFileSync("background.js", "utf8");

const start = background.indexOf(BEGIN);
const stop = background.indexOf(END);
if (start === -1 || stop === -1) {
  console.error("background.js is missing the inline markers — refusing to guess.");
  process.exit(1);
}

const next = BEGIN + "\n" + storage + "\n" + END;
const updated = background.slice(0, start) + next + background.slice(stop + END.length);

if (updated === background) {
  console.log("background.js already up to date.");
} else {
  fs.writeFileSync("background.js", updated);
  console.log(`background.js updated — inlined ${storage.split("\n").length} lines from storage.js.`);
}
