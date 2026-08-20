import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Analyze from "./Analyze";
import { api } from "../api";

// Component tests: the three tabs each render a different row shape from a
// different endpoint, which is exactly where this page has broken before.

vi.mock("../api", async () => {
  const actual = await vi.importActual("../api");
  return { ...actual, api: vi.fn() };
});

vi.mock("recharts", async () => {
  const actual = await vi.importActual("recharts");
  return {
    ...actual,
    ResponsiveContainer: ({ children }) => (
      <div style={{ width: 600, height: 300 }}>{children}</div>
    ),
  };
});

const ROUTER_FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true };
const renderAnalyze = () =>
  render(
    <MemoryRouter future={ROUTER_FUTURE}>
      <Analyze />
    </MemoryRouter>,
  );

const narrative = (over = {}) => ({
  similarity: 0.87,
  first: { tweet_id: "1", account: "alice", created_at: "2026-01-02T03:04:05Z" },
  follower: { tweet_id: "2", account: "bob", created_at: "2026-01-02T04:04:05Z" },
  ...over,
});

function routeApi(byTab = {}) {
  api.mockImplementation(async (path) => {
    const tab = path.replace("/analytics/", "").replace("/", "");
    return { results: byTab[tab] || [] };
  });
}

beforeEach(() => api.mockReset());

describe("tab navigation", () => {
  it("opens on the velocity tab", async () => {
    routeApi();
    renderAnalyze();
    await waitFor(() => expect(api).toHaveBeenCalledWith("/analytics/velocity/"));
    expect(screen.getByRole("button", { name: "velocity" })).toHaveAttribute("aria-pressed", "true");
  });

  it.each(["topics", "narratives"])("requests the %s endpoint when its tab is clicked", async (tab) => {
    const user = userEvent.setup();
    routeApi();
    renderAnalyze();
    await waitFor(() => expect(api).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: tab }));
    await waitFor(() => expect(api).toHaveBeenCalledWith(`/analytics/${tab}/`));
  });

  it("moves the pressed state to the selected tab", async () => {
    const user = userEvent.setup();
    routeApi();
    renderAnalyze();
    await waitFor(() => expect(api).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: "topics" }));
    expect(screen.getByRole("button", { name: "topics" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "velocity" })).toHaveAttribute("aria-pressed", "false");
  });

  it("links across to the saved searches page", async () => {
    routeApi();
    renderAnalyze();
    expect(screen.getByRole("link", { name: /Open saved searches/ })).toHaveAttribute("href", "/searches");
  });

  // Regression: rows from the previous tab survived one frame into the next tab,
  // and narratives reading `item.first.tweet_id` off a tweet unmounted the app.
  it("clears the previous tab's rows before rendering the next", async () => {
    const user = userEvent.setup();
    routeApi({
      velocity: [{ id: "t1", account: "alice", text: "velocity row" }],
      narratives: [narrative()],
    });
    renderAnalyze();
    await screen.findByText("velocity row");
    await user.click(screen.getByRole("button", { name: "narratives" }));
    await waitFor(() => expect(screen.queryByText("velocity row")).toBeNull());
    expect(await screen.findByText(/Narrative 1/)).toBeInTheDocument();
  });
});

describe("velocity tab", () => {
  it("renders the ranked tweets", async () => {
    routeApi({ velocity: [{ id: "t1", account: "alice", text: "accelerating post" }] });
    renderAnalyze();
    expect(await screen.findByText("accelerating post")).toBeInTheDocument();
  });

  it("shows an empty state when no deltas exist", async () => {
    routeApi();
    renderAnalyze();
    expect(await screen.findByText("No metric deltas yet.")).toBeInTheDocument();
  });

  it("shows the server error instead of the empty state", async () => {
    api.mockRejectedValue(new Error("Service unavailable"));
    renderAnalyze();
    expect(await screen.findByText("Service unavailable")).toBeInTheDocument();
    expect(screen.queryByText("No metric deltas yet.")).toBeNull();
  });
});

describe("topics tab", () => {
  it("explains an empty window rather than rendering an empty chart", async () => {
    const user = userEvent.setup();
    routeApi();
    renderAnalyze();
    await waitFor(() => expect(api).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: "topics" }));
    expect(await screen.findByText("No topics in this window yet.")).toBeInTheDocument();
  });

  it("renders the chart once topics exist", async () => {
    const user = userEvent.setup();
    routeApi({ topics: [{ topic: "starship", current_count: 9 }] });
    renderAnalyze();
    await waitFor(() => expect(api).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: "topics" }));
    await waitFor(() => expect(screen.queryByText("No topics in this window yet.")).toBeNull());
  });
});

describe("narratives tab", () => {
  async function openNarratives(user) {
    renderAnalyze();
    await waitFor(() => expect(api).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: "narratives" }));
  }

  it("renders the propagation pair and similarity", async () => {
    const user = userEvent.setup();
    routeApi({ narratives: [narrative()] });
    await openNarratives(user);
    expect(await screen.findByText(/87% similar/)).toBeInTheDocument();
    expect(screen.getByText("@alice")).toBeInTheDocument();
    expect(screen.getByText("@bob")).toBeInTheDocument();
  });

  it("shows an empty state when nothing propagated", async () => {
    const user = userEvent.setup();
    routeApi();
    await openNarratives(user);
    expect(await screen.findByText("No similar claim propagation detected yet.")).toBeInTheDocument();
  });

  // Defensive: a malformed row must be skipped, not crash the whole page.
  it("skips rows missing either side of the pair", async () => {
    const user = userEvent.setup();
    routeApi({ narratives: [{ similarity: 0.5 }, narrative()] });
    await openNarratives(user);
    expect(await screen.findByText(/Narrative 1/)).toBeInTheDocument();
    expect(screen.queryByText(/Narrative 2/)).toBeNull();
  });
});
