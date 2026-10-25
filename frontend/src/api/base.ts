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
