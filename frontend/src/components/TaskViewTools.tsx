import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { downloadTaskExport } from "../api/export";
import type { TaskQuery } from "../api/tasks";
import { loadViews, newViewId, persistViews, type SavedView } from "../lib/savedViews";
import { EMPTY_VIEW, type TaskViewFilters } from "../lib/useTaskFilters";
import "../styles/taskViewTools.css";

/*  Toolbar extras for the task list: copy link, saved views, export. Kept out
 *  of TaskList.tsx so that page only has to place one element. */

function Popover({ label, children }: { label: string; children: (close: () => void) => ReactNode }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      ref.current?.querySelector<HTMLElement>(".cm-tvt-trigger")?.focus();
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="cm-tvt-pop" ref={ref}>
      <button
        type="button"
        className="cm-btn cm-btn-outline cm-tvt-trigger"
        aria-expanded={open}
        aria-haspopup="true"
        onClick={() => setOpen((o) => !o)}
      >
        {label}
      </button>
      {open && <div className="cm-tvt-panel">{children(close)}</div>}
    </div>
  );
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Clipboard API is missing on plain-HTTP origins and in some embedded
    // webviews; fall back to a transient textarea and execCommand.
    try {
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand("copy");
      area.remove();
      return ok;
    } catch {
      return false;
    }
  }
}

function sameFilters(a: TaskViewFilters, b: TaskViewFilters): boolean {
  return (Object.keys(EMPTY_VIEW) as (keyof TaskViewFilters)[]).every((k) => a[k] === b[k]);
}

function describe(f: TaskViewFilters): string {
  const parts: string[] = [];
  if (f.status !== "all") parts.push(f.status);
  if (f.mine === "1") parts.push("Started by me");
  if (f.q) parts.push(`“${f.q}”`);
  if (f.from || f.to) parts.push(`${f.from || "…"} – ${f.to || "…"}`);
  if (f.group !== "none") parts.push(`by ${f.group}`);
  return parts.join(" · ") || "No filters";
}

function CopyLink() {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<number>(0);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  async function copy() {
    // The hash URL *is* the view, so the address bar is exactly what to share.
    const ok = await copyText(window.location.href);
    setState(ok ? "copied" : "failed");
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setState("idle"), 2500);
  }

  return (
    <>
      <button type="button" className="cm-btn cm-btn-outline" onClick={copy}>
        Copy link
      </button>
      <span className="cm-visually-hidden" role="status">
        {state === "copied" ? "Link copied" : state === "failed" ? "Couldn't copy the link" : ""}
      </span>
      {state !== "idle" && (
        <span className="cm-tvt-toast" aria-hidden="true" data-failed={state === "failed" ? "true" : undefined}>
          {state === "copied" ? "Link copied" : "Couldn't copy — copy it from the address bar"}
        </span>
      )}
    </>
  );
}

