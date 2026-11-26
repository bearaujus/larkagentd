import AxeBuilder from "@axe-core/playwright";
import { test, expect } from "./fixtures";

test("session expiry removes protected screens without waiting for another action", async ({
  page,
  state,
}) => {
  state.sessionSeconds = 2;
  await page.goto("/dashboard/people");
  await expect(
    page.getByRole("heading", { name: "People & roles" }),
  ).toBeVisible();
  await expect(page.getByLabel("Login token", { exact: true })).toBeVisible({
    timeout: 6000,
  });
  await expect(
    page.getByText("Your session ended.", { exact: false }),
  ).toBeVisible();
  await expect(page.getByText("Protected admin", { exact: true })).toHaveCount(
    0,
  );
});

test("export failures never offer an error response as a file", async ({
  page,
  state,
}) => {
  await page.goto("/dashboard/usage");
  await expect(
    page.getByRole("button", { name: "Expand alex@example.com", exact: true }),
  ).toBeVisible();
  state.failUsage = true;
  let downloads = 0;
  page.on("download", () => downloads++);
  await page.getByRole("button", { name: "Export all matching data" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "temporarily unavailable",
  );
  expect(downloads).toBe(0);
});

test("overview and login meet automated accessibility checks", async ({
  page,
  state,
}) => {
  await page.goto("/dashboard/");
  await expect(page.getByText("1.3M", { exact: true })).toBeVisible();
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  state.loggedIn = false;
  await page.reload();
  await expect(page.getByLabel("Login token", { exact: true })).toBeVisible();
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
});

test("login uses a transient token and clears it after failure and success", async ({
  page,
  state,
}) => {
  state.loggedIn = false;
  state.expired = true;
  await page.goto("/dashboard/");
  await page
    .getByLabel("Login token", { exact: true })
    .fill("synthetic-expired");
  await page.getByRole("button", { name: "Open dashboard" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "invalid, expired or already used",
  );
  await expect(page.getByLabel("Login token", { exact: true })).toHaveValue("");
  state.expired = false;
  await page.getByLabel("Login token", { exact: true }).fill("synthetic-once");
  await page.getByRole("button", { name: "Open dashboard" }).click();
  await expect(
    page.getByRole("heading", { name: "Overview", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(() => ({
      local: { ...localStorage },
      session: { ...sessionStorage },
    })),
  ).toEqual({ local: {}, session: {} });
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page.getByLabel("Login token", { exact: true })).toBeVisible();
});
test("member cannot enter the dashboard", async ({ page, state }) => {
  state.role = "member";
  await page.goto("/dashboard/usage");
  await expect(page.getByLabel("Login token", { exact: true })).toBeVisible();
  await page
    .getByLabel("Login token", { exact: true })
    .fill("synthetic-member");
  await page.getByRole("button", { name: "Open dashboard" }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  expect(
    state.requests.filter((r) => r.path.startsWith("/usage")),
  ).toHaveLength(0);
});
test("role matrix protects admins and limits manager administration", async ({
  page,
  state,
}) => {
  await page.goto("/dashboard/people");
  await expect(
    page.getByText("Protected admin", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Add manager" })).toBeVisible();
  await expect(
    page.getByText("Directory: directory-admin@example.com").first(),
  ).toBeVisible();
  await expect(
    page.getByText("admin@example.com", { exact: true }).first(),
  ).toBeVisible();
  state.role = "manager";
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "People & roles" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Add manager" })).toHaveCount(
    0,
  );
  await expect(
    page.getByRole("button", { name: "Remove", exact: true }),
  ).toHaveCount(0);
  await page.goto("/dashboard/groups/oc_platform");
  await expect(page.getByRole("button", { name: "Add member" })).toBeVisible();
  state.loggedIn = false;
  await page.getByRole("radio", { name: /All users/ }).check();
  await page.getByRole("button", { name: "Save mode" }).click();
  await expect(page.getByLabel("Login token", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Saved members (1)" }),
  ).toHaveCount(0);
});
test("mode changes preserve members and failed writes leave server state intact", async ({
  page,
  state,
}) => {
  await page.goto("/dashboard/groups/oc_platform");
  await expect(
    page.getByText("alex@example.com", { exact: true }),
  ).toBeVisible();
  await page.getByRole("radio", { name: /All users/ }).check();
  await page.getByRole("button", { name: "Save mode" }).click();
  await expect(page.getByText("Current mode: All users")).toBeVisible();
  expect(state.groups[0].members).toEqual(["alex@example.com"]);
  state.failWrite = true;
  await page.getByRole("radio", { name: /Specific users/ }).check();
  await page.getByRole("button", { name: "Save mode" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "temporarily unavailable",
  );
  expect(state.groups[0].mode).toBe("all_users");
  await expect(
    page.getByRole("radio", { name: /Specific users/ }),
  ).toBeChecked();
  state.failWrite = false;
  await page.getByRole("button", { name: "Save mode" }).click();
  await expect(page.getByText("Current mode: Specific users")).toBeVisible();
  expect(state.groups[0].members).toEqual(["alex@example.com"]);
});
test("group preview, defaults, group access, duplicate and delete confirmation", async ({
  page,
  state,
}) => {
  state.metadata = "unavailable";
  await page.goto("/dashboard/groups");
  await expect(
    page.getByText(/Group members can ask before opening a direct chat/),
  ).toBeVisible();
  await expect(page.getByLabel("Lark group ID", { exact: true })).toHaveCount(
    0,
  );
  await page.getByRole("button", { name: "Add group", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Lark group ID", { exact: true }).fill("oc_new");
  await dialog.getByRole("button", { name: "Continue" }).click();
  await expect(
    dialog.getByText(/directory could not confirm a name/),
  ).toBeVisible();
  expect(state.groups).toHaveLength(1);
  await expect(
    dialog.getByRole("button", { name: "Add group", exact: true }),
  ).toBeDisabled();
  await dialog.getByRole("checkbox").check();
  await dialog.getByRole("button", { name: "Add group", exact: true }).click();
  await expect(page.getByText("Current mode: All users")).toBeVisible();
  expect(state.groups.find((g) => g.group_id === "oc_new")?.mode).toBe(
    "all_users",
  );
  await page.getByRole("button", { name: "Delete group", exact: true }).click();
  await expect(page.getByRole("alertdialog")).toContainText(
    "its saved members, and its added group sources",
  );
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  expect(state.groups).toHaveLength(2);
  await page.getByRole("button", { name: "Delete group", exact: true }).click();
  await page
    .getByRole("alertdialog")
    .getByRole("button", { name: "Delete group", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Groups", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Add group", exact: true }).click();
  await page.getByLabel("Lark group ID", { exact: true }).fill("oc_platform");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("alert")).toContainText("already registered");
});
test("admins edit and save group tool scopes", async ({ page, state }) => {
  await page.goto("/dashboard/groups/oc_platform");
  await expect(
    page.getByRole("heading", { name: "Tools", exact: true }),
  ).toBeVisible();
  expect(
    state.requests.some(
      (request) => request.path === "/tools" && request.method === "GET",
    ),
  ).toBe(true);
  const queryTool = page.getByRole("checkbox", { name: /run_query/ });
  const globallyOff = page.getByRole("checkbox", { name: /workflow_info/ });
  await expect(globallyOff).toBeEnabled();
  await expect(globallyOff).not.toBeChecked();
  await expect(page.getByText(/Off for everyone/)).toBeVisible();
  await queryTool.check();
  expect(state.groups[0].tools).toEqual(["connection_status"]);
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator(".success-line")).toContainText(
    "Group tools saved.",
  );
  expect(state.groups[0].tools).toEqual(["connection_status", "run_query"]);
  expect(
    state.requests.some(
      (request) =>
        request.path === "/groups/oc_platform/tools" &&
        request.method === "PUT" &&
        JSON.stringify(request.body.tools) ===
          JSON.stringify(["connection_status", "run_query"]),
    ),
  ).toBe(true);
  await expect(queryTool).toBeChecked();
  const invalid = await page.evaluate(async () => {
    const response = await fetch("/v1/dashboard/groups/oc_platform/tools", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tools: ["not_scopable"] }),
    });
    return { status: response.status, body: await response.json() };
  });
  expect(invalid).toEqual({
    status: 400,
    body: { error: "invalid_tool" },
  });
});
test("a saved tool that is off for everyone is shown as saved and can be removed", async ({
  page,
  state,
}) => {
  state.groups[0].tools = ["connection_status", "workflow_info"];
  await page.goto("/dashboard/groups/oc_platform");
  const globallyOff = page.getByRole("checkbox", { name: /workflow_info/ });
  await expect(globallyOff).toBeChecked();
  await expect(page.getByText(/no effect until it is turned on/)).toBeVisible();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator(".success-line")).toContainText(
    "Group tools saved.",
  );
  expect(state.groups[0].tools).toEqual(["connection_status", "workflow_info"]);
  await globallyOff.uncheck();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator(".success-line")).toContainText(
    "Group tools saved.",
  );
  expect(state.groups[0].tools).toEqual(["connection_status"]);
});
test("group tool editor reports an admin_required response", async ({
  page,
  state,
}) => {
  await page.goto("/dashboard/groups/oc_platform");
  await page.getByRole("checkbox", { name: /run_query/ }).check();
  state.role = "manager";
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Only admins can manage managers or change group tools.",
  );
  expect(state.groups[0].tools).toEqual(["connection_status"]);
});
test("tool scope actions stay in the draft until Save", async ({
  page,
  state,
}) => {
  await page.goto("/dashboard/groups/oc_platform");
  const enabled = page.getByRole("checkbox", { name: /run_query/ });
  const original = [...state.groups[0].tools];
  await page.getByRole("button", { name: "Disable all" }).click();
  expect(state.groups[0].tools).toEqual(original);
  await expect(page.getByText(/No tools are enabled/)).toBeVisible();
  await page.getByRole("button", { name: "Enable all" }).click();
  expect(state.groups[0].tools).toEqual(original);
  await enabled.uncheck();
  await page.getByRole("button", { name: "Reset to default" }).click();
  expect(state.groups[0].tools).toEqual(original);
  await expect(
    page.getByRole("checkbox", { name: /connection_status/ }),
  ).toBeChecked();
  // The defaults include a tool that is off for everyone; the list shows it and Save sends it.
  await expect(
    page.getByRole("checkbox", { name: /workflow_info/ }),
  ).toBeChecked();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator(".success-line")).toContainText(
    "Group tools saved.",
  );
  expect(state.groups[0].tools).toEqual(["connection_status", "workflow_info"]);
  expect(
    state.requests.some(
      (request) =>
        request.path === "/groups/oc_platform/tools" &&
        request.method === "PUT",
    ),
  ).toBe(true);
});
test("group tool kinds show whole-kind enabled counts and a whole-list summary", async ({
  page,
  state,
}) => {
  await page.goto("/dashboard/groups/oc_platform");
  await expect(
    page.getByText("1 of 16 enabled", { exact: true }),
  ).toBeVisible();
  const builtin = page.getByRole("button", { name: /Builtin.*1 of 2 enabled/ });
  await expect(builtin).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("button", { name: /^Mc/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Workflow/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Gitlab/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Lark/ })).toBeVisible();
});

