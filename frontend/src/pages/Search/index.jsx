import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Pause, Play, Plus, RefreshCw, Trash2 } from "lucide-react";
import { api } from "@/api";
import { useAuth } from "@/auth";
import { cn } from "@/lib/cn";
import { compact, duration } from "@/format";
import InfiniteSentinel from "@/InfiniteSentinel";
import TweetCard from "@/TweetCard";
import { Button } from "@/ui/button";
import { Empty, ErrorNote, Skeleton } from "@/ui/controls";
import { PageHead, Panel, PanelBody, PanelHead } from "@/ui/panel";
import { SCHEDULE_TONE, Status } from "@/ui/status";
import { Tab, TabList, TabPanel, Tabs } from "@/ui/tabs";
import DeleteDialog from "./DeleteDialog";
import QueryDialog from "./QueryDialog";
import Workflow from "./Workflow";
import { usePoll } from "@/usePoll";

const SEARCHES_POLL_MS = 20000;

const STATE_LABEL = {
  running: "Fetching",
  queued: "Queued",
  paused: "Paused",
  idle: "Waiting",
};

/** One saved query in the rail: what it is, and what it is doing right now. */
function QueryRow({ search, active, onSelect }) {
  const state = search.schedule?.state || "idle";
  // A paused query has no next run, so it must not show a countdown. It was
  // rendering "next in 0s" (and, for one, "next in 23h 59m") beside the word
  // Paused -- two claims that cannot both be true.
  const due = state === "paused"
    ? "not scheduled"
    : search.schedule?.is_due
      ? "due now"
      : search.schedule?.next_due_at
        ? `next in ${duration(search.schedule.seconds_until_due)}`
        : "not run yet";
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        aria-current={active ? "true" : undefined}
        className={cn(
          "w-full border-l-2 px-3 py-2 text-left transition-colors",
          active
            ? "border-l-accent bg-ink-700"
            : "border-l-transparent hover:border-l-line-strong hover:bg-ink-800",
        )}
      >
        <span className="flex items-baseline justify-between gap-2">
          <span className="truncate text-sm font-medium">{search.name || search.slug}</span>
          <span className="shrink-0 font-mono text-2xs tabular text-fg-dim">
            {compact(search.hit_count || 0)}
          </span>
        </span>
        <span className="mt-1 flex items-center justify-between gap-2">
          <Status tone={SCHEDULE_TONE[state]} className="text-2xs">
            {STATE_LABEL[state]}
          </Status>
          <span className="truncate font-mono text-2xs text-fg-dim">{due}</span>
        </span>
      </button>
    </li>
  );
}

