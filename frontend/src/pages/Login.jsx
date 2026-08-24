import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import AuthLayout, { PasswordField } from "./AuthLayout";

export default function Login() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const { signIn } = useAuth();
  const navigate = useNavigate();

  async function submit(event) {
    event.preventDefault();
    if (busy) return;
    setError("");
    setBusy(true);
    try {
      const tokens = await api("/auth/login/", {
        method: "POST",
        body: { username: username.trim(), password },
        // A failed login is a 401/400 about these credentials, not an expired
        // session -- retrying it through the refresh path would be nonsense.
        retry: false,
      });
      signIn(tokens);
      navigate("/", { replace: true });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthLayout
      eyebrow="Welcome back"
      title="Sign in to Signal Archive"
      subtitle="Your research workspace is ready when you are."
      error={error}
      footer={
        <>
          New to Signal Archive? <Link to="/signup">Create an account</Link>
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
            placeholder="your username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoFocus
            required
          />
        </label>

        <PasswordField
          label="Password"
          name="password"
          autoComplete="current-password"
          placeholder="Enter your password"
          value={password}
          onChange={setPassword}
        />

        <button
          className="auth-submit"
          type="submit"
          disabled={busy || !username.trim() || !password}
        >
          {busy ? "Signing in…" : "Sign in"}
          {!busy && <span aria-hidden="true">→</span>}
        </button>
      </form>
    </AuthLayout>
  );
}
