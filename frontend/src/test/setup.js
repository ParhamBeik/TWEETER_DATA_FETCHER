import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

// findBy*/waitFor give up after 1s by default, which is a bet on how fast the
// machine is rather than on whether the code is right. Sixteen isolated jsdom
// workers on a runner that is also hosting a Postgres service container will
// lose that bet: the same three or four tests fail, a different three or four
// each run, and none of them found a bug. Five seconds still fails fast on a
// genuinely broken assertion -- the only thing it buys is the right to be slow.
configure({ asyncUtilTimeout: 5000 });

// jsdom ships neither of these, and the app uses both: InfiniteSentinel relies on
// IntersectionObserver and Recharts measures its container with ResizeObserver.
class NoopObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
}

globalThis.IntersectionObserver = NoopObserver;
globalThis.ResizeObserver = NoopObserver;

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
