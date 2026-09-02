import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Ops from "./Ops";
import { api } from "../api";

// Component tests: the ops page is the operator's control surface for triggering
// cycles and repairing the X session, so each control is driven through the DOM.

vi.mock("../api", async () => {
  const actual = await vi.importActual("../api");
  return { ...actual, api: vi.fn() };
});

const run = (over = {}) => ({
  run_id: "r1",
  subsystem: "live",
  status: "completed",
  target: null,
  started_at: "2026-01-02T03:04:05Z",
  finished_at: "2026-01-02T03:05:05Z",
  summary: { ingested_tweets: 7, raw_pages: 2 },
  ...over,
});

const session = (over = {}) => ({
  configured: true,
  cookie_names: ["auth_token", "ct0"],
  header_names: ["authorization"],
  transaction_id_pools: { UserTweets: 3 },
  ...over,
});

function routeApi({ runs = [], health = session(), detail = null } = {}) {
  api.mockImplementation(async (path) => {
    if (path === "/session/") return health;
    if (path.startsWith("/runs/") && path !== "/runs/" && !path.includes("?")) {
      return detail ?? run();
    }
    if (path.startsWith("/runs/")) return { results: runs };
    return {};
  });
}

beforeEach(() => api.mockReset());

describe("session health panel", () => {
  it("reports a configured session", async () => {
    routeApi();
    render(<Ops />);
    expect(await screen.findByText("Configured")).toBeInTheDocument();
  });

  it("warns loudly when no X session is loaded", async () => {
    routeApi({ health: session({ configured: false, cookie_names: [], header_names: [] }) });
    render(<Ops />);
    expect(await screen.findByText("No active session")).toBeInTheDocument();
  });

  it("shows which credential-critical cookies are present", async () => {
    routeApi({ health: session({ cookie_names: ["auth_token"] }) });
    render(<Ops />);
    await screen.findByText("Configured");
    // auth_token present, ct0 missing, bearer present.
    expect(screen.getAllByText("present")).toHaveLength(2);
    expect(screen.getAllByText("missing")).toHaveLength(1);
  });

  it("summarises the transaction-id pools", async () => {
    routeApi();
    render(<Ops />);
    expect(await screen.findByText("UserTweets:3")).toBeInTheDocument();
  });

  it("says 'none' when no tx-id pools exist", async () => {
    routeApi({ health: session({ transaction_id_pools: {} }) });
    render(<Ops />);
    expect(await screen.findByText("none")).toBeInTheDocument();
  });

  it("keeps the update button disabled until JSON is entered", async () => {
    const user = userEvent.setup();
    routeApi();
    render(<Ops />);
    const button = await screen.findByRole("button", { name: "Update session" });
    expect(button).toBeDisabled();
    await user.type(screen.getByLabelText("X session JSON"), "{{}");
    expect(button).toBeEnabled();
  });

  it("posts a parsed session payload", async () => {
    const user = userEvent.setup();
    routeApi();
    render(<Ops />);
    await screen.findByRole("button", { name: "Update session" });
    // paste, not type: user-event treats `{` as a special key sequence.
    await user.click(screen.getByLabelText("X session JSON"));
    await user.paste('{"cookies":{"ct0":"x"}}');
    await user.click(screen.getByRole("button", { name: "Update session" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/session/", {
      method: "POST",
      body: { cookies: { ct0: "x" } },
    }));
  });

  it("rejects malformed JSON with a readable message and no request", async () => {
    const user = userEvent.setup();
    routeApi();
    render(<Ops />);
    await screen.findByRole("button", { name: "Update session" });
    await user.type(screen.getByLabelText("X session JSON"), "not json");
    await user.click(screen.getByRole("button", { name: "Update session" }));
    expect(await screen.findByText("Session must be valid JSON.")).toBeInTheDocument();
    expect(api).not.toHaveBeenCalledWith("/session/", expect.objectContaining({ method: "POST" }));
  });
});

describe("cycle controls", () => {
  it.each([["Run live poll", "live"], ["Run archive walk", "historical"], ["Run searches", "search"]])(
    "%s queues the %s subsystem",
    async (label, subsystem) => {
      const user = userEvent.setup();
      routeApi();
      render(<Ops />);
      await user.click(await screen.findByRole("button", { name: label }));
      await waitFor(() => expect(api).toHaveBeenCalledWith("/cycles/", {
        method: "POST",
        body: { subsystem },
      }));
    },
  );

  it("confirms the queued cycle", async () => {
    const user = userEvent.setup();
    routeApi();
    render(<Ops />);
    await user.click(await screen.findByRole("button", { name: "Run live poll" }));
    expect(await screen.findByText("Queued live cycle.")).toBeInTheDocument();
  });

  it("filters the run list by subsystem", async () => {
    const user = userEvent.setup();
    routeApi();
    render(<Ops />);
    await waitFor(() => expect(api).toHaveBeenCalledWith("/runs/"));
    await user.selectOptions(screen.getByLabelText("Filter by subsystem"), "search");
    await waitFor(() => expect(api).toHaveBeenCalledWith("/runs/?subsystem=search"));
  });
});

