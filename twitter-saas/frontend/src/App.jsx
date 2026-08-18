import { useState } from "react";
import { Link, Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { getToken, setToken } from "./api";
import Feed from "./pages/Feed";
import Search from "./pages/Search";
import Accounts from "./pages/Accounts";
import Cycles from "./pages/Cycles";
import Login from "./pages/Login";
import Pulse from "./pages/Pulse";
import Analyze from "./pages/Analyze";

function RequireAuth({ children }) {
  return getToken() ? children : <Navigate to="/login" replace />;
}

export default function App() {
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
        <Link className="brand" to="/">Signal Archive</Link>
        {authed && (
          <nav>
            <Link to="/">Pulse</Link>
            <Link to="/feed">Feed</Link>
            <Link to="/analyze">Analyze</Link>
            <Link to="/accounts">Accounts</Link>
            <Link to="/ops">Ops</Link>
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
                <Pulse />
              </RequireAuth>
            }
          />
          <Route
            path="/feed"
            element={
              <RequireAuth>
                <Feed />
              </RequireAuth>
            }
          />
          <Route
            path="/analyze"
            element={
              <RequireAuth>
                <Analyze />
              </RequireAuth>
            }
          />
          <Route
            path="/accounts"
            element={
              <RequireAuth>
                <Accounts />
              </RequireAuth>
            }
          />
          <Route
            path="/ops"
            element={
              <RequireAuth>
                <Cycles />
              </RequireAuth>
            }
          />
          <Route
            path="/searches"
            element={
              <RequireAuth>
                <Search />
              </RequireAuth>
            }
          />
          <Route
            path="*"
            element={
              <RequireAuth>
                <Pulse />
              </RequireAuth>
            }
          />
        </Routes>
      </main>
    </div>
  );
}
