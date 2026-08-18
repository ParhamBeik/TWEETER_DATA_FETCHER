import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, setToken } from "../api";

export default function Login({ onAuth }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [mode, setMode] = useState("login"); // "login" | "register"
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  async function submit(e) {
    e.preventDefault();
    if (busy) return; // a second Enter press otherwise fires a duplicate register
    setError("");
    setBusy(true);
    try {
      const path = mode === "login" ? "/auth/login/" : "/auth/register/";
      const data = await api(path, { method: "POST", body: { username, password } });
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
    <section className="auth">
      <h2>{mode === "login" ? "Sign in" : "Create account"}</h2>
      <form onSubmit={submit}>
        <input
          aria-label="Username"
          name="username"
          autoComplete="username"
          placeholder="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoFocus
        />
        <input
          aria-label="Password"
          name="password"
          autoComplete={mode === "login" ? "current-password" : "new-password"}
          placeholder="password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <button type="submit" disabled={busy || !username || !password}>
          {busy ? "Working…" : mode === "login" ? "Login" : "Register"}
        </button>
      </form>
      {error && <p className="error" role="alert">{error}</p>}
      <button
        className="link"
        onClick={() => setMode(mode === "login" ? "register" : "login")}
      >
        {mode === "login" ? "Need an account? Register" : "Have an account? Login"}
      </button>
    </section>
  );
}
