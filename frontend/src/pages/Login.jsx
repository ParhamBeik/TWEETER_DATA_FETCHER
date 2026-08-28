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
      navigate("/feed", { replace: true });
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
          New to Signal Archive? <Link className="text-accent hover:underline" to="/signup">Create an account</Link>
        </>
      }
    >
      <form className="flex flex-col gap-4" onSubmit={submit} noValidate>
        <label className="flex flex-col gap-1">
          <span className="eyebrow">Username</span>
          <input
            className="w-full rounded-sm border border-line bg-ink-850 px-2.5 py-2 text-sm text-fg placeholder:text-fg-dim hover:border-line-strong focus:border-accent focus:outline-none"
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
          className="mt-1 inline-flex h-10 w-full items-center justify-center gap-2 rounded-sm bg-accent px-4 text-sm font-semibold text-accent-ink transition-colors hover:bg-accent/85 disabled:opacity-40"
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
