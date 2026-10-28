import "../styles/users.css";

import { useEffect, useId, useRef } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import { IconX } from "./icons";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * A small centred dialog: overlay, Escape to close, focus kept inside, focus
 * handed back on close and page scroll locked. `busy` blocks every way of
 * closing it while a request is in flight, so a half-sent change can't be
 * walked away from.
 */
export function Modal({
  title,
  description,
  busy = false,
  onClose,
  children,
}: {
  title: string;
  description?: string;
  busy?: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  const uid = useId();
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    // First field if there is one, otherwise the dialog itself.
    const first = dialogRef.current?.querySelector<HTMLElement>("input, select, textarea");
    (first ?? dialogRef.current)?.focus();
    return () => {
      document.body.style.overflow = overflow;
      previous?.focus?.();
    };
  }, []);

  function onKeyDown(event: KeyboardEvent) {
    if (event.key === "Escape") {
      event.stopPropagation();
      if (!busy) onClose();
      return;
    }
    if (event.key !== "Tab" || !dialogRef.current) return;
    const items = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE));
    if (items.length === 0) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <div className="cm-modal-root" onKeyDown={onKeyDown}>
      <div className="cm-modal-overlay" aria-hidden="true" onClick={() => !busy && onClose()} />
      <div
        className="cm-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${uid}-title`}
        tabIndex={-1}
        ref={dialogRef}
      >
        <header className="cm-modal-header">
          <div>
            <h2 id={`${uid}-title`} className="cm-modal-title">
              {title}
            </h2>
            {description && <p className="cm-modal-desc">{description}</p>}
          </div>
          <button
            type="button"
            className="cm-btn cm-btn-ghost cm-btn-icon"
            aria-label="Close"
            onClick={onClose}
            disabled={busy}
          >
            <IconX />
          </button>
        </header>
        {children}
      </div>
    </div>
  );
}
