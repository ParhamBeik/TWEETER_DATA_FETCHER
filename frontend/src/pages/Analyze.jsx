import { useCallback, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../api";
import {
  AXIS_PROPS,
  BAR_RADIUS_X,
  LINE,
  SERIES,
  TOOLTIP_STYLE,
  bucketLabel,
} from "../charts";
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

const TABS = [
  { value: "topics", label: "Topics" },
  { value: "velocity", label: "Velocity" },
  { value: "narratives", label: "Narratives" },
];

const DIMENSIONS = [
  { value: "hashtags", label: "Hashtags" },
  { value: "phrases", label: "Phrases" },
  { value: "both", label: "Both" },
];

export default function Analyze() {
  const navigate = useNavigate();
  const accounts = useAccounts();
  const [tab, setTab] = useState("topics");
  const [range, setRange] = useState("24h");
  const [bucket, setBucket] = useState("auto");
  const [selected, setSelected] = useState([]);
  const [dimension, setDimension] = useState("hashtags");
  const [live, setLive] = useState(true);
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    const params = windowParams({ range, bucket, accounts: selected });
    if (tab === "topics") params.set("dimension", dimension);
    try {
      const result = await api(`/analytics/${tab}/?${params}`);
      setData(result);
      setError("");
    } catch (e) {
      setError(e.message);
    }
  }, [tab, range, bucket, selected, dimension]);

  // Each tab returns a different row shape. Clearing on switch matters: without
  // it the new tab renders the previous tab's rows for one frame, and
  // `narratives` reading `item.first.tweet_id` off a tweet threw, unmounting
  // the whole app.
  const switchTab = (next) => {
    setData(null);
    setTab(next);
  };

  useLiveRefresh(load, [tab, range, bucket, selected, dimension], { live });

  const results = data?.results || [];
  const topics = results.slice(0, 20).map((row) => ({
    ...row,
    label: row.kind === "hashtag" ? `#${row.topic}` : row.topic,
  }));
  const series = (data?.series || []).map((row) => ({
    ...row,
    label: bucketLabel(row.bucket, data?.bucket || bucket),
  }));

  return (
    <section className="stack-md">
      <header className="page-head">
        <div>
          <p className="eyebrow">Analyze</p>
          <h2 className="page-title">What is accelerating in the archive?</h2>
        </div>
        <Link className="inline-cta" to="/searches">
          Open saved searches →
        </Link>
      </header>

      <div className="control-bar">
        <Segmented label="Time range" options={RANGES} value={range} onChange={setRange} />
        <Segmented label="Bucket" options={BUCKETS} value={bucket} onChange={setBucket} />
        <AccountPicker accounts={accounts} selected={selected} onChange={setSelected} />
        <span className="control-spacer" />
        <button
          type="button"
          aria-pressed={live}
          className={live ? "chip active" : "chip"}
          onClick={() => setLive((was) => !was)}
          title="Refresh every 30 seconds"
        >
          {live ? "● Live" : "❙❙ Paused"}
        </button>
      </div>

      <div className="tabs">
        {TABS.map(({ value, label }) => (
          <button
            type="button"
            aria-pressed={tab === value}
            className={tab === value ? "tab active" : "tab"}
            key={value}
            onClick={() => switchTab(value)}
          >
            {label}
          </button>
        ))}
      </div>

      {error && <p className="error">{error}</p>}

      {tab === "topics" && (
        <div className="stack-sm">
          <div className="control-bar control-bar-secondary">
            <Segmented
              label="Topic source"
              options={DIMENSIONS}
              value={dimension}
              onChange={setDimension}
            />
            <span className="muted">
              Compared against the previous {range}. Click a topic to see its posts.
            </span>
          </div>
          {topics.length ? (
            <>
              {/* Height follows the row count. A fixed box squeezed 20 bars into
                  420px, so Recharts dropped every other category label and the
                  bars ran together. 30px a row keeps one label per bar. */}
              <article className="panel" style={{ height: topics.length * 30 + 56 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart
                    data={topics}
                    layout="vertical"
                    margin={{ left: 12, right: 24 }}
                    onClick={(state) => {
                      const topic = state?.activePayload?.[0]?.payload?.topic;
                      if (topic) navigate(`/feed?q=${encodeURIComponent(topic)}&window=`);
                    }}
                  >
                    <CartesianGrid stroke={LINE} horizontal={false} />
                    <XAxis type="number" allowDecimals={false} {...AXIS_PROPS} />
                    <YAxis type="category" dataKey="label" width={140} {...AXIS_PROPS} />
                    <Tooltip
                      cursor={{ fill: "rgba(255,255,255,0.04)" }}
                      contentStyle={TOOLTIP_STYLE}
                      formatter={(value, _name, item) => [
                        `${value} posts · ${item.payload.delta >= 0 ? "+" : ""}${item.payload.delta} vs previous`,
                        item.payload.kind === "hashtag" ? "Hashtag" : "Phrase",
                      ]}
                    />
                    <Bar dataKey="current_count" radius={BAR_RADIUS_X} maxBarSize={22}>
                      {topics.map((row) => (
                        // Colour follows the kind, not the row's rank, so
                        // re-filtering never repaints a surviving topic.
                        <Cell
                          key={row.topic}
                          fill={row.kind === "hashtag" ? SERIES[0] : SERIES[2]}
                        />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </article>
              <ul className="topic-list">
                {topics.map((row) => (
                  <li className="topic-row" key={`${row.kind}:${row.topic}`}>
                    <button
                      type="button"
                      className="link topic-name"
                      onClick={() => navigate(`/feed?q=${encodeURIComponent(row.topic)}&window=`)}
                    >
                      <span
                        className="swatch"
                        style={{ background: row.kind === "hashtag" ? SERIES[0] : SERIES[2] }}
                      />
                      {row.label}
                    </button>
                    <span className="tabular">
                      {compact(row.current_count)}
                      <span className={row.delta > 0 ? "delta up" : row.delta < 0 ? "delta down" : "delta flat"}>
                        {row.delta >= 0 ? "+" : ""}
                        {row.delta}
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p className="muted">
              {data ? "No topics in this window yet." : "Loading topics…"}
            </p>
          )}
        </div>
      )}

      {tab === "velocity" && (
        <div className="stack-sm">
          <article className="panel">
            <h3>Engagement gained over time</h3>
            <p className="muted">
              Likes, reposts and views added per bucket across every post we were
              already watching — not the totals those posts carry.
            </p>
            {series.length ? (
              <div className="chart-box">
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
              <p className="muted chart-empty">
                No metric deltas yet — velocity needs at least two snapshots of the
                same post inside the window.
              </p>
            )}
          </article>
          {results.map((tweet) => (
            <TweetCard key={tweet.id} tweet={tweet} />
          ))}
          {data && !results.length && (
            <p className="muted">Nothing gained engagement in this window.</p>
          )}
        </div>
      )}

      {tab === "narratives" && (
        <div className="stack-sm">
          <p className="muted">
            Near-identical posts from different accounts, within a propagation window
            of each other.
          </p>
          {results
            .filter((item) => item?.first && item?.follower)
            .map((item, index) => (
              <article
                className="panel narrative"
                key={`${item.first.tweet_id}-${item.follower.tweet_id}`}
              >
                <p className="eyebrow">
                  Narrative {index + 1} · {(item.similarity * 100).toFixed(0)}% similar
                </p>
                <div className="pair-grid">
                  {[
                    ["posted first", item.first],
                    ["followed", item.follower],
                  ].map(([role, side]) => (
                    <p key={role}>
                      <a
                        href={`https://x.com/${side.account}/status/${side.tweet_id}`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        <strong>@{side.account}</strong>
                      </a>
                      <br />
                      {role} · <span title={absoluteTime(side.created_at)}>
                        {relativeTime(side.created_at)}
                      </span>
                    </p>
                  ))}
                </div>
              </article>
            ))}
          {data && !results.length && (
            <p className="muted">No similar claim propagation detected yet.</p>
          )}
        </div>
      )}
    </section>
  );
}
