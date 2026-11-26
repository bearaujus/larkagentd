import assert from "node:assert/strict";
import { chromium } from "@playwright/test";

let input = "";
for await (const chunk of process.stdin) input += chunk;
const { origin, token } = JSON.parse(input);
const browser = await chromium.launch();
try {
  const context = await browser.newContext();
  const page = await context.newPage();
  await context.route("**/*", (route) =>
    new URL(route.request().url()).origin === origin
      ? route.continue()
      : route.abort(),
  );
  await page.goto(`${origin}/dashboard/`);
  await page.getByLabel("Login token", { exact: true }).fill(token);
  await page.getByRole("button", { name: "Open dashboard" }).click();
  await page.getByRole("heading", { name: "Overview", exact: true }).waitFor();
  const [cookie] = (await context.cookies()).filter(
    (c) => c.name === "larkagentd_dashboard",
  );
  assert(
    cookie.httpOnly &&
      cookie.sameSite === "Strict" &&
      cookie.path === "/v1/dashboard",
  );
  assert.equal(await page.evaluate(() => document.cookie), "");
  const denied = await context.request.patch(
    `${origin}/v1/dashboard/bot-access`,
    {
      headers: { Origin: "https://other.example.com" },
    },
  );
  assert.equal(denied.status(), 403);
  const governance = await page.goto(`${origin}/dashboard/governance`);
  assert.equal(governance.status(), 200);
  await page
    .getByRole("heading", { name: "Governance", exact: true })
    .waitFor();
  await page.getByText("In use for new turns: smoke-model.").waitFor();
  await page.getByLabel("FreeTier token limit").fill("500");
  await page.getByRole("button", { name: "Save FreeTier limit" }).click();
  await page.getByText(/^Active limit 500 tokens per user/).waitFor();
  const freePool = page.getByRole("region", { name: "FreeTier pool" });
  await freePool.getByLabel("FreeTier pool token limit").fill("1500");
  await freePool.getByRole("button", { name: "Save pool limit" }).click();
  await freePool.getByText(/1,500 remaining/).waitFor();
  const audit = page.getByRole("region", { name: "Quota changes" });
  await audit
    .getByRole("row")
    .filter({ hasText: "FreeTier shared pool" })
    .getByText("admin@example.com", { exact: true })
    .waitFor();
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await page.getByLabel("Login token", { exact: true }).waitFor();
  const session = await context.request.get(
    `${origin}/v1/dashboard/auth/session`,
  );
  assert.equal(session.status(), 401);
} finally {
  await browser.close();
}
