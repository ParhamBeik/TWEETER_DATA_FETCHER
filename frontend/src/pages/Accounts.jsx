import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { api } from "../api";
import InfiniteSentinel from "../InfiniteSentinel";
import TweetCard from "../TweetCard";
import { cn } from "@/lib/cn";
import { absoluteTime, compact, duration } from "../format";
import { Button } from "@/ui/button";
import { Empty, ErrorNote } from "@/ui/controls";
import { Input, Select } from "@/ui/field";
import { PageHead, Panel, PanelBody, PanelHead } from "@/ui/panel";
import { Badge, TONE } from "@/ui/status";

const TIERS = [1, 2, 3, 4, 5, 6, 7];

function formatWhen(value) {
  return value ? absoluteTime(value) : "never";
}

const TH = "pb-2 pr-3 text-left eyebrow font-normal";
const TD = "py-2 pr-3 align-top text-xs";

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
  const [rosterQuery, setRosterQuery] = useState("");
  const activeHandle = useRef(null);

  async function loadAccounts(query = rosterQuery) {
    const search = query.trim();
    try {
      const [data, metrics] = await Promise.all([
        api(`/accounts/${search ? `?q=${encodeURIComponent(search)}` : ""}`),
        api("/analytics/accounts/"),
      ]);
      setAccounts(Array.isArray(data) ? data : data.results || []);
      setAnalytics(Object.fromEntries((metrics.results || []).map((row) => [row.account, row])));
    } catch (e) {
      setError(e.message);
    } finally {
      setRosterLoaded(true);
    }
  }

  useEffect(() => {
    const timer = setTimeout(() => loadAccounts(rosterQuery), rosterQuery ? 300 : 0);
    return () => clearTimeout(timer);
  }, [rosterQuery]);

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
    <section className="flex flex-col gap-5">
      <PageHead
        label="Accounts"
        title="Roster, tiers and collection health"
        lede="A tier sets how much of the shared X budget an account may spend. The interval beside it is what the collector actually measured from how often that account posts."
      />

      <form className="flex flex-wrap items-end gap-2" onSubmit={addAccount}>
        <Input
          className="w-52"
          aria-label="Account handle"
          placeholder="handle, e.g. elonmusk"
          value={handle}
          onChange={(e) => setHandle(e.target.value)}
        />
        <Select
          className="w-28"
          aria-label="Priority tier"
          value={priority}
          onChange={(e) => setPriority(e.target.value)}
        >
          {TIERS.map((n) => (
            <option key={n} value={n}>
              Priority {n}
            </option>
          ))}
        </Select>
        <Button type="submit" variant="primary" disabled={!handle.trim()}>
          Track account
        </Button>
      </form>

      {status && <p className="annunciator border-l-accent text-sm text-fg-muted">{status}</p>}
      {error && <ErrorNote>{error}</ErrorNote>}

      {compare.length > 0 && (
        <Panel>
          <PanelHead label="Comparison" title="Compare accounts" />
          <PanelBody className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {compare.map((row) => {
              const metric = analytics[row] || {};
              return (
                <div key={row} className="annunciator border-l-accent">
                  <p className="font-mono text-sm">@{row}</p>
                  <p className="mt-1 font-mono text-lg tabular">
                    {compact(Math.round(metric.average_engagement || 0))}
                  </p>
                  <p className="text-xs text-fg-dim">
                    avg engagement · {compact(metric.posts || 0)} posts
                  </p>
                </div>
              );
            })}
          </PanelBody>
        </Panel>
      )}

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,28rem)]">
        <Panel className="min-w-0">
          {/* The roster is the 64 accounts being collected, not the 2,409
              authors the parser has ever seen. Searching reaches the rest,
              which is how you find one to start tracking. */}
          <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-2">
            <PanelHead
              label={`${accounts.length} ${rosterQuery ? "matching" : "tracked"} accounts`}
              className="p-0"
            />
            <div className="relative">
              <Input
                aria-label="Search all accounts"
                placeholder="Search all accounts…"
                value={rosterQuery}
                className="h-8 w-56 pr-8 text-sm"
                onChange={(e) => setRosterQuery(e.target.value)}
              />
              {rosterQuery && (
                <button
                  type="button"
                  aria-label="Clear account search"
                  onClick={() => setRosterQuery("")}
                  className="absolute right-1 top-1/2 -translate-y-1/2 rounded-sm p-1.5 text-fg-dim hover:text-fg"
                >
                  <X className="size-3.5" aria-hidden="true" />
                </button>
              )}
            </div>
          </div>
          <PanelBody className="overflow-x-auto">
            <table className="w-full min-w-[46rem]">
              <thead>
                <tr className="border-b border-line">
                  <th scope="col" className={TH}>Account</th>
                  <th scope="col" className={TH}>Tier</th>
                  <th scope="col" className={TH}>Polled every</th>
                  <th scope="col" className={TH}>Last checked</th>
                  <th scope="col" className={TH}>Status</th>
                  <th scope="col" className={TH}>Posts</th>
                  <th scope="col" className={TH}>Engagement</th>
                  <th scope="col" className={TH}>Compare</th>
                  <th scope="col" className={TH} />
                </tr>
              </thead>
              <tbody>
                {accounts.map((a) => (
                  <tr
                    key={a.handle}
                    className={cn(
                      "border-b border-line last:border-b-0",
                      selected === a.handle && "bg-ink-700",
                    )}
                  >
                    <td className={TD}>
                      <button
                        type="button"
                        className="font-mono text-sm hover:text-accent hover:underline"
                        onClick={() => openTimeline(a.handle)}
                      >
                        @{a.handle}
                      </button>
                      {a.display_name && a.display_name !== a.handle && (
                        <div className="text-fg-dim">{a.display_name}</div>
                      )}
                    </td>
                    <td className={TD}>
                      <Select
                        aria-label={`Tier for @${a.handle}`}
                        className="w-24"
                        value={a.priority}
                        onChange={(e) => patch(a.handle, { priority: Number(e.target.value) })}
                      >
                        {TIERS.map((n) => (
                          <option key={n} value={n}>
                            P{n}
                          </option>
                        ))}
                      </Select>
                    </td>
                    <td className={cn(TD, "font-mono tabular")}>
                      {duration(a.poll_interval_seconds)}
                    </td>
                    <td className={cn(TD, "text-fg-muted")}>{formatWhen(a.last_checked_at)}</td>
                    <td className={TD}>
                      {a.quarantined ? (
                        <Badge tone={TONE.danger}>quarantined</Badge>
                      ) : a.tracking ? (
                        <Badge tone={TONE.ok}>tracking</Badge>
                      ) : (
                        <Badge tone={TONE.idle}>off</Badge>
                      )}
                      {a.quarantine_reason && (
                        <div className="mt-1 text-fg-dim">{a.quarantine_reason}</div>
                      )}
                      {a.last_status && <div className="mt-1 text-fg-dim">{a.last_status}</div>}
                    </td>
                    <td className={cn(TD, "font-mono tabular")}>{compact(a.recent_tweet_count)}</td>
                    <td className={cn(TD, "font-mono tabular")}>
                      {compact(Math.round(analytics[a.handle]?.average_engagement || 0))}
                    </td>
                    <td className={TD}>
                      <input
                        aria-label={`Compare @${a.handle}`}
                        type="checkbox"
                        className="accent-[var(--color-accent)]"
                        checked={compare.includes(a.handle)}
                        onChange={() => toggleCompare(a.handle)}
                      />
                    </td>
                    <td className={cn(TD, "whitespace-nowrap")}>
                      <Button size="sm" variant="quiet" onClick={() => fetchNow(a.handle)}>
                        Fetch
                      </Button>
                      <Button
                        size="sm"
                        variant="quiet"
                        onClick={() => patch(a.handle, { tracking: !a.tracking })}
                      >
                        {a.tracking ? "Disable" : "Enable"}
                      </Button>
                      {a.quarantined && (
                        <Button
                          size="sm"
                          variant="quiet"
                          onClick={() => patch(a.handle, { quarantined: false })}
                        >
                          Unquarantine
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!rosterLoaded && <p className="mt-3 text-xs text-fg-muted">Loading accounts…</p>}
            {rosterLoaded && accounts.length === 0 && rosterQuery && (
              <Empty className="mt-3" title={`No account matches "${rosterQuery}"`}>
                Search covers every author the collector has ever seen, tracked or not.
              </Empty>
            )}
            {rosterLoaded && accounts.length === 0 && !rosterQuery && (
              <Empty className="mt-3" title="No tracked accounts yet">
                Add a handle above to start collecting its timeline.
              </Empty>
            )}
          </PanelBody>
        </Panel>

        <div className="min-w-0">
          {selected ? (
            <>
              <p className="eyebrow mb-2">Timeline · @{selected}</p>
              <div className="rounded-sm bg-paper">
                {tweets.map((t) => (
                  <TweetCard key={t.id} tweet={t} />
                ))}
                <InfiniteSentinel
                  next={next}
                  loading={loading}
                  onLoad={(url) => selected && openTimeline(selected, url)}
                />
              </div>
              {!loading && tweets.length === 0 && (
                <Empty title="Nothing collected yet">
                  The first fetch for this account may still be running.
                </Empty>
              )}
            </>
          ) : (
            <Empty title="Pick an account">
              Select a handle to read what the collector has captured from it.
            </Empty>
          )}
        </div>
      </div>
    </section>
  );
}
