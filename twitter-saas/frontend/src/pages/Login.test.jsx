import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Login from "./Login";
import { api, getToken } from "../api";

// Component tests: Login owns real interaction logic (mode toggle, submit
// guarding, token persistence), so it is driven through the DOM with the network
// boundary mocked -- the seam a user's actions actually cross.

vi.mock("../api", async () => {
  const actual = await vi.importActual("../api");
  return { ...actual, api: vi.fn() };
});

const navigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return { ...actual, useNavigate: () => navigate };
});

// v7 flags only silence the upgrade warnings; they do not change what is tested.
const ROUTER_FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true };

const renderLogin = (props = {}) =>
  render(
    <MemoryRouter future={ROUTER_FUTURE}>
      <Login {...props} />
    </MemoryRouter>,
  );

beforeEach(() => {
  navigate.mockClear();
  api.mockReset();
});

describe("Login form", () => {
  it("starts in sign-in mode", () => {
    renderLogin();
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Login" })).toBeInTheDocument();
  });

  it("exposes both fields by accessible name", () => {
    renderLogin();
    expect(screen.getByLabelText("Username")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
  });

  it("masks the password field", () => {
    renderLogin();
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
  });

  it("keeps submit disabled until both fields are filled", async () => {
    const user = userEvent.setup();
    renderLogin();
    const submit = screen.getByRole("button", { name: "Login" });
    expect(submit).toBeDisabled();
    await user.type(screen.getByLabelText("Username"), "operator");
    expect(submit).toBeDisabled();
    await user.type(screen.getByLabelText("Password"), "pw");
    expect(submit).toBeEnabled();
  });

  it("toggles to register mode and back", async () => {
    const user = userEvent.setup();
    renderLogin();
    await user.click(screen.getByRole("button", { name: /Need an account/ }));
    expect(screen.getByRole("heading", { name: "Create account" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Have an account/ }));
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });
});

describe("Login submission", () => {
  async function fillAndSubmit(user, buttonName = "Login") {
    await user.type(screen.getByLabelText("Username"), "operator");
    await user.type(screen.getByLabelText("Password"), "secret");
    await user.click(screen.getByRole("button", { name: buttonName }));
  }

  it("posts to the login endpoint and stores the returned token", async () => {
    const user = userEvent.setup();
    api.mockResolvedValue({ token: "tok-123" });
    renderLogin();
    await fillAndSubmit(user);
    await waitFor(() => expect(api).toHaveBeenCalledWith("/auth/login/", {
      method: "POST",
      body: { username: "operator", password: "secret" },
    }));
    expect(getToken()).toBe("tok-123");
  });

  it("posts to the register endpoint in register mode", async () => {
    const user = userEvent.setup();
    api.mockResolvedValue({ token: "tok-456" });
    renderLogin();
    await user.click(screen.getByRole("button", { name: /Need an account/ }));
    await fillAndSubmit(user, "Register");
    await waitFor(() => expect(api).toHaveBeenCalledWith("/auth/register/", expect.anything()));
  });

  it("notifies the parent and routes to the dashboard on success", async () => {
    const user = userEvent.setup();
    const onAuth = vi.fn();
    api.mockResolvedValue({ token: "tok-123" });
    renderLogin({ onAuth });
    await fillAndSubmit(user);
    await waitFor(() => expect(onAuth).toHaveBeenCalled());
    expect(navigate).toHaveBeenCalledWith("/");
  });

  it("surfaces the server's rejection as an alert and stores no token", async () => {
    const user = userEvent.setup();
    api.mockRejectedValue(new Error("Invalid credentials."));
    renderLogin();
    await fillAndSubmit(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid credentials.");
    expect(getToken()).toBeNull();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("does not fire a duplicate request when submitted twice quickly", async () => {
    const user = userEvent.setup();
    let release;
    api.mockImplementation(() => new Promise((resolve) => { release = () => resolve({ token: "t" }); }));
    renderLogin();
    await user.type(screen.getByLabelText("Username"), "operator");
    await user.type(screen.getByLabelText("Password"), "secret");
    const submit = screen.getByRole("button", { name: "Login" });
    await user.click(submit);
    expect(submit).toBeDisabled();
    expect(api).toHaveBeenCalledTimes(1);
    release();
    // Settle the pending state update so it does not leak into the next test.
    await waitFor(() => expect(navigate).toHaveBeenCalled());
  });
});
