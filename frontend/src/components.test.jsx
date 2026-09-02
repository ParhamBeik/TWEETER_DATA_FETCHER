import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import InfiniteSentinel from "./InfiniteSentinel";
import { api } from "./api";
import { pivotSeries } from "./charts";
import { absoluteTime, compact } from "./format";

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


// Unit tests: the two shared pure helpers with real branching. Everything else
// in format.js/charts.js is a one-liner covered by the component tests above.

describe("compact", () => {
  it("leaves counts under a thousand alone", () => {
    expect(compact(0)).toBe("0");
    expect(compact(999)).toBe("999");
  });

  it("keeps one decimal below ten of a unit and drops it above", () => {
    expect(compact(1234)).toBe("1.2K");
    expect(compact(12345)).toBe("12K");
    expect(compact(3400000)).toBe("3.4M");
    expect(compact(2100000000)).toBe("2.1B");
  });

  it("handles negatives and non-numbers without producing NaN", () => {
    expect(compact(-1500)).toBe("-1.5K");
    expect(compact(null)).toBe("0");
    expect(compact(undefined)).toBe("0");
  });
});

describe("absoluteTime", () => {
  it("names the month so the date cannot be read the other way round", () => {
    // "9/2/2026" is 9 February to most readers and 2 September to the rest.
    expect(absoluteTime("2026-09-02T13:24:28Z")).toMatch(/2 Sept? 2026/);
    expect(absoluteTime("2026-09-02T13:24:28Z")).not.toMatch(/9\/2/);
  });

  it("uses a 24-hour clock", () => {
    expect(absoluteTime("2026-09-02T13:24:28Z")).toMatch(/\b\d{2}:\d{2}\b/);
    expect(absoluteTime("2026-09-02T13:24:28Z")).not.toMatch(/[AP]M/i);
  });

  it("returns an empty string rather than 'Invalid Date'", () => {
    expect(absoluteTime(null)).toBe("");
    expect(absoluteTime("not a date")).toBe("");
  });
});

describe("pivotSeries", () => {
  const rows = [
    { bucket: "2026-01-02T01:00:00Z", source_subsystem: "live", count: 2 },
    { bucket: "2026-01-02T01:00:00Z", source_subsystem: "historical", count: 5 },
    { bucket: "2026-01-02T02:00:00Z", source_subsystem: "live", count: 3 },
  ];

  it("collapses long-format rows into one row per bucket", () => {
    const { rows: wide, keys } = pivotSeries(rows, "source_subsystem");
    expect(keys.sort()).toEqual(["historical", "live"]);
    expect(wide).toHaveLength(2);
    expect(wide[0]).toMatchObject({ live: 2, historical: 5 });
  });

  // A missing key would otherwise render as a gap and shift the stack, making
  // the second bucket look like it belongs to a different series.
  it("fills an absent key with zero rather than leaving a hole", () => {
    const { rows: wide } = pivotSeries(rows, "source_subsystem");
    expect(wide[1]).toMatchObject({ live: 3, historical: 0 });
  });

  it("orders buckets chronologically whatever order the API returned", () => {
    const { rows: wide } = pivotSeries([...rows].reverse(), "source_subsystem");
    expect(wide.map((r) => r.bucket)).toEqual([
      "2026-01-02T01:00:00Z",
      "2026-01-02T02:00:00Z",
    ]);
  });

  it("buckets an unlabelled row under 'unknown' instead of dropping it", () => {
    const { keys } = pivotSeries([{ bucket: "2026-01-02T01:00:00Z", count: 1 }], "source_subsystem");
    expect(keys).toEqual(["unknown"]);
  });

  it("reads a named value field for series that are not plain counts", () => {
    const { rows: wide } = pivotSeries(
      [{ bucket: "2026-01-02T01:00:00Z", endpoint: "UserTweets", requests: 25 }],
      "endpoint",
      "requests",
    );
    expect(wide[0]).toMatchObject({ UserTweets: 25 });
  });

  it("returns nothing for an empty or missing series", () => {
    expect(pivotSeries([], "endpoint")).toEqual({ rows: [], keys: [] });
    expect(pivotSeries(undefined, "endpoint")).toEqual({ rows: [], keys: [] });
  });
});
