import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, setToken } from "../api";

const passwordRules = [
  ["length", "At least 8 characters", (value) => value.length >= 8],
  ["case", "Uppercase and lowercase letters", (value) => /[a-z]/.test(value) && /[A-Z]/.test(value)],
  ["number", "A number or symbol", (value) => /\d/.test(value) || /[^\w\s]/.test(value)],
];

function getPasswordChecks(password, confirmation) {
  return [
    ...passwordRules.map(([id, label, test]) => ({ id, label, ok: test(password) })),
    { id: "match", label: "Passwords match", ok: Boolean(password && password === confirmation) },
  ];
}

function strengthFor(checks, password) {
  if (!password) return null;
  const score = checks.filter(({ id, ok }) => id !== "match" && ok).length;
  return score === 3 ? "Strong" : score === 2 ? "Good" : "Needs work";
}

export default function Login({ onAuth }) {
  const [mode, setMode] = useState("login");
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirmation, setShowConfirmation] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();
  const registering = mode === "register";
  const checks = getPasswordChecks(password, confirmation);
  const passwordValid = checks.every(({ ok }) => ok);

  function switchMode(nextMode) {
    setMode(nextMode);
    setError("");
    setConfirmation("");
  }

  async function submit(e) {
    e.preventDefault();
    if (busy) return;
    setError("");
    setBusy(true);
    try {
      const path = registering ? "/auth/register/" : "/auth/login/";
      const data = await api(path, {
        method: "POST",
        body: { username: identifier.trim(), password },
      });
      setToken(data.token);
      onAuth?.();
      navigate("/");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

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
          <p className="auth-eyebrow">Welcome back</p>
          <h2>{registering ? "Create your account" : "Sign in to Signal Archive"}</h2>
          <p className="auth-subtitle">
            {registering
              ? "Start building a sharper view of the conversations you follow."
              : "Your research workspace is ready when you are."}
          </p>
        </div>

        <div className="auth-tabs" role="tablist" aria-label="Authentication mode">
          <button
            type="button"
            role="tab"
            aria-selected={mode === "login"}
            className={mode === "login" ? "auth-tab active" : "auth-tab"}
            onClick={() => switchMode("login")}
          >
            Sign in
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === "register"}
            className={mode === "register" ? "auth-tab active" : "auth-tab"}
            onClick={() => switchMode("register")}
          >
            Create account
          </button>
        </div>

        {error && <p className="auth-error" role="alert">{error}</p>}

        <form className="auth-form" onSubmit={submit} noValidate>
          <label className="auth-field">
            <span>Email or username</span>
            <input
              aria-label="Email or username"
              name="username"
              autoComplete="username"
              inputMode="email"
              placeholder="you@example.com"
              value={identifier}
              onChange={(e) => setIdentifier(e.target.value)}
              autoFocus
              required
            />
          </label>

          <label className="auth-field">
            <span className="auth-label-row">
              Password
              {registering && password && <em className="auth-strength">{strengthFor(checks, password)}</em>}
            </span>
            <span className="auth-input-wrap">
              <input
                aria-label="Password"
                name="password"
                autoComplete={registering ? "new-password" : "current-password"}
                placeholder={registering ? "Create a password" : "Enter your password"}
                type={showPassword ? "text" : "password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
              <button
                type="button"
                className="auth-reveal"
                aria-label={showPassword ? "Hide password" : "Show password"}
                onClick={() => setShowPassword((shown) => !shown)}
              >
                {showPassword ? "Hide" : "Show"}
              </button>
            </span>
          </label>

          {registering && (
            <>
              <label className="auth-field">
                <span>Confirm password</span>
                <span className="auth-input-wrap">
                  <input
                    aria-label="Confirm password"
                    name="confirm-password"
                    autoComplete="new-password"
                    placeholder="Re-enter your password"
                    type={showConfirmation ? "text" : "password"}
                    value={confirmation}
                    onChange={(e) => setConfirmation(e.target.value)}
                    required
                  />
                  <button
                    type="button"
                    className="auth-reveal"
                    aria-label={showConfirmation ? "Hide confirmation" : "Show confirmation"}
                    onClick={() => setShowConfirmation((shown) => !shown)}
                  >
                    {showConfirmation ? "Hide" : "Show"}
                  </button>
                </span>
              </label>

              <div className="auth-requirements">
                <p>Password requirements</p>
                <ul>
                  {checks.map(({ id, label, ok }) => (
                    <li className={ok ? "passed" : ""} key={id}>
                      <span>{ok ? "✓" : "○"}</span>{label}
                    </li>
                  ))}
                </ul>
              </div>
            </>
          )}

          <button
            className="auth-submit"
            type="submit"
            disabled={busy || !identifier.trim() || !password || (registering && !passwordValid)}
          >
            {busy ? "Working…" : registering ? "Create account" : "Sign in"}
            {!busy && <span aria-hidden="true">→</span>}
          </button>
        </form>

        <p className="auth-footer">
          {registering ? "Already have an account?" : "New to Signal Archive?"}{" "}
          <button type="button" className="auth-switch" onClick={() => switchMode(registering ? "login" : "register")}>
            {registering ? "Sign in" : "Create an account"}
          </button>
        </p>
      </div>
    </section>
  );
}
