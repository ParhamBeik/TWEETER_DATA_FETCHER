// Chart tokens, kept in one place so every chart reads as one system.
//
// These are the reference dark-mode categorical steps, validated against this
// app's chart surface (#121a2d) with the dataviz validator: lightness band,
// chroma floor, CVD separation, normal-vision floor and contrast all pass
// (worst adjacent CVD ΔE 8.4, worst adjacent normal-vision ΔE 19.3).
// The teal UI accent (--accent) is deliberately NOT used for data marks -- at
// OKLCH L 0.85 it sits far outside the dark-mode band. It stays UI chrome.
//
// Slots are assigned in fixed order and never cycled. A chart that would need a
// sixth series folds the tail into "other" instead of inventing a hue.
export const SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"];

// Status is a reserved palette: it never doubles as "series 4", and it always
// ships with a label, never colour alone. Contrast on #121a2d: 5.17 / 9.45 /
// 6.57 / 3.61, all clear of 3:1.
export const STATUS = {
  good: "#0ca30c",
  warning: "#fab219",
  serious: "#ec835a",
  critical: "#d03b3b",
};

// FetchRun.status -> status role. `partial` is a warning rather than a failure:
// the run did collect something, it just could not prove it finished.
export const RUN_STATUS_COLOR = {
  completed: STATUS.good,
  partial: STATUS.warning,
  running: "#3987e5",
  failed: STATUS.critical,
  auth_required: STATUS.serious,
};

// Which pipeline captured a tweet. Fixed so the colour follows the subsystem,
// never its rank in a filtered result -- hiding "search" must not repaint live.
export const SUBSYSTEM_COLOR = {
  live: SERIES[0],
  historical: SERIES[1],
  search: SERIES[2],
  unknown: "#5b6b8c",
};

export const SUBSYSTEM_LABEL = {
  live: "Live poll",
  historical: "Archive walk",
  search: "Saved search",
  unknown: "Before tracking",
};

export const AXIS = "#91a1bd";
export const SURFACE = "#121a2d";
export const LINE = "#26334d";

// 4px rounded data-ends anchored to the baseline (vertical vs horizontal bars).
export const BAR_RADIUS_Y = [4, 4, 0, 0];
export const BAR_RADIUS_X = [0, 4, 4, 0];

export const TOOLTIP_STYLE = {
  background: SURFACE,
  border: `1px solid ${LINE}`,
  borderRadius: 8,
  color: "#e5edf9",
};

export const AXIS_PROPS = {
  stroke: AXIS,
  tick: { fill: AXIS, fontSize: 12 },
  tickLine: false,
};

// A 2px surface gap between stacked segments, per the mark spec. Recharts wants
// this as a stroke matching the surface rather than a real gap.
export const STACK_GAP = { stroke: SURFACE, strokeWidth: 2 };

/** Axis/tooltip label for a bucket timestamp, at the granularity being charted. */
export function bucketLabel(iso, bucket) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  if (bucket === "hour") {
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

/**
 * Pivot the API's long-format series (one row per bucket per key) into the
 * wide rows Recharts stacks, filling absent keys with 0 so a gap in one series
 * does not shift the stack.
 */
export function pivotSeries(rows, keyField, valueField = "count") {
  const byBucket = new Map();
  const keys = new Set();
  for (const row of rows || []) {
    const key = row[keyField] || "unknown";
    keys.add(key);
    const entry = byBucket.get(row.bucket) || { bucket: row.bucket };
    entry[key] = (entry[key] || 0) + (row[valueField] || 0);
    byBucket.set(row.bucket, entry);
  }
  const ordered = [...byBucket.values()].sort(
    (a, b) => new Date(a.bucket) - new Date(b.bucket),
  );
  for (const entry of ordered) {
    for (const key of keys) if (!(key in entry)) entry[key] = 0;
  }
  return { rows: ordered, keys: [...keys] };
}
