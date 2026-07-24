import { useEffect, useState } from "react";
import { api } from "../api";
import TweetCard from "../TweetCard";

export default function Feed() {
  const [tweets, setTweets] = useState([]);
  const [next, setNext] = useState(null);
  const [error, setError] = useState("");

  async function load(url) {
    try {
      // DRF cursor pagination returns `next` as an absolute URL; strip
      // everything up to and including /api so the api() wrapper re-adds it.
      const path = url ? url.replace(/^.*\/api/, "") : "/feed/";
      const data = await api(path);
      setTweets((prev) => (url ? [...prev, ...data.results] : data.results));
      setNext(data.next);
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => {
    load();
  }, []);

  return (
    <section>
      <h2>Feed</h2>
      {error && <p className="error">{error}</p>}
      {tweets.map((t) => (
        <TweetCard key={t.id} tweet={t} />
      ))}
      {next && <button onClick={() => load(next)}>Load more</button>}
    </section>
  );
}
