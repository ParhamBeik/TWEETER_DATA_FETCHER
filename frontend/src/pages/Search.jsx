import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import InfiniteSentinel from "../InfiniteSentinel";
import RunStatus from "../RunStatus";
import TweetCard from "../TweetCard";

// Search section: submit a query (enqueues a fetch job) and browse results
// per Top/Latest product. Products are separate Search rows server-side.
export default function Search() {
  const [product, setProduct] = useState("Top");
  const [searches, setSearches] = useState([]);
  const [selected, setSelected] = useState(null);
  const [results, setResults] = useState([]);
  const [next, setNext] = useState(null);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");
  const [name, setName] = useState("");
  const [depth, setDepth] = useState(1);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const activeSearchId = useRef(null);

  async function loadSearches(p = product) {
    try {
      const data = await api(`/searches/?product=${p}`);
      setSearches(data.results || data);
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => {
    loadSearches(product);
    setSelected(null);
    setResults([]);
    setNext(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [product]);

  async function openResults(search, url) {
    // Only de-duplicate the infinite-scroll append; guarding on `loading` outright
    // made clicking a second search while the first loaded do nothing at all.
    if (url && loading) return;
    if (!url) {
      setSelected(search);
      setResults([]);
      setNext(null);
    }
    activeSearchId.current = search.id;
    setLoading(true);
    setError("");
    try {
      const path = url
        ? url.replace(/^.*\/api/, "")
        : `/searches/${search.id}/results/`;
      const data = await api(path);
      // A slower response for the previously selected search must not paint here.
      if (activeSearchId.current !== search.id) return;
      setResults((prev) => (url ? [...prev, ...(data.results || [])] : data.results || []));
      setNext(data.next || null);
    } catch (e) {
      if (activeSearchId.current === search.id) setError(e.message);
    } finally {
      if (activeSearchId.current === search.id) setLoading(false);
    }
  }

  async function submit(e) {
    e.preventDefault();
    setError("");
    setStatus("");
    try {
      await api("/searches/", {
        method: "POST",
        body: { raw_query: query, name: name || query.slice(0, 60), product, pagination_depth: depth },
      });
      setStatus("Search queued. Results appear once the job runs.");
      setQuery("");
      setName("");
      loadSearches(product);
    } catch (e) {
      setError(e.message);
    }
  }

  async function refresh(search) {
    try {
      await api(`/searches/${search.id}/refresh/`, { method: "POST" });
      setStatus(`Re-queued "${search.slug}".`);
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <section className="search">
      <header>
        <p className="eyebrow">Searches</p>
        <h2 className="page-title">Saved archive queries</h2>
      </header>
      <RunStatus />

      <div className="tabs">
        {["Top", "Latest"].map((p) => (
          <button
            key={p}
            type="button"
            aria-pressed={p === product}
            className={p === product ? "tab active" : "tab"}
            onClick={() => setProduct(p)}
          >
            {p}
          </button>
        ))}
      </div>

      <form className="search-form" onSubmit={submit}>
        <input
          aria-label="Search name"
          placeholder="name (optional)"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <select
          aria-label="Pagination depth"
          value={depth}
          onChange={(e) => setDepth(Number(e.target.value))}
        >
          {[1, 2, 3].map((value) => <option key={value} value={value}>depth {value}</option>)}
        </select>
        <input
          aria-label="Raw query"
          placeholder="raw query, e.g. (Iran OR Gold) lang:en min_faves:1000"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button type="submit" disabled={!query.trim()}>
          Run {product}
        </button>
      </form>

      {status && <p className="status">{status}</p>}
      {error && <p className="error">{error}</p>}

      <div className="split">
        <ul className="search-list">
          {searches.map((s) => (
            <li key={s.id} className={selected?.id === s.id ? "active" : ""}>
              <button className="link" onClick={() => openResults(s)}>
                {s.name || s.slug} <small className="muted">· depth {s.pagination_depth}</small>
              </button>
              <button className="link small" onClick={() => refresh(s)}>
                refresh
              </button>
            </li>
          ))}
          {searches.length === 0 && <li className="muted">No {product} searches yet.</li>}
        </ul>

        <div className="search-results">
          {selected && <h3>{selected.name || selected.slug}</h3>}
          {results.map((t) => (
            <TweetCard key={t.id} tweet={t} />
          ))}
          <InfiniteSentinel
            next={next}
            loading={loading}
            onLoad={(url) => selected && openResults(selected, url)}
          />
          {selected && !loading && results.length === 0 && (
            <p className="muted">No results yet — the job may still be running.</p>
          )}
        </div>
      </div>
    </section>
  );
}
