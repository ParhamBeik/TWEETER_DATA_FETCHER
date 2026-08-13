import { useEffect, useState } from "react";
import { api } from "../api";
import TweetCard from "../TweetCard";

export default function Trending() {
  const [tweets, setTweets] = useState([]);
  const [error, setError] = useState("");

  useEffect(() => {
    api("/trending/")
      .then((data) => setTweets(Array.isArray(data) ? data : data.results || []))
      .catch((e) => setError(e.message));
  }, []);

  return (
    <section>
      <h2>Trending</h2>
      <p className="muted">Ranked by engagement change per hour over the last 24 hours.</p>
      {error && <p className="error">{error}</p>}
      {tweets.map((t) => (
        <TweetCard key={t.id} tweet={t} />
      ))}
      {!tweets.length && !error && <p className="muted">No metric deltas yet.</p>}
    </section>
  );
}
