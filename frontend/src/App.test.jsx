import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { AuthProvider } from "./auth";
import { clearTokens, getRefreshToken } from "./api";

// Component tests for the shell only: every page is stubbed so these assert
// routing, the auth guard and the nav contract rather than page internals.

vi.mock("./pages/Feed", () => ({ default: () => <div>feed page</div> }));
vi.mock("./pages/Search", () => ({ default: () => <div>search page</div> }));
vi.mock("./pages/Accounts", () => ({ default: () => <div>accounts page</div> }));
vi.mock("./pages/Cycles", () => ({ default: () => <div>cycles page</div> }));
vi.mock("./pages/Pulse", () => ({ default: () => <div>pulse page</div> }));
vi.mock("./pages/Analyze", () => ({ default: () => <div>analyze page</div> }));
vi.mock("./pages/Login", () => ({ default: () => <div>login page</div> }));
vi.mock("./pages/Signup", () => ({ default: () => <div>signup page</div> }));

const ROUTER_FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true };

const renderApp = (route = "/") =>
  render(
    <MemoryRouter initialEntries={[route]} future={ROUTER_FUTURE}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  );

/** Put a session in place the way a completed sign-in does. */
function signedIn({ is_staff = true } = {}) {
  localStorage.setItem("tsaas_refresh", "refresh-123");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url) => {
      if (String(url).includes("/auth/refresh/")) {
        return { ok: true, status: 200, json: async () => ({ access: "access-123", refresh: "refresh-456" }) };
      }
      if (String(url).includes("/auth/me/")) {
        return { ok: true, status: 200, json: async () => ({ username: "carol", is_staff }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    }),
  );
}

beforeEach(() => {
  clearTokens();
  localStorage.clear();
});

describe("unauthenticated visitors", () => {
  it.each([
    ["/", "dashboard"],
    ["/feed", "feed"],
    ["/analyze", "analyze"],
    ["/accounts", "accounts"],
    ["/ops", "ops"],
    ["/searches", "searches"],
  ])("redirects %s to the login form", async (route) => {
    renderApp(route);
    expect(await screen.findByText("login page")).toBeInTheDocument();
  });

  it("hides the primary navigation entirely", () => {
    renderApp("/");
    expect(screen.queryByRole("navigation")).toBeNull();
  });

  it("still renders the brand link", () => {
    renderApp("/");
    expect(screen.getByRole("link", { name: "Signal Archive" })).toBeInTheDocument();
  });

  it("can reach the signup page", async () => {
    renderApp("/signup");
    expect(await screen.findByText("signup page")).toBeInTheDocument();
  });
});

describe("authenticated operators", () => {
  beforeEach(() => signedIn());

  it.each([
    ["/", "pulse page"],
    ["/feed", "feed page"],
    ["/analyze", "analyze page"],
    ["/accounts", "accounts page"],
    ["/ops", "cycles page"],
    ["/searches", "search page"],
  ])("renders %s", async (route, expected) => {
    renderApp(route);
    expect(await screen.findByText(expected)).toBeInTheDocument();
  });

  it("falls back to the dashboard for an unknown route", async () => {
    renderApp("/does-not-exist");
    expect(await screen.findByText("pulse page")).toBeInTheDocument();
  });

  // Regression: /searches was routed but absent from the nav, so the Search page
  // was unreachable by clicking anything in the UI.
  it.each(["Pulse", "Feed", "Analyze", "Searches", "Accounts", "Ops"])(
    "links to %s in the primary navigation",
    async (label) => {
      renderApp("/");
      expect(await screen.findByRole("link", { name: label })).toBeInTheDocument();
    },
  );

  it("navigates to a page when its nav link is clicked", async () => {
    const user = userEvent.setup();
    renderApp("/");
    await user.click(await screen.findByRole("link", { name: "Searches" }));
    expect(await screen.findByText("search page")).toBeInTheDocument();
  });

  it("clears the session and returns to the login form on sign out", async () => {
    const user = userEvent.setup();
    renderApp("/feed");
    await user.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(getRefreshToken()).toBeNull();
    expect(await screen.findByText("login page")).toBeInTheDocument();
  });

  it("hides the navigation after signing out", async () => {
    const user = userEvent.setup();
    renderApp("/feed");
    await user.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(screen.queryByRole("navigation")).toBeNull();
  });

  it("sends a signed-in visitor away from the login page", async () => {
    renderApp("/login");
    expect(await screen.findByText("pulse page")).toBeInTheDocument();
  });
});

describe("a non-staff reader", () => {
  beforeEach(() => signedIn({ is_staff: false }));

  it("does not see the Ops link", async () => {
    renderApp("/");
    // Wait for the session to restore before asserting on an absence.
    expect(await screen.findByRole("link", { name: "Feed" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Ops" })).toBeNull();
  });

  it("still sees the reading pages", async () => {
    renderApp("/");
    expect(await screen.findByRole("link", { name: "Analyze" })).toBeInTheDocument();
  });
});

describe("session restore", () => {
  it("does not bounce a returning visitor to the login form while refreshing", async () => {
    localStorage.setItem("tsaas_refresh", "refresh-123");
    let releaseRefresh;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url) => {
        if (String(url).includes("/auth/refresh/")) {
          await new Promise((resolve) => {
            releaseRefresh = resolve;
          });
          return { ok: true, status: 200, json: async () => ({ access: "a", refresh: "r" }) };
        }
        return { ok: true, status: 200, json: async () => ({ username: "carol", is_staff: false }) };
      }),
    );

    renderApp("/feed");

    expect(await screen.findByText(/Restoring your session/i)).toBeInTheDocument();
    expect(screen.queryByText("login page")).toBeNull();

    releaseRefresh();
    expect(await screen.findByText("feed page")).toBeInTheDocument();
  });

  it("falls through to the login form when the stored token is dead", async () => {
    localStorage.setItem("tsaas_refresh", "expired");
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 401, json: async () => ({}) })));

    renderApp("/feed");

    expect(await screen.findByText("login page")).toBeInTheDocument();
  });
});