test("tool search and status filters refine the visible lens", async ({
  page,
  state,
}) => {
  await page.goto("/dashboard/groups/oc_platform");
  await page.getByLabel("Search tools").fill("synthetic document");
  await expect(
    page.getByRole("checkbox", { name: /read_document/ }),
  ).toBeVisible();
  await expect(
    page.getByRole("checkbox", { name: /inspect_sheet/ }),
  ).toBeHidden();
  await page.getByLabel("Search tools").fill("");
  await page.getByRole("radio", { name: "Disabled" }).check();
  await expect(page.getByRole("checkbox", { name: /run_query/ })).toBeVisible();
  await expect(
    page.getByRole("checkbox", { name: /connection_status/ }),
  ).toBeHidden();
});

test("saving a filtered tool view preserves unseen enabled selections", async ({
  page,
  state,
}) => {
  state.groups[0].tools = [
    "connection_status",
    "workflow_info",
    "read_document",
  ];
  await page.goto("/dashboard/groups/oc_platform");
  await page.getByRole("radio", { name: "Enabled" }).check();
  await expect(page.getByRole("checkbox", { name: /run_query/ })).toBeHidden();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator(".success-line")).toContainText(
    "Group tools saved.",
  );
  expect(state.groups[0].tools).toEqual([
    "connection_status",
    "read_document",
    "workflow_info",
  ]);
  const request = state.requests.find(
    (item) =>
      item.path === "/groups/oc_platform/tools" && item.method === "PUT",
  );
  expect(request?.body.tools).toEqual([
    "connection_status",
    "read_document",
    "workflow_info",
  ]);
});

