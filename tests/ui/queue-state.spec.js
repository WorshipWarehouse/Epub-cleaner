"use strict";

const { test, expect } = require("@playwright/test");

const reviewFinding = {
  id: "review-1",
  rule: "branding.text_block",
  severity: "medium",
  severity_label: "medium",
  message: "Possible third-party mention",
  remediation: "review",
  target: "OEBPS/chapter.xhtml",
  confidence: 0.6,
};

const report = {
  summary: {
    verdict: "Review needed.",
    counts: { critical: 0, high: 0, medium: 1, low: 0, informational: 0 },
  },
  findings: [reviewFinding],
};

const cleanReport = {
  summary: { verdict: "Cleaned." },
  output: {
    download_url: "/api/download/job-1",
    name: "first.cleaned.epub",
    sha256: "abc",
  },
  removed: [],
  stats: {},
};

test("per-file review selections and background cleaning stay isolated", async ({ page }) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(String(error)));

  let inspectCount = 0;
  await page.route("**/api/transfers", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ transfers: [], storage: {} }) }),
  );
  await page.route("**/api/inspect", (route) => {
    inspectCount += 1;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        job_id: `job-${inspectCount}`,
        report,
        reviewable: [reviewFinding],
      }),
    });
  });

  let releaseClean;
  const cleanGate = new Promise((resolve) => {
    releaseClean = resolve;
  });
  await page.route("**/api/clean", async (route) => {
    await cleanGate;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ job_id: "job-1", report: cleanReport }),
    });
  });

  await page.goto("/");
  await page.setInputFiles("#file-input", [
    { name: "first.epub", mimeType: "application/epub+zip", buffer: Buffer.from("first") },
    { name: "second.epub", mimeType: "application/epub+zip", buffer: Buffer.from("second") },
  ]);
  await expect(page.locator("#file-queue .queue-item")).toHaveCount(2);

  await page.locator("#inspect-btn").click();
  await expect(page.locator('#file-queue .queue-item[data-status="ready"]')).toHaveCount(2);
  await expect(page.locator("#review-block")).toBeVisible();

  const firstCheckbox = page.locator("#review-list input[type=checkbox]").first();
  await firstCheckbox.check();
  await page.locator("#file-queue .queue-select").nth(1).click();
  await page.locator("#file-queue .queue-select").nth(0).click();
  await expect(page.locator("#review-list input[type=checkbox]").first()).toBeChecked();

  const cleanRequest = page.waitForRequest(
    (request) => request.url().includes("/api/clean") && request.method() === "POST",
  );
  await page.locator("#clean-btn").click();
  await cleanRequest;
  await page.locator("#file-queue .queue-select").nth(1).click();
  releaseClean();

  await expect(page.locator('#file-queue .queue-item[data-status="cleaned"]')).toHaveCount(1);
  await expect(page.locator("#cleaned")).toBeHidden();
  await expect(page.locator("#results")).toBeVisible();

  await page.locator("#file-queue .queue-select").nth(0).click();
  await expect(page.locator("#cleaned")).toBeVisible();
  await expect(page.locator("#results")).toBeHidden();
  expect(pageErrors).toEqual([]);
});

async function addFiles(page, names) {
  await page.setInputFiles("#file-input", names.map((name) => ({
    name, mimeType: "application/epub+zip", buffer: Buffer.from(name),
  })));
}

async function dropFile(page, name) {
  await page.evaluate((name) => {
    const transfer = new DataTransfer();
    transfer.items.add(new File([name], name));
    window.dispatchEvent(new DragEvent("drop", { dataTransfer: transfer, cancelable: true }));
  }, name);
}

async function inspectionResponse(route) {
  await route.fulfill({ json: { job_id: "job", report, reviewable: [reviewFinding] } });
}

