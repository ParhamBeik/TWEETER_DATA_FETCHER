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
    await waitFor(() => expect(lastFeedPath()).toBe("/feed/?window=today"));
  });

  it("renders every tweet returned by the API", async () => {
    api.mockResolvedValue(page([tweet("1", "first post"), tweet("2", "second post")]));
    renderFeed();
    expect(await screen.findByText("first post")).toBeInTheDocument();
    expect(screen.getByText("second post")).toBeInTheDocument();
  });

  it("shows an empty state when nothing matches", async () => {
    renderFeed();
    expect(await screen.findByText("No posts match these filters")).toBeInTheDocument();
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
    expect(screen.queryByText("No posts match these filters")).toBeNull();
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

    await user.click(screen.getByRole("button", { name: "This week" }));

    await waitFor(() => {
      const path = lastFeedPath();
      expect(path).toContain("sort=top");
      expect(path).toContain("window=week");
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

  it("can reach posts from accounts that are no longer tracked", async () => {
    // They stay archived and stay inside "archive total", so without this the
    // headline counted rows no screen could open.
    const user = userEvent.setup();
    renderFeed();
    await waitFor(() => expect(feedCalls().length).toBeGreaterThan(0));

    await user.click(screen.getByRole("button", { name: /Include untracked/ }));

    await waitFor(() => expect(lastFeedPath()).toContain("include_untracked=1"));
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
    await user.click(screen.getByRole("button", { name: "This week" }));

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
    await user.click(screen.getByRole("button", { name: "This week" }));
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

  // The export is a job now: POST to queue it, poll until it finishes, then
  // fetch the file from an authenticated download URL. It moved off the request
  // thread because streaming it held one of two gunicorn workers for the whole
  // download, so two at once took the console down.
  const finishedJob = (overrides = {}) => ({
    id: 7,
    status: "completed",
    row_count: 2,
    truncated: false,
    error: "",
    filename: "tweets_2026-09-03.csv",
    download_url: "/api/export/7/download/",
    ...overrides,
  });

  /** Route the feed, the roster, and the two export calls through one mock. */
  const mockExportApi = (job = finishedJob(), { queued } = {}) => {
    api.mockImplementation((path, options) => {
      if (path === "/export/" && options?.method === "POST") {
        return Promise.resolve(queued ?? { id: job.id, status: "pending" });
      }
      if (path.startsWith("/export/")) return Promise.resolve(job);
      if (path.startsWith("/accounts/")) return Promise.resolve({ results: [] });
      return Promise.resolve(page([]));
    });
  };

  const downloadFetch = (overrides = {}) =>
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      blob: async () => new Blob(["{}"]),
      ...overrides,
    });

  it("queues the chosen format with the active filters", async () => {
    const user = userEvent.setup();
    mockExportApi();
    vi.stubGlobal("fetch", downloadFetch());
    renderFeed("/feed?sort=top&window=7d");
    await waitFor(() => expect(feedCalls().length).toBeGreaterThan(0));

    await user.click(screen.getByRole("button", { name: "Export CSV" }));

    await waitFor(() =>
      expect(api).toHaveBeenCalledWith("/export/", expect.objectContaining({ method: "POST" })),
    );
    const [, options] = api.mock.calls.find(([path]) => path === "/export/");
    expect(options.body.format).toBe("csv");
    // The filters travel as the feed's own query string, so the worker rebuilds
    // exactly the queryset that was on screen.
    expect(options.body.query).toContain("window=7d");
    expect(options.body.query).toContain("sort=top");
  });

  it("queues JSONL from the JSONL button", async () => {
    const user = userEvent.setup();
    mockExportApi();
    vi.stubGlobal("fetch", downloadFetch());
    renderFeed();
    await waitFor(() => expect(feedCalls().length).toBeGreaterThan(0));
    await user.click(screen.getByRole("button", { name: "Export JSONL" }));
    await waitFor(() => {
      const call = api.mock.calls.find(([path]) => path === "/export/");
      expect(call[1].body.format).toBe("jsonl");
    });
  });

  it("downloads the finished file with the authenticated URL", async () => {
    const user = userEvent.setup();
    mockExportApi();
    const fetchSpy = downloadFetch();
    vi.stubGlobal("fetch", fetchSpy);
    renderFeed();
    await waitFor(() => expect(feedCalls().length).toBeGreaterThan(0));

    await user.click(screen.getByRole("button", { name: "Export CSV" }));

    await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe("/api/export/7/download/");
    // The file is behind auth, so it cannot be a plain link.
    expect(init.headers.Authorization).toBe("Bearer access-123");
  });

  it("says so when the export hit the row ceiling", async () => {
    const user = userEvent.setup();
    mockExportApi(finishedJob({ truncated: true, row_count: 100000 }));
    vi.stubGlobal("fetch", downloadFetch());
    renderFeed();
    await waitFor(() => expect(feedCalls().length).toBeGreaterThan(0));

    await user.click(screen.getByRole("button", { name: "Export CSV" }));

    // A prefix of the answer presented as the answer is the failure mode worth
    // guarding against; it is a notice rather than an error because the file
    // that downloaded is still correct as far as it goes.
    expect(await screen.findByText(/first 100,000 posts/)).toBeInTheDocument();
  });

  it("reports a failed job rather than downloading nothing", async () => {
    const user = userEvent.setup();
    mockExportApi(finishedJob({ status: "failed", error: "disk full", download_url: null }));
    vi.stubGlobal("fetch", downloadFetch());
    renderFeed();
    await waitFor(() => expect(feedCalls().length).toBeGreaterThan(0));
    await user.click(screen.getByRole("button", { name: "Export CSV" }));
    expect(await screen.findByText("disk full")).toBeInTheDocument();
  });

  // Regression: a network rejection here used to escape as an unhandled promise,
  // leaving the operator with a button that silently did nothing.
  it("reports a network failure instead of rejecting unhandled", async () => {
    const user = userEvent.setup();
    api.mockImplementation((path) =>
      path.startsWith("/accounts/")
        ? Promise.resolve({ results: [] })
        : path.startsWith("/export/")
          ? Promise.reject(new Error("Network error — the API is unreachable."))
          : Promise.resolve(page([])),
    );
    renderFeed();
    await waitFor(() => expect(feedCalls().length).toBeGreaterThan(0));
    await user.click(screen.getByRole("button", { name: "Export CSV" }));
    expect(await screen.findByText(/the API is unreachable/)).toBeInTheDocument();
  });

  it("disables both buttons while a job is running", async () => {
    const user = userEvent.setup();
    mockExportApi(finishedJob({ status: "pending" }));
    vi.stubGlobal("fetch", downloadFetch());
    renderFeed();
    await waitFor(() => expect(feedCalls().length).toBeGreaterThan(0));

    await user.click(screen.getByRole("button", { name: "Export CSV" }));

    // A button that looks idle while a job runs invites a second one.
    expect(await screen.findByRole("button", { name: /Preparing CSV/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Export JSONL" })).toBeDisabled();
  });
});

// Regressions from the August 2026 QA pass. Each pins a behaviour that was
// wrong on the live site, so it cannot quietly come back.
describe("Feed QA regressions", () => {
  it("offers reach as its own sort rather than folding views into engagement", async () => {
    const user = userEvent.setup();
    renderFeed();
    await waitFor(() => expect(feedCalls().length).toBe(1));

    await user.click(screen.getByRole("button", { name: "Most viewed" }));
    await waitFor(() => expect(lastFeedPath()).toContain("sort=views"));

    await user.click(screen.getByRole("button", { name: "Most engaged" }));
    await waitFor(() => expect(lastFeedPath()).toContain("sort=top"));
  });

  it("searches as you type, without needing Enter", async () => {
    const user = userEvent.setup();
    renderFeed();
    await waitFor(() => expect(feedCalls().length).toBe(1));

    await user.type(screen.getByLabelText("Search archive"), "trump");
    // Debounced: the request arrives on its own, with no submit.
    await waitFor(() => expect(lastFeedPath()).toContain("q=trump"), { timeout: 2000 });
  });

  it("clears the query when the box is emptied", async () => {
    const user = userEvent.setup();
    renderFeed("/feed?q=trump");
    await waitFor(() => expect(lastFeedPath()).toContain("q=trump"));

    await user.click(screen.getByRole("button", { name: "Clear search" }));
    await waitFor(() => expect(lastFeedPath()).not.toContain("q="));
    expect(screen.getByLabelText("Search archive")).toHaveValue("");
  });

  it("asks for calendar windows, not rolling ones dressed up as days", async () => {
    const user = userEvent.setup();
    renderFeed();
    await waitFor(() => expect(lastFeedPath()).toContain("window=today"));

    await user.click(screen.getByRole("button", { name: "This month" }));
    await waitFor(() => expect(lastFeedPath()).toContain("window=month"));
  });

  it("renders the cleaned text and lets the browser pick text direction", async () => {
    api.mockImplementation((path) =>
      Promise.resolve(
        path.startsWith("/accounts/")
          ? { results: [] }
          : page([
              {
                ...tweet("1", "raw R&amp;D https://t.co/x https://t.co/x"),
                text_clean: "raw R&D https://t.co/x",
              },
            ]),
      ),
    );
    renderFeed();
    const body = await screen.findByText("raw R&D https://t.co/x");
    expect(body).toHaveAttribute("dir", "auto");
  });
});
