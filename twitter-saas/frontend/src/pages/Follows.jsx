import { useEffect, useState } from "react";
import { api } from "../api";
import InfiniteSentinel from "../InfiniteSentinel";
import TweetCard from "../TweetCard";

// Follows section: manage followed handles (following any handle auto-tracks it
// and enqueues an initial fetch) and view a followed account's timeline.
export default function Follows() {
  const [follows, setFollows] = useState([]);
  const [handle, setHandle] = useState("");
  const [selected, setSelected] = useState(null);
  const [tweets, setTweets] = useState([]);
  const [next, setNext] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function loadFollows() {
    try {
      setFollows(await api("/follows/"));
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => {
    loadFollows();
  }, []);

  async function addFollow(e) {
    e.preventDefault();
    setError("");
    try {
      await api("/follows/", { method: "POST", body: { handle: handle.replace(/^@/, "") } });
      setHandle("");
      loadFollows();
    } catch (e) {
      setError(e.message);
    }
  }

  async function unfollow(h) {
    try {
      await api("/follows/", { method: "DELETE", body: { handle: h } });
      if (selected === h) {
        setSelected(null);
        setTweets([]);
        setNext(null);
      }
      loadFollows();
    } catch (e) {
      setError(e.message);
    }
  }

  async function openTimeline(h, url) {
    if (loading) return;
    if (!url) {
      setSelected(h);
      setTweets([]);
      setNext(null);
    }
    setLoading(true);
    setError("");
    try {
      const path = url ? url.replace(/^.*\/api/, "") : `/accounts/${h}/tweets/`;
      const data = await api(path);
      setTweets((prev) => (url ? [...prev, ...data.results] : data.results));
      setNext(data.next);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="follows">
      <h2>Follows</h2>

      <form className="follow-form" onSubmit={addFollow}>
        <input
          placeholder="handle, e.g. elonmusk"
          value={handle}
          onChange={(e) => setHandle(e.target.value)}
        />
        <button type="submit" disabled={!handle.trim()}>
          Follow
        </button>
      </form>
      {error && <p className="error">{error}</p>}

      <div className="split">
        <ul className="follow-list">
          {follows.map((f) => (
            <li key={f.id} className={selected === f.handle ? "active" : ""}>
              <button className="link" onClick={() => openTimeline(f.handle)}>
                @{f.handle}
              </button>
              <button className="link small" onClick={() => unfollow(f.handle)}>
                unfollow
              </button>
            </li>
          ))}
          {follows.length === 0 && <li className="muted">Not following anyone yet.</li>}
        </ul>

        <div className="timeline">
          {selected && <h3>@{selected}</h3>}
          {tweets.map((t) => (
            <TweetCard key={t.id} tweet={t} />
          ))}
          <InfiniteSentinel
            next={next}
            loading={loading}
            onLoad={(url) => selected && openTimeline(selected, url)}
          />
          {selected && !loading && tweets.length === 0 && (
            <p className="muted">No tweets yet — the fetch may still be running.</p>
          )}
        </div>
      </div>
    </section>
  );
}
