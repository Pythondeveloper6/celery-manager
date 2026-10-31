import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import vm from "node:vm";
const require = createRequire(import.meta.url);
const ts = require("typescript");
const file = resolve(dirname(new URL(import.meta.url).pathname), "../src/lib/schemaForm.ts");
const source = ts.transpileModule(readFileSync(file, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
const mod = { exports: {} };
vm.runInThisContext(`(function(require,module,exports){${source}\n})`, { filename: file })(require, mod, mod.exports);
const { schemaFields, setKwarg, readKwarg } = mod.exports;

const schema = {
  type: "object",
  required: ["image_id"],
  properties: {
    image_id: { type: "integer", title: "Image Id", description: "Image to resize", minimum: 1 },
    size: { enum: ["small", "large"], type: "string", default: "small" },
    sharpen: { type: "boolean", default: false },
    ratio: { anyOf: [{ type: "number" }, { type: "null" }], default: null },
  },
};

test("simple schemas become form fields", () => {
  const fields = schemaFields(schema);
  assert.deepEqual(fields.map((f) => [f.name, f.kind, f.required]), [
    ["image_id", "integer", true], ["size", "enum", false], ["sharpen", "boolean", false], ["ratio", "number", false],
  ]);
  assert.deepEqual(fields[1].options, ["small", "large"]);
  assert.equal(fields[0].description, "Image to resize");
});

test("nested or unknown shapes fall back to the JSON editor", () => {
  assert.equal(schemaFields(null), null);
  assert.equal(schemaFields({ type: "object", properties: { items: { type: "array" } } }), null);
  assert.equal(schemaFields({ type: "object", properties: { ref: { $ref: "#/$defs/X" } } }), null);
});

test("editing a field rewrites only that key of the kwargs JSON", () => {
  let text = '{"size": "small"}';
  text = setKwarg(text, "image_id", "42", "integer");
  assert.deepEqual(JSON.parse(text), { size: "small", image_id: 42 });
  text = setKwarg(text, "sharpen", true, "boolean");
  assert.equal(readKwarg(text, "sharpen"), true);
  text = setKwarg(text, "image_id", "", "integer");
  assert.equal("image_id" in JSON.parse(text), false);
  // Text that is not a number is kept as typed, so the server rejects it visibly.
  assert.equal(JSON.parse(setKwarg(text, "image_id", "4x", "integer")).image_id, "4x");
  // Broken JSON is left alone rather than overwritten.
  assert.equal(setKwarg("{oops", "size", "large", "enum"), "{oops");
});
