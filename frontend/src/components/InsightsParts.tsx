import { RANGE_KEYS, type RangeKey } from "../api/analytics";
import { RANGE_LABEL } from "../lib/insights";

export function RangeTabs({ value, onChange }: { value: RangeKey; onChange: (r: RangeKey) => void }) {
  return (
    <div className="cm-tabs-list" role="group" aria-label="Time range">
      {RANGE_KEYS.map((r) => (
        <button key={r} type="button" className="cm-tabs-trigger" aria-pressed={r === value} aria-label={RANGE_LABEL[r]} onClick={() => onChange(r)}>
          {r}
        </button>
      ))}
    </div>
  );
}

export function Kpi({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "danger" }) {
  return (
    <div className="cm-card cm-kpi" data-tone={tone}>
      <p className="cm-kpi-label">{label}</p>
      <p className="cm-kpi-value">{value}</p>
      {sub && <p className="cm-kpi-sub">{sub}</p>}
    </div>
  );
}
