import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Dashboard from "./Dashboard";
import { api } from "../api";

// Component tests: The dashboard composes two endpoints into stat tiles, charts and
// pipeline panels, so the network boundary is the only thing mocked.

vi.mock("../api", async () => {
  const actual = await vi.importActual("../api");
  return { ...actual, api: vi.fn() };
});

// Recharts measures its container, which jsdom reports as 0x0 and then renders
// nothing. Charts are not the subject of these tests; the data plumbing is.
vi.mock("recharts", async () => {
  const actual = await vi.importActual("recharts");
  return {
    ...actual,
    ResponsiveContainer: ({ children }) => (
      <div style={{ width: 800, height: 300 }}>{children}</div>
    ),
  };
});

const HOUR = 3600 * 1000;
const bucketAt = (hoursAgo) => new Date(Date.now() - hoursAgo * HOUR).toISOString();

const ingestion = (over = {}) => ({
  since: bucketAt(24),
  until: bucketAt(0),
  bucket: "hour",
  captured: [
    { bucket: bucketAt(2), source_subsystem: "live", count: 4 },
    { bucket: bucketAt(2), source_subsystem: "historical", count: 30 },
    { bucket: bucketAt(1), source_subsystem: "live", count: 6 },
  ],
  posted: [{ bucket: bucketAt(2), count: 12 }],
  runs: [],
  run_totals: [
    { subsystem: "live", status: "completed", count: 8 },
    { subsystem: "historical", status: "failed", count: 2 },
  ],
  requests: [{ bucket: bucketAt(2), endpoint: "UserTweets", requests: 25, kind: "ok" }],
  totals: {
    captured: 40,
    captured_previous: 22,
    captured_delta: 18,
    by_subsystem: { live: 10, historical: 30 },
    archive_total: 125000,
    oldest_tweet: new Date(Date.now() - 30 * 24 * HOUR).toISOString(),
  },
  ...over,
});

const pipeline = (over = {}) => ({
  now: new Date().toISOString(),
  subsystems: [
    {
      subsystem: "live",
      interval_seconds: 1800,
      running: 0,
      last_run: {
        run_id: "r1",
        status: "completed",
        target: "all",
        started_at: bucketAt(0.2),
        finished_at: bucketAt(0.1),
        ingested_tweets: 42,
      },
      next_due_in_seconds: 600,
    },
    {
      subsystem: "historical",
      interval_seconds: 300,
      running: 1,
      last_run: null,
      next_due_in_seconds: 0,
    },
  ],
  rate_limits: [
    { endpoint: "UserTweets", remaining: 12, limit: 50, reset_epoch: 0, resets_in_seconds: 240 },
  ],
  endpoint_health: { UserTweets: "healthy", SearchTimeline: "stale_query_id" },
  running: [],
  archive: {
    complete: 3,
    tracked: 10,
    stalled: 1,
    walking: [
      { handle: "elonmusk", priority: 1, pages: 120, stalled_ticks: 0, outcome: "paused_for_quota", quarantined: false },
    ],
  },
  quarantined: [{ handle: "ghost", quarantine_reason: "dead handle", quarantined_at: null }],
  searches: [],
  ...over,
});

const renderPulse = () =>
  render(
    <MemoryRouter>
      <Dashboard />
    </MemoryRouter>,
  );

function mockEndpoints({ flow = ingestion(), state = pipeline() } = {}) {
  api.mockImplementation((path) =>
    Promise.resolve(path.startsWith("/analytics/ingestion/") ? flow : state),
  );
}

beforeEach(() => {
  api.mockReset();
  mockEndpoints();
});

describe("Pulse data loading", () => {
  it("requests the ingestion series and the pipeline snapshot", async () => {
    renderPulse();
    await waitFor(() => {
      const paths = api.mock.calls.map(([path]) => path);
      expect(paths.some((p) => p.startsWith("/analytics/ingestion/?range=24h"))).toBe(true);
      expect(paths).toContain("/stats/pipeline/");
    });
  });

  it("refetches with the chosen range", async () => {
    const user = userEvent.setup();
    renderPulse();
    await waitFor(() => expect(api).toHaveBeenCalled());

    await user.click(screen.getByRole("button", { name: "30d" }));

    await waitFor(() =>
      expect(api.mock.calls.map(([p]) => p)).toContainEqual(
        expect.stringContaining("range=30d"),
      ),
    );
  });

  it("surfaces a failure instead of rendering a silently blank dashboard", async () => {
    api.mockRejectedValue(new Error("Service unavailable"));
    renderPulse();
    expect(await screen.findByText("Service unavailable")).toBeInTheDocument();
  });
});

