import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import BudgetRail from "./BudgetRail";
import { api } from "./api";

// Component tests: the rail is one endpoint rendered as a gauge, so the network
// boundary is the only thing mocked. What is worth pinning is that the numbers
// are readable to assistive technology and that an unreported budget says so
// rather than rendering as a full tank.

vi.mock("./api", async () => {
  const actual = await vi.importActual("./api");
  return { ...actual, api: vi.fn() };
});

const pipeline = (over = {}) => ({
  rate_limits: [
    {
      endpoint: "UserTweets",
      remaining: 12,
      limit: 50,
      reset_epoch: 0,
      resets_in_seconds: 240,
    },
  ],
  running: [],
  ...over,
});

beforeEach(() => {
  api.mockReset();
  api.mockResolvedValue(pipeline());
});

describe("budget rail", () => {
  it("reports the remaining allowance and when it comes back", async () => {
    render(<BudgetRail />);
    expect(await screen.findByText("12/50")).toBeInTheDocument();
    expect(screen.getByText(/resets 4m 0s/)).toBeInTheDocument();
  });

  it("exposes the gauge as a meter for assistive technology", async () => {
    render(<BudgetRail />);
    const meter = await screen.findByRole("meter", { name: /UserTweets requests remaining/ });
    expect(meter).toHaveAttribute("aria-valuenow", "12");
    expect(meter).toHaveAttribute("aria-valuemax", "50");
  });

  it("names the collector that is spending right now", async () => {
    api.mockResolvedValue(
      pipeline({ running: [{ run_id: "r1", subsystem: "search", target: "gold:Latest" }] }),
    );
    render(<BudgetRail />);
    expect(await screen.findByText("search fetching gold:Latest")).toBeInTheDocument();
  });

  it("counts concurrent fetches rather than naming one of them", async () => {
    api.mockResolvedValue(
      pipeline({
        running: [
          { run_id: "r1", subsystem: "live", target: "all" },
          { run_id: "r2", subsystem: "search", target: "gold:Latest" },
        ],
      }),
    );
    render(<BudgetRail />);
    expect(await screen.findByText("2 fetches running")).toBeInTheDocument();
  });

  it("says the collector is idle when nothing is running", async () => {
    render(<BudgetRail />);
    expect(await screen.findByText("Collector idle")).toBeInTheDocument();
  });

  it("says no quota is reported rather than drawing a full tank", async () => {
    api.mockResolvedValue(pipeline({ rate_limits: [] }));
    render(<BudgetRail />);
    expect(await screen.findByText(/No quota reported yet/)).toBeInTheDocument();
  });

  it("ignores an endpoint with no limit recorded", async () => {
    api.mockResolvedValue(
      pipeline({ rate_limits: [{ endpoint: "SearchTimeline", remaining: 0, limit: 0 }] }),
    );
    render(<BudgetRail />);
    expect(await screen.findByText(/No quota reported yet/)).toBeInTheDocument();
  });

  it("says so when collector state cannot be read at all", async () => {
    api.mockRejectedValue(new Error("down"));
    render(<BudgetRail />);
    await waitFor(() =>
      expect(screen.getByText("Collector state unreachable.")).toBeInTheDocument(),
    );
  });
});
