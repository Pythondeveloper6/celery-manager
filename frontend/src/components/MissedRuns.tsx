import { useEffect, useState } from "react";
import { getMissedRuns, type MissedRuns as MissedRunsData } from "../api/schedules";
import "../styles/missed-runs.css";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

function formatDue(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function formatAgo(value: string): string {
  const diff = Date.now() - new Date(value).getTime();
  if (Number.isNaN(diff)) return "";
  if (diff < HOUR) return `${Math.max(1, Math.round(diff / MINUTE))} min ago`;
  if (diff < DAY) return `${Math.round(diff / HOUR)} h ago`;
  return `${Math.round(diff / DAY)} d ago`;
}

// Runs that were due in the last 7 days but never reached a worker. Newest
// first, since the latest gap is what someone opening the page wants to see.
export function MissedRuns({ name }: { name: string }) {
  const [data, setData] = useState<MissedRunsData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError(null);
    getMissedRuns(name)
      .then((result) => {
        if (!cancelled) setData(result);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Couldn't load missed runs.");
      });
    return () => {
      cancelled = true;
    };
  }, [name]);

  return (
    <section className="cm-missed-section" aria-labelledby="cm-missed-heading">
      <h2 id="cm-missed-heading" className="cm-card-title">
        Missed runs
      </h2>
      {error ? (
        <p className="cm-error" role="alert">
          {error}
        </p>
      ) : data === null ? (
        <p className="cm-loading">
          <span className="cm-live-dot" aria-hidden="true" />
          Checking for missed runs&hellip;
        </p>
      ) : !data.active ? (
        <div className="cm-card">
          <p className="cm-empty-state">
            Detection isn&apos;t active yet: beat hasn&apos;t stamped a run of this schedule. Run beat with{" "}
            <code>-S celery_manager.beat:Scheduler</code> (and make sure the task uses <code>@track</code>).
          </p>
        </div>
      ) : data.missed.length === 0 ? (
        <div className="cm-card">
          <p className="cm-empty-state">
            No missed runs in the last 7 days. Detection has been active since {formatDue(data.tracking_since ?? "")}; a
            run counts as missed {Math.round(data.grace_seconds / 60)} min after it was due.
          </p>
        </div>
      ) : (
        <ul className="cm-card cm-card-flush cm-missed-list">
          {[...data.missed].reverse().map((m) => (
            <li key={m.due_at} className="cm-missed-row">
              <span className="cm-occurrence-time">{formatDue(m.due_at)}</span>
              <span className="cm-missed-ago">{formatAgo(m.due_at)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