test("queue focus, clean readiness, new selection and error actions", async ({ page }) => {
  let count = 0;
  await page.route("**/api/inspect", (route) => {
    count += 1;
    return count === 2 ? route.fulfill({ status: 500, json: { detail: "Failed" } }) : inspectionResponse(route);
  });
  await page.goto("/");
  await addFiles(page, ["first.epub", "failed.epub"]);
  await page.locator("#file-queue .queue-select").first().click();
  await expect(page.locator("#file-queue .queue-select").first()).toBeFocused();
  await page.evaluate(() => document.querySelector("#inspect-btn").click());
  await expect(page.getByRole("button", { name: "Retry", exact: true })).toBeVisible();
  await expect(page.locator("#clean-btn")).toBeEnabled();
  await expect(page.locator("#file-queue .queue-select").first()).toBeFocused();
  await page.locator("#review-list input").check();
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await page.locator("#inspect-btn").click();
  await expect(page.locator('[data-status="ready"]')).toHaveCount(2);
  await expect(page.locator("#review-list input")).toBeChecked();
  await addFiles(page, ["new.epub"]);
  await expect(page.locator("#results")).toBeHidden();
  await expect(page.locator("#cleaned")).toBeHidden();
});

test("inspection deadline releases later files and permits individual removal", async ({ page }) => {
  await page.clock.install();
  let count = 0;
  await page.route("**/api/inspect", (route) => {
    count += 1;
    if (count === 1) return; // The first upload never returns.
    return inspectionResponse(route);
  });
  await page.goto("/");
  await addFiles(page, ["stalled.epub", "next.epub"]);
  await page.locator("#inspect-btn").click();
  await expect.poll(() => count).toBe(1);
  await page.clock.fastForward(60_001);
  await expect(page.locator('[data-status="error"]')).toHaveCount(1);
  await expect(page.locator('[data-status="ready"]')).toHaveCount(1);
  await page.locator("#file-queue .queue-select").last().click();
  await page.locator("#review-list input").check();
  await page.getByRole("button", { name: "Remove", exact: true }).click();
  await expect(page.locator("#file-queue .queue-item")).toHaveCount(1);
  await expect(page.locator("#review-list input")).toBeChecked();
  await expect(page.locator("#clean-btn")).toBeEnabled();
});

test("drops during cleaning resume inspection and review controls recover on failure", async ({ page }) => {
  await page.route("**/api/inspect", inspectionResponse);
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  await page.route("**/api/clean", async (route) => {
    expect(route.request().postDataJSON().approved).toEqual(["review-1"]);
    await gate;
    await route.fulfill({ status: 500, json: { detail: "Try again" } });
  });
  await page.goto("/");
  await addFiles(page, ["first.epub"]);
  await page.locator("#inspect-btn").click();
  await expect(page.locator("#clean-btn")).toBeEnabled();
  await page.locator("#review-list input").check();
  await page.locator("#clean-btn").click();
  await expect(page.locator("#results")).toBeHidden();
  await dropFile(page, "later.epub");
  await expect(page.locator('[data-status="queued"]')).toHaveCount(1);
  release();
  await expect(page.locator('[data-status="ready"]')).toHaveCount(2);
  await page.locator("#file-queue .queue-select").first().click();
  await expect(page.locator("#review-list input")).toBeChecked();
  await expect(page.locator("#review-list input")).toBeEnabled();
  await expect(page.locator("#clean-btn")).toBeEnabled();
});

test("drops during inspection run after the current batch", async ({ page }) => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  let count = 0;
  await page.route("**/api/inspect", async (route) => {
    count += 1;
    if (count === 1) await gate;
    await inspectionResponse(route);
  });
  await page.goto("/");
  await dropFile(page, "first.epub");
  await expect(page.locator('[data-status="inspecting"]')).toHaveCount(1);
  await dropFile(page, "second.epub");
  await expect(page.locator('[data-status="queued"]')).toHaveCount(1);
  release();
  await expect(page.locator('[data-status="ready"]')).toHaveCount(2);
  await expect(page.locator("#clean-btn")).toBeEnabled();
});
