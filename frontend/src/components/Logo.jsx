import { NavLink } from "react-router-dom";
import { cn } from "@/lib/cn";

// Four vertical telemetry frequency bars: rising to peak signal spike, then tapering.
// Uses currentColor and stepped opacities (0.4, 0.75, 1.0, 0.55), razor-sharp at 16px.
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
      <rect x="4" y="16" width="4.5" height="10" rx="1.5" fill="currentColor" opacity="0.4" />
      <rect x="11" y="9" width="4.5" height="17" rx="1.5" fill="currentColor" opacity="0.75" />
      <rect x="18" y="5" width="4.5" height="21" rx="1.5" fill="currentColor" />
      <rect x="25" y="12" width="4.5" height="14" rx="1.5" fill="currentColor" opacity="0.55" />
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
