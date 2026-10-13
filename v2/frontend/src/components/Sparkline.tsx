import styles from "./Sparkline.module.css";

/**
 * A tiny line chart drawn as SVG presentation attributes (no style attribute, so it is fine under
 * the production CSP). `values` are plotted left to right between 0 and `max` (default: their
 * own maximum). The label is read by screen readers, with the latest value.
 */
export function Sparkline({
  values,
  max,
  label,
  unit = "",
}: {
  values: number[];
  max?: number;
  label: string;
  unit?: string;
}) {
  const width = 120;
  const height = 32;
  const top = max ?? Math.max(1, ...values);
  const step = values.length > 1 ? width / (values.length - 1) : width;
  const points = values
    .map((value, index) => {
      const y = height - 2 - (Math.min(Math.max(value, 0), top) / top) * (height - 4);
      return `${(index * step).toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const last = values[values.length - 1];
  const text = last === undefined ? `${label}: no data` : `${label}: ${last.toFixed(1)}${unit}`;
  return (
    <svg
      className={styles.spark}
      viewBox={`0 0 ${String(width)} ${String(height)}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={text}
    >
      <title>{text}</title>
      <line x1="0" x2={width} y1={height - 1} y2={height - 1} className={styles.base} />
      {values.length > 1 ? <polyline points={points} className={styles.line} fill="none" /> : null}
    </svg>
  );
}
