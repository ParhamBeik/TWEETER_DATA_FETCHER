import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SearchWorkspace from "./index";
import { buildQuery } from "./query";
import { api } from "../../api";

// Component tests: the workspace is the operator's control surface for the
// search collector, so every control is driven through the DOM and the network
// boundary is the only thing mocked. The one genuinely pure piece -- composing a
// raw X query out of the builder's fields -- is unit tested at the bottom.

vi.mock("../../api", async () => {
  const actual = await vi.importActual("../../api");
  return { ...actual, api: vi.fn() };
});

vi.mock("../../auth", async () => {
  const actual = await vi.importActual("../../auth");
  return { ...actual, useAuth: () => ({ isStaff: true, authed: true }) };
});

const saved = (id, over = {}) => ({
  id,
  name: `search ${id}`,
  slug: `search-${id}`,
  raw_query: `query ${id}`,
  product: "Top",
  pagination_depth: 1,
  rolling_hours: 24,
  interval_seconds: 1800,
  enabled: true,
  hit_count: 4,
  schedule: {
    state: "idle",
    enabled: true,
    interval_seconds: 1800,
    last_run_at: new Date(Date.now() - 600000).toISOString(),
    next_due_at: new Date(Date.now() + 1200000).toISOString(),
    seconds_until_due: 1200,
    is_due: false,
    queued_task_id: "",
  },
  last_run: null,
  ...over,
});

const run = (over = {}) => ({
  run_id: "run-1",
  subsystem: "search",
  status: "completed",
  started_at: new Date(Date.now() - 300000).toISOString(),
  finished_at: new Date(Date.now() - 240000).toISOString(),
  summary: { ingested_tweets: 12, new_tweets: 5, pages_by_endpoint: { SearchTimeline: 3 }, stop_reason: "success_search_window_crossed" },
  failure_ledger: {},
  ...over,
});

function routeApi({ searches = [saved(1)], results = [], runs = [run()], extra } = {}) {
  api.mockImplementation(async (path, init) => {
    if (extra) {
      const custom = await extra(path, init);
      if (custom !== undefined) return custom;
    }
    // Order matters: the list path is "/searches/", which is also the prefix of
    // every per-search action, so it has to be matched exactly and first.
    if (path === "/searches/") return { results: searches };
    if (path.includes("/results/")) return { results, next: null };
    if (path.includes("/runs/")) return { results: runs, next: null };
    if (path.includes("/schedule/")) return searches[0]?.schedule || {};
    return {};
  });
}

const renderWorkspace = (route = "/search/1") =>
  render(
    <MemoryRouter initialEntries={[route]}>
      <Routes>
        <Route path="/search" element={<SearchWorkspace />} />
        <Route path="/search/:searchId" element={<SearchWorkspace />} />
      </Routes>
    </MemoryRouter>,
  );

beforeEach(() => api.mockReset());

describe("the query rail", () => {
  it("lists every saved query with what it has collected", async () => {
    routeApi({ searches: [saved(1), saved(2, { hit_count: 91 })] });
    renderWorkspace("/search");
    expect(await screen.findByText("search 1")).toBeInTheDocument();
    expect(screen.getByText("91")).toBeInTheDocument();
  });

  it("says when each query next runs", async () => {
    routeApi();
    renderWorkspace("/search");
    expect(await screen.findByText("next in 20m 0s")).toBeInTheDocument();
  });

  it("says a query is due rather than showing a stale countdown", async () => {
    routeApi({
      searches: [saved(1, { schedule: { ...saved(1).schedule, is_due: true } })],
    });
    renderWorkspace("/search");
    expect(await screen.findByText("due now")).toBeInTheDocument();
  });

  it("names the state a query is actually in", async () => {
    routeApi({
      searches: [saved(1, { schedule: { ...saved(1).schedule, state: "running" } })],
    });
    renderWorkspace("/search");
    expect(await screen.findByText("Fetching")).toBeInTheDocument();
  });

  it("invites the operator to pick one before any is selected", async () => {
    routeApi();
    renderWorkspace("/search");
    expect(await screen.findByText("Pick a query")).toBeInTheDocument();
  });

  it("explains an empty rail rather than showing nothing", async () => {
    routeApi({ searches: [] });
    renderWorkspace("/search");
    expect(await screen.findByText(/No saved queries yet/)).toBeInTheDocument();
  });
});

