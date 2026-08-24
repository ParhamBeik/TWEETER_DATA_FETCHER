import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Feed from "./Feed";
import { api, setTokens } from "../api";

// Component tests: Feed is where filter state, pagination and export converge,
// so it is driven through the DOM with only the network boundary mocked.

vi.mock("../api", async () => {
  const actual = await vi.importActual("../api");
  return { ...actual, api: vi.fn() };
});

// RunStatus polls /runs/ on an interval and is not the subject here.
vi.mock("../RunStatus", () => ({ default: () => null }));

const page = (results, next = null) => ({ results, next });
const tweet = (id, text) => ({ id, account: "elonmusk", text });

/** Resolves the feed request only when the returned `release` is called. */
function deferredPage(results) {
  let release;
  const promise = new Promise((resolve) => {
    release = () => resolve(page(results));
  });
  return { promise, release: () => release() };
}

beforeEach(() => {
  api.mockReset();
  api.mockResolvedValue(page([]));
});

describe("Feed initial render", () => {
  it("requests the unfiltered feed on mount", async () => {
    render(<Feed />);
    await waitFor(() => expect(api).toHaveBeenCalledWith("/feed/"));
  });

  it("renders every tweet returned by the API", async () => {
    api.mockResolvedValue(page([tweet("1", "first post"), tweet("2", "second post")]));
    render(<Feed />);
    expect(await screen.findByText("first post")).toBeInTheDocument();
    expect(screen.getByText("second post")).toBeInTheDocument();
  });

  it("shows an empty state when the archive returns nothing", async () => {
    render(<Feed />);
    expect(await screen.findByText("No tweets yet.")).toBeInTheDocument();
  });

  it("shows the server error instead of a blank page when the feed fails", async () => {
    api.mockRejectedValue(new Error("Service unavailable"));
    render(<Feed />);
    expect(await screen.findByText("Service unavailable")).toBeInTheDocument();
  });

  it("does not claim the archive is empty while an error is displayed", async () => {
    api.mockRejectedValue(new Error("Service unavailable"));
    render(<Feed />);
    await screen.findByText("Service unavailable");
    expect(screen.queryByText("No tweets yet.")).toBeNull();
  });
});

describe("Feed filters", () => {
  it("labels every filter control for assistive technology", async () => {
    render(<Feed />);
    await waitFor(() => expect(api).toHaveBeenCalled());
    ["Search archive", "Account", "Tier", "Posted after", "Posted before", "Run id"]
      .forEach((name) => expect(screen.getByLabelText(name)).toBeInTheDocument());
  });

  it("encodes the text query into the feed request", async () => {
    const user = userEvent.setup();
    render(<Feed />);
    await waitFor(() => expect(api).toHaveBeenCalled());
    await user.type(screen.getByLabelText("Search archive"), "starship");
    await user.click(screen.getByRole("button", { name: "Filter" }));
    await waitFor(() => expect(api).toHaveBeenLastCalledWith("/feed/?q=starship"));
  });

  it("combines several filters into one query string", async () => {
    const user = userEvent.setup();
    render(<Feed />);
    await waitFor(() => expect(api).toHaveBeenCalled());
    await user.type(screen.getByLabelText("Account"), "elonmusk");
    await user.selectOptions(screen.getByLabelText("Tier"), "2");
    await user.click(screen.getByRole("button", { name: "Filter" }));
    await waitFor(() => {
      const path = api.mock.lastCall[0];
      expect(path).toContain("account=elonmusk");
      expect(path).toContain("tier=2");
    });
  });

  it("omits blank filters from the query string", async () => {
    const user = userEvent.setup();
    render(<Feed />);
    await waitFor(() => expect(api).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: "Filter" }));
    await waitFor(() => expect(api).toHaveBeenLastCalledWith("/feed/"));
  });

  it("replaces rather than appends results when a filter is applied", async () => {
    const user = userEvent.setup();
    api.mockResolvedValueOnce(page([tweet("1", "old result")]));
    render(<Feed />);
    await screen.findByText("old result");
    api.mockResolvedValueOnce(page([tweet("2", "new result")]));
    await user.type(screen.getByLabelText("Search archive"), "starship");
    await user.click(screen.getByRole("button", { name: "Filter" }));
    expect(await screen.findByText("new result")).toBeInTheDocument();
    expect(screen.queryByText("old result")).toBeNull();
  });

  // Regression: a single in-flight guard covered both the filter path and the
  // infinite-scroll append, so filtering during the initial load was dropped and
  // the feed stayed empty until the user acted again.
  it("still applies a filter submitted while the first page is loading", async () => {
    const user = userEvent.setup();
    const initial = deferredPage([tweet("1", "old result")]);
    api.mockReturnValueOnce(initial.promise);
    render(<Feed />);
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1));

    api.mockResolvedValueOnce(page([tweet("2", "filtered result")]));
    await user.type(screen.getByLabelText("Search archive"), "starship");
    await user.click(screen.getByRole("button", { name: "Filter" }));

    await waitFor(() => expect(api).toHaveBeenLastCalledWith("/feed/?q=starship"));
    initial.release();
    expect(await screen.findByText("filtered result")).toBeInTheDocument();
  });

  it("discards a superseded response so stale rows never paint", async () => {
    const user = userEvent.setup();
    const initial = deferredPage([tweet("1", "stale result")]);
    api.mockReturnValueOnce(initial.promise);
    render(<Feed />);
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1));

    api.mockResolvedValueOnce(page([tweet("2", "fresh result")]));
    await user.type(screen.getByLabelText("Search archive"), "starship");
    await user.click(screen.getByRole("button", { name: "Filter" }));
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

  const exportFetch = (overrides = {}) => vi.fn().mockResolvedValue({
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
    render(<Feed />);
    await waitFor(() => expect(api).toHaveBeenCalled());
    await user.type(screen.getByLabelText("Account"), "elonmusk");
    await user.click(screen.getByRole("button", { name: "Filter" }));
    await waitFor(() => expect(api).toHaveBeenCalledTimes(2));

    await user.click(screen.getByRole("button", { name: "Export CSV" }));
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toContain("/api/export/");
    expect(url).toContain("format=csv");
    expect(url).toContain("account=elonmusk");
    expect(init.headers.Authorization).toBe("Bearer access-123");
  });

  it("requests JSONL from the JSONL button", async () => {
    const user = userEvent.setup();
    const fetchSpy = exportFetch();
    vi.stubGlobal("fetch", fetchSpy);
    render(<Feed />);
    await waitFor(() => expect(api).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: "Export JSONL" }));
    await waitFor(() => expect(fetchSpy.mock.calls[0][0]).toContain("format=jsonl"));
  });

  it("reports the status code when the export request is rejected", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", exportFetch({ ok: false, status: 503, statusText: "Service Unavailable" }));
    render(<Feed />);
    await waitFor(() => expect(api).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: "Export CSV" }));
    expect(await screen.findByText(/Export failed \(503/)).toBeInTheDocument();
  });

  // Regression: a network rejection here used to escape as an unhandled promise,
  // leaving the operator with a button that silently did nothing.
  it("reports a network failure instead of rejecting unhandled", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    render(<Feed />);
    await waitFor(() => expect(api).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: "Export CSV" }));
    expect(await screen.findByText(/Export failed — the API is unreachable/)).toBeInTheDocument();
  });
});
