import { statusLabel } from "../lib/status";

/** Dot + word. Colour comes from `data-status` in tokens.css, so a status this
 *  build doesn't know about still renders, just neutral. */
export function StatusBadge({ status }: { status: string }) {
  return (
    <span className="cm-status" data-status={status}>
      {statusLabel(status)}
    </span>
  );
}
