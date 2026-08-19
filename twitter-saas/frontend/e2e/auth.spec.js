import { expect, test } from "@playwright/test";
import { OPERATOR, login } from "./helpers";

// End-to-end: authentication crosses the browser, the Vite proxy, DRF token auth
// and localStorage -- none of which the component suite exercises together.

test.describe("authentication", () => {
  test("sends an anonymous visitor to the login form", async ({ page }) => {
    await page.goto("/feed");
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
    await expect(page.getByRole("navigation")).toHaveCount(0);
  });

  test("rejects bad credentials without granting access", async ({ page }) => {
    await page.goto("/login");
    await page.getByLabel("Username").fill(OPERATOR.username);
    await page.getByLabel("Password").fill("wrong-password");
    await page.getByRole("button", { name: "Login" }).click();
    // Assert the server's own rejection, not merely "some alert": a generic
    // check here passes even when the API is unreachable.
    await expect(page.getByRole("alert")).toHaveText("invalid credentials");
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  });

  test("signs the operator in and reveals the console", async ({ page }) => {
    await login(page);
    await expect(page.getByRole("navigation")).toBeVisible();
  });

  test("keeps the session across a reload", async ({ page }) => {
    await login(page);
    await page.reload();
    await expect(page.getByRole("navigation")).toBeVisible();
  });

  test("logs out and locks the console again", async ({ page }) => {
    await login(page);
    await page.getByRole("button", { name: "Logout" }).click();
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
    await page.goto("/accounts");
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  });

  test("returns to the login form when the stored token is rejected", async ({ page }) => {
    await login(page);
    // Simulate a server-side token revocation.
    await page.evaluate(() => localStorage.setItem("tsaas_token", "no-longer-valid"));
    await page.goto("/feed");
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  });
});
