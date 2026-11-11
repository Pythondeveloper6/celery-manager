import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import vm from "node:vm";
const require = createRequire(import.meta.url);
const ts = require("typescript");
const file = resolve(dirname(new URL(import.meta.url).pathname), "../src/lib/llm.ts");
const source = ts.transpileModule(readFileSync(file, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
const mod = { exports: {} };
vm.runInThisContext(`(function(require,module,exports){${source}\n})`, { filename: file })(require, mod, mod.exports);
const { formatTokens, formatCost } = mod.exports;

test("token counts are compacted", () => {
  assert.equal(formatTokens(0), "0");
  assert.equal(formatTokens(1234), "1,234");
  assert.equal(formatTokens(12345), "12.3k");
  assert.equal(formatTokens(250000), "250k");
  assert.equal(formatTokens(1500000), "1.50M");
});

test("null cost is n/a, never zero", () => {
  assert.equal(formatCost(null), "n/a");
  assert.equal(formatCost(undefined), "n/a");
  assert.equal(formatCost(0), "$0.00");
  assert.equal(formatCost(0.0042), "$0.0042");
  assert.equal(formatCost(12.345), "$12.35");
  assert.equal(formatCost(1234.5), "$1,235");
});
