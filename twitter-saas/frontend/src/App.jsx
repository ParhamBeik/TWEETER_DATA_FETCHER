import { useState } from "react";
import { Link, Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { getToken, setToken } from "./api";
import Feed from "./pages/Feed";
import Search from "./pages/Search";
import Accounts from "./pages/Accounts";
import Cycles from "./pages/Cycles";
import Login from "./pages/Login";

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
        <h1>Fetcher</h1>
        {authed && (
          <nav>
            <Link to="/">Feed</Link>
            <Link to="/accounts">Accounts</Link>
            <Link to="/cycles">Cycles</Link>
            <Link to="/search">Search</Link>
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
            path="/accounts"
            element={
              <RequireAuth>
                <Accounts />
              </RequireAuth>
            }
          />
          <Route
            path="/cycles"
            element={
              <RequireAuth>
                <Cycles />
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
        </Routes>
      </main>
    </div>
  );
}
