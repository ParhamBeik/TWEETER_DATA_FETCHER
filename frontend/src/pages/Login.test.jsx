import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Login from "./Login";
import { api } from "../api";

// Component tests: Login owns real interaction logic (submit guarding, error
// surfacing, handing the token pair to the auth context), so it is driven
// through the DOM with the network boundary mocked -- the seam a user's actions
// actually cross.

vi.mock("../api", async () => {
  const actual = await vi.importActual("../api");
  return { ...actual, api: vi.fn() };
});

const signIn = vi.fn();
vi.mock("../auth", () => ({ useAuth: () => ({ signIn }) }));

const navigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return { ...actual, useNavigate: () => navigate };
});

// v7 flags only silence the upgrade warnings; they do not change what is tested.
const ROUTER_FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true };

const renderLogin = () =>
  render(
    <MemoryRouter future={ROUTER_FUTURE}>
      <Login />
    </MemoryRouter>,
  );

async function fillIn(user, { username = "carol", password = "correct-horse" } = {}) {
  await user.type(screen.getByLabelText("Username"), username);
  await user.type(screen.getByLabelText("Password"), password);
}

beforeEach(() => {
  navigate.mockClear();
  signIn.mockClear();
  api.mockReset();
});

describe("Login form", () => {
  it("exposes both fields by accessible name", () => {
    renderLogin();
    expect(screen.getByLabelText("Username")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
  });

  it("masks the password field", () => {
    renderLogin();
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
  });

  it("shows and hides the password on demand", async () => {
    const user = userEvent.setup();
    renderLogin();

    await user.click(screen.getByRole("button", { name: "Show password" }));
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "text");

    await user.click(screen.getByRole("button", { name: "Hide password" }));
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
  });

  it("keeps submit disabled until both fields are filled", async () => {
    const user = userEvent.setup();
    renderLogin();
    const submit = screen.getByRole("button", { name: /Sign in/ });

    expect(submit).toBeDisabled();
    await user.type(screen.getByLabelText("Username"), "carol");
    expect(submit).toBeDisabled();
    await user.type(screen.getByLabelText("Password"), "correct-horse");
    expect(submit).toBeEnabled();
  });

  it("offers a route to the signup page", () => {
    renderLogin();
    expect(screen.getByRole("link", { name: /Create an account/i })).toHaveAttribute(
      "href",
      "/signup",
    );
  });
});

describe("Login submission", () => {
  it("posts the credentials to the login endpoint", async () => {
    const user = userEvent.setup();
    api.mockResolvedValue({ access: "a", refresh: "r", user: { username: "carol" } });
    renderLogin();

    await fillIn(user);
    await user.click(screen.getByRole("button", { name: /Sign in/ }));

    await waitFor(() => expect(api).toHaveBeenCalled());
    const [path, options] = api.mock.calls[0];
    expect(path).toBe("/auth/login/");
    expect(options.method).toBe("POST");
    expect(options.body).toEqual({ username: "carol", password: "correct-horse" });
  });

  it("does not route a failed login through the token refresh path", async () => {
    // A rejected login is about these credentials, not an expired session.
    const user = userEvent.setup();
    api.mockResolvedValue({ access: "a", refresh: "r" });
    renderLogin();

    await fillIn(user);
    await user.click(screen.getByRole("button", { name: /Sign in/ }));

    await waitFor(() => expect(api).toHaveBeenCalled());
    expect(api.mock.calls[0][1].retry).toBe(false);
  });

  it("hands the token pair to the session and routes to the dashboard", async () => {
    const user = userEvent.setup();
    const tokens = { access: "a", refresh: "r", user: { username: "carol" } };
    api.mockResolvedValue(tokens);
    renderLogin();

    await fillIn(user);
    await user.click(screen.getByRole("button", { name: /Sign in/ }));

    await waitFor(() => expect(signIn).toHaveBeenCalledWith(tokens));
    expect(navigate).toHaveBeenCalledWith("/feed", { replace: true });
  });

  it("surfaces the server's rejection and starts no session", async () => {
    const user = userEvent.setup();
    api.mockRejectedValue(new Error("Incorrect username or password."));
    renderLogin();

    await fillIn(user, { password: "wrong-password" });
    await user.click(screen.getByRole("button", { name: /Sign in/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Incorrect username or password.");
    expect(signIn).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("does not fire a duplicate request when submitted twice quickly", async () => {
    const user = userEvent.setup();
    let release;
    api.mockImplementation(() => new Promise((resolve) => {
      release = () => resolve({ access: "a", refresh: "r" });
    }));
    renderLogin();

    await fillIn(user);
    const submit = screen.getByRole("button", { name: /Sign in/ });
    await user.click(submit);
    await user.click(submit);

    expect(api).toHaveBeenCalledTimes(1);

    // Let the in-flight request settle inside act(), so the resulting state
    // update does not land after the test has finished.
    await act(async () => {
      release();
    });
  });
});
