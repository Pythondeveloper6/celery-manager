// The one place the API location is decided.
//
// Deliberately *relative* ("api", not "/api"): fetch() and EventSource resolve
// it against the page's own URL, so the same bundle works at the root
// (http://host/ -> /api/...) and when mounted under a prefix inside another
// app (http://host/celery-manager/ -> /celery-manager/api/...). HashRouter keeps
// the path fixed at the mount root, so the document URL never drifts.
// This is why the mount point needs its trailing slash: "/celery-manager"
// would resolve "api" to "/api".
export const API_BASE = "api";

// A 403 on a state-changing request means the signed-in role isn't allowed
// to do it (or, rarely, the CSRF origin check refused it). The server's
// `detail` already says which role is needed, so prefer it over the
// caller's generic "X failed." message.
export async function forbiddenMessage(response: Response): Promise<string> {
  try {
    const body = await response.json();
    if (typeof body?.detail === "string" && body.detail.trim()) return body.detail.trim();
  } catch {
    // Not JSON; fall through.
  }
  return "Not allowed: your role can't do this.";
}
