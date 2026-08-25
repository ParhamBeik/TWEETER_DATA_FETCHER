import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Feed from "./Feed";
import { api, setTokens } from "../api";

// Component tests: Feed is where filter state, pagination and export converge,
// so it is driven through the DOM with only the network boundary mocked.

vi.mock("../api", async () => {
  const actual = await vi.importActual("../api");
  return { ...actual, api: vi.fn() };
});

const page = (results, next = null) => ({ results, next });
const tweet = (id, text) => ({ id, tweet_id: id, account: "elonmusk", text, type: "Tweet" });

// Feed reads its filter state from the URL, so it needs a router.
const renderFeed = (initial = "/feed") =>
  render(
    <MemoryRouter initialEntries={[initial]}>
      <Feed />
    </MemoryRouter>,
  );

/** Resolves the feed request only when the returned `release` is called. */
function deferredPage(results) {
  let release;
  const promise = new Promise((resolve) => {
    release = () => resolve(page(results));
  });
  return { promise, release: () => release() };
}

/** The feed request, ignoring the roster fetch the account picker makes. */
const feedCalls = () => api.mock.calls.filter(([path]) => path.startsWith("/feed/"));
const lastFeedPath = () => feedCalls().at(-1)?.[0];

beforeEach(() => {
  api.mockReset();
  api.mockImplementation((path) =>
    Promise.resolve(path.startsWith("/accounts/") ? { results: [] } : page([])),
  );
});

describe("Feed initial render", () => {
  it("requests today's feed on mount", async () => {
    renderFeed();
    await waitFor(() => expect(lastFeedPath()).toBe("/feed/?window=24h"));
  });

  it("renders every tweet returned by the API", async () => {
    api.mockResolvedValue(page([tweet("1", "first post"), tweet("2", "second post")]));
    renderFeed();
    expect(await screen.findByText("first post")).toBeInTheDocument();
    expect(screen.getByText("second post")).toBeInTheDocument();
  });

  it("shows an empty state when nothing matches", async () => {
    renderFeed();
    expect(await screen.findByText("No posts match these filters.")).toBeInTheDocument();
  });

  it("shows the server error instead of a blank page when the feed fails", async () => {
    api.mockRejectedValue(new Error("Service unavailable"));
    renderFeed();
    expect(await screen.findByText("Service unavailable")).toBeInTheDocument();
  });

  it("does not claim the feed is empty while an error is displayed", async () => {
    api.mockRejectedValue(new Error("Service unavailable"));
    renderFeed();
    await screen.findByText("Service unavailable");
    expect(screen.queryByText("No posts match these filters.")).toBeNull();
  });
});

