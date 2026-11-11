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
