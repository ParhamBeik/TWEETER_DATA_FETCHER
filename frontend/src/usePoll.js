import { useEffect, useRef } from "react";

/**
 * Run `callback` now, then every `intervalMs` — but only while the tab is visible.
 *
 * Five screens polled on their own timers and none of them stopped when the tab
 * went to the background. A console left open in another window kept asking the
 * API for collector state indefinitely, which is load nobody is reading: the
 * answer is repainted on a page no one is looking at.
 *
 * Returning to the tab refreshes once immediately rather than waiting out the
 * remaining interval, so what you see on switching back is current — otherwise
 * pausing would trade wasted requests for stale numbers.
 *
 * The callback is held in a ref so a caller can pass an inline arrow without
 * restarting the timer on every render; only `intervalMs` and `enabled` do that.
 *
 * `leading: false` is for callers that already load on mount for their own
 * reasons — Ops reloads whenever its subsystem filter changes, and without this
 * the hook's own first call would double every one of those requests.
 */
export function usePoll(callback, intervalMs, { enabled = true, leading = true } = {}) {
  const saved = useRef(callback);
  saved.current = callback;

  useEffect(() => {
    if (!enabled || !intervalMs) return undefined;

    let timer = null;
    const hidden = () =>
      typeof document !== "undefined" && document.visibilityState === "hidden";

    const stop = () => {
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
    };

    const start = () => {
      if (timer !== null) return;
      timer = setInterval(() => saved.current(), intervalMs);
    };

    // The mount call still fires in a background tab: a component that mounts
    // hidden needs its first paint to have data even though it will not tick.
    if (leading) saved.current();
    if (!hidden()) start();

    const onVisibility = () => {
      if (hidden()) {
        stop();
        return;
      }
      saved.current();
      start();
    };

    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [intervalMs, enabled, leading]);
}

export default usePoll;