function SavedViews({ current, onApply }: { current: TaskViewFilters; onApply: (f: TaskViewFilters) => void }) {
  const [views, setViews] = useState<SavedView[]>(loadViews);
  const [name, setName] = useState("");
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameText, setRenameText] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [storageError, setStorageError] = useState(false);

  function commit(next: SavedView[]) {
    setViews(next);
    setStorageError(!persistViews(next));
  }

  function save() {
    const trimmed = name.trim();
    if (!trimmed) return;
    commit([...views, { id: newViewId(), name: trimmed, filters: current }]);
    setName("");
  }

  function rename(id: string) {
    const trimmed = renameText.trim();
    if (trimmed) commit(views.map((v) => (v.id === id ? { ...v, name: trimmed } : v)));
    setRenaming(null);
  }

  return (
    <Popover label={views.length > 0 ? `Views (${views.length})` : "Views"}>
      {(close) => (
        <div className="cm-tvt-views">
          {views.length === 0 ? (
            <p className="cm-field-hint">No saved views yet. Set up filters, then name and save them here.</p>
          ) : (
            <ul className="cm-tvt-list">
              {views.map((v) => (
                <li key={v.id} className="cm-tvt-item" data-active={sameFilters(v.filters, current) ? "true" : undefined}>
                  {renaming === v.id ? (
                    <form
                      className="cm-tvt-row"
                      onSubmit={(e) => {
                        e.preventDefault();
                        rename(v.id);
                      }}
                    >
                      <input
                        className="cm-input"
                        aria-label={`New name for ${v.name}`}
                        value={renameText}
                        autoFocus
                        onChange={(e) => setRenameText(e.target.value)}
                      />
                      <button type="submit" className="cm-btn cm-btn-primary cm-btn-sm">
                        Save
                      </button>
                      <button type="button" className="cm-btn cm-btn-ghost cm-btn-sm" onClick={() => setRenaming(null)}>
                        Cancel
                      </button>
                    </form>
                  ) : (
                    <>
                      <button
                        type="button"
                        className="cm-tvt-apply"
                        onClick={() => {
                          onApply(v.filters);
                          close();
                        }}
                      >
                        <span className="cm-tvt-name">{v.name}</span>
                        <span className="cm-tvt-desc">{describe(v.filters)}</span>
                      </button>
                      {confirmDelete === v.id ? (
                        <span className="cm-tvt-row">
                          <button
                            type="button"
                            className="cm-btn cm-btn-destructive cm-btn-sm"
                            onClick={() => {
                              commit(views.filter((x) => x.id !== v.id));
                              setConfirmDelete(null);
                            }}
                          >
                            Delete view
                          </button>
                          <button type="button" className="cm-btn cm-btn-ghost cm-btn-sm" onClick={() => setConfirmDelete(null)}>
                            Keep
                          </button>
                        </span>
                      ) : (
                        <span className="cm-tvt-row">
                          <button
                            type="button"
                            className="cm-btn cm-btn-ghost cm-btn-sm"
                            aria-label={`Rename ${v.name}`}
                            onClick={() => {
                              setRenaming(v.id);
                              setRenameText(v.name);
                            }}
                          >
                            Rename
                          </button>
                          <button
                            type="button"
                            className="cm-btn cm-btn-ghost cm-btn-sm"
                            aria-label={`Delete ${v.name}`}
                            onClick={() => setConfirmDelete(v.id)}
                          >
                            Delete
                          </button>
                        </span>
                      )}
                    </>
                  )}
                </li>
              ))}
            </ul>
          )}

          <form
            className="cm-tvt-save"
            onSubmit={(e) => {
              e.preventDefault();
              save();
            }}
          >
            <label htmlFor="cm-tvt-name" className="cm-field-hint">
              Save the current filters ({describe(current)})
            </label>
            <div className="cm-tvt-row">
              <input
                id="cm-tvt-name"
                className="cm-input"
                placeholder="View name, e.g. SMTP failures"
                value={name}
                maxLength={60}
                onChange={(e) => setName(e.target.value)}
              />
              <button type="submit" className="cm-btn cm-btn-primary" disabled={!name.trim()}>
                Save view
              </button>
            </div>
          </form>
          {storageError && (
            <p className="cm-error" role="alert">
              This browser wouldn&rsquo;t store the view, so it will be lost on reload. Check that site data isn&rsquo;t blocked.
            </p>
          )}
        </div>
      )}
    </Popover>
  );
}

function ExportMenu({ params }: { params: TaskQuery }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null);

  async function run(format: "csv" | "json", close: () => void) {
    setBusy(true);
    setMessage(null);
    close();
    try {
      const result = await downloadTaskExport(format, params);
      setMessage({
        text: result.truncated
          ? `Exported the newest ${result.rows.toLocaleString()} of ${result.total.toLocaleString()} runs. Narrow the filters to get the rest.`
          : `Exported ${result.rows.toLocaleString()} run${result.rows === 1 ? "" : "s"}.`,
        error: false,
      });
    } catch (err) {
      setMessage({ text: err instanceof Error ? err.message : "Export failed. Try again.", error: true });
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Popover label={busy ? "Exporting…" : "Export"}>
        {(close) => (
          <div className="cm-tvt-menu">
            <p className="cm-field-hint">Downloads every run matching the current filters.</p>
            <button type="button" className="cm-tvt-apply" disabled={busy} onClick={() => run("csv", close)}>
              <span className="cm-tvt-name">CSV</span>
              <span className="cm-tvt-desc">For spreadsheets</span>
            </button>
            <button type="button" className="cm-tvt-apply" disabled={busy} onClick={() => run("json", close)}>
              <span className="cm-tvt-name">JSON</span>
              <span className="cm-tvt-desc">For scripts, with parsed args</span>
            </button>
          </div>
        )}
      </Popover>
      {message && (
        <p className={message.error ? "cm-error" : "cm-notice"} role={message.error ? "alert" : "status"}>
          {message.text}
        </p>
      )}
    </>
  );
}

export function TaskViewTools(props: {
  filters: TaskViewFilters;
  onApplyView: (f: TaskViewFilters) => void;
  exportParams: TaskQuery;
}) {
  return (
    <div className="cm-tvt">
      <SavedViews current={props.filters} onApply={props.onApplyView} />
      <CopyLink />
      <ExportMenu params={props.exportParams} />
    </div>
  );
}
