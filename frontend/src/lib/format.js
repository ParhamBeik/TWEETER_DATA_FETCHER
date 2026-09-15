// Formatting shared by the feed, the dashboard and the analytics pages.

/** 1234 -> "1.2K". Engagement counts run to millions and need to fit a chip. */
export function compact(value) {
  const n = Number(value) || 0;
  if (Math.abs(n) < 1000) return String(n);
  const units = [
    [1e9, "B"],
    [1e6, "M"],
    [1e3, "K"],
  ];
  for (const [size, suffix] of units) {
    if (Math.abs(n) >= size) {
      const scaled = n / size;
      // One decimal below 10 ("1.2K"), none above ("12K") -- keeps the width
      // stable enough for a metrics row that repeats down a long feed.
      return `${scaled >= 10 || scaled <= -10 ? Math.round(scaled) : scaled.toFixed(1)}${suffix}`;
    }
  }
  return String(n);
}

/** Signed count for a delta chip: "+18", "-4", "0". */
export function signed(value) {
  const n = Number(value) || 0;
  return `${n > 0 ? "+" : ""}${compact(n)}`;
}

const DIVISIONS = [
  [60, "second"],
  [60, "minute"],
  [24, "hour"],
  [7, "day"],
  [4.35, "week"],
  [12, "month"],
  [Infinity, "year"],
];

/** "3 minutes ago" / "in 4 hours". Falls back to "" on an unparseable value. */
export function relativeTime(value) {
  if (!value) return "";
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  let duration = (date.getTime() - Date.now()) / 1000;
  for (const [amount, unit] of DIVISIONS) {
    if (Math.abs(duration) < amount) return formatter.format(Math.round(duration), unit);
    duration /= amount;
  }
  return "";
}

/** "4m 12s" from a second count -- run durations and quota reset countdowns. */
export function duration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m ${total % 60}s`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours}h ${minutes % 60}m` : `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

/**
 * The canonical link to a post on X.
 *
 * `Tweet.url` is built by the engine and is normally present, but it is blank
 * when the tweet id was missing at parse time. Rebuilding from the handle and
 * id keeps "open the exact post" working rather than rendering a dead link.
 */
export function permalink(tweet) {
  if (!tweet) return "";
  if (tweet.url) return tweet.url;
  const handle = tweet.author?.handle || tweet.account;
  if (!handle || !tweet.tweet_id) return "";
  return `https://x.com/${handle}/status/${tweet.tweet_id}`;
}

/** Link to a specific status id when only the id and handle are known. */
export function statusLink(handle, tweetId) {
  return handle && tweetId ? `https://x.com/${handle}/status/${tweetId}` : "";
}

/** Absolute timestamp for a tooltip/`title`, local to the reader. */
// A fixed format rather than the browser's locale. "9/2/2026" is 9 February to
// most of the world and 2 September to the rest, and the windows these
// timestamps sit next to are Tehran calendar days -- an ambiguous date next to a
// day-bounded number is a reading error waiting to happen. The month name and a
// 24-hour clock cannot be misread; the time zone stays the reader's own.
const ABSOLUTE = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

export function absoluteTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : ABSOLUTE.format(date);
}
