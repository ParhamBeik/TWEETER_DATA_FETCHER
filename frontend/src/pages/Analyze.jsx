import { useCallback, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { EyeOff } from "lucide-react";
import { api } from "../api";
import { useAuth } from "../auth";
import { AXIS_PROPS, LINE, SERIES, TOOLTIP_STYLE, bucketLabel } from "../charts";
import {
  AccountPicker,
  BUCKETS,
  RANGES,
  Segmented,
  useAccounts,
  useLiveRefresh,
  windowParams,
} from "../filters";
import { absoluteTime, compact, relativeTime } from "../format";
import TweetCard from "../TweetCard";
import { Button } from "@/ui/button";
import { Chip, Empty, ErrorNote, Skeleton } from "@/ui/controls";
import { PageHead, Panel, PanelBody, PanelHead } from "@/ui/panel";
import { Tab, TabList, TabPanel, Tabs } from "@/ui/tabs";

const DIMENSIONS = [
  { value: "hashtags", label: "Hashtags" },
  { value: "phrases", label: "Phrases" },
  { value: "both", label: "Both" },
];

const RANKINGS = [
  { value: "surging", label: "Surging" },
  { value: "volume", label: "Most posts" },
];

/**
 * How much more (or less) of the conversation a term is than it used to be.
 *
 * Stated as a multiple rather than as the raw score: "3.1× its usual rate" is
 * something a reader can check against their own sense of the week, where a
 * z-score is something they have to take on trust.
 */
function rateLabel(row) {
  if (!row.baseline_share) return "new this window";
  if (!row.share) return "quiet this window";
  const multiple = row.share / row.baseline_share;
  if (!Number.isFinite(multiple) || multiple <= 0) return "quiet this window";
  if (multiple >= 1) return `${multiple.toFixed(1)}× its usual rate`;
  return `${(1 / multiple).toFixed(1)}× below its usual rate`;
}

function TopicRow({ row, onOpen, onHide, canHide, showKind, rank }) {
  const label = row.kind === "hashtag" ? `#${row.topic}` : row.topic;
  return (
    <tr className="group border-b border-line last:border-b-0">
      <td className="py-2 pr-3">
        <button
          type="button"
          onClick={() => onOpen(row.topic)}
          className="text-left text-sm hover:text-accent hover:underline"
        >
          {label}
        </button>
        {/* Only worth saying when the table actually mixes the two. With the
            Phrases filter on, every row carried an identical "PHRASE" badge --
            and it read as a claim that single words were phrases. */}
        {showKind && (
          <span className="ml-2 font-mono text-2xs uppercase tracking-wider text-fg-dim">
            {row.kind}
          </span>
        )}
      </td>
      <td className="py-2 pr-3 text-right font-mono text-sm tabular">{compact(row.docs)}</td>
      <td className="py-2 pr-3 text-right font-mono text-sm tabular text-fg-muted">
        {compact(row.authors)}
      </td>
      {/* The column the table is actually sorted by. Without it the only
          visible ranking number was the rate multiplier, which runs
          3.1x, 18.7x, 2.4x, 15.2x down the page and looks like no order. */}
      {rank !== "volume" && (
        <td className="py-2 pr-3 text-right font-mono text-sm tabular text-fg-muted">
          {typeof row.score === "number" ? row.score.toFixed(2) : "—"}
        </td>
      )}
      <td className="py-2 pr-3 text-xs text-fg-muted">{rateLabel(row)}</td>
      <td className="py-2 text-right">
        {canHide && (
          <button
            type="button"
            onClick={() => onHide(row.topic)}
            aria-label={`Hide ${label}`}
            title="Hide this term from the panel"
            className="text-fg-dim opacity-0 transition-opacity hover:text-danger focus-visible:opacity-100 group-hover:opacity-100"
          >
            <EyeOff className="size-3.5" aria-hidden="true" />
          </button>
        )}
      </td>
    </tr>
  );
}

function Topics({ data, params, onReload, navigate, range }) {
  const { isStaff } = useAuth();
  const [dimension, setDimension] = params.dimension;
  const [rank, setRank] = params.rank;
  const rows = data?.results || [];

  async function hide(topic) {
    await api("/analytics/topics/hidden/", { method: "POST", body: { topic } });
    onReload();
  }

  return (
    <div className="flex flex-col gap-4">
      <Panel>
        <PanelHead
          label="Topics"
          title="What is being talked about that was not before"
          lede="Ranked by how unusual a term's rate is against the previous window of equal length, counting posts rather than word occurrences. Reposts, terms from a single account, and words common enough to be filler in any language are left out."
          actions={
            <>
              <Segmented
                label="Topic source"
                options={DIMENSIONS}
                value={dimension}
                onChange={setDimension}
              />
              <Segmented label="Rank by" options={RANKINGS} value={rank} onChange={setRank} />
            </>
          }
        />
        <PanelBody>
          {!data ? (
            <div className="flex flex-col gap-2">
              <Skeleton className="h-8" />
              <Skeleton className="h-8" />
              <Skeleton className="h-8" />
            </div>
          ) : rows.length === 0 ? (
            <Empty title="Nothing stands out in this window">
              {/* Hashtags are rare in this archive -- around one post in sixty
                  uses one -- so an empty hashtag table is the normal answer, not
                  a broken panel. Saying which question came back empty is the
                  difference between "no signal" and "no feature". */}
              {dimension === "hashtags"
                ? "No hashtag cleared the bar here. These accounts rarely use them, so this dimension is often empty — try Phrases, a longer range, or Both."
                : "Either the archive is quiet, or every term is running at its usual rate. Widen the time range to compare against a longer baseline."}
            </Empty>
          ) : (
            <table className="w-full">
              <caption className="sr-only">
                Terms ranked by {rank === "volume" ? "post count" : "how much their rate rose"}
              </caption>
              <thead>
                <tr className="border-b border-line text-left">
                  <th scope="col" className="pb-2 pr-3 eyebrow font-normal">
                    Term
                  </th>
                  <th scope="col" className="pb-2 pr-3 text-right eyebrow font-normal">
                    Posts
                  </th>
                  <th scope="col" className="pb-2 pr-3 text-right eyebrow font-normal">
                    Accounts
                  </th>
                  {rank !== "volume" && (
                    <th
                      scope="col"
                      className="pb-2 pr-3 text-right eyebrow font-normal"
                      title="How unusual this term's rate is, accounting for sample size. This is the sort order."
                    >
                      Surge
                    </th>
                  )}
                  <th scope="col" className="pb-2 pr-3 eyebrow font-normal">
                    Against baseline
                  </th>
                  <th scope="col" className="pb-2" />
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <TopicRow
                    key={`${row.kind}:${row.topic}`}
                    row={row}
                    rank={rank}
                    showKind={dimension === "both"}
                    canHide={isStaff}
                    onHide={hide}
                    onOpen={(topic) =>
                      navigate(`/feed?q=${encodeURIComponent(topic)}&window=${encodeURIComponent(range || "24h")}`)
                    }
                  />
                ))}
              </tbody>
            </table>
          )}
        </PanelBody>
      </Panel>
      {data?.total_docs != null && (
        <p className="text-xs text-fg-dim">
          Measured over {compact(data.total_docs)} posts, against{" "}
          {compact(data.previous_total_docs)} in the previous window.
        </p>
      )}
    </div>
  );
}

