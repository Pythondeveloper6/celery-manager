import "../styles/retention.css";

import { useCallback, useEffect, useId, useState } from "react";
import type { FormEvent } from "react";
import { getRetention, previewRetention, saveRetention } from "../api/retention";
import type { Days, RetentionPreview, RetentionRules, RetentionSettings } from "../api/retention";
import { needsRole, useCan } from "../lib/role";

// The form edits strings, so an empty box can mean "keep forever" and a half
// typed number is not silently coerced.
interface Override {
  task: string;
  days: string;
  forever: boolean;
}

interface Draft {
  default_days: string;
  failed_days: string;
  audit_days: string;
  overrides: Override[];
}

function toDraft(s: RetentionRules): Draft {
  const text = (d: Days) => (d === null ? "" : String(d));
  return {
    default_days: text(s.default_days),
    failed_days: text(s.failed_days),
    audit_days: text(s.audit_days),
    overrides: Object.entries(s.tasks).map(([task, days]) => ({ task, days: text(days), forever: days === null })),
  };
}

function parseDays(label: string, value: string): Days {
  const trimmed = value.trim();
  if (trimmed === "") return null;
  if (!/^\d+$/.test(trimmed)) throw new Error(`${label} must be a whole number of days, or empty to keep forever.`);
  return Number(trimmed);
}

function toRules(draft: Draft): RetentionRules {
  const tasks: Record<string, Days> = {};
  for (const row of draft.overrides) {
    if (!row.task) throw new Error("Pick a task for every override, or remove the row.");
    if (row.task in tasks) throw new Error(`${row.task} has more than one override.`);
    const days = row.forever ? null : parseDays(`Days for ${row.task}`, row.days);
    if (!row.forever && days === null) throw new Error(`Enter days for ${row.task}, or tick Keep forever.`);
    tasks[row.task] = days;
  }
  return {
    default_days: parseDays("Keep runs for", draft.default_days),
    failed_days: parseDays("Keep failed runs for", draft.failed_days),
    audit_days: parseDays("Keep the audit log for", draft.audit_days),
    tasks,
  };
}

