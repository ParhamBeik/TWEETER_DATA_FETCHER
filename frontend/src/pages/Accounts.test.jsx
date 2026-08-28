import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Accounts from "./Accounts";
import { api } from "../api";

// Component tests: the roster table is the operator's main control surface, so
// every row action is exercised through the DOM with the API boundary mocked.

vi.mock("../api", async () => {
  const actual = await vi.importActual("../api");
  return { ...actual, api: vi.fn() };
});

const account = (handle, over = {}) => ({
  handle,
  priority: 3,
  poll_interval_seconds: 900,
  tracking: true,
  quarantined: false,
  recent_tweet_count: 12,
  last_checked_at: "2026-01-02T03:04:05Z",
  ...over,
});

/** Routes each mocked call by path so tests can ignore call ordering. */
function routeApi({ accounts = [], analytics = [], tweets = { results: [], next: null } } = {}) {
  api.mockImplementation(async (path) => {
    if (path === "/accounts/") return accounts;
    if (path === "/analytics/accounts/") return { results: analytics };
    if (path.endsWith("/tweets/")) return tweets;
    return {};
  });
}

beforeEach(() => api.mockReset());

describe("Accounts roster", () => {
  it("loads the roster and its analytics together", async () => {
    routeApi({ accounts: [account("elonmusk")] });
    render(<Accounts />);
    await waitFor(() => expect(api).toHaveBeenCalledWith("/accounts/"));
    expect(api).toHaveBeenCalledWith("/analytics/accounts/");
  });

  it("renders a row per tracked account", async () => {
    routeApi({ accounts: [account("elonmusk"), account("jack")] });
    render(<Accounts />);
    expect(await screen.findByRole("button", { name: "@elonmusk" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "@jack" })).toBeInTheDocument();
  });

  // Regression: the empty state rendered before the roster request resolved, so
  // every load flashed "No tracked accounts yet" at the operator.
  it("shows a loading state rather than the empty state while fetching", async () => {
    api.mockImplementation(() => new Promise(() => {}));
    render(<Accounts />);
    expect(await screen.findByText("Loading accounts…")).toBeInTheDocument();
    expect(screen.queryByText("No tracked accounts yet")).toBeNull();
  });

  it("shows the empty state only once the roster is known to be empty", async () => {
    routeApi({ accounts: [] });
    render(<Accounts />);
    expect(await screen.findByText("No tracked accounts yet")).toBeInTheDocument();
  });

  it("surfaces a roster failure to the operator", async () => {
    api.mockRejectedValue(new Error("Service unavailable"));
    render(<Accounts />);
    expect(await screen.findByText("Service unavailable")).toBeInTheDocument();
  });

  it("renders tracking, quarantine and off states distinctly", async () => {
    routeApi({
      accounts: [
        account("tracked"),
        account("paused", { tracking: false }),
        account("blocked", { quarantined: true, quarantine_reason: "user id unresolved" }),
      ],
    });
    render(<Accounts />);
    expect(await screen.findByText("tracking")).toBeInTheDocument();
    expect(screen.getByText("off")).toBeInTheDocument();
    expect(screen.getByText("quarantined")).toBeInTheDocument();
    expect(screen.getByText("user id unresolved")).toBeInTheDocument();
  });

  it("renders 'never' when an account has not been checked yet", async () => {
    routeApi({ accounts: [account("elonmusk", { last_checked_at: null })] });
    render(<Accounts />);
    expect(await screen.findByText("never")).toBeInTheDocument();
  });
});

