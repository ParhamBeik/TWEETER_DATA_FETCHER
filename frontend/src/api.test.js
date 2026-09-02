import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  api,
  authorizedFetch,
  clearTokens,
  getAccessToken,
  getRefreshToken,
  hasSession,
  setTokens,
} from "./api";

// Unit tests: api.js is pure request-shaping logic over `fetch` with no UI, so a
// stubbed global is the right seam -- no server or DOM needed.

function mockFetch(response) {
  const spy = vi.fn().mockResolvedValue(response);
  vi.stubGlobal("fetch", spy);
  return spy;
}

const jsonResponse = (body, { status = 200, ok = true } = {}) => ({
  ok,
  status,
  statusText: "",
  json: async () => body,
});

beforeEach(() => {
  clearTokens();
  localStorage.clear();
  // Stubs persist across tests, so reset them here rather than leaking one
  // test's fake navigator into the next one's refresh path.
  vi.unstubAllGlobals();
  vi.stubGlobal("location", { assign: vi.fn(), pathname: "/feed" });
});

describe("token storage", () => {
  it("keeps the access token out of localStorage", () => {
    setTokens({ access: "access-1", refresh: "refresh-1" });
    expect(getAccessToken()).toBe("access-1");
    expect(localStorage.getItem("tsaas_refresh")).toBe("refresh-1");
    // The token sent on every request must not be readable from storage.
    expect(String(localStorage.getItem("tsaas_refresh"))).not.toContain("access-1");
  });

  it("clears both halves of the session", () => {
    setTokens({ access: "access-1", refresh: "refresh-1" });
    clearTokens();
    expect(getAccessToken()).toBeNull();
    expect(getRefreshToken()).toBeNull();
  });

  it("reports a restorable session from the refresh token alone", () => {
    // This is the page-reload case: memory is empty, storage is not.
    localStorage.setItem("tsaas_refresh", "refresh-1");
    expect(hasSession()).toBe(true);
  });
});

describe("api()", () => {
  it("prefixes the path with /api and defaults to GET", async () => {
    const fetchSpy = mockFetch(jsonResponse({ ok: true }));
    await api("/feed/");
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe("/api/feed/");
    expect(init.method).toBe("GET");
    expect(init.body).toBeUndefined();
  });

  it("omits the Authorization header when there is no session", async () => {
    const fetchSpy = mockFetch(jsonResponse({}));
    await api("/feed/");
    expect(fetchSpy.mock.calls[0][1].headers.Authorization).toBeUndefined();
  });

  it("sends the access token as a Bearer header", async () => {
    setTokens({ access: "access-1", refresh: "refresh-1" });
    const fetchSpy = mockFetch(jsonResponse({}));
    await api("/feed/");
    expect(fetchSpy.mock.calls[0][1].headers.Authorization).toBe("Bearer access-1");
  });

  it("serializes a body and sends the requested method", async () => {
    const fetchSpy = mockFetch(jsonResponse({}));
    await api("/searches/", { method: "POST", body: { query: "ai" } });
    const [, init] = fetchSpy.mock.calls[0];
    expect(init.method).toBe("POST");
    expect(init.body).toBe('{"query":"ai"}');
    expect(init.headers["Content-Type"]).toBe("application/json");
  });

  it("returns the parsed JSON body on success", async () => {
    mockFetch(jsonResponse({ results: [1, 2] }));
    await expect(api("/feed/")).resolves.toEqual({ results: [1, 2] });
  });

  it("returns null for 204 No Content without parsing a body", async () => {
    mockFetch({ ok: true, status: 204, json: async () => {
      throw new Error("204 has no body to parse");
    } });
    await expect(api("/accounts/1/")).resolves.toBeNull();
  });

  it("raises the API's `detail` message on an error response", async () => {
    mockFetch(jsonResponse({ detail: "Not found." }, { ok: false, status: 404 }));
    await expect(api("/feed/")).rejects.toThrow("Not found.");
  });

  it("carries per-field errors so a form can point at the bad input", async () => {
    mockFetch(
      jsonResponse(
        { detail: "That username is taken.", errors: { username: ["That username is taken."] } },
        { ok: false, status: 400 },
      ),
    );
    await expect(api("/auth/register/", { method: "POST" })).rejects.toMatchObject({
      fieldErrors: { username: ["That username is taken."] },
      status: 400,
    });
  });

  it("falls back to statusText when the error body is not JSON", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false,
      status: 502,
      statusText: "Bad Gateway",
      json: async () => {
        throw new SyntaxError("Unexpected token < in JSON");
      },
    }));
    await expect(api("/feed/")).rejects.toThrow("Bad Gateway");
  });

  it("reports a network failure in words rather than the browser's opaque error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(api("/feed/")).rejects.toThrow("Network error — the API is unreachable.");
  });
});