function Velocity({ data, bucket }) {
  const results = data?.results || [];
  const series = (data?.series || []).map((row) => ({
    ...row,
    label: bucketLabel(row.bucket, data?.bucket || bucket),
  }));
  return (
    <div className="flex flex-col gap-4">
      <Panel>
        <PanelHead
          label="Velocity"
          title="Engagement gained over time"
          lede="Likes and reposts added per bucket across posts we were already watching — not the totals those posts carry."
        />
        <PanelBody>
          {series.length ? (
            <div className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={series} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
                  <CartesianGrid stroke={LINE} vertical={false} />
                  <XAxis dataKey="label" {...AXIS_PROPS} />
                  <YAxis width={56} tickFormatter={compact} {...AXIS_PROPS} />
                  <Tooltip
                    contentStyle={TOOLTIP_STYLE}
                    labelFormatter={(_label, payload) =>
                      absoluteTime(payload?.[0]?.payload?.bucket)
                    }
                    formatter={(value, name) => [
                      compact(value),
                      name === "gained" ? "engagement gained" : "posts moving",
                    ]}
                  />
                  <Line
                    type="monotone"
                    dataKey="gained"
                    stroke={SERIES[0]}
                    strokeWidth={2}
                    // A handful of buckets renders as a bare line with nothing
                    // to aim at; show the points until the series is dense.
                    dot={series.length <= 12 ? { r: 4 } : false}
                    activeDot={{ r: 6 }}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <Empty title="No movement recorded yet">
              Velocity needs at least two snapshots of the same post inside the window.
            </Empty>
          )}
        </PanelBody>
      </Panel>
      {results.length > 0 && (
        <div className="mx-auto w-full max-w-2xl rounded-sm bg-paper">
          {results.map((tweet) => (
            <TweetCard key={tweet.id} tweet={tweet} />
          ))}
        </div>
      )}
      {data && !results.length && (
        <p className="text-sm text-fg-muted">Nothing gained engagement in this window.</p>
      )}
    </div>
  );
}

