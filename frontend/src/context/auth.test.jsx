import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { AuthProvider, useAuth } from "./auth";
import { api } from "@/lib/api";

// ingestionEnabled is tri-state: null means unknown (still loading, or the
// config call failed). Only an explicit false may read as "collection paused",
// so an unknown answer must never collapse to false.

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual("@/lib/api");
  return { ...actual, api: vi.fn(), hasSession: () => false };
});

function Probe() {
  const { ingestionEnabled } = useAuth();
  return <output data-testid="ingestion">{String(ingestionEnabled)}</output>;
}

beforeEach(() => {
  api.mockReset();
});

it("starts unknown, not paused, while /auth/config/ is in flight", () => {
  api.mockImplementation(() => new Promise(() => {}));
  render(<AuthProvider><Probe /></AuthProvider>);
  expect(screen.getByTestId("ingestion")).toHaveTextContent("null");
});

it("stays unknown when /auth/config/ fails", async () => {
  let reject;
  api.mockImplementation(() => new Promise((_, r) => { reject = r; }));
  render(<AuthProvider><Probe /></AuthProvider>);
  await waitFor(() => expect(api).toHaveBeenCalledWith("/auth/config/"));
  reject(new Error("boom"));
  // Let the rejection settle, then confirm it did not become false.
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(screen.getByTestId("ingestion")).toHaveTextContent("null");
});

it("reports the server's answer once /auth/config/ succeeds", async () => {
  api.mockResolvedValue({ ingestion_enabled: false });
  render(<AuthProvider><Probe /></AuthProvider>);
  await waitFor(() => expect(screen.getByTestId("ingestion")).toHaveTextContent("false"));
});
