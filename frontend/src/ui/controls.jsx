import { cn } from "@/lib/cn";

/**
 * A row of mutually exclusive options.
 *
 * `aria-pressed` rather than a radiogroup: these read as toggle buttons, and the
 * pressed state is what a screen reader needs since the label alone does not say
 * which is active.
 */
export function Segmented({ label, options, value, onChange, name, className }) {
  return (
    <div
      role="group"
      aria-label={label}
      className={cn("inline-flex rounded-sm border border-line p-0.5", className)}
    >
      {options.map((option) => {
        const active = value === option.value;
        return (
          <button
            key={option.value}
            type="button"
            name={name}
            aria-pressed={active}
            onClick={() => onChange(option.value)}
            className={cn(
              "rounded-xs px-2 py-1 text-xs font-medium transition-colors",
              active ? "bg-accent text-accent-ink" : "text-fg-muted hover:text-fg",
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

/** Independent on/off chips -- post types, media-only, and similar. */
export function ToggleChips({ label, options, values, onChange, className }) {
  return (
    <div role="group" aria-label={label} className={cn("flex flex-wrap gap-1", className)}>
      {options.map((option) => {
        const on = values.includes(option.value);
        return (
          <Chip
            key={option.value}
            pressed={on}
            onClick={() =>
              onChange(on ? values.filter((v) => v !== option.value) : [...values, option.value])
            }
          >
            {option.label}
          </Chip>
        );
      })}
    </div>
  );
}

export function Chip({ pressed, className, children, ...props }) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      className={cn(
        "rounded-sm border px-2 py-1 text-xs font-medium transition-colors",
        pressed
          ? "border-accent/50 bg-accent-soft text-accent"
          : "border-line text-fg-muted hover:border-line-strong hover:text-fg",
        className,
      )}
      {...props}
    >
      {children}
    </button>
  );
}

/** A labelled readout: the mono value under its name. */
export function Readout({ label, value, hint, tone, className }) {
  return (
    <div className={cn("min-w-0", className)}>
      <p className="eyebrow">{label}</p>
      <p
        className={cn(
          "mt-1 truncate font-mono text-lg tabular",
          tone === "dim" ? "text-fg-muted" : "text-fg",
        )}
      >
        {value ?? "—"}
      </p>
      {hint && <p className="mt-0.5 truncate text-xs text-fg-dim">{hint}</p>}
    </div>
  );
}

/** Loading and empty states, so no screen ever renders as a blank rectangle. */
export function Skeleton({ className }) {
  return (
    <div
      className={cn("motion-safe:animate-pulse rounded-sm bg-ink-700", className)}
      aria-hidden="true"
    />
  );
}

/**
 * An empty screen is an invitation to act, so this takes an action rather than
 * only an apology.
 */
export function Empty({ title, children, action, className }) {
  return (
    <div
      className={cn(
        "flex flex-col items-start gap-2 rounded-sm border border-dashed border-line px-4 py-8",
        className,
      )}
    >
      <p className="text-sm font-medium text-fg">{title}</p>
      {children && <p className="max-w-prose text-xs text-fg-muted">{children}</p>}
      {action}
    </div>
  );
}

export function ErrorNote({ children, className }) {
  return (
    <p
      role="alert"
      className={cn(
        "annunciator border-l-danger rounded-xs bg-danger/5 py-2 text-sm text-danger",
        className,
      )}
    >
      {children}
    </p>
  );
}
