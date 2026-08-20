import { expect, test } from "@playwright/test";
import { login } from "./helpers";

// End-to-end: every page is reached by clicking, and each renders real data from
// the seeded archive -- proving the proxy, auth header and serializers line up.

test.beforeEach(async ({ page }) => login(page));

test.describe("navigation", () => {
  const destinations = [
    ["Feed", /The captured stream/],
    ["Analyze", /What is accelerating in the archive/],
    ["Searches", /Saved archive queries/],
    ["Accounts", /Roster, tiers, and collection health/],
    ["Ops", /Collection health and operator controls/],
    ["Pulse", /What changed since you last looked/],
  ];

  for (const [link, heading] of destinations) {
    test(`reaches ${link} from the nav`, async ({ page }) => {
      await page.getByRole("link", { name: link, exact: true }).click();
      await expect(page.getByRole("heading", { name: heading })).toBeVisible();
    });
  }
});

test.describe("feed", () => {
  test.beforeEach(async ({ page }) => {
    await page.getByRole("link", { name: "Feed", exact: true }).click();
    await expect(page.getByRole("heading", { name: /The captured stream/ })).toBeVisible();
  });

  test("lists tweets from tracked accounts only", async ({ page }) => {
    await expect(page.getByText("Falcon launch cadence is accelerating this quarter.")).toBeVisible();
    await expect(page.getByText("Market volatility spiked on the announcement.")).toBeVisible();
    // gamma_muted is not tracked, so its tweet must not reach the feed.
    await expect(page.getByText("This account is not currently tracked.")).toHaveCount(0);
  });

  test("filters the archive by account", async ({ page }) => {
    await page.getByLabel("Account").fill("beta_watch");
    await page.getByRole("button", { name: "Filter" }).click();
    await expect(page.getByText("Market volatility spiked on the announcement.")).toBeVisible();
    await expect(page.getByText("Falcon launch cadence is accelerating this quarter.")).toHaveCount(0);
  });

  test("filters the archive by full-text query", async ({ page }) => {
    await page.getByLabel("Search archive").fill("volatility");
    await page.getByRole("button", { name: "Filter" }).click();
    await expect(page.getByText("Market volatility spiked on the announcement.")).toBeVisible();
    await expect(page.getByText("Falcon launch cadence is accelerating this quarter.")).toHaveCount(0);
  });

  test("says so when a filter matches nothing", async ({ page }) => {
    await page.getByLabel("Account").fill("nobody_at_all");
    await page.getByRole("button", { name: "Filter" }).click();
    await expect(page.getByText("No tweets yet.")).toBeVisible();
  });

  test("downloads a CSV export of the current filter", async ({ page }) => {
    const download = page.waitForEvent("download");
    await page.getByRole("button", { name: "Export CSV" }).click();
    expect((await download).suggestedFilename()).toBe("tweets.csv");
  });

  test("downloads a JSONL export", async ({ page }) => {
    const download = page.waitForEvent("download");
    await page.getByRole("button", { name: "Export JSONL" }).click();
    expect((await download).suggestedFilename()).toBe("tweets.jsonl");
  });
});

test.describe("accounts", () => {
  test.beforeEach(async ({ page }) => {
    await page.getByRole("link", { name: "Accounts", exact: true }).click();
    await expect(page.getByRole("heading", { name: /Roster, tiers/ })).toBeVisible();
  });

  test("shows the roster with its collection states", async ({ page }) => {
    await expect(page.getByRole("button", { name: "@alpha_signal" })).toBeVisible();
    await expect(page.getByText("quarantined")).toBeVisible();
    await expect(page.getByText("off")).toBeVisible();
  });

  test("opens an account timeline", async ({ page }) => {
    await page.getByRole("button", { name: "@alpha_signal" }).click();
    await expect(page.getByText("Second stage recovered intact after the burn.")).toBeVisible();
  });

  test("toggles tracking off and persists it across a reload", async ({ page }) => {
    const row = page.getByRole("row", { name: /alpha_signal/ });
    await row.getByRole("button", { name: "disable" }).click();
    await expect(row.getByRole("button", { name: "enable" })).toBeVisible();
    await page.reload();
    await expect(page.getByRole("row", { name: /alpha_signal/ }).getByRole("button", { name: "enable" }))
      .toBeVisible();
    // Restore the fixture for any later spec.
    await page.getByRole("row", { name: /alpha_signal/ }).getByRole("button", { name: "enable" }).click();
  });

  test("adds an account to the comparison tray", async ({ page }) => {
    await page.getByLabel("Compare @alpha_signal").check();
    await expect(page.getByText("Compare accounts")).toBeVisible();
  });
});

test.describe("searches", () => {
  test.beforeEach(async ({ page }) => {
    await page.getByRole("link", { name: "Searches", exact: true }).click();
    await expect(page.getByRole("heading", { name: /Saved archive queries/ })).toBeVisible();
  });

  test("lists the seeded saved search and opens its results", async ({ page }) => {
    await page.getByRole("button", { name: /Launch coverage/ }).click();
    await expect(page.getByText("Falcon launch cadence is accelerating this quarter.")).toBeVisible();
  });

  test("keeps the submit button disabled until a query is typed", async ({ page }) => {
    const submit = page.getByRole("button", { name: "Run Top" });
    await expect(submit).toBeDisabled();
    await page.getByLabel("Raw query").fill("lang:en");
    await expect(submit).toBeEnabled();
  });

  test("switches to the Latest product", async ({ page }) => {
    await page.getByRole("button", { name: "Latest" }).click();
    await expect(page.getByText("No Latest searches yet.")).toBeVisible();
  });
});

test.describe("ops", () => {
  test.beforeEach(async ({ page }) => {
    await page.getByRole("link", { name: "Ops", exact: true }).click();
    await expect(page.getByRole("heading", { name: /Collection health/ })).toBeVisible();
  });

  test("reports that no X session is configured in a clean environment", async ({ page }) => {
    await expect(page.getByText("● No active X session")).toBeVisible();
  });

  test("rejects a malformed session payload before sending it", async ({ page }) => {
    await page.getByLabel("X session JSON").fill("definitely not json");
    await page.getByRole("button", { name: "Update session" }).click();
    await expect(page.getByText("Session must be valid JSON.")).toBeVisible();
  });

  test("queues a cycle and confirms it to the operator", async ({ page }) => {
    // settings_e2e keeps Celery non-eager, so this records intent only -- no
    // pipeline subprocess runs and no request reaches X.
    await page.getByRole("button", { name: "Run live" }).click();
    await expect(page.getByText("Queued live cycle.")).toBeVisible();
  });
});
