import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../api";
import {
  AXIS_PROPS,
  BAR_RADIUS_Y,
  LINE,
  RUN_STATUS_COLOR,
  SERIES,
  STACK_GAP,
  SUBSYSTEM_COLOR,
  SUBSYSTEM_LABEL,
  TOOLTIP_STYLE,
  bucketLabel,
  pivotSeries,
} from "../charts";
import { RANGES, Segmented, useLiveRefresh, windowParams } from "../filters";
import { absoluteTime, compact, duration, relativeTime, signed } from "../format";

function Stat({ label, value, hint, delta, tone }) {
  return (
    <article className="stat-card">
      <p className="stat-label">{label}</p>
      <strong className="stat-value">{value ?? "—"}</strong>
      <div className="stat-foot">
        {delta != null && (
          <span className={`delta ${delta > 0 ? "up" : delta < 0 ? "down" : "flat"}`}>
            {signed(delta)}
          </span>
        )}
        <small className={`stat-hint ${tone || ""}`}>{hint}</small>
      </div>
    </article>
  );
}

/** Legend + tooltip share one label map so a series is never named two ways. */
function labelled(keys, names) {
  return keys.map((key) => ({ key, name: names[key] || key }));
}

function ChartFrame({ title, lede, children, tall }) {
  return (
    <article className={tall ? "panel panel-tall" : "panel"}>
      <h3>{title}</h3>
      {lede && <p className="muted">{lede}</p>}
      {children}
    </article>
  );
}

function EmptyChart({ message }) {
  return <p className="muted chart-empty">{message}</p>;
}

