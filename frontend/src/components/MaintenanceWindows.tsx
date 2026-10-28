import "../styles/maintenance.css";

import { useCallback, useEffect, useId, useMemo, useState } from "react";
import type { FormEvent } from "react";
import {
  createMaintenanceWindow,
  deleteMaintenanceWindow,
  listMaintenanceWindows,
  previewMaintenanceWindow,
  updateMaintenanceWindow,
  type MaintenanceWindow,
  type MaintenanceWindowInput,
  type WindowSpan,
} from "../api/maintenance";
import { needsRole, useCan } from "../lib/role";
import { Modal } from "./Modal";

// "Maintenance windows": a recurring weekly span in which chosen schedules'
// firings are skipped, with nothing to resume afterwards. Lives under the
// schedules table. Viewers see the windows; operators create, edit and delete.

// Index = the API's day number (0 = Monday).
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const PREVIEW_DEBOUNCE_MS = 350;

function formatDuration(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  if (h === 0) return `${m} min`;
  return m === 0 ? `${h} h` : `${h} h ${m} min`;
}

function formatDays(days: number[]): string {
  if (days.length === 7) return "Every day";
  return days.map((d) => DAYS[d]).join(", ");
}

/** A moment in the window's own timezone, so the preview matches what the operator typed. */
function formatIn(value: string, timeZone: string, withDate = true): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const options: Intl.DateTimeFormatOptions = withDate
    ? { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }
    : { hour: "2-digit", minute: "2-digit", hour12: false };
  try {
    return date.toLocaleString(undefined, { ...options, timeZone });
  } catch {
    return date.toLocaleString(undefined, options);
  }
}

function timezones(extra: string): string[] {
  let all: string[] = [];
  try {
    all = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf?.("timeZone") ?? [];
  } catch {
    all = [];
  }
  return all.includes(extra) ? all : [extra, ...all];
}