test("saving while a kind is collapsed keeps the whole draft", async ({
  page,
  state,
}) => {
  state.groups[0].tools = ["connection_status", "run_query", "read_document"];
  await page.goto("/dashboard/groups/oc_platform");
  const mc = page.getByRole("button", { name: /Mc.*1 of 4 enabled/ });
  await mc.click();
  await expect(mc).toHaveAttribute("aria-expanded", "false");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator(".success-line")).toContainText(
    "Group tools saved.",
  );
  expect(state.groups[0].tools).toEqual([
    "connection_status",
    "read_document",
    "run_query",
  ]);
});

test("kind sections operate from the keyboard", async ({ page, state }) => {
  await page.goto("/dashboard/groups/oc_platform");
  const builtin = page.getByRole("button", { name: /Builtin.*1 of 2 enabled/ });
  await builtin.focus();
  await page.keyboard.press("Enter");
  await expect(builtin).toHaveAttribute("aria-expanded", "false");
  await expect(
    page.getByRole("checkbox", { name: /connection_status/ }),
  ).toBeHidden();
  await page.keyboard.press("Space");
  await expect(builtin).toHaveAttribute("aria-expanded", "true");
  await expect(
    page.getByRole("checkbox", { name: /connection_status/ }),
  ).toBeVisible();
});

