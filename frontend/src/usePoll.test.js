import { renderHook } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { usePoll } from "./usePoll";

// Five screens polled on their own timers and none of them stopped when the tab
// went to the background, so a console left open in another window kept asking
// the API for state nobody was reading. What matters here is that the pausing
// does not trade wasted requests for stale numbers.

function setVisibility(state) {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => state,
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("usePoll", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "visible",
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("calls once on mount and then on the interval", () => {
    const poll = vi.fn();
    renderHook(() => usePoll(poll, 1000));
    expect(poll).toHaveBeenCalledTimes(1);

    act(() => vi.advanceTimersByTime(2000));
    expect(poll).toHaveBeenCalledTimes(3);
  });

  it("stops while the tab is hidden", () => {
    const poll = vi.fn();
    renderHook(() => usePoll(poll, 1000));
    poll.mockClear();

    act(() => setVisibility("hidden"));
    act(() => vi.advanceTimersByTime(10000));
    expect(poll).not.toHaveBeenCalled();
  });

  it("refreshes immediately on returning, not after the remaining interval", () => {
    const poll = vi.fn();
    renderHook(() => usePoll(poll, 1000));
    act(() => setVisibility("hidden"));
    poll.mockClear();

    act(() => setVisibility("visible"));
    // Otherwise pausing would trade wasted requests for a stale first paint.
    expect(poll).toHaveBeenCalledTimes(1);

    act(() => vi.advanceTimersByTime(1000));
    expect(poll).toHaveBeenCalledTimes(2);
  });

  it("does not stack timers when visibility flips repeatedly", () => {
    const poll = vi.fn();
    renderHook(() => usePoll(poll, 1000));
    for (let i = 0; i < 3; i += 1) {
      act(() => setVisibility("hidden"));
      act(() => setVisibility("visible"));
    }
    poll.mockClear();

    act(() => vi.advanceTimersByTime(1000));
    expect(poll).toHaveBeenCalledTimes(1);
  });

  it("skips the mount call when leading is false", () => {
    const poll = vi.fn();
    renderHook(() => usePoll(poll, 1000, { leading: false }));
    // Ops already loads on mount and on every filter change; the hook's own
    // first call would double both.
    expect(poll).not.toHaveBeenCalled();

    act(() => vi.advanceTimersByTime(1000));
    expect(poll).toHaveBeenCalledTimes(1);
  });

  it("does nothing when disabled", () => {
    const poll = vi.fn();
    renderHook(() => usePoll(poll, 1000, { enabled: false }));
    act(() => vi.advanceTimersByTime(5000));
    expect(poll).not.toHaveBeenCalled();
  });

  it("stops polling after unmount", () => {
    const poll = vi.fn();
    const { unmount } = renderHook(() => usePoll(poll, 1000));
    unmount();
    poll.mockClear();

    act(() => vi.advanceTimersByTime(5000));
    expect(poll).not.toHaveBeenCalled();
  });

  it("uses the latest callback without restarting the timer", () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = renderHook(({ fn }) => usePoll(fn, 1000), {
      initialProps: { fn: first },
    });
    rerender({ fn: second });
    first.mockClear();

    act(() => vi.advanceTimersByTime(1000));
    // A callback held by value would have restarted the interval on every
    // render, which is what makes an inline arrow safe to pass here.
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });
});
