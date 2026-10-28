import "../styles/task-call.css";
import "../styles/schedule-editor.css";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import {
  previewSchedule,
  revertSchedule,
  saveSchedule,
  type ScheduleSpec,
  type ScheduleSummary,
} from "../api/schedules";
import { formatPausedBy } from "../lib/pausedBy";
import { IconX } from "./icons";

// "Edit schedule": change a schedule's crontab or interval, with the next
// five runs previewed live (the server computes them, with Celery's own
// parser, exactly as it will project them once saved). Same side sheet as
// TaskCallDialog, and like it the dialog makes its own API calls.

interface Props {
  schedule: ScheduleSummary;
  onClose: () => void;
  // Called after a save or a revert with the schedule's new summary and a
  // sentence for the page's notice.
  onSaved: (updated: ScheduleSummary, notice: string) => void;
}

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

const CRON_FIELDS = [
  { key: "minute", label: "Minute", hint: "0-59" },
  { key: "hour", label: "Hour", hint: "0-23" },
  { key: "day_of_month", label: "Day of month", hint: "1-31" },
  { key: "month_of_year", label: "Month", hint: "1-12" },
  { key: "day_of_week", label: "Day of week", hint: "0-6 or mon-sun" },
] as const;

type CronKey = (typeof CRON_FIELDS)[number]["key"];
type CronFields = Record<CronKey, string>;

const UNITS = [
  { value: 86400, label: "days" },
  { value: 3600, label: "hours" },
  { value: 60, label: "minutes" },
  { value: 1, label: "seconds" },
] as const;

const DEFAULT_CRON: CronFields = { minute: "0", hour: "*", day_of_month: "*", month_of_year: "*", day_of_week: "*" };

// Wait this long after the last keystroke before asking for a preview.
const PREVIEW_DEBOUNCE_MS = 350;

/** The largest unit the interval is a whole number of, so 7200s reads "2 hours". */
function splitSeconds(seconds: number): { amount: string; unit: number } {
  const unit = UNITS.find((u) => seconds >= u.value && seconds % u.value === 0)?.value ?? 1;
  return { amount: String(seconds / unit), unit };
}

function formatRun(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: parsed.getSeconds() ? "2-digit" : undefined,
  });
}