export default function Pulse() {
  const [range, setRange] = useState("24h");
  const [ingestion, setIngestion] = useState(null);
  const [pipeline, setPipeline] = useState(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const params = windowParams({ range });
      const [flow, state] = await Promise.all([
        api(`/analytics/ingestion/?${params}`),
        api("/stats/pipeline/"),
      ]);
      setIngestion(flow);
      setPipeline(state);
      // This view re-polls; without clearing, one transient blip would pin an
      // error banner to the page for the rest of the session.
      setError("");
    } catch (e) {
      setError(e.message);
    }
  }, [range]);

  useLiveRefresh(load, [range]);

  const bucket = ingestion?.bucket || "hour";
  const captured = pivotSeries(ingestion?.captured || [], "source_subsystem");
  const posted = (ingestion?.posted || []).map((row) => ({
    bucket: row.bucket,
    count: row.count,
  }));
  const spend = pivotSeries(ingestion?.requests || [], "endpoint", "requests");
  const totals = ingestion?.totals || {};
  const bySubsystem = totals.by_subsystem || {};

  const quota = pipeline?.rate_limits || [];
  const userTweets = quota.find((row) => row.endpoint === "UserTweets");
  const archive = pipeline?.archive || {};
  const archivePercent = archive.tracked
    ? Math.round((archive.complete / archive.tracked) * 100)
    : 0;

  const successRate = (() => {
    const rows = ingestion?.run_totals || [];
    const total = rows.reduce((sum, row) => sum + row.count, 0);
    if (!total) return null;
    const good = rows
      .filter((row) => row.status === "completed")
      .reduce((sum, row) => sum + row.count, 0);
    return Math.round((good / total) * 100);
  })();

  const oldest = totals.oldest_tweet;
  const historySpan = oldest
    ? `${Math.max(1, Math.round((Date.now() - new Date(oldest)) / 86400000))} days deep`
    : "no dated posts yet";

  const axisTick = (value) => bucketLabel(value, bucket);
  const tooltipLabel = (value) => absoluteTime(value);

  return (
    <section className="stack-lg">
      <header className="page-head">
        <div>
          <p className="eyebrow">Ingestion</p>
          <h2 className="page-title">How is the collector doing?</h2>
          <p className="page-lede">
            Where the archive is growing from, what it is costing, and what the
            pipeline is doing right now.
          </p>
        </div>
        <Segmented label="Time range" options={RANGES} value={range} onChange={setRange} />
      </header>
      {error && <p className="error">{error}</p>}

      <div className="stat-grid">
        <Stat
          label="Captured this window"
          value={compact(totals.captured ?? 0)}
          delta={totals.captured_delta}
          hint="vs the previous equal period"
        />
        <Stat
          label="Archive total"
          value={compact(totals.archive_total ?? 0)}
          hint={historySpan}
        />
        <Stat
          label="Quota remaining"
          value={userTweets ? `${userTweets.remaining}/${userTweets.limit}` : "—"}
          hint={
            userTweets
              ? `UserTweets · resets in ${duration(userTweets.resets_in_seconds)}`
              : "no rate-limit state yet"
          }
          tone={userTweets && userTweets.remaining < 10 ? "warn" : ""}
        />
        <Stat
          label="Accounts archived"
          value={archive.tracked ? `${archive.complete}/${archive.tracked}` : "—"}
          hint={`${archivePercent}% of the backfill is finished`}
        />
        <Stat
          label="Run success rate"
          value={successRate == null ? "—" : `${successRate}%`}
          hint={`${(ingestion?.run_totals || []).reduce((s, r) => s + r.count, 0)} runs in window`}
          tone={successRate != null && successRate < 60 ? "warn" : ""}
        />
      </div>

      <ChartFrame
        title="Who is doing the collecting"
        lede="Posts captured per bucket, split by the pipeline that saw them first. This is where you can tell whether the archive walk or the live poll is carrying the load."
        tall
      >
        {captured.rows.length ? (
          <div className="chart-box">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={captured.rows} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid stroke={LINE} vertical={false} />
                <XAxis dataKey="bucket" tickFormatter={axisTick} {...AXIS_PROPS} />
                <YAxis allowDecimals={false} width={48} {...AXIS_PROPS} />
                <Tooltip
                  cursor={{ fill: "rgba(255,255,255,0.04)" }}
                  contentStyle={TOOLTIP_STYLE}
                  labelFormatter={tooltipLabel}
                />
                <Legend />
                {labelled(captured.keys, SUBSYSTEM_LABEL).map(({ key, name }, index) => (
                  <Bar
                    key={key}
                    dataKey={key}
                    name={name}
                    stackId="captured"
                    fill={SUBSYSTEM_COLOR[key] || SERIES[index % SERIES.length]}
                    radius={index === captured.keys.length - 1 ? BAR_RADIUS_Y : 0}
                    maxBarSize={40}
                    {...STACK_GAP}
                  />
                ))}
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <EmptyChart message="Nothing captured in this window yet." />
        )}
        <ul className="inline-legend">
          {Object.entries(bySubsystem).map(([key, count]) => (
            <li key={key}>
              <span className="swatch" style={{ background: SUBSYSTEM_COLOR[key] || SERIES[0] }} />
              {SUBSYSTEM_LABEL[key] || key}: <strong>{compact(count)}</strong>
            </li>
          ))}
        </ul>
      </ChartFrame>

      <div className="split-grid">
        <ChartFrame
          title="Archive coverage"
          lede="Posts by when they were written, not when we fetched them — the backfill fills in history, the live poll only adds to the leading edge."
        >
          {posted.length ? (
            <div className="chart-box">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={posted} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
                  <CartesianGrid stroke={LINE} vertical={false} />
                  <XAxis dataKey="bucket" tickFormatter={axisTick} {...AXIS_PROPS} />
                  <YAxis allowDecimals={false} width={48} {...AXIS_PROPS} />
                  <Tooltip
                    contentStyle={TOOLTIP_STYLE}
                    labelFormatter={tooltipLabel}
                    formatter={(value) => [value, "posts written"]}
                  />
                  <Area
                    type="monotone"
                    dataKey="count"
                    name="Posts written"
                    stroke={SERIES[0]}
                    strokeWidth={2}
                    fill={SERIES[0]}
                    fillOpacity={0.18}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <EmptyChart message="No dated posts in this window." />
          )}
        </ChartFrame>

        <ChartFrame
          title="Request spend"
          lede="Pages fetched per bucket, by endpoint. All three fetchers share one X rate budget, so this is where it went."
        >
          {spend.rows.length ? (
            <div className="chart-box">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={spend.rows} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
                  <CartesianGrid stroke={LINE} vertical={false} />
                  <XAxis dataKey="bucket" tickFormatter={axisTick} {...AXIS_PROPS} />
                  <YAxis allowDecimals={false} width={48} {...AXIS_PROPS} />
                  <Tooltip
                    cursor={{ fill: "rgba(255,255,255,0.04)" }}
                    contentStyle={TOOLTIP_STYLE}
                    labelFormatter={tooltipLabel}
                  />
                  <Legend />
                  {spend.keys.map((key, index) => (
                    <Bar
                      key={key}
                      dataKey={key}
                      name={key}
                      stackId="spend"
                      fill={
                        key.startsWith("HTTP")
                          ? RUN_STATUS_COLOR.failed
                          : SERIES[index % SERIES.length]
                      }
                      radius={index === spend.keys.length - 1 ? BAR_RADIUS_Y : 0}
                      maxBarSize={40}
                      {...STACK_GAP}
                    />
                  ))}
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <EmptyChart message="No request telemetry in this window yet." />
          )}
        </ChartFrame>
      </div>

      <div className="split-grid">
        <article className="panel">
          <h3>Pipeline right now</h3>
          <ul className="pipeline-list">
            {(pipeline?.subsystems || []).map((row) => (
              <li key={row.subsystem}>
                <div className="pipeline-head">
                  <strong>{SUBSYSTEM_LABEL[row.subsystem] || row.subsystem}</strong>
                  {row.running > 0 ? (
                    <span className="badge badge-running">running now</span>
                  ) : (
                    <span className="muted">
                      {/* A zero countdown means the interval already elapsed and
                          beat has not picked it up yet -- "next in 0s" read like
                          a broken clock. */}
                      {row.next_due_in_seconds > 0
                        ? `next in ${duration(row.next_due_in_seconds)}`
                        : "due now"}
                    </span>
                  )}
                </div>
                {row.last_run ? (
                  <div className="pipeline-detail">
                    <span
                      className={`run-badge ${row.last_run.status}`}
                      style={{ borderColor: RUN_STATUS_COLOR[row.last_run.status] }}
                    >
                      {row.last_run.status.replace("_", " ")}
                    </span>
                    <span>+{compact(row.last_run.ingested_tweets)} posts</span>
                    <span className="muted">{relativeTime(row.last_run.started_at)}</span>
                    <span className="muted">{row.last_run.target || "all"}</span>
                  </div>
                ) : (
                  <p className="muted">Has not run yet.</p>
                )}
              </li>
            ))}
            {!pipeline && <li className="muted">Loading pipeline state…</li>}
          </ul>

          <h4>Rate budget</h4>
          {quota.length ? (
            <ul className="quota-list">
              {quota.map((row) => {
                const percent = row.limit ? Math.round((row.remaining / row.limit) * 100) : 0;
                return (
                  <li key={row.endpoint}>
                    <div className="quota-head">
                      <span>{row.endpoint}</span>
                      <span className="tabular">
                        {row.remaining}/{row.limit} · resets in {duration(row.resets_in_seconds)}
                      </span>
                    </div>
                    <div
                      className="quota-bar"
                      role="meter"
                      aria-valuenow={row.remaining}
                      aria-valuemin={0}
                      aria-valuemax={row.limit || 0}
                      aria-label={`${row.endpoint} requests remaining`}
                    >
                      <span
                        style={{
                          width: `${percent}%`,
                          background: percent < 20 ? RUN_STATUS_COLOR.failed : SERIES[0],
                        }}
                      />
                    </div>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="muted">No rate-limit state recorded yet.</p>
          )}

          <h4>Endpoint health</h4>
          <div className="chip-row">
            {Object.entries(pipeline?.endpoint_health || {}).map(([endpoint, state]) => (
              <span
                key={endpoint}
                className={`badge ${state === "healthy" ? "badge-ok" : "badge-warn"}`}
              >
                {endpoint}: {String(state).replace(/_/g, " ")}
              </span>
            ))}
            {!Object.keys(pipeline?.endpoint_health || {}).length && (
              <span className="muted">Nothing reported yet.</span>
            )}
          </div>
        </article>

        <article className="panel">
          <h3>Backfill progress</h3>
          <p className="muted">
            The archive walk is a finite job: each account&apos;s timeline is walked
            backwards once and then leaves the queue.
          </p>
          <div
            className="progress-bar"
            role="meter"
            aria-valuenow={archive.complete || 0}
            aria-valuemin={0}
            aria-valuemax={archive.tracked || 0}
            aria-label="Accounts fully archived"
          >
            <span style={{ width: `${archivePercent}%` }} />
          </div>
          <p className="progress-caption">
            <strong>{archive.complete ?? 0}</strong> of {archive.tracked ?? 0} accounts fully
            archived
            {archive.stalled ? ` · ${archive.stalled} stalled` : ""}
          </p>
          <ul className="walk-list">
            {(archive.walking || []).map((row) => (
              <li key={row.handle}>
                <span className="walk-handle">@{row.handle}</span>
                <span className="tabular">{compact(row.pages)} pages</span>
                <span className="muted">{String(row.outcome).replace(/_/g, " ")}</span>
                {row.quarantined && <span className="badge badge-warn">quarantined</span>}
              </li>
            ))}
            {!(archive.walking || []).length && (
              <li className="muted">Every tracked account is fully archived.</li>
            )}
          </ul>
          {(pipeline?.quarantined || []).length > 0 && (
            <>
              <h4>Quarantined</h4>
              <ul className="walk-list">
                {pipeline.quarantined.map((row) => (
                  <li key={row.handle}>
                    <span className="walk-handle">@{row.handle}</span>
                    <span className="muted">{row.quarantine_reason || "unavailable"}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
          <Link className="inline-cta" to="/ops">
            Open run history and controls →
          </Link>
        </article>
      </div>
    </section>
  );
}
