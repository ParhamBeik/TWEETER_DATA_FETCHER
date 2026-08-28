import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Signup, { getPasswordChecks, strengthFor } from "./Signup";
import { api } from "../api";

// Unit tests for the two pure helpers, component tests for the form. The
// password rules here mirror the server's; the server still decides, and these
// assert that its per-field rejections reach the right input.

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

const ROUTER_FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true };

const renderSignup = () =>
  render(
    <MemoryRouter future={ROUTER_FUTURE}>
      <Signup />
    </MemoryRouter>,
  );

const STRONG = "Correct-Horse-9";

async function fillIn(user, { username = "carol", password = STRONG, confirm = STRONG } = {}) {
  await user.type(screen.getByLabelText("Username"), username);
  await user.type(screen.getByLabelText("Password"), password);
  await user.type(screen.getByLabelText("Confirm password"), confirm);
}

beforeEach(() => {
  navigate.mockClear();
  signIn.mockClear();
  api.mockReset();
});

describe("password checks", () => {
  it("requires ten characters, mixed case, and a number or symbol", () => {
    const byId = Object.fromEntries(
      getPasswordChecks("Correct-Horse-9", "Correct-Horse-9").map((c) => [c.id, c.ok]),
    );
    expect(byId).toMatchObject({ length: true, case: true, number: true, match: true });
  });

  it.each([
    ["Short-1", "length"],
    ["alllowercase9", "case"],
    ["CorrectHorseStaple", "number"],
  ])("fails %s on the %s rule", (password, ruleId) => {
    const check = getPasswordChecks(password, password).find((c) => c.id === ruleId);
    expect(check.ok).toBe(false);
  });

  it("does not report a match for two empty fields", () => {
    const match = getPasswordChecks("", "").find((c) => c.id === "match");
    expect(match.ok).toBe(false);
  });

  it("reports no strength before anything is typed", () => {
    expect(strengthFor(getPasswordChecks("", ""), "")).toBeNull();
  });

  it("grades a fully valid password as strong", () => {
    expect(strengthFor(getPasswordChecks(STRONG, STRONG), STRONG)).toBe("Strong");
  });
});

describe("Signup form", () => {
  it("keeps submit disabled until the password rules pass", async () => {
    const user = userEvent.setup();
    renderSignup();
    const submit = screen.getByRole("button", { name: /Create account/ });

    await user.type(screen.getByLabelText("Username"), "carol");
    await user.type(screen.getByLabelText("Password"), "weak");
    expect(submit).toBeDisabled();

    await user.clear(screen.getByLabelText("Password"));
    await user.type(screen.getByLabelText("Password"), STRONG);
    await user.type(screen.getByLabelText("Confirm password"), STRONG);
    expect(submit).toBeEnabled();
  });

  it("stays disabled when the confirmation does not match", async () => {
    const user = userEvent.setup();
    renderSignup();

    await fillIn(user, { confirm: "Something-Else-9" });

    expect(screen.getByRole("button", { name: /Create account/ })).toBeDisabled();
  });

  it("treats email as optional", async () => {
    const user = userEvent.setup();
    api.mockResolvedValue({ access: "a", refresh: "r" });
    renderSignup();

    await fillIn(user);
    await user.click(screen.getByRole("button", { name: /Create account/ }));

    await waitFor(() => expect(api).toHaveBeenCalled());
    expect(api.mock.calls[0][1].body.email).toBe("");
  });

  it("posts the account details to the register endpoint", async () => {
    const user = userEvent.setup();
    api.mockResolvedValue({ access: "a", refresh: "r" });
    renderSignup();

    await fillIn(user);
    await user.type(screen.getByLabelText("Email"), "carol@example.com");
    await user.click(screen.getByRole("button", { name: /Create account/ }));

    await waitFor(() => expect(api).toHaveBeenCalled());
    const [path, options] = api.mock.calls[0];
    expect(path).toBe("/auth/register/");
    expect(options.body).toEqual({
      username: "carol",
      email: "carol@example.com",
      password: STRONG,
    });
  });

  it("signs the new account in and routes to the dashboard", async () => {
    const user = userEvent.setup();
    const tokens = { access: "a", refresh: "r", user: { username: "carol", is_staff: false } };
    api.mockResolvedValue(tokens);
    renderSignup();

    await fillIn(user);
    await user.click(screen.getByRole("button", { name: /Create account/ }));

    await waitFor(() => expect(signIn).toHaveBeenCalledWith(tokens));
    expect(navigate).toHaveBeenCalledWith("/feed", { replace: true });
  });

  it("puts the server's per-field rejection next to the field it names", async () => {
    const user = userEvent.setup();
    const failure = new Error("That username is taken.");
    failure.fieldErrors = { username: ["That username is taken."] };
    api.mockRejectedValue(failure);
    renderSignup();

    await fillIn(user);
    await user.click(screen.getByRole("button", { name: /Create account/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent("That username is taken.");
    // Twice: once in the banner, once beside the username input.
    expect(await screen.findAllByText("That username is taken.")).toHaveLength(2);
    expect(signIn).not.toHaveBeenCalled();
  });

  it("surfaces a password the server rejects even though the local rules passed", async () => {
    const user = userEvent.setup();
    const failure = new Error("This password is too common.");
    failure.fieldErrors = { password: ["This password is too common."] };
    api.mockRejectedValue(failure);
    renderSignup();

    await fillIn(user);
    await user.click(screen.getByRole("button", { name: /Create account/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent("This password is too common.");
  });

  it("offers a route back to the login page", () => {
    renderSignup();
    expect(screen.getByRole("link", { name: /Sign in/i })).toHaveAttribute("href", "/login");
  });
});
