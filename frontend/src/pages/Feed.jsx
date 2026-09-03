import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Archive, ArrowUp, Download, Image, X } from "lucide-react";
import { api, authorizedFetch } from "../api";
import { AccountPicker, Segmented, ToggleChips, useAccounts } from "../filters";
import InfiniteSentinel from "../InfiniteSentinel";
import TweetCard from "../TweetCard";
import { Button } from "@/ui/button";
import { Chip, Empty, ErrorNote } from "@/ui/controls";
import { Input, Select } from "@/ui/field";
import { PageHead } from "@/ui/panel";

// Engagement and reach are separate questions and get separate buttons. They
// used to be one "Most engaged" sort that summed likes+reposts+views, and since
// views are a hundred times larger than anything else it only ever ranked reach.
const SORTS = [
  { value: "latest", label: "Latest" },
  { value: "top", label: "Most engaged" },
  { value: "views", label: "Most viewed" },
];

// Calendar boundaries, resolved Tehran-side by the API. These labels used to be
// aliases for rolling windows, so at 00:30 "Today" was almost entirely
// yesterday. "1h" stays rolling because that is what the word means.
const WINDOWS = [
  { value: "1h", label: "1h" },
  { value: "today", label: "Today" },
  { value: "week", label: "This week" },
  { value: "month", label: "This month" },
  { value: "", label: "All time" },
];

const POST_TYPES = [
  { value: "tweet", label: "Posts" },
  { value: "reply", label: "Replies" },
  { value: "retweet", label: "Reposts" },
  { value: "quote", label: "Quotes" },
];

// Export polling. The ceiling matters more than the interval: a job that never
// finishes has to end as a message, not a spinner that runs until the tab dies.
const EXPORT_POLL_MS = 1500;
const EXPORT_POLL_LIMIT = 120; // ~3 minutes

const DEFAULTS = {
  sort: "latest",
  window: "today",
  q: "",
  types: [],
  accounts: [],
  tier: "",
  has_media: false,
  // Posts from accounts that were tracked once and are not any more. They stay
  // archived and stay counted in "archive total", so without this they were
  // 6,159 rows the headline claimed and no screen could reach.
  include_untracked: false,
};

/** Read the whole filter state out of the URL, so a view is shareable. */
function readFilters(params) {
  return {
    sort: params.get("sort") || DEFAULTS.sort,
    window: params.has("window") ? params.get("window") : DEFAULTS.window,
    q: params.get("q") || "",
    types: (params.get("types") || "").split(",").filter(Boolean),
    accounts: params.getAll("account"),
    tier: params.get("tier") || "",
    has_media: params.get("has_media") === "1",
    include_untracked: params.get("include_untracked") === "1",
  };
}

function feedQuery(filters) {
  const params = new URLSearchParams();
  if (filters.sort !== "latest") params.set("sort", filters.sort);
  if (filters.window) params.set("window", filters.window);
  if (filters.q) params.set("q", filters.q);
  if (filters.types.length) params.set("types", filters.types.join(","));
  if (filters.tier) params.set("tier", filters.tier);
  if (filters.has_media) params.set("has_media", "1");
  if (filters.include_untracked) params.set("include_untracked", "1");
  for (const handle of filters.accounts) params.append("account", handle);
  return params;
}

