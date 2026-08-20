import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import InfiniteSentinel from "../InfiniteSentinel";
import TweetCard from "../TweetCard";

function formatWhen(value) {
  return value ? new Date(value).toLocaleString() : "never";
}

export default function Accounts() {
  const [accounts, setAccounts] = useState([]);
  const [analytics, setAnalytics] = useState({});
  const [compare, setCompare] = useState([]);
  const [handle, setHandle] = useState("");
  const [priority, setPriority] = useState(7);
  const [selected, setSelected] = useState(null);
  const [tweets, setTweets] = useState([]);
  const [next, setNext] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  // Distinguishes "still fetching the roster" from "the roster is genuinely
  // empty"; without it the table flashed "No tracked accounts yet" on every load.
  const [rosterLoaded, setRosterLoaded] = useState(false);
  const activeHandle = useRef(null);

  async function loadAccounts() {
    try {
      const [data, metrics] = await Promise.all([api("/accounts/"), api("/analytics/accounts/")]);
      setAccounts(Array.isArray(data) ? data : data.results || []);
      setAnalytics(Object.fromEntries((metrics.results || []).map((row) => [row.account, row])));
    } catch (e) {
      setError(e.message);
    } finally {
      setRosterLoaded(true);
    }
  }

  useEffect(() => {
    loadAccounts();
  }, []);

  async function addAccount(e) {
    e.preventDefault();
    setError("");
    try {
      await api("/accounts/", {
        method: "POST",
        body: { handle: handle.replace(/^@/, ""), priority: Number(priority) },
      });
      setHandle("");
      setStatus("Account tracked; initial fetch queued.");
      loadAccounts();
    } catch (e) {
      setError(e.message);
    }
  }

  async function patch(h, body) {
    setError("");
    try {
      await api(`/accounts/${h}/`, { method: "PATCH", body });
      loadAccounts();
    } catch (e) {
      setError(e.message);
    }
  }

  async function fetchNow(h) {
    try {
      await api(`/accounts/${h}/fetch/`, { method: "POST" });
      setStatus(`Queued live + historical for @${h}.`);
    } catch (e) {
      setError(e.message);
    }
  }

  async function openTimeline(h, url) {
    // Only block duplicate page-appends. Blocking on `loading` outright meant
    // clicking a second account while the first timeline loaded did nothing.
    if (url && loading) return;
    if (!url) {
      setSelected(h);
      setTweets([]);
      setNext(null);
    }
    // Switching accounts mid-load must not let the slower response for the
    // previous handle paint into the newly selected timeline.
    activeHandle.current = h;
    setLoading(true);
    setError("");
    try {
      const path = url ? url.replace(/^.*\/api/, "") : `/accounts/${h}/tweets/`;
      const data = await api(path);
      if (activeHandle.current !== h) return;
      setTweets((prev) => (url ? [...prev, ...data.results] : data.results));
      setNext(data.next);
    } catch (e) {
      if (activeHandle.current === h) setError(e.message);
    } finally {
      if (activeHandle.current === h) setLoading(false);
    }
  }

  function toggleCompare(handle) {
    setCompare((current) => current.includes(handle)
      ? current.filter((item) => item !== handle)
      : [...current, handle].slice(-4));
  }

  return (
    <section className="accounts">
      <header>
        <p className="eyebrow">Accounts</p>
        <h2 className="page-title">Roster, tiers, and collection health</h2>
      </header>
      <form className="follow-form" onSubmit={addAccount}>
        <input
          aria-label="Account handle"
          placeholder="handle, e.g. elonmusk"
          value={handle}
          onChange={(e) => setHandle(e.target.value)}
        />
        <select
          aria-label="Priority tier"
          value={priority}
          onChange={(e) => setPriority(e.target.value)}
        >
          {[1, 2, 3, 4, 5, 6, 7].map((n) => (
            <option key={n} value={n}>
              P{n}
            </option>
          ))}
        </select>
        <button type="submit" disabled={!handle.trim()}>
          Track
        </button>
      </form>
      {status && <p className="status">{status}</p>}
      {error && <p className="error">{error}</p>}
      {compare.length > 0 && (
        <article className="panel comparison">
          <h3>Compare accounts</h3>
          <div className="comparison-grid">
            {compare.map((handle) => {
              const metric = analytics[handle] || {};
              return <div key={handle}><strong>@{handle}</strong><br /><span className="muted">{metric.posts || 0} posts · {Math.round(metric.average_engagement || 0).toLocaleString()} avg engagement</span></div>;
            })}
          </div>
        </article>
      )}

      <div className="split wide">
        <div className="account-table-wrap">
          <table className="account-table">
            <thead>
              <tr>
                <th>Account</th>
                <th>Tier</th>
                <th>Interval</th>
                <th>Last checked</th>
                <th>Status</th>
                <th>Tweets</th>
                <th>Engagement</th>
                <th>Compare</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {accounts.map((a) => (
                <tr key={a.handle} className={selected === a.handle ? "active" : ""}>
                  <td>
                    <button className="link" onClick={() => openTimeline(a.handle)}>
                      @{a.handle}
                    </button>
                    {a.display_name && a.display_name !== a.handle && (
                      <div className="muted">{a.display_name}</div>
                    )}
                  </td>
                  <td>
                    <select
                      value={a.priority}
                      onChange={(e) => patch(a.handle, { priority: Number(e.target.value) })}
                    >
                      {[1, 2, 3, 4, 5, 6, 7].map((n) => (
                        <option key={n} value={n}>
                          P{n}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>{a.poll_interval_seconds}s</td>
                  <td>
                    <div>{formatWhen(a.last_checked_at)}</div>
                  </td>
                  <td>
                    {a.quarantined ? (
                      <span className="run-badge failed">quarantined</span>
                    ) : a.tracking ? (
                      <span className="run-badge completed">tracking</span>
                    ) : (
                      <span className="run-badge">off</span>
                    )}
                    {a.quarantine_reason && <div className="muted">{a.quarantine_reason}</div>}
                    {a.last_status && <div className="muted">{a.last_status}</div>}
                  </td>
                  <td>{a.recent_tweet_count}</td>
                  <td>{Math.round(analytics[a.handle]?.average_engagement || 0).toLocaleString()}</td>
                  <td><input aria-label={`Compare @${a.handle}`} type="checkbox" checked={compare.includes(a.handle)} onChange={() => toggleCompare(a.handle)} /></td>
                  <td className="account-actions">
                    <button className="link small" onClick={() => fetchNow(a.handle)}>
                      fetch
                    </button>
                    <button
                      className="link small"
                      onClick={() => patch(a.handle, { tracking: !a.tracking })}
                    >
                      {a.tracking ? "disable" : "enable"}
                    </button>
                    {a.quarantined && (
                      <button
                        className="link small"
                        onClick={() => patch(a.handle, { quarantined: false })}
                      >
                        unquarantine
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!rosterLoaded && <p className="muted">Loading accounts…</p>}
          {rosterLoaded && accounts.length === 0 && (
            <p className="muted">No tracked accounts yet.</p>
          )}
        </div>

        <div className="timeline">
          {selected && <h3>@{selected}</h3>}
          {tweets.map((t) => (
            <TweetCard key={t.id} tweet={t} />
          ))}
          <InfiniteSentinel
            next={next}
            loading={loading}
            onLoad={(url) => selected && openTimeline(selected, url)}
          />
          {selected && !loading && tweets.length === 0 && (
            <p className="muted">No tweets yet — the fetch may still be running.</p>
          )}
        </div>
      </div>
    </section>
  );
}
