// Thin fetch wrapper: token auth + JSON. Token stored in localStorage.
const TOKEN_KEY = "tsaas_token";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export async function api(path, { method = "GET", body } = {}) {
  const headers = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Token ${token}`;
  let res;
  try {
    res = await fetch(`/api${path}`, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    // fetch() rejects only on network failure, where there is no status text to
    // report. Without this every offline//DNS blip surfaced as the browser's
    // opaque "Failed to fetch" in the middle of the UI.
    throw new Error("Network error — the API is unreachable.");
  }
  // A token that the server no longer accepts otherwise leaves every page
  // throwing "Invalid token." forever with no way back to the login form.
  if (res.status === 401 && token) {
    setToken(null);
    if (typeof window !== "undefined") window.location.assign("/login");
    throw new Error("Session expired — please sign in again.");
  }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  if (res.status === 204) return null;
  return res.json();
}