test("managers see disabled group tool controls and receive server authorization errors", async ({
  page,
  state,
}) => {
  state.role = "manager";
  await page.goto("/dashboard/groups/oc_platform");
  await expect(
    page.getByText(
      "Managers can view group tools but only admins can change them.",
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("checkbox", { name: /connection_status/ }),
  ).toBeDisabled();
  for (const label of [
    "Enable all",
    "Disable all",
    "Reset to default",
    "Save",
  ]) {
    await expect(
      page.getByRole("button", { name: label, exact: true }),
    ).toBeDisabled();
  }
  const result = await page.evaluate(async () => {
    const response = await fetch("/v1/dashboard/groups/oc_platform/tools", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tools: [] }),
    });
    return { status: response.status, body: await response.json() };
  });
  expect(result).toEqual({
    status: 403,
    body: { error: "admin_required" },
  });
});
test("two models retain totals, server pagination and complete all-history export", async ({
  page,
  state,
}) => {
  await page.goto("/dashboard/usage");
  await page
    .getByRole("button", { name: "Expand alex@example.com", exact: true })
    .click();
  const breakdown = page.getByRole("table", {
    name: "Historical models for alex@example.com",
  });
  await expect(breakdown.getByRole("row", { name: /model-a/ })).toContainText(
    "160",
  );
  await expect(breakdown.getByRole("row", { name: /model-b/ })).toContainText(
    "240",
  );
  await expect(page.getByText("1-50 of 60", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.getByText("51-60 of 60", { exact: true })).toBeVisible();
  await page.getByLabel("Period · UTC").selectOption("all");
  await page.getByRole("button", { name: "Apply filters" }).click();
  await expect(page.getByText("1-50 of 60", { exact: true })).toBeVisible();
  const ready = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export all matching data" }).click();
  const file = await ready;
  const stream = await file.createReadStream();
  let content = "";
  for await (const chunk of stream!) content += chunk;
  expect(content.split("\n")).toHaveLength(62);
  const request = state.requests.find((r) => r.path === "/usage/export")!;
  expect(Object.fromEntries(request.params)).toEqual({
    dataset: "users",
    all_time: "true",
  });
  await expect(page.getByText("61 matching rows exported")).toBeVisible();
  expect(
    state.requests
      .filter((r) => r.path.startsWith("/directory"))
      .every(
        (r) =>
          Object.entries(r.body)
            .filter(([key]) => ["emails", "owners", "group_ids"].includes(key))
            .reduce(
              (count, [, value]) => count + (value as string[]).length,
              0,
            ) <= 20,
      ),
  ).toBe(true);
});
test("pending, stale, missing and broken metadata degrade without blocking policies", async ({
  page,
  state,
}) => {
  state.metadata = "pending";
  await page.goto("/dashboard/groups/oc_platform");
  await expect(page.getByText("Alex Morgan", { exact: true })).toBeVisible();
  expect(state.pollCount).toBeGreaterThan(2);
  state.metadata = "stale";
  state.brokenImage = true;
  await page.reload();
  await expect(page.getByText("Last-known profile").first()).toBeVisible();
  await expect(
    page.getByRole("radio", { name: /Specific users/ }),
  ).toBeEnabled();
  state.metadata = "not_found";
  await page.reload();
  await expect(page.getByText("Not found in directory").first()).toBeVisible();
  await expect(
    page.getByText("alex@example.com", { exact: true }),
  ).toBeVisible();
});
test("dataset filters, missing values, errors and empty states", async ({
  page,
  state,
}) => {
  await page.goto("/dashboard/usage");
  await page
    .getByRole("button", { name: "query activity", exact: true })
    .click();
  await expect(
    page.getByRole("table").getByText("incomplete", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("cell", { name: "Unavailable", exact: true }),
  ).toHaveCount(3);
  await page.getByText("More filters", { exact: true }).click();
  await page.getByLabel("Outcome", { exact: true }).selectOption("incomplete");
  await page.getByRole("button", { name: "Apply filters" }).click();
  await expect
    .poll(() =>
      state.requests
        .filter((r) => r.path === "/usage/queries")
        .at(-1)
        ?.params.get("status"),
    )
    .toBe("incomplete");
  await page.getByRole("button", { name: "Sessions", exact: true }).click();
  await expect(
    page.getByRole("table").getByText("Usage unavailable", { exact: true }),
  ).toBeVisible();
  expect(
    state.requests
      .filter((r) => r.path === "/usage/llm")
      .at(-1)
      ?.params.has("status"),
  ).toBe(false);
  state.failUsage = true;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  state.failUsage = false;
  state.empty = true;
  await page.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "No matching records" }),
  ).toBeVisible();
});
test("daily pagination identifies partial history and uses server summary", async ({
  page,
  state,
}) => {
  await page.goto("/dashboard/");
  await expect(page.getByText("1.3M", { exact: true })).toBeVisible();
  await page.getByLabel("Period · UTC").selectOption("all");
  await page.getByRole("button", { name: "Apply filters" }).click();
  await expect(page.getByText("UTC · Recorded days 1-50 of 60")).toBeVisible();
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.getByText("UTC · Recorded days 51-60 of 60")).toBeVisible();
  expect(state.requests.some((r) => r.path === "/usage/summary")).toBe(true);
});
test("keyboard controls, mobile layout and accessibility", async ({
  page,
  state,
}) => {
  void state;
  await page.goto("/dashboard/groups/oc_platform");
  await expect(page.getByRole("button", { name: "Add member" })).toBeVisible();
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  expect(results.violations).toEqual([]);
  await page.getByRole("radio", { name: /Specific users/ }).focus();
  await page.keyboard.press("ArrowLeft");
  await expect(page.getByRole("radio", { name: /All users/ })).toBeChecked();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.getByRole("button", { name: "Open navigation" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
});
test("dashboard screenshots with synthetic fixtures", async ({
  page,
  state,
}, info) => {
  state.loggedIn = true;
  test.skip(
    info.project.name !== "chromium" ||
      process.env.LARKAGENTD_SCREENSHOTS !== "1",
  );
  await page.setViewportSize({ width: 1440, height: 900 });
  const capture = async (name: string, selector?: string) => {
    await page.evaluate(() => {
      const label = document.createElement("div");
      label.textContent = "SYNTHETIC DATA";
      label.style.cssText =
        "position:fixed;bottom:12px;right:16px;background:#15243c;color:white;padding:6px 9px;font:11px system-ui;z-index:100;border-radius:4px";
      document.body.append(label);
    });
    const path = `../docs/screenshots/${name}.png`;
    if (selector)
      await page.locator(selector).screenshot({ path, animations: "disabled" });
    else await page.screenshot({ path, animations: "disabled" });
  };
  await page.goto("/dashboard/");
  await expect(page.getByText("1.3M", { exact: true })).toBeVisible();
  await capture("overview");
  await page.goto("/dashboard/groups");
  await expect(
    page.getByText("Platform Engineering", { exact: true }),
  ).toBeVisible();
  await capture("groups");

  await page.route("**/v1/dashboard/auth/session", (route) =>
    route.fulfill({
      json: {
        email: "owner@example.com",
        role: "group_admin",
        group_id: "oc_platform",
        expires_at: Date.now() / 1000 + 900,
      },
    }),
  );
  await page.route("**/knowledge/sources", (route) =>
    route.fulfill({
      json: {
        sources: [
          {
            id: "12345",
            name: "Launch checklist",
            markdown: "Confirm rollout owners and publish the pilot dates.",
            server: null,
            uri: null,
          },
        ],
        mcp_servers: [],
      },
    }),
  );
  await page.route("**/knowledge/learned", (route) =>
    route.fulfill({
      json: {
        group_name: "Platform Engineering",
        learning: true,
        state: "enabled",
        next: null,
        records: [
          {
            status: "verified",
            activation: "source_checked",
            sources: [
              {
                source_id: "pilot-note",
                time: "2026-01-05T09:00:00Z",
                excerpt: "We agreed to invite 30 people to the first pilot.",
                url: null,
              },
            ],
            id: "pilot",
            category: "decision",
            content: "The pilot will start with 30 participants.",
          },
        ],
      },
    }),
  );
  await page.goto("/dashboard/");
  await expect(
    page.getByText("The pilot will start with 30 participants."),
  ).toBeVisible();
  await capture("group-knowledge");
});

test("group rows open with pointer and keyboard while copy and badges stay independent", async ({
  page,
  state,
}) => {
  await page.goto("/dashboard/groups");
  const row = page.locator(".group-row").first();
  await row
    .getByRole("button", { name: "Specific users", exact: true })
    .hover();
  await expect(page.getByRole("tooltip")).toContainText(
    "saved members and inherited",
  );
  await row
    .getByRole("button", { name: "Specific users", exact: true })
    .click();
  await expect(page).toHaveURL(/dashboard\/groups$/);
  await row.getByRole("button", { name: "Copy oc_platform" }).click();
  await expect(page).toHaveURL(/dashboard\/groups$/);
  await row.click({ position: { x: 450, y: 25 } });
  await expect(page).toHaveURL(/groups\/oc_platform$/);
  await page.getByRole("link", { name: "All groups" }).click();
  await page.getByRole("link", { name: /Open .* group settings/ }).focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/groups\/oc_platform$/);
  expect(state.groups).toHaveLength(1);
});

test("search suggests only an unregistered group ID and seeds the add modal", async ({
  page,
  state,
}) => {
  await page.goto("/dashboard/groups");
  await page.getByLabel("Find a registered group").fill("unmatched words");
  await expect(
    page.getByRole("button", { name: "Add this group" }),
  ).toHaveCount(0);
  await page.getByLabel("Find a registered group").fill("  oc_new  ");
  await page.getByRole("button", { name: "Add this group" }).click();
  await expect(page.getByLabel("Lark group ID", { exact: true })).toHaveValue(
    "oc_new",
  );
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(
    page.getByRole("dialog").getByText("Platform Engineering"),
  ).toBeVisible();
  expect(
    state.requests.filter((r) => r.path === "/groups" && r.method === "POST"),
  ).toHaveLength(0);
  await page.getByRole("button", { name: "Close add dialog" }).click();
  await page.getByRole("button", { name: "Add group", exact: true }).click();
  await expect(page.getByLabel("Lark group ID", { exact: true })).toHaveValue(
    "",
  );
});

test("member review preserves email, handles duplicate inherited grants and failed writes", async ({
  page,
  state,
}) => {
  await page.goto("/dashboard/groups/oc_platform");
  await expect(page.getByLabel("Email address", { exact: true })).toHaveCount(
    0,
  );
  await page.getByRole("button", { name: "Add member", exact: true }).click();
  await page
    .getByLabel("Email address", { exact: true })
    .fill("admin@example.com");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "already inherits access",
  );
  await page
    .getByLabel("Email address", { exact: true })
    .fill("alex@example.com");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("alert")).toContainText("already a saved member");
  await page
    .getByLabel("Email address", { exact: true })
    .fill("New@Example.com");
  await page.getByRole("button", { name: "Continue" }).click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByText("Example Person", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByText("new@example.com", { exact: true }).first(),
  ).toBeVisible();
  expect(state.groups[0].members).toEqual(["alex@example.com"]);
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  state.failWrite = true;
  await dialog.getByRole("button", { name: "Add member", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText(
    "temporarily unavailable",
  );
  await expect(
    dialog.getByRole("heading", { name: "Confirm person" }),
  ).toBeVisible();
  state.failWrite = false;
  await dialog.getByRole("button", { name: "Add member", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(state.groups[0].members).toContain("new@example.com");
});

test("people adds managers only after review and explains protected roles on focus and tap", async ({
  page,
  state,
}) => {
  await page.goto("/dashboard/people");
  const info = page.getByRole("button", { name: "About configured admins" });
  await info.focus();
  await expect(page.getByRole("tooltip")).toContainText("configuration change");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("tooltip")).toHaveCount(0);
  await page.getByRole("button", { name: "Manager", exact: true }).click();
  await expect(page.getByRole("tooltip")).toContainText(
    "cannot manage admins or other managers",
  );
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Add manager", exact: true }).click();
  await page
    .getByLabel("Email address", { exact: true })
    .fill("next-manager@example.com");
  await page.getByRole("button", { name: "Continue" }).click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByText("Example Person", { exact: true }),
  ).toBeVisible();
  expect(
    state.requests.filter(
      (r) => r.path.startsWith("/managers/") && r.method === "PUT",
    ),
  ).toHaveLength(0);
  await dialog
    .getByRole("button", { name: "Add manager", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  expect(
    state.requests.some(
      (r) =>
        r.path === "/managers/next-manager%40example.com" && r.method === "PUT",
    ),
  ).toBe(true);
});

test("identity review acknowledges a different directory email without rewriting the grant", async ({
  page,
  state,
}) => {
  await page.goto("/dashboard/groups/oc_platform");
  await page.getByRole("button", { name: "Add member", exact: true }).click();
  const input = page.getByLabel("Email address", { exact: true });
  await expect(input).toBeFocused();
  await input.fill("alias@example.com");
  await page.getByRole("button", { name: "Continue" }).click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByText("Directory: preferred@example.com"),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "Add member", exact: true }),
  ).toBeDisabled();
  await dialog.getByRole("checkbox").check();
  await dialog.getByRole("button", { name: "Edit email" }).click();
  await input.fill("different@example.com");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(
    dialog.getByText("different@example.com", { exact: true }).first(),
  ).toBeVisible();
  await expect(
    dialog.getByText("Directory: preferred@example.com"),
  ).toHaveCount(0);
  await dialog.getByRole("button", { name: "Edit email" }).click();
  await input.fill("alias@example.com");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(dialog.getByRole("checkbox")).not.toBeChecked();
  await dialog.getByRole("checkbox").check();
  await dialog.getByRole("button", { name: "Add member", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(state.groups[0].members).toContain("alias@example.com");
  expect(state.groups[0].members).not.toContain("preferred@example.com");
  expect(state.groups[0].members).not.toContain("different@example.com");
});

test("Group Brain settings stay isolated between registered groups", async ({
  page,
  state,
}) => {
  state.groups.push({ ...state.groups[0], group_id: "oc_second" });
  await page.goto("/dashboard/groups/oc_platform?view=knowledge");
  await expect(page.locator(".group-brain-toggle input")).not.toBeChecked();
  await expect(page.locator(".group-brain-status")).toContainText(
    "Configured: disabled",
  );
  await expect(page.locator(".group-brain-status")).toContainText(
    "Effective: disabled",
  );
  await expect(
    page
      .locator(".group-brain")
      .getByRole("button", { name: "Save learning settings" }),
  ).toBeDisabled();
  await page.goto("/dashboard/groups/oc_second?view=knowledge");
  await expect(page.locator(".group-brain-toggle input")).not.toBeChecked();
  expect(state.knowledge.oc_platform.version).toBe(0);
  expect(state.knowledge.oc_second.configured_enabled).toBe(false);
  expect(
    state.requests.some(
      (request) =>
        request.path === "/groups/oc_second/knowledge" &&
        request.method === "GET",
    ),
  ).toBe(true);
});

test("Group Brain enablement requires references and explicit sharing acknowledgement", async ({
  page,
  state,
}) => {
  await page.goto("/dashboard/groups/oc_platform?view=knowledge");
  const enable = page.locator(".group-brain-toggle input");
  await enable.focus();
  await page.keyboard.press("Space");
  const dialog = page.getByRole("dialog", { name: "Enable group learning?" });
  await expect(dialog.getByLabel("Notice reference")).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(enable).toBeFocused();
  await page.keyboard.press("Space");
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("save useful facts");
  await expect(dialog).toContainText("Only members of this group");
  await expect(dialog).toContainText(
    "Source-backed facts can become active automatically",
  );
  const confirm = dialog.getByRole("button", { name: "Confirm enablement" });
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel("Notice reference").fill("notice_example");
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel("Policy reference").fill("policy_example");
  await expect(confirm).toBeDisabled();
  await dialog.getByRole("checkbox").check();
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(enable).toBeChecked();
  await expect(
    page.getByText("Passive learning.", { exact: true }),
  ).toBeVisible();
  await expect(page.locator(".group-brain").getByRole("radio")).toHaveCount(0);
  await page
    .locator(".group-brain")
    .getByRole("button", { name: "Save learning settings" })
    .click();
  await expect(page.locator(".group-brain")).toContainText(
    "Learning settings saved.",
  );
  const request = state.requests.find(
    (item) =>
      item.path === "/groups/oc_platform/knowledge" && item.method === "PATCH",
  );
  expect(request?.body).toEqual({
    version: 0,
    enabled: true,
    notice_ref: "notice_example",
    policy_ref: "policy_example",
    acknowledge_group_sharing: true,
  });
  expect(state.knowledge.oc_platform.configured_enabled).toBe(true);
  expect(state.knowledge.oc_platform.version).toBe(1);
});

test("Group Brain reflows without page overflow on a narrow viewport", async ({
  page,
  state,
}) => {
  await page.goto("/dashboard/groups/oc_platform?view=knowledge");
  await expect(
    page.getByRole("heading", { name: "Learning settings" }),
  ).toBeVisible();
  expect(
    state.requests.some(
      (request) =>
        request.path === "/groups/oc_platform/knowledge" &&
        request.method === "GET",
    ),
  ).toBe(true);
  await page.setViewportSize({ width: 360, height: 800 });
  const sizes = await page.evaluate(() => ({
    page: document.documentElement.scrollWidth,
    viewport: document.documentElement.clientWidth,
  }));
  expect(sizes.page).toBeLessThanOrEqual(sizes.viewport);
});

test("Group Brain effective state explains a globally disabled configured group", async ({
  page,
  state,
}) => {
  state.knowledge.oc_platform = {
    ...state.knowledge.oc_platform,
    configured_enabled: true,
    effective_state: "degraded",
    reason_codes: ["global_disabled"],
    notice_ref: "notice_example",
    policy_ref: "policy_example",
  };
  await page.goto("/dashboard/groups/oc_platform?view=knowledge");
  await expect(page.locator(".group-brain-status")).toContainText(
    "Configured: enabled",
  );
  await expect(page.locator(".group-brain-status")).toContainText(
    "Effective: not active",
  );
  await expect(page.locator(".group-brain-status")).toContainText(
    "Disabled globally",
  );
});

test("Group Brain version conflict refreshes the baseline without losing the draft", async ({
  page,
  state,
}) => {
  state.knowledge.oc_platform = {
    ...state.knowledge.oc_platform,
    configured_enabled: true,
    effective_state: "enabled",
    reason_codes: [],
    notice_ref: "notice_example",
    policy_ref: "policy_example",
  };
  state.conflictNextKnowledgeSave = true;
  await page.goto("/dashboard/groups/oc_platform?view=knowledge");
  const recall = page.getByRole("checkbox", { name: "Conversation recall" });
  await recall.uncheck();
  await page
    .locator(".group-brain")
    .getByRole("button", { name: "Save learning settings" })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "Someone else changed learning settings",
  );
  await expect(recall).not.toBeChecked();
  expect(state.knowledge.oc_platform.capabilities.recall).toBe(true);
  const request = state.requests.find(
    (item) =>
      item.path === "/groups/oc_platform/knowledge" && item.method === "PATCH",
  );
  expect(request?.body).toEqual({ version: 0, recall_enabled: false });
  expect(
    state.requests.some(
      (item) =>
        item.path === "/groups/oc_platform/knowledge" && item.method === "GET",
    ),
  ).toBe(true);
});

test("Group Brain pause and resume send the current version", async ({
  page,
  state,
}) => {
  state.knowledge.oc_platform = {
    ...state.knowledge.oc_platform,
    configured_enabled: true,
    effective_state: "enabled",
    reason_codes: [],
    notice_ref: "notice_example",
    policy_ref: "policy_example",
  };
  await page.goto("/dashboard/groups/oc_platform?view=knowledge");
  await page
    .locator(".group-brain")
    .getByRole("button", { name: "Pause" })
    .click();
  await expect(page.locator(".group-brain-status")).toContainText(
    "Effective: paused",
  );
  await page
    .locator(".group-brain")
    .getByRole("button", { name: "Resume" })
    .click();
  await expect(page.locator(".group-brain-status")).toContainText(
    "Effective: active",
  );
  expect(
    state.requests
      .filter(
        (item) =>
          item.path.endsWith("/knowledge/pause") ||
          item.path.endsWith("/knowledge/resume"),
      )
      .map((item) => item.body.version),
  ).toEqual([0, 1]);
});

test("Group Brain purge needs the exact group ID and disables the panel while purging", async ({
  page,
  state,
}) => {
  state.knowledge.oc_platform = {
    ...state.knowledge.oc_platform,
    configured_enabled: true,
    effective_state: "enabled",
    reason_codes: [],
    notice_ref: "notice_example",
    policy_ref: "policy_example",
  };
  await page.goto("/dashboard/groups/oc_platform?view=knowledge");
  await page
    .locator(".group-brain")
    .getByRole("button", { name: "Purge" })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Purge conversation knowledge?",
  });
  await expect(dialog).toContainText(
    "permanently removes the group’s discussion history",
  );
  await expect(dialog).toContainText("Backups follow the retention policy");
  const purge = dialog.getByRole("button", {
    name: "Purge conversation knowledge",
  });
  await expect(purge).toBeDisabled();
  await dialog.getByLabel(/Type this group's chat ID/).fill("oc_other");
  await expect(purge).toBeDisabled();
  await dialog.getByLabel(/Type this group's chat ID/).fill("oc_platform");
  await purge.click();
  await expect(page.locator(".group-brain-status")).toContainText(
    "Effective: purging",
  );
  const request = state.requests.find(
    (item) =>
      item.path === "/groups/oc_platform/knowledge/purge" &&
      item.method === "POST",
  );
  expect(request?.body).toEqual({ version: 0, confirm: "oc_platform" });
  await expect(page.locator(".group-brain-toggle input")).toBeDisabled();
  await expect(
    page
      .locator(".group-brain")
      .getByRole("button", { name: "Save learning settings" }),
  ).toBeDisabled();
  await expect(
    page.locator(".group-brain").getByRole("button", { name: "Purge" }),
  ).toBeDisabled();
});

test("Group Brain managers can view metadata but cannot write", async ({
  page,
  state,
}) => {
  state.role = "manager";
  await page.goto("/dashboard/groups/oc_platform?view=knowledge");
  await page.getByText("Jobs and audit history", { exact: true }).click();
  await expect(page.locator(".group-brain-readonly")).toContainText(
    "Workspace admins can change them",
  );
  await expect(page.locator(".group-brain-toggle input")).toBeDisabled();
  await expect(
    page
      .locator(".group-brain")
      .getByRole("button", { name: "Save learning settings" }),
  ).toBeDisabled();
  await expect(
    page.locator(".group-brain-lists").getByRole("button", { name: "Retry" }),
  ).toBeDisabled();
  expect(
    state.requests.filter(
      (request) =>
        request.path.includes("/knowledge") &&
        ["PATCH", "POST"].includes(request.method),
    ),
  ).toHaveLength(0);
});

test("Group Brain API fixture rejects manager mutations", async ({
  page,
  state,
}) => {
  state.role = "manager";
  await page.goto("/dashboard/groups/oc_platform?view=knowledge");
  const result = await page.evaluate(async () => {
    const response = await fetch("/v1/dashboard/groups/oc_platform/knowledge", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version: 0, enabled: true }),
    });
    return { status: response.status, body: await response.json() };
  });
  expect(result).toEqual({
    status: 403,
    body: { error: "admin_required" },
  });
  expect(state.knowledge.oc_platform.configured_enabled).toBe(false);
});

