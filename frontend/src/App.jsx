import { Link, Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth";
import Feed from "./pages/Feed";
import Search from "./pages/Search";
import Accounts from "./pages/Accounts";
import Cycles from "./pages/Cycles";
import Login from "./pages/Login";
import Signup from "./pages/Signup";
import Pulse from "./pages/Pulse";
import Analyze from "./pages/Analyze";

function RequireAuth({ children }) {
  const { authed, status } = useAuth();
  // Hold the route while the stored refresh token is being tried, or a reload
  // redirects a signed-in user to /login before the session is restored.
  if (status === "restoring") return <p className="auth-restoring">Restoring your session…</p>;
  return authed ? children : <Navigate to="/login" replace />;
}

function RedirectIfAuthed({ children }) {
  const { authed, status } = useAuth();
  if (status === "restoring") return null;
  return authed ? <Navigate to="/" replace /> : children;
}

export default function App() {
  const { authed, isStaff, user, signOut } = useAuth();

  return (
    <div className="app">
      <header className="topbar">
        <Link className="brand" to="/">Signal Archive</Link>
        {authed && (
          <nav>
            <Link to="/">Pulse</Link>
            <Link to="/feed">Feed</Link>
            <Link to="/analyze">Analyze</Link>
            <Link to="/searches">Searches</Link>
            <Link to="/accounts">Accounts</Link>
            {/* Ops drives the collector and the shared X session. The API
                rejects a non-staff caller regardless; hiding it keeps the
                console honest about what this account can do. */}
            {isStaff && <Link to="/ops">Ops</Link>}
            <span className="topbar-user">{user?.username}</span>
            <button className="link" onClick={signOut}>
              Sign out
            </button>
          </nav>
        )}
      </header>

      <main>
        <Routes>
          <Route
            path="/login"
            element={
              <RedirectIfAuthed>
                <Login />
              </RedirectIfAuthed>
            }
          />
          <Route
            path="/signup"
            element={
              <RedirectIfAuthed>
                <Signup />
              </RedirectIfAuthed>
            }
          />
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
