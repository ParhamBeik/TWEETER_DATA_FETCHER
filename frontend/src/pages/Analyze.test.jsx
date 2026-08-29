import { render, screen, waitFor, within } from "@testing-library/react";
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

// The topics panel offers a staff-only "hide this term" control, so the page
// reads identity. Stubbed rather than wrapped in a real AuthProvider: that would
// put a token refresh and a /auth/me round trip into every test in this file.
vi.mock("../auth", async () => {
  const actual = await vi.importActual("../auth");
  return { ...actual, useAuth: () => ({ isStaff: true, authed: true }) };
});

const mockNavigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});

const topics = {
  dimension: "both",
  rank: "surging",
  total_docs: 1000,
  previous_total_docs: 1000,
  results: [
    {
      topic: "starship",
      kind: "hashtag",
      docs: 40,
      authors: 12,
      previous_docs: 10,
      delta: 30,
      share: 0.04,
      baseline_share: 0.01,
      score: 6.1,
    },
    {
      topic: "interest rates",
      kind: "phrase",
      docs: 12,
      authors: 5,
      previous_docs: 20,
      delta: -8,
      share: 0.012,
      baseline_share: 0.02,
      score: -1.9,
    },
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

    await user.click(screen.getByRole("tab", { name: "Velocity" }));

    await waitFor(() => expect(analyticsPaths().at(-1)).toContain("/analytics/velocity/"));
  });

  it("carries the topic dimension only on the topics tab", async () => {
    const user = userEvent.setup();
    renderAnalyze();
    await waitFor(() => expect(analyticsPaths().at(-1)).toContain("dimension=phrases"));

    await user.click(screen.getByRole("button", { name: "Hashtags" }));
    await waitFor(() => expect(analyticsPaths().at(-1)).toContain("dimension=hashtags"));

    await user.click(screen.getByRole("tab", { name: "Velocity" }));
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
  it("shows the post and account counts behind each term", async () => {
    renderAnalyze();
    expect(await screen.findByText("#starship")).toBeInTheDocument();
    expect(screen.getByText("interest rates")).toBeInTheDocument();
    // Documents and distinct authors, not raw occurrence counts -- these are
    // the numbers that let a reader judge whether a term earned its place.
    const row = screen.getByRole("button", { name: "#starship" }).closest("tr");
    expect(within(row).getByText("40")).toBeInTheDocument();
    expect(within(row).getByText("12")).toBeInTheDocument();
  });

  it("states each term's rate against its own baseline", async () => {
    renderAnalyze();
    expect(await screen.findByText("4.0× its usual rate")).toBeInTheDocument();
    expect(screen.getByText("1.7× below its usual rate")).toBeInTheDocument();
  });

  it("can rank by volume instead of by what surged", async () => {
    const user = userEvent.setup();
    renderAnalyze();
    await waitFor(() => expect(analyticsPaths().at(-1)).toContain("rank=surging"));

    await user.click(screen.getByRole("button", { name: "Most posts" }));

    await waitFor(() => expect(analyticsPaths().at(-1)).toContain("rank=volume"));
  });

  it("deep-links a topic into the feed", async () => {
    const user = userEvent.setup();
    renderAnalyze();
    await user.click(await screen.findByRole("button", { name: "#starship" }));
    expect(mockNavigate).toHaveBeenCalledWith("/feed?q=starship&window=24h");
  });

  it("shows an empty state rather than a bare axis", async () => {
    mockAnalytics({ topics: { results: [], dimension: "hashtags" } });
    renderAnalyze();
    expect(await screen.findByText("Nothing stands out in this window")).toBeInTheDocument();
  });
});

describe("Analyze velocity", () => {
  it("renders the ranked posts under the gained-engagement chart", async () => {
    const user = userEvent.setup();
    renderAnalyze();
    await waitFor(() => expect(analyticsPaths().length).toBeGreaterThan(0));
    await user.click(screen.getByRole("tab", { name: "Velocity" }));

    expect(await screen.findByText("climbing")).toBeInTheDocument();
    expect(screen.getByText("Engagement gained over time")).toBeInTheDocument();
  });

  it("explains why the chart is empty when there are no snapshots", async () => {
    const user = userEvent.setup();
    mockAnalytics({ velocity: { results: [], series: [] } });
    renderAnalyze();
    await waitFor(() => expect(analyticsPaths().length).toBeGreaterThan(0));
    await user.click(screen.getByRole("tab", { name: "Velocity" }));

    expect(await screen.findByText(/at least two snapshots/)).toBeInTheDocument();
  });
});

describe("Analyze narratives", () => {
  // Regression: switching tabs without clearing rendered the previous tab's rows
  // for one frame, and narratives reading `item.first.tweet_id` off a tweet threw.
  it("does not render the previous tab's rows while switching", async () => {
    const user = userEvent.setup();
    renderAnalyze();
    await screen.findByText("#starship");

    await user.click(screen.getByRole("tab", { name: "Narratives" }));

    expect(await screen.findByText(/82% similar/)).toBeInTheDocument();
    expect(screen.queryByText("#starship")).toBeNull();
  });

  it("links both sides of a narrative to the original posts", async () => {
    const user = userEvent.setup();
    renderAnalyze();
    await waitFor(() => expect(analyticsPaths().length).toBeGreaterThan(0));
    await user.click(screen.getByRole("tab", { name: "Narratives" }));

    const link = await screen.findByRole("link", { name: "@alice" });
    expect(link).toHaveAttribute("href", "https://x.com/alice/status/1");
  });
});