export default function SearchWorkspace() {
  const { searchId } = useParams();
  const navigate = useNavigate();
  const { isStaff } = useAuth();

  const [searches, setSearches] = useState(null);
  const [results, setResults] = useState([]);
  const [next, setNext] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [saving, setSaving] = useState(false);

  // A slower response for a previously selected query must not paint over the
  // one the operator is now looking at.
  const activeId = useRef(null);
  const searchesRequestSeq = useRef(0);

  const selected = (searches || []).find((row) => String(row.id) === String(searchId)) || null;

  // Returns the rail as the server just described it, or `null` when a newer
  // request superseded this one. `null` and `[]` are not interchangeable here:
  // callers navigate on the result, and reporting "no saved queries" for a
  // response we merely chose to ignore would strand the operator on the empty
  // workspace while their other queries still exist.
  const loadSearches = useCallback(async () => {
    const request = ++searchesRequestSeq.current;
    try {
      const data = await api("/searches/");
      if (request !== searchesRequestSeq.current) return null;
      setSearches(data.results || data);
      setError("");
      return data.results || data;
    } catch (e) {
      if (request !== searchesRequestSeq.current) return null;
      setError(e.message);
      setSearches([]);
      return [];
    }
  }, []);

  // The rail shows live state per query, so it has to keep up on its own --
  // but only while someone is looking at it.
  usePoll(loadSearches, SEARCHES_POLL_MS);

  const loadResults = useCallback(
    async (url) => {
      if (!searchId) return;
      if (url && loading) return;
      activeId.current = searchId;
      setLoading(true);
      try {
        const path = url ? url.replace(/^.*\/api/, "") : `/searches/${searchId}/results/`;
        const data = await api(path);
        if (activeId.current !== searchId) return;
        setResults((prev) => (url ? [...prev, ...(data.results || [])] : data.results || []));
        setNext(data.next || null);
        setError("");
      } catch (e) {
        // A deleted or mistyped id surfaced DRF's own "No Search matches the
        // given query.", which reads like a bug report rather than an answer.
        if (activeId.current === searchId) {
          setError(
            /No Search matches/i.test(e.message)
              ? "That saved query no longer exists — it may have been deleted."
              : e.message,
          );
        }
      } finally {
        if (activeId.current === searchId) setLoading(false);
      }
    },
    [searchId, loading],
  );

  useEffect(() => {
    setResults([]);
    setNext(null);
    loadResults();
    // loadResults changes with `loading`, which would re-fetch page 1 on every
    // load; the query id is the only thing that should restart the list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchId]);

  async function createSearch(body) {
    setSaving(true);
    try {
      const created = await api("/searches/", { method: "POST", body });
      await loadSearches();
      setNotice(`"${created.name}" created and queued to run now.`);
      navigate(`/search/${created.id}`);
    } finally {
      setSaving(false);
    }
  }

  async function updateSearch(body) {
    setSaving(true);
    try {
      await api(`/searches/${selected.id}/`, { method: "PATCH", body });
      await loadSearches();
      setNotice("Changes saved. They take effect on the next run.");
    } finally {
      setSaving(false);
    }
  }

  async function act(path, message) {
    try {
      await api(`/searches/${selected.id}/${path}/`, { method: "POST" });
      await loadSearches();
      setNotice(message);
      setError("");
    } catch (e) {
      setError(e.message);
    }
  }

  async function removeSearch() {
    const deleted = selected;
    const removed = await api(`/searches/${deleted.id}/`, { method: "DELETE" });
    // Fall back to the list we already hold when the 20s rail poll superseded
    // this refresh, and filter the deleted row out either way -- a refresh that
    // raced the DELETE can still describe it as present.
    const refreshed = await loadSearches();
    const remaining = (refreshed ?? searches ?? []).filter(
      (row) => String(row.id) !== String(deleted.id),
    );
    setNotice(
      `Deleted "${deleted.name}" — ${compact(removed.hits || 0)} result(s), ` +
        `${removed.fetch_runs || 0} run(s) and its schedule are gone.`,
    );
    navigate(remaining.length ? `/search/${remaining[0].id}` : "/search");
  }

  const paused = selected?.schedule?.state === "paused";

  return (
    <section className="flex flex-col gap-5">
      <PageHead
        label="Search"
        title="Saved queries"
        lede="Each phrase is its own recurring job with its own schedule, history and results. Deleting one stops and removes everything behind it."
        actions={
          isStaff && (
            <Button variant="primary" onClick={() => setCreating(true)}>
              <Plus className="size-4" aria-hidden="true" />
              New search
            </Button>
          )
        }
      />

      {notice && <p className="annunciator border-l-accent text-sm text-fg-muted">{notice}</p>}
      {error && <ErrorNote>{error}</ErrorNote>}

      <div className="grid gap-5 lg:grid-cols-[16rem_minmax(0,1fr)]">
        <Panel className="h-max overflow-hidden">
          <PanelHead label={`${searches?.length ?? 0} queries`} className="px-3 py-2" />
          {searches === null ? (
            <div className="flex flex-col gap-1 p-3">
              <Skeleton className="h-10" />
              <Skeleton className="h-10" />
            </div>
          ) : searches.length === 0 ? (
            <p className="p-3 text-xs text-fg-muted">
              No saved queries yet. Create one to start collecting from X search.
            </p>
          ) : (
            <ul className="flex flex-col py-1">
              {searches.map((row) => (
                <QueryRow
                  key={row.id}
                  search={row}
                  active={String(row.id) === String(searchId)}
                  onSelect={() => navigate(`/search/${row.id}`)}
                />
              ))}
            </ul>
          )}
        </Panel>

        {!selected ? (
          <Empty title="Pick a query">
            Choose a saved query to read what it found and see when it next runs.
          </Empty>
        ) : (
          <div className="flex min-w-0 flex-col gap-4">
            <Panel>
              <PanelHead
                label={`${selected.product} · every ${duration(selected.interval_seconds)}`}
                title={selected.name || selected.slug}
                actions={
                  isStaff && (
                    <>
                      <Button size="sm" onClick={() => act("refresh", "Run queued.")}>
                        <RefreshCw className="size-3.5" aria-hidden="true" />
                        Run now
                      </Button>
                      <Button
                        size="sm"
                        onClick={() =>
                          act("pause", paused ? "Schedule resumed." : "Schedule paused.")
                        }
                      >
                        {paused ? (
                          <Play className="size-3.5" aria-hidden="true" />
                        ) : (
                          <Pause className="size-3.5" aria-hidden="true" />
                        )}
                        {paused ? "Resume" : "Pause"}
                      </Button>
                      <Button size="sm" onClick={() => setEditing(true)}>
                        Edit
                      </Button>
                      <Button
                        size="sm"
                        variant="danger"
                        aria-label={`Delete ${selected.name || selected.slug}`}
                        onClick={() => setDeleting(true)}
                      >
                        <Trash2 className="size-3.5" aria-hidden="true" />
                        Delete
                      </Button>
                    </>
                  )
                }
              />
              <PanelBody className="py-3">
                <p className="break-words font-mono text-xs text-fg-muted">{selected.raw_query}</p>
              </PanelBody>
            </Panel>

            <Tabs defaultValue="results">
              <TabList>
                <Tab value="results">Results</Tab>
                <Tab value="workflow">Workflow</Tab>
              </TabList>

              <TabPanel value="results" className="pt-4">
                {results.length === 0 && !loading ? (
                  <Empty title="Nothing stored yet">
                    This query has not returned any results. If it has never run, the first run may
                    still be queued.
                  </Empty>
                ) : (
                  <div className="mx-auto flex max-w-2xl flex-col rounded-sm bg-paper">
                    {results.map((tweet) => (
                      <TweetCard key={tweet.id} tweet={tweet} />
                    ))}
                    <InfiniteSentinel next={next} loading={loading} onLoad={loadResults} />
                  </div>
                )}
              </TabPanel>

              <TabPanel value="workflow" className="pt-4">
                <Workflow
                  search={selected}
                  running={selected.schedule?.state}
                  onRunNow={() => act("refresh", "Run queued.")}
                />
              </TabPanel>
            </Tabs>
          </div>
        )}
      </div>

      <QueryDialog
        open={creating}
        onOpenChange={setCreating}
        onSubmit={createSearch}
        saving={saving}
      />
      {selected && (
        <>
          <QueryDialog
            key={selected.id}
            open={editing}
            onOpenChange={setEditing}
            search={selected}
            onSubmit={updateSearch}
            saving={saving}
          />
          <DeleteDialog
            open={deleting}
            onOpenChange={setDeleting}
            search={selected}
            onConfirm={removeSearch}
          />
        </>
      )}
    </section>
  );
}
