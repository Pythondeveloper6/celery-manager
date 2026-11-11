import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import vm from "node:vm";
const require = createRequire(import.meta.url);
const ts = require("typescript");
const file = resolve(dirname(new URL(import.meta.url).pathname), "../src/lib/cronPresets.ts");
const source = ts.transpileModule(readFileSync(file, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
const mod = { exports: {} };
vm.runInThisContext(`(function(require,module,exports){${source}\n})`, { filename: file })(require, mod, mod.exports);
const { CRON_PRESETS, presetIdFor, presetFields } = mod.exports;

test("every preset round-trips to its own id", () => {
  for (const p of CRON_PRESETS) assert.equal(presetIdFor(p.fields), p.id);
});

test("preset ids are unique and fields are 5 non-empty strings", () => {
  assert.equal(new Set(CRON_PRESETS.map((p) => p.id)).size, CRON_PRESETS.length);
  for (const p of CRON_PRESETS) {
    assert.equal(Object.keys(p.fields).length, 5);
    for (const v of Object.values(p.fields)) assert.ok(v.length > 0);
  }
});

test("edited or blank fields are matched sensibly", () => {
  const base = presetFields("weekdays-9");
  assert.equal(presetIdFor({ ...base, minute: "30" }), "custom");
  assert.equal(presetIdFor({ minute: " * ", hour: "", day_of_month: "", month_of_year: "", day_of_week: "" }), "every-minute");
  assert.equal(presetFields("nope"), null);
});

test("presetFields returns a copy", () => {
  const a = presetFields("hourly");
  a.minute = "9";
  assert.equal(presetFields("hourly").minute, "0");
});
