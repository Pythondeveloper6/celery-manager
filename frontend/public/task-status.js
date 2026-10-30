/** Authenticated task status widget. See docs/embedding.md. No credentials are stored. */
export function mountTaskStatus(element, { taskId, baseUrl = "", interval = 5000, fetch: request = globalThis.fetch } = {}) {
  if (!element || typeof taskId !== "string" || !taskId) throw new TypeError("An element and taskId are required.");
  const delay = Math.min(60000, Math.max(1000, Number(interval) || 5000));
  const base = new URL(baseUrl || ".", globalThis.location?.href || "http://localhost/");
  if (!["https:", "http:"].includes(base.protocol) || base.username || base.password) throw new TypeError("Use an HTTP(S) dashboard URL without credentials.");
  const url = new URL(`${base.pathname.replace(/\/$/, "")}/api/v1/tasks/${encodeURIComponent(taskId)}`, base.origin);
  let stopped = false, timer, controller;
  const status = globalThis.document.createElement("span");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  element.replaceChildren(status);
  const terminal = new Set(["succeeded", "failed", "skipped", "revoked", "interrupted"]);
  async function update() {
    if (stopped) return;
    controller = new AbortController();
    let repeat = true;
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await request(url.href, { credentials: "include", signal: controller.signal, headers: { Accept: "application/json" } });
      if (stopped) return;
      if (response.status === 401 || response.status === 403) {
        status.textContent = "Sign in to view this task.";
        repeat = false;
      } else if (response.status === 404) {
        status.textContent = "Waiting for task record…";
      } else if (!response.ok) {
        status.textContent = "Task status unavailable. Retrying…";
      } else {
        const task = await response.json();
        if (stopped) return;
        status.textContent = `${typeof task.task_name === "string" ? task.task_name : taskId}: ${typeof task.status === "string" ? task.status : "unknown"}`;
        repeat = !terminal.has(task.status);
      }
    } catch {
      if (!stopped) status.textContent = "Task status unavailable. Retrying…";
    } finally {
      clearTimeout(timeout);
      if (!stopped && repeat) timer = setTimeout(update, delay);
    }
  }
  status.textContent = "Loading task status…";
  void update();
  return () => { stopped = true; clearTimeout(timer); controller?.abort(); };
}
