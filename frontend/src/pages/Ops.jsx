import { useEffect, useState } from "react";
import { api } from "../api";
import { cn } from "@/lib/cn";
import { absoluteTime, compact } from "../format";
import { Button } from "@/ui/button";
import { Empty, ErrorNote } from "@/ui/controls";
import { Select, Textarea } from "@/ui/field";
import { PageHead, Panel, PanelBody, PanelHead } from "@/ui/panel";
import { Badge, RUN_TONE, Status, TONE, toneEdge } from "@/ui/status";

// The two cookies X actually authenticates with; everything else is ad/telemetry noise.
const REQUIRED_COOKIES = ["auth_token", "ct0"];

const SUBSYSTEMS = [
  { value: "live", label: "Live poll" },
  { value: "historical", label: "Archive walk" },
  { value: "search", label: "Searches" },
];

function runDuration(run) {
  if (!run.started_at) return "";
  const end = run.finished_at ? new Date(run.finished_at) : new Date();
  return `${Math.max(0, Math.round((end - new Date(run.started_at)) / 1000))}s`;
}

function Fact({ term, value, ok }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-line py-1 last:border-b-0">
      <dt className="font-mono text-xs text-fg-muted">{term}</dt>
      <dd
        className={cn(
          "font-mono text-xs",
          ok === true ? "text-ok" : ok === false ? "text-danger" : "text-fg",
        )}
      >
        {value}
      </dd>
    </div>
  );
}

