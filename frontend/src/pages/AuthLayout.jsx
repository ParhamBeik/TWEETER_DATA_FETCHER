import { useState } from "react";
import { cn } from "@/lib/cn";

// Shared chrome for the two auth pages, so sign-in and sign-up cannot drift
// apart visually while staying separate routes.
//
// The showcase panel is gone. This is a single-operator instrument behind a
// login, not a product with visitors to persuade, and the marketing column was
// the most templated thing in the app.
export default function AuthLayout({ eyebrow, title, subtitle, error, children, footer }) {
  return (
    <section className="flex flex-col gap-6">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1 className="mt-1 text-xl font-semibold tracking-tight">{title}</h1>
        <p className="mt-1.5 text-sm text-fg-muted">{subtitle}</p>
      </div>

      {error && (
        <p
          role="alert"
          className="annunciator border-l-danger rounded-xs bg-danger/5 py-2 text-sm text-danger"
        >
          {error}
        </p>
      )}

      {children}

      <p className="border-t border-line pt-4 text-xs text-fg-muted">{footer}</p>
    </section>
  );
}

const CONTROL =
  "w-full rounded-sm border border-line bg-ink-850 px-2.5 py-2 text-sm text-fg " +
  "placeholder:text-fg-dim hover:border-line-strong focus:border-accent focus:outline-none";

export function TextField({ label, hint, error, className, ...props }) {
  return (
    <label className={cn("flex flex-col gap-1", className)}>
      <span className="flex items-baseline justify-between gap-2">
        <span className="eyebrow">{label}</span>
        {hint}
      </span>
      <input aria-label={label} className={CONTROL} {...props} />
      {error && <span className="text-xs text-danger">{error}</span>}
    </label>
  );
}

// Password input with a show/hide toggle. Both pages need it, and the toggle
// state belongs to the field rather than to either page.
export function PasswordField({
  label,
  name,
  hint,
  value,
  onChange,
  autoComplete,
  placeholder,
  error,
}) {
  const [revealed, setRevealed] = useState(false);
  return (
    <label className="flex flex-col gap-1">
      <span className="flex items-baseline justify-between gap-2">
        <span className="eyebrow">{label}</span>
        {hint}
      </span>
      <span className="relative block">
        <input
          aria-label={label}
          name={name}
          autoComplete={autoComplete}
          placeholder={placeholder}
          type={revealed ? "text" : "password"}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          required
          className={cn(CONTROL, "pr-14")}
        />
        <button
          type="button"
          className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-fg-muted hover:text-fg"
          aria-label={revealed ? `Hide ${label.toLowerCase()}` : `Show ${label.toLowerCase()}`}
          onClick={() => setRevealed((shown) => !shown)}
        >
          {revealed ? "Hide" : "Show"}
        </button>
      </span>
      {error && <span className="text-xs text-danger">{error}</span>}
    </label>
  );
}
