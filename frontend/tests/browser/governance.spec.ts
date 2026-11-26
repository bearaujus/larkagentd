import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures";

async function governanceFixture(page: Page) {
  const calls: { path: string; body: Record<string, unknown> }[] = [];
  const history: object[] = [];
  const model = {
    model: "model-one",
    effective_model: "model-one",
    effort: null as string | null,
  };
  const quota = {
    cadence: "monthly",
    free_limit: 1000,
    cycle_start: "2026-11-01",
    resets_on: "2026-12-01",
    pending: null as Record<string, unknown> | null,
  };
  const prices: Record<string, Record<string, string | null>> = {};
  const pool = (limit: number, used: number) => ({
    limit,
    recurring_limit: limit,
    adjusted: false,
    pending: null as { limit: number | null; effective_on: string } | null,
    used,
    unavailable_sessions: 0,
  });
  const pools = { free: pool(1000, 120), flex: pool(400, 60) };
  const flex = {
    email: "member@example.com",
    enabled: false,
    active_limit: 50,
    pending_limit: null as number | null,
    used: 15,
  };
  await page.route("**/v1/dashboard/governance**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.split("/governance")[1];
    if (path === "/history") {
      return route.fulfill({ json: { items: history, next_before: null } });
    }
    if (request.method() !== "GET") {
      const body = request.postDataJSON();
      calls.push({ path, body });
      if (path === "/model")
        Object.assign(model, body, { effective_model: body.model });
      if (path.startsWith("/prices/")) prices[path.split("/").at(-1)!] = body;
      if (path === "/quota") {
        if (body.free_limit !== undefined) quota.free_limit = body.free_limit;
        if (body.cadence !== undefined) {
          quota.pending = {
            cadence: body.cadence,
            free_limit: quota.free_limit,
            effective_on: quota.resets_on,
          };
        }
      }
      if (path.startsWith("/pools/")) {
        const tier = path.endsWith("/free") ? "free" : "flex";
        const selected = pools[tier];
        history.unshift({
          id: String(history.length + 1),
          occurred_at: "2026-11-26T12:00:00+00:00",
          actor_email: "admin@example.com",
          resource: "shared_pool",
          target: tier,
          before: { limit: selected.limit },
          after: { limit: body.limit },
        });
        selected.limit = body.limit;
        selected.recurring_limit = body.limit;
      }
      if (path.startsWith("/flex/")) {
        if (body.enabled !== undefined) flex.enabled = body.enabled;
        if (body.token_limit !== undefined) {
          flex.active_limit = body.token_limit;
          flex.pending_limit = null;
        }
      }
    }
    await route.fulfill({
      json: {
        ...model,
        quota,
        prices,
        pools,
        flex: [flex],
        models: [
          { id: "model-one", efforts: [] },
          { id: "model-two", efforts: ["low", "high"] },
        ],
      },
    });
  });
  return calls;
}

