import { useEffect, useState } from "react";
import { api } from "@/api";
import { cn } from "@/lib/cn";
import { duration } from "@/format";
import { Status, TONE } from "@/ui/status";

/**
 * The shared X request budget, pinned above every page.
 *
 * This is the one fact that explains everything else in the console. All three
 * collectors -- the live poll, the archive walk and every saved search -- spend
 * a single metered budget (UserTweets is 50 requests per 15 minutes). Why a
 * search is late, why the backfill only advanced one account, why a fetch is
 * asleep: the answer is almost always here. Burying it inside one page's panel
 * meant the operator had to already suspect the budget to go look at it.
 *
 * Ticks rather than a percentage bar: this is a countable allowance, and seeing
 * fourteen segments left reads faster than "28%".
 */

const TICKS = 20;
// Below this share of an endpoint's allowance, a collector is about to start
// sleeping rather than fetching. Named as a threshold because that is what it
// is -- see FETCH_HISTORICAL_QUOTA_FLOOR, the same idea server-side.
const LOW = 0.25;
const GUARDED = 0.5;

function tone(share) {
  if (share <= LOW) return TONE.danger;
  if (share <= GUARDED) return TONE.warn;
  return TONE.ok;
}

const FILL = {
  ok: "bg-ok",
  warn: "bg-warn",
  danger: "bg-danger",
};

function Gauge({ endpoint, remaining, limit, resetsInSeconds }) {
  const share = limit > 0 ? Math.max(0, Math.min(1, remaining / limit)) : 0;
  const lit = limit > 0 ? Math.round(share * TICKS) : 0;
  const role = tone(share);
  return (
    <div className="flex min-w-0 items-center gap-2.5">
      <span className="font-mono text-2xs uppercase tracking-wider text-fg-dim">{endpoint}</span>
      <span
        className="flex items-center gap-px"
        role="meter"
        aria-valuenow={remaining}
        aria-valuemin={0}
        aria-valuemax={limit}
        aria-label={`${endpoint} requests remaining`}
      >
        {Array.from({ length: TICKS }, (_, index) => (
          <span
            key={index}
            className={cn(
              "h-3 w-1 rounded-[1px] transition-colors",
              index < lit ? FILL[role] : "bg-line",
            )}
          />
        ))}
      </span>
      <span className="font-mono text-2xs tabular text-fg-muted">
        {remaining}/{limit}
      </span>
      {resetsInSeconds > 0 && (
        <span className="font-mono text-2xs tabular text-fg-dim">
          resets {duration(resetsInSeconds)}
        </span>
      )}
    </div>
  );
}

export default function BudgetRail() {
  const [pipeline, setPipeline] = useState(null);
  const [reachable, setReachable] = useState(true);

  useEffect(() => {
    let active = true;
    const load = () =>
      api("/stats/pipeline/")
        .then((data) => {
          if (!active) return;
          setPipeline(data);
          setReachable(true);
        })
        .catch(() => active && setReachable(false));
    load();
    const timer = setInterval(load, 20000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);

  const limits = (pipeline?.rate_limits || []).filter((row) => row.limit > 0);
  const running = pipeline?.running || [];

  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-2 border-b border-line bg-ink-850 px-4 py-2 sm:px-6">
      <span className="eyebrow shrink-0">X budget</span>

      {limits.length > 0 ? (
        limits.map((row) => (
          <Gauge
            key={row.endpoint}
            endpoint={row.endpoint}
            remaining={row.remaining}
            limit={row.limit}
            resetsInSeconds={row.resets_in_seconds}
          />
        ))
      ) : (
        <span className="text-xs text-fg-dim">
          {reachable
            ? "No quota reported yet — the collector writes this after its first request."
            : "Collector state unreachable."}
        </span>
      )}

      <span className="ml-auto flex items-center gap-3">
        {running.length > 0 ? (
          <Status tone={TONE.active}>
            {running.length === 1
              ? `${running[0].subsystem} fetching ${running[0].target || "all"}`
              : `${running.length} fetches running`}
          </Status>
        ) : (
          <Status tone={TONE.idle}>Collector idle</Status>
        )}
      </span>
    </div>
  );
}