describe("tracking a new account", () => {
  it("keeps Track disabled until a handle is entered", async () => {
    const user = userEvent.setup();
    routeApi();
    render(<Accounts />);
    const track = await screen.findByRole("button", { name: "Track account" });
    expect(track).toBeDisabled();
    await user.type(screen.getByLabelText("Account handle"), "elonmusk");
    expect(track).toBeEnabled();
  });

  it("strips a leading @ and posts the selected tier", async () => {
    const user = userEvent.setup();
    routeApi();
    render(<Accounts />);
    await screen.findByRole("button", { name: "Track account" });
    await user.type(screen.getByLabelText("Account handle"), "@elonmusk");
    await user.selectOptions(screen.getByLabelText("Priority tier"), "2");
    await user.click(screen.getByRole("button", { name: "Track account" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/accounts/", {
      method: "POST",
      body: { handle: "elonmusk", priority: 2 },
    }));
  });

  it("confirms the queued fetch and clears the input", async () => {
    const user = userEvent.setup();
    routeApi();
    render(<Accounts />);
    await screen.findByRole("button", { name: "Track account" });
    const input = screen.getByLabelText("Account handle");
    await user.type(input, "elonmusk");
    await user.click(screen.getByRole("button", { name: "Track account" }));
    expect(await screen.findByText("Account tracked; initial fetch queued.")).toBeInTheDocument();
    expect(input).toHaveValue("");
  });

  it("reports a rejected handle without clearing the field", async () => {
    const user = userEvent.setup();
    routeApi();
    api.mockImplementation(async (path, opts) => {
      if (opts?.method === "POST") throw new Error("No such account on X.");
      return path === "/accounts/" ? [] : { results: [] };
    });
    render(<Accounts />);
    await screen.findByRole("button", { name: "Track account" });
    await user.type(screen.getByLabelText("Account handle"), "nope");
    await user.click(screen.getByRole("button", { name: "Track account" }));
    expect(await screen.findByText("No such account on X.")).toBeInTheDocument();
  });
});

