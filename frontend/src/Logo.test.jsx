import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import Logo, { Brand } from "./Logo";

// Unit tests for the mark: geometry and the accessible-name contract. The
// shell tests already prove it is wired into the sidebar; this file only
// asserts what the SVG itself guarantees.

describe("Logo", () => {
  it("exposes the product name when it stands alone", () => {
    render(<Logo />);
    expect(screen.getByRole("img", { name: "Signal Archive" })).toBeInTheDocument();
  });

  it("is silent next to the wordmark", () => {
    render(<Logo decorative />);
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("draws three archive strata", () => {
    const { container } = render(<Logo />);
    expect(container.querySelectorAll("rect")).toHaveLength(3);
  });
});

const ROUTER_FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true };

describe("Brand", () => {
  it("is a home link when given a destination", () => {
    render(
      <MemoryRouter future={ROUTER_FUTURE}>
        <Brand to="/feed" />
      </MemoryRouter>,
    );
    expect(screen.getByRole("link", { name: "Signal Archive" })).toHaveAttribute("href", "/feed");
  });

  it("is not a link on the signed-out pages", () => {
    render(<Brand />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("Signal Archive")).toBeInTheDocument();
  });
});
