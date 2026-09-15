import { useEffect } from "react";
import { Link, useNavigate } from "react-router-dom";

/**
 * Old bookmarks land here instead of silently jumping elsewhere. The operator
 * sees where the page went and can follow immediately or wait for the redirect.
 */
export default function LegacyRedirect({ to, from, target, delayMs = 4000 }) {
  const navigate = useNavigate();

  useEffect(() => {
    const timer = window.setTimeout(() => navigate(to, { replace: true }), delayMs);
    return () => window.clearTimeout(timer);
  }, [to, navigate, delayMs]);

  return (
    <div
      role="status"
      className="mx-auto max-w-lg rounded-sm border border-line bg-paper px-4 py-5"
    >
      <p className="text-sm text-fg-muted">
        <span className="font-mono text-fg">{from}</span> is now{" "}
        <Link to={to} className="font-medium text-accent hover:underline">
          {target}
        </Link>
        . Redirecting in a few seconds, or follow the link now.
      </p>
    </div>
  );
}
