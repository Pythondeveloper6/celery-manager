import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import vm from "node:vm";
const require = createRequire(import.meta.url);
const ts = require("typescript");
const source = ts.transpileModule(readFileSync(new URL("../src/lib/safeguards.ts", import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
function environment(answers) {
  const calls = [], prompts = [], exports = {};
  const original = async (...args) => { calls.push(args); return new Response("{}"); };
  const context = { exports, fetch: original, URL, Headers, Request, Response, window: { location: { href: "https://host.test/manager/" }, prompt: message => { prompts.push(message); return answers.shift(); } } };
  vm.runInNewContext(source, context);
  const restore = exports.installOperatorSafeguards("production", [{ method: "POST", path: "/api/tasks/start", action: "task.start" }]);
  return { context, calls, prompts, restore, original };
}
test("sensitive mounted requests carry a Unicode-safe reason and exact environment", async () => {
  const state = environment(["سبب إعادة المحاولة", "production"]);
  await state.context.fetch("api/v1/tasks/start", { method: "POST", body: "{}" });
  assert.equal(state.calls.length, 1);
  assert.equal(decodeURIComponent(state.calls[0][1].headers.get("X-CM-Action-Reason")), "سبب إعادة المحاولة");
  assert.equal(state.calls[0][1].headers.get("X-CM-Confirm-Environment"), "production");
  state.restore(); assert.equal(state.context.fetch, state.original);
});
test("cancelled or wrong-environment confirmations never send a request", async () => {
  for (const answers of [[null], ["reviewed", "staging"]]) {
    const state = environment(answers);
    assert.equal((await state.context.fetch("api/tasks/start", { method: "POST" })).status, 428);
    assert.equal(state.calls.length, 0); state.restore();
  }
});
test("reads and other origins are never intercepted", async () => {
  const state = environment([]);
  await state.context.fetch("api/tasks");
  await state.context.fetch("https://other.test/api/tasks/start", { method: "POST" });
  assert.equal(state.calls.length, 2); assert.equal(state.prompts.length, 0); state.restore();
});
