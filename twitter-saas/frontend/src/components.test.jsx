import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import InfiniteSentinel from "./InfiniteSentinel";
import RunStatus from "./RunStatus";
import { api } from "./api";

// Unit tests for the two shared widgets. IntersectionObserver is stubbed per-test
// so the sentinel's callback can be fired deterministically.

vi.mock("./api", async () => {
  const actual = await vi.importActual("./api");
  return { ...actual, api: vi.fn() };
});

/** Installs an IntersectionObserver stub and returns a trigger for its callback. */
function captureObserver() {
  const state = { callback: null, observed: 0, disconnected: 0, options: null };
  globalThis.IntersectionObserver = class {
    constructor(cb, options) {
      state.callback = cb;
      state.options = options;
    }
    observe() { state.observed += 1; }
    unobserve() {}
    disconnect() { state.disconnected += 1; }
    takeRecords() { return []; }
  };
  return state;
}

describe("InfiniteSentinel", () => {
  it("observes the sentinel when another page is available", () => {
    const observer = captureObserver();
    render(<InfiniteSentinel next="/api/feed/?cursor=abc" loading={false} onLoad={vi.fn()} />);
    expect(observer.observed).toBe(1);
  });

  it("does not observe when there is no next page", () => {
    const observer = captureObserver();
    render(<InfiniteSentinel next={null} loading={false} onLoad={vi.fn()} />);
    expect(observer.observed).toBe(0);
  });

  it("does not observe while a page is already loading", () => {
    const observer = captureObserver();
    render(<InfiniteSentinel next="/api/feed/?cursor=abc" loading onLoad={vi.fn()} />);
    expect(observer.observed).toBe(0);
  });

  it("requests the next page when the sentinel scrolls into view", () => {
    const observer = captureObserver();
    const onLoad = vi.fn();
    render(<InfiniteSentinel next="/api/feed/?cursor=abc" loading={false} onLoad={onLoad} />);
    observer.callback([{ isIntersecting: true }]);
    expect(onLoad).toHaveBeenCalledWith("/api/feed/?cursor=abc");
  });

  it("ignores a non-intersecting entry", () => {
    const observer = captureObserver();
    const onLoad = vi.fn();
    render(<InfiniteSentinel next="/api/feed/?cursor=abc" loading={false} onLoad={onLoad} />);
    observer.callback([{ isIntersecting: false }]);
    expect(onLoad).not.toHaveBeenCalled();
  });

  it("prefetches before the sentinel is visible", () => {
    const observer = captureObserver();
    render(<InfiniteSentinel next="/api/feed/?cursor=abc" loading={false} onLoad={vi.fn()} />);
    expect(observer.options.rootMargin).toBe("300px");
  });

  it("disconnects the observer on unmount", () => {
    const observer = captureObserver();
    const { unmount } = render(
      <InfiniteSentinel next="/api/feed/?cursor=abc" loading={false} onLoad={vi.fn()} />,
    );
    unmount();
    expect(observer.disconnected).toBe(1);
  });

  it("announces loading through a live region", () => {
    captureObserver();
    render(<InfiniteSentinel next="/api/feed/?cursor=abc" loading onLoad={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading…");
  });

  it("stays silent when idle", () => {
    captureObserver();
    render(<InfiniteSentinel next={null} loading={false} onLoad={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent("");
  });
});

describe("RunStatus", () => {
  beforeEach(() => api.mockReset());

  it("renders nothing until runs arrive", async () => {
    api.mockResolvedValue({ results: [] });
    const { container } = render(<RunStatus />);
    await waitFor(() => expect(api).toHaveBeenCalledWith("/runs/"));
    expect(container).toBeEmptyDOMElement();
  });

  it("lists the most recent runs", async () => {
    api.mockResolvedValue({
      results: [
        { run_id: "r1", subsystem: "live", status: "completed", target: null, started_at: "2026-01-02T03:04:05Z" },
      ],
    });
    render(<RunStatus />);
    expect(await screen.findByLabelText("Recent fetch runs")).toBeInTheDocument();
    expect(screen.getByText("live")).toBeInTheDocument();
    expect(screen.getByText("completed")).toBeInTheDocument();
  });

  it("labels an untargeted run as 'all'", async () => {
    api.mockResolvedValue({
      results: [{ run_id: "r1", subsystem: "live", status: "completed", target: null, started_at: "2026-01-02T03:04:05Z" }],
    });
    render(<RunStatus />);
    expect(await screen.findByText("all")).toBeInTheDocument();
  });

  it("humanises the auth_required status", async () => {
    api.mockResolvedValue({
      results: [{ run_id: "r1", subsystem: "live", status: "auth_required", target: null, started_at: "2026-01-02T03:04:05Z" }],
    });
    render(<RunStatus />);
    expect(await screen.findByText("auth required")).toBeInTheDocument();
  });

  it("shows at most five runs", async () => {
    api.mockResolvedValue({
      results: Array.from({ length: 9 }, (_, i) => ({
        run_id: `r${i}`, subsystem: `sub${i}`, status: "completed", target: null, started_at: "2026-01-02T03:04:05Z",
      })),
    });
    render(<RunStatus />);
    await screen.findByText("sub0");
    expect(screen.queryByText("sub5")).toBeNull();
  });

  it("stays out of the way when the runs endpoint fails", async () => {
    api.mockRejectedValue(new Error("Service unavailable"));
    const { container } = render(<RunStatus />);
    await waitFor(() => expect(api).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
