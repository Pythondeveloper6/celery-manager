import { useEffect, useId, useState } from "react";
import { bulkCancelTasks, type BulkCancelFilter, type BulkCancelResult } from "../api/tasks";
import { plural } from "../lib/bulkCancel";
import "../styles/bulk-cancel.css";

/* Bulk cancel controls for the task list. Kept out of TaskList.tsx so that
   page only has to say *what* is selected or filtered; the confirm steps,
   the dry-run count and the terminate opt-in all live here.

   Both controls confirm inline (never window.confirm) and default to a plain
   revoke: a queued run is skipped when a worker reaches it, one already
   running is left to finish. Stopping running work is a separate, explicit
   opt-in, because it kills worker processes mid-task. */

function TerminateOption({ checked, onChange }: { checked: boolean; onChange: (value: boolean) => void }) {
  const id = useId();
  return (
    <label className="cm-bulk-terminate" htmlFor={id}>
      <input id={id} type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      Also stop runs already executing
    </label>
  );
}

export function CancelSelected({
  taskIds,
  onDone,
  onError,
}: {
  taskIds: string[];
  onDone: (result: BulkCancelResult) => void;
  onError: (message: string) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [terminate, setTerminate] = useState(false);
  const [busy, setBusy] = useState(false);
  const count = taskIds.length;

  // A changed selection is a different question; don't carry a half-made
  // confirmation over to it.
  const selectionKey = taskIds.join(",");
  useEffect(() => {
    setConfirming(false);
    setTerminate(false);
  }, [selectionKey]);

  async function run() {
    setBusy(true);
    try {
      onDone(await bulkCancelTasks({ task_ids: taskIds }, { terminate }));
      setConfirming(false);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Bulk cancel failed.");
    } finally {
      setBusy(false);
    }
  }

  if (!confirming) {
    return (
      <button type="button" className="cm-btn cm-btn-destructive-outline cm-btn-sm" onClick={() => setConfirming(true)}>
        Cancel {count} selected
      </button>
    );
  }
  return (
    <div className="cm-bulk-confirm" role="group" aria-label="Confirm bulk cancel">
      <TerminateOption checked={terminate} onChange={setTerminate} />
      <button type="button" className="cm-btn cm-btn-outline cm-btn-sm" onClick={() => setConfirming(false)} disabled={busy}>
        Keep them
      </button>
      <button type="button" className="cm-btn cm-btn-destructive cm-btn-sm" onClick={run} disabled={busy} aria-busy={busy}>
        {busy ? "Cancelling…" : `${terminate ? "Stop" : "Cancel"} ${plural(count, "run")}`}
      </button>
    </div>
  );
}

type MatchingState =
  | { step: "idle" }
  | { step: "counting" }
  | { step: "confirm"; matched: number; limit: number }
  | { step: "running"; matched: number; limit: number };

export function CancelMatching({
  filter,
  onDone,
  onError,
}: {
  filter: BulkCancelFilter;
  onDone: (result: BulkCancelResult) => void;
  onError: (message: string) => void;
}) {
  const [state, setState] = useState<MatchingState>({ step: "idle" });
  const [terminate, setTerminate] = useState(false);

  // The count belongs to the filter it was taken under. Any filter change
  // drops the confirmation instead of offering to cancel a stale number.
  const filterKey = JSON.stringify(filter);
  useEffect(() => {
    setState({ step: "idle" });
    setTerminate(false);
  }, [filterKey]);

  async function count() {
    setState({ step: "counting" });
    try {
      const result = await bulkCancelTasks({ filter }, { dry_run: true });
      setState({ step: "confirm", matched: result.matched, limit: result.limit });
    } catch (err) {
      setState({ step: "idle" });
      onError(err instanceof Error ? err.message : "Couldn't count the matching runs.");
    }
  }

  async function run(matched: number, limit: number) {
    setState({ step: "running", matched, limit });
    try {
      onDone(await bulkCancelTasks({ filter }, { terminate }));
      setState({ step: "idle" });
    } catch (err) {
      setState({ step: "confirm", matched, limit });
      onError(err instanceof Error ? err.message : "Bulk cancel failed.");
    }
  }

  if (state.step === "idle" || state.step === "counting") {
    return (
      <button
        type="button"
        className="cm-btn cm-btn-ghost cm-btn-sm cm-bulk-matching-trigger"
        onClick={count}
        disabled={state.step === "counting"}
        aria-busy={state.step === "counting"}
      >
        {state.step === "counting" ? "Counting…" : "Cancel all matching…"}
      </button>
    );
  }

  const { matched, limit } = state;
  const busy = state.step === "running";
  return (
    <div className="cm-bulk-bar cm-bulk-matching" role="group" aria-label="Cancel all matching runs">
      {matched === 0 ? (
        <>
          <span className="cm-bulk-bar-label">No queued or running runs match these filters.</span>
          <button type="button" className="cm-btn cm-btn-outline cm-btn-sm" onClick={() => setState({ step: "idle" })}>
            Close
          </button>
        </>
      ) : matched > limit ? (
        <>
          <span className="cm-bulk-bar-label">
            {matched.toLocaleString()} runs match — more than the {limit.toLocaleString()} one bulk cancel allows.
            Narrow the filters and try again.
          </span>
          <button type="button" className="cm-btn cm-btn-outline cm-btn-sm" onClick={() => setState({ step: "idle" })}>
            Close
          </button>
        </>
      ) : (
        <>
          <span className="cm-bulk-bar-label">
            Cancel {plural(matched, "queued or running run")} matching these filters?
          </span>
          <div className="cm-bulk-confirm">
            <TerminateOption checked={terminate} onChange={setTerminate} />
            <button
              type="button"
              className="cm-btn cm-btn-outline cm-btn-sm"
              onClick={() => setState({ step: "idle" })}
              disabled={busy}
            >
              Keep them
            </button>
            <button
              type="button"
              className="cm-btn cm-btn-destructive cm-btn-sm"
              onClick={() => run(matched, limit)}
              disabled={busy}
              aria-busy={busy}
            >
              {busy ? "Cancelling…" : `${terminate ? "Stop" : "Cancel"} ${plural(matched, "run")}`}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