function Narratives({ data, stale }) {
  // `stale` means these rows answer the window the user just left. The pairwise
  // query runs for the better part of ten seconds, so without this the old
  // results sat under the new range button the whole time with nothing saying
  // they were about to be replaced -- the same "reading the wrong window"
  // problem as the error case, only quieter.
  const showing = stale ? null : data;
  const results = (showing?.results || []).filter((item) => item?.first && item?.follower);
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-fg-muted">
        Near-identical posts from different accounts, within a propagation window of each other.
      </p>
      {/* This query compares posts against each other pairwise and takes real
          seconds. A single thin skeleton read as "nothing here", so it says how
          long it expects to be. */}
      {!showing && (
        <div className="flex flex-col gap-2">
          <p className="text-xs text-fg-dim">
            Comparing posts against each other — this takes a few seconds.
          </p>
          <Skeleton className="h-20" />
          <Skeleton className="h-20" />
          <Skeleton className="h-20" />
        </div>
      )}
      {showing && !results.length && (
        <Empty title="No repeated claims found">
          Nothing in this window was posted near-identically by two different accounts.
        </Empty>
      )}
      {results.map((item, index) => (
        <Panel key={`${item.first.tweet_id}-${item.follower.tweet_id}`}>
          <PanelBody className="py-3">
            <p className="eyebrow">
              Narrative {index + 1} · {(item.similarity * 100).toFixed(0)}% similar
            </p>
            <div className="mt-2 grid gap-3 sm:grid-cols-2">
              {[
                ["Posted first", item.first],
                ["Followed", item.follower],
              ].map(([role, side]) => (
                <div key={role} className="annunciator">
                  <p className="eyebrow">{role}</p>
                  <a
                    className="text-sm font-medium hover:text-accent hover:underline"
                    href={`https://x.com/${side.account}/status/${side.tweet_id}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    @{side.account}
                  </a>
                  <p className="text-xs text-fg-dim" title={absoluteTime(side.created_at)}>
                    {relativeTime(side.created_at)}
                  </p>
                  {side.text && (
                    <p className="mt-2 text-xs leading-relaxed text-fg-muted line-clamp-4 whitespace-pre-wrap break-words">
                      {side.text}
                    </p>
                  )}
                </div>
              ))}
            </div>
          </PanelBody>
        </Panel>
      ))}
    </div>
  );
}

function Accounts({ data }) {
  const rows = data?.results || [];
  return (
    <Panel>
      <PanelHead
        label="Accounts"
        title="Who is posting, and who is landing"
        lede="Tracked accounts in this window, ranked by average engagement per post rather than by volume — a prolific account is not the same as an effective one. Engagement counts likes, reposts, replies and quotes; views are reach and are listed separately."
      />
      <PanelBody>
        {!data ? (
          <Skeleton className="h-24" />
        ) : rows.length === 0 ? (
          <Empty title="No tracked account posted in this window">
            Widen the time range, or check the collector on the dashboard.
          </Empty>
        ) : (
          <table className="w-full">
            <thead>
              <tr className="border-b border-line text-left">
                <th scope="col" className="pb-2 pr-3 eyebrow font-normal">
                  Account
                </th>
                <th scope="col" className="pb-2 pr-3 text-right eyebrow font-normal">
                  Posts
                </th>
                <th scope="col" className="pb-2 pr-3 text-right eyebrow font-normal">
                  Avg engagement
                </th>
                <th scope="col" className="pb-2 pr-3 text-right eyebrow font-normal">
                  Total
                </th>
                <th scope="col" className="pb-2 text-right eyebrow font-normal">
                  Replies
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.account} className="border-b border-line last:border-b-0">
                  <td className="py-2 pr-3 font-mono text-sm">@{row.account}</td>
                  <td className="py-2 pr-3 text-right font-mono text-sm tabular">
                    {compact(row.posts)}
                  </td>
                  <td className="py-2 pr-3 text-right font-mono text-sm tabular">
                    {compact(Math.round(row.average_engagement))}
                  </td>
                  <td className="py-2 pr-3 text-right font-mono text-sm tabular text-fg-muted">
                    {compact(row.total_engagement)}
                  </td>
                  <td className="py-2 text-right font-mono text-sm tabular text-fg-muted">
                    {compact(row.replies)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </PanelBody>
    </Panel>
  );
}

/** The exact request a set of controls asks for. Comparing it against the request
 *  the rows on screen actually came from is what makes "you are looking at the
 *  window you just left" detectable while a slow panel refetches. */
function panelKey({ tab, range, bucket, selected, dimension, rank }) {
  const params = windowParams({ range, bucket, accounts: selected });
  if (tab === "topics") {
    params.set("dimension", dimension);
    params.set("rank", rank);
  }
  return `${tab}?${params}`;
}

export default function Analyze() {
  const navigate = useNavigate();
  const accounts = useAccounts();
  const [tab, setTab] = useState("topics");
  const [range, setRange] = useState("24h");
  const [bucket, setBucket] = useState("auto");
  const [selected, setSelected] = useState([]);
  const [dimension, setDimension] = useState("phrases");
  const [rank, setRank] = useState("surging");
  const [live, setLive] = useState(true);
  const [data, setData] = useState(null);
  const [loadedKey, setLoadedKey] = useState(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    const key = panelKey({ tab, range, bucket, selected, dimension, rank });
    const params = windowParams({ range, bucket, accounts: selected });
    if (tab === "topics") {
      params.set("dimension", dimension);
      params.set("rank", rank);
    }
    try {
      const result = await api(`/analytics/${tab}/?${params}`);
      setData(result);
      setLoadedKey(key);
      setError("");
    } catch (e) {
      // Drop the rows as well as showing the error. Keeping them meant a failed
      // 7d narratives request left the previous 24h results on screen under a
      // "7d" button -- the reader was looking at a different window than the one
      // the controls claimed, with nothing saying so.
      setData(null);
      setLoadedKey(null);
      setError(e.message);
    }
  }, [tab, range, bucket, selected, dimension, rank]);

  // Each tab returns a different row shape. Clearing on switch matters: without
  // it the new tab renders the previous tab's rows for one frame, and
  // `narratives` reading `item.first.tweet_id` off a tweet threw, unmounting
  // the whole app.
  const switchTab = (next) => {
    setData(null);
    setLoadedKey(null);
    setTab(next);
  };

  const stale = loadedKey !== panelKey({ tab, range, bucket, selected, dimension, rank });

  useLiveRefresh(load, [tab, range, bucket, selected, dimension, rank], { live });

  return (
    <section className="flex flex-col gap-5">
      <PageHead
        label="Analyze"
        title="What changed in the archive"
        lede="Every panel here reads the tracked-account archive over the window you pick, and compares it against the equally long window before it."
      />

      <div className="flex flex-wrap items-center gap-3">
        <Segmented label="Time range" options={RANGES} value={range} onChange={setRange} />
        <Segmented label="Bucket" options={BUCKETS} value={bucket} onChange={setBucket} />
        <AccountPicker accounts={accounts} selected={selected} onChange={setSelected} />
        <Chip
          pressed={live}
          className="ml-auto"
          onClick={() => setLive((was) => !was)}
          title="Refresh every 30 seconds"
        >
          {live ? "● Live" : "❙❙ Paused"}
        </Chip>
      </div>

      {error && <ErrorNote>{error}</ErrorNote>}

      <Tabs value={tab} onValueChange={switchTab}>
        <TabList>
          <Tab value="topics">Topics</Tab>
          <Tab value="velocity">Velocity</Tab>
          <Tab value="narratives">Narratives</Tab>
          <Tab value="accounts">Accounts</Tab>
        </TabList>

        <TabPanel value="topics" className="pt-4">
          <Topics
            data={data}
            navigate={navigate}
            onReload={load}
            range={range}
            params={{ dimension: [dimension, setDimension], rank: [rank, setRank] }}
          />
        </TabPanel>
        <TabPanel value="velocity" className="pt-4">
          <Velocity data={data} bucket={bucket} />
        </TabPanel>
        <TabPanel value="narratives" className="pt-4">
          <Narratives data={data} stale={stale} />
        </TabPanel>
        <TabPanel value="accounts" className="pt-4">
          <Accounts data={data} />
        </TabPanel>
      </Tabs>
    </section>
  );
}
