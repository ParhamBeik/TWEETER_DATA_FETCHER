import { render, screen, within } from "@testing-library/react";
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
vi.mock("./pages/Ops", () => ({ default: () => <div>ops page</div> }));
vi.mock("./pages/Dashboard", () => ({ default: () => <div>dashboard page</div> }));
vi.mock("./pages/Analyze", () => ({ default: () => <div>analyze page</div> }));
vi.mock("./pages/Login", () => ({ default: () => <div>login page</div> }));
vi.mock("./pages/Signup", () => ({ default: () => <div>signup page</div> }));
// The budget rail polls collector state on every screen; the shell tests are
// about routing, not about what it reports.
vi.mock("./BudgetRail", () => ({ default: () => null }));

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
    ["/", "root"],
    ["/feed", "feed"],
    ["/analyze", "analyze"],
    ["/accounts", "accounts"],
    ["/ops", "ops"],
    ["/search", "search"],
    ["/dashboard", "dashboard"],
  ])("redirects %s to the login form", async (route) => {
    renderApp(route);
    expect(await screen.findByText("login page")).toBeInTheDocument();
  });

  it("hides the primary navigation entirely", () => {
    renderApp("/");
    expect(screen.queryByRole("navigation")).toBeNull();
  });

  // The signed-out shell is the form and nothing else: a marketing panel and a
  // brand link to a place you cannot reach are chrome this console has no use
  // for.

  it("can reach the signup page", async () => {
    renderApp("/signup");
    expect(await screen.findByText("signup page")).toBeInTheDocument();
  });
});

describe("authenticated operators", () => {
  beforeEach(() => signedIn());

  it.each([
    ["/dashboard", "dashboard page"],
    ["/feed", "feed page"],
    ["/analyze", "analyze page"],
    ["/accounts", "accounts page"],
    ["/ops", "ops page"],
    ["/search", "search page"],
    ["/search/12", "search page"],
  ])("renders %s", async (route, expected) => {
    renderApp(route);
    expect(await screen.findByText(expected)).toBeInTheDocument();
  });

  // The feed, not the dashboard: someone who lands here came to read what was
  // collected, and instrument readings are a deliberate second stop.
  it("lands on the feed at the root", async () => {
    renderApp("/");
    expect(await screen.findByText("feed page")).toBeInTheDocument();
  });

  it("puts the archive mark on the home link", async () => {
    renderApp("/feed");
    expect(await screen.findByRole("link", { name: "Signal Archive" })).toBeInTheDocument();
  });

  it.each([
    ["/pulse", "Dashboard"],
    ["/cycles", "Ops"],
    ["/searches", "Search"],
  ])("explains where the old %s bookmark went", async (route, target) => {
    renderApp(route);
    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent(target);
    expect(notice).toHaveTextContent("Redirecting");
  });

  it("forwards /searches/:id toward the search workspace", async () => {
    renderApp("/searches/12");
    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("Search");
    expect(within(notice).getByRole("link", { name: "Search" })).toHaveAttribute("href", "/search/12");
  });

  it("falls back to the feed for an unknown route", async () => {
    renderApp("/does-not-exist");
    expect(await screen.findByText("feed page")).toBeInTheDocument();
  });

  // Regression: a route that is not in the nav is unreachable by clicking
  // anything in the UI, which is how the search page was once stranded.
  it.each(["Feed", "Search", "Dashboard", "Analyze", "Accounts", "Ops"])(
    "links to %s in the primary navigation",
    async (label) => {
      renderApp("/");
      expect(await screen.findByRole("link", { name: label })).toBeInTheDocument();
    },
  );

  it("navigates to a page when its nav link is clicked", async () => {
    const user = userEvent.setup();
    renderApp("/feed");
    await user.click(await screen.findByRole("link", { name: "Search" }));
    expect(await screen.findByText("search page")).toBeInTheDocument();
  });

  // Order is the operator's order of attention, and it is a contract: the
  // dashboard used to be first, which put instrument readings in front of a
  // person who came to read posts.
  it("orders the navigation feed, search, then dashboard", async () => {
    renderApp("/feed");
    await screen.findByRole("link", { name: "Feed" });
    const nav = screen.getByRole("navigation", { name: "Sections" });
    const labels = within(nav)
      .getAllByRole("link")
      .map((link) => link.getAttribute("href"));
    expect(labels.slice(0, 3)).toEqual(["/feed", "/search", "/dashboard"]);
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
    expect(await screen.findByText("feed page")).toBeInTheDocument();
  });
});

describe("a non-staff reader", () => {
  beforeEach(() => signedIn({ is_staff: false }));

  it("does not see the Ops link", async () => {
    renderApp("/feed");
    // Wait for the session to restore before asserting on an absence.
    expect(await screen.findByRole("link", { name: "Feed" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Ops" })).toBeNull();
  });

  it("still sees the reading pages", async () => {
    renderApp("/feed");
    expect(await screen.findByRole("link", { name: "Analyze" })).toBeInTheDocument();
  });

  it("sends a typed /ops URL back to the feed", async () => {
    renderApp("/ops");
    expect(await screen.findByText("feed page")).toBeInTheDocument();
    expect(screen.queryByText("ops page")).toBeNull();
  });

  it("exposes a skip link to the main column", async () => {
    renderApp("/feed");
    expect(await screen.findByRole("link", { name: "Skip to main content" })).toHaveAttribute(
      "href",
      "#main-content",
    );
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


it("contains mobile menu focus and restores it after Escape", async () => {
  signedIn();
  const user = userEvent.setup();
  renderApp("/feed");
  const trigger = await screen.findByRole("button", { name: "Open menu" });
  await user.click(trigger);
  const dialog = screen.getByRole("dialog", { name: "Menu" });
  expect(dialog).toContainElement(document.activeElement);
  const last = within(dialog).getByRole("button", { name: "Sign out" });
  last.focus();
  await user.tab();
  expect(dialog).toContainElement(document.activeElement);
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(trigger).toHaveFocus();
});
