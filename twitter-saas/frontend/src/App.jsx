import { useState } from "react";
import { Link, Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { getToken, setToken } from "./api";
import Feed from "./pages/Feed";
import Search from "./pages/Search";
import Follows from "./pages/Follows";
import Login from "./pages/Login";

function RequireAuth({ children }) {
  return getToken() ? children : <Navigate to="/login" replace />;
}

export default function App() {
  // Re-render nav when auth changes by keying off a token snapshot.
  const [authed, setAuthed] = useState(Boolean(getToken()));
  const navigate = useNavigate();

  function logout() {
    setToken(null);
    setAuthed(false);
    navigate("/login");
  }

  return (
    <div className="app">
      <header className="topbar">
        <h1>Twitter SaaS</h1>
        {authed && (
          <nav>
            <Link to="/">Feed</Link>
            <Link to="/search">Search</Link>
            <Link to="/follows">Follows</Link>
            <button className="link" onClick={logout}>
              Logout
            </button>
          </nav>
        )}
      </header>

      <main>
        <Routes>
          <Route path="/login" element={<Login onAuth={() => setAuthed(true)} />} />
          <Route
            path="/"
            element={
              <RequireAuth>
                <Feed />
              </RequireAuth>
            }
          />
          <Route
            path="/search"
            element={
              <RequireAuth>
                <Search />
              </RequireAuth>
            }
          />
          <Route
            path="/follows"
            element={
              <RequireAuth>
                <Follows />
              </RequireAuth>
            }
          />
        </Routes>
      </main>
    </div>
  );
}