export default function Feed() {
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = readFilters(searchParams);
  const accounts = useAccounts();

  const [tweets, setTweets] = useState([]);
  const [next, setNext] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [pending, setPending] = useState([]);
  const [draftQuery, setDraftQuery] = useState(filters.q);
  // Which format is currently being built, so the button can say so. An export
  // is now a job, and a button that looks idle while one runs invites a second.
  const [exporting, setExporting] = useState("");
  const [notice, setNotice] = useState("");

  // Monotonic id for the active query. A page-append that is still in flight when
  // the user re-filters belongs to the previous query, so its rows must be
  // discarded rather than concatenated onto the new result set.
  const queryId = useRef(0);
  const inFlight = useRef(false);
  // Latest rendered rows, for the poll below to diff against without reaching
  // into a state updater.
  const shown = useRef(tweets);
  shown.current = tweets;
  const key = feedQuery(filters).toString();

  const load = useCallback(
    async (url) => {
      // Only the infinite-scroll append is de-duplicated. Guarding the filter path
      // too meant a Filter submitted while page 1 was still loading was dropped
      // silently, and the feed stayed empty until the user acted again.
      if (url && inFlight.current) return;
      const id = url ? queryId.current : (queryId.current += 1);
      inFlight.current = true;
      setLoading(true);
      try {
        const path = url ? url.replace(/^.*\/api/, "") : `/feed/${key ? `?${key}` : ""}`;
        const data = await api(path);
        if (id !== queryId.current) return; // superseded by a newer query
        setTweets((prev) => (url ? [...prev, ...data.results] : data.results));
        setNext(data.next);
        setError("");
      } catch (e) {
        if (id === queryId.current) setError(e.message);
      } finally {
        if (id === queryId.current) {
          inFlight.current = false;
          setLoading(false);
        }
      }
    },
    [key],
  );

  useEffect(() => {
    setTweets([]);
    setNext(null);
    setPending([]);
    load();
  }, [load]);

  // Poll for arrivals and hold them behind a pill rather than shifting the list
  // under the reader's cursor. Only meaningful for the chronological sort.
  useEffect(() => {
    if (filters.sort !== "latest") return undefined;
    const timer = setInterval(async () => {
      try {
        const data = await api(`/feed/${key ? `?${key}` : ""}`);
        // Read the current rows off a ref rather than from inside a setTweets
        // updater: an updater must be pure, and StrictMode double-invokes it,
        // so setting other state in there fires twice per poll.
        const known = new Set(shown.current.map((t) => t.id));
        setPending((data.results || []).filter((t) => !known.has(t.id)));
      } catch {
        /* a transient blip must not clear the feed the reader is looking at */
      }
    }, 30000);
    return () => clearInterval(timer);
  }, [key, filters.sort]);

  // Typing searches on its own after a pause. Without this the box only
  // responded to Enter, with nothing on screen saying so, and clearing it left
  // the previous results in place.
  useEffect(() => {
    if (draftQuery === filters.q) return undefined;
    const timer = setTimeout(() => update({ q: draftQuery }), 350);
    return () => clearTimeout(timer);
  }, [draftQuery, filters.q]);

  // A filter change from elsewhere (back button, cleared chip) has to be
  // reflected in the box, or the two disagree about what is being searched.
  useEffect(() => {
    setDraftQuery(filters.q);
  }, [filters.q]);

  function update(patch) {
    const nextFilters = { ...filters, ...patch };
    const params = new URLSearchParams();
    if (nextFilters.sort !== DEFAULTS.sort) params.set("sort", nextFilters.sort);
    if (nextFilters.window !== DEFAULTS.window) params.set("window", nextFilters.window);
    if (nextFilters.q) params.set("q", nextFilters.q);
    if (nextFilters.types.length) params.set("types", nextFilters.types.join(","));
    if (nextFilters.tier) params.set("tier", nextFilters.tier);
    if (nextFilters.has_media) params.set("has_media", "1");
    if (nextFilters.include_untracked) params.set("include_untracked", "1");
    for (const handle of nextFilters.accounts) params.append("account", handle);
    setSearchParams(params, { replace: true });
  }

  function showPending() {
    setTweets((current) => [...pending, ...current]);
    setPending([]);
  }

  // The export is built on a worker, not streamed out of the request. A
  // full-archive extract used to hold one of two gunicorn workers for its whole
  // duration, so two at once took the console down for everyone. The cost of
  // that fix is that the UI has to wait for a job rather than a response.
  async function downloadExport(fmt) {
    setExporting(fmt);
    setError("");
    try {
      const job = await api("/export/", {
        method: "POST",
        body: { format: fmt, query: feedQuery(filters).toString() },
      });
      const finished = await waitForExport(job.id);
      if (finished.status !== "completed") {
        setError(finished.error || "Export failed.");
        return;
      }
      await saveExport(finished);
      if (finished.truncated) {
        setNotice(
          `Exported the first ${finished.row_count.toLocaleString()} posts. ` +
            "Narrow the window or add filters to export the rest.",
        );
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setExporting("");
    }
  }

  // Poll until the worker finishes. Bounded: a job that never resolves must
  // surface as an error rather than spinning silently forever.
  async function waitForExport(id) {
    for (let attempt = 0; attempt < EXPORT_POLL_LIMIT; attempt += 1) {
      const job = await api(`/export/${id}/`);
      if (job.status === "completed" || job.status === "failed") return job;
      await new Promise((resolve) => setTimeout(resolve, EXPORT_POLL_MS));
    }
    throw new Error("Export is taking longer than expected — check back shortly.");
  }

  // The download is authenticated, so it cannot be a plain link: fetch it with
  // the access token and hand the browser a blob.
  async function saveExport(job) {
    let url;
    try {
      const res = await authorizedFetch(`/api${job.download_url.replace(/^\/api/, "")}`);
      if (!res.ok) throw new Error(`Download failed (${res.status}).`);
      url = URL.createObjectURL(await res.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = job.filename;
      link.click();
    } finally {
      // Revoking synchronously after click() can cancel the download before the
      // browser has read the blob; defer to the next task instead.
      if (url) setTimeout(() => URL.revokeObjectURL(url), 0);
    }
  }

  return (
    <section className="flex flex-col gap-5">
      <PageHead
        label="Feed"
        title="Tracked accounts"
        // Naming the collector is the point: this page and the Search page look
        // alike and hold different things, and the difference used to be
        // invisible because both streams were merged into this one.
        lede="Everything the account collector has captured from the timelines you track. Saved searches are on their own page."
        actions={
          <>
            <Button
              size="sm"
              disabled={Boolean(exporting)}
              onClick={() => downloadExport("jsonl")}
            >
              <Download className="size-3.5" aria-hidden="true" />
              {exporting === "jsonl" ? "Preparing JSONL…" : "Export JSONL"}
            </Button>
            <Button
              size="sm"
              disabled={Boolean(exporting)}
              onClick={() => downloadExport("csv")}
            >
              <Download className="size-3.5" aria-hidden="true" />
              {exporting === "csv" ? "Preparing CSV…" : "Export CSV"}
            </Button>
          </>
        }
      />

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_15rem]">
        <div className="order-2 min-w-0 lg:order-1">
          {error && <ErrorNote className="mb-3">{error}</ErrorNote>}
          {/* A truncated export is a correct answer to a different question,
              so it is said plainly rather than as an error. */}
          {notice && (
            <p className="mb-3 text-xs text-fg-muted" role="status">
              {notice}
            </p>
          )}

          <div className="mx-auto max-w-2xl">
            {pending.length > 0 && (
              <Button
                variant="primary"
                className="mb-3 w-full"
                onClick={showPending}
              >
                <ArrowUp className="size-4" aria-hidden="true" />
                {pending.length} new post{pending.length === 1 ? "" : "s"}
              </Button>
            )}

            {!loading && !tweets.length && !error ? (
              <Empty title="No posts match these filters">
                Widen the time window, or clear the account and type filters.
              </Empty>
            ) : (
              <div className="rounded-sm bg-paper">
                {tweets.map((t) => (
                  <TweetCard key={t.id} tweet={t} />
                ))}
                <InfiniteSentinel next={next} loading={loading} onLoad={load} />
              </div>
            )}
          </div>
        </div>

        {/* Sticky rail rather than a bar across the top: the filters stay
            reachable through a long scroll, and the reading column keeps a
            measure that does not stretch to the window. */}
        {/* A details element on purpose: on a phone the full control stack is
            ~480px, so the first post used to start below the fold. Collapsed it
            is one row; on desktop the CSS in index.css hides the summary and
            forces the panel open, so nothing changes there.
            No `open` attribute: setting it collapsed the phone case back open
            again and put the first post 885px down, which is the whole thing
            this element exists to prevent. Desktop is handled entirely in CSS. */}
        <details className="feed-filters order-1 lg:sticky lg:top-4 lg:order-2 lg:h-max">
          <summary className="mb-3 cursor-pointer list-none rounded-sm border border-line px-3 py-2 text-sm text-fg-muted lg:hidden">
            Filters and sort
          </summary>
          <div className="feed-filters-body flex flex-col gap-4">
          {/* Debounced, so typing searches on its own. Enter-only meant a
              typed query did nothing until you guessed to press it, and
              emptying the box left the old results on screen. */}
          <form
            className="relative"
            role="search"
            onSubmit={(e) => {
              e.preventDefault();
              update({ q: draftQuery });
            }}
          >
            <Input
              aria-label="Search archive"
              placeholder="Search the archive"
              value={draftQuery}
              className={draftQuery ? "pr-9" : undefined}
              onChange={(e) => setDraftQuery(e.target.value)}
            />
            {draftQuery ? (
              <button
                type="button"
                aria-label="Clear search"
                title="Clear search"
                onClick={() => {
                  setDraftQuery("");
                  update({ q: "" });
                }}
                className="absolute right-1 top-1/2 -translate-y-1/2 rounded-sm p-1.5 text-muted transition-colors hover:text-ink"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            ) : null}
          </form>

          <div className="flex flex-col gap-1.5">
            <span className="eyebrow">Sort</span>
            <Segmented
              label="Sort"
              options={SORTS}
              value={filters.sort}
              onChange={(sort) => update({ sort })}
              className="self-start"
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <span className="eyebrow">Time window</span>
            <Segmented
              label="Time window"
              options={WINDOWS}
              value={filters.window}
              onChange={(window) => update({ window })}
              className="self-start"
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <span className="eyebrow">Post types</span>
            <ToggleChips
              label="Post types"
              options={POST_TYPES}
              values={filters.types}
              onChange={(types) => update({ types })}
            />
            <Chip
              pressed={filters.has_media}
              className="mt-1 self-start"
              onClick={() => update({ has_media: !filters.has_media })}
            >
              <Image className="mr-1 inline size-3" aria-hidden="true" />
              Media only
            </Chip>
          </div>

          <div className="flex flex-col gap-1.5">
            <span className="eyebrow">Accounts</span>
            <AccountPicker
              accounts={accounts}
              selected={filters.accounts}
              onChange={(next) => update({ accounts: next })}
            />
            <Select
              aria-label="Tier"
              className="mt-1"
              value={filters.tier}
              onChange={(e) => update({ tier: e.target.value })}
            >
              <option value="">All tiers</option>
              {[1, 2, 3, 4, 5, 6, 7].map((n) => (
                <option key={n} value={n}>
                  Priority {n}
                </option>
              ))}
            </Select>
            <Chip
              pressed={filters.include_untracked}
              className="mt-1 self-start"
              onClick={() => update({ include_untracked: !filters.include_untracked })}
            >
              <Archive className="mr-1 inline size-3" aria-hidden="true" />
              Include untracked
            </Chip>
          </div>
          </div>
        </details>
      </div>
    </section>
  );
}
