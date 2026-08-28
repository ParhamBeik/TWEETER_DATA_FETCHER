// Composing an X search query, and the vocabulary for the knobs around it.
//
// The raw query is still the source of truth -- the engine sends exactly what is
// stored -- but almost nobody remembers X's operator syntax, and a text box that
// silently accepts anything gives no feedback until a run comes back empty. The
// builder writes the operators; the preview shows what will actually be sent, so
// the string stays inspectable and editable rather than hidden behind a form.

/** Compose a raw X query from the builder's fields. */
export function buildQuery({ terms = "", from = "", language = "", minFaves = "", since = "", until = "" }) {
  const parts = [];
  const cleanTerms = terms.trim();
  if (cleanTerms) parts.push(cleanTerms);

  const handles = from
    .split(/[\s,]+/)
    .map((handle) => handle.replace(/^@/, "").trim())
    .filter(Boolean);
  if (handles.length === 1) parts.push(`from:${handles[0]}`);
  // X has no from: list operator, so several accounts become an OR group.
  if (handles.length > 1) parts.push(`(${handles.map((h) => `from:${h}`).join(" OR ")})`);

  if (language.trim()) parts.push(`lang:${language.trim()}`);
  const faves = Number(minFaves);
  if (faves > 0) parts.push(`min_faves:${Math.floor(faves)}`);
  if (since) parts.push(`since:${since}`);
  if (until) parts.push(`until:${until}`);
  return parts.join(" ");
}

export const PRODUCTS = [
  { value: "Top", label: "Top" },
  { value: "Latest", label: "Latest" },
];

export const PRODUCT_HINT = {
  Top: "X's own ranking. Fewer results, weighted to engagement — good for finding the loudest post about something.",
  Latest: "Reverse chronological. Everything X will serve, newest first — good for watching a topic as it happens.",
};

export const DEPTHS = [1, 2, 3];

// Every knob says what it does to a run, because none of these are guessable
// from their names and an unexplained control is one nobody touches.
export const HINTS = {
  depth:
    "How many pages to pull per run. Page 1 comes over HTTP; deeper pages need a browser and cost minutes each.",
  rolling:
    "How far back a run keeps paging. It stops once results are older than this, so a query spanning months needs a wider window than a day.",
  interval: "How often this query runs on its own, once it has run for the first time.",
};

export const INTERVALS = [
  { value: 900, label: "15 min" },
  { value: 1800, label: "30 min" },
  { value: 3600, label: "1 hour" },
  { value: 21600, label: "6 hours" },
  { value: 86400, label: "Daily" },
];

export const ROLLING_HOURS = [
  { value: 6, label: "6 hours" },
  { value: 24, label: "24 hours" },
  { value: 72, label: "3 days" },
  { value: 168, label: "1 week" },
];
