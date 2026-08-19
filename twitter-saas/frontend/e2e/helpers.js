import { expect } from "@playwright/test";

// Must match apps/fetching/management/commands/seed_e2e.py.
export const OPERATOR = { username: "e2e-operator", password: "e2e-password-123" };

export async function login(page) {
  await page.goto("/login");
  await page.getByLabel("Username").fill(OPERATOR.username);
  await page.getByLabel("Password").fill(OPERATOR.password);
  await page.getByRole("button", { name: "Login" }).click();
  // The dashboard heading is the first authenticated paint.
  await expect(page.getByRole("heading", { name: /What changed since you last looked/ }))
    .toBeVisible();
}