describe("Dashboard stat tiles", () => {
  it("reports what was captured and how it compares to the previous period", async () => {
    renderPulse();
    expect(await screen.findByText("40")).toBeInTheDocument();
    expect(screen.getByText("+18")).toBeInTheDocument();
  });

  it("compacts the archive total and states how deep the history goes", async () => {
    renderPulse();
    expect(await screen.findByText("125K")).toBeInTheDocument();
    expect(screen.getByText(/30 days deep/)).toBeInTheDocument();
  });

  // Quota is no longer a tile here: it moved to the budget rail that sits above
  // every page, since it explains what the other collectors are doing too. See
  // BudgetRail.test.jsx.

  it("shows backfill completion as a fraction of the roster", async () => {
    renderPulse();
    expect(await screen.findByText("3/10")).toBeInTheDocument();
    expect(screen.getByText(/30% of the backfill is finished/)).toBeInTheDocument();
  });

  it("names provider-depth stops on the archive tile instead of calling them finished", async () => {
    mockEndpoints({
      state: pipeline({
        archive: { complete: 19, depth_limited: 45, tracked: 64, stalled: 0, walking: [] },
      }),
    });
    renderPulse();
    expect(await screen.findByText("64/64")).toBeInTheDocument();
    expect(screen.getAllByText(/19 complete · 45 at X limit/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/of the backfill is finished/)).not.toBeInTheDocument();
  });

  it("derives the run success rate from the window's run outcomes", async () => {
    renderPulse();
    // 8 completed of 10 runs.
    expect(await screen.findByText("80%")).toBeInTheDocument();
  });

  it("falls back to an em dash before the endpoints resolve", () => {
    api.mockReturnValue(new Promise(() => {}));
    renderPulse();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });
});

describe("Dashboard collection attribution", () => {
  it("names which pipeline captured how much", async () => {
    renderPulse();
    // Scoped to the legend list: "Archive walk" also names a collector in the
    // pipeline panel further down the page.
    const legend = (await screen.findAllByText("Archive walk"))
      .map((node) => node.closest("li"))
      .find(Boolean);
    expect(within(legend).getByText("30")).toBeInTheDocument();
    const live = within(legend.parentElement).getByText("Live poll").closest("li");
    expect(within(live).getByText("10")).toBeInTheDocument();
  });

  it("explains an empty window rather than drawing a blank chart", async () => {
    mockEndpoints({ flow: ingestion({ captured: [], totals: {} }) });
    renderPulse();
    expect(await screen.findByText("Nothing captured in this window yet.")).toBeInTheDocument();
  });

  it("says so when there is no request telemetry yet", async () => {
    mockEndpoints({ flow: ingestion({ requests: [] }) });
    renderPulse();
    expect(
      await screen.findByText("No request telemetry in this window yet."),
    ).toBeInTheDocument();
  });
});

describe("Dashboard pipeline panel", () => {
  it("shows the last run and the countdown to the next one", async () => {
    renderPulse();
    // "+N new", not rows re-seen: an archive walk that stored nothing was
    // reporting "+51 posts" while the chart beside it showed zero for it.
    expect(await screen.findByText("+42 new")).toBeInTheDocument();
    expect(screen.getByText("next in 10m 0s")).toBeInTheDocument();
  });

  it("marks a subsystem that is running right now", async () => {
    renderPulse();
    expect(await screen.findByText("fetching now")).toBeInTheDocument();
  });

  it("reports endpoint health with words, never colour alone", async () => {
    renderPulse();
    expect(await screen.findByText("UserTweets: healthy")).toBeInTheDocument();
    expect(screen.getByText("SearchTimeline: stale query id")).toBeInTheDocument();
  });
});

describe("Pulse backfill panel", () => {
  it("lists the accounts still walking with their progress", async () => {
    renderPulse();
    expect(await screen.findByText("@elonmusk")).toBeInTheDocument();
    expect(screen.getByText("120 pages")).toBeInTheDocument();
    expect(screen.getByText("paused for quota")).toBeInTheDocument();
  });

  it("counts stalled accounts alongside the completed ones", async () => {
    renderPulse();
    expect(await screen.findByText(/of 10 accounts fully archived/)).toBeInTheDocument();
    expect(screen.getByText(/1 stalled/)).toBeInTheDocument();
  });

  it("names quarantined accounts and why", async () => {
    renderPulse();
    expect(await screen.findByText("@ghost")).toBeInTheDocument();
    expect(screen.getByText("dead handle")).toBeInTheDocument();
  });

  it("says the walk is done when nothing is left", async () => {
    mockEndpoints({
      state: pipeline({
        archive: { complete: 10, tracked: 10, stalled: 0, walking: [] },
        quarantined: [],
      }),
    });
    renderPulse();
    expect(
      await screen.findByText("Every tracked account is fully archived."),
    ).toBeInTheDocument();
  });

  it("does not call a provider-depth stop a finished archive", async () => {
    // Component test: Pulse is a view over one API snapshot, so a fixture with
    // walking empty and depth_limited set is the exact lie this panel used to tell.
    mockEndpoints({
      state: pipeline({
        archive: {
          complete: 19,
          depth_limited: 45,
          tracked: 64,
          stalled: 0,
          walking: [],
        },
        quarantined: [],
      }),
    });
    renderPulse();
    expect((await screen.findAllByText(/45 stopped at X's serving depth/)).length).toBeGreaterThan(0);
    expect(
      screen.queryByText("Every tracked account is fully archived."),
    ).not.toBeInTheDocument();
  });
});
