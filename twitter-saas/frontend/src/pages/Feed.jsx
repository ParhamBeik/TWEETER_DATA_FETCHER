import { useEffect, useState } from "react";
import { api, getToken } from "../api";
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

  async function load(url) {
    if (loading) return;
    setLoading(true);
    try {
      const path = url ? url.replace(/^.*\/api/, "") : feedPath(applied);
      const data = await api(path);
      setTweets((prev) => (url ? [...prev, ...data.results] : data.results));
      setNext(data.next);
      setError("");
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [applied]);

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
    const headers = {};
    const token = getToken();
    if (token) headers.Authorization = `Token ${token}`;
    const res = await fetch(`/api/export/?${params}`, { headers });
    if (!res.ok) {
      setError("Export failed");
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `tweets.${fmt}`;
    link.click();
    URL.revokeObjectURL(url);
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
          placeholder="search archive"
          value={filters.q}
          onChange={(e) => setFilters({ ...filters, q: e.target.value })}
        />
        <input
          placeholder="account"
          value={filters.account}
          onChange={(e) => setFilters({ ...filters, account: e.target.value })}
        />
        <select
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
        <input
          type="datetime-local"
          value={filters.since}
          onChange={(e) => setFilters({ ...filters, since: e.target.value })}
        />
        <input
          type="datetime-local"
          value={filters.until}
          onChange={(e) => setFilters({ ...filters, until: e.target.value })}
        />
        <input
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
