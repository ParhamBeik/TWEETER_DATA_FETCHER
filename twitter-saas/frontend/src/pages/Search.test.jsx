import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Search from "./Search";
import { api } from "../api";

// Component tests: saved-search creation and result browsing are pure UI state
// over the API, so the network boundary is the only thing mocked.

vi.mock("../api", async () => {
  const actual = await vi.importActual("../api");
  return { ...actual, api: vi.fn() };
});
vi.mock("../RunStatus", () => ({ default: () => null }));

const saved = (id, over = {}) => ({ id, name: `search ${id}`, slug: `search-${id}`, pagination_depth: 1, ...over });

function routeApi({ searches = [], results = { results: [], next: null } } = {}) {
  api.mockImplementation(async (path) => {
    if (path.startsWith("/searches/?")) return { results: searches };
    if (path.includes("/results/")) return results;
    return {};
  });
}

beforeEach(() => api.mockReset());

describe("product tabs", () => {
  it("requests Top searches on mount", async () => {
    routeApi();
    render(<Search />);
    await waitFor(() => expect(api).toHaveBeenCalledWith("/searches/?product=Top"));
  });

  it("marks the active product tab as pressed", async () => {
    routeApi();
    render(<Search />);
    await waitFor(() => expect(api).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "Top" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Latest" })).toHaveAttribute("aria-pressed", "false");
  });

  it("reloads for the Latest product when its tab is clicked", async () => {
    const user = userEvent.setup();
    routeApi();
    render(<Search />);
    await waitFor(() => expect(api).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: "Latest" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/searches/?product=Latest"));
  });

  it("names the product in the empty state", async () => {
    routeApi();
    render(<Search />);
    expect(await screen.findByText("No Top searches yet.")).toBeInTheDocument();
  });
});

describe("creating a search", () => {
  it("labels every control in the create form", async () => {
    routeApi();
    render(<Search />);
    await waitFor(() => expect(api).toHaveBeenCalled());
    ["Search name", "Pagination depth", "Raw query"].forEach((name) =>
      expect(screen.getByLabelText(name)).toBeInTheDocument());
  });

  it("keeps submit disabled until a raw query is entered", async () => {
    const user = userEvent.setup();
    routeApi();
    render(<Search />);
    await waitFor(() => expect(api).toHaveBeenCalled());
    const submit = screen.getByRole("button", { name: "Run Top" });
    expect(submit).toBeDisabled();
    await user.type(screen.getByLabelText("Raw query"), "lang:en");
    expect(submit).toBeEnabled();
  });

  it("posts the query, product and depth", async () => {
    const user = userEvent.setup();
    routeApi();
    render(<Search />);
    await waitFor(() => expect(api).toHaveBeenCalled());
    await user.type(screen.getByLabelText("Search name"), "Iran gold");
    await user.selectOptions(screen.getByLabelText("Pagination depth"), "3");
    await user.type(screen.getByLabelText("Raw query"), "(Iran OR Gold) lang:en");
    await user.click(screen.getByRole("button", { name: "Run Top" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/searches/", {
      method: "POST",
      body: {
        raw_query: "(Iran OR Gold) lang:en",
        name: "Iran gold",
        product: "Top",
        pagination_depth: 3,
      },
    }));
  });

  it("derives a name from the query when none is given", async () => {
    const user = userEvent.setup();
    routeApi();
    render(<Search />);
    await waitFor(() => expect(api).toHaveBeenCalled());
    await user.type(screen.getByLabelText("Raw query"), "lang:en");
    await user.click(screen.getByRole("button", { name: "Run Top" }));
    await waitFor(() => expect(api.mock.calls.find(([p]) => p === "/searches/")[1].body.name).toBe("lang:en"));
  });

  it("confirms the queued job and clears the query field", async () => {
    const user = userEvent.setup();
    routeApi();
    render(<Search />);
    await waitFor(() => expect(api).toHaveBeenCalled());
    const input = screen.getByLabelText("Raw query");
    await user.type(input, "lang:en");
    await user.click(screen.getByRole("button", { name: "Run Top" }));
    expect(await screen.findByText(/Search queued/)).toBeInTheDocument();
    expect(input).toHaveValue("");
  });

  it("reports a rejected query", async () => {
    const user = userEvent.setup();
    api.mockImplementation(async (path, opts) => {
      if (opts?.method === "POST") throw new Error("Invalid search operator.");
      return { results: [] };
    });
    render(<Search />);
    await waitFor(() => expect(api).toHaveBeenCalled());
    await user.type(screen.getByLabelText("Raw query"), "bad:::");
    await user.click(screen.getByRole("button", { name: "Run Top" }));
    expect(await screen.findByText("Invalid search operator.")).toBeInTheDocument();
  });
});

describe("browsing results", () => {
  it("lists saved searches with their depth", async () => {
    routeApi({ searches: [saved(1, { pagination_depth: 2 })] });
    render(<Search />);
    // Scoped to the list: "depth 2" is also an option in the create form's select.
    const entry = await screen.findByRole("button", { name: /search 1/ });
    expect(entry).toHaveTextContent("depth 2");
  });

  it("loads results when a saved search is opened", async () => {
    const user = userEvent.setup();
    routeApi({
      searches: [saved(1)],
      results: { results: [{ id: "t1", account: "a", text: "matched tweet" }], next: null },
    });
    render(<Search />);
    await user.click(await screen.findByRole("button", { name: /search 1/ }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/searches/1/results/"));
    expect(await screen.findByText("matched tweet")).toBeInTheDocument();
  });

  it("explains an empty result set rather than showing nothing", async () => {
    const user = userEvent.setup();
    routeApi({ searches: [saved(1)] });
    render(<Search />);
    await user.click(await screen.findByRole("button", { name: /search 1/ }));
    expect(await screen.findByText(/No results yet/)).toBeInTheDocument();
  });

  it("re-queues a saved search from its refresh action", async () => {
    const user = userEvent.setup();
    routeApi({ searches: [saved(1)] });
    render(<Search />);
    await screen.findByRole("button", { name: /search 1/ });
    await user.click(screen.getByRole("button", { name: "refresh" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/searches/1/refresh/", { method: "POST" }));
    expect(await screen.findByText(/Re-queued/)).toBeInTheDocument();
  });

  // Regression: `if (loading) return` blocked opening a second search while the
  // first was still loading, so the click appeared to do nothing.
  it("switches to another search while the first is still loading", async () => {
    const user = userEvent.setup();
    let releaseFirst;
    api.mockImplementation(async (path) => {
      if (path.startsWith("/searches/?")) return { results: [saved(1), saved(2)] };
      if (path === "/searches/1/results/") {
        return new Promise((resolve) => {
          releaseFirst = () => resolve({ results: [{ id: "t1", account: "a", text: "first result" }] });
        });
      }
      return { results: [{ id: "t2", account: "b", text: "second result" }], next: null };
    });
    render(<Search />);
    await user.click(await screen.findByRole("button", { name: /search 1/ }));
    await user.click(screen.getByRole("button", { name: /search 2/ }));
    expect(await screen.findByText("second result")).toBeInTheDocument();

    releaseFirst();
    await waitFor(() => expect(screen.queryByText("first result")).toBeNull());
  });
});