function WindowDialog({
  existing,
  defaultTimezone,
  scheduleNames,
  onClose,
  onSaved,
}: {
  existing: MaintenanceWindow | null;
  defaultTimezone: string;
  scheduleNames: string[];
  onClose: () => void;
  onSaved: (message: string) => void;
}) {
  const uid = useId();
  const [name, setName] = useState(existing?.name ?? "");
  const [days, setDays] = useState<number[]>(existing?.days_of_week ?? [6]);
  const [start, setStart] = useState(existing?.start_time ?? "02:00");
  const initialHours = existing ? existing.duration_minutes % 60 === 0 : true;
  const [unit, setUnit] = useState<60 | 1>(initialHours ? 60 : 1);
  const [amount, setAmount] = useState(
    existing ? String(existing.duration_minutes / (initialHours ? 60 : 1)) : "2",
  );
  const [timezone, setTimezone] = useState(existing?.timezone ?? defaultTimezone);
  const [all, setAll] = useState(existing ? existing.applies_to === "*" : true);
  const [chosen, setChosen] = useState<string[]>(
    existing && existing.applies_to !== "*" ? existing.applies_to : [],
  );
  const [enabled, setEnabled] = useState(existing?.enabled ?? true);

  const [preview, setPreview] = useState<WindowSpan[] | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const minutes = Math.round(Number(amount) * unit);
  const durationOk = amount.trim() !== "" && Number.isFinite(minutes) && minutes >= 1;
  const recurrenceError =
    days.length === 0 ? "Choose at least one day." : !start ? "Choose a start time." : !durationOk ? "Enter how long it lasts." : null;
  const recurrenceKey = recurrenceError ? null : JSON.stringify([days, start, minutes, timezone]);

  useEffect(() => {
    if (!recurrenceKey) {
      setPreview(null);
      setPreviewError(null);
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => {
      previewMaintenanceWindow(
        { days_of_week: days, start_time: start, duration_minutes: minutes, timezone },
        controller.signal,
      )
        .then((spans) => {
          setPreview(spans);
          setPreviewError(null);
        })
        .catch((err) => {
          if (controller.signal.aborted) return;
          setPreview(null);
          setPreviewError(err instanceof Error ? err.message : "Couldn't preview this window.");
        });
    }, PREVIEW_DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
    // recurrenceKey stands in for the four fields it is made from.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recurrenceKey]);

  const appliesOk = all || chosen.length > 0;
  const canSubmit = !busy && name.trim() !== "" && !recurrenceError && appliesOk && !previewError;
  const zones = useMemo(() => timezones(timezone), [timezone]);

  function toggleDay(day: number) {
    setDays((prev) => (prev.includes(day) ? prev.filter((d) => d !== day) : [...prev, day].sort()));
  }

  function toggleSchedule(schedule: string) {
    setChosen((prev) => (prev.includes(schedule) ? prev.filter((s) => s !== schedule) : [...prev, schedule]));
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    const input: MaintenanceWindowInput = {
      name: name.trim(),
      days_of_week: days,
      start_time: start,
      duration_minutes: minutes,
      timezone,
      applies_to: all ? "*" : chosen,
      enabled,
    };
    try {
      if (existing) await updateMaintenanceWindow(existing.id, input);
      else await createMaintenanceWindow(input);
      onSaved(existing ? "Maintenance window saved." : "Maintenance window created.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't save the maintenance window.");
      setBusy(false);
    }
  }

  return (
    <Modal
      title={existing ? "Edit maintenance window" : "New maintenance window"}
      description="Runs of the chosen schedules that are due inside the window are skipped. Nothing needs resuming afterwards."
      busy={busy}
      onClose={onClose}
    >
      <form className="cm-modal-body cm-mw-form" onSubmit={submit} noValidate>
        <div className="cm-field">
          <label htmlFor={`${uid}-name`}>Name</label>
          <input
            id={`${uid}-name`}
            className="cm-input"
            value={name}
            maxLength={255}
            autoComplete="off"
            onChange={(e) => setName(e.target.value)}
            disabled={busy}
          />
        </div>

        <fieldset className="cm-mw-fieldset">
          <legend className="cm-mw-legend">Days</legend>
          <div className="cm-mw-days">
            {DAYS.map((label, day) => (
              <button
                key={label}
                type="button"
                className="cm-btn cm-btn-outline cm-btn-sm cm-mw-day"
                aria-pressed={days.includes(day)}
                disabled={busy}
                onClick={() => toggleDay(day)}
              >
                {label}
              </button>
            ))}
          </div>
        </fieldset>

        <div className="cm-mw-row">
          <div className="cm-field">
            <label htmlFor={`${uid}-start`}>Starts at</label>
            <input
              id={`${uid}-start`}
              className="cm-input"
              type="time"
              value={start}
              onChange={(e) => setStart(e.target.value)}
              disabled={busy}
            />
          </div>
          <div className="cm-field">
            <label htmlFor={`${uid}-amount`}>Lasts</label>
            <div className="cm-mw-duration">
              <input
                id={`${uid}-amount`}
                className="cm-input"
                type="number"
                min={1}
                step="any"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                disabled={busy}
              />
              <select
                className="cm-select"
                aria-label="Duration unit"
                value={unit}
                onChange={(e) => setUnit(Number(e.target.value) as 60 | 1)}
                disabled={busy}
              >
                <option value={60}>hours</option>
                <option value={1}>minutes</option>
              </select>
            </div>
          </div>
        </div>

        <div className="cm-field">
          <label htmlFor={`${uid}-tz`}>Timezone</label>
          <input
            id={`${uid}-tz`}
            className="cm-input"
            list={`${uid}-zones`}
            value={timezone}
            spellCheck={false}
            autoComplete="off"
            onChange={(e) => setTimezone(e.target.value)}
            disabled={busy}
          />
          <datalist id={`${uid}-zones`}>
            {zones.map((z) => (
              <option key={z} value={z} />
            ))}
          </datalist>
          <p className="cm-field-hint">An IANA name. Daylight saving changes follow it; the window keeps its length.</p>
        </div>

        <fieldset className="cm-mw-fieldset">
          <legend className="cm-mw-legend">Applies to</legend>
          <label className="cm-mw-check">
            <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} disabled={busy} />
            <span>All schedules</span>
          </label>
          {!all &&
            (scheduleNames.length === 0 ? (
              <p className="cm-field-hint">No schedules are configured.</p>
            ) : (
              <div className="cm-mw-schedules">
                {scheduleNames.map((s) => (
                  <label className="cm-mw-check" key={s}>
                    <input
                      type="checkbox"
                      checked={chosen.includes(s)}
                      onChange={() => toggleSchedule(s)}
                      disabled={busy}
                    />
                    <span>{s}</span>
                  </label>
                ))}
              </div>
            ))}
          {!all && chosen.length === 0 && scheduleNames.length > 0 && (
            <p className="cm-field-hint">Choose at least one schedule.</p>
          )}
        </fieldset>

        <label className="cm-mw-check">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} disabled={busy} />
          <span>Enabled</span>
        </label>

        <section className="cm-schedule-preview" aria-live="polite" aria-labelledby={`${uid}-preview`}>
          <h3 id={`${uid}-preview`} className="cm-sheet-label">
            Next windows
          </h3>
          {recurrenceError ? (
            <p className="cm-field-hint">{recurrenceError}</p>
          ) : previewError ? (
            <p className="cm-field-error" role="alert">
              {previewError}
            </p>
          ) : preview === null ? (
            <p className="cm-loading">
              <span className="cm-live-dot" aria-hidden="true" />
              Working out the next windows&hellip;
            </p>
          ) : (
            <ol className="cm-schedule-preview-list">
              {preview.map((span) => (
                <li key={span.start}>
                  {formatIn(span.start, timezone)} to {formatIn(span.end, timezone, false)}
                </li>
              ))}
            </ol>
          )}
        </section>

        {error && (
          <p className="cm-error" role="alert">
            {error}
          </p>
        )}
        <div className="cm-modal-actions">
          <button type="button" className="cm-btn cm-btn-outline" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="cm-btn cm-btn-primary" disabled={!canSubmit} aria-busy={busy}>
            {busy ? "Saving…" : existing ? "Save window" : "Create window"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function DeleteDialog({
  target,
  onClose,
  onDeleted,
}: {
  target: MaintenanceWindow;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      await deleteMaintenanceWindow(target.id);
      onDeleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't delete the maintenance window.");
      setBusy(false);
    }
  }

  return (
    <Modal
      title="Delete maintenance window"
      description={`Runs inside "${target.name}" will no longer be skipped.`}
      busy={busy}
      onClose={onClose}
    >
      <div className="cm-modal-body">
        {error && (
          <p className="cm-error" role="alert">
            {error}
          </p>
        )}
        <div className="cm-modal-actions">
          <button type="button" className="cm-btn cm-btn-outline" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="button" className="cm-btn cm-btn-primary" onClick={confirm} disabled={busy} aria-busy={busy}>
            {busy ? "Deleting…" : "Delete window"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

export function MaintenanceWindows({
  scheduleNames,
  onChanged,
}: {
  scheduleNames: string[];
  // The schedule list's chips depend on the windows: tell the page to reload.
  onChanged: () => void;
}) {
  const canOperate = useCan("operator");
  const [windows, setWindows] = useState<MaintenanceWindow[] | null>(null);
  const [defaultTimezone, setDefaultTimezone] = useState("UTC");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [editing, setEditing] = useState<MaintenanceWindow | "new" | null>(null);
  const [deleting, setDeleting] = useState<MaintenanceWindow | null>(null);

  const refresh = useCallback(async () => {
    try {
      const list = await listMaintenanceWindows();
      setWindows(list.windows);
      setDefaultTimezone(list.default_timezone);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't load maintenance windows.");
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  function changed(message: string) {
    setEditing(null);
    setDeleting(null);
    setNotice(message);
    refresh();
    onChanged();
  }

  return (
    <section className="cm-mw" aria-labelledby="cm-mw-heading">
      <div className="cm-mw-head">
        <div>
          <h2 className="cm-card-title" id="cm-mw-heading">
            Maintenance windows
          </h2>
          <p className="cm-page-lede">
            Skip chosen schedules on a recurring window, such as every Sunday from 02:00 to 04:00. Beat keeps sending;
            there is nothing to resume.
          </p>
        </div>
        {canOperate ? (
          <button type="button" className="cm-btn cm-btn-primary" onClick={() => setEditing("new")}>
            New window
          </button>
        ) : (
          <button type="button" className="cm-btn cm-btn-primary" disabled title={needsRole("operator")}>
            New window
          </button>
        )}
      </div>

      {notice && (
        <p className="cm-notice" role="status">
          {notice}
        </p>
      )}

      <div className="cm-card cm-card-flush">
        {error ? (
          <div className="cm-card-pad cm-stack">
            <p className="cm-error" role="alert">
              {error}
            </p>
            <button type="button" className="cm-btn cm-btn-outline" onClick={refresh}>
              Try again
            </button>
          </div>
        ) : windows === null ? (
          <div className="cm-card-pad">
            <p className="cm-loading">
              <span className="cm-live-dot" aria-hidden="true" />
              Loading maintenance windows&hellip;
            </p>
          </div>
        ) : windows.length === 0 ? (
          <p className="cm-empty-state">No maintenance windows yet.</p>
        ) : (
          <div className="cm-table-scroll">
            <table className="cm-table cm-table-stack">
              <thead>
                <tr>
                  <th scope="col">Window</th>
                  <th scope="col">When</th>
                  <th scope="col">Applies to</th>
                  <th scope="col" className="cm-col-fit">
                    State
                  </th>
                  <th scope="col" className="cm-col-fit">
                    <span className="cm-visually-hidden">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {windows.map((w) => (
                  <tr key={w.id}>
                    <td className="cm-cell-primary">{w.name}</td>
                    <td>
                      {formatDays(w.days_of_week)} at {w.start_time} for {formatDuration(w.duration_minutes)}
                      <span className="cm-pause-state-by cm-mw-sub">{w.timezone}</span>
                      {w.enabled && w.next_start && (
                        <span className="cm-pause-state-by cm-mw-sub">
                          {w.active ? "Until" : "Next"}: {formatIn(w.active ? (w.next_end ?? w.next_start) : w.next_start, w.timezone)}
                        </span>
                      )}
                    </td>
                    <td>{w.applies_to === "*" ? "All schedules" : w.applies_to.join(", ")}</td>
                    <td className="cm-col-fit">
                      {!w.enabled ? (
                        <span className="cm-badge">Disabled</span>
                      ) : w.active ? (
                        <span className="cm-badge cm-mw-active">In progress</span>
                      ) : (
                        <span className="cm-schedule-enabled">Scheduled</span>
                      )}
                    </td>
                    <td className="cm-col-fit">
                      {canOperate && (
                        <div className="cm-mw-actions">
                          <button type="button" className="cm-btn cm-btn-outline cm-btn-sm" onClick={() => setEditing(w)}>
                            Edit
                          </button>
                          <button type="button" className="cm-btn cm-btn-outline cm-btn-sm" onClick={() => setDeleting(w)}>
                            Delete
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {editing && (
        <WindowDialog
          existing={editing === "new" ? null : editing}
          defaultTimezone={defaultTimezone}
          scheduleNames={scheduleNames}
          onClose={() => setEditing(null)}
          onSaved={changed}
        />
      )}
      {deleting && (
        <DeleteDialog
          target={deleting}
          onClose={() => setDeleting(null)}
          onDeleted={() => changed("Maintenance window deleted.")}
        />
      )}
    </section>
  );
}
