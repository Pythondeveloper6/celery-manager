/** "failed" -> "Failed". Statuses arrive lowercase from the API; the UI is
 *  sentence case everywhere, and doing it here rather than with
 *  `text-transform` keeps the accessible name and the visible word identical. */
export function statusLabel(status: string): string {
  return status ? status.charAt(0).toUpperCase() + status.slice(1) : status;
}