describe("Feed sorting and windowing", () => {
  it("asks for the engagement ordering when Most engaged is chosen", async () => {
    const user = userEvent.setup();
    renderFeed();
    await waitFor(() => expect(feedCalls().length).toBeGreaterThan(0));

    await user.click(screen.getByRole("button", { name: "Most engaged" }));

    await waitFor(() => expect(lastFeedPath()).toContain("sort=top"));
  });

  it("changes the window without losing the sort", async () => {
    const user = userEvent.setup();
    renderFeed("/feed?sort=top");
    await waitFor(() => expect(feedCalls().length).toBeGreaterThan(0));

    await user.click(screen.getByRole("button", { name: "Week" }));

    await waitFor(() => {
      const path = lastFeedPath();
      expect(path).toContain("sort=top");
      expect(path).toContain("window=7d");
    });
  });

  it("drops the window entirely for All time", async () => {
    const user = userEvent.setup();
    renderFeed();
    await waitFor(() => expect(feedCalls().length).toBeGreaterThan(0));

    await user.click(screen.getByRole("button", { name: "All time" }));

    await waitFor(() => expect(lastFeedPath()).toBe("/feed/"));
  });

  it("marks the active sort for assistive technology", async () => {
    renderFeed("/feed?sort=top");
    await waitFor(() => expect(feedCalls().length).toBeGreaterThan(0));
    expect(screen.getByRole("button", { name: "Most engaged" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Latest" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });
});

describe("Feed content filters", () => {
  it("encodes selected post types into one parameter", async () => {
    const user = userEvent.setup();
    renderFeed();
    await waitFor(() => expect(feedCalls().length).toBeGreaterThan(0));

    await user.click(screen.getByRole("button", { name: "Replies" }));
    await user.click(screen.getByRole("button", { name: "Reposts" }));

    await waitFor(() => expect(lastFeedPath()).toContain("types=reply%2Cretweet"));
  });

  it("toggles a post type back off", async () => {
    const user = userEvent.setup();
    renderFeed("/feed?types=reply");
    await waitFor(() => expect(feedCalls().length).toBeGreaterThan(0));

    await user.click(screen.getByRole("button", { name: "Replies" }));

    await waitFor(() => expect(lastFeedPath()).not.toContain("types="));
  });

  it("requests media-only posts", async () => {
    const user = userEvent.setup();
    renderFeed();
    await waitFor(() => expect(feedCalls().length).toBeGreaterThan(0));

    await user.click(screen.getByRole("button", { name: /Media only/ }));

    await waitFor(() => expect(lastFeedPath()).toContain("has_media=1"));
  });

  it("encodes the text query into the feed request", async () => {
    const user = userEvent.setup();
    renderFeed();
    await waitFor(() => expect(feedCalls().length).toBeGreaterThan(0));

    await user.type(screen.getByLabelText("Search archive"), "starship{Enter}");

    await waitFor(() => expect(lastFeedPath()).toContain("q=starship"));
  });

  it("restores the whole filter set from the URL", async () => {
    renderFeed("/feed?sort=top&window=7d&types=retweet&has_media=1&tier=2");
    await waitFor(() => {
      const path = lastFeedPath();
      expect(path).toContain("sort=top");
      expect(path).toContain("window=7d");
      expect(path).toContain("types=retweet");
      expect(path).toContain("has_media=1");
      expect(path).toContain("tier=2");
    });
  });

  it("replaces rather than appends results when a filter changes", async () => {
    const user = userEvent.setup();
    // Keyed on path, not call order: the account picker's roster request races
    // the feed request, so `mockResolvedValueOnce` is not reliably the feed's.
    let body = page([tweet("1", "old result")]);
    api.mockImplementation((path) =>
      Promise.resolve(path.startsWith("/accounts/") ? { results: [] } : body),
    );
    renderFeed();
    await screen.findByText("old result");

    body = page([tweet("2", "new result")]);
    await user.click(screen.getByRole("button", { name: "Week" }));

    expect(await screen.findByText("new result")).toBeInTheDocument();
    expect(screen.queryByText("old result")).toBeNull();
  });

  it("discards a superseded response so stale rows never paint", async () => {
    const user = userEvent.setup();
    const initial = deferredPage([tweet("1", "stale result")]);
    api.mockImplementation((path) =>
      path.startsWith("/accounts/") ? Promise.resolve({ results: [] }) : initial.promise,
    );
    renderFeed();
    await waitFor(() => expect(feedCalls().length).toBe(1));

    api.mockResolvedValue(page([tweet("2", "fresh result")]));
    await user.click(screen.getByRole("button", { name: "Week" }));
    await screen.findByText("fresh result");

    initial.release();
    await waitFor(() => expect(screen.queryByText("stale result")).toBeNull());
  });
});

describe("Feed export", () => {
  beforeEach(() => {
    setTokens({ access: "access-123", refresh: "refresh-123" });
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:mock"),
      revokeObjectURL: vi.fn(),
    });
    // jsdom does not implement navigation, so a real anchor click would warn.
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  });

  const exportFetch = (overrides = {}) =>
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      blob: async () => new Blob(["{}"]),
      ...overrides,
    });

  it("requests the chosen format with the active filters and auth token", async () => {
    const user = userEvent.setup();
    const fetchSpy = exportFetch();
    vi.stubGlobal("fetch", fetchSpy);
    renderFeed("/feed?sort=top&window=7d");
    await waitFor(() => expect(feedCalls().length).toBeGreaterThan(0));

    await user.click(screen.getByRole("button", { name: "Export CSV" }));

    await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toContain("/api/export/");
    expect(url).toContain("format=csv");
    expect(url).toContain("window=7d");
    expect(init.headers.Authorization).toBe("Bearer access-123");
  });

  it("requests JSONL from the JSONL button", async () => {
    const user = userEvent.setup();
    const fetchSpy = exportFetch();
    vi.stubGlobal("fetch", fetchSpy);
    renderFeed();
    await waitFor(() => expect(feedCalls().length).toBeGreaterThan(0));
    await user.click(screen.getByRole("button", { name: "Export JSONL" }));
    await waitFor(() => expect(fetchSpy.mock.calls[0][0]).toContain("format=jsonl"));
  });

  it("reports the status code when the export request is rejected", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      exportFetch({ ok: false, status: 503, statusText: "Service Unavailable" }),
    );
    renderFeed();
    await waitFor(() => expect(feedCalls().length).toBeGreaterThan(0));
    await user.click(screen.getByRole("button", { name: "Export CSV" }));
    expect(await screen.findByText(/Export failed \(503/)).toBeInTheDocument();
  });

  // Regression: a network rejection here used to escape as an unhandled promise,
  // leaving the operator with a button that silently did nothing.
  it("reports a network failure instead of rejecting unhandled", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    renderFeed();
    await waitFor(() => expect(feedCalls().length).toBeGreaterThan(0));
    await user.click(screen.getByRole("button", { name: "Export CSV" }));
    expect(await screen.findByText(/Export failed — the API is unreachable/)).toBeInTheDocument();
  });
});
