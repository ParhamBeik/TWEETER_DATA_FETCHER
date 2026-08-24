import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import AuthLayout, { PasswordField } from "./AuthLayout";

// Mirrors the server's validators closely enough to be useful while typing.
// The server decides -- these only save a round trip, and its per-field errors
// are shown verbatim when they disagree.
const passwordRules = [
  ["length", "At least 10 characters", (value) => value.length >= 10],
  ["case", "Uppercase and lowercase letters", (value) => /[a-z]/.test(value) && /[A-Z]/.test(value)],
  ["number", "A number or symbol", (value) => /\d/.test(value) || /[^\w\s]/.test(value)],
];

export function getPasswordChecks(password, confirmation) {
  return [
    ...passwordRules.map(([id, label, test]) => ({ id, label, ok: test(password) })),
    { id: "match", label: "Passwords match", ok: Boolean(password && password === confirmation) },
  ];
}

export function strengthFor(checks, password) {
  if (!password) return null;
  const score = checks.filter(({ id, ok }) => id !== "match" && ok).length;
  return score === 3 ? "Strong" : score === 2 ? "Good" : "Needs work";
}

export default function Signup() {
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [error, setError] = useState("");
  const [fieldErrors, setFieldErrors] = useState({});
  const [busy, setBusy] = useState(false);
  const { signIn } = useAuth();
  const navigate = useNavigate();

  const checks = getPasswordChecks(password, confirmation);
  const passwordValid = checks.every(({ ok }) => ok);

  async function submit(event) {
    event.preventDefault();
    if (busy) return;
    setError("");
    setFieldErrors({});
    setBusy(true);
    try {
      const tokens = await api("/auth/register/", {
        method: "POST",
        body: { username: username.trim(), email: email.trim(), password },
        retry: false,
      });
      signIn(tokens);
      navigate("/", { replace: true });
    } catch (err) {
      setError(err.message);
      setFieldErrors(err.fieldErrors || {});
    } finally {
      setBusy(false);
    }
  }

  const firstOf = (field) => fieldErrors[field]?.[0];

  return (
    <AuthLayout
      eyebrow="Get started"
      title="Create your account"
      subtitle="Start building a sharper view of the conversations you follow."
      error={error}
      footer={
        <>
          Already have an account? <Link to="/login">Sign in</Link>
        </>
      }
    >
      <form className="auth-form" onSubmit={submit} noValidate>
        <label className="auth-field">
          <span>Username</span>
          <input
            aria-label="Username"
            name="username"
            autoComplete="username"
            placeholder="pick a username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoFocus
            required
          />
          {firstOf("username") && <span className="auth-field-error">{firstOf("username")}</span>}
        </label>

        <label className="auth-field">
          <span>Email <em className="auth-optional">optional</em></span>
          <input
            aria-label="Email"
            name="email"
            type="email"
            autoComplete="email"
            placeholder="you@example.com"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
          {firstOf("email") && <span className="auth-field-error">{firstOf("email")}</span>}
        </label>

        <PasswordField
          label="Password"
          name="new-password"
          autoComplete="new-password"
          placeholder="Create a password"
          value={password}
          onChange={setPassword}
          error={firstOf("password")}
          hint={password && <em className="auth-strength">{strengthFor(checks, password)}</em>}
        />

        <PasswordField
          label="Confirm password"
          name="confirm-password"
          autoComplete="new-password"
          placeholder="Re-enter your password"
          value={confirmation}
          onChange={setConfirmation}
        />

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

        <button
          className="auth-submit"
          type="submit"
          disabled={busy || !username.trim() || !passwordValid}
        >
          {busy ? "Creating your account…" : "Create account"}
          {!busy && <span aria-hidden="true">→</span>}
        </button>
      </form>
    </AuthLayout>
  );
}
