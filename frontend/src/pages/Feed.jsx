import { useCallback, useEffect, useRef, useState } from "react";
import { api, authorizedFetch } from "../api";
import InfiniteSentinel from "../InfiniteSentinel";
import RunStatus from "../RunStatus";
import TweetCard from "../TweetCard";

const emptyFilters = {
  q: "",
  account: "",
  tier: "",
  since: "",
  until: "",
  run_id: "",
};

function feedPath(filters) {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  const query = params.toString();
  return query ? `/feed/?${query}` : "/feed/";
}

export default function Feed() {
  const [tweets, setTweets] = useState([]);
  const [next, setNext] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [filters, setFilters] = useState(emptyFilters);
  const [applied, setApplied] = useState(emptyFilters);
  // Monotonic id for the active query. A page-append that is still in flight when
  // the user re-filters belongs to the previous query, so its rows must be
  // discarded rather than concatenated onto the new result set.
  const queryId = useRef(0);
  const inFlight = useRef(false);

  const load = useCallback(async (url) => {
    // Only the infinite-scroll append is de-duplicated. Guarding the filter path
    // too meant a Filter submitted while page 1 was still loading was dropped
    // silently, and the feed stayed empty until the user acted again.
    if (url && inFlight.current) return;
    const id = url ? queryId.current : (queryId.current += 1);
    inFlight.current = true;
    setLoading(true);
    try {
      const path = url ? url.replace(/^.*\/api/, "") : feedPath(applied);
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
  }, [applied]);

  useEffect(() => {
    load();
  }, [load]);

  function apply(e) {
    e.preventDefault();
    setTweets([]);
    setNext(null);
    setApplied({ ...filters });
  }

  async function downloadExport(fmt) {
    const params = new URLSearchParams();
    Object.entries(applied).forEach(([key, value]) => {
      if (value) params.set(key, value);
    });
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
      <header>
        <p className="eyebrow">Feed</p>
        <h2 className="page-title">The captured stream</h2>
      </header>
      <RunStatus />
      <form className="feed-filters" onSubmit={apply}>
        <input
          aria-label="Search archive"
          placeholder="search archive"
          value={filters.q}
          onChange={(e) => setFilters({ ...filters, q: e.target.value })}
        />
        <input
          aria-label="Account"
          placeholder="account"
          value={filters.account}
          onChange={(e) => setFilters({ ...filters, account: e.target.value })}
        />
        <select
          aria-label="Tier"
          value={filters.tier}
          onChange={(e) => setFilters({ ...filters, tier: e.target.value })}
        >
          <option value="">all tiers</option>
          {[1, 2, 3, 4, 5, 6, 7].map((n) => (
            <option key={n} value={n}>
              P{n}
            </option>
          ))}
        </select>
        {/* Two bare datetime-local inputs are indistinguishable: neither the
            placeholder attribute nor the native widget says which bound is which. */}
        <input
          aria-label="Posted after"
          title="Posted after"
          type="datetime-local"
          value={filters.since}
          onChange={(e) => setFilters({ ...filters, since: e.target.value })}
        />
        <input
          aria-label="Posted before"
          title="Posted before"
          type="datetime-local"
          value={filters.until}
          onChange={(e) => setFilters({ ...filters, until: e.target.value })}
        />
        <input
          aria-label="Run id"
          placeholder="run id"
          value={filters.run_id}
          onChange={(e) => setFilters({ ...filters, run_id: e.target.value })}
        />
        <button type="submit">Filter</button>
        <button type="button" className="link" onClick={() => downloadExport("jsonl")}>
          Export JSONL
        </button>
        <button type="button" className="link" onClick={() => downloadExport("csv")}>
          Export CSV
        </button>
      </form>
      {error && <p className="error">{error}</p>}
      {tweets.map((t) => (
        <TweetCard key={t.id} tweet={t} />
      ))}
      <InfiniteSentinel next={next} loading={loading} onLoad={load} />
      {!loading && !tweets.length && !error && <p className="muted">No tweets yet.</p>}
    </section>
  );
}
