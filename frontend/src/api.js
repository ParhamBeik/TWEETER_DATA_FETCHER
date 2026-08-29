// Thin fetch wrapper: JWT auth + JSON.
//
// The access token is short-lived (30 min) and kept in memory; only the refresh
// token is persisted. A refresh survives a page reload, but a token sitting in
// localStorage is readable by anything that manages to run script on the page,
// so the one used on every request is not left there.
//
// A 401 no longer means "log out". It usually means the access token aged out
// mid-session, so the request is refreshed and retried once, transparently, and
// the user is only sent back to the login form if the refresh itself fails.
const REFRESH_KEY = "tsaas_refresh";

let accessToken = null;
// Concurrent 401s must not each start their own refresh: the first one rotates
// the refresh token and blacklists the old one, so the rest would refresh with
// a dead token and log the user out. They all await the same promise.
let refreshInFlight = null;

export function getRefreshToken() {
  return localStorage.getItem(REFRESH_KEY);
}

export function getAccessToken() {
  return accessToken;
}

export function setTokens({ access, refresh } = {}) {
  accessToken = access || null;
  if (refresh) localStorage.setItem(REFRESH_KEY, refresh);
}

export function clearTokens() {
  accessToken = null;
  refreshInFlight = null;
  localStorage.removeItem(REFRESH_KEY);
}

// True when there is any chance of restoring a session -- either a live access
// token or a refresh token to trade in. Drives the initial authed state, so a
// reload does not bounce a signed-in user to /login before the first refresh.
export function hasSession() {
  return Boolean(accessToken || getRefreshToken());
}

async function requestRefresh() {
  const refresh = getRefreshToken();
  if (!refresh) return false;
  try {
    const res = await fetch("/api/auth/refresh/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh }),
    });
    if (!res.ok) return false;
    const data = await res.json();
    setTokens({ access: data.access, refresh: data.refresh });
    return true;
  } catch {
    return false;
  }
}

export function refreshSession() {
  if (!refreshInFlight) {
    refreshInFlight = requestRefresh().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

function endSession() {
  clearTokens();
  if (typeof window !== "undefined" && window.location.pathname !== "/login") {
    window.location.assign("/login");
  }
}

async function send(path, { method, body }) {
  const headers = { "Content-Type": "application/json" };
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  try {
    return await fetch(`/api${path}`, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    // fetch() rejects only on network failure, where there is no status text to
    // report. Without this every offline/DNS blip surfaced as the browser's
    // opaque "Failed to fetch" in the middle of the UI.
    throw new Error("Network error — the API is unreachable.");
  }
}

// For responses that are not JSON -- the CSV/JSONL export downloads a blob, so
// it cannot go through api(). Same refresh-and-retry-once rule, so a long
// browsing session does not fail its first export on an aged access token.
export async function authorizedFetch(url, init = {}) {
  const withAuth = () => ({
    ...init,
    headers: { ...(init.headers || {}), ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}) },
  });
  if (!accessToken && getRefreshToken()) await refreshSession();
  let res = await fetch(url, withAuth());
  if (res.status === 401 && getRefreshToken() && (await refreshSession())) {
    res = await fetch(url, withAuth());
  }
  return res;
}

export async function api(path, { method = "GET", body, retry = true } = {}) {
  // A page load starts with no access token -- it is memory-only by design --
  // so sending straight away spends a request that is certain to 401 and logs a
  // console error on every cold load. Trade the refresh token in first; the
  // shared promise means several parallel calls still cause one refresh.
  if (!accessToken && retry && getRefreshToken()) await refreshSession();
  let res = await send(path, { method, body });

  if (res.status === 401 && retry && getRefreshToken()) {
    if (await refreshSession()) {
      res = await send(path, { method, body });
    }
  }

  if (res.status === 401) {
    // Either there was nothing to refresh with or the refresh was rejected.
    // Now it really is over.
    endSession();
    throw new Error("Session expired — please sign in again.");
  }

  if (!res.ok) {
    const payload = await res.json().catch(() => ({}));
    const error = new Error(payload.detail || res.statusText);
    // Per-field messages from the register endpoint, so a form can point at the
    // input that was wrong instead of showing one banner.
    error.fieldErrors = payload.errors || null;
    error.status = res.status;
    throw error;
  }
  if (res.status === 204) return null;
  return res.json();
}
