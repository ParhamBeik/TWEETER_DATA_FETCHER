import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, setToken } from "../api";

export default function Login({ onAuth }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [mode, setMode] = useState("login"); // "login" | "register"
  const [error, setError] = useState("");
  const navigate = useNavigate();

  async function submit(e) {
    e.preventDefault();
    setError("");
    try {
      const path = mode === "login" ? "/auth/login/" : "/auth/register/";
      const data = await api(path, { method: "POST", body: { username, password } });
      setToken(data.token);
      onAuth?.();
      navigate("/");
    } catch (err) {
      setError(err.message);
    }
  }

  return (
    <section className="auth">
      <h2>{mode === "login" ? "Sign in" : "Create account"}</h2>
      <form onSubmit={submit}>
        <input
          placeholder="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoFocus
        />
        <input
          placeholder="password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <button type="submit">{mode === "login" ? "Login" : "Register"}</button>
      </form>
      {error && <p className="error">{error}</p>}
      <button
        className="link"
        onClick={() => setMode(mode === "login" ? "register" : "login")}
      >
        {mode === "login" ? "Need an account? Register" : "Have an account? Login"}
      </button>
    </section>
  );
}
