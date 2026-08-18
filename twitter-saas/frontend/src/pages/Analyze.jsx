import { useEffect, useState } from "react";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api";
import { AXIS_PROPS, BAR_RADIUS_X, SERIES, TOOLTIP_STYLE } from "../charts";
import { Link } from "react-router-dom";
import TweetCard from "../TweetCard";

const tabs = ["velocity", "topics", "narratives"];

export default function Analyze() {
  const [tab, setTab] = useState("velocity");
  const [data, setData] = useState([]);
  const [error, setError] = useState("");

  useEffect(() => {
    // Each tab returns a different row shape. Without clearing first, the new
    // tab renders the previous tab's rows for one frame -- and `narratives`
    // reading `item.first.tweet_id` off a tweet threw, unmounting the whole app.
    setData([]);
    let current = true;
    api(`/analytics/${tab}/`)
      .then((result) => {
        if (!current) return;
        setData(result.results || []);
        setError("");
      })
      .catch((err) => current && setError(err.message));
    return () => {
      current = false;
    };
  }, [tab]);

  return (
    <section className="space-y-6">
      <header>
        <p className="eyebrow">Analyze</p>
        <h2 className="page-title">What is accelerating in the archive?</h2>
        <Link className="text-sm font-semibold text-accent" to="/searches">Open saved searches →</Link>
      </header>
      <div className="tabs">
        {tabs.map((name) => (
          <button className={tab === name ? "tab active" : "tab"} key={name} onClick={() => setTab(name)}>
            {name}
          </button>
        ))}
      </div>
      {error && <p className="error">{error}</p>}
      {tab === "velocity" && (
        <div className="space-y-4">
          <p className="muted">Tweets ranked by engagement gained during the selected metric window.</p>
          {data.map((tweet) => <TweetCard key={tweet.id} tweet={tweet} />)}
          {!data.length && !error && <p className="muted">No metric deltas yet.</p>}
        </div>
      )}
      {tab === "topics" && (
        <article className="panel h-[420px]">
          {data.length ? (
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data.slice(0, 20)} layout="vertical" margin={{ left: 12, right: 16 }}>
                <XAxis type="number" allowDecimals={false} {...AXIS_PROPS} />
                <YAxis type="category" dataKey="topic" width={120} {...AXIS_PROPS} />
                <Tooltip
                  cursor={{ fill: "rgba(255,255,255,0.04)" }}
                  contentStyle={TOOLTIP_STYLE}
                  formatter={(value) => [value, "tweets this window"]}
                />
                <Bar dataKey="current_count" fill={SERIES[0]} radius={BAR_RADIUS_X} maxBarSize={22} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <p className="muted">No topics in this window yet.</p>
          )}
        </article>
      )}
      {tab === "narratives" && (
        <div className="space-y-3">
          {data.filter((item) => item?.first && item?.follower).map((item, index) => (
            <article className="panel" key={`${item.first.tweet_id}-${item.follower.tweet_id}`}>
              <p className="eyebrow">Narrative {index + 1} · {(item.similarity * 100).toFixed(0)}% similar</p>
              <div className="grid gap-3 sm:grid-cols-2">
                <p><strong>@{item.first.account}</strong><br />posted first · {new Date(item.first.created_at).toLocaleString()}</p>
                <p><strong>@{item.follower.account}</strong><br />followed · {new Date(item.follower.created_at).toLocaleString()}</p>
              </div>
            </article>
          ))}
          {!data.length && !error && <p className="muted">No similar claim propagation detected yet.</p>}
        </div>
      )}
    </section>
  );
}
