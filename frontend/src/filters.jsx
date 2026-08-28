// Filter controls shared by the dashboard, the feed and Analyze, so the three
// pages offer one vocabulary for "which window" and "which accounts" instead of
// three. The presentation moved onto the ui/ primitives; the hooks and the
// windowParams contract below are unchanged and still shared.
import { useEffect, useRef, useState } from "react";
import * as Popover from "@radix-ui/react-popover";
import { Check } from "lucide-react";
import { api } from "./api";
import { cn } from "@/lib/cn";
import { Input } from "@/ui/field";

export { Segmented, ToggleChips } from "@/ui/controls";

export const RANGES = [
  { value: "1h", label: "1h" },
  { value: "24h", label: "24h" },
  { value: "7d", label: "7d" },
  { value: "30d", label: "30d" },
  { value: "90d", label: "90d" },
];

export const BUCKETS = [
  { value: "auto", label: "Auto" },
  { value: "hour", label: "Hourly" },
  { value: "day", label: "Daily" },
  { value: "week", label: "Weekly" },
];

/**
 * The tracked-account roster, cached for the life of the page.
 *
 * Every page that filters by account needs the same list; fetching it per page
 * mount made three identical requests on every navigation.
 */
export function useAccounts() {
  const [accounts, setAccounts] = useState([]);
  useEffect(() => {
    let active = true;
    api("/accounts/")
      .then((data) => {
        if (!active) return;
        const rows = Array.isArray(data) ? data : data.results || [];
        setAccounts(rows.filter((row) => row.tracking));
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, []);
  return accounts;
}

/** Avatar with an initial-letter fallback, used in the picker and the feed. */
export function Avatar({ account, size = 40, className = "" }) {
  const [failed, setFailed] = useState(false);
  const handle = account?.handle || account?.account || "";
  const url = account?.avatar_url;
  const style = { width: size, height: size };
  if (!url || failed) {
    return (
      <div
        className={cn(
          "avatar avatar-fallback flex shrink-0 items-center justify-center rounded-full",
          "bg-ink-700 font-mono text-xs text-fg-muted",
          className,
        )}
        style={style}
        aria-hidden="true"
      >
        {(handle[0] || "?").toUpperCase()}
      </div>
    );
  }
  return (
    <img
      className={cn("avatar shrink-0 rounded-full object-cover", className)}
      style={style}
      src={url}
      alt=""
      loading="lazy"
      // X serves avatars from pbs.twimg.com; if that is blocked or the row is
      // stale the element would otherwise collapse to a broken-image glyph.
      onError={() => setFailed(true)}
    />
  );
}

/**
 * Multi-select account picker.
 *
 * Radix Popover handles outside-click, Escape and focus return -- the three
 * things a hand-rolled popover gets subtly wrong and that make one a keyboard
 * trap.
 */
export function AccountPicker({ accounts, selected, onChange }) {
  const [query, setQuery] = useState("");
  const search = useRef(null);

  const visible = accounts.filter((row) =>
    `${row.handle} ${row.display_name || ""}`.toLowerCase().includes(query.toLowerCase()),
  );
  const summary =
    selected.length === 0
      ? "All accounts"
      : selected.length === 1
        ? `@${selected[0]}`
        : `${selected.length} accounts`;

  return (
    <Popover.Root>
      <Popover.Trigger
        className="inline-flex h-8 items-center gap-1.5 rounded-sm border border-line px-2.5 text-xs text-fg-muted hover:border-line-strong hover:text-fg"
        aria-haspopup="listbox"
      >
        {summary}
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          sideOffset={4}
          align="start"
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            search.current?.focus();
          }}
          className="z-50 w-64 rounded-sm border border-line-strong bg-ink-800 p-2"
        >
          <Input
            ref={search}
            aria-label="Filter accounts"
            placeholder="filter accounts"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          {selected.length > 0 && (
            <button
              type="button"
              className="mt-1.5 text-xs text-accent hover:underline"
              onClick={() => onChange([])}
            >
              Clear selection
            </button>
          )}
          <ul className="mt-1.5 max-h-64 overflow-y-auto" role="listbox" aria-label="Accounts">
            {visible.map((row) => {
              const on = selected.includes(row.handle);
              return (
                <li key={row.handle}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={on}
                    className={cn(
                      "flex w-full items-center gap-2 rounded-xs px-1.5 py-1 text-left hover:bg-ink-700",
                      on && "text-accent",
                    )}
                    onClick={() =>
                      onChange(
                        on ? selected.filter((h) => h !== row.handle) : [...selected, row.handle],
                      )
                    }
                  >
                    <Avatar account={row} size={20} />
                    <span className="min-w-0 flex-1 truncate text-xs">
                      {row.display_name || row.handle}
                    </span>
                    <span className="shrink-0 font-mono text-2xs text-fg-dim">@{row.handle}</span>
                    {on && <Check className="size-3 shrink-0" aria-hidden="true" />}
                  </button>
                </li>
              );
            })}
            {!visible.length && (
              <li className="px-1.5 py-2 text-xs text-fg-dim">No matching account.</li>
            )}
          </ul>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

/** Serialize window + account state into the query string the API expects. */
export function windowParams({ range, bucket, accounts }) {
  const params = new URLSearchParams();
  if (range) params.set("range", range);
  if (bucket && bucket !== "auto") params.set("bucket", bucket);
  for (const handle of accounts || []) params.append("account", handle);
  return params;
}

/**
 * Poll `load` on an interval while `live` is on, and run it once on mount and
 * whenever a dependency changes. Returns nothing; the caller owns the state.
 */
export function useLiveRefresh(load, deps, { live = true, interval = 30000 } = {}) {
  const saved = useRef(load);
  saved.current = load;
  useEffect(() => {
    saved.current();
    if (!live) return undefined;
    const timer = setInterval(() => saved.current(), interval);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, live, interval]);
}
