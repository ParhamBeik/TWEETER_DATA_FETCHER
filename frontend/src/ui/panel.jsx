import { cn } from "@/lib/cn";

/** A chassis panel. Hairline rule, no shadow -- instruments are flat. */
export function Panel({ className, children, ...props }) {
  return (
    <section
      className={cn("rounded-sm border border-line bg-ink-800", className)}
      {...props}
    >
      {children}
    </section>
  );
}

/**
 * Panel header. `label` is the readout eyebrow naming what is being reported on;
 * `title` is the plain question the panel answers.
 */
export function PanelHead({ label, title, lede, actions, className }) {
  return (
    <header
      className={cn(
        "flex flex-wrap items-start justify-between gap-3 border-b border-line px-4 py-3",
        className,
      )}
    >
      <div className="min-w-0">
        {label && <p className="eyebrow">{label}</p>}
        {title && <h2 className="mt-0.5 text-md font-semibold tracking-tight">{title}</h2>}
        {lede && <p className="mt-1 max-w-prose text-xs text-fg-muted">{lede}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </header>
  );
}

export function PanelBody({ className, ...props }) {
  return <div className={cn("p-4", className)} {...props} />;
}

/** Page header: what this screen is for, above every control. */
export function PageHead({ label, title, lede, actions }) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <p className="eyebrow">{label}</p>
        <h1 className="mt-1 text-xl font-semibold tracking-tight">{title}</h1>
        {lede && <p className="mt-1.5 max-w-2xl text-sm text-fg-muted">{lede}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </header>
  );
}