test("Group Brain job retry and metadata-only audit pagination work", async ({
  page,
  state,
}) => {
  await page.goto("/dashboard/groups/oc_platform?view=knowledge");
  await page.getByText("Jobs and audit history", { exact: true }).click();
  await expect(page.getByText(/index · failed · 2 attempts/)).toBeVisible();
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.locator(".success-line")).toContainText("Retry queued.");
  expect(state.knowledgeJobs.oc_platform[0].state).toBe("queued");
  expect(
    state.requests.some(
      (request) =>
        request.path === "/groups/oc_platform/knowledge/jobs/2/retry" &&
        request.method === "POST" &&
        Object.keys(request.body).length === 0,
    ),
  ).toBe(true);
  await expect(page.getByText(/group_registered · success/)).toHaveCount(0);
  await page.getByRole("button", { name: "Load more" }).click();
  await expect(page.getByText(/group_registered · success/)).toBeVisible();
  expect(
    state.requests.some(
      (request) =>
        request.path === "/groups/oc_platform/knowledge/audit" &&
        request.params.get("before") === "2",
    ),
  ).toBe(true);
});

test("Group Brain purging state makes all write controls unavailable", async ({
  page,
  state,
}) => {
  state.knowledge.oc_platform = {
    ...state.knowledge.oc_platform,
    configured_enabled: false,
    effective_state: "purging",
    reason_codes: ["purging"],
  };
  await page.goto("/dashboard/groups/oc_platform?view=knowledge");
  await expect(page.locator(".group-brain-toggle input")).toBeDisabled();
  for (const label of ["Save", "Pause", "Purge"]) {
    await expect(
      page.locator(".group-brain").getByRole("button", { name: label }),
    ).toBeDisabled();
  }
  await expect(page.locator(".group-brain-status")).toContainText(
    "Purge is in progress",
  );
});

