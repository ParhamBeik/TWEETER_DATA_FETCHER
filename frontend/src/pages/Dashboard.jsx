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
  alignSeries,
  bucketLabel,
  pivotSeries,
} from "../charts";
import { RANGES, Segmented, useLiveRefresh, windowParams } from "../filters";
import { absoluteTime, compact, duration, relativeTime, signed } from "../format";
import { cn } from "@/lib/cn";
import { Empty, ErrorNote, Skeleton } from "@/ui/controls";
import { PageHead, Panel, PanelBody, PanelHead } from "@/ui/panel";
import { Badge, RUN_TONE, Status, TONE, toneEdge } from "@/ui/status";

function Stat({ label, value, hint, delta, tone }) {
  return (
    <div className="border-l border-line px-4 first:border-l-0 first:pl-0">
      <p className="eyebrow">{label}</p>
      <p className="mt-1 font-mono text-xl tabular">{value ?? "—"}</p>
      <p className="mt-0.5 flex flex-wrap items-baseline gap-1.5 text-xs">
        {delta != null && (
          <span
            className={cn(
              "font-mono tabular",
              delta > 0 ? "text-ok" : delta < 0 ? "text-danger" : "text-fg-dim",
            )}
          >
            {signed(delta)}
          </span>
        )}
        <span className={tone === "warn" ? "text-warn" : "text-fg-dim"}>{hint}</span>
      </p>
    </div>
  );
}

/** Legend + tooltip share one label map so a series is never named two ways. */
function labelled(keys, names) {
  return keys.map((key) => ({ key, name: names[key] || key }));
}

function Chart({ children, empty, show, className }) {
  if (!show) return <Empty title={empty} />;
  return (
    <div className={cn("h-64", className)}>
      <ResponsiveContainer width="100%" height="100%">
        {children}
      </ResponsiveContainer>
    </div>
  );
}

