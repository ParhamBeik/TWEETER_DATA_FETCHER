import { useEffect, useState } from "react";
import { api } from "./api";

export default function RunStatus() {
  const [runs, setRuns] = useState([]);

  useEffect(() => {
    let active = true;
    const load = () => api("/runs/")
      .then((data) => active && setRuns((data.results || []).slice(0, 5)))
      .catch(() => {});
    load();
    const timer = setInterval(load, 15000);
    return () => { active = false; clearInterval(timer); };
  }, []);

  if (!runs.length) return null;
  return (
    <aside className="run-status" aria-label="Recent fetch runs">
      <h3>Fetcher status</h3>
      {runs.map((run) => (
        <div key={run.run_id}>
          <span className={`run-badge ${run.status}`}>{run.status.replace("_", " ")}</span>
          <strong>{run.subsystem}</strong>
          <span>{run.target || "all"}</span>
          <small>{new Date(run.started_at).toLocaleString()}</small>
        </div>
      ))}
    </aside>
  );
}