test("Group Brain approves a visible note only after sharing confirmation", async ({
  page,
  state,
}) => {
  state.knowledge.oc_platform = {
    ...state.knowledge.oc_platform,
    configured_enabled: true,
    effective_state: "enabled",
    policy_ref: "policy_example",
    counts: { ...state.knowledge.oc_platform.counts, pending_review: 1 },
  };
  state.knowledgeCandidates.oc_platform = [
    {
      memory_id: "mem_example0001",
      revision: 3,
      category: "decision",
      state: "candidate",
      content_hash: "sha256_example",
      proposed_by: "ou_alice0001",
      created_at: "2026-11-26T12:00:00Z",
      content: "Use the shared release checklist.",
      reason: "It keeps release steps consistent.",
    },
  ];
  await page.goto("/dashboard/groups/oc_platform?view=knowledge");
  const review = page.locator(".group-brain-review-lists");
  const approve = review.getByRole("button", { name: "Approve" });
  await expect(approve).toBeDisabled();
  await review
    .getByLabel("I confirm this note may be shared with everyone in this group")
    .check();
  await expect(approve).toBeEnabled();
  await approve.click();
  await expect(review).toContainText("No notes waiting for review.");
  await expect(page.locator(".group-brain")).toContainText("Note approved.");
  expect(
    state.requests.find(
      (request) =>
        request.path ===
          "/groups/oc_platform/knowledge/candidates/mem_example0001/approve" &&
        request.method === "POST",
    )?.body,
  ).toEqual({
    revision: 3,
    content_hash: "sha256_example",
    acknowledge_group_sharing: true,
  });
});

