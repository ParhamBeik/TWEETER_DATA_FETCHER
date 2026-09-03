import { Suspense, lazy, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import {
  Activity,
  ChartNoAxesColumn,
  Gauge,
  LogOut,
  Menu,
  Radio,
  Search as SearchIcon,
  Users,
  X,
} from "lucide-react";
import { useAuth } from "./auth";
import BudgetRail from "./BudgetRail";
import { Brand } from "./Logo";
import { cn } from "@/lib/cn";
import { Button } from "@/ui/button";
import Feed from "./pages/Feed";
import SearchWorkspace from "./pages/Search";
import Accounts from "./pages/Accounts";
import Ops from "./pages/Ops";
import Login from "./pages/Login";
import Signup from "./pages/Signup";
import { Skeleton } from "@/ui/controls";

// Only these two pull in recharts, and it is the single largest thing in the
// bundle -- eager, it downloaded on the login screen for a chart nobody had
// asked for yet. Both pages already render Skeletons while their data loads, so
// the split shows the loading language they use anyway rather than inventing one.
const Dashboard = lazy(() => import("./pages/Dashboard"));
const Analyze = lazy(() => import("./pages/Analyze"));

/** Matches the stat-tile rows both lazy pages open with. */
function PageFallback() {
  return (
    <div className="flex flex-col gap-5">
      <Skeleton className="h-16" />
      <div className="grid gap-3 sm:grid-cols-3">
        <Skeleton className="h-20" />
        <Skeleton className="h-20" />
        <Skeleton className="h-20" />
      </div>
      <Skeleton className="h-64" />
    </div>
  );
}

// Order is the operator's order of attention: what came in, what you asked for,
// then how the machine is doing. The dashboard used to be the landing page,
// which put instrument readings in front of a person who came to read posts.
const NAV = [
  { to: "/feed", label: "Feed", icon: Radio, hint: "Tracked accounts" },
  { to: "/search", label: "Search", icon: SearchIcon, hint: "Saved queries" },
  { to: "/dashboard", label: "Dashboard", icon: Gauge, hint: "Collector health" },
  { to: "/analyze", label: "Analyze", icon: ChartNoAxesColumn, hint: "What changed" },
  { to: "/accounts", label: "Accounts", icon: Users, hint: "Roster and tiers" },
];

const STAFF_NAV = [{ to: "/ops", label: "Ops", icon: Activity, hint: "Runs and session" }];

function RequireAuth({ children }) {
  const { authed, status } = useAuth();
  // Hold the route while the stored refresh token is being tried, or a reload
  // redirects a signed-in user to /login before the session is restored.
  if (status === "restoring") {
    return <p className="p-6 text-sm text-fg-muted">Restoring your session…</p>;
  }
  return authed ? children : <Navigate to="/login" replace />;
}

function RedirectIfAuthed({ children }) {
  const { authed, status } = useAuth();
  if (status === "restoring") return null;
  return authed ? <Navigate to="/feed" replace /> : children;
}

function NavItem({ to, label, icon: Icon, hint, onNavigate }) {
  return (
    <NavLink
      to={to}
      onClick={onNavigate}
      className={({ isActive }) =>
        cn(
          "group flex items-center gap-2.5 rounded-sm border-l-2 py-1.5 pl-2.5 pr-2 transition-colors",
          isActive
            ? "border-l-accent bg-ink-700 text-fg"
            : "border-l-transparent text-fg-muted hover:bg-ink-800 hover:text-fg",
        )
      }
    >
      <Icon className="size-4 shrink-0" aria-hidden="true" />
      <span className="min-w-0">
        <span className="block text-sm font-medium leading-tight">{label}</span>
        {/* Sighted-only gloss on where the link goes. Folded into the accessible
            name it would read as "Feed Tracked accounts", which is worse than
            the destination on its own. */}
        <span aria-hidden="true" className="block truncate text-2xs text-fg-dim">
          {hint}
        </span>
      </span>
    </NavLink>
  );
}

function Sidebar({ isStaff, user, signOut, onNavigate, className }) {
  return (
    <div className={cn("flex h-full flex-col gap-6 border-r border-line bg-ink-850 p-3", className)}>
      <Brand to="/feed" onClick={onNavigate} className="px-2 pt-1" />

      <nav className="flex flex-col gap-0.5" aria-label="Sections">
        {NAV.map((item) => (
          <NavItem key={item.to} {...item} onNavigate={onNavigate} />
        ))}
        {/* Ops drives the collector and the shared X session. The API rejects a
            non-staff caller regardless; hiding it keeps the console honest about
            what this account can do. */}
        {isStaff && STAFF_NAV.map((item) => <NavItem key={item.to} {...item} onNavigate={onNavigate} />)}
      </nav>

      <div className="mt-auto border-t border-line pt-3">
        <p className="px-2 font-mono text-xs text-fg-muted">{user?.username}</p>
        <p className="px-2 text-2xs text-fg-dim">{isStaff ? "Operator" : "Read only"}</p>
        <Button variant="quiet" size="sm" className="mt-1.5 w-full justify-start" onClick={signOut}>
          <LogOut className="size-3.5" aria-hidden="true" />
          Sign out
        </Button>
      </div>
    </div>
  );
}

export default function App() {
  const { authed, isStaff, user, signOut, status } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);

  // `status === "restoring"` must not fall into the signed-out shell. That shell
  // ends in a catch-all redirect to /login, so a reload on /dashboard would be
  // rewritten to /login before the stored refresh token had been tried -- and
  // once the session came back, /login redirected on to the default page. The
  // requested route was gone either way. RequireAuth already holds each route
  // during a restore; this keeps the shell from deciding first.
  if (!authed && status !== "restoring") {
    return (
      <main className="mx-auto w-full max-w-md px-4 py-16">
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
          <Route path="*" element={<Navigate to="/login" replace />} />
        </Routes>
      </main>
    );
  }

  return (
    <div className="flex min-h-screen">
      <Sidebar
        isStaff={isStaff}
        user={user}
        signOut={signOut}
        className="hidden w-56 shrink-0 lg:flex"
      />

      {menuOpen && (
        <div className="fixed inset-0 z-50 flex lg:hidden">
          <div
            className="absolute inset-0 bg-ink-900/80"
            onClick={() => setMenuOpen(false)}
            aria-hidden="true"
          />
          <Sidebar
            isStaff={isStaff}
            user={user}
            signOut={signOut}
            onNavigate={() => setMenuOpen(false)}
            className="relative w-64"
          />
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b border-line px-3 py-2 lg:hidden">
          <Button
            variant="quiet"
            size="icon"
            aria-label={menuOpen ? "Close menu" : "Open menu"}
            onClick={() => setMenuOpen((was) => !was)}
          >
            {menuOpen ? <X className="size-4" /> : <Menu className="size-4" />}
          </Button>
          <Brand compact />
        </div>

        <BudgetRail />

        <main className="min-w-0 flex-1 px-4 py-6 sm:px-6">
          {/* One boundary around the whole route table: only the two lazy
              routes can suspend, and each already replaces this with its own
              skeletons as soon as its chunk lands. */}
          <Suspense fallback={<PageFallback />}>
          <Routes>
            <Route path="/" element={<Navigate to="/feed" replace />} />
            <Route path="/pulse" element={<Navigate to="/dashboard" replace />} />
            <Route path="/cycles" element={<Navigate to="/ops" replace />} />
            <Route
              path="/feed"
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
                  <SearchWorkspace />
                </RequireAuth>
              }
            />
            <Route
              path="/search/:searchId"
              element={
                <RequireAuth>
                  <SearchWorkspace />
                </RequireAuth>
              }
            />
            <Route
              path="/dashboard"
              element={
                <RequireAuth>
                  <Dashboard />
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
                  <Ops />
                </RequireAuth>
              }
            />
            <Route path="*" element={<Navigate to="/feed" replace />} />
          </Routes>
          </Suspense>
        </main>
      </div>
    </div>
  );
}
