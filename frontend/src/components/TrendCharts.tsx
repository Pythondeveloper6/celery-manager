import { useEffect, useMemo, useRef, useState } from "react";
import type { Bucket, Overview } from "../api/analytics";
import { formatBucket, niceMax } from "../lib/insights";
import { statusLabel } from "../lib/status";
import { formatDuration } from "../lib/taskProgress";

/* Charts are plain SVG sized to their container (a ResizeObserver feeds the
   pixel width in), so text stays a readable 11px at 390px instead of being
   scaled down with a viewBox. Colours come from CSS (insights.css) so the
   same markup follows light and dark. Every chart has a text summary and a
   collapsible data table, so nothing is conveyed by colour or shape alone. */

// Stack order, bottom to top: the outcome people care about sits at the base.
const STACK_ORDER = ["succeeded", "failed", "retried", "revoked", "skipped", "started", "queued"];

const HEIGHT = 200;
const MARGIN = { top: 8, right: 8, bottom: 24, left: 52 };

function useWidth(): [React.RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(640);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWidth(Math.max(240, Math.floor(el.clientWidth)));
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => setWidth(Math.max(240, Math.floor(el.clientWidth))));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

function xTickIndexes(count: number, width: number): number[] {
  // About one label per 90px keeps them from touching on a phone.
  const wanted = Math.max(2, Math.min(6, Math.floor(width / 90)));
  if (count <= wanted) return Array.from({ length: count }, (_, i) => i);
  const step = (count - 1) / (wanted - 1);
  return Array.from({ length: wanted }, (_, i) => Math.round(i * step));
}

function Legend({ items }: { items: { key: string; label: string; status?: string; cls?: string }[] }) {
  return (
    <ul className="cm-ch-legend">
      {items.map((item) => (
        <li key={item.key}>
          <span className={`cm-ch-swatch ${item.cls ?? ""}`} data-status={item.status} aria-hidden="true" />
          {item.label}
        </li>
      ))}
    </ul>
  );
}

function bucketSummary(b: Bucket, range: Overview["range"]): string {
  const parts = STACK_ORDER.filter((s) => b.counts[s]).map((s) => `${b.counts[s]} ${s}`);
  return `${formatBucket(b.start, range)}: ${b.total} run${b.total === 1 ? "" : "s"}${parts.length ? ` (${parts.join(", ")})` : ""}`;
}