describe("run list and detail", () => {
  it("shows an empty state when nothing has run", async () => {
    routeApi({ runs: [] });
    render(<Ops />);
    expect(await screen.findByText("No fetch runs yet.")).toBeInTheDocument();
  });

  it("renders a run's status, subsystem and ingest count", async () => {
    routeApi({ runs: [run()] });
    render(<Ops />);
    expect(await screen.findByText("completed")).toBeInTheDocument();
    expect(screen.getByText("+7")).toBeInTheDocument();
  });

  it("renders an em dash when a run ingested nothing", async () => {
    routeApi({ runs: [run({ summary: { ingested_tweets: 0 } })] });
    render(<Ops />);
    expect(await screen.findByText("—")).toBeInTheDocument();
  });

  it("humanises the auth_required status", async () => {
    routeApi({ runs: [run({ status: "auth_required" })] });
    render(<Ops />);
    expect(await screen.findByText("auth required")).toBeInTheDocument();
  });

  it("offers retry only for failed or partial runs", async () => {
    routeApi({ runs: [run({ run_id: "ok" }), run({ run_id: "bad", status: "failed" })] });
    render(<Ops />);
    await screen.findByText("failed");
    expect(screen.getAllByRole("button", { name: "retry" })).toHaveLength(1);
  });

  it("re-queues the subsystem from a failed run's retry action", async () => {
    const user = userEvent.setup();
    routeApi({ runs: [run({ status: "failed", subsystem: "search" })] });
    render(<Ops />);
    await user.click(await screen.findByRole("button", { name: "retry" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/cycles/", {
      method: "POST",
      body: { subsystem: "search" },
    }));
  });

  it("prompts the operator to pick a run before any is selected", async () => {
    routeApi({ runs: [run()] });
    render(<Ops />);
    expect(await screen.findByText("Pick a run")).toBeInTheDocument();
  });

  it("shows the log excerpt when a run is inspected", async () => {
    const user = userEvent.setup();
    routeApi({
      runs: [run()],
      detail: run({ log_excerpt: "fetched 7 tweets", failure_ledger: {} }),
    });
    render(<Ops />);
    await user.click(await screen.findByRole("button", { name: /completed/ }));
    expect(await screen.findByText("fetched 7 tweets")).toBeInTheDocument();
  });

  it("renders the failure ledger for a failed run", async () => {
    const user = userEvent.setup();
    routeApi({
      runs: [run({ status: "failed" })],
      detail: run({ status: "failed", failure_ledger: { http_404: 3 } }),
    });
    render(<Ops />);
    await user.click(await screen.findByRole("button", { name: /failed/ }));
    expect(await screen.findByText(/http_404/)).toBeInTheDocument();
  });
});

describe("polling resilience", () => {
  // Fake timers only here: elsewhere they let the 10s poll fire outside act().
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
  afterEach(() => vi.useRealTimers());

  // Regression: `load` set an error but never cleared it, so one transient blip
  // pinned an error banner to the page for the rest of the session.
  it("clears a transient error once polling succeeds again", async () => {
    let failNext = true;
    api.mockImplementation(async (path) => {
      if (failNext && path.startsWith("/runs/")) {
        failNext = false;
        throw new Error("Service unavailable");
      }
      if (path === "/session/") return session();
      return { results: [] };
    });
    render(<Ops />);
    expect(await screen.findByText("Service unavailable")).toBeInTheDocument();

    await act(() => vi.advanceTimersByTimeAsync(10000));
    await waitFor(() => expect(screen.queryByText("Service unavailable")).toBeNull());
  });

  // Regression: the 10s poll re-reads only the newest page and used to replace
  // the whole list with it, so older pages vanished seconds after the reader
  // pulled them and "Load older runs" looked like a dead button.
  it("keeps older pages when the poll refreshes the newest one", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    api.mockImplementation(async (path) => {
      if (path === "/session/") return session();
      if (path.includes("cursor=")) return { results: [run({ run_id: "old-1" })], next: null };
      if (path.startsWith("/runs/?") || path === "/runs/") {
        return { results: [run({ run_id: "new-1" })], next: "/api/runs/?cursor=abc" };
      }
      return {};
    });
    render(<Ops />);
    await screen.findByText(/1 runs · more available/i);

    await user.click(screen.getByRole("button", { name: /Load older runs/i }));
    await screen.findByText(/2 runs/i);

    await act(() => vi.advanceTimersByTimeAsync(10000));

    // The freshly polled page is still there, and so is the older one.
    await waitFor(() => expect(screen.getByText(/2 runs/i)).toBeInTheDocument());
  });

  it("stops polling once unmounted", async () => {
    routeApi();
    const { unmount } = render(<Ops />);
    await waitFor(() => expect(api).toHaveBeenCalled());
    unmount();
    const callsAfterUnmount = api.mock.calls.length;
    await act(() => vi.advanceTimersByTimeAsync(30000));
    expect(api.mock.calls.length).toBe(callsAfterUnmount);
  });
});
