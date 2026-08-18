import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { getToken, setToken } from "./api";

// Component tests for the shell only: every page is stubbed so these assert
// routing, the auth guard and the nav contract rather than page internals.

vi.mock("./pages/Feed", () => ({ default: () => <div>feed page</div> }));
vi.mock("./pages/Search", () => ({ default: () => <div>search page</div> }));
vi.mock("./pages/Accounts", () => ({ default: () => <div>accounts page</div> }));
vi.mock("./pages/Cycles", () => ({ default: () => <div>cycles page</div> }));
vi.mock("./pages/Pulse", () => ({ default: () => <div>pulse page</div> }));
vi.mock("./pages/Analyze", () => ({ default: () => <div>analyze page</div> }));
vi.mock("./pages/Login", () => ({ default: () => <div>login page</div> }));

const ROUTER_FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true };

const renderApp = (route = "/") =>
  render(
    <MemoryRouter initialEntries={[route]} future={ROUTER_FUTURE}>
      <App />
    </MemoryRouter>,
  );

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
});

describe("authenticated operators", () => {
  beforeEach(() => setToken("tok-123"));

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
    (label) => {
      renderApp("/");
      expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
    },
  );

  it("navigates to a page when its nav link is clicked", async () => {
    const user = userEvent.setup();
    renderApp("/");
    await user.click(screen.getByRole("link", { name: "Searches" }));
    expect(await screen.findByText("search page")).toBeInTheDocument();
  });

  it("clears the token and returns to the login form on logout", async () => {
    const user = userEvent.setup();
    renderApp("/feed");
    await user.click(screen.getByRole("button", { name: "Logout" }));
    expect(getToken()).toBeNull();
    expect(await screen.findByText("login page")).toBeInTheDocument();
  });

  it("hides the navigation after logging out", async () => {
    const user = userEvent.setup();
    renderApp("/feed");
    await user.click(screen.getByRole("button", { name: "Logout" }));
    expect(screen.queryByRole("navigation")).toBeNull();
  });
});
