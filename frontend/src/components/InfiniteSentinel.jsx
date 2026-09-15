import { useEffect, useRef } from "react";

export default function InfiniteSentinel({ next, loading, onLoad }) {
  const ref = useRef(null);

  useEffect(() => {
    if (!next || loading || !ref.current) return undefined;
    const observer = new IntersectionObserver(
      ([entry]) => entry.isIntersecting && onLoad(next),
      { rootMargin: "300px" },
    );
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, [next, loading, onLoad]);

  return (
    <div ref={ref} className="py-4 text-center text-xs text-fg-dim" role="status">
      {loading ? "Loading…" : ""}
    </div>
  );
}
