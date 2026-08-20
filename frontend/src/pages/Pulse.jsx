import { useEffect, useState } from "react";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api";
import { AXIS_PROPS, BAR_RADIUS_Y, SERIES, TOOLTIP_STYLE } from "../charts";

function Stat({ label, value, hint }) {
  return (
    <article className="rounded-2xl border border-line bg-surface p-5 shadow-lg shadow-black/10">
      <p className="text-sm text-slate-400">{label}</p>
      <strong className="mt-2 block text-3xl text-white">{value ?? "—"}</strong>
      <small className="mt-2 block text-slate-500">{hint}</small>
    </article>
  );
}

export default function Pulse() {
  const [overview, setOverview] = useState(null);
  const [topics, setTopics] = useState([]);
  const [error, setError] = useState("");

  useEffect(() => {
    Promise.all([api("/stats/overview/"), api("/analytics/topics/")])
      .then(([stats, topicData]) => {
        setOverview(stats);
        setTopics(topicData.results || []);
      })
      .catch((err) => setError(err.message));
  }, []);

  // Oldest-first so the axis reads left-to-right in time. Keyed by start time,
  // not subsystem -- every recent run is "live", so labelling by subsystem gave
  // five identical ticks and hid the only dimension that varies.
  const health = (overview?.latest_runs || [])
    .slice()
    .reverse()
    .map((run) => ({
      name: new Date(run.started_at).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      }),
      subsystem: run.subsystem,
      tweets: run.summary?.ingested_tweets || 0,
    }));

  return (
    <section className="space-y-8">
      <header>
        <p className="eyebrow">Archive pulse</p>
        <h2 className="page-title">What changed since you last looked?</h2>
        <p className="page-lede">Archive activity, collection health, and emerging topics in one view.</p>
      </header>
      {error && <p className="error">{error}</p>}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat label="Archive tweets" value={overview?.tweets} hint={`${overview?.tweets_in_window ?? 0} in the last 24 hours`} />
        <Stat label="Tracked accounts" value={overview?.tracked_accounts} hint={`${overview?.quarantined_accounts ?? 0} quarantined`} />
        <Stat label="Recent cycles" value={overview?.latest_runs?.length} hint="Latest completed, partial, or failed runs" />
        <Stat label="Topic spikes" value={topics.length} hint="Hashtags seen in the current window" />
      </div>
      <div className="grid gap-5 lg:grid-cols-[1.4fr,1fr]">
        <article className="panel min-h-80">
          <h3>Recent ingestion</h3>
          <p className="muted">Tweets captured by the most recent runs.</p>
          <div className="h-60">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={health} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
                <XAxis dataKey="name" {...AXIS_PROPS} />
                <YAxis allowDecimals={false} width={44} {...AXIS_PROPS} />
                <Tooltip
                  cursor={{ fill: "rgba(255,255,255,0.04)" }}
                  contentStyle={TOOLTIP_STYLE}
                  labelFormatter={(label, payload) =>
                    `${payload?.[0]?.payload?.subsystem || "run"} · ${label}`
                  }
                  formatter={(value) => [value, "tweets ingested"]}
                />
                <Bar dataKey="tweets" fill={SERIES[0]} radius={BAR_RADIUS_Y} maxBarSize={48} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </article>
        <article className="panel">
          <h3>Topic spikes</h3>
          <ul className="space-y-3">
            {topics.slice(0, 8).map((topic) => (
              <li className="flex items-center justify-between border-b border-line pb-3" key={topic.topic}>
                <span>#{topic.topic}</span>
                <span className={topic.delta > 0 ? "text-accent" : "text-slate-400"}>
                  {topic.current_count} · {topic.delta >= 0 ? "+" : ""}{topic.delta}
                </span>
              </li>
            ))}
            {!topics.length && <li className="muted">No topic data yet.</li>}
          </ul>
        </article>
      </div>
    </section>
  );
}
