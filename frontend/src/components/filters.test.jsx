import { renderHook } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useLiveRefresh } from "./filters";

// useLiveRefresh drives Dashboard and Analyze. It used to own a bare
// setInterval with no visibility handling, so those two screens kept requesting
// analytics for a tab nobody was looking at while every other screen stopped.
// It now delegates its timer to usePoll; these tests pin both halves of that --
// the pausing, and the dependency-triggered reload that had to survive it.

function setVisibility(state) {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => state,
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("useLiveRefresh", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "visible",
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    // Put the tab back. `visibilityState` is redefined on the shared document,
    // and vitest reuses a worker across test files -- leaving it "hidden" makes
    // every usePoll-driven screen in whatever file runs next stop polling, which
    // surfaces as a different unrelated test failing on each run.
    setVisibility("visible");
  });

  it("loads once on mount, not twice", () => {
    // The mount load and the poll's leading call are the same request; if the
    // hook ever stops passing `leading: false`, every filter change doubles.
    const load = vi.fn();
    renderHook(() => useLiveRefresh(load, [], { interval: 1000 }));
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("keeps polling while the tab is visible", () => {
    const load = vi.fn();
    renderHook(() => useLiveRefresh(load, [], { interval: 1000 }));
    load.mockClear();

    act(() => vi.advanceTimersByTime(3000));
    expect(load).toHaveBeenCalledTimes(3);
  });

  it("stops polling once the tab is hidden", () => {
    const load = vi.fn();
    renderHook(() => useLiveRefresh(load, [], { interval: 1000 }));
    load.mockClear();

    act(() => setVisibility("hidden"));
    act(() => vi.advanceTimersByTime(5000));
    expect(load).not.toHaveBeenCalled();
  });

  it("refreshes immediately on returning to the tab", () => {
    const load = vi.fn();
    renderHook(() => useLiveRefresh(load, [], { interval: 1000 }));
    act(() => setVisibility("hidden"));
    load.mockClear();

    act(() => setVisibility("visible"));
    // Otherwise pausing would trade wasted requests for numbers that are stale
    // at exactly the moment someone looks at them.
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("reloads when a dependency changes", () => {
    // A filter change has to repaint now, not on the next tick. usePoll only
    // restarts on enabled/interval, so this stays the hook's own effect.
    const load = vi.fn();
    const { rerender } = renderHook(({ range }) => useLiveRefresh(load, [range], { interval: 1000 }), {
      initialProps: { range: "24h" },
    });
    load.mockClear();

    rerender({ range: "7d" });
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("does not poll when live is off", () => {
    const load = vi.fn();
    renderHook(() => useLiveRefresh(load, [], { live: false, interval: 1000 }));
    load.mockClear();

    act(() => vi.advanceTimersByTime(5000));
    expect(load).not.toHaveBeenCalled();
  });
});