test("Group Brain keeps metadata-only notes rejectable without offering approval", async ({
  page,
  state,
}) => {
  state.knowledge.oc_platform = {
    ...state.knowledge.oc_platform,
    configured_enabled: true,
  };
  state.knowledgeCandidates.oc_platform = [
    {
      memory_id: "mem_example0002",
      revision: 1,
      category: "fact",
      state: "candidate",
      content_hash: "sha256_hidden",
      proposed_by: "ou_alice0001",
      created_at: "2026-11-26T12:00:00Z",
    },
  ];
  await page.goto("/dashboard/groups/oc_platform?view=knowledge");
  const review = page.locator(".group-brain-review-lists");
  await expect(review).toContainText(
    "Text is hidden because the reviewer cannot see its source.",
  );
  await expect(review.getByRole("button", { name: "Approve" })).toHaveCount(0);
  await expect(review.getByRole("button", { name: "Reject" })).toBeVisible();
});

test("Group Brain managers see review lists without action buttons", async ({
  page,
  state,
}) => {
  state.role = "manager";
  state.knowledge.oc_platform = {
    ...state.knowledge.oc_platform,
    configured_enabled: true,
  };
  state.knowledgeCandidates.oc_platform = [
    {
      memory_id: "mem_example0003",
      revision: 1,
      category: "fact",
      state: "candidate",
      content_hash: "sha256_manager",
      proposed_by: "ou_alice0001",
      created_at: "2026-11-26T12:00:00Z",
      content: "A visible group note.",
      reason: "For shared context.",
    },
  ];
  await page.goto("/dashboard/groups/oc_platform?view=knowledge");
  const review = page.locator(".group-brain-review-lists");
  await expect(review).toContainText("Notes to review");
  await expect(review.getByRole("button")).toHaveCount(0);
});