describe("row actions", () => {
  it("queues an on-demand fetch and confirms it", async () => {
    const user = userEvent.setup();
    routeApi({ accounts: [account("elonmusk")] });
    render(<Accounts />);
    await screen.findByRole("button", { name: "@elonmusk" });
    await user.click(screen.getByRole("button", { name: "Fetch" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/accounts/elonmusk/fetch/", { method: "POST" }));
    expect(await screen.findByText("Queued live + historical for @elonmusk.")).toBeInTheDocument();
  });

  it("disables tracking from the row toggle", async () => {
    const user = userEvent.setup();
    routeApi({ accounts: [account("elonmusk")] });
    render(<Accounts />);
    await screen.findByRole("button", { name: "@elonmusk" });
    await user.click(screen.getByRole("button", { name: "Disable" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/accounts/elonmusk/", {
      method: "PATCH",
      body: { tracking: false },
    }));
  });

  it("re-enables a paused account", async () => {
    const user = userEvent.setup();
    routeApi({ accounts: [account("elonmusk", { tracking: false })] });
    render(<Accounts />);
    await screen.findByRole("button", { name: "@elonmusk" });
    await user.click(screen.getByRole("button", { name: "Enable" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/accounts/elonmusk/", {
      method: "PATCH",
      body: { tracking: true },
    }));
  });

  it("offers unquarantine only for quarantined accounts", async () => {
    routeApi({ accounts: [account("ok"), account("blocked", { quarantined: true })] });
    render(<Accounts />);
    await screen.findByRole("button", { name: "@ok" });
    expect(screen.getAllByRole("button", { name: "Unquarantine" })).toHaveLength(1);
  });

  it("clears quarantine through the row action", async () => {
    const user = userEvent.setup();
    routeApi({ accounts: [account("blocked", { quarantined: true })] });
    render(<Accounts />);
    await screen.findByRole("button", { name: "@blocked" });
    await user.click(screen.getByRole("button", { name: "Unquarantine" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/accounts/blocked/", {
      method: "PATCH",
      body: { quarantined: false },
    }));
  });

  it("changes an account's tier from the row select", async () => {
    const user = userEvent.setup();
    routeApi({ accounts: [account("elonmusk", { priority: 3 })] });
    render(<Accounts />);
    await screen.findByRole("button", { name: "@elonmusk" });
    const row = screen.getByRole("row", { name: /elonmusk/ });
    await user.selectOptions(within(row).getByRole("combobox"), "1");
    await waitFor(() => expect(api).toHaveBeenCalledWith("/accounts/elonmusk/", {
      method: "PATCH",
      body: { priority: 1 },
    }));
  });
});

describe("account timeline", () => {
  it("loads the timeline when a handle is clicked", async () => {
    const user = userEvent.setup();
    routeApi({
      accounts: [account("elonmusk")],
      tweets: { results: [{ id: "1", account: "elonmusk", text: "hello there" }], next: null },
    });
    render(<Accounts />);
    await user.click(await screen.findByRole("button", { name: "@elonmusk" }));
    expect(await screen.findByText("hello there")).toBeInTheDocument();
  });

  it("explains an empty timeline rather than rendering nothing", async () => {
    const user = userEvent.setup();
    routeApi({ accounts: [account("elonmusk")] });
    render(<Accounts />);
    await user.click(await screen.findByRole("button", { name: "@elonmusk" }));
    expect(await screen.findByText(/Nothing collected yet/)).toBeInTheDocument();
  });

  // Regression: `if (loading) return` covered the initial open as well as the
  // page-append, so clicking a second account mid-load was silently ignored.
  it("switches to another account while the first timeline is still loading", async () => {
    const user = userEvent.setup();
    let releaseFirst;
    api.mockImplementation(async (path) => {
      if (path === "/accounts/") return [account("elonmusk"), account("jack")];
      if (path === "/analytics/accounts/") return { results: [] };
      if (path === "/accounts/elonmusk/tweets/") {
        return new Promise((resolve) => {
          releaseFirst = () => resolve({ results: [{ id: "1", account: "elonmusk", text: "elon post" }] });
        });
      }
      return { results: [{ id: "2", account: "jack", text: "jack post" }], next: null };
    });
    render(<Accounts />);
    await user.click(await screen.findByRole("button", { name: "@elonmusk" }));
    await user.click(screen.getByRole("button", { name: "@jack" }));
    expect(await screen.findByText("jack post")).toBeInTheDocument();

    // The superseded response must not paint into the newly selected timeline.
    releaseFirst();
    await waitFor(() => expect(screen.queryByText("elon post")).toBeNull());
  });
});

describe("comparison tray", () => {
  it("adds a checked account to the comparison panel", async () => {
    const user = userEvent.setup();
    routeApi({
      accounts: [account("elonmusk")],
      analytics: [{ account: "elonmusk", posts: 42, average_engagement: 1234.6 }],
    });
    render(<Accounts />);
    await user.click(await screen.findByLabelText("Compare @elonmusk"));
    expect(await screen.findByText("Compare accounts")).toBeInTheDocument();
    expect(screen.getByText(/42 posts/)).toBeInTheDocument();
  });

  it("removes the account again when unchecked", async () => {
    const user = userEvent.setup();
    routeApi({ accounts: [account("elonmusk")] });
    render(<Accounts />);
    const checkbox = await screen.findByLabelText("Compare @elonmusk");
    await user.click(checkbox);
    await screen.findByText("Compare accounts");
    await user.click(checkbox);
    await waitFor(() => expect(screen.queryByText("Compare accounts")).toBeNull());
  });

  it("keeps at most four accounts in the tray", async () => {
    const user = userEvent.setup();
    const handles = ["a", "b", "c", "d", "e"];
    routeApi({ accounts: handles.map((h) => account(h)) });
    render(<Accounts />);
    await screen.findByRole("button", { name: "@a" });
    for (const h of handles) {
      await user.click(screen.getByLabelText(`Compare @${h}`));
    }
    const tray = screen.getByText("Compare accounts").closest("section");
    expect(within(tray).getAllByText(/^@/)).toHaveLength(4);
    expect(within(tray).queryByText("@a")).toBeNull();
  });
});