describe("results", () => {
  it("shows what the selected query stored", async () => {
    routeApi({
      results: [{ id: 7, tweet_id: "7", account: "stranger", text: "a stored hit", type: "Tweet" }],
    });
    renderWorkspace();
    expect(await screen.findByText("a stored hit")).toBeInTheDocument();
  });

  it("reads results from the search's own endpoint, never the feed", async () => {
    routeApi();
    renderWorkspace();
    await waitFor(() =>
      expect(api.mock.calls.map(([p]) => p)).toContain("/searches/1/results/"),
    );
    expect(api.mock.calls.map(([p]) => p)).not.toContain("/feed/");
  });

  it("explains an empty result set", async () => {
    routeApi({ results: [] });
    renderWorkspace();
    expect(await screen.findByText("Nothing stored yet")).toBeInTheDocument();
  });

  it("shows the exact query being sent to X", async () => {
    routeApi();
    renderWorkspace();
    expect(await screen.findByText("query 1")).toBeInTheDocument();
  });
});

describe("the workflow tab", () => {
  it("reports the schedule in the operator's terms", async () => {
    const user = userEvent.setup();
    routeApi();
    renderWorkspace();
    await user.click(await screen.findByRole("tab", { name: "Workflow" }));

    // Scoped to the schedule panel: the rail also names the state of every
    // query, this one included.
    const panel = (await screen.findByText("When this query runs")).closest("section");
    expect(within(panel).getByText("Runs every")).toBeInTheDocument();
    expect(within(panel).getByText("30m 0s")).toBeInTheDocument();
    expect(within(panel).getByText("Waiting")).toBeInTheDocument();
  });

  it("translates the engine's stop reason into plain language", async () => {
    const user = userEvent.setup();
    routeApi();
    renderWorkspace();
    await user.click(await screen.findByRole("tab", { name: "Workflow" }));

    // The raw value is "success_search_window_crossed"; that string was on
    // screen before, which told an operator nothing.
    expect(
      await screen.findByText("Reached the end of the look-back window"),
    ).toBeInTheDocument();
  });

  it("shows what a run cost and what it collected", async () => {
    const user = userEvent.setup();
    routeApi();
    renderWorkspace();
    await user.click(await screen.findByRole("tab", { name: "Workflow" }));

    // "Stored" now means new to this query, with what the run merely re-saw
    // beside it -- a repoll that added nothing used to claim it stored 40.
    expect(await screen.findByText(/5 new/)).toBeInTheDocument();
    expect(screen.getByText(/12 seen/)).toBeInTheDocument();
    expect(screen.getByText("3 pages fetched")).toBeInTheDocument();
  });

  it("surfaces a failure ledger entry rather than a bare status", async () => {
    const user = userEvent.setup();
    routeApi({
      runs: [run({ status: "partial", failure_ledger: { "SearchTimeline:404": { count: 2 } } })],
    });
    renderWorkspace();
    await user.click(await screen.findByRole("tab", { name: "Workflow" }));

    expect(await screen.findByText("SearchTimeline:404 ×2")).toBeInTheDocument();
  });

  it("explains a query that has never run", async () => {
    const user = userEvent.setup();
    routeApi({ runs: [] });
    renderWorkspace();
    await user.click(await screen.findByRole("tab", { name: "Workflow" }));

    expect(await screen.findByText("No runs yet")).toBeInTheDocument();
  });
});

describe("operator controls", () => {
  it("queues a run on demand", async () => {
    const user = userEvent.setup();
    routeApi();
    renderWorkspace();
    await user.click(await screen.findByRole("button", { name: "Run now" }));

    await waitFor(() =>
      expect(api).toHaveBeenCalledWith("/searches/1/refresh/", { method: "POST" }),
    );
    expect(await screen.findByText("Run queued.")).toBeInTheDocument();
  });

  it("pauses the schedule without touching the results", async () => {
    const user = userEvent.setup();
    routeApi();
    renderWorkspace();
    await user.click(await screen.findByRole("button", { name: "Pause" }));

    await waitFor(() =>
      expect(api).toHaveBeenCalledWith("/searches/1/pause/", { method: "POST" }),
    );
    expect(await screen.findByText("Schedule paused.")).toBeInTheDocument();
  });

  it("offers to resume a paused query", async () => {
    routeApi({
      searches: [saved(1, { schedule: { ...saved(1).schedule, state: "paused" } })],
    });
    renderWorkspace();
    expect(await screen.findByRole("button", { name: "Resume" })).toBeInTheDocument();
  });
});

