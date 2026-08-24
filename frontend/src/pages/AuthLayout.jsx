import { useState } from "react";

// Shared chrome for the two auth pages, so sign-in and sign-up cannot drift
// apart visually while staying separate routes.
export default function AuthLayout({ eyebrow, title, subtitle, error, children, footer }) {
  return (
    <section className="auth-page">
      <div className="auth-showcase" aria-hidden="true">
        <div className="auth-mark">
          <span />
          <span />
          <span />
        </div>
        <p className="auth-kicker">Signal Archive</p>
        <h1>See the signal<br />behind the noise.</h1>
        <p className="auth-showcase-copy">
          A calmer workspace for tracking accounts, exploring conversations, and finding what matters.
        </p>
        <div className="auth-orbit">
          <i />
          <i />
          <i />
        </div>
      </div>

      <div className="auth-card">
        <div className="auth-card-header">
          <p className="auth-eyebrow">{eyebrow}</p>
          <h2>{title}</h2>
          <p className="auth-subtitle">{subtitle}</p>
        </div>

        {error && <p className="auth-error" role="alert">{error}</p>}

        {children}

        <p className="auth-footer">{footer}</p>
      </div>
    </section>
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
    <label className="auth-field">
      <span className="auth-label-row">
        {label}
        {hint}
      </span>
      <span className="auth-input-wrap">
        <input
          aria-label={label}
          name={name}
          autoComplete={autoComplete}
          placeholder={placeholder}
          type={revealed ? "text" : "password"}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          required
        />
        <button
          type="button"
          className="auth-reveal"
          aria-label={revealed ? `Hide ${label.toLowerCase()}` : `Show ${label.toLowerCase()}`}
          onClick={() => setRevealed((shown) => !shown)}
        >
          {revealed ? "Hide" : "Show"}
        </button>
      </span>
      {error && <span className="auth-field-error">{error}</span>}
    </label>
  );
}
