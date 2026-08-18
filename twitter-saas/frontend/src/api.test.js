import { describe, expect, it, vi } from "vitest";
import { api, getToken, setToken } from "./api";

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

describe("token storage", () => {
  it("round-trips a token", () => {
    setToken("abc123");
    expect(getToken()).toBe("abc123");
  });

  it("clears the token when set to a falsy value", () => {
    setToken("abc123");
    setToken(null);
    expect(getToken()).toBeNull();
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

  it("omits the Authorization header when no token is stored", async () => {
    const fetchSpy = mockFetch(jsonResponse({}));
    await api("/feed/");
    expect(fetchSpy.mock.calls[0][1].headers.Authorization).toBeUndefined();
  });

  it("attaches the stored token as a DRF Token header", async () => {
    setToken("abc123");
    const fetchSpy = mockFetch(jsonResponse({}));
    await api("/feed/");
    expect(fetchSpy.mock.calls[0][1].headers.Authorization).toBe("Token abc123");
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
    mockFetch(jsonResponse({ detail: "Invalid token." }, { ok: false, status: 401 }));
    await expect(api("/feed/")).rejects.toThrow("Invalid token.");
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
});