describe("creating a query", () => {
  it("builds the raw query from the guided fields", async () => {
    const user = userEvent.setup();
    routeApi();
    renderWorkspace("/search");
    await user.click(await screen.findByRole("button", { name: "New search" }));

    await user.type(screen.getByLabelText("Words or phrases"), "gold");
    await user.type(screen.getByLabelText("From accounts"), "@reuters");

    expect(screen.getByLabelText("Query sent to X")).toHaveValue("gold from:reuters");
  });

  it("says what each knob does to a run", async () => {
    const user = userEvent.setup();
    routeApi();
    renderWorkspace("/search");
    await user.click(await screen.findByRole("button", { name: "New search" }));

    expect(screen.getByText(/Page 1 comes over HTTP/)).toBeInTheDocument();
    expect(screen.getByText(/How often this query runs on its own/)).toBeInTheDocument();
  });

  it("posts the whole configuration, not just the query", async () => {
    const user = userEvent.setup();
    const created = vi.fn();
    routeApi({
      extra: async (path, init) => {
        if (path === "/searches/" && init?.method === "POST") {
          created(init.body);
          return { id: 9, name: "gold" };
        }
        return undefined;
      },
    });
    renderWorkspace("/search");
    await user.click(await screen.findByRole("button", { name: "New search" }));
    await user.type(screen.getByLabelText("Query sent to X"), "gold");
    await user.click(screen.getByRole("button", { name: "Create and run" }));

    await waitFor(() => expect(created).toHaveBeenCalled());
    expect(created.mock.calls[0][0]).toMatchObject({
      raw_query: "gold",
      product: "Top",
      pagination_depth: 1,
      rolling_hours: 24,
      interval_seconds: 1800,
    });
  });
});

describe("deleting a query", () => {
  const openDelete = async (user) => {
    await user.click(await screen.findByRole("button", { name: "Delete search 1" }));
  };

  it("names everything that will be destroyed", async () => {
    const user = userEvent.setup();
    routeApi();
    renderWorkspace();
    await openDelete(user);

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/4 stored results/)).toBeInTheDocument();
    expect(within(dialog).getByText(/schedule/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Its whole run history/)).toBeInTheDocument();
    expect(within(dialog).getByText(/pagination cursors/)).toBeInTheDocument();
  });

  it("stays disabled until the name is typed exactly", async () => {
    const user = userEvent.setup();
    routeApi();
    renderWorkspace();
    await openDelete(user);

    const confirm = await screen.findByRole("button", { name: "Delete search" });
    expect(confirm).toBeDisabled();

    await user.type(screen.getByLabelText("Type the name to confirm"), "search 1");
    expect(confirm).toBeEnabled();
  });

  it("does not delete on a near-miss", async () => {
    const user = userEvent.setup();
    routeApi();
    renderWorkspace();
    await openDelete(user);
    await user.type(screen.getByLabelText("Type the name to confirm"), "search");

    expect(await screen.findByRole("button", { name: "Delete search" })).toBeDisabled();
  });

  it("deletes and reports what went", async () => {
    const user = userEvent.setup();
    routeApi({
      extra: async (path, init) => {
        if (path === "/searches/1/" && init?.method === "DELETE") {
          return { hits: 4, fetch_runs: 2, raw_pages: 6, search_tweets: 4 };
        }
        return undefined;
      },
    });
    renderWorkspace();
    await openDelete(user);
    await user.type(screen.getByLabelText("Type the name to confirm"), "search 1");
    await user.click(screen.getByRole("button", { name: "Delete search" }));

    await waitFor(() =>
      expect(api).toHaveBeenCalledWith("/searches/1/", { method: "DELETE" }),
    );
    expect(await screen.findByText(/4 result\(s\), 2 run\(s\) and its schedule are gone/)).toBeInTheDocument();
  });
});

describe("buildQuery", () => {
  it("returns an empty string when nothing is filled in", () => {
    expect(buildQuery({})).toBe("");
  });

  it("passes terms through untouched", () => {
    expect(buildQuery({ terms: "gold OR bullion" })).toBe("gold OR bullion");
  });

  it("strips a leading @ from a handle", () => {
    expect(buildQuery({ from: "@reuters" })).toBe("from:reuters");
  });

  it("ORs several handles, because X has no from: list operator", () => {
    expect(buildQuery({ from: "reuters, business" })).toBe(
      "(from:reuters OR from:business)",
    );
  });

  it("omits a zero or blank minimum-likes filter", () => {
    expect(buildQuery({ terms: "gold", minFaves: "0" })).toBe("gold");
    expect(buildQuery({ terms: "gold", minFaves: "" })).toBe("gold");
  });

  it("composes every field in X's operator order", () => {
    expect(
      buildQuery({
        terms: "gold",
        from: "reuters",
        language: "en",
        minFaves: "1000",
        since: "2026-01-01",
        until: "2026-02-01",
      }),
    ).toBe("gold from:reuters lang:en min_faves:1000 since:2026-01-01 until:2026-02-01");
  });
});
