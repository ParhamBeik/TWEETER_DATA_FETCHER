import { useEffect, useState } from "react";
import { api } from "../api";

// The two cookies X actually authenticates with; everything else is ad/telemetry noise.
const REQUIRED_COOKIES = ["auth_token", "ct0"];

function duration(run) {
  if (!run.started_at) return "";
  const end = run.finished_at ? new Date(run.finished_at) : new Date();
  const seconds = Math.max(0, Math.round((end - new Date(run.started_at)) / 1000));
  return `${seconds}s`;
}

export default function Cycles() {
  const [runs, setRuns] = useState([]);
  const [selected, setSelected] = useState(null);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [subsystem, setSubsystem] = useState("");
  const [session, setSession] = useState(null);
  const [sessionJson, setSessionJson] = useState("");

  async function load() {
    try {
      const [data, health] = await Promise.all([
        api(subsystem ? `/runs/?subsystem=${subsystem}` : "/runs/"),
        api("/session/"),
      ]);
      setRuns(data.results || []);
      setSession(health);
    } catch (e) {
      setError(e.message);
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

  async function retry(run) {
    await trigger(run.subsystem);
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

  return (
    <section className="cycles">
      <header>
        <p className="eyebrow">Operations</p>
        <h2 className="page-title">Collection health and operator controls</h2>
      </header>
      <article className="panel session-panel">
        <div>
          <h3>Session health</h3>
          {/* Only the credential-critical names are called out. Listing all ~57
              cookie names was a wall of text that hid whether auth actually works. */}
          <p className={session?.configured ? "session-state ok" : "session-state bad"}>
            {session?.configured ? "● Configured" : "● No active X session"}
          </p>
          <dl className="session-facts">
            {REQUIRED_COOKIES.map((name) => (
              <div key={name}>
                <dt>{name}</dt>
                <dd className={session?.cookie_names?.includes(name) ? "ok" : "bad"}>
                  {session?.cookie_names?.includes(name) ? "present" : "missing"}
                </dd>
              </div>
            ))}
            <div>
              <dt>bearer</dt>
              <dd className={session?.header_names?.includes("authorization") ? "ok" : "bad"}>
                {session?.header_names?.includes("authorization") ? "present" : "missing"}
              </dd>
            </div>
            <div>
              <dt>cookies</dt>
              <dd>{session?.cookie_names?.length || 0} total</dd>
            </div>
            <div>
              <dt>tx-id pools</dt>
              <dd>
                {Object.entries(session?.transaction_id_pools || {})
                  .map(([endpoint, count]) => `${endpoint}:${count}`)
                  .join(" · ") || "none"}
              </dd>
            </div>
          </dl>
          {session?.updated_at && (
            <p className="muted">Updated {new Date(session.updated_at).toLocaleString()}</p>
          )}
          {session?.last_auth_required_at && (
            <p className="muted">Last auth-required run: {new Date(session.last_auth_required_at).toLocaleString()}</p>
          )}
        </div>
        <form onSubmit={saveSession}>
          <textarea
            aria-label="X session JSON"
            placeholder={'{"cookies":{"auth_token":"...","ct0":"..."},"headers":{"authorization":"Bearer ..."}}'}
            value={sessionJson}
            onChange={(e) => setSessionJson(e.target.value)}
          />
          <button type="submit" disabled={!sessionJson.trim()}>Update session</button>
        </form>
      </article>
      <div className="cycle-controls">
        <button type="button" onClick={() => trigger("live")}>
          Run live
        </button>
        <button type="button" onClick={() => trigger("historical")}>
          Run historical
        </button>
        <button type="button" onClick={() => trigger("search")}>
          Run search
        </button>
        <select value={subsystem} onChange={(e) => setSubsystem(e.target.value)}>
          <option value="">all subsystems</option>
          <option value="live">live</option>
          <option value="historical">historical</option>
          <option value="search">search</option>
        </select>
      </div>
      {status && <p className="status">{status}</p>}
      {error && <p className="error">{error}</p>}

      <div className="split wide">
        <ul className="cycle-list">
          {runs.map((run) => (
            <li key={run.run_id} className={selected?.run_id === run.run_id ? "active" : ""}>
              <button className="link run-row" onClick={() => inspect(run)}>
                <span className={`run-badge ${run.status}`}>{run.status.replace("_", " ")}</span>
                <strong>{run.subsystem}</strong>
                <span className="run-target">{run.target || "all"}</span>
                <span className="run-ingested">
                  {run.summary?.ingested_tweets ? `+${run.summary.ingested_tweets}` : "—"}
                </span>
                <small>{duration(run)}</small>
                <small className="run-when">
                  {new Date(run.started_at).toLocaleTimeString([], {
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                </small>
              </button>
              {(run.status === "failed" || run.status === "partial") && (
                <button className="link small" onClick={() => retry(run)}>
                  retry
                </button>
              )}
            </li>
          ))}
          {runs.length === 0 && <li className="muted">No fetch runs yet.</li>}
        </ul>

        <div className="cycle-detail">
          {selected ? (
            <>
              <h3>
                {selected.subsystem} / {selected.target || "all"}
              </h3>
              <p>
                <span className={`run-badge ${selected.status}`}>
                  {selected.status.replace("_", " ")}
                </span>{" "}
                ingested {selected.summary?.ingested_tweets ?? 0} · pages{" "}
                {selected.summary?.raw_pages ?? 0}
              </p>
              {selected.failure_ledger && Object.keys(selected.failure_ledger).length > 0 && (
                <pre className="log-excerpt">
                  {JSON.stringify(selected.failure_ledger, null, 2)}
                </pre>
              )}
              {selected.log_excerpt && (
                <pre className="log-excerpt">{selected.log_excerpt}</pre>
              )}
            </>
          ) : (
            <p className="muted">Select a run to inspect logs and failures.</p>
          )}
        </div>
      </div>
    </section>
  );
}
