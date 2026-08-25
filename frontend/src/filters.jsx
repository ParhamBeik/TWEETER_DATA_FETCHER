// Filter controls shared by Pulse, Feed and Analyze, so the three pages offer
// one vocabulary for "which window" and "which accounts" instead of three.
import { useEffect, useRef, useState } from "react";
import { api } from "./api";

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
 * A row of mutually exclusive options. `aria-pressed` rather than a radiogroup:
 * these read as toggle buttons and the pressed state is what a screen reader
 * needs, since the label alone does not say which is active.
 */
export function Segmented({ label, options, value, onChange, name }) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          name={name}
          aria-pressed={value === option.value}
          className={value === option.value ? "segment active" : "segment"}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

/** Independent on/off chips -- post types, media-only, and similar. */
export function ToggleChips({ label, options, values, onChange }) {
  return (
    <div className="chip-row" role="group" aria-label={label}>
      {options.map((option) => {
        const on = values.includes(option.value);
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={on}
            className={on ? "chip active" : "chip"}
            onClick={() =>
              onChange(
                on ? values.filter((v) => v !== option.value) : [...values, option.value],
              )
            }
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

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
      <div className={`avatar avatar-fallback ${className}`} style={style} aria-hidden="true">
        {(handle[0] || "?").toUpperCase()}
      </div>
    );
  }
  return (
    <img
      className={`avatar ${className}`}
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
 * Multi-select account picker. Closes on outside click and on Escape, so it
 * does not trap keyboard users inside an open popover.
 */
export function AccountPicker({ accounts, selected, onChange }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const root = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const close = (event) => {
      if (root.current && !root.current.contains(event.target)) setOpen(false);
    };
    const escape = (event) => event.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

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
    <div className="account-picker" ref={root}>
      <button
        type="button"
        className="picker-trigger"
        aria-expanded={open}
        aria-haspopup="listbox"
        onClick={() => setOpen((was) => !was)}
      >
        {summary}
      </button>
      {open && (
        <div className="picker-menu" role="listbox" aria-label="Accounts">
          <input
            aria-label="Filter accounts"
            placeholder="filter accounts"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          {selected.length > 0 && (
            <button type="button" className="link small" onClick={() => onChange([])}>
              Clear selection
            </button>
          )}
          <ul>
            {visible.map((row) => {
              const on = selected.includes(row.handle);
              return (
                <li key={row.handle}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={on}
                    className={on ? "picker-row active" : "picker-row"}
                    onClick={() =>
                      onChange(
                        on
                          ? selected.filter((h) => h !== row.handle)
                          : [...selected, row.handle],
                      )
                    }
                  >
                    <Avatar account={row} size={24} />
                    <span className="picker-name">{row.display_name || row.handle}</span>
                    <span className="picker-handle">@{row.handle}</span>
                  </button>
                </li>
              );
            })}
            {!visible.length && <li className="muted picker-empty">No matching account.</li>}
          </ul>
        </div>
      )}
    </div>
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
