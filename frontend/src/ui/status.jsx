import { cva } from "class-variance-authority";
import { cn } from "@/lib/cn";

// Status roles, mirroring charts.js so a run that is amber in a chart is amber
// in a list. `partial` is a warning rather than a failure: the run did collect
// something, it just could not prove it finished.
export const TONE = {
  ok: "ok",
  warn: "warn",
  serious: "serious",
  danger: "danger",
  idle: "idle",
  active: "active",
};

export const RUN_TONE = {
  completed: TONE.ok,
  partial: TONE.warn,
  running: TONE.active,
  failed: TONE.danger,
  auth_required: TONE.serious,
};

export const SCHEDULE_TONE = {
  running: TONE.active,
  queued: TONE.warn,
  paused: TONE.idle,
  idle: TONE.ok,
};

const dot = cva("inline-block size-1.5 shrink-0 rounded-full", {
  variants: {
    tone: {
      ok: "bg-ok",
      warn: "bg-warn",
      serious: "bg-serious",
      danger: "bg-danger",
      idle: "bg-fg-dim",
      // The one place motion is spent: a collector that is spending quota right
      // now is the single most time-sensitive fact on any of these screens.
      active: "bg-accent motion-safe:animate-pulse",
    },
  },
  defaultVariants: { tone: "idle" },
});

const badge = cva(
  "inline-flex items-center gap-1.5 rounded-xs border px-1.5 py-0.5 font-mono text-2xs uppercase tracking-wider",
  {
    variants: {
      tone: {
        ok: "border-ok/30 text-ok",
        warn: "border-warn/30 text-warn",
        serious: "border-serious/30 text-serious",
        danger: "border-danger/30 text-danger",
        idle: "border-line-strong text-fg-dim",
        active: "border-accent/40 text-accent",
      },
    },
    defaultVariants: { tone: "idle" },
  },
);

/**
 * Status as a dot plus its name, never colour alone.
 *
 * The label is not optional on purpose: a bare coloured dot is unreadable to
 * anyone with a colour vision deficiency and ambiguous to everyone else.
 */
export function Status({ tone, children, className }) {
  return (
    <span className={cn("inline-flex items-center gap-1.5 text-xs text-fg-muted", className)}>
      <span className={dot({ tone })} aria-hidden="true" />
      {children}
    </span>
  );
}

export function Badge({ tone, children, className, ...props }) {
  return (
    <span className={cn(badge({ tone }), className)} {...props}>
      {children}
    </span>
  );
}

const EDGE = {
  ok: "border-l-ok",
  warn: "border-l-warn",
  serious: "border-l-serious",
  danger: "border-l-danger",
  active: "border-l-accent",
  idle: "border-l-line-strong",
};

/** The annunciator edge: a row's status as a coloured left rule. */
export function toneEdge(tone) {
  return EDGE[tone] || EDGE.idle;
}
