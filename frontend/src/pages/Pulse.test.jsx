import { render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Pulse from "./Pulse";
import { api } from "../api";

// Component tests: Pulse is a read-only dashboard, so these cover the stat tiles,
// the topic list and the degraded states rather than Recharts' internals.

vi.mock("../api", async () => {
  const actual = await vi.importActual("../api");
  return { ...actual, api: vi.fn() };
});

// ResponsiveContainer measures its parent, which is always 0x0 in jsdom and so
// renders nothing. A fixed size lets the chart mount without a layout engine.
vi.mock("recharts", async () => {
  const actual = await vi.importActual("recharts");
  return {
    ...actual,
    ResponsiveContainer: ({ children }) => (
      <div style={{ width: 600, height: 300 }}>{children}</div>
    ),
  };
});

const overview = (over = {}) => ({
  tweets: 1200,
  tweets_in_window: 34,
  tracked_accounts: 8,
  quarantined_accounts: 1,
  latest_runs: [
    { run_id: "r1", subsystem: "live", started_at: "2026-01-02T03:04:05Z", summary: { ingested_tweets: 5 } },
  ],
  ...over,
});

function routeApi({ stats = overview(), topics = [] } = {}) {
  api.mockImplementation(async (path) => {
    if (path === "/stats/overview/") return stats;
    if (path === "/analytics/topics/") return { results: topics };
    return {};
  });
}

beforeEach(() => api.mockReset());

describe("Pulse data loading", () => {
  it("requests the overview and topic endpoints", async () => {
    routeApi();
    render(<Pulse />);
    await waitFor(() => expect(api).toHaveBeenCalledWith("/stats/overview/"));
    expect(api).toHaveBeenCalledWith("/analytics/topics/");
  });

  it("surfaces a failure instead of rendering a silently blank dashboard", async () => {
    api.mockRejectedValue(new Error("Service unavailable"));
    render(<Pulse />);
    expect(await screen.findByText("Service unavailable")).toBeInTheDocument();
  });
});

describe("Pulse stat tiles", () => {
  it("renders the archive and account totals", async () => {
    routeApi();
    render(<Pulse />);
    expect(await screen.findByText("1200")).toBeInTheDocument();
    expect(screen.getByText("8")).toBeInTheDocument();
  });

  it("renders the supporting hints", async () => {
    routeApi();
    render(<Pulse />);
    expect(await screen.findByText("34 in the last 24 hours")).toBeInTheDocument();
    expect(screen.getByText("1 quarantined")).toBeInTheDocument();
  });

  it("falls back to an em dash before the overview resolves", () => {
    api.mockImplementation(() => new Promise(() => {}));
    render(<Pulse />);
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("reports zero counts rather than blanks when the archive is empty", async () => {
    routeApi({
      stats: overview({ tweets: 0, tweets_in_window: 0, tracked_accounts: 0, quarantined_accounts: 0, latest_runs: [] }),
    });
    render(<Pulse />);
    expect(await screen.findByText("0 in the last 24 hours")).toBeInTheDocument();
    expect(screen.getByText("0 quarantined")).toBeInTheDocument();
  });
});

describe("Pulse topic spikes", () => {
  it("lists topics with their count and delta", async () => {
    routeApi({ topics: [{ topic: "starship", current_count: 12, delta: 4 }] });
    render(<Pulse />);
    expect(await screen.findByText("#starship")).toBeInTheDocument();
    expect(screen.getByText("12 · +4")).toBeInTheDocument();
  });

  it("signs a negative delta without an extra plus", async () => {
    routeApi({ topics: [{ topic: "falling", current_count: 3, delta: -2 }] });
    render(<Pulse />);
    expect(await screen.findByText("3 · -2")).toBeInTheDocument();
  });

  it("caps the list at eight topics", async () => {
    routeApi({
      topics: Array.from({ length: 12 }, (_, i) => ({ topic: `t${i}`, current_count: i, delta: 0 })),
    });
    render(<Pulse />);
    await screen.findByText("#t0");
    expect(screen.queryByText("#t8")).toBeNull();
  });

  it("shows an empty state when no topics are available", async () => {
    routeApi({ topics: [] });
    render(<Pulse />);
    expect(await screen.findByText("No topic data yet.")).toBeInTheDocument();
  });

  it("counts the topics in the stat tile", async () => {
    routeApi({ topics: [{ topic: "a", current_count: 1, delta: 0 }, { topic: "b", current_count: 2, delta: 0 }] });
    render(<Pulse />);
    await screen.findByText("#a");
    // "Topic spikes" is both a stat label and the panel heading; the hint text
    // only appears on the tile, so anchor there.
    const tile = screen.getByText("Hashtags seen in the current window").closest("article");
    expect(tile).toHaveTextContent("Topic spikes");
    expect(within(tile).getByText("2")).toBeInTheDocument();
  });
});
