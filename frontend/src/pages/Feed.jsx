import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, authorizedFetch } from "../api";
import {
  AccountPicker,
  Segmented,
  ToggleChips,
  useAccounts,
} from "../filters";
import InfiniteSentinel from "../InfiniteSentinel";
import TweetCard from "../TweetCard";

const SORTS = [
  { value: "latest", label: "Latest" },
  { value: "top", label: "Most engaged" },
];

const WINDOWS = [
  { value: "1h", label: "1h" },
  { value: "24h", label: "Today" },
  { value: "7d", label: "Week" },
  { value: "30d", label: "Month" },
  { value: "", label: "All time" },
];

const POST_TYPES = [
  { value: "tweet", label: "Posts" },
  { value: "reply", label: "Replies" },
  { value: "retweet", label: "Reposts" },
  { value: "quote", label: "Quotes" },
];

const DEFAULTS = {
  sort: "latest",
  window: "24h",
  q: "",
  types: [],
  accounts: [],
  tier: "",
  has_media: false,
};

/** Read the whole filter state out of the URL, so a view is shareable. */
function readFilters(params) {
  return {
    sort: params.get("sort") || DEFAULTS.sort,
    window: params.has("window") ? params.get("window") : DEFAULTS.window,
    q: params.get("q") || "",
    types: (params.get("types") || "").split(",").filter(Boolean),
    accounts: params.getAll("account"),
    tier: params.get("tier") || "",
    has_media: params.get("has_media") === "1",
  };
}

function feedQuery(filters) {
  const params = new URLSearchParams();
  if (filters.sort !== "latest") params.set("sort", filters.sort);
  if (filters.window) params.set("window", filters.window);
  if (filters.q) params.set("q", filters.q);
  if (filters.types.length) params.set("types", filters.types.join(","));
  if (filters.tier) params.set("tier", filters.tier);
  if (filters.has_media) params.set("has_media", "1");
  for (const handle of filters.accounts) params.append("account", handle);
  return params;
}

