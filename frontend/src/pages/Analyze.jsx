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
  const multiple = row.share / row.baseline_share;
  if (multiple >= 1) return `${multiple.toFixed(1)}× its usual rate`;
  return `${(1 / multiple).toFixed(1)}× below its usual rate`;
}

function TopicRow({ row, onOpen, onHide, canHide }) {
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
        <span className="ml-2 font-mono text-2xs uppercase tracking-wider text-fg-dim">
          {row.kind}
        </span>
      </td>
      <td className="py-2 pr-3 text-right font-mono text-sm tabular">{compact(row.docs)}</td>
      <td className="py-2 pr-3 text-right font-mono text-sm tabular text-fg-muted">
        {compact(row.authors)}
      </td>
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

function Topics({ data, params, onReload, navigate }) {
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
              Either the archive is quiet, or every term is running at its usual rate. Widen the
              time range to compare against a longer baseline.
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
                    canHide={isStaff}
                    onHide={hide}
                    onOpen={(topic) =>
                      navigate(`/feed?q=${encodeURIComponent(topic)}&window=`)
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
          lede="Likes, reposts and views added per bucket across posts we were already watching — not the totals those posts carry."
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

function Narratives({ data }) {
  const results = (data?.results || []).filter((item) => item?.first && item?.follower);
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-fg-muted">
        Near-identical posts from different accounts, within a propagation window of each other.
      </p>
      {!data && <Skeleton className="h-24" />}
      {data && !results.length && (
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
        lede="Tracked accounts in this window, ranked by average engagement per post rather than by volume — a prolific account is not the same as an effective one."
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
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    const params = windowParams({ range, bucket, accounts: selected });
    if (tab === "topics") {
      params.set("dimension", dimension);
      params.set("rank", rank);
    }
    try {
      const result = await api(`/analytics/${tab}/?${params}`);
      setData(result);
      setError("");
    } catch (e) {
      setError(e.message);
    }
  }, [tab, range, bucket, selected, dimension, rank]);

  // Each tab returns a different row shape. Clearing on switch matters: without
  // it the new tab renders the previous tab's rows for one frame, and
  // `narratives` reading `item.first.tweet_id` off a tweet threw, unmounting
  // the whole app.
  const switchTab = (next) => {
    setData(null);
    setTab(next);
  };

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
            params={{ dimension: [dimension, setDimension], rank: [rank, setRank] }}
          />
        </TabPanel>
        <TabPanel value="velocity" className="pt-4">
          <Velocity data={data} bucket={bucket} />
        </TabPanel>
        <TabPanel value="narratives" className="pt-4">
          <Narratives data={data} />
        </TabPanel>
        <TabPanel value="accounts" className="pt-4">
          <Accounts data={data} />
        </TabPanel>
      </Tabs>
    </section>
  );
}
