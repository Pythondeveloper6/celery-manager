import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import vm from "node:vm";
const require = createRequire(import.meta.url);
const ts = require("typescript");
const root = resolve(dirname(new URL(import.meta.url).pathname), "../src");
let active;
const hooks = {
  useState(initial) { const runner = active, slot = runner.cursor++; if (!(slot in runner.slots)) runner.slots[slot] = typeof initial === "function" ? initial() : initial; return [runner.slots[slot], value => { runner.slots[slot] = typeof value === "function" ? value(runner.slots[slot]) : value; }]; },
  useRef(initial) { const [ref] = hooks.useState(() => ({ current: initial })); return ref; },
  useId() { return "test-id"; },
  useEffect(effect, deps) { const runner = active, slot = runner.cursor++, previous = runner.slots[slot]; if (!previous || deps.some((value, index) => value !== previous.deps[index])) { runner.effects.push(() => { previous?.cleanup?.(); runner.slots[slot] = { deps, cleanup: effect() }; }); } },
};
function load(file, overrides = {}) {
  const filename = resolve(root, file);
  const source = ts.transpileModule(readFileSync(filename, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  const imports = id => {
    if (Object.hasOwn(overrides, id)) return overrides[id];
    if (id === "react") return hooks;
    if (id.includes("/api/base") || id === "./base") return { API_BASE: "api" };
    if (id.includes("/api/tasks")) return { errorMessage: async (_, fallback) => fallback };
    if (id.includes("/lib/role")) return { useCan: () => true };
    if (id === "./ScheduleReconciliation") return { ScheduleReconciliation: () => null };
    return require(id);
  };
  vm.runInThisContext(`(function(require,module,exports){${source}\n})`, { filename })(imports, module, module.exports);
  return module.exports;
}
function runner(component, props) {
  const state = { slots: [], cursor: 0, effects: [], props, tree: null };
  state.render = (next = state.props) => { state.props = next; state.cursor = 0; active = state; state.tree = component(next); for (const effect of state.effects.splice(0)) effect(); return state.tree; };
  state.render(); state.render(); return state;
}
function elements(tree) {
  if (Array.isArray(tree)) return tree.flatMap(elements);
  if (!tree || typeof tree !== "object" || !tree.props) return [];
  return [tree, ...elements(tree.props.children)];
}
function text(tree) { if (Array.isArray(tree)) return tree.map(text).join(""); if (tree === null || tree === undefined || typeof tree === "boolean") return ""; return typeof tree === "object" ? text(tree.props?.children) : String(tree); }
function button(state, name) { return elements(state.tree).find(e => e.type === "button" && text(e) === name); }
function field(state, type, index = 0) { return elements(state.tree).filter(e => e.type === type)[index]; }
function deferred() { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; }
const response = body => ({ ok: true, json: async () => body });

test("export preserves identity constraints alongside ordinary filters", async () => {
  const { downloadTaskExport } = load("api/export.ts");
  const requested = [];
  const previous = { fetch: globalThis.fetch, document: globalThis.document };
  globalThis.document = { createElement: () => ({ click() {}, remove() {} }), body: { appendChild() {} } };
  globalThis.fetch = async url => { requested.push(new URL(url, "https://example.test/jobs/")); return { ok: true, blob: async () => new Blob(["runs"]), headers: new Headers() }; };
  try {
    await downloadTaskExport("json", { started_by_me: true, initiator: "subject+42", status: "failed", q: "import" });
    assert.equal(requested[0].searchParams.get("started_by_me"), "true");
    assert.equal(requested[0].searchParams.get("initiator"), "subject+42");
    assert.equal(requested[0].searchParams.get("status"), "failed");
    await downloadTaskExport("csv", { started_by_me: false });
    assert.equal(requested[1].searchParams.get("started_by_me"), "false");
  } finally { Object.assign(globalThis, previous); }
});

test("switching failure target clears its note and rejects late history from the previous task", async () => {
  const { FailureReview } = load("components/FailureReview.tsx");
  const pending = deferred(), requested = [];
  const previous = globalThis.fetch;
  globalThis.fetch = async (url, options) => { requested.push({ url, options }); return pending.promise; };
  try {
    const state = runner(FailureReview, { taskId: "first", onChanged() {} });
    field(state, "textarea").props.onChange({ target: { value: "first task note" } }); state.render();
    const loading = button(state, "Show history").props.onClick(); state.render();
    assert.equal(field(state, "input").props.disabled, true);
    state.render({ taskId: "second", onChanged() {} }); state.render();
    assert.equal(field(state, "textarea").props.value, "");
    pending.resolve(response({ items: [{ state: "ignored", actor: "old actor", created_at: new Date().toISOString(), note: "old task history" }] }));
    await loading; state.render();
    assert.equal(field(state, "input").props.value, "second");
    assert.doesNotMatch(text(state.tree), /old task history/);
    assert.match(requested[0].url, /first\/history$/);
    assert.equal(button(state, "Show history").props.disabled, false);
  } finally { globalThis.fetch = previous; }
});

test("a refreshed schedule blocks stale invocation writes until explicitly reloaded", async () => {
  const { BackendScheduleActions } = load("components/BackendSchedules.tsx");
  const schedule = { name: "daily", enabled: true, backend_writable: true, definition: { args: [1], kwargs: {}, options: {} } };
  const actions = runner(BackendScheduleActions, { schedule, onChanged() {}, onError() {} });
  const editor = elements(actions.tree).find(e => typeof e.type === "function" && e.type.name === "ScheduleInvocationEditor");
  const state = runner(editor.type, editor.props);
  field(state, "textarea").props.onChange({ target: { value: "[99]" } }); state.render();
  const next = { ...schedule, definition: { ...schedule.definition, args: [2] } };
  state.render({ ...editor.props, schedule: next });
  assert.equal(button(state, "Save arguments and options").props.disabled, true);
  assert.match(text(state.tree), /backend definition changed/);
  let requests = 0;
  const previous = globalThis.fetch; globalThis.fetch = async () => { requests++; return response({}); };
  try { await field(state, "form").props.onSubmit({ preventDefault() {} }); assert.equal(requests, 0); }
  finally { globalThis.fetch = previous; }
  button(state, "Reload current definition").props.onClick(); state.render();
  assert.equal(JSON.parse(field(state, "textarea").props.value)[0], 2);
  assert.equal(button(state, "Save arguments and options").props.disabled, false);
});

test("reconciliation invalidates approval on input changes and consumes failed apply plans", async () => {
  const { ScheduleReconciliation } = load("components/ScheduleReconciliation.tsx");
  const plan = { version: 1, backend: "sqlalchemy-beat", ownership: "sql", observed_fingerprint: "snapshot", changes: [{ name: "daily", action: "create", apply: true, before: null, after: { name: "daily", task: "task", args: [], kwargs: {}, options: {} } }] };
  const previous = globalThis.fetch, requests = [];
  globalThis.fetch = async (url, options) => { requests.push({ url, body: JSON.parse(options.body) }); return url.endsWith("/plan") ? response(plan) : { ok: false }; };
  try {
    const state = runner(ScheduleReconciliation, { writable: true, onChanged() {} });
    field(state, "select").props.onChange({ target: { value: "sql" } }); state.render();
    await button(state, "Generate review plan").props.onClick(); state.render();
    assert.equal(button(state, "Apply reviewed changes").props.disabled, true);
    // Approval is the last checkbox; preceding boxes select repairs and writes.
    const approval = elements(state.tree).filter(e => e.type === "input").at(-1);
    approval.props.onChange({ target: { checked: true } }); state.render();
    assert.equal(button(state, "Apply reviewed changes").props.disabled, false);
    field(state, "textarea").props.onChange({ target: { value: "{}" } }); state.render();
    assert.equal(button(state, "Apply reviewed changes"), undefined);
    field(state, "textarea").props.onChange({ target: { value: "" } }); state.render();
    await button(state, "Generate review plan").props.onClick(); state.render();
    elements(state.tree).filter(e => e.type === "input").at(-1).props.onChange({ target: { checked: true } }); state.render();
    await button(state, "Apply reviewed changes").props.onClick(); state.render();
    assert.equal(button(state, "Apply reviewed changes"), undefined);
    assert.match(text(state.tree), /Couldn't reconcile schedules/);
    assert.equal(requests.filter(request => request.url.endsWith("/apply")).length, 1);
    assert.deepEqual(requests.at(-1).body, plan);
  } finally { globalThis.fetch = previous; }
});

test("a completed resolution for the previous failure cannot annotate the new target", async () => {
  const { FailureReview } = load("components/FailureReview.tsx");
  const pending = deferred(), requests = [];
  let changed = 0;
  const previous = globalThis.fetch;
  globalThis.fetch = async (url, options) => { requests.push({ url, body: JSON.parse(options.body) }); return pending.promise; };
  const onChanged = () => { changed++; };
  try {
    const state = runner(FailureReview, { taskId: "first", onChanged });
    field(state, "textarea").props.onChange({ target: { value: "first-specific note" } }); state.render();
    const saving = button(state, "Ignore failure").props.onClick(); state.render();
    state.render({ taskId: "second", onChanged }); state.render();
    pending.resolve(response({ state: "ignored" })); await saving; state.render();
    assert.equal(changed, 1);
    assert.equal(requests.length, 1);
    assert.match(requests[0].url, /first\/ignore$/);
    assert.equal(requests[0].body.note, "first-specific note");
    assert.doesNotMatch(text(state.tree), /Failure ignored/);
    assert.equal(field(state, "textarea").props.value, "");
  } finally { globalThis.fetch = previous; }
});


test("read-only capabilities suppress operator/admin controls without hiding viewer content", () => {
  let readOnly = true;
  const role = load("lib/role.tsx", {
    react: { createContext: value => ({ value }), useContext: context => context.value },
    "./auth": { useAuthOptions: () => ({ read_only: readOnly }) },
  });
  role.RoleContext.value = "admin";
  assert.equal(role.useCan("operator"), false);
  assert.equal(role.useCan("admin"), false);
  assert.equal(role.useCan("viewer"), true);
  readOnly = false;
  assert.equal(role.useCan("operator"), true);
  assert.equal(role.useCan("admin"), true);
});
