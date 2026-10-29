import test from "node:test";
import assert from "node:assert/strict";
import { mountTaskStatus } from "../public/task-status.js";

const flush = () => new Promise(setImmediate);
function host() {
  globalThis.document = { createElement: () => ({ setAttribute() {}, textContent: "" }) };
  return { replaceChildren(child) { this.child = child; } };
}
test("uses versioned mounted URL and treats task text as text", async () => {
  const element = host();
  let count = 0;
  const dispose = mountTaskStatus(element, { taskId: "abc", baseUrl: "https://example.test/jobs/", fetch: async (url, options) => {
    count++;
    assert.equal(url, "https://example.test/jobs/api/v1/tasks/abc");
    assert.equal(options.credentials, "include");
    return { ok: true, json: async () => ({ task_name: "<script>bad</script>", status: "succeeded" }) };
  } });
  await flush();
  assert.equal(element.child.textContent, "<script>bad</script>: succeeded");
  assert.equal(count, 1);
  dispose();
});
test("auth denial stops polling and asks for sign in", async () => {
  const element = host();
  const dispose = mountTaskStatus(element, { taskId: "abc", fetch: async () => ({ status: 401 }) });
  await flush();
  assert.equal(element.child.textContent, "Sign in to view this task.");
  dispose();
});
test("an unrecorded task waits, and destroy aborts pending work", async () => {
  const element = host();
  let signal;
  const dispose = mountTaskStatus(element, { taskId: "abc", fetch: async (_, options) => {
    signal = options.signal;
    return { status: 404 };
  } });
  await flush();
  assert.match(element.child.textContent, /Waiting/);
  dispose();
  assert.equal(signal.aborted, true);
});
test("rejects credential URLs and non-HTTP URLs", () => {
  for (const baseUrl of ["javascript:alert(1)", "https://secret:token@example.test"]) {
    assert.throws(() => mountTaskStatus(host(), { taskId: "abc", baseUrl }), TypeError);
  }
});