export default function Ops() {
  const [runs, setRuns] = useState([]);
  const [selected, setSelected] = useState(null);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [subsystem, setSubsystem] = useState("");
  const [session, setSession] = useState(null);
  const [sessionJson, setSessionJson] = useState("");
  const [next, setNext] = useState(null);
  const [loadingMore, setLoadingMore] = useState(false);

  async function load() {
    try {
      const [data, health] = await Promise.all([
        api(subsystem ? `/runs/?subsystem=${subsystem}` : "/runs/"),
        api("/session/"),
      ]);
      setRuns(data.results || []);
      setNext(data.next || null);
      setSession(health);
      // This view re-polls every 10s. Without clearing, a single transient blip
      // pinned an error banner to the page for the rest of the session.
      setError("");
    } catch (e) {
      setError(e.message);
    }
  }

  /**
   * Append the next page of history.
   *
   * The list is 30 runs per page and the page never followed `next`, so every
   * run older than the last 30 was unreachable -- while the dashboard reported
   * thousands in the same window.
   */
  async function loadMore() {
    if (!next || loadingMore) return;
    setLoadingMore(true);
    try {
      const data = await api(next.replace(/^.*\/api/, ""));
      setRuns((current) => [...current, ...(data.results || [])]);
      setNext(data.next || null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoadingMore(false);
    }
  }

  useEffect(() => {
    load();
    const timer = setInterval(load, 10000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subsystem]);

  async function trigger(name) {
    setError("");
    try {
      await api("/cycles/", { method: "POST", body: { subsystem: name } });
      setStatus(`Queued ${name} cycle.`);
      load();
    } catch (e) {
      setError(e.message);
    }
  }

  async function inspect(run) {
    setError("");
    try {
      setSelected(await api(`/runs/${run.run_id}/`));
    } catch (e) {
      setError(e.message);
    }
  }

  async function saveSession(e) {
    e.preventDefault();
    try {
      await api("/session/", { method: "POST", body: JSON.parse(sessionJson) });
      setSessionJson("");
      setStatus("Session updated. Values are never returned by the API.");
      load();
    } catch (e) {
      setError(e instanceof SyntaxError ? "Session must be valid JSON." : e.message);
    }
  }

  const hasCookie = (name) => Boolean(session?.cookie_names?.includes(name));
  const hasBearer = Boolean(session?.header_names?.includes("authorization"));

  return (
    <section className="flex flex-col gap-5">
      <PageHead
        label="Ops"
        title="Collector controls and run history"
        lede="One X session serves every collector here. When it lapses, every fetch stops — which is why it is the first thing on this page."
      />

      <Panel>
        <PanelHead
          label="Credentials"
          title="X session"
          actions={
            session?.configured ? (
              <Status tone={TONE.ok}>Configured</Status>
            ) : (
              <Status tone={TONE.danger}>No active session</Status>
            )
          }
        />
        <PanelBody className="grid gap-5 lg:grid-cols-2">
          <div>
            {/* Only the credential-critical names are called out. Listing all ~57
                cookie names was a wall of text that hid whether auth actually
                works. */}
            <dl>
              {REQUIRED_COOKIES.map((name) => (
                <Fact
                  key={name}
                  term={name}
                  ok={hasCookie(name)}
                  value={hasCookie(name) ? "present" : "missing"}
                />
              ))}
              <Fact term="bearer" ok={hasBearer} value={hasBearer ? "present" : "missing"} />
              <Fact term="cookies" value={`${session?.cookie_names?.length || 0} total`} />
              <Fact
                term="tx-id pools"
                value={
                  Object.entries(session?.transaction_id_pools || {})
                    .map(([endpoint, count]) => `${endpoint}:${count}`)
                    .join(" · ") || "none"
                }
              />
            </dl>
            {session?.updated_at && (
              <p className="mt-2 text-xs text-fg-dim">
                Updated {absoluteTime(session.updated_at)}
              </p>
            )}
            {session?.last_auth_required_at && (
              <p className="text-xs text-warn">
                Last auth-required run: {absoluteTime(session.last_auth_required_at)}
              </p>
            )}
          </div>
          <form onSubmit={saveSession} className="flex flex-col gap-2">
            <label className="eyebrow" htmlFor="session-json">
              Replace session
            </label>
            <Textarea
              id="session-json"
              rows={6}
              aria-label="X session JSON"
              placeholder={
                '{"cookies":{"auth_token":"...","ct0":"..."},"headers":{"authorization":"Bearer ..."}}'
              }
              value={sessionJson}
              onChange={(e) => setSessionJson(e.target.value)}
            />
            <Button
              type="submit"
              variant="primary"
              className="self-start"
              disabled={!sessionJson.trim()}
            >
              Update session
            </Button>
          </form>
        </PanelBody>
      </Panel>

      <div className="flex flex-wrap items-center gap-2">
        {SUBSYSTEMS.map((row) => (
          <Button key={row.value} onClick={() => trigger(row.value)}>
            Run {row.label.toLowerCase()}
          </Button>
        ))}
        <Select
          aria-label="Filter by subsystem"
          className="ml-auto w-auto"
          value={subsystem}
          onChange={(e) => setSubsystem(e.target.value)}
        >
          <option value="">All subsystems</option>
          {SUBSYSTEMS.map((row) => (
            <option key={row.value} value={row.value}>
              {row.label}
            </option>
          ))}
        </Select>
      </div>

      {status && <p className="annunciator border-l-accent text-sm text-fg-muted">{status}</p>}
      {error && <ErrorNote>{error}</ErrorNote>}

      <div className="grid gap-5 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
        <Panel className="h-max overflow-hidden">
          <PanelHead
            label={next ? `${runs.length} runs · more available` : `${runs.length} runs`}
            className="px-3 py-2"
          />
          {runs.length === 0 ? (
            <p className="p-3 text-xs text-fg-muted">No fetch runs yet.</p>
          ) : (
            <ul className="flex flex-col divide-y divide-line">
              {runs.map((run) => {
                const tone = RUN_TONE[run.status] || TONE.idle;
                return (
                  <li key={run.run_id}>
                    <button
                      type="button"
                      onClick={() => inspect(run)}
                      aria-current={selected?.run_id === run.run_id ? "true" : undefined}
                      className={cn(
                        "w-full border-l-2 px-3 py-2 text-left transition-colors hover:bg-ink-700",
                        toneEdge(tone),
                        selected?.run_id === run.run_id && "bg-ink-700",
                      )}
                    >
                      <span className="flex items-center justify-between gap-2">
                        <Badge tone={tone}>{run.status.replace("_", " ")}</Badge>
                        <span className="font-mono text-2xs tabular text-fg-dim">
                          {new Date(run.started_at).toLocaleTimeString([], {
                            hour: "2-digit",
                            minute: "2-digit",
                          })}{" "}
                          · {runDuration(run)}
                        </span>
                      </span>
                      <span className="mt-1 flex items-baseline justify-between gap-2">
                        <span className="truncate text-xs">
                          <strong className="font-medium">{run.subsystem}</strong>{" "}
                          <span className="font-mono text-fg-dim">{run.target || "all"}</span>
                        </span>
                        <span className="shrink-0 font-mono text-2xs tabular text-fg-muted">
                          {/* New rows, not rows touched: a re-poll that
                              re-stored what it already had showed "+40". */}
                          {(run.summary?.new_tweets ?? run.summary?.ingested_tweets)
                            ? `+${compact(run.summary.new_tweets ?? run.summary.ingested_tweets)}`
                            : "—"}
                        </span>
                      </span>
                    </button>
                    {/* Only where it can help: re-running the subsystem is the
                        repair for a run that failed or could not prove it
                        finished, and noise on the ones that succeeded. */}
                    {(run.status === "failed" || run.status === "partial") && (
                      <Button
                        size="sm"
                        variant="quiet"
                        className="mb-1 ml-2"
                        onClick={() => trigger(run.subsystem)}
                      >
                        retry
                      </Button>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
          {next && (
            <div className="border-t border-line p-2">
              <Button
                size="sm"
                variant="quiet"
                className="w-full"
                disabled={loadingMore}
                onClick={loadMore}
              >
                {loadingMore ? "Loading…" : "Load older runs"}
              </Button>
            </div>
          )}
        </Panel>

        <Panel className="h-max">
          {selected ? (
            <>
              <PanelHead
                label={selected.run_id}
                title={`${selected.subsystem} / ${selected.target || "all"}`}
                actions={
                  <Badge tone={RUN_TONE[selected.status]}>
                    {selected.status.replace("_", " ")}
                  </Badge>
                }
              />
              <PanelBody className="flex flex-col gap-3">
                <p className="font-mono text-xs tabular text-fg-muted">
                  ingested {selected.summary?.ingested_tweets ?? 0} · pages{" "}
                  {selected.summary?.raw_pages ?? 0}
                </p>
                {selected.failure_ledger && Object.keys(selected.failure_ledger).length > 0 && (
                  <pre className="overflow-x-auto rounded-sm border border-line bg-ink-900 p-3 font-mono text-xs text-danger">
                    {JSON.stringify(selected.failure_ledger, null, 2)}
                  </pre>
                )}
                {selected.log_excerpt && (
                  <pre className="max-h-96 overflow-auto rounded-sm border border-line bg-ink-900 p-3 font-mono text-xs text-fg-muted">
                    {selected.log_excerpt}
                  </pre>
                )}
              </PanelBody>
            </>
          ) : (
            <PanelBody>
              <Empty title="Pick a run">
                Select a run to read its log excerpt and failure ledger.
              </Empty>
            </PanelBody>
          )}
        </Panel>
      </div>
    </section>
  );
}
