import "../styles/task-call.css";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import {
  listRegisteredTasks,
  retryTask,
  startTask,
  type RegisteredTask,
  type SignatureParam,
} from "../api/tasks";
import { IconSearch, IconX } from "./icons";

// One editor for both "start a task" and "retry with changes": both end in
// "pick/confirm a task name, edit args + kwargs, dispatch". The dialog does
// its own API call so the pages mounting it stay thin.

export interface RetrySource {
  taskId: string;
  taskName: string;
  // As stored on the run: JSON text, or a Python repr for older rows.
  args: string;
  kwargs: string;
}

interface Props {
  mode: "start" | "retry";
  retryOf?: RetrySource;
  onClose: () => void;
  // Called with the id of the newly dispatched run.
  onDone: (taskId: string) => void;
}

/** Pretty JSON when the stored text parses, otherwise the raw text so the
 *  operator sees what is there (and the validator explains what is wrong). */
function prettyStored(raw: string | null | undefined, empty: string): string {
  if (!raw) return empty;
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

/** Parse the args editor. Returns the value or an error sentence. */
function parseArgs(text: string): { value?: unknown[]; error?: string } {
  if (!text.trim()) return { value: [] };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (err) {
    return { error: `Not valid JSON: ${err instanceof Error ? err.message : "syntax error"}.` };
  }
  if (!Array.isArray(parsed)) return { error: 'Args must be a JSON array, like ["a", 1].' };
  return { value: parsed };
}

function parseKwargs(text: string): { value?: Record<string, unknown>; error?: string } {
  if (!text.trim()) return { value: {} };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (err) {
    return { error: `Not valid JSON: ${err instanceof Error ? err.message : "syntax error"}.` };
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { error: 'Kwargs must be a JSON object, like {"name": "value"}.' };
  }
  return { value: parsed as Record<string, unknown> };
}

function realParams(sig: SignatureParam[] | null): SignatureParam[] {
  return (sig ?? []).filter((p) => p.kind !== "var_args" && p.kind !== "var_kwargs");
}

/** kwargs prefill: every parameter that has a JSON-able default. */
function kwargsFromSignature(sig: SignatureParam[] | null): string {
  const out: Record<string, unknown> = {};
  for (const p of realParams(sig)) {
    if (p.has_default && p.default !== undefined) out[p.name] = p.default;
  }
  return Object.keys(out).length ? JSON.stringify(out, null, 2) : "{}";
}

function signatureHint(sig: SignatureParam[] | null): string | null {
  if (!sig) return null;
  const parts = sig.map((p) => {
    if (p.kind === "var_args") return `*${p.name}`;
    if (p.kind === "var_kwargs") return `**${p.name}`;
    let s = p.name;
    if (p.annotation) s += `: ${p.annotation}`;
    if (p.has_default) s += ` = ${JSON.stringify(p.default)}`;
    return s;
  });
  return parts.length ? `(${parts.join(", ")})` : "";
}

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function TaskCallDialog({ mode, retryOf, onClose, onDone }: Props) {
  const isRetry = mode === "retry";
  const uid = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const argsRef = useRef<HTMLTextAreaElement>(null);

  const [registered, setRegistered] = useState<RegisteredTask[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [taskName, setTaskName] = useState<string>(retryOf?.taskName ?? "");
  const [signature, setSignature] = useState<SignatureParam[] | null>(null);
  const [argsText, setArgsText] = useState(isRetry ? prettyStored(retryOf?.args, "[]") : "[]");
  const [kwargsText, setKwargsText] = useState(isRetry ? prettyStored(retryOf?.kwargs, "{}") : "{}");
  const [queue, setQueue] = useState("");
  const [when, setWhen] = useState<"now" | "countdown" | "eta">("now");
  const [countdown, setCountdown] = useState("");
  const [eta, setEta] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // Remember what had focus and give it back on close; lock page scroll.
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = overflow;
      previous?.focus?.();
    };
  }, []);

  useEffect(() => {
    (isRetry ? argsRef.current : searchRef.current)?.focus();
  }, [isRetry]);

  useEffect(() => {
    if (isRetry) return;
    let cancelled = false;
    listRegisteredTasks()
      .then((items) => !cancelled && setRegistered(items))
      .catch((err) => !cancelled && setLoadError(err instanceof Error ? err.message : "Couldn't load tasks."));
    return () => {
      cancelled = true;
    };
  }, [isRetry]);

  // In retry mode the signature is only a hint; fetch it quietly.
  useEffect(() => {
    if (!isRetry || !retryOf) return;
    let cancelled = false;
    listRegisteredTasks()
      .then((items) => {
        if (!cancelled) setSignature(items.find((t) => t.name === retryOf.taskName)?.signature ?? null);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [isRetry, retryOf]);

  function onKeyDown(event: React.KeyboardEvent) {
    if (event.key === "Escape") {
      event.stopPropagation();
      if (!submitting) onClose();
      return;
    }
    if (event.key !== "Tab" || !dialogRef.current) return;
    const nodes = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
      (n) => n.offsetParent !== null,
    );
    if (nodes.length === 0) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !dialogRef.current.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !dialogRef.current.contains(active))) {
      event.preventDefault();
      first.focus();
    }
  }

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    const items = registered ?? [];
    return q ? items.filter((t) => t.name.toLowerCase().includes(q)) : items;
  }, [registered, query]);

  function pick(name: string) {
    const task = registered?.find((t) => t.name === name) ?? null;
    setTaskName(name);
    setSignature(task?.signature ?? null);
    setArgsText("[]");
    setKwargsText(kwargsFromSignature(task?.signature ?? null));
    setTimeout(() => argsRef.current?.focus(), 0);
  }

  const selected = registered?.find((t) => t.name === taskName) ?? null;
  const argsResult = useMemo(() => parseArgs(argsText), [argsText]);
  const kwargsResult = useMemo(() => parseKwargs(kwargsText), [kwargsText]);

  let scheduleError: string | null = null;
  if (!isRetry && when === "countdown") {
    const n = Number(countdown);
    if (countdown.trim() === "" || !Number.isFinite(n) || n < 0) scheduleError = "Enter the delay in seconds, 0 or more.";
  }
  if (!isRetry && when === "eta" && (!eta || Number.isNaN(new Date(eta).getTime()))) {
    scheduleError = "Pick the date and time to run at.";
  }

  const canSubmit =
    !submitting && taskName.trim() !== "" && !argsResult.error && !kwargsResult.error && !scheduleError;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!canSubmit || !argsResult.value || !kwargsResult.value) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      if (isRetry && retryOf) {
        const { new_task_id } = await retryTask(retryOf.taskId, {
          args: argsResult.value,
          kwargs: kwargsResult.value,
        });
        onDone(new_task_id);
      } else {
        const { task_id } = await startTask({
          task_name: taskName.trim(),
          args: argsResult.value,
          kwargs: kwargsResult.value,
          ...(queue.trim() ? { queue: queue.trim() } : {}),
          ...(when === "countdown" ? { countdown: Number(countdown) } : {}),
          ...(when === "eta" ? { eta: new Date(eta).toISOString() } : {}),
        });
        onDone(task_id);
      }
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : "Couldn't dispatch the task. Try again.");
      setSubmitting(false);
    }
  }

  const hint = signatureHint(signature);
  const required = realParams(signature).filter((p) => p.required && p.kind === "positional");
  const title = isRetry ? "Retry with changes" : "Start a task";
  const showPicker = !isRetry && !taskName;
  const exactMatch = registered?.some((t) => t.name === query.trim());

  return (
    <div className="cm-sheet-root" onKeyDown={onKeyDown}>
      <div className="cm-sheet-overlay" onClick={() => !submitting && onClose()} aria-hidden="true" />
      <div
        className="cm-sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${uid}-title`}
        ref={dialogRef}
      >
        <header className="cm-sheet-header">
          <div>
            <h2 id={`${uid}-title`} className="cm-sheet-title">
              {title}
            </h2>
            <p className="cm-sheet-desc">
              {isRetry
                ? "Edit the arguments, then dispatch a new run. The original run is kept and linked."
                : "Dispatch a registered task now or later. The action is recorded in the audit log."}
            </p>
          </div>
          <button type="button" className="cm-btn cm-btn-ghost cm-btn-icon" onClick={onClose} aria-label="Close">
            <IconX />
          </button>
        </header>

        <form className="cm-sheet-body" onSubmit={submit} noValidate>
          {showPicker ? (
            <div className="cm-field">
              <label htmlFor={`${uid}-search`}>Task</label>
              <div className="cm-sheet-search">
                <IconSearch />
                <input
                  id={`${uid}-search`}
                  ref={searchRef}
                  className="cm-input"
                  type="search"
                  placeholder="Search registered tasks"
                  value={query}
                  autoComplete="off"
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      if (matches.length === 1) pick(matches[0].name);
                      else if (query.trim() && !exactMatch && matches.length === 0) pick(query.trim());
                    }
                  }}
                />
              </div>
              {loadError && (
                <p className="cm-field-error" role="alert">
                  {loadError} You can still type a task name below.
                </p>
              )}
              <ul className="cm-picker" aria-label="Registered tasks">
                {registered === null && !loadError && <li className="cm-picker-empty">Loading tasks&hellip;</li>}
                {registered !== null && registered.length === 0 && (
                  <li className="cm-picker-empty">
                    No tasks are registered in this process. Type a task name to use it anyway.
                  </li>
                )}
                {registered !== null && registered.length > 0 && matches.length === 0 && query.trim() && (
                  <li className="cm-picker-empty">No registered task matches &ldquo;{query.trim()}&rdquo;.</li>
                )}
                {matches.map((t) => (
                  <li key={t.name}>
                    <button type="button" className="cm-picker-item" onClick={() => pick(t.name)}>
                      <span className="cm-picker-name">{t.name}</span>
                      {t.doc && <span className="cm-picker-doc">{t.doc}</span>}
                    </button>
                  </li>
                ))}
                {query.trim() && !exactMatch && (
                  <li>
                    <button type="button" className="cm-picker-item" onClick={() => pick(query.trim())}>
                      <span className="cm-picker-name">Use &ldquo;{query.trim()}&rdquo;</span>
                      <span className="cm-picker-doc">Not in the list. Dispatch it by name.</span>
                    </button>
                  </li>
                )}
              </ul>
            </div>
          ) : (
            <div className="cm-field">
              <span className="cm-sheet-label">Task</span>
              <div className="cm-sheet-chosen">
                <code>{taskName}</code>
                {!isRetry && (
                  <button
                    type="button"
                    className="cm-btn cm-btn-ghost cm-btn-sm"
                    onClick={() => {
                      setTaskName("");
                      setSignature(null);
                      setTimeout(() => searchRef.current?.focus(), 0);
                    }}
                  >
                    Change
                  </button>
                )}
              </div>
              {selected?.doc && <p className="cm-field-hint">{selected.doc}</p>}
              {hint && <p className="cm-field-hint cm-sheet-sig">{taskName.split(".").pop()}{hint}</p>}
            </div>
          )}

          {!showPicker && (
            <>
              <div className="cm-field">
                <label htmlFor={`${uid}-args`}>Args (JSON array)</label>
                <textarea
                  id={`${uid}-args`}
                  ref={argsRef}
                  className="cm-input cm-input-code cm-sheet-textarea"
                  rows={4}
                  spellCheck={false}
                  value={argsText}
                  aria-invalid={argsResult.error ? true : undefined}
                  aria-describedby={`${uid}-args-msg`}
                  onChange={(e) => setArgsText(e.target.value)}
                />
                {argsResult.error ? (
                  <p id={`${uid}-args-msg`} className="cm-field-error" role="alert">
                    {argsResult.error}
                  </p>
                ) : (
                  <p id={`${uid}-args-msg`} className="cm-field-hint">
                    {required.length > 0 && argsResult.value?.length === 0
                      ? `Positional values in order: ${required.map((p) => p.name).join(", ")}.`
                      : "Positional arguments, in order."}
                  </p>
                )}
              </div>

              <div className="cm-field">
                <label htmlFor={`${uid}-kwargs`}>Kwargs (JSON object)</label>
                <textarea
                  id={`${uid}-kwargs`}
                  className="cm-input cm-input-code cm-sheet-textarea"
                  rows={5}
                  spellCheck={false}
                  value={kwargsText}
                  aria-invalid={kwargsResult.error ? true : undefined}
                  aria-describedby={`${uid}-kwargs-msg`}
                  onChange={(e) => setKwargsText(e.target.value)}
                />
                {kwargsResult.error ? (
                  <p id={`${uid}-kwargs-msg`} className="cm-field-error" role="alert">
                    {kwargsResult.error}
                  </p>
                ) : (
                  <p id={`${uid}-kwargs-msg`} className="cm-field-hint">
                    Keyword arguments by name. Defaults from the task are prefilled.
                  </p>
                )}
              </div>

              {!isRetry && (
                <>
                  <div className="cm-field">
                    <label htmlFor={`${uid}-queue`}>Queue (optional)</label>
                    <input
                      id={`${uid}-queue`}
                      className="cm-input"
                      value={queue}
                      placeholder={selected?.queue ?? "Default queue"}
                      onChange={(e) => setQueue(e.target.value)}
                    />
                  </div>

                  <div className="cm-sheet-row">
                    <div className="cm-field">
                      <label htmlFor={`${uid}-when`}>Run</label>
                      <select
                        id={`${uid}-when`}
                        className="cm-select"
                        value={when}
                        onChange={(e) => setWhen(e.target.value as typeof when)}
                      >
                        <option value="now">As soon as a worker is free</option>
                        <option value="countdown">After a delay</option>
                        <option value="eta">At a specific time</option>
                      </select>
                    </div>
                    {when === "countdown" && (
                      <div className="cm-field">
                        <label htmlFor={`${uid}-countdown`}>Delay (seconds)</label>
                        <input
                          id={`${uid}-countdown`}
                          className="cm-input"
                          type="number"
                          min={0}
                          value={countdown}
                          aria-invalid={scheduleError ? true : undefined}
                          onChange={(e) => setCountdown(e.target.value)}
                        />
                      </div>
                    )}
                    {when === "eta" && (
                      <div className="cm-field">
                        <label htmlFor={`${uid}-eta`}>Run at (your local time)</label>
                        <input
                          id={`${uid}-eta`}
                          className="cm-input"
                          type="datetime-local"
                          value={eta}
                          aria-invalid={scheduleError ? true : undefined}
                          onChange={(e) => setEta(e.target.value)}
                        />
                      </div>
                    )}
                  </div>
                  {scheduleError && (
                    <p className="cm-field-error" role="alert">
                      {scheduleError}
                    </p>
                  )}
                </>
              )}
            </>
          )}

          {submitError && (
            <p className="cm-error" role="alert">
              {submitError}
            </p>
          )}

          <footer className="cm-sheet-footer">
            <button type="button" className="cm-btn cm-btn-outline" onClick={onClose} disabled={submitting}>
              Cancel
            </button>
            <button type="submit" className="cm-btn cm-btn-primary" disabled={!canSubmit} aria-busy={submitting}>
              {submitting ? "Dispatching…" : isRetry ? "Retry with these arguments" : "Start task"}
            </button>
          </footer>
        </form>
      </div>
    </div>
  );
}
