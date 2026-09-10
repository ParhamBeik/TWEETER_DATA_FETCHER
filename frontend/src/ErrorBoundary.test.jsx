import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, Link } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ErrorBoundary from "./ErrorBoundary";

function Explode() {
  throw new Error("chart received a shape it did not expect");
}

beforeEach(() => {
  // React logs the caught error itself, and the boundary logs it again on
  // purpose. Silenced so a passing run is not a wall of red.
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ErrorBoundary", () => {
  it("shows a readable panel instead of a blank page", () => {
    render(
      <MemoryRouter>
        <ErrorBoundary>
          <Explode />
        </ErrorBoundary>
      </MemoryRouter>,
    );

    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("This screen stopped rendering")).toBeInTheDocument();
  });

  it("reports the underlying message rather than swallowing it", () => {
    render(
      <MemoryRouter>
        <ErrorBoundary>
          <Explode />
        </ErrorBoundary>
      </MemoryRouter>,
    );

    expect(
      screen.getByText("chart received a shape it did not expect"),
    ).toBeInTheDocument();
  });

  it("offers a way out that does not need the keyboard shortcut for reload", () => {
    render(
      <MemoryRouter>
        <ErrorBoundary>
          <Explode />
        </ErrorBoundary>
      </MemoryRouter>,
    );

    expect(screen.getByRole("button", { name: "Reload the console" })).toBeInTheDocument();
  });

  it("clears itself when the operator navigates to another section", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/broken"]}>
        <ErrorBoundary>
          <Routes>
            <Route path="/broken" element={<Explode />} />
            <Route path="/feed" element={<p>the feed</p>} />
          </Routes>
        </ErrorBoundary>
        {/* Outside the boundary, so it survives the failure the way the real
            sidebar does. */}
        <Link to="/feed">Feed</Link>
      </MemoryRouter>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Feed" }));

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("the feed")).toBeInTheDocument();
  });

  it("renders its children untouched when nothing throws", () => {
    render(
      <MemoryRouter>
        <ErrorBoundary>
          <p>a working screen</p>
        </ErrorBoundary>
      </MemoryRouter>,
    );

    expect(screen.getByText("a working screen")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