test("admins save global settings, rates and independently enable FlexTier", async ({
  page,
  state,
}) => {
  state.role = "admin";
  const calls = await governanceFixture(page);
  await page.goto("/dashboard/governance");
  await expect(page.getByRole("heading", { name: "Governance" })).toBeVisible();
  await expect(
    page.getByRole("combobox", { name: "Reasoning effort", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("combobox", { name: "Model", exact: true })
    .selectOption("model-two");
  await page
    .getByRole("combobox", { name: "Reasoning effort", exact: true })
    .selectOption("high");
  await page.getByRole("button", { name: "Save model" }).click();
  await expect(
    page.getByText("In use for new turns: model-two (high effort)."),
  ).toBeVisible();
  await page
    .getByRole("combobox", { name: "Model to price", exact: true })
    .selectOption("model-two");
  await page.getByLabel("Uncached input").fill("0.125");
  await page.getByRole("button", { name: "Save rates" }).click();
  await expect(page.getByText("Model rates saved.")).toBeVisible();

  const frequency = page.getByRole("combobox", {
    name: "Reset frequency",
    exact: true,
  });
  await frequency.selectOption("weekly");
  await page.getByLabel("FreeTier token limit").fill("500");
  await page.getByRole("button", { name: "Save FreeTier limit" }).click();
  await expect(
    page.getByText(/^Active limit 500 tokens per user/),
  ).toBeVisible();
  await expect(frequency).toHaveValue("weekly");
  await page.getByRole("button", { name: "Save reset frequency" }).click();
  await expect(
    page.getByText(
      "Reset frequency changes to weekly on 2026-12-01 at 00:00 UTC.",
    ),
  ).toBeVisible();
  await page
    .getByRole("checkbox", { name: "Enable FlexTier for member@example.com" })
    .click();
  await expect(page.getByText("FlexTier assignment updated.")).toBeVisible();
  await expect(
    page.getByRole("checkbox", {
      name: "Enable FlexTier for member@example.com",
    }),
  ).toBeChecked();
  await page.getByLabel("FlexTier limit for member@example.com").fill("25");
  await page.getByRole("button", { name: "Save limit", exact: true }).click();
  await expect(page.getByText("FlexTier limit updated.")).toBeVisible();
  await expect(
    page.getByRole("cell", { name: "25", exact: true }),
  ).toBeVisible();
  expect(calls).toEqual([
    { path: "/model", body: { model: "model-two", effort: "high" } },
    {
      path: "/prices/model-two",
      body: {
        input_tokens: "0.125",
        output_tokens: null,
        cache_read_tokens: null,
        cache_write_tokens: null,
      },
    },
    { path: "/quota", body: { free_limit: 500 } },
    { path: "/quota", body: { cadence: "weekly" } },
    { path: "/flex/member%40example.com", body: { enabled: true } },
    { path: "/flex/member%40example.com", body: { token_limit: 25 } },
  ]);
});

test("admins save immediate pool limits and see who changed them", async ({
  page,
  state,
}) => {
  state.role = "admin";
  const calls = await governanceFixture(page);
  await page.goto("/dashboard/governance");
  const free = page.getByRole("region", { name: "FreeTier pool" });
  const flex = page.getByRole("region", { name: "FlexTier pool" });
  const freeLimit = free.getByLabel("FreeTier pool token limit");
  await freeLimit.fill("1500");
  await free.getByRole("button", { name: "Save pool limit" }).click();
  await expect(free.getByText(/1,380 remaining/)).toBeVisible();
  await freeLimit.fill("100");
  await free.getByRole("button", { name: "Save pool limit" }).click();
  await expect(free.getByText(/120 tokens used.*0 remaining/)).toBeVisible();
  await flex.getByLabel("FlexTier pool token limit").fill("250");
  await flex.getByRole("button", { name: "Save pool limit" }).click();
  await expect(flex.getByText(/190 remaining/)).toBeVisible();
  const row = page
    .getByRole("region", { name: "Quota changes" })
    .getByRole("row")
    .filter({ hasText: "FlexTier shared pool" });
  await expect(row).toContainText("admin@example.com");
  await expect(row).toContainText("400 tokens -> 250 tokens");
  expect(calls).toEqual([
    { path: "/pools/free", body: { limit: 1500 } },
    { path: "/pools/free", body: { limit: 100 } },
    { path: "/pools/flex", body: { limit: 250 } },
  ]);
});

test("managers can view governance and pause new group requests without changing group membership", async ({
  page,
  state,
}) => {
  await governanceFixture(page);
  state.role = "manager";
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/dashboard/governance");
  await expect(
    page.getByText("Admins can change these settings."),
  ).toBeVisible();
  await expect(
    page.getByRole("combobox", { name: "Model", exact: true }),
  ).toBeDisabled();
  await expect(page.getByLabel("FreeTier token limit")).toHaveCount(0);
  await expect(
    page.getByRole("region", { name: "FreeTier pool" }),
  ).toBeVisible();
  await expect(page.getByLabel("FreeTier pool token limit")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Add FlexTier assignment" }),
  ).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(
    390,
  );
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);

  await page.getByRole("button", { name: "Pause new requests" }).click();
  await expect(
    page.getByRole("status").getByText("Paused", { exact: true }),
  ).toBeVisible();
  expect(state.groups[0].members).toEqual(["alex@example.com"]);
  await page.getByRole("button", { name: "Resume requests" }).click();
  await expect(
    page.getByText("Accepting requests", { exact: true }),
  ).toBeVisible();
});

test("governance screenshots with synthetic fixtures", async ({
  page,
  state,
}, info) => {
  state.loggedIn = true;
  test.skip(
    info.project.name !== "chromium" ||
      process.env.LARKAGENTD_SCREENSHOTS !== "1",
  );
  await governanceFixture(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/dashboard/governance");
  await expect(
    page.getByRole("button", { name: "Pause new requests" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Governance", exact: true }),
  ).toBeVisible();
  await page.evaluate(() => {
    const label = document.createElement("div");
    label.textContent = "SYNTHETIC DATA";
    label.style.cssText =
      "position:fixed;bottom:12px;right:16px;background:#15243c;color:white;padding:6px 9px;font:11px system-ui;z-index:100;border-radius:4px";
    document.body.append(label);
  });
  await page.screenshot({
    path: "../docs/screenshots/governance.png",
    animations: "disabled",
  });
});