test("group facts show the cited message and require confirmation before removal", async ({
  page,
  state,
}) => {
  state.loggedIn = true;
  const fact = {
    id: "pilot",
    category: "decision",
    activation: "source_checked",
    status: "verified",
    content: "Use the blue template.",
    sources: [
      {
        source_id: "note",
        excerpt: "We agreed to use blue.",
        url: "https://applink.larksuite.com/client/message/open?messageId=om_example",
        time: "2026-01-01T10:00:00Z",
      },
    ],
  };
  let retired = false;
  await page.route("**/knowledge/learned", (route) =>
    route.fulfill({
      json: {
        group_name: "Test group",
        state: "enabled",
        learning: true,
        next: null,
        records: retired ? [] : [fact],
      },
    }),
  );
  await page.route("**/learned/pilot/retire", (route) => {
    retired = true;
    return route.fulfill({ json: { status: "revoked" } });
  });
  await page.goto("/dashboard/groups/oc_platform");
  await page
    .getByRole("navigation", { name: "Group sections" })
    .getByRole("link", { name: "Knowledge" })
    .click();
  await expect(page.getByText(fact.sources[0].excerpt)).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Open source message" }),
  ).toHaveAttribute("href", fact.sources[0].url);
  await page.getByRole("button", { name: "Stop using this fact" }).click();
  await page.getByRole("button", { name: "Cancel" }).click();
  expect(retired).toBe(false);
  await page.getByRole("button", { name: "Stop using this fact" }).click();
  await page
    .getByRole("alertdialog")
    .getByRole("button", { name: "Stop using this fact" })
    .click();
  await expect(page.getByText("No active facts on this page.")).toBeVisible();
});