export function RunsChart({ data }: { data: Overview }) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState<number | null>(null);
  const { buckets, range } = data;

  const statuses = useMemo(() => {
    const present = new Set(buckets.flatMap((b) => Object.keys(b.counts)));
    const known = STACK_ORDER.filter((s) => present.has(s));
    return [...known, ...[...present].filter((s) => !STACK_ORDER.includes(s))];
  }, [buckets]);

  const maxTotal = Math.max(0, ...buckets.map((b) => b.total));
  const yMax = niceMax(maxTotal);
  const plotW = width - MARGIN.left - MARGIN.right;
  const plotH = HEIGHT - MARGIN.top - MARGIN.bottom;
  const slot = plotW / buckets.length;
  const barW = Math.max(1, slot - (slot > 8 ? 2 : 1));
  const y = (v: number) => MARGIN.top + plotH - (v / yMax) * plotH;
  const peak = buckets.reduce((best, b) => (b.total > best.total ? b : best), buckets[0]);

  const summary =
    maxTotal === 0
      ? "No runs in this period."
      : `${data.totals.runs} runs across ${buckets.length} intervals; the busiest was ${bucketSummary(peak, range)}.`;

  const hovered = hover !== null ? buckets[hover] : null;

  return (
    <figure className="cm-chart">
      <figcaption className="cm-chart-title">Runs by status</figcaption>
      <p className="cm-chart-readout" aria-hidden="true">
        {hovered ? bucketSummary(hovered, range) : "Hover a bar for details."}
      </p>
      <div ref={ref} className="cm-chart-frame">
        <svg
          width={width}
          height={HEIGHT}
          role="img"
          aria-label={`Stacked bar chart of runs by status. ${summary}`}
          onPointerLeave={() => setHover(null)}
          onPointerMove={(e) => {
            const rect = e.currentTarget.getBoundingClientRect();
            const i = Math.floor((e.clientX - rect.left - MARGIN.left) / slot);
            setHover(i >= 0 && i < buckets.length ? i : null);
          }}
        >
          {[0, 0.5, 1].map((f) => (
            <g key={f}>
              <line className="cm-ch-grid" x1={MARGIN.left} x2={width - MARGIN.right} y1={y(yMax * f)} y2={y(yMax * f)} />
              <text className="cm-ch-tick" x={MARGIN.left - 6} y={y(yMax * f)} textAnchor="end" dominantBaseline="middle">
                {Math.round(yMax * f * 10) / 10}
              </text>
            </g>
          ))}
          {buckets.map((b, i) => {
            const x = MARGIN.left + i * slot + (slot - barW) / 2;
            let acc = 0;
            return (
              <g key={b.start}>
                {hover === i && <rect className="cm-ch-hover" x={MARGIN.left + i * slot} y={MARGIN.top} width={slot} height={plotH} />}
                {statuses.map((s) => {
                  const n = b.counts[s] ?? 0;
                  if (!n) return null;
                  const top = y(acc + n);
                  const h = y(acc) - top;
                  acc += n;
                  return <rect key={s} className="cm-ch-bar" data-status={s} x={x} y={top} width={barW} height={Math.max(h - 0.5, 0.5)} rx={1} />;
                })}
              </g>
            );
          })}
          {xTickIndexes(buckets.length, width).map((i) => (
            <text
              key={i}
              className="cm-ch-tick"
              x={MARGIN.left + i * slot + slot / 2}
              y={HEIGHT - 6}
              textAnchor={i === 0 ? "start" : i === buckets.length - 1 ? "end" : "middle"}
            >
              {formatBucket(buckets[i].start, range)}
            </text>
          ))}
        </svg>
      </div>
      <Legend items={statuses.map((s) => ({ key: s, label: statusLabel(s), status: s, cls: "cm-ch-swatch-status" }))} />
      <p className="cm-chart-summary">{summary}</p>
      <details className="cm-chart-table">
        <summary>View runs as a table</summary>
        <div className="cm-table-scroll">
          <table className="cm-table">
            <thead>
              <tr>
                <th scope="col">Interval start</th>
                <th scope="col">Runs</th>
                {statuses.map((s) => (
                  <th key={s} scope="col">
                    {statusLabel(s)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {buckets.map((b) => (
                <tr key={b.start}>
                  <th scope="row">{formatBucket(b.start, range)}</th>
                  <td>{b.total}</td>
                  {statuses.map((s) => (
                    <td key={s}>{b.counts[s] ?? 0}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}

export function RuntimeChart({ data }: { data: Overview }) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState<number | null>(null);
  const { buckets, range } = data;
  const maxV = Math.max(0, ...buckets.map((b) => b.runtime_p95 ?? 0));
  const yMax = niceMax(maxV);
  const plotW = width - MARGIN.left - MARGIN.right;
  const plotH = HEIGHT - MARGIN.top - MARGIN.bottom;
  const slot = plotW / buckets.length;
  const x = (i: number) => MARGIN.left + i * slot + slot / 2;
  const y = (v: number) => MARGIN.top + plotH - (v / yMax) * plotH;

  // A bucket with no finished runs breaks the line instead of dipping to zero.
  const path = (pick: (b: Bucket) => number | null) => {
    let d = "";
    let pen = false;
    buckets.forEach((b, i) => {
      const v = pick(b);
      if (v === null) {
        pen = false;
        return;
      }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  };

  const withData = buckets.filter((b) => b.runtime_p95 !== null);
  const worst = withData.reduce<Bucket | null>((best, b) => (best === null || (b.runtime_p95 ?? 0) > (best.runtime_p95 ?? 0) ? b : best), null);
  const summary = worst
    ? `Overall p95 runtime ${formatDuration(data.totals.runtime_p95)}, median ${formatDuration(data.totals.runtime_p50)}. Slowest interval: ${formatBucket(worst.start, range)} at ${formatDuration(worst.runtime_p95)}.`
    : "No finished runs with a recorded runtime in this period.";
  const hovered = hover !== null ? buckets[hover] : null;

  return (
    <figure className="cm-chart">
      <figcaption className="cm-chart-title">Runtime</figcaption>
      <p className="cm-chart-readout" aria-hidden="true">
        {hovered
          ? `${formatBucket(hovered.start, range)}: p95 ${formatDuration(hovered.runtime_p95)}, median ${formatDuration(hovered.runtime_p50)}`
          : "Hover the chart for details."}
      </p>
      <div ref={ref} className="cm-chart-frame">
        <svg
          width={width}
          height={HEIGHT}
          role="img"
          aria-label={`Line chart of p95 runtime. ${summary}`}
          onPointerLeave={() => setHover(null)}
          onPointerMove={(e) => {
            const rect = e.currentTarget.getBoundingClientRect();
            const i = Math.floor((e.clientX - rect.left - MARGIN.left) / slot);
            setHover(i >= 0 && i < buckets.length ? i : null);
          }}
        >
          {[0, 0.5, 1].map((f) => (
            <g key={f}>
              <line className="cm-ch-grid" x1={MARGIN.left} x2={width - MARGIN.right} y1={y(yMax * f)} y2={y(yMax * f)} />
              <text className="cm-ch-tick" x={MARGIN.left - 6} y={y(yMax * f)} textAnchor="end" dominantBaseline="middle">
                {f === 0 ? "0" : formatDuration(yMax * f)}
              </text>
            </g>
          ))}
          {hover !== null && <line className="cm-ch-cursor" x1={x(hover)} x2={x(hover)} y1={MARGIN.top} y2={MARGIN.top + plotH} />}
          <path className="cm-ch-line cm-ch-line-p50" d={path((b) => b.runtime_p50)} />
          <path className="cm-ch-line cm-ch-line-p95" d={path((b) => b.runtime_p95)} />
          {buckets.map((b, i) =>
            b.runtime_p95 === null ? null : <circle key={b.start} className="cm-ch-dot" cx={x(i)} cy={y(b.runtime_p95)} r={buckets.length > 40 ? 1.5 : 2.5} />,
          )}
          {xTickIndexes(buckets.length, width).map((i) => (
            <text
              key={i}
              className="cm-ch-tick"
              x={x(i)}
              y={HEIGHT - 6}
              textAnchor={i === 0 ? "start" : i === buckets.length - 1 ? "end" : "middle"}
            >
              {formatBucket(buckets[i].start, range)}
            </text>
          ))}
        </svg>
      </div>
      <Legend
        items={[
          { key: "p95", label: "p95", cls: "cm-ch-swatch-p95" },
          { key: "p50", label: "Median (p50)", cls: "cm-ch-swatch-p50" },
        ]}
      />
      <p className="cm-chart-summary">{summary}</p>
      <details className="cm-chart-table">
        <summary>View runtimes as a table</summary>
        <div className="cm-table-scroll">
          <table className="cm-table">
            <thead>
              <tr>
                <th scope="col">Interval start</th>
                <th scope="col">Median</th>
                <th scope="col">p95</th>
                <th scope="col">Success rate</th>
              </tr>
            </thead>
            <tbody>
              {buckets.map((b) => (
                <tr key={b.start}>
                  <th scope="row">{formatBucket(b.start, range)}</th>
                  <td>{formatDuration(b.runtime_p50)}</td>
                  <td>{formatDuration(b.runtime_p95)}</td>
                  <td>{b.success_rate === null ? "—" : `${Math.round(b.success_rate * 100)}%`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}
