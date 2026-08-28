import { useEffect, useState } from "react";
import { api } from "@/api";
import { cn } from "@/lib/cn";
import { absoluteTime, compact, duration, relativeTime } from "@/format";
import { Panel, PanelBody, PanelHead } from "@/ui/panel";
import { Readout, Empty, Skeleton } from "@/ui/controls";
import { Badge, RUN_TONE, SCHEDULE_TONE, Status, toneEdge } from "@/ui/status";

// How a search run actually ends, in the operator's words rather than the
// engine's. These strings come back verbatim in the run summary; leaving them
// raw is why "success_search_window_crossed" was on screen at all.
const OUTCOMES = {
  success_search_window_crossed: "Reached the end of the look-back window",
  success_reached_known_ground: "Caught up with what the last run already had",
  repeated_cursor_history: "X started repeating pages",
  no_bottom_cursor: "X stopped offering more pages",
  provider_depth_limit: "X refused to serve any deeper",
};

function outcomeLabel(raw) {
  if (!raw) return "";
  return OUTCOMES[raw] || String(raw).replace(/_/g, " ");
}

function runDuration(run) {
  if (!run.started_at || !run.finished_at) return null;
  return duration((new Date(run.finished_at) - new Date(run.started_at)) / 1000);
}

/** One run, expanded into what it did and what it cost. */
function Run({ run }) {
  const summary = run.summary || {};
  const pages = summary.pages_by_endpoint?.SearchTimeline ?? summary.pages ?? null;
  const failures = Object.entries(run.failure_ledger || {});
  const tone = RUN_TONE[run.status] || "idle";
  return (
    <li className={cn("annunciator border-l-2 py-2", toneEdge(tone))}>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <Badge tone={tone}>{run.status.replace("_", " ")}</Badge>
        <time
          className="font-mono text-xs text-fg-muted"
          dateTime={run.started_at}
          title={absoluteTime(run.started_at)}
        >
          {relativeTime(run.started_at)}
        </time>
        {runDuration(run) && (
          <span className="font-mono text-xs tabular text-fg-dim">took {runDuration(run)}</span>
        )}
      </div>
      <div className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 font-mono text-xs tabular text-fg-muted">
        <span>{compact(summary.ingested_tweets || 0)} results stored</span>
        {pages !== null && <span>{compact(pages)} pages fetched</span>}
      </div>
      {summary.stop_reason && (
        <p className="mt-1 text-xs text-fg-dim">{outcomeLabel(summary.stop_reason)}</p>
      )}
      {failures.map(([key, value]) => (
        <p key={key} className="mt-1 font-mono text-xs text-danger">
          {key} ×{value?.count ?? 1}
        </p>
      ))}
    </li>
  );
}

/**
 * The recurring job behind one phrase.
 *
 * Everything here was previously invisible: the cadence lived in a database
 * column, the dispatcher enforced it on a five-minute tick, and the console
 * showed neither. When a query went quiet there was no screen that could say
 * whether it was paused, queued behind another fetch, or simply not due yet.
 */
export default function Workflow({ search, onRunNow, running }) {
  const [runs, setRuns] = useState(null);
  const [schedule, setSchedule] = useState(search.schedule);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    const load = () => {
      api(`/searches/${search.id}/runs/`)
        .then((data) => active && setRuns(data.results || []))
        .catch((e) => active && setError(e.message));
      api(`/searches/${search.id}/schedule/`)
        .then((data) => active && setSchedule(data))
        .catch(() => {});
    };
    load();
    // The countdown and the queued/running state are the reason to be on this
    // tab at all, so they refresh without asking.
    const timer = setInterval(load, 15000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [search.id, running]);

  const state = schedule?.state || "idle";
  const nextDue = schedule?.is_due
    ? "Due now"
    : schedule?.next_due_at
      ? `in ${duration(schedule.seconds_until_due)}`
      : "After the first run";

  return (
    <div className="flex flex-col gap-4">
      <Panel>
        <PanelHead
          label="Schedule"
          title="When this query runs"
          lede="One job per query. It runs on its own clock and is queued by the dispatcher when due, so a slow query cannot starve the others."
        />
        <PanelBody className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <div>
            <p className="eyebrow">State</p>
            <Status tone={SCHEDULE_TONE[state]} className="mt-1.5 text-sm">
              {state === "paused"
                ? "Paused"
                : state === "running"
                  ? "Fetching now"
                  : state === "queued"
                    ? "Queued"
                    : "Waiting"}
            </Status>
          </div>
          <Readout label="Runs every" value={duration(schedule?.interval_seconds || 0)} />
          <Readout
            label="Next run"
            value={nextDue}
            hint={schedule?.next_due_at ? absoluteTime(schedule.next_due_at) : undefined}
          />
          <Readout
            label="Last run"
            value={schedule?.last_run_at ? relativeTime(schedule.last_run_at) : "Never"}
            hint={schedule?.last_run_at ? absoluteTime(schedule.last_run_at) : undefined}
          />
        </PanelBody>
      </Panel>

      <Panel>
        <PanelHead
          label="History"
          title="What each run did"
          lede="Page 1 comes over HTTP; deeper pages are scrolled in a browser. A run ends when it crosses the look-back window, catches up with what the last run stored, or X stops serving."
          actions={
            <button
              type="button"
              onClick={onRunNow}
              className="rounded-sm border border-line px-2.5 py-1 text-xs text-fg-muted hover:border-line-strong hover:text-fg"
            >
              Run now
            </button>
          }
        />
        <PanelBody>
          {error && <p className="text-sm text-danger">{error}</p>}
          {runs === null && !error && (
            <div className="flex flex-col gap-2">
              <Skeleton className="h-12" />
              <Skeleton className="h-12" />
            </div>
          )}
          {runs?.length === 0 && (
            <Empty title="No runs yet">
              The first run is queued when a query is created. If nothing appears, check the X
              session on the Ops page.
            </Empty>
          )}
          {runs?.length > 0 && (
            <ul className="flex flex-col divide-y divide-line">
              {runs.map((run) => (
                <Run key={run.run_id} run={run} />
              ))}
            </ul>
          )}
        </PanelBody>
      </Panel>
    </div>
  );
}
