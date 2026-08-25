import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Analyze from "./Analyze";
import { api } from "../api";

// Component tests: Analyze turns one filter bar into three differently-shaped
// endpoints, so the filter-to-request mapping and each tab's render are the
// behaviour worth pinning.

vi.mock("../api", async () => {
  const actual = await vi.importActual("../api");
  return { ...actual, api: vi.fn() };
});

vi.mock("recharts", async () => {
  const actual = await vi.importActual("recharts");
  return {
    ...actual,
    ResponsiveContainer: ({ children }) => (
      <div style={{ width: 800, height: 300 }}>{children}</div>
    ),
  };
});

const mockNavigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});

const topics = {
  dimension: "hashtags",
  results: [
    { topic: "starship", kind: "hashtag", current_count: 40, previous_count: 10, delta: 30 },
    { topic: "interest rates", kind: "phrase", current_count: 12, previous_count: 20, delta: -8 },
  ],
};

const velocity = {
  bucket: "hour",
  results: [{ id: 1, tweet_id: "1", account: "elonmusk", text: "climbing", velocity: 890, type: "Tweet" }],
  series: [
    { bucket: new Date(Date.now() - 3600000).toISOString(), gained: 890, tweets: 1 },
  ],
};

const narratives = {
  results: [
    {
      first: { account: "alice", tweet_id: "1", created_at: new Date().toISOString() },
      follower: { account: "bob", tweet_id: "2", created_at: new Date().toISOString() },
      similarity: 0.82,
    },
  ],
};

const renderAnalyze = () =>
  render(
    <MemoryRouter>
      <Analyze />
    </MemoryRouter>,
  );

/** Route each analytics path to its own fixture; /accounts/ is the picker's. */
function mockAnalytics(over = {}) {
  const bodies = { topics, velocity, narratives, ...over };
  api.mockImplementation((path) => {
    if (path.startsWith("/accounts/")) return Promise.resolve({ results: [] });
    const name = path.split("/")[2];
    return Promise.resolve(bodies[name] ?? { results: [] });
  });
}

const analyticsPaths = () =>
  api.mock.calls.map(([p]) => p).filter((p) => p.startsWith("/analytics/"));

beforeEach(() => {
  api.mockReset();
  mockNavigate.mockReset();
  mockAnalytics();
});

describe("Analyze filters", () => {
  it("opens on topics for the last 24 hours", async () => {
    renderAnalyze();
    await waitFor(() =>
      expect(analyticsPaths().at(-1)).toContain("/analytics/topics/?range=24h"),
    );
  });

  it("passes the range and bucket to the active tab", async () => {
    const user = userEvent.setup();
    renderAnalyze();
    await waitFor(() => expect(analyticsPaths().length).toBeGreaterThan(0));

    await user.click(screen.getByRole("button", { name: "7d" }));
    await user.click(screen.getByRole("button", { name: "Daily" }));

    await waitFor(() => {
      const path = analyticsPaths().at(-1);
      expect(path).toContain("range=7d");
      expect(path).toContain("bucket=day");
    });
  });

  it("omits the bucket when it is left on auto", async () => {
    renderAnalyze();
    await waitFor(() => expect(analyticsPaths().at(-1)).not.toContain("bucket="));
  });

  it("switches endpoint when a different tab is chosen", async () => {
    const user = userEvent.setup();
    renderAnalyze();
    await waitFor(() => expect(analyticsPaths().length).toBeGreaterThan(0));

    await user.click(screen.getByRole("button", { name: "Velocity" }));

    await waitFor(() => expect(analyticsPaths().at(-1)).toContain("/analytics/velocity/"));
  });

  it("carries the topic dimension only on the topics tab", async () => {
    const user = userEvent.setup();
    renderAnalyze();
    await waitFor(() => expect(analyticsPaths().at(-1)).toContain("dimension=hashtags"));

    await user.click(screen.getByRole("button", { name: "Phrases" }));
    await waitFor(() => expect(analyticsPaths().at(-1)).toContain("dimension=phrases"));

    await user.click(screen.getByRole("button", { name: "Velocity" }));
    await waitFor(() => {
      const path = analyticsPaths().at(-1);
      expect(path).toContain("/analytics/velocity/");
      expect(path).not.toContain("dimension=");
    });
  });

  it("can be paused so it stops polling", async () => {
    const user = userEvent.setup();
    renderAnalyze();
    await waitFor(() => expect(analyticsPaths().length).toBeGreaterThan(0));

    const toggle = screen.getByRole("button", { name: /Live/ });
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    await user.click(toggle);

    expect(screen.getByRole("button", { name: /Paused/ })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("surfaces an API failure", async () => {
    api.mockRejectedValue(new Error("Service unavailable"));
    renderAnalyze();
    expect(await screen.findByText("Service unavailable")).toBeInTheDocument();
  });
});

describe("Analyze topics", () => {
  it("renders hashtags and mined phrases with their period delta", async () => {
    renderAnalyze();
    expect(await screen.findByText("#starship")).toBeInTheDocument();
    expect(screen.getByText("interest rates")).toBeInTheDocument();
    expect(screen.getByText("+30")).toBeInTheDocument();
    expect(screen.getByText("-8")).toBeInTheDocument();
  });

  it("deep-links a topic into the feed", async () => {
    const user = userEvent.setup();
    renderAnalyze();
    await user.click(await screen.findByRole("button", { name: /#starship/ }));
    expect(mockNavigate).toHaveBeenCalledWith("/feed?q=starship&window=");
  });

  it("shows an empty state rather than a bare axis", async () => {
    mockAnalytics({ topics: { results: [], dimension: "hashtags" } });
    renderAnalyze();
    expect(await screen.findByText("No topics in this window yet.")).toBeInTheDocument();
  });
});

describe("Analyze velocity", () => {
  it("renders the ranked posts under the gained-engagement chart", async () => {
    const user = userEvent.setup();
    renderAnalyze();
    await waitFor(() => expect(analyticsPaths().length).toBeGreaterThan(0));
    await user.click(screen.getByRole("button", { name: "Velocity" }));

    expect(await screen.findByText("climbing")).toBeInTheDocument();
    expect(screen.getByText("Engagement gained over time")).toBeInTheDocument();
  });

  it("explains why the chart is empty when there are no snapshots", async () => {
    const user = userEvent.setup();
    mockAnalytics({ velocity: { results: [], series: [] } });
    renderAnalyze();
    await waitFor(() => expect(analyticsPaths().length).toBeGreaterThan(0));
    await user.click(screen.getByRole("button", { name: "Velocity" }));

    expect(await screen.findByText(/needs at least two snapshots/)).toBeInTheDocument();
  });
});

describe("Analyze narratives", () => {
  // Regression: switching tabs without clearing rendered the previous tab's rows
  // for one frame, and narratives reading `item.first.tweet_id` off a tweet threw.
  it("does not render the previous tab's rows while switching", async () => {
    const user = userEvent.setup();
    renderAnalyze();
    await screen.findByText("#starship");

    await user.click(screen.getByRole("button", { name: "Narratives" }));

    expect(await screen.findByText(/82% similar/)).toBeInTheDocument();
    expect(screen.queryByText("#starship")).toBeNull();
  });

  it("links both sides of a narrative to the original posts", async () => {
    const user = userEvent.setup();
    renderAnalyze();
    await waitFor(() => expect(analyticsPaths().length).toBeGreaterThan(0));
    await user.click(screen.getByRole("button", { name: "Narratives" }));

    const link = await screen.findByRole("link", { name: "@alice" });
    expect(link).toHaveAttribute("href", "https://x.com/alice/status/1");
  });
});
