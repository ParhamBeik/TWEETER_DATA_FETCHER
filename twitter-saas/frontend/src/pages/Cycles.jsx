import { useEffect, useState } from "react";
import { api } from "../api";

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

  async function load() {
    try {
      const path = subsystem ? `/runs/?subsystem=${subsystem}` : "/runs/";
      const data = await api(path);
      setRuns(data.results || []);
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

  return (
    <section className="cycles">
      <h2>Cycles</h2>
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
              <button className="link" onClick={() => inspect(run)}>
                <span className={`run-badge ${run.status}`}>{run.status.replace("_", " ")}</span>
                <strong>{run.subsystem}</strong>
                <span>{run.target || "all"}</span>
                <small>{duration(run)}</small>
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