export default function Feed() {
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = readFilters(searchParams);
  const accounts = useAccounts();

  const [tweets, setTweets] = useState([]);
  const [next, setNext] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [pending, setPending] = useState([]);
  const [draftQuery, setDraftQuery] = useState(filters.q);

  // Monotonic id for the active query. A page-append that is still in flight when
  // the user re-filters belongs to the previous query, so its rows must be
  // discarded rather than concatenated onto the new result set.
  const queryId = useRef(0);
  const inFlight = useRef(false);
  // Latest rendered rows, for the poll below to diff against without reaching
  // into a state updater.
  const shown = useRef(tweets);
  shown.current = tweets;
  const key = feedQuery(filters).toString();

  const load = useCallback(
    async (url) => {
      // Only the infinite-scroll append is de-duplicated. Guarding the filter path
      // too meant a Filter submitted while page 1 was still loading was dropped
      // silently, and the feed stayed empty until the user acted again.
      if (url && inFlight.current) return;
      const id = url ? queryId.current : (queryId.current += 1);
      inFlight.current = true;
      setLoading(true);
      try {
        const path = url ? url.replace(/^.*\/api/, "") : `/feed/${key ? `?${key}` : ""}`;
        const data = await api(path);
        if (id !== queryId.current) return; // superseded by a newer query
        setTweets((prev) => (url ? [...prev, ...data.results] : data.results));
        setNext(data.next);
        setError("");
      } catch (e) {
        if (id === queryId.current) setError(e.message);
      } finally {
        if (id === queryId.current) {
          inFlight.current = false;
          setLoading(false);
        }
      }
    },
    [key],
  );

  useEffect(() => {
    setTweets([]);
    setNext(null);
    setPending([]);
    load();
  }, [load]);

  // Poll for arrivals and hold them behind a pill rather than shifting the list
  // under the reader's cursor. Only meaningful for the chronological sort.
  useEffect(() => {
    if (filters.sort !== "latest") return undefined;
    const timer = setInterval(async () => {
      try {
        const data = await api(`/feed/${key ? `?${key}` : ""}`);
        // Read the current rows off a ref rather than from inside a setTweets
        // updater: an updater must be pure, and StrictMode double-invokes it,
        // so setting other state in there fires twice per poll.
        const known = new Set(shown.current.map((t) => t.id));
        setPending((data.results || []).filter((t) => !known.has(t.id)));
      } catch {
        /* a transient blip must not clear the feed the reader is looking at */
      }
    }, 30000);
    return () => clearInterval(timer);
  }, [key, filters.sort]);

  function update(patch) {
    const nextFilters = { ...filters, ...patch };
    const params = new URLSearchParams();
    if (nextFilters.sort !== DEFAULTS.sort) params.set("sort", nextFilters.sort);
    if (nextFilters.window !== DEFAULTS.window) params.set("window", nextFilters.window);
    if (nextFilters.q) params.set("q", nextFilters.q);
    if (nextFilters.types.length) params.set("types", nextFilters.types.join(","));
    if (nextFilters.tier) params.set("tier", nextFilters.tier);
    if (nextFilters.has_media) params.set("has_media", "1");
    for (const handle of nextFilters.accounts) params.append("account", handle);
    setSearchParams(params, { replace: true });
  }

  function showPending() {
    setTweets((current) => [...pending, ...current]);
    setPending([]);
  }

  async function downloadExport(fmt) {
    const params = feedQuery(filters);
    params.set("format", fmt);
    let url;
    try {
      const res = await authorizedFetch(`/api/export/?${params}`);
      if (!res.ok) {
        // Previously any non-OK read "Export failed" with no cause, and a network
        // rejection escaped as an unhandled promise with no message at all.
        setError(`Export failed (${res.status} ${res.statusText || "error"}).`);
        return;
      }
      const blob = await res.blob();
      url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `tweets.${fmt}`;
      link.click();
      setError("");
    } catch {
      setError("Export failed — the API is unreachable.");
    } finally {
      // Revoking synchronously after click() can cancel the download before the
      // browser has read the blob; defer to the next task instead.
      if (url) setTimeout(() => URL.revokeObjectURL(url), 0);
    }
  }

  return (
    <section>
      <header className="page-head">
        <div>
          <p className="eyebrow">Feed</p>
          <h2 className="page-title">The captured stream</h2>
        </div>
      </header>

      <div className="control-bar">
        <Segmented
          label="Sort"
          options={SORTS}
          value={filters.sort}
          onChange={(sort) => update({ sort })}
        />
        <Segmented
          label="Time window"
          options={WINDOWS}
          value={filters.window}
          onChange={(window) => update({ window })}
        />
        <AccountPicker
          accounts={accounts}
          selected={filters.accounts}
          onChange={(next) => update({ accounts: next })}
        />
        <select
          aria-label="Tier"
          value={filters.tier}
          onChange={(e) => update({ tier: e.target.value })}
        >
          <option value="">all tiers</option>
          {[1, 2, 3, 4, 5, 6, 7].map((n) => (
            <option key={n} value={n}>
              P{n}
            </option>
          ))}
        </select>
        <form
          className="search-field"
          onSubmit={(e) => {
            e.preventDefault();
            update({ q: draftQuery });
          }}
        >
          <input
            aria-label="Search archive"
            placeholder="search archive"
            value={draftQuery}
            onChange={(e) => setDraftQuery(e.target.value)}
          />
        </form>
      </div>

      <div className="control-bar control-bar-secondary">
        <ToggleChips
          label="Post types"
          options={POST_TYPES}
          values={filters.types}
          onChange={(types) => update({ types })}
        />
        <button
          type="button"
          aria-pressed={filters.has_media}
          className={filters.has_media ? "chip active" : "chip"}
          onClick={() => update({ has_media: !filters.has_media })}
        >
          🖼 Media only
        </button>
        <span className="control-spacer" />
        <button type="button" className="link" onClick={() => downloadExport("jsonl")}>
          Export JSONL
        </button>
        <button type="button" className="link" onClick={() => downloadExport("csv")}>
          Export CSV
        </button>
      </div>

      {error && <p className="error">{error}</p>}
      <div className="feed-column">
        {pending.length > 0 && (
          <button type="button" className="new-posts-pill" onClick={showPending}>
            ↑ {pending.length} new post{pending.length === 1 ? "" : "s"}
          </button>
        )}
        {tweets.map((t) => (
          <TweetCard key={t.id} tweet={t} />
        ))}
        <InfiniteSentinel next={next} loading={loading} onLoad={load} />
        {!loading && !tweets.length && !error && (
          <p className="muted">No posts match these filters.</p>
        )}
      </div>
    </section>
  );
}
