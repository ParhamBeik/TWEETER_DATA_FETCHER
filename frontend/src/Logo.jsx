import { NavLink } from "react-router-dom";
import { cn } from "@/lib/cn";

// Same construction as Holdings' three ascending bars: three rounded rects,
// currentColor, opacity steps, readable at 16px. The geometry is the inverse —
// depth, not growth. Top bar is the live poll (shallow, full weight); each
// layer below is one page further back in the archive walk.
export default function Logo({ size = 24, title = "Signal Archive", decorative = false }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      role={decorative ? "presentation" : "img"}
      aria-hidden={decorative ? true : undefined}
      aria-label={decorative ? undefined : title}
    >
      <rect x="5" y="6" width="10" height="6" rx="1.5" fill="currentColor" />
      <rect x="5" y="13" width="16" height="6" rx="1.5" fill="currentColor" opacity="0.7" />
      <rect x="5" y="20" width="22" height="6" rx="1.5" fill="currentColor" opacity="0.45" />
    </svg>
  );
}

/** Chassis tile + wordmark. A link on the signed-in shell; plain on auth pages. */
export function Brand({ to, onClick, compact = false, className }) {
  const lockup = (
    <>
      <span className="flex size-8 shrink-0 items-center justify-center rounded-sm border border-line bg-ink-700 text-accent">
        <Logo size={18} decorative />
      </span>
      <span className={cn("font-bold tracking-tight", compact ? "text-sm" : "text-md")}>
        Signal Archive
      </span>
    </>
  );
  if (to) {
    return (
      <NavLink
        to={to}
        onClick={onClick}
        aria-label="Signal Archive"
        className={cn("flex items-center gap-2", className)}
      >
        {lockup}
      </NavLink>
    );
  }
  return <div className={cn("flex items-center gap-2", className)}>{lockup}</div>;
}