export function ScheduleEditor({ schedule, onClose, onSaved }: Props) {
  const uid = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const firstFieldRef = useRef<HTMLSelectElement>(null);

  // Start from the saved edit, else from what the code says.
  const initial = schedule.schedule_override ?? schedule.code_schedule;
  const [kind, setKind] = useState<ScheduleSpec["type"]>(initial?.type ?? "crontab");
  const [cron, setCron] = useState<CronFields>(
    initial?.type === "crontab"
      ? {
          minute: initial.minute,
          hour: initial.hour,
          day_of_month: initial.day_of_month,
          month_of_year: initial.month_of_year,
          day_of_week: initial.day_of_week,
        }
      : DEFAULT_CRON,
  );
  const initialInterval = initial?.type === "interval" ? splitSeconds(initial.seconds) : { amount: "1", unit: 3600 };
  const [amount, setAmount] = useState(initialInterval.amount);
  const [unit, setUnit] = useState<number>(initialInterval.unit);

  const [preview, setPreview] = useState<string[] | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [busy, setBusy] = useState<"save" | "revert" | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // The spec the form describes, or a sentence saying why it doesn't yet.
  const draft = useMemo((): { spec?: ScheduleSpec; error?: string } => {
    if (kind === "crontab") return { spec: { type: "crontab", ...cron } };
    const n = Number(amount);
    if (amount.trim() === "" || !Number.isFinite(n) || n <= 0) return { error: "Enter how often it runs, a number above 0." };
    return { spec: { type: "interval", seconds: n * unit } };
  }, [kind, cron, amount, unit]);

  const specKey = draft.spec ? JSON.stringify(draft.spec) : null;

  useEffect(() => {
    firstFieldRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!draft.spec) {
      setPreview(null);
      setPreviewError(null);
      setPreviewing(false);
      return;
    }
    const spec = draft.spec;
    const controller = new AbortController();
    setPreviewing(true);
    const timer = setTimeout(() => {
      previewSchedule(schedule.name, spec, controller.signal)
        .then((times) => {
          setPreview(times);
          setPreviewError(null);
        })
        .catch((err) => {
          if (controller.signal.aborted) return;
          setPreview(null);
          setPreviewError(err instanceof Error ? err.message : "Couldn't preview this schedule.");
        })
        .finally(() => {
          if (!controller.signal.aborted) setPreviewing(false);
        });
    }, PREVIEW_DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
    // specKey stands in for draft.spec: re-run only when the spec's content changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [specKey, schedule.name]);

  const editingOff = !schedule.schedule_editing;
  const canSave = !editingOff && !busy && !!draft.spec && !previewError && !previewing;

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!canSave || !draft.spec) return;
    setBusy("save");
    setSubmitError(null);
    try {
      const saved = await saveSchedule(schedule.name, draft.spec);
      const stale = saved.unmatched_overrides.length;
      onSaved(
        saved,
        stale
          ? `Schedule saved. ${stale} skipped or cancelled run${stale === 1 ? " no longer lines" : "s no longer line"} up with it and won't apply unless you change it back.`
          : "Schedule saved. Beat picks it up within a few seconds.",
      );
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : "Couldn't save the schedule.");
      setBusy(null);
    }
  }

  async function revert() {
    setBusy("revert");
    setSubmitError(null);
    try {
      const updated = await revertSchedule(schedule.name);
      onSaved(updated, "Schedule reverted to the code's.");
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : "Couldn't revert the schedule.");
      setBusy(null);
    }
  }

  function onKeyDown(event: React.KeyboardEvent) {
    if (event.key === "Escape") {
      event.stopPropagation();
      if (!busy) onClose();
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

  const edited = formatPausedBy(schedule.edited_by, schedule.edited_at);

  return (
    <div className="cm-sheet-root" onKeyDown={onKeyDown}>
      <div className="cm-sheet-overlay" onClick={() => !busy && onClose()} aria-hidden="true" />
      <div className="cm-sheet" role="dialog" aria-modal="true" aria-labelledby={`${uid}-title`} ref={dialogRef}>
        <header className="cm-sheet-header">
          <div>
            <h2 id={`${uid}-title`} className="cm-sheet-title">
              Edit schedule
            </h2>
            <p className="cm-sheet-desc">
              {schedule.name}. In the code: <code>{schedule.code_schedule_spec}</code>
            </p>
          </div>
          <button type="button" className="cm-btn cm-btn-ghost cm-btn-icon" onClick={onClose} aria-label="Close">
            <IconX />
          </button>
        </header>

        <form className="cm-sheet-body" onSubmit={save} noValidate>
          {editingOff && (
            <div className="cm-alert" role="note">
              <p>
                Editing is off. Beat only applies an edit when it runs <code>celery_manager.beat:DatabaseScheduler</code>.
                Once it does, set <code>schedule_editing=True</code> in the celery-manager config. You can still preview
                a schedule here.
              </p>
            </div>
          )}

          {schedule.schedule_override && (
            <p className="cm-field-hint">
              Edited in the dashboard{edited ? ` ${edited}` : ""}. Reverting runs it as the code says again.
            </p>
          )}

          <div className="cm-field">
            <label htmlFor={`${uid}-kind`}>Runs</label>
            <select
              id={`${uid}-kind`}
              ref={firstFieldRef}
              className="cm-select"
              value={kind}
              onChange={(e) => setKind(e.target.value as ScheduleSpec["type"])}
            >
              <option value="crontab">On a crontab</option>
              <option value="interval">Every fixed interval</option>
            </select>
          </div>

          {kind === "crontab" ? (
            <fieldset className="cm-schedule-cron">
              <legend className="cm-visually-hidden">Crontab fields</legend>
              {CRON_FIELDS.map((f) => (
                <div className="cm-field" key={f.key}>
                  <label htmlFor={`${uid}-${f.key}`}>{f.label}</label>
                  <input
                    id={`${uid}-${f.key}`}
                    className="cm-input cm-input-code"
                    value={cron[f.key]}
                    placeholder="*"
                    spellCheck={false}
                    autoComplete="off"
                    onChange={(e) => setCron((prev) => ({ ...prev, [f.key]: e.target.value }))}
                  />
                  <p className="cm-field-hint">{f.hint}</p>
                </div>
              ))}
            </fieldset>
          ) : (
            <div className="cm-sheet-row">
              <div className="cm-field">
                <label htmlFor={`${uid}-amount`}>Every</label>
                <input
                  id={`${uid}-amount`}
                  className="cm-input"
                  type="number"
                  min={1}
                  step="any"
                  value={amount}
                  aria-invalid={draft.error ? true : undefined}
                  onChange={(e) => setAmount(e.target.value)}
                />
              </div>
              <div className="cm-field">
                <label htmlFor={`${uid}-unit`}>Unit</label>
                <select
                  id={`${uid}-unit`}
                  className="cm-select"
                  value={unit}
                  onChange={(e) => setUnit(Number(e.target.value))}
                >
                  {UNITS.map((u) => (
                    <option key={u.value} value={u.value}>
                      {u.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          )}
          {kind === "interval" && (
            <p className="cm-field-hint">The next run is counted from the last one, not from when you save.</p>
          )}

          <section className="cm-schedule-preview" aria-live="polite" aria-labelledby={`${uid}-preview`}>
            <h3 id={`${uid}-preview`} className="cm-sheet-label">
              Next five runs
            </h3>
            {draft.error ? (
              <p className="cm-field-error" role="alert">
                {draft.error}
              </p>
            ) : previewError ? (
              <p className="cm-field-error" role="alert">
                {previewError}
              </p>
            ) : preview === null ? (
              <p className="cm-loading">
                <span className="cm-live-dot" aria-hidden="true" />
                Working out the next runs&hellip;
              </p>
            ) : (
              <ol className="cm-schedule-preview-list" data-stale={previewing || undefined}>
                {preview.map((t) => (
                  <li key={t}>{formatRun(t)}</li>
                ))}
              </ol>
            )}
          </section>

          {submitError && (
            <p className="cm-error" role="alert">
              {submitError}
            </p>
          )}

          <footer className="cm-sheet-footer">
            {schedule.schedule_override && (
              <button
                type="button"
                className="cm-btn cm-btn-ghost cm-schedule-revert"
                onClick={revert}
                disabled={!!busy}
                aria-busy={busy === "revert"}
              >
                {busy === "revert" ? "Reverting…" : "Revert to code"}
              </button>
            )}
            <button type="button" className="cm-btn cm-btn-outline" onClick={onClose} disabled={!!busy}>
              Cancel
            </button>
            <button
              type="submit"
              className="cm-btn cm-btn-primary"
              disabled={!canSave}
              aria-busy={busy === "save"}
              title={editingOff ? "Schedule editing is off" : undefined}
            >
              {busy === "save" ? "Saving…" : "Save schedule"}
            </button>
          </footer>
        </form>
      </div>
    </div>
  );
}
