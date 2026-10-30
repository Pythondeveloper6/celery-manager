/** Adds explicit human preconditions to manager-owned sensitive HTTP requests. */
export type SensitiveAction = { method: string; path: string; action: string };
export function installOperatorSafeguards(environment: string, actions: SensitiveAction[], ask: (message: string) => string | null = message => window.prompt(message)) {
  if (!actions.length) return () => {};
  const original = globalThis.fetch;
  const mount = new URL("api/", window.location.href);
  const wrapped: typeof fetch = async (input, init) => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, window.location.href);
    const method = (init?.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    const path = url.pathname.startsWith(mount.pathname) ? "/api/" + url.pathname.slice(mount.pathname.length).replace(/^v1\//, "") : "";
    const action = url.origin === mount.origin ? actions.find(rule => rule.method === method && new RegExp(`^(?:${rule.path})$`).test(path)) : undefined;
    if (!action) return original(input, init);
    const reason = ask(`Reason for ${action.action} in ${environment} (recorded in the audit log):`);
    const confirmation = reason?.trim() ? ask(`Type ${environment} to confirm ${action.action}:`) : null;
    if (!reason?.trim() || reason.trim().length > 500 || confirmation !== environment) {
      return new Response(JSON.stringify({ detail: "Action cancelled: a reason and exact environment confirmation are required." }), { status: 428, headers: { "Content-Type": "application/json" } });
    }
    const headers = new Headers(init?.headers || (input instanceof Request ? input.headers : undefined));
    headers.set("X-CM-Action-Reason", encodeURIComponent(reason.trim()));
    headers.set("X-CM-Confirm-Environment", encodeURIComponent(confirmation));
    return original(input, { ...init, headers });
  };
  globalThis.fetch = wrapped;
  return () => { if (globalThis.fetch === wrapped) globalThis.fetch = original; };
}