export default function Dashboard() {
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
  // One range control above three panels has to mean one x-axis under all three.
  const [capturedRows, postedRows, spendRows] = alignSeries([
    captured.rows,
    posted,
    spend.rows,
  ]);
  const totals = ingestion?.totals || {};
  const bySubsystem = totals.by_subsystem || {};

  const archive = pipeline?.archive || {};
  const totalWalked = (archive.complete || 0) + (archive.depth_limited || 0);
  const archivePercent = archive.tracked
    ? Math.round((totalWalked / archive.tracked) * 100)
    : 0;

  // "Partial" is its own thing: a saved search that reached the end of what X
  // will serve finishes partial, and so does a run that genuinely broke off. A
  // bare percentage counting only "completed" therefore reads as a failing
  // pipeline when most of the shortfall is X's ceiling, so the tile names the
  // split instead of leaving one number to be misread.
  const runCounts = (() => {
    const rows = ingestion?.run_totals || [];
    const by = (status) =>
      rows.filter((row) => row.status === status).reduce((sum, row) => sum + row.count, 0);
    const total = rows.reduce((sum, row) => sum + row.count, 0);
    return { total, completed: by("completed"), partial: by("partial") };
  })();
  const successRate = runCounts.total
    ? Math.round((runCounts.completed / runCounts.total) * 100)
    : null;
  const runBreakdown = runCounts.total
    ? `${[
        `${compact(runCounts.completed)} complete`,
        runCounts.partial ? `${compact(runCounts.partial)} partial` : null,
      ]
        .filter(Boolean)
        .join(" · ")} of ${compact(runCounts.total)} runs`
    : "no runs in window";

  const oldest = totals.oldest_tweet;
  const historySpan = oldest
    ? `${Math.max(1, Math.round((Date.now() - new Date(oldest)) / 86400000))} days deep`
    : "no dated posts yet";

  const axisTick = (value) => bucketLabel(value, bucket);
  const tooltipLabel = (value) => absoluteTime(value);

  return (
    <section className="flex flex-col gap-5">
      <PageHead
        label="Dashboard"
        title="How is the collector doing?"
        lede="Where the archive is growing from, what it is costing, and what the pipeline is doing right now."
        actions={
          <Segmented label="Time range" options={RANGES} value={range} onChange={setRange} />
        }
      />
      {error && <ErrorNote>{error}</ErrorNote>}

      <Panel>
        <PanelBody className="grid grid-cols-2 gap-y-4 md:grid-cols-5">
          {/* The delta is only meaningful when there is a previous period to
              compare against. On a 13-day-old deployment the 90d view read
              "+60K vs the previous equal period", which was just the whole
              number wearing a growth badge. */}
          <Stat
            label="Captured this window"
            value={compact(totals.captured ?? 0)}
            delta={totals.has_previous === false ? undefined : totals.captured_delta}
            hint={
              totals.has_previous === false
                ? "no earlier period to compare"
                : "vs the previous equal period"
            }
          />
          {/* Archive total counts every row; the feed only reaches accounts
              still tracked. Advertising one number hid ~6K posts no screen
              could open. */}
          <Stat
            label="Archive total"
            value={compact(totals.archive_total ?? 0)}
            hint={
              totals.archive_tracked != null
                && totals.archive_tracked !== totals.archive_total
                ? `${compact(totals.archive_tracked)} tracked · ${compact(
                    (totals.archive_total ?? 0) - totals.archive_tracked,
                  )} retained · ${historySpan}`
                : historySpan
            }
          />
          <Stat
            label="Search results held"
            value={compact(totals.search_total ?? 0)}
            hint="rolling 30-day window"
          />
          <Stat
            label="Accounts archived"
            value={archive.tracked ? `${totalWalked}/${archive.tracked}` : "—"}
            hint={
              archive.depth_limited
                ? `${archive.complete} complete · ${archive.depth_limited} at X limit`
                : `${archivePercent}% of the backfill is finished`
            }
          />
          <Stat
            label="Runs completed"
            value={successRate == null ? "—" : `${successRate}%`}
            hint={runBreakdown}
            // Only a real failure earns the warning colour. Partial runs were
            // painting the tile red for hitting a limit X imposes on everyone.
            tone={
              successRate != null &&
              runCounts.total - runCounts.completed - runCounts.partial >
                runCounts.total * 0.1
                ? "warn"
                : ""
            }
          />
        </PanelBody>
      </Panel>

      <Panel>
        <PanelHead
          label="Collection flow"
          title="Who is doing the collecting"
          lede="Posts captured per bucket, split by the collector that saw them first. Saved-search hits are counted here too, and they expire after 30 days — the archive walk and live poll are what grow the permanent archive."
        />
        <PanelBody>
          <Chart show={captured.rows.length} empty="Nothing captured in this window yet.">
            <BarChart data={capturedRows} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
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
          </Chart>
          <ul className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs">
            {Object.entries(bySubsystem).map(([key, count]) => (
              <li key={key} className="flex items-center gap-1.5">
                <span
                  className="size-2 rounded-[1px]"
                  style={{ background: SUBSYSTEM_COLOR[key] || SERIES[0] }}
                  aria-hidden="true"
                />
                <span className="text-fg-muted">{SUBSYSTEM_LABEL[key] || key}</span>
                <span className="font-mono tabular">{compact(count)}</span>
              </li>
            ))}
          </ul>
        </PanelBody>
      </Panel>

      <div className="grid gap-5 xl:grid-cols-2">
        <Panel>
          <PanelHead
            label="Coverage"
            title="How far back the archive reaches"
            lede="Posts by when they were written, not when we fetched them — the backfill fills in history, the live poll only adds to the leading edge."
          />
          <PanelBody>
            <Chart show={posted.length} empty="No dated posts in this window.">
              <AreaChart data={postedRows} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
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
            </Chart>
          </PanelBody>
        </Panel>

        <Panel>
          <PanelHead
            label="Spend"
            title="Where the request budget went"
            lede="Pages fetched per bucket, by endpoint. All three collectors share the one X budget shown at the top of this screen."
          />
          <PanelBody>
            <Chart show={spend.rows.length} empty="No request telemetry in this window yet.">
              <BarChart data={spendRows} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
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
            </Chart>
          </PanelBody>
        </Panel>
      </div>

      <div className="grid gap-5 xl:grid-cols-2">
        <Panel>
          <PanelHead label="Now" title="What each collector is doing" />
          <PanelBody className="flex flex-col gap-3">
            {!pipeline && <Skeleton className="h-20" />}
            {(pipeline?.subsystems || []).map((row) => {
              const tone = row.running > 0 ? TONE.active : TONE.idle;
              return (
                <div key={row.subsystem} className={cn("annunciator border-l-2", toneEdge(tone))}>
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <strong className="text-sm">
                      {SUBSYSTEM_LABEL[row.subsystem] || row.subsystem}
                    </strong>
                    {row.running > 0 ? (
                      <Status tone={TONE.active}>fetching now</Status>
                    ) : (
                      <span className="font-mono text-xs text-fg-dim">
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
                    <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-fg-muted">
                      <Badge tone={RUN_TONE[row.last_run.status]}>
                        {row.last_run.status.replace("_", " ")}
                      </Badge>
                      {/* New rows, not rows re-seen. This said "+51 posts" for
                          an archive walk that added nothing, directly
                          contradicting the collection-flow chart above it. */}
                      <span className="font-mono tabular">
                        {(row.last_run.new_tweets ?? row.last_run.ingested_tweets)
                          ? `+${compact(row.last_run.new_tweets ?? row.last_run.ingested_tweets)} new`
                          : "nothing new"}
                      </span>
                      <span>{relativeTime(row.last_run.started_at)}</span>
                      <span className="truncate font-mono text-fg-dim">
                        {row.last_run.target || "all"}
                      </span>
                    </div>
                  ) : (
                    <p className="mt-1 text-xs text-fg-dim">Has not run yet.</p>
                  )}
                </div>
              );
            })}

            <div className="mt-1 border-t border-line pt-3">
              <p className="eyebrow">Endpoint health</p>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {Object.entries(pipeline?.endpoint_health || {}).map(([endpoint, state]) => (
                  <Badge key={endpoint} tone={state === "healthy" ? TONE.ok : TONE.warn}>
                    {endpoint}: {String(state).replace(/_/g, " ")}
                  </Badge>
                ))}
                {!Object.keys(pipeline?.endpoint_health || {}).length && (
                  <span className="text-xs text-fg-dim">Nothing reported yet.</span>
                )}
              </div>
            </div>
          </PanelBody>
        </Panel>

        <Panel>
          <PanelHead
            label="Backfill"
            title="How much history is in"
            lede="The archive walk is a finite job: each account's timeline is walked backwards once and then leaves the queue."
          />
          <PanelBody className="flex flex-col gap-3">
            <div
              className="h-1.5 w-full overflow-hidden rounded-xs bg-ink-700"
              role="meter"
              aria-valuenow={archive.complete || 0}
              aria-valuemin={0}
              aria-valuemax={archive.tracked || 0}
              aria-label="Accounts fully archived"
            >
              <span
                className="block h-full bg-accent transition-[width]"
                style={{ width: `${archivePercent}%` }}
              />
            </div>
            <p className="text-xs text-fg-muted">
              <strong className="font-mono tabular text-fg">{archive.complete ?? 0}</strong> of{" "}
              {archive.tracked ?? 0} accounts fully archived
              {archive.stalled ? ` · ${archive.stalled} stalled` : ""}
              {archive.depth_limited
                ? ` · ${archive.depth_limited} stopped at X's serving depth`
                : ""}
            </p>
            <ul className="flex flex-col divide-y divide-line">
              {(archive.walking || []).map((row) => (
                <li
                  key={row.handle}
                  className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 py-1.5 text-xs"
                >
                  <span className="font-mono">@{row.handle}</span>
                  <span className="font-mono tabular text-fg-muted">
                    {compact(row.pages)} pages
                  </span>
                  <span className="text-fg-dim">{String(row.outcome).replace(/_/g, " ")}</span>
                  {row.quarantined && <Badge tone={TONE.warn}>quarantined</Badge>}
                </li>
              ))}
              {!(archive.walking || []).length && (
                <li className="py-1.5 text-xs text-fg-dim">
                  {archive.depth_limited
                    ? `${archive.depth_limited} account(s) stopped at X's serving depth — as deep as the API goes, not their first tweet`
                    : "Every tracked account is fully archived."}
                </li>
              )}
            </ul>

            {(pipeline?.quarantined || []).length > 0 && (
              <div className="border-t border-line pt-3">
                <p className="eyebrow">Quarantined</p>
                <ul className="mt-1.5 flex flex-col gap-1">
                  {pipeline.quarantined.map((row) => (
                    <li key={row.handle} className="flex flex-wrap gap-x-3 text-xs">
                      <span className="font-mono">@{row.handle}</span>
                      <span className="text-fg-dim">
                        {row.quarantine_reason || "unavailable"}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <Link className="mt-auto text-xs text-accent hover:underline" to="/ops">
              Open run history and controls →
            </Link>
          </PanelBody>
        </Panel>
      </div>
    </section>
  );
}