function when(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

export function RetentionSettingsCard() {
  const canEdit = useCan("admin");
  const [stored, setStored] = useState<RetentionSettings | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"save" | "preview" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [preview, setPreview] = useState<RetentionPreview | null>(null);

  const headingId = useId();
  const uid = useId();

  const load = useCallback(() => {
    setLoadError(null);
    getRetention()
      .then((s) => {
        setStored(s);
        setDraft(toDraft(s));
      })
      .catch((err) => setLoadError(err instanceof Error ? err.message : "Couldn't load the retention settings."));
  }, []);

  useEffect(load, [load]);

  function edit(change: (d: Draft) => Draft) {
    setDraft((d) => (d ? change(d) : d));
    // A preview or confirmation describes the form as it was.
    setPreview(null);
    setSaved(false);
    setError(null);
  }

  function editRow(index: number, patch: Partial<Override>) {
    edit((d) => ({ ...d, overrides: d.overrides.map((o, i) => (i === index ? { ...o, ...patch } : o)) }));
  }

  async function run(kind: "save" | "preview") {
    if (!draft) return;
    setError(null);
    let rules: RetentionRules;
    try {
      rules = toRules(draft);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Check the form.");
      return;
    }
    setBusy(kind);
    try {
      if (kind === "preview") {
        setPreview(await previewRetention(rules));
      } else {
        const next = await saveRetention(rules);
        setStored(next);
        setDraft(toDraft(next));
        setPreview(null);
        setSaved(true);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setBusy(null);
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    void run("save");
  }

  const used = new Set(draft?.overrides.map((o) => o.task));
  const offered = (stored?.known_tasks ?? []).filter((t) => !used.has(t));
  const keepingEverything =
    draft !== null &&
    draft.default_days.trim() === "" &&
    draft.audit_days.trim() === "" &&
    draft.overrides.every((o) => o.forever);

  return (
    <section className="cm-card cm-settings-section" aria-labelledby={headingId}>
      <div className="cm-card-header">
        <h2 className="cm-card-title" id={headingId}>
          Data retention
        </h2>
        <p className="cm-card-description">
          <span className="cm-scope-tag">Deployment-wide.</span>{" "}
          <span>
            Delete runs, with their logs, once they are older than a number of days. Empty means keep forever,
            which is the default. Queued and running tasks are never deleted before they have been idle for the
            whole period.
          </span>
        </p>
      </div>

      {loadError ? (
        <div className="cm-stack">
          <p className="cm-error" role="alert">
            {loadError}
          </p>
          <button type="button" className="cm-btn cm-btn-outline" onClick={load}>
            Try again
          </button>
        </div>
      ) : !draft || !stored ? (
        <p className="cm-loading">
          <span className="cm-live-dot" aria-hidden="true" />
          Loading retention settings&hellip;
        </p>
      ) : (
        <form className="cm-settings-form" onSubmit={handleSubmit} noValidate>
          {!canEdit && <p className="cm-field-hint">Only an admin can change data retention.</p>}
          <fieldset className="cm-fieldset" disabled={busy !== null || !canEdit}>
            <legend className="cm-visually-hidden">Data retention</legend>

            <div className="cm-retention-grid">
              <div className="cm-field">
                <label htmlFor={`${uid}-default`}>Keep runs for (days)</label>
                <input
                  id={`${uid}-default`}
                  className="cm-input cm-retention-days"
                  inputMode="numeric"
                  value={draft.default_days}
                  placeholder="Forever"
                  onChange={(e) => edit((d) => ({ ...d, default_days: e.target.value }))}
                />
                <p className="cm-field-hint">For every task without its own rule below.</p>
              </div>
              <div className="cm-field">
                <label htmlFor={`${uid}-failed`}>Keep failed runs for (days)</label>
                <input
                  id={`${uid}-failed`}
                  className="cm-input cm-retention-days"
                  inputMode="numeric"
                  value={draft.failed_days}
                  placeholder="Same as above"
                  onChange={(e) => edit((d) => ({ ...d, failed_days: e.target.value }))}
                />
                <p className="cm-field-hint">Optional. It can only keep failures longer, so it must be at least the number before it.</p>
              </div>
              <div className="cm-field">
                <label htmlFor={`${uid}-audit`}>Keep the audit log for (days)</label>
                <input
                  id={`${uid}-audit`}
                  className="cm-input cm-retention-days"
                  inputMode="numeric"
                  value={draft.audit_days}
                  placeholder="Forever"
                  onChange={(e) => edit((d) => ({ ...d, audit_days: e.target.value }))}
                />
              </div>
            </div>

            <div className="cm-field">
              <span id={`${uid}-overrides`}>Per-task rules</span>
              {draft.overrides.length === 0 ? (
                <p className="cm-field-hint">No overrides. Every task follows the default.</p>
              ) : (
                <div className="cm-retention-overrides" role="group" aria-labelledby={`${uid}-overrides`}>
                  {draft.overrides.map((row, index) => (
                    <div className="cm-retention-row" key={index}>
                      <select
                        className="cm-select"
                        aria-label="Task"
                        value={row.task}
                        onChange={(e) => editRow(index, { task: e.target.value })}
                      >
                        <option value="">Choose a task&hellip;</option>
                        {/* The row's own task stays selectable even if it no longer exists anywhere. */}
                        {row.task && !offered.includes(row.task) && <option value={row.task}>{row.task}</option>}
                        {offered.map((t) => (
                          <option key={t} value={t}>
                            {t}
                          </option>
                        ))}
                      </select>
                      <input
                        className="cm-input"
                        inputMode="numeric"
                        aria-label={`Days to keep ${row.task || "this task"}`}
                        value={row.forever ? "" : row.days}
                        disabled={row.forever}
                        placeholder="days"
                        onChange={(e) => editRow(index, { days: e.target.value })}
                      />
                      <label className="cm-retention-forever">
                        <input
                          type="checkbox"
                          checked={row.forever}
                          onChange={(e) => editRow(index, { forever: e.target.checked })}
                        />
                        Keep forever
                      </label>
                      <button
                        type="button"
                        className="cm-btn cm-btn-sm cm-btn-outline"
                        onClick={() => edit((d) => ({ ...d, overrides: d.overrides.filter((_, i) => i !== index) }))}
                      >
                        Remove
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <div>
                <button
                  type="button"
                  className="cm-btn cm-btn-sm cm-btn-secondary"
                  onClick={() => edit((d) => ({ ...d, overrides: [...d.overrides, { task: "", days: "", forever: false }] }))}
                >
                  Add a task rule
                </button>
              </div>
            </div>
          </fieldset>

          {error && (
            <p className="cm-error" role="alert">
              {error}
            </p>
          )}
          {saved && (
            <p className="cm-notice" role="status">
              Retention saved. It applies the next time a prune runs.
            </p>
          )}

          {preview && (
            <p className="cm-notice" role="status">
              {preview.runs === 0 && preview.audit_log === 0
                ? "Nothing would be deleted right now."
                : `A prune now would delete ${preview.runs} run${preview.runs === 1 ? "" : "s"}${
                    preview.audit_log > 0 ? ` and ${preview.audit_log} audit log entries` : ""
                  }${
                    Object.keys(preview.by_task).length > 0
                      ? `: ${Object.entries(preview.by_task)
                          .sort((a, b) => b[1] - a[1])
                          .slice(0, 8)
                          .map(([task, n]) => `${task} (${n})`)
                          .join(", ")}`
                      : ""
                  }.`}
            </p>
          )}

          <div className="cm-settings-actions">
            {/* Preview works for any role: it deletes nothing. */}
            <button type="button" className="cm-btn cm-btn-secondary" disabled={busy !== null} onClick={() => void run("preview")}>
              {busy === "preview" ? "Counting…" : "Preview what would be deleted"}
            </button>
            {canEdit ? (
              <button type="submit" className="cm-btn cm-btn-primary" disabled={busy !== null}>
                {busy === "save" ? "Saving…" : "Save retention"}
              </button>
            ) : (
              <span className="cm-field-hint">{needsRole("admin")} to save.</span>
            )}
            {keepingEverything && <span className="cm-field-hint">Nothing is deleted while every field is empty.</span>}
          </div>

          <div>
            <h3 className="cm-card-title">Last prune</h3>
            {stored.last_prune ? (
              <dl className="cm-retention-summary">
                <dt>When</dt>
                <dd>
                  {when(stored.last_prune.at)} by {stored.last_prune.by}
                </dd>
                <dt>Deleted</dt>
                <dd>
                  {stored.last_prune.runs_deleted} run{stored.last_prune.runs_deleted === 1 ? "" : "s"}
                  {stored.last_prune.audit_deleted > 0 && `, ${stored.last_prune.audit_deleted} audit log entries`}
                </dd>
              </dl>
            ) : (
              <p className="cm-field-hint">
                No prune has run yet. Run <code>celery-manager prune</code>, or schedule the built-in task (see the data
                retention docs).
              </p>
            )}
          </div>
        </form>
      )}
    </section>
  );
}