describe("expired access token", () => {
  it("refreshes and retries once, transparently", async () => {
    setTokens({ access: "stale", refresh: "refresh-1" });
    const fetchSpy = vi.fn()
      .mockResolvedValueOnce(jsonResponse({}, { ok: false, status: 401 }))
      .mockResolvedValueOnce(jsonResponse({ access: "fresh", refresh: "refresh-2" }))
      .mockResolvedValueOnce(jsonResponse({ results: [] }));
    vi.stubGlobal("fetch", fetchSpy);

    await expect(api("/feed/")).resolves.toEqual({ results: [] });

    expect(fetchSpy.mock.calls[1][0]).toBe("/api/auth/refresh/");
    // The retry carries the new token, and the rotated refresh token is kept.
    expect(fetchSpy.mock.calls[2][1].headers.Authorization).toBe("Bearer fresh");
    expect(getRefreshToken()).toBe("refresh-2");
  });

  it("ends the session when the refresh is rejected too", async () => {
    setTokens({ access: "stale", refresh: "dead" });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({}, { ok: false, status: 401 })));

    await expect(api("/feed/")).rejects.toThrow("Session expired — please sign in again.");
    expect(getRefreshToken()).toBeNull();
    expect(location.assign).toHaveBeenCalledWith("/login");
  });

  it("refreshes only once for several requests that fail together", async () => {
    // Rotation blacklists the spent refresh token, so a second concurrent
    // refresh would present a dead token and log the user out.
    setTokens({ access: "stale", refresh: "refresh-1" });
    const fetchSpy = vi.fn(async (url) => {
      if (String(url).includes("/auth/refresh/")) {
        return jsonResponse({ access: "fresh", refresh: "refresh-2" });
      }
      return getAccessToken() === "fresh"
        ? jsonResponse({ ok: true })
        : jsonResponse({}, { ok: false, status: 401 });
    });
    vi.stubGlobal("fetch", fetchSpy);

    await Promise.all([api("/feed/"), api("/accounts/"), api("/searches/")]);

    const refreshCalls = fetchSpy.mock.calls.filter(([url]) => String(url).includes("/auth/refresh/"));
    expect(refreshCalls).toHaveLength(1);
  });

  it("serialises the refresh across tabs and re-reads the rotated token", async () => {
    // The lock is what stops a second tab from presenting a token the first tab
    // has already spent -- the 401 that followed used to end both sessions.
    localStorage.setItem("tsaas_refresh", "refresh-1");
    const order = [];
    vi.stubGlobal("navigator", {
      locks: {
        request: async (name, fn) => {
          order.push(`lock:${name}`);
          return fn();
        },
      },
    });
    const fetchSpy = vi.fn(async (url, init) => {
      if (String(url).includes("/auth/refresh/")) {
        order.push(`refresh:${JSON.parse(init.body).refresh}`);
        return jsonResponse({ access: "fresh", refresh: "refresh-2" });
      }
      return jsonResponse({ results: [] });
    });
    vi.stubGlobal("fetch", fetchSpy);

    await api("/feed/");

    expect(order).toEqual(["lock:tsaas-refresh", "refresh:refresh-1"]);
    expect(getRefreshToken()).toBe("refresh-2");
  });

  it("refreshes before the first request of a cold page load", async () => {
    // After a reload the access token is gone (memory only) but the refresh
    // token is not. Sending regardless spent a request that could only 401,
    // and every cold load logged a console error before recovering.
    localStorage.setItem("tsaas_refresh", "refresh-1");
    const fetchSpy = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ access: "fresh", refresh: "refresh-2" }))
      .mockResolvedValueOnce(jsonResponse({ results: [] }));
    vi.stubGlobal("fetch", fetchSpy);

    await expect(api("/feed/")).resolves.toEqual({ results: [] });

    expect(fetchSpy).toHaveBeenCalledTimes(2);
    expect(fetchSpy.mock.calls[0][0]).toBe("/api/auth/refresh/");
    expect(fetchSpy.mock.calls[1][1].headers.Authorization).toBe("Bearer fresh");
  });

  it("still sends unauthenticated when there is nothing to refresh with", async () => {
    const fetchSpy = mockFetch(jsonResponse({ results: [] }));

    await api("/feed/");

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(fetchSpy.mock.calls[0][1].headers.Authorization).toBeUndefined();
  });

  it("does not retry when the caller opted out", async () => {
    // A failed login is about these credentials, not an expired session.
    setTokens({ access: "stale", refresh: "refresh-1" });
    const fetchSpy = mockFetch(jsonResponse({}, { ok: false, status: 401 }));

    await expect(api("/auth/login/", { method: "POST", retry: false })).rejects.toThrow();

    expect(fetchSpy.mock.calls.every(([url]) => !String(url).includes("/auth/refresh/"))).toBe(true);
  });
});

describe("authorizedFetch()", () => {
  it("attaches the bearer token for non-JSON downloads", async () => {
    setTokens({ access: "access-1", refresh: "refresh-1" });
    const fetchSpy = mockFetch({ ok: true, status: 200 });

    await authorizedFetch("/api/export/?format=csv");

    expect(fetchSpy.mock.calls[0][1].headers.Authorization).toBe("Bearer access-1");
  });

  it("refreshes and retries a download whose token aged out", async () => {
    setTokens({ access: "stale", refresh: "refresh-1" });
    const fetchSpy = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 401 })
      .mockResolvedValueOnce(jsonResponse({ access: "fresh", refresh: "refresh-2" }))
      .mockResolvedValueOnce({ ok: true, status: 200 });
    vi.stubGlobal("fetch", fetchSpy);

    const res = await authorizedFetch("/api/export/?format=csv");

    expect(res.ok).toBe(true);
    expect(fetchSpy.mock.calls[2][1].headers.Authorization).toBe("Bearer fresh");
  });
});
